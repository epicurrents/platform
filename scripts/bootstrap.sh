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

# Base compose for setup steps that need the dev .:/code bind-mount (init_env
# writes .env back to the host through it). Prod overlay only for the final
# `up`, when the stack must run without the host source tree mounted in.
COMPOSE="docker compose"
COMPOSE_PROD="docker compose -f docker-compose.yml -f docker-compose.prod.yml"

# ── Arguments ────────────────────────────────────────────────────────────────

START=true
TS_AUTHKEY_ARG=""
TS_HOSTNAME_ARG=""
TS_MODE="join"
while [ $# -gt 0 ]; do
    case "$1" in
        --no-start) START=false; shift ;;
        --tailscale-authkey)    TS_AUTHKEY_ARG="${2:-}"; shift 2 ;;
        --tailscale-authkey=*)  TS_AUTHKEY_ARG="${1#*=}"; shift ;;
        --tailscale-hostname)   TS_HOSTNAME_ARG="${2:-}"; shift 2 ;;
        --tailscale-hostname=*) TS_HOSTNAME_ARG="${1#*=}"; shift ;;
        --tailscale-mode)       TS_MODE="${2:-}"; shift 2 ;;
        --tailscale-mode=*)     TS_MODE="${1#*=}"; shift ;;
        -h|--help)
            # Print the header comment block (everything up to the first
            # non-comment line), sans the shebang and leading '# '.
            awk 'NR > 1 && !/^#/ { exit } NR > 1 { sub(/^# ?/, ""); print }' "$0"
            exit 0
            ;;
        *) die "Unknown argument: $1 (try --help)" ;;
    esac
done

case "$TS_MODE" in
    join|serve) ;;
    *) die "--tailscale-mode must be 'join' or 'serve' (got '$TS_MODE')." ;;
esac

# ── Sanity checks ────────────────────────────────────────────────────────────

[ "$(id -u)" -eq 0 ] \
    && die "Run this script as a regular user, not root. sudo will be used where needed."

# ── Plan ─────────────────────────────────────────────────────────────────────
# The step list is computed per run: the first pass ends at the .env review
# pause, the second pass includes the project-conditional and start-up steps.

FIRST_RUN=false
if [ ! -f .env ]; then
    FIRST_RUN=true
fi

ACTIVE_PROJECT=""
ACTIVE_PLUGINS=""
DICOM_ENABLED=false
if [ "$FIRST_RUN" = false ]; then
    ACTIVE_PROJECT="$(grep -E '^EPICURRENTS_PROJECT=' .env | head -1 | cut -d= -f2 | tr -d ' "'"'"'')"
    # `|| true` so a pre-existing .env without the EPICURRENTS_PLUGINS line
    # (upgraded deployment) does not trip `set -e -o pipefail` on the failed grep.
    ACTIVE_PLUGINS="$(grep -E '^EPICURRENTS_PLUGINS=' .env | head -1 | cut -d= -f2 | tr -d ' "'"'"'' || true)"
    # DICOM ships its OHIF viewer as a `update = none` submodule; fetch it when
    # the dicom plugin is enabled. Match `dicom` as a whole comma-separated
    # entry so a name like `dicom-foo` does not trigger it.
    case ",$ACTIVE_PLUGINS," in
        *,dicom,*) DICOM_ENABLED=true ;;
    esac
fi

progress_step git    "Check git"                     direct
progress_step docker "Check Docker Engine"           direct
progress_step group  "Check docker group membership" direct
progress_step subs   "Initialise submodules (viewer + docs)"
if [ -z "${SKIP_DEV_TOOLS_INSTALL:-}" ]; then
    progress_step devtools "Install dev tooling (git hooks + AI-tool symlinks)"
fi
# Declared before the image step because the clone has to land before the build:
# the image installs the project's requirements and COPYs its source. Guarded on
# ACTIVE_PROJECT, which is empty on the first pass — that pass ends at the .env
# review and never reaches either step.
if [ -n "$ACTIVE_PROJECT" ] && [ ! -d "projects/$ACTIVE_PROJECT" ]; then
    progress_step project_clone "Clone project ($ACTIVE_PROJECT)"
