#!/usr/bin/env bash
# bootstrap_steps.sh — the executable half of a bootstrap run, from the point
# where a container runtime is available to the closing summary.
#
# Sourced in place (not called as a function) by scripts/bootstrap.sh and
# scripts/bootstrap-podman.sh once each has installed its own prerequisites and
# set COMPOSE / COMPOSE_PROD. Sourcing rather than wrapping keeps these steps
# running in exactly the scope they used to occupy when they lived inline, so
# the move that created this file changed no behaviour.
#
# The two callers differ only in how compose is spelled — `docker compose` or
# `sudo -E podman compose` — which is why every command here goes through
# $COMPOSE or $COMPOSE_PROD and never names a runtime directly. Keep it that
# way: a bare `docker` call added here is one that breaks the Podman path
# silently, since compose would still work and only that call would fail.
#
# Expected from the caller: COMPOSE, COMPOSE_PROD, START, FIRST_RUN,
# ACTIVE_PROJECT, ACTIVE_PLUGINS, DICOM_ENABLED, TS_* and the progress helpers.

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

# ── .env values that arrive shortened ────────────────────────────────────────
# Two characters carry meaning in a .env value, and both lose the tail of it
# without anything saying so: compose is silent, and the application
# authenticates with a value it has no way to know was cut.
#
# `$` — compose interpolates .env, and the copy it hands a container through
# `env_file` goes through the same pass, so a `$` is read as a variable
# reference and replaced with nothing, the name it forms being unset.
# `smtp$ecret99` reaches the application as `smtp`.
#
# ` #` — a `#` opens a comment where whitespace precedes it, so `hunter2 #old`
# arrives as `hunter2`. A `#` *inside* a value is kept, and `ab#cd` is passed
# through whole by every reader here, so it is deliberately not flagged;
# init_env declines to generate one, but for legibility rather than for this.
#
# init_env generates neither, but nothing stops an operator pasting one: an SMTP
# password, an external database credential, a remote borg repository URL. Both
# are checked on the second pass, the first time bootstrap sees the file as the
# operator left it.
#
# Two escapes are honoured rather than flagged, because each is how the format
# itself says "I meant this literally": `$$` for a dollar, and quoting the whole
# value for a hash. A deployment stuck with a credential it does not control
# needs both.
step_env_value_guard() {
    local dollars hashes
    # Values only — a `$` in a comment is not interpolated into anything. The
    # second pattern strips escaped `$$` first so only unescaped ones remain.
    dollars="$(grep -nE '^[A-Za-z_][A-Za-z0-9_]*=' .env \
        | sed 's/\$\$//g' \
        | grep -E '^[0-9]+:[A-Za-z_][A-Za-z0-9_]*=[^=]*\$' \
        | cut -d: -f1,2 || true)"
    if [ -n "$dollars" ]; then
        printf '\n'
        warn "These .env values contain an unescaped \$, which docker compose will strip:"
        printf '%s\n' "$dollars" | sed 's/^/    line /'
        printf '\n'
        printf 'The application receives a shortened value and nothing reports it. Either\n' >&2
        printf 'choose a value without a $, or double it ($$) so compose passes one through.\n' >&2
        die "Refusing to continue with values that would not survive the trip to the container."
    fi
    # The character class after `=` does two jobs. It excludes a value opening
    # with a quote, since quoting is the format's own way of keeping a `#`, and
    # refusing that would refuse a correct file. And by requiring one
    # non-blank character first it lets `KEY= # note` past — an empty value with
    # a comment beside it, which has no tail to lose.
    hashes="$(grep -nE '^[A-Za-z_][A-Za-z0-9_]*=[[:space:]]*[^[:space:]"'"'"'].*[[:space:]]#' .env \
        | cut -d: -f1,2 || true)"
    if [ -n "$hashes" ]; then
        printf '\n'
        warn "These .env values have a space before a #, which starts a comment:"
        printf '%s\n' "$hashes" | sed 's/^/    line /'
        printf '\n'
        printf 'Everything from the # onward is dropped and the application receives only\n' >&2
        printf 'what precedes it, with nothing reporting the loss. Either remove the space\n' >&2
        printf 'before the #, or quote the whole value so it is kept.\n' >&2
        die "Refusing to continue with values that would not survive the trip to the container."
    fi
}
if [ "$FIRST_RUN" = false ]; then
    step_env_value_guard
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
#   project-myproject               -> https://github.com/epicurrents/project-myproject
#   someorg/thing                   -> https://github.com/someorg/thing
#   https://host/org/thing.git      -> used as-is (any scheme)
#   git@host:org/thing.git          -> used as-is (scp-style SSH)
#   /srv/src/thing  ~/src/thing     -> cloned from the local filesystem
#
# Bare names expand to HTTPS on the epicurrents org because that is what every
# submodule in .gitmodules and both clone lines in the getting-started guide
# already use, and project repositories there are named project-<name>; a
# deployment that authenticates by SSH key gives the full git@ form instead.
# Leaving the variable unset is not an error on its own — the base platform
# runs without a project — but it is one when
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

