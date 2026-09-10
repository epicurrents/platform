#!/usr/bin/env bash
# bootstrap.sh — go from a fresh machine to a running Epicurrents platform in
# one script. On Ubuntu/Debian the prerequisite is a NON-ROOT user with sudo —
# git and docker are installed automatically if missing. Running as root is
# refused: a deployment whose files are owned by root is one the unprivileged
# containers cannot read. Cloud images commonly give you root and nothing else,
# so creating that user is step zero (see docs/getting-started.md). On macOS /
# Windows / non-Ubuntu Linux, install git and Docker Desktop yourself first;
# the script will detect them and skip the install steps.
#
# What it does:
#   1. Installs git if missing (Ubuntu/Debian only).
#   2. Installs Docker Engine 25+ if missing (Ubuntu/Debian only).
#   3. Adds the current user to the docker group on Linux.
#   4. Initialises submodules (viewer + docs).
#   5. Builds the Python image.
#   6. Generates .env (first run only) — pauses for the operator to review.
#   7. Builds the frontend bundles via the on-demand frontend-build service.
#   8. Initialises the Borg backup repositories, local and remote (idempotent).
#   8b. Activates the configured project (when EPICURRENTS_PROJECT is set).
#   9. Starts the stack using the production compose overlay.
#
# Two-pass on first setup, with a single pause to review .env:
#
#   ./scripts/bootstrap.sh        # installs prereqs, inits submodules, builds
#                                 # the image, writes .env, exits.
#   $EDITOR .env                  # review and customise.
#   ./scripts/bootstrap.sh        # builds frontend, inits borg, starts stack.
#
# Flags:
#   --no-start                  Skip starting the stack (steps 1–8 only).
#   --tailscale-authkey <key>   After the stack starts, put this deployment on
#                               your tailnet. The single-use key is never written
#                               to disk.
#   --tailscale-hostname <name> Tailnet device name (default: TS_HOSTNAME in .env).
#   --tailscale-mode join|serve How. `join` (default) installs Tailscale on the
#                               host, which is the only arrangement that gives
#                               containers a route *out* — what the evidence-host
#                               log shipper needs. `serve` instead runs a
#                               userspace container that publishes the web UI at
#                               https://<name>.<tailnet>.ts.net and leaves the
#                               host alone. See the two scripts' headers.
#
# Output: on a terminal the run renders as a live step checklist (see
# scripts/lib/progress.sh); steps that may prompt for input are tagged
# `interactive` and print straight to the terminal. Captured step output is
# written to bootstrap.log. When stdout is not a TTY the output is plain and
# sequential. Override with BOOTSTRAP_PROGRESS=plain or =fancy.
#
# This script targets the production deploy scenario (the prod compose overlay
# is used for the final `up`). For a local dev setup, see docs/getting-started.md.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/.."

# shellcheck source=scripts/lib/progress.sh
. "$SCRIPT_DIR/lib/progress.sh"
# shellcheck source=scripts/lib/install-docker.sh
. "$SCRIPT_DIR/lib/install-docker.sh"
# shellcheck source=scripts/lib/bootstrap_plan.sh
. "$SCRIPT_DIR/lib/bootstrap_plan.sh"

# Base compose for setup steps that need the dev .:/code bind-mount (init_env
# writes .env back to the host through it). Prod overlay only for the final
# `up`, when the stack must run without the host source tree mounted in.
COMPOSE="docker compose"
COMPOSE_PROD="docker compose -f docker-compose.yml -f docker-compose.prod.yml"

bootstrap_parse_args "$@"
bootstrap_require_non_root
bootstrap_env_state

# ── Plan ─────────────────────────────────────────────────────────────────────
# The step list is computed per run: the first pass ends at the .env review
# pause, the second pass includes the project-conditional and start-up steps.
# The three prerequisite steps are this script's own; everything after them is
# shared with the Podman path.
progress_step git    "Check git"                     direct
progress_step docker "Check Docker Engine"           direct
progress_step group  "Check docker group membership" direct
bootstrap_plan_common
bootstrap_begin

# ── 1. git ───────────────────────────────────────────────────────────────────
# Needed for `git submodule update`. Auto-installable on Ubuntu/Debian; on
# other platforms (macOS, etc.) the user installed git to clone in the first
# place, so we just verify presence.

step_git() {
    if command -v git &>/dev/null; then
        step_note "already installed: $(git --version)"
    elif command -v apt-get &>/dev/null; then
        sudo apt-get update -y -qq
        sudo apt-get install -y -qq git
        step_note "installed: $(git --version)"
    else
        die "git not found and apt-get is unavailable. Install git manually and re-run."
    fi
}
run_step git step_git

# ── 2. Docker Engine ─────────────────────────────────────────────────────────
# The install itself lives in lib/install-docker.sh, shared with the
# prepare-host.sh that ships in a distribution package so both paths install the
# same engine from the same repository. What stays here is this script's own
# framing: the step renderer, and the fact that a missing apt-get is fatal on the
# server this script targets.

step_docker() {
    if ! install_docker_engine; then
        die "Docker Engine ${DOCKER_MIN_MAJOR}+ is required. See the message above."
    fi
    local version
    if ! version="$(require_docker_engine_version)"; then
        die "Upgrade Docker and re-run this script."
    fi
    step_note "Docker Engine ${version}"
}
run_step docker step_docker

# ── 3. docker group (Linux only) ─────────────────────────────────────────────
# On macOS / Windows the docker daemon runs under the user via Docker Desktop —
# no group membership needed. Detect by absence of `getent`, which is Linux-only.
# Runs in the current shell (direct step) because it swaps COMPOSE to the sudo
# fallback when the group change can't take effect until next login.

NEEDS_NEWGRP=false
step_group() {
    if ! command -v getent &>/dev/null; then
        step_note "non-Linux host — docker group check skipped"
    elif getent group docker | grep -qw "$USER"; then
        step_note "$USER is in the docker group"
    else
        sudo usermod -aG docker "$USER"
        warn "Added $USER to the docker group. Group change takes effect on next login."
        warn "This session will use sudo for docker commands."
        # -H, not just -E. With -E alone sudo preserves the caller's HOME, so
        # root writes its docker state — ~/.docker/.token_seed and the buildx
        # lock — into the invoking user's home, owned by root. Every later
        # docker command that user runs *without* sudo then fails on
        # permission, which is exactly the state this branch is working toward:
        # it has just added them to the docker group so the next login needs no
        # sudo at all. The failure surfaces one deployment step later as an
        # unexplained "permission denied" on a lock file.
        COMPOSE="sudo -EH $COMPOSE"
        COMPOSE_PROD="sudo -EH $COMPOSE_PROD"
        NEEDS_NEWGRP=true
        # The captured steps below will run compose through sudo with their
        # output hidden; keep the timestamp fresh so no prompt can stall them.
        progress_sudo_keepalive
        step_note "added to docker group — using sudo for this session"
    fi
}
run_step group step_group

# shellcheck source=scripts/lib/bootstrap_steps.sh
. "$SCRIPT_DIR/lib/bootstrap_steps.sh"