fi
progress_step image "Build the Python image"
if [ "$FIRST_RUN" = true ]; then
    progress_step envgen "Generate .env with random secrets"
else
    if [ "$DICOM_ENABLED" = true ]; then
        progress_step ohif "Initialise OHIF viewer submodule"
    fi
    progress_step frontend "Build frontend bundles"
    progress_step borg     "Initialise Borg backup repositories"
    if [ "$START" = true ]; then
        if [ -n "$ACTIVE_PROJECT" ]; then
            progress_step activate "Activate project ($ACTIVE_PROJECT)"
        fi
        progress_step up "Start the stack (production overlay)"
        if [ -n "$TS_AUTHKEY_ARG" ]; then
            if [ "$TS_MODE" = "serve" ]; then
                progress_step tailnet "Publish the UI on the tailnet" direct
            else
                progress_step tailnet "Join the tailnet" direct
            fi
        fi
    fi
fi

if [ "$FIRST_RUN" = true ]; then
    progress_begin "Epicurrents bootstrap — first pass (pauses after generating .env)"
else
    progress_begin "Epicurrents bootstrap"
fi

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
# Volume subpath support requires Engine 25+.

step_docker() {
    if command -v docker &>/dev/null; then
        ok "already installed: $(docker --version 2>/dev/null || sudo docker --version)"
    else
        if ! command -v apt-get &>/dev/null; then
            die "Docker Engine 25+ not found and apt-get is unavailable. \
Install Docker manually and re-run."
        fi
        info "Installing Docker Engine from Docker's official repository"

        sudo apt-get update -y -qq
        sudo apt-get install -y -qq ca-certificates curl gnupg lsb-release

        sudo install -m 0755 -d /etc/apt/keyrings
        curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
            | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
        sudo chmod a+r /etc/apt/keyrings/docker.gpg

        # shellcheck disable=SC1091
        CODENAME="$(. /etc/os-release && echo "$VERSION_CODENAME")"
        ARCH="$(dpkg --print-architecture)"
        echo \
            "deb [arch=${ARCH} signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/ubuntu ${CODENAME} stable" \
            | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null

        sudo apt-get update -y -qq
        sudo apt-get install -y \
            docker-ce docker-ce-cli containerd.io \
            docker-buildx-plugin docker-compose-plugin

        sudo systemctl enable --now docker
    fi

    DOCKER_SERVER_VERSION="$(sudo docker version --format '{{.Server.Version}}' 2>/dev/null \
        || docker version --format '{{.Server.Version}}' 2>/dev/null)"
    DOCKER_MAJOR="${DOCKER_SERVER_VERSION%%.*}"
    if [ "${DOCKER_MAJOR:-0}" -lt 25 ]; then
        die "Docker Engine 25+ is required for volume subpath support (found ${DOCKER_SERVER_VERSION}). \
Upgrade Docker and re-run this script."
    fi
    step_note "Docker Engine ${DOCKER_SERVER_VERSION}"
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

# ── Pre-.env compose guard ───────────────────────────────────────────────────
# docker compose evaluates the redis service's ${REDIS_PASSWORD:?} guard at
# config-load time for EVERY subcommand — including the image build and the
# init_env run below, both of which must happen before .env exists. Without a
# value, `docker compose build web` aborts before bootstrap can generate the
# secret. Provide a throwaway value for these pre-.env commands only: on this
# first pass redis is never started (build + `run --no-deps`), and the pass
# exits right after init_env writes a real secret into .env, so the placeholder
# never reaches a running container. The second pass (with .env present) skips
# this branch and uses the generated secret.
if [ "$FIRST_RUN" = true ]; then
    export REDIS_PASSWORD="bootstrap-placeholder"
fi

# ── Unescaped `$` in .env values ─────────────────────────────────────────────
# docker compose interpolates .env, and the copy it hands a container through
# `env_file` goes through the same pass, so a `$` in a value is read as a
# variable reference and replaced — with nothing, since the name it forms is
# unset. `smtp$ecret99` reaches the application as `smtp`.
#
# init_env no longer generates such a value, but nothing stops an operator
# pasting one: an SMTP password, an external database credential, a remote borg
# repository URL. The failure is silent on both sides — compose says nothing,
# and the application authenticates with a value it has no way to know is
# truncated — so this is checked here, on the second pass, which is the first
# time bootstrap sees the file as the operator left it.
#
# `$$` is compose's escape and is left alone: a deployment that genuinely needs
# a literal `$` in a credential it does not control has no other option, and
# the value reaching the container is then correct.
step_env_dollar_guard() {
    local offenders
    # Values only — a `$` in a comment is not interpolated into anything. The
    # second pattern strips escaped `$$` first so only unescaped ones remain.
    offenders="$(grep -nE '^[A-Za-z_][A-Za-z0-9_]*=' .env \
        | sed 's/\$\$//g' \
        | grep -E '^[0-9]+:[A-Za-z_][A-Za-z0-9_]*=[^=]*\$' \
        | cut -d: -f1,2 || true)"
    if [ -n "$offenders" ]; then
        printf '\n'
        warn "These .env values contain an unescaped \$, which docker compose will strip:"
        printf '%s\n' "$offenders" | sed 's/^/    line /'
        printf '\n'
        printf 'The application receives a shortened value and nothing reports it. Either\n' >&2
        printf 'choose a value without a $, or double it ($$) so compose passes one through.\n' >&2
        die "Refusing to continue with values that would not survive the trip to the container."
    fi
}
if [ "$FIRST_RUN" = false ]; then
    step_env_dollar_guard
fi

# ── 4. Submodules ────────────────────────────────────────────────────────────

step_subs() {
    git submodule update --init --recursive
}
run_step subs step_subs

# ── 4b. Dev tooling (git hooks + AI-tool symlinks) ───────────────────────────
# Project-scoped git hooks live in scripts/git-hooks/ and review-agent specs
# live in .review/. The install script symlinks the hooks into .git/hooks/
# and creates .claude/agents -> ../.review/agents so Claude Code finds the
# specs under its expected path. Idempotent — safe to re-run.

step_devtools() {
    bash scripts/install-dev-tools.sh
}
if [ -z "${SKIP_DEV_TOOLS_INSTALL:-}" ]; then
    run_step devtools step_devtools
fi

# ── 4c. Clone the active project ─────────────────────────────────────────────
# Projects live in their own repositories, so a fresh checkout of the platform
# has no projects/<name>/ to activate. Clone it before step 5, because the image
# build reads the project from disk twice: it installs the project's
# requirements.lock, and its `COPY . .` is how the project's Python source
# reaches the production image, which unlike dev has no source bind-mount. The
# first pass never gets here — it has no .env yet, so ACTIVE_PROJECT is empty
# and it stops at the review pause below.
#
# EPICURRENTS_PROJECT_REPO says where to clone from and accepts four forms, so
# that a private project can be reached however the deployment already
# authenticates:
#
#   myproject                       -> https://github.com/epicurrents/myproject
#   someorg/thing                   -> https://github.com/someorg/thing
#   https://host/org/thing.git      -> used as-is (any scheme)
#   git@host:org/thing.git          -> used as-is (scp-style SSH)
#   /srv/src/thing  ~/src/thing     -> cloned from the local filesystem
#
# Bare names expand to HTTPS on the epicurrents org because that is what every
# submodule in .gitmodules and both clone lines in the getting-started guide
# already use; a deployment that authenticates by SSH key gives the full
# git@ form instead. Leaving the variable unset is not an error on its own —
# the base platform runs without a project — but it is one when
# EPICURRENTS_PROJECT names a project that is not already on disk.

resolve_project_repo() {
    # $1 = the raw EPICURRENTS_PROJECT_REPO value. Echoes a clonable source.
    # The tilde pattern is quoted because bash expands an unquoted `~/` inside a
    # case pattern to $HOME, which then fails to match the literal `~/...` read
    # out of .env — and the value falls through to the bare-name branch and is
    # turned into a GitHub URL. git does not expand `~` either, so the branch
    # substitutes $HOME itself rather than passing the tilde through.
    case "$1" in
        *://*)      printf '%s' "$1" ;;          # any explicit scheme
        *@*:*)      printf '%s' "$1" ;;          # scp-style SSH
        '~'/*)      printf '%s' "$HOME/${1#'~'/}" ;;
        /*|./*|../*)
                    printf '%s' "$1" ;;          # local path
        */*)        printf 'https://github.com/%s' "$1" ;;
        *)          printf 'https://github.com/epicurrents/%s' "$1" ;;
    esac
}

