#!/usr/bin/env bash
# bootstrap-podman.sh — bring up the Epicurrents platform on a Podman host.
#
# The Podman counterpart to scripts/bootstrap.sh. The two differ in exactly two
# things: which prerequisites they install, and how compose is spelled — here
# `sudo -E podman compose`, there `docker compose`. Everything after the
# prerequisites is the same run, and lives in scripts/lib/bootstrap_plan.sh and
# scripts/lib/bootstrap_steps.sh so that it cannot drift between the two again.
# It did drift once: this script sat untouched through six changes to the Docker
# path and silently stopped cloning the active project, vendoring the Pyodide
# runtime, activating the project, generating lead fields, selecting the TLS
# proxy overlay, and guarding .env values against truncation.
#
# Backed by docker-compose v2 (Docker Inc.'s Go binary) talking to Podman over
# its Docker-compatible socket. Primarily exercised on RHEL 9; Rocky / Alma /
# Fedora paths are stubbed in but less-tested.
#
# Why docker-compose v2 and not podman-compose: podman-compose is a Python
# reimplementation that silently ignores parts of the Compose specification these
# files rely on. docker-compose v2 speaks the specification natively and works
# against the rootful podman socket.
#
# Why rootful: every service declares `user: "1000:1000"` against a bind mount of
# the deployment, and only a rootful runtime maps that uid to the tree's owner.
# Rootless Podman remaps it into the invoking user's subuid range, where the
# checkout is one the containers cannot write. So each compose call goes through
# sudo while the script itself still refuses to run as root.
#
# What it does:
#   1. Verifies it's on a RHEL-family host (RHEL, Rocky, Alma, Fedora).
#   2. Installs git if missing.
#   3. Installs Podman, docker-compose v2 as its compose provider, and enables
#      the rootful podman socket.
#   4-10. The shared bootstrap run — submodules, project clone, image build,
#      .env generation, frontend bundles, Pyodide vendoring, Borg repositories,
#      project activation, stack start, lead fields. See bootstrap_steps.sh.
#
# Two-pass on first setup, same as the Docker bootstrap:
#
#   ./scripts/bootstrap-podman.sh   # install prereqs, init submodules, build
#                                   # the image, write .env, exit.
#   $EDITOR .env                    # review and customise.
#   ./scripts/bootstrap-podman.sh   # build frontend, init borg, start stack.
#
# Flags: the same set scripts/bootstrap.sh accepts — --no-start and the
# --tailscale-* trio. See bootstrap_plan.sh.
#
# LIMITATIONS:
#   - Rootful mode only, for the reason above.
#   - Downloads docker-compose v2 from GitHub releases (no RHEL package ships
#     the upstream binary).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/.."

# shellcheck source=scripts/lib/progress.sh
. "$SCRIPT_DIR/lib/progress.sh"
# shellcheck source=scripts/lib/bootstrap_plan.sh
. "$SCRIPT_DIR/lib/bootstrap_plan.sh"

# Rootful Podman: every compose invocation goes through sudo. -E preserves the
# invoking user's environment so the pre-.env REDIS_PASSWORD placeholder and the
# rest of the shared steps' exports survive the transition.
COMPOSE="sudo -E podman compose"
COMPOSE_PROD="sudo -E podman compose -f docker-compose.yml -f docker-compose.prod.yml"

# The `OS_RELEASE_FILE` env override exists so the script tests can point at a
# fixture file instead of the host's real /etc/os-release.
OS_RELEASE_FILE="${OS_RELEASE_FILE:-/etc/os-release}"

bootstrap_parse_args "$@"
bootstrap_require_non_root
bootstrap_env_state

# ── Plan ─────────────────────────────────────────────────────────────────────
# The three prerequisite steps are this script's own; everything after them is
# shared with the Docker path.
progress_step distro "Check the host distribution"      direct
progress_step git    "Check git"                        direct
progress_step podman "Check Podman + compose provider"  direct
bootstrap_plan_common
bootstrap_begin

# ── 1. Distro detection ──────────────────────────────────────────────────────

step_distro() {
if [ ! -f "$OS_RELEASE_FILE" ]; then
    die "$OS_RELEASE_FILE not found — cannot identify the host distro."
fi
# shellcheck disable=SC1090
. "$OS_RELEASE_FILE"
DISTRO_ID="${ID:-unknown}"
DISTRO_LIKE="${ID_LIKE:-}"
DISTRO_VERSION="${VERSION_ID:-unknown}"

case "$DISTRO_ID" in
    rhel|rocky|almalinux|fedora|centos)
        ok "Detected ${PRETTY_NAME:-$DISTRO_ID $DISTRO_VERSION}"
        ;;
    *)
        # Allow generic "rhel-like" descendants via ID_LIKE.
        case "$DISTRO_LIKE" in
            *rhel*|*fedora*)
                warn "Distro $DISTRO_ID not directly supported; treating as RHEL-like."
                ;;
            *)
                die "This script targets RHEL-family hosts. Detected: $DISTRO_ID. \
Use scripts/bootstrap.sh for Debian/Ubuntu (Docker) instead."
                ;;
        esac
        ;;
