#!/usr/bin/env bash
# bootstrap-podman.sh — bring up the Epicurrents platform on a Podman host.
#
# Mirrors scripts/bootstrap.sh but targets Podman instead of Docker Engine,
# backed by docker-compose v2 (Docker Inc.'s Go binary) talking to Podman
# over its Docker-compatible socket. Primarily exercised on RHEL 9; Rocky /
# Alma / Fedora paths are stubbed in but less-tested. Runs Podman *rootful*
# (`sudo podman …`); rootless support is ROADMAP'd alongside the entrypoint
# refactor.
#
# Why docker-compose v2 and not podman-compose: the project's compose files
# rely on `volume.subpath` to share one `data` volume across postgres /
# recordings / staging / celery / borg. podman-compose (Python wrapper)
# silently ignores `subpath`, which mounts the whole data volume into
# postgres and breaks initdb. docker-compose v2 speaks the Compose
# Specification natively, including subpath, and works against the rootful
# podman socket.
#
# What it does:
#   1. Verifies it's on a RHEL-family host (RHEL, Rocky, Alma, Fedora).
#   2. Installs git if missing.
#   3. Installs Podman if missing.
#   4. Installs docker-compose v2 as the compose backend, removes any
#      previously-installed podman-compose, and enables podman.socket.
#   5. Initialises submodules (viewer + docs).
#   6. Builds the Python image.
#   7. Generates .env (first run only) — pauses for the operator to review.
#   8. Builds the frontend bundles via the on-demand frontend-build service.
#   9. Initialises the local Borg backup repository (idempotent).
#  10. Starts the stack using the production compose overlay.
#
# Two-pass on first setup, same as the Docker bootstrap:
#
#   ./scripts/bootstrap-podman.sh   # install prereqs, init submodules, build
#                                   # the image, write .env, exit.
#   $EDITOR .env                    # review and customise.
#   ./scripts/bootstrap-podman.sh   # build frontend, init borg, start stack.
#
# Flags:
#   --no-start    Skip starting the stack at the end (steps 1–9 only).
#
# LIMITATIONS:
#   - Rootful mode only. Rootless support is ROADMAP'd alongside the
#     entrypoint refactor (search ROADMAP.md for "podman" or "entrypoint").
#   - Downloads docker-compose v2 from GitHub releases (no RHEL package
#     ships the upstream binary).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/.."

# Rootful Podman: every compose invocation goes through sudo. -E preserves
# the invoking user's environment so .env writes land with the right
# ownership when we chown them back below.
COMPOSE="sudo -E podman compose"
COMPOSE_PROD="sudo -E podman compose -f docker-compose.yml -f docker-compose.prod.yml"

# ── Helpers ──────────────────────────────────────────────────────────────────

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
info() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
ok()   { printf '    \033[32m✓\033[0m  %s\n'  "$*"; }
warn() { printf '    \033[33m!\033[0m  %s\n'  "$*"; }
die()  { printf '\n\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

# ── Arguments ────────────────────────────────────────────────────────────────

START=true
for arg in "$@"; do
    case "$arg" in
        --no-start) START=false ;;
        -h|--help)
            sed -n '2,46p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *) die "Unknown argument: $arg (try --help)" ;;
    esac
done

# ── 0. Sanity checks ─────────────────────────────────────────────────────────

[ "$(id -u)" -eq 0 ] \
    && die "Run this script as a regular user, not root. sudo will be used where needed."

# ── 1. Distro detection ──────────────────────────────────────────────────────

# The `OS_RELEASE_FILE` env override exists so the script tests can point
# at a fixture file instead of the host's real /etc/os-release.
OS_RELEASE_FILE="${OS_RELEASE_FILE:-/etc/os-release}"
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

# ── 2. git ───────────────────────────────────────────────────────────────────

info "Checking git"
if command -v git &>/dev/null; then
    ok "Already installed: $(git --version)"
else
    info "Installing git"
    sudo dnf install -y -q git
    ok "Installed: $(git --version)"
fi

# ── 3. Podman + podman-compose ───────────────────────────────────────────────

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

# ── 4. Submodules ────────────────────────────────────────────────────────────

info "Initialising submodules (viewer + docs)"
git submodule update --init --recursive
ok "Submodules ready"

# ── 4b. Dev tooling (git hooks + AI-tool symlinks) ───────────────────────────

if [ -z "${SKIP_DEV_TOOLS_INSTALL:-}" ]; then
    info "Installing dev tooling (git hooks + AI-tool symlinks)"
    bash scripts/install-dev-tools.sh
    ok "Dev tooling installed"
fi

# ── 5. Build the Python image ────────────────────────────────────────────────
# Needed before init_env can run (init_env executes inside the web image).