step_project_clone() {
    local source
    source="$(resolve_project_repo "$PROJECT_REPO")"
    printf 'Cloning project %s from %s\n' "$ACTIVE_PROJECT" "$source"
    git clone --depth 1 "$source" "projects/$ACTIVE_PROJECT"
}

if [ -n "$ACTIVE_PROJECT" ] && [ ! -d "projects/$ACTIVE_PROJECT" ]; then
    PROJECT_REPO="$(grep -E '^EPICURRENTS_PROJECT_REPO=' .env | head -1 | cut -d= -f2- | tr -d ' "'"'"'' || true)"
    if [ -z "$PROJECT_REPO" ]; then
        printf '\nEPICURRENTS_PROJECT=%s but projects/%s/ does not exist and\n' "$ACTIVE_PROJECT" "$ACTIVE_PROJECT"
        printf 'EPICURRENTS_PROJECT_REPO is not set, so there is nowhere to clone it from.\n\n' >&2
        printf 'Set EPICURRENTS_PROJECT_REPO in .env, or clone the project yourself into\n' >&2
        printf 'projects/%s/ and re-run. Leave EPICURRENTS_PROJECT blank to run the base\n' "$ACTIVE_PROJECT" >&2
        printf 'platform with no project.\n' >&2
        exit 1
    fi
    run_step project_clone step_project_clone