esac
}
run_step distro step_distro

# ── 2. git ───────────────────────────────────────────────────────────────────

step_git() {
info "Checking git"
if command -v git &>/dev/null; then
    ok "Already installed: $(git --version)"
else
    info "Installing git"
    sudo dnf install -y -q git
    ok "Installed: $(git --version)"
fi
}
run_step git step_git

# ── 3. Podman + the compose provider ─────────────────────────────────────────

step_podman() {
info "Checking Podman"

if command -v podman &>/dev/null; then
    ok "Already installed: $(podman --version)"
else
    info "Installing Podman"
    sudo dnf install -y -q podman
    ok "Installed: $(podman --version)"
fi

PODMAN_VERSION="$(podman version --format '{{.Client.Version}}' 2>/dev/null || podman --version | awk '{print $NF}')"
ok "Podman $PODMAN_VERSION"

# `podman compose` (subcommand) is a thin shim that delegates to whichever
# compose backend it finds on PATH. The `podman-compose` Python wrapper
# (Podman's traditional companion) silently ignores `volume.subpath`,
# which this project uses to share one `data` volume across postgres /
# recordings / staging / celery / borg. Without subpath support postgres
# mounts the whole data volume root, sees the sibling subdirectories, and
# refuses initdb.
#
# docker-compose v2 (Docker Inc.'s Go binary) speaks the Compose
# Specification natively — including subpath — and talks to Podman over
# its Docker-compatible socket. Install it as the compose backend.

info "Installing docker-compose v2 as the compose backend"

# Remove podman-compose if a previous attempt installed it; otherwise it
# wins the provider preference order and silently re-introduces the
# subpath bug.
if command -v podman-compose &>/dev/null; then
    warn "Removing existing podman-compose to avoid backend conflicts"
    sudo dnf remove -y -q podman-compose 2>/dev/null || true
    # Sweep pip-installed copies (system + per-user) too — dnf removal
    # doesn't touch those, and a pip-installed binary on PATH would still
    # win the provider preference order.
    sudo pip uninstall -y podman-compose 2>/dev/null || true
    pip uninstall -y podman-compose 2>/dev/null || true
fi

if command -v docker-compose &>/dev/null; then
    ok "Already installed: $(docker-compose version --short 2>&1 | head -1)"
else
    # Pinned version + embedded checksum: an unverified `latest` binary
    # installed to a root-owned path is root-executed arbitrary code if
    # the release or the TLS path is ever compromised (this script
    # explicitly supports corporate MITM proxies). Bump the version and
    # both sums together from the release's checksums.txt.
    DC_VERSION="v5.1.4"
    case "$(uname -m)" in
        x86_64)  DC_SHA256="33b208d7e76639db742fae84b966cc01dacae58ca3fc4dabbc907045aefdf0c4" ;;
        aarch64) DC_SHA256="d4fb48b72857810314d3ee77123c89954101844efa4788031221f4c370495946" ;;
        *) die "No pinned docker-compose checksum for architecture $(uname -m); add it to this script from the ${DC_VERSION} release checksums.txt." ;;
    esac
    DC_URL="https://github.com/docker/compose/releases/download/${DC_VERSION}/docker-compose-linux-$(uname -m)"
    DC_TMP="$(mktemp)"
    curl -fsSL "$DC_URL" -o "$DC_TMP"
    echo "${DC_SHA256}  ${DC_TMP}" | sha256sum -c - >/dev/null \
        || die "docker-compose download failed checksum verification"
    sudo install -m 0755 "$DC_TMP" /usr/local/bin/docker-compose
    rm -f "$DC_TMP"
    # Some RHEL `sudo` configurations don't have /usr/local/bin in
    # secure_path, which would prevent `sudo podman compose` from finding
    # the binary. Mirror it into /usr/bin as a safety net.
    if ! sudo env | grep -E '^PATH=' | tr ':' '\n' | grep -q '/usr/local/bin'; then
        sudo cp /usr/local/bin/docker-compose /usr/bin/docker-compose
    fi
    ok "Installed: $(docker-compose version --short 2>&1 | head -1)"
fi

# Enable the rootful podman socket so docker-compose can drive podman
# over the Docker-compatible API. Idempotent.
info "Enabling rootful podman socket"
sudo systemctl enable --now podman.socket
ok "podman.socket active"

# Confirm `podman compose` now resolves to docker-compose, not whatever
# else might be lingering on PATH or pinned via containers.conf.
PROVIDER="$(podman compose version 2>&1 | head -1)"
case "$PROVIDER" in
    *Docker\ Compose*|*docker-compose*)
        ok "podman compose backend: $PROVIDER"
        ;;
    *)
        die "podman compose did not pick up docker-compose. Got: '$PROVIDER'. \
Check /etc/containers/containers.conf and ~/.config/containers/containers.conf \
for a pinned compose_providers entry."
        ;;
esac
}
run_step podman step_podman

# shellcheck source=scripts/lib/bootstrap_steps.sh
. "$SCRIPT_DIR/lib/bootstrap_steps.sh"