info "Building the Python image"
$COMPOSE build web
ok "Image built"

# ── 6. Generate .env (first run) ─────────────────────────────────────────────
# init_env auto-fills SECRET_KEY, BORG_PASSPHRASE, ADMIN_PASSWORD, the VAPID
# keypair, and the federation Ed25519 keypair. The web image's entrypoint
# blocks on postgres, so we bypass it with --entrypoint python. (See the
# 🟢 ROADMAP entry on making the entrypoint DB-aware for the durable fix.)
#
# Rootful Podman writes files as root by default; chown back to the
# invoking user so the operator can edit .env without sudo.

if [ ! -f .env ]; then
    info "Generating .env with random secrets"
    $COMPOSE run --rm --no-deps \
        --entrypoint python \
        web manage.py init_env
    sudo chown "$(id -u):$(id -g)" .env
    ok ".env created at $(pwd)/.env"

    echo
    bold "================================================================"
    bold " First-run pause — review .env before continuing"
    bold "================================================================"
    echo
    echo "A new .env has been generated with random secrets. Review and"
    echo "customise it now — at minimum:"
    echo
    echo "  DJANGO_MODE           (default: production)"
    echo "  EPICURRENTS_PROJECT   (active project name, blank = base platform)"
    echo "  EPICURRENTS_PLUGINS   (comma-separated plugin names, blank = none)"
    echo "  DB_NAME / DB_USERNAME / DB_PASSWORD"
    echo "  ADMIN_USERNAME / ADMIN_EMAIL"
    echo "  ALLOWED_HOSTS / FRONTEND_URL"
    echo "  FEDERATION_INSTANCE_URL  (only if enabling federation)"
    echo "  BORG_REMOTE_REPO         (only if using remote backup)"
    echo
    bold "Then re-run this script to complete the bootstrap:"
    echo "  ./scripts/bootstrap-podman.sh"
    echo
    exit 0
fi

ok ".env present"

# ── 6a. Plugin-conditional submodules ────────────────────────────────────────

ACTIVE_PLUGINS="$(grep -E '^EPICURRENTS_PLUGINS=' .env | head -1 | cut -d= -f2 | tr -d ' "'"'"'' || true)"
case ",$ACTIVE_PLUGINS," in
    *,dicom,*)
        info "Initialising OHIF viewer submodule (required by the dicom plugin)"
        git submodule update --init --checkout plugins/dicom/ohif-viewer
        ok "OHIF viewer ready"
        ;;
esac

# ── 7. Frontend bundles ──────────────────────────────────────────────────────

info "Building frontend bundles (Node container, ~3–5 min on first run)"
$COMPOSE --profile build run --rm frontend-build
ok "Frontend bundles built"

# ── 8. Initialise Borg backup repo (idempotent) ──────────────────────────────

info "Initialising local Borg backup repository"
# The local tier is optional; a deployment with an append-only remote may keep
# no second copy on the disk it is protecting. Mirrors scripts/bootstrap.sh.
if grep -qiE '^BACKUP_LOCAL_ENABLED=(0|false|no|off)[[:space:]]*$' .env 2>/dev/null; then
    echo "Local Borg repository disabled (BACKUP_LOCAL_ENABLED); skipping init."
elif $COMPOSE run --rm --entrypoint borg borg info /backup &>/dev/null; then
    ok "Borg repository already initialised"
else
    $COMPOSE run --rm --entrypoint borg borg init --encryption repokey /backup
    ok "Borg repository initialised"
fi

# ── 9. Start the stack ───────────────────────────────────────────────────────

if [ "$START" = true ]; then
    info "Starting the stack (production overlay)"
    $COMPOSE_PROD up -d
    ok "Stack is up"

    echo
    $COMPOSE_PROD ps
fi

# ── 10. Summary ──────────────────────────────────────────────────────────────

echo
bold "================================================================"
bold " Bootstrap complete (Podman)"
bold "================================================================"
echo

if [ ! -f "$HOME/.ssh/id_borg" ] && [ ! -f "$HOME/.ssh/id_borg.pub" ]; then
    echo "Optional — remote Borg backups:"
    echo "  ssh-keygen -t ed25519 -C 'borg@$(hostname)' -f ~/.ssh/id_borg -N ''"
    echo "  Then set BORG_SSH_KEY_PATH and BORG_REMOTE_REPO in .env and restart borg."
    echo
fi

if [ "$START" = true ]; then
    HOST_PORT="$(grep -E '^HOST_PORT=' .env | head -1 | cut -d= -f2 | tr -d ' ')"
    echo "The platform is reachable at:  http://localhost:${HOST_PORT:-8000}/"
    echo "Tail the logs with:            sudo podman compose logs -f web"
fi