fi

# ── 5. Build the Python image ────────────────────────────────────────────────
# Needed before init_env can run (init_env executes inside the web image).
# The build is cached after the first run, so re-running this script is cheap.
#
# The build reads EPICURRENTS_PROJECT out of .env (compose interpolates it into
# the build arg) to find the project's requirements.lock. On the first pass
# there is no .env, so the arg is empty and the image is built without a
# project — which is correct, since that pass exists only to produce .env and
# the second pass rebuilds once the project has been cloned.

step_image() {
    $COMPOSE build web
}
run_step image step_image

# ── 6. Generate .env (first run) ─────────────────────────────────────────────
# init_env auto-fills SECRET_KEY, BORG_PASSPHRASE, ADMIN_PASSWORD, the VAPID
# keypair, and the federation Ed25519 keypair. Other values (DB credentials,
# admin email, etc.) keep the .env.example defaults — adequate for a test
# deploy, but should be reviewed before any production traffic.

step_envgen() {
    # The web service declares `env_file: ./.env`, and docker compose treats a
    # missing env_file as a hard error — so .env must exist before this `run`.
    # Seed it from .env.example (giving every key a placeholder); init_env then
    # fills the empty/placeholder secrets in place.
    cp .env.example .env
    $COMPOSE run --rm --no-deps \
        --entrypoint python \
        --user "$(id -u):$(id -g)" \
        web manage.py init_env
    step_note ".env created at $(pwd)/.env"
}

if [ "$FIRST_RUN" = true ]; then
    run_step envgen step_envgen

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
    echo "  ./scripts/bootstrap.sh"
    echo
    if [ -n "$TS_AUTHKEY_ARG" ]; then
        warn "Tailnet flags apply to the run that starts the stack — pass them again on the next run."
    fi
    exit 0
fi

# ── 6a. Plugin-conditional submodules ────────────────────────────────────────
# Most submodules are marked active in .gitmodules and were already fetched by
# step 4. Plugin-specific submodules carry `update = none` so they're skipped
# by default; we init them explicitly here based on the enabled plugins. The
# dicom plugin ships the OHIF viewer this way. scripts/enable_plugin.sh runs
# the same fetch when a plugin is enabled after bootstrap.

step_ohif() {
    # --checkout overrides the `update = none` in .gitmodules for this single
    # invocation, so OHIF gets a real working tree.
    git submodule update --init --checkout plugins/dicom/ohif-viewer
}
if [ "$DICOM_ENABLED" = true ]; then
    run_step ohif step_ohif
fi

# ── 7. Frontend bundles ──────────────────────────────────────────────────────
# Uses the on-demand frontend-build profile service (Node 20 container) so the
# deploy host doesn't need Node installed. Writes ./frontend/dist and
# ./frontend/viewer-dist back to the host via bind-mount. ~3–5 min on the
# first run.