# ── 6a. Install the pinned viewer edition ────────────────────────────────────
# The public viewer page loads a prebuilt edition bundle. Building it here means
# running the builder's `npm run setup`, which clones every workspace package
# from its own repository and builds them in dependency order — the step that
# makes a fresh deploy fragile. frontend/viewer-pin.json names a release asset
# instead, verified by SHA-256 before it is unpacked.
#
# Runs before the frontend build so a fetched edition is in place when the
# per-project libs are written beside it; neither clears the other's output.
# An empty pin is not an error — it means this deployment still builds
# the edition from the checkout, so the step reports that and moves on.
#
# The dev compose file, not the production overlay: production gives `vendor` a
# single writable mount for the Pyodide tree and mounts viewer-dist read-only
# everywhere, so the overlay's vendor service cannot write this. Running it
# unprivileged through `.:/code` also keeps the output owned by 1000:1000, the
# same as the frontend build that writes into the same directory.

step_viewer() {
    $COMPOSE --profile vendor run --rm --no-deps -T vendor python manage.py vendor_viewer
}
run_step viewer step_viewer

step_frontend() {
    sync_frontend_project
    step_note "frontend/.env: VITE_PROJECT=${ACTIVE_PROJECT:-<base>}"
    $COMPOSE --profile build run --rm frontend-build
}
run_step frontend step_frontend

# ── 7a. Vendor the Pyodide runtime ───────────────────────────────────────────
# The viewer's Python analysis tools load their interpreter from the deployment's
# own origin (/vendor/pyodide/<version>/), so the runtime and the wheels it needs
# have to be on disk before anything can use them. Not shipped in the repo or the
# image: the tree is version-pinned, gitignored and ~47 MiB. Idempotent, so a
# re-run costs a hash check. The step is fatal on failure rather than skipped —
# a deployment missing this tree looks completely healthy and fails only in a
# browser console, the day someone opens the analysis panel.
#
# Written through the `vendor` service rather than `web`: production mounts the
# tree into web read-only, so web is the one container that cannot populate it.

step_pyodide() {
    $COMPOSE_PROD --profile vendor run --rm --no-deps -T vendor python manage.py vendor_pyodide
}
run_step pyodide step_pyodide

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

# ── 9a. Static lead fields ───────────────────────────────────────────────────
# The other half of the vendored asset tree: pre-computed lead fields the viewer's
# source-localisation tool fetches instead of asking the compute API per montage,
# which is also what makes them service-worker cacheable and available offline.
# Unlike the Pyodide runtime these are computed rather than downloaded — a couple
# of seconds — so there is no reason to skip a run. It reads and writes the
# LeadFieldCache table, so it belongs after the stack is up and migrated; a
# --no-start deployment generates them with its own first `compose run`. Run
# through the overlay the stack is running under, not the base file, so the
# command sees the deployment's settings rather than the base file's development
# defaults. Same writer service as step 7a, for the same reason.

step_leadfields() {
    $COMPOSE_PROD --profile vendor run --rm --no-deps -T vendor python manage.py generate_compute_static
}

if [ "$START" = true ]; then
    run_step up step_up
    run_step leadfields step_leadfields
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

if [ "${NEEDS_NEWGRP:-false}" = true ]; then
    echo
    warn "Log out and back in (or run 'newgrp docker') to use docker without sudo."
fi