# Keep frontend/.env's VITE_PROJECT in lockstep with EPICURRENTS_PROJECT so the
# frontend bundle targets the same project as the backend. Vite bakes VITE_PROJECT
# in at build time, reading it only from frontend/.env (loadEnv over /work in the
# build container); the frontend-build service does not inject it. A blank value
# builds the base bundle. Rewrite the single line via a temp file so this works on
# both GNU and BSD userlands (unlike `sed -i`, whose in-place flag differs).
sync_frontend_project() {
    local fe_env="frontend/.env" tmp
    # No frontend checkout (e.g. backend-only bootstrap fixtures) — nothing to
    # configure; the frontend build step is a no-op there too.
    [ -d frontend ] || return 0
    [ -f "$fe_env" ] || cp frontend/.env.example "$fe_env"
    tmp="$(mktemp)"
    grep -vE '^VITE_PROJECT=' "$fe_env" > "$tmp" || true
    printf 'VITE_PROJECT=%s\n' "$ACTIVE_PROJECT" >> "$tmp"
    mv "$tmp" "$fe_env"
}

step_frontend() {
    sync_frontend_project
    step_note "frontend/.env: VITE_PROJECT=${ACTIVE_PROJECT:-<base>}"
    $COMPOSE --profile build run --rm frontend-build
}
run_step frontend step_frontend

# ── 8. Initialise Borg backup repo (idempotent) ──────────────────────────────
# borg info exits 0 if the repo already exists; otherwise we initialise with
# the repokey encryption mode using the auto-generated BORG_PASSPHRASE in .env.
# --no-deps would skip init-volumes, which is the step that creates the
# /data/borg subpath, so we let deps run normally.

step_borg() {
    # An empty BORG_PASSPHRASE is .env's documented way to turn repokey backups
    # off. Without this guard `borg init --encryption repokey` asks for a
    # passphrase, and `compose run` gives it a TTY to ask on, so the documented
    # opt-out stalls the bootstrap at an invisible prompt. -T removes the TTY in
    # both calls so any future prompt fails the step rather than waiting.
    if ! grep -qE '^BORG_PASSPHRASE=.+' .env; then
        step_note "skipped — BORG_PASSPHRASE is empty"
        return 0
    fi
    local remote
    remote="$(grep -E '^BORG_REMOTE_REPO=' .env | head -1 | cut -d= -f2- | tr -d ' "' || true)"

    # The local tier is optional. A deployment with a solid append-only remote
    # may not want a second copy on the disk it is protecting.
    if grep -qiE '^BACKUP_LOCAL_ENABLED=(0|false|no|off)[[:space:]]*$' .env; then
        step_note "local repository disabled (BACKUP_LOCAL_ENABLED)"
    elif $COMPOSE run --rm -T --entrypoint borg borg info /backup &>/dev/null; then
        step_note "local repository already initialised"
    else
        $COMPOSE run --rm -T --entrypoint borg borg init --encryption repokey /backup
        step_note "local repository initialised"
    fi

    # The remote too, for the same reason the local one is done here: borgmatic
    # does not create a missing repository, it fails, and leaving this manual is
    # what left a deployment backing up to nothing for months. Non-fatal, since
    # the remote host may legitimately not exist yet at bootstrap time — the
    # emitter refuses to start with no repository at all, so the case where this
    # failure actually matters is already caught, loudly, at the container.
    if [ -n "$remote" ]; then
        if $COMPOSE run --rm -T --entrypoint borg borg info "$remote" &>/dev/null; then
            step_note "remote repository already initialised"
        elif $COMPOSE run --rm -T --entrypoint borg borg init --encryption repokey "$remote"; then
            step_note "remote repository initialised — export its key, it is not the local one"
        else
            step_note "remote repository could not be initialised; do it before relying on off-host backup"
        fi
    fi
}
run_step borg step_borg

# ── 8b. Activate the configured project ──────────────────────────────────────
# With EPICURRENTS_PROJECT set (blank = base platform, handled by skipping this
# step), activate the project before the app starts. activate_project applies
# the project's migrations and records it as active in the database. Two hard
# requirements from the command: EPICURRENTS_PROJECT must be set so the settings
# loader adds the project app to INSTALLED_APPS (it is — from .env), and the
# application server must NOT be running — which is why this runs here, before
# step_up. It executes against PostgreSQL via `compose run` (never on the host,
# which would target the dev SQLite database and corrupt state). db is brought
# up first so the run can connect; --no-deps keeps compose from starting web's
# dependency chain (the app services), and the image's default entrypoint waits
# for db to be ready.

step_activate() {
    $COMPOSE up -d db
    $COMPOSE run --rm --no-deps web python manage.py activate_project "$ACTIVE_PROJECT"
    step_note "project '$ACTIVE_PROJECT' activated"
}
# Only when starting the stack: activation brings db up, which --no-start must not
# do. A --no-start operator activates manually before their own `up` (see summary).
if [ "$START" = true ] && [ -n "$ACTIVE_PROJECT" ]; then
    run_step activate step_activate
fi

# ── Compose overlay selection ────────────────────────────────────────────────
# The bundled TLS proxy is opt-in per deployment, keyed off a PROXY_DOMAIN value
# in .env. With it, caddy terminates TLS in front of web and web's port binding
# drops to loopback; without it the stack keeps the prod overlay's direct
# binding, which is what a tailnet-only deployment or one behind an existing
# institutional ingress wants. Resolved here rather than next to COMPOSE_PROD at
# the top of the script because .env does not exist yet on the first pass.
if [ -f .env ] && grep -qE '^PROXY_DOMAIN=[^[:space:]]' .env; then
    COMPOSE_PROD="$COMPOSE_PROD -f docker-compose.proxy.yml"
fi

# ── 9. Start the stack ───────────────────────────────────────────────────────

step_up() {
    $COMPOSE_PROD up -d
}

step_tailnet() {
    # With an auth key present, put the deployment on the tailnet now that web is
    # up. The key travels only in this process's environment — never to disk.
    # Direct step: both scripts print the resulting name and follow-up
    # instructions the operator needs to read.
    local script="scripts/tailscale-join.sh"
    if [ "$TS_MODE" = "serve" ]; then
        script="scripts/tailscale-serve.sh"
    fi
    if [ -n "$TS_HOSTNAME_ARG" ]; then
        TS_AUTHKEY="$TS_AUTHKEY_ARG" bash "$script" --hostname "$TS_HOSTNAME_ARG"
    else
        TS_AUTHKEY="$TS_AUTHKEY_ARG" bash "$script"
    fi
}

if [ "$START" = true ]; then
    run_step up step_up
    if [ -n "$TS_AUTHKEY_ARG" ]; then
        run_step tailnet step_tailnet
    fi
elif [ -n "$TS_AUTHKEY_ARG" ]; then
    warn "--tailscale-authkey ignored with --no-start (the stack must be up to register)."
fi

# ── 10. Summary ──────────────────────────────────────────────────────────────

echo
bold "================================================================"
bold " Bootstrap complete"
bold "================================================================"
echo

if [ "$START" = true ]; then
    $COMPOSE_PROD ps
    echo
fi

if [ ! -f "$HOME/.ssh/id_borg" ] && [ ! -f "$HOME/.ssh/id_borg.pub" ]; then
    echo "Optional — remote Borg backups:"
    echo "  ssh-keygen -t ed25519 -C 'borg@$(hostname)' -f ~/.ssh/id_borg -N ''"
    echo "  Then set BORG_SSH_KEY_PATH and BORG_REMOTE_REPO in .env and restart borg."
    echo
fi

if [ "$START" = true ]; then
    HOST_PORT="$(grep -E '^HOST_PORT=' .env | head -1 | cut -d= -f2 | tr -d ' ')"
    echo "The platform is reachable at:  http://localhost:${HOST_PORT:-8000}/"
    echo "Tail the logs with:            scripts/logs.sh"
else
    echo "Start the stack when you're ready:"
    echo "  $COMPOSE_PROD up -d"
fi

if [ "$NEEDS_NEWGRP" = true ]; then
    echo
    warn "Log out and back in (or run 'newgrp docker') to use docker without sudo."
fi
