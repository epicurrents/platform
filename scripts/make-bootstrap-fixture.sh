#!/usr/bin/env bash
# make-bootstrap-fixture.sh — assemble a minimal copy of the platform in one of
# three shapes: a CI bootstrap-smoke fixture (default), a browsable base-UI demo
# (--demo), or a runnable distribution for a project (--dist).
#
# By default it copies only the Docker config and backend platform parts — no
# frontend, no projects, no plugins, no docs, no git history. The Vue frontend
# and the projects/ and plugins/ trees are excluded by default: the base platform
# imports none of them when EPICURRENTS_PROJECT and EPICURRENTS_PLUGINS are
# blank, and a backend bring-up does not need the frontend bundles. Pass
# --with-frontend, --with-project and/or --with-plugin to include them. The
# projects/__init__.py package marker is the one exception, and always ships —
# the bundled Dockerfile copies projects/ unconditionally, so a package without
# it cannot build its own image.
#
# Projects and plugins are both add-on trees merged into settings and URLs at
# boot, and are copied by the same rules; they differ in cardinality. Exactly one
# project is active (EPICURRENTS_PROJECT), so copying several activates none and
# leaves the choice to the operator, whereas every copied plugin is activated
# (EPICURRENTS_PLUGINS is a comma-separated list) because that is the composition
# a deployment actually runs.
#
# --demo builds a self-contained package an interested party can run on a naive
# Docker host to browse the base UI: it bundles the prebuilt frontend/dist (no
# viewer-dist, no project) and generates a human start.sh + README.md.
#
# --dist builds a runnable distribution: like --demo but also bundles the
# compiled viewer-dist (so the signal viewer works) and, with --with-project
# NAME, activates that project — the full experience for a given project.
#
# Usage:
#   scripts/make-bootstrap-fixture.sh <dest> [options]
#
#   <dest>               Directory to populate. Created if absent. Must be empty
#                        unless --force is given (which clears it first).
#   --force              Overwrite a non-empty destination.
#   --with-frontend      Also copy the Vue frontend source (excludes
#                        node_modules / dist / caches; the frontend-build service
#                        rebuilds those). The runner then builds the bundles.
#   --with-project NAME  Also copy projects/NAME. Repeatable. With --dist (or the
#                        default fixture) a single project is activated
#                        (EPICURRENTS_PROJECT) so its migrations run and API mounts.
#   --with-projects      Copy every projects/* tree (none auto-activated).
#   --with-plugin NAME   Also copy plugins/NAME. Repeatable. Every copied plugin
#                        is activated (EPICURRENTS_PLUGINS) so its migrations run
#                        and its API mounts.
#   --with-plugins       Copy and activate every plugins/* tree.
#   --demo               Browsable base-UI package: prebuilt frontend/dist (run
#                        `npm run build` in frontend/ first), no viewer-dist, no
#                        project, no plugins; human start.sh + README.md. The
#                        signal viewer is not included. Excludes --with-frontend
#                        and the project / plugin flags.
#   --dist               Runnable distribution: prebuilt frontend/dist + viewer-dist
#                        (run `npm run build` and `npm run build:viewer` first),
#                        plus --with-project NAME and/or --with-plugin NAME to
#                        activate them. Generates a human start.sh + README.md.
#                        Excludes --with-frontend.
#
# After it runs: cd into <dest> and run ./start.sh (--demo / --dist) or
# ./bootstrap-smoke.sh (default), or the docker compose `test` / `test-postgres`
# services.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# ── Helpers ──────────────────────────────────────────────────────────────────

bold() { printf '\033[1m%s\033[0m\n'          "$*"; }
info() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
ok()   { printf '    \033[32m✓\033[0m  %s\n'  "$*"; }
die()  { printf '\n\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

# ── Arguments ────────────────────────────────────────────────────────────────

DEST=""
FORCE=false
WITH_FRONTEND=false
WITH_ALL_PROJECTS=false
WITH_ALL_PLUGINS=false
DEMO=false
DIST=false
PROJECTS=()
PLUGINS=()

while [ $# -gt 0 ]; do
    case "$1" in
        --force) FORCE=true ;;
        --with-frontend) WITH_FRONTEND=true ;;
        --with-projects) WITH_ALL_PROJECTS=true ;;
        --with-plugins) WITH_ALL_PLUGINS=true ;;
        --demo) DEMO=true ;;
        --dist) DIST=true ;;
        --with-project)
            shift
            [ $# -gt 0 ] || die "--with-project requires a project name."
            PROJECTS+=("$1")
            ;;
        --with-plugin)
            shift
            [ $# -gt 0 ] || die "--with-plugin requires a plugin name."
            PLUGINS+=("$1")
            ;;
        # Print the header comment as the usage text: from line 2 up to the first
        # line that is not a comment, so adding options above never needs a line
        # number here (the old fixed range had drifted into the code below it).
        -h|--help) sed -n '2,$p' "$0" | sed -n '/^#/!q; s/^# \{0,1\}//p'; exit 0 ;;
        -*) die "Unknown option: $1 (try --help)" ;;
        *)
            [ -z "$DEST" ] || die "Only one destination may be given (got '$DEST' and '$1')."
            DEST="$1"
            ;;
    esac
    shift
done

[ -n "$DEST" ] || die "Destination directory required. Usage: $0 <dest> [options]"
command -v rsync &>/dev/null || die "rsync is required but not installed."

# Resolve DEST to an absolute path without requiring it to exist yet.
mkdir -p "$DEST"
DEST="$(cd "$DEST" && pwd)"
[ "$DEST" != "$REPO_ROOT" ] || die "Destination must not be the repository root itself."

if [ -n "$(ls -A "$DEST" 2>/dev/null)" ]; then
    [ "$FORCE" = true ] || die "Destination '$DEST' is not empty. Re-run with --force to overwrite."
    info "Clearing non-empty destination (--force)"
    find "$DEST" -mindepth 1 -maxdepth 1 -exec rm -rf {} +
    ok "Cleared $DEST"
fi

# Resolve the project list: --with-projects expands to every project directory.
if [ "$WITH_ALL_PROJECTS" = true ]; then
    while IFS= read -r d; do PROJECTS+=("$(basename "$d")"); done \
        < <(find "$REPO_ROOT/projects" -mindepth 1 -maxdepth 1 -type d ! -name '__pycache__' | sort)
fi
# Deduplicate (a name passed via both --with-project and --with-projects).
# A read loop rather than mapfile, which macOS's bash 3.2 lacks.
if [ ${#PROJECTS[@]} -gt 0 ]; then
    _dedup=()
    while IFS= read -r _p; do
        [ -n "$_p" ] && _dedup+=("$_p")
    done < <(printf '%s\n' "${PROJECTS[@]}" | sort -u)
    PROJECTS=("${_dedup[@]}")
fi

# Same two steps for plugins. Deliberately mirrored rather than factored into a
# helper: bash 3.2 has neither namerefs nor a safe way to pass a possibly-empty
# array under `set -u`, so a shared function would need more guard syntax than
# the duplication costs.
if [ "$WITH_ALL_PLUGINS" = true ]; then
    while IFS= read -r d; do PLUGINS+=("$(basename "$d")"); done \
        < <(find "$REPO_ROOT/plugins" -mindepth 1 -maxdepth 1 -type d ! -name '__pycache__' | sort)
fi
if [ ${#PLUGINS[@]} -gt 0 ]; then
    _dedup=()
    while IFS= read -r _p; do
        [ -n "$_p" ] && _dedup+=("$_p")
    done < <(printf '%s\n' "${PLUGINS[@]}" | sort -u)
    PLUGINS=("${_dedup[@]}")
fi

# ── Mode validation ────────────────────────────────────────────────────────────
# --demo and --dist are human-package modes; both ship the prebuilt frontend/dist
# (not the source) and the dist must already be built on the host. --dist adds the
# compiled viewer-dist and supports activating a project; --demo is base-UI only.
[ "$DEMO" = false ] || [ "$DIST" = false ] \
    || die "--demo and --dist are mutually exclusive (--demo is base UI only; --dist bundles the viewer and a project)."

if [ "$DEMO" = true ] || [ "$DIST" = true ]; then
    [ "$WITH_FRONTEND" = false ] \
        || die "--demo / --dist ship the prebuilt frontend/dist, not the source; drop --with-frontend."
    [ -f "$REPO_ROOT/frontend/dist/index.html" ] \
        || die "frontend/dist is not built. Run 'npm run build' in frontend/ (or scripts/rebuild-frontend.sh) first."
fi
if [ "$DEMO" = true ]; then
    [ ${#PROJECTS[@]} -eq 0 ] \
        || die "--demo builds the base platform with no project; use --dist --with-project NAME for a project distributable."
    [ ${#PLUGINS[@]} -eq 0 ] \
        || die "--demo builds the base platform with no plugins; use --dist --with-plugin NAME to include one."
fi
if [ "$DIST" = true ]; then
    # The builder edition copied into viewer-dist/ names its UMD bundle .umd.js; the
    # per-project base builds under viewer-dist/<project>/ name theirs .umd.cjs.
    [ -f "$REPO_ROOT/frontend/viewer-dist/epicurrents-lib.umd.js" ] \
        || die "frontend/viewer-dist is not built. Run 'npm run build:viewer' in frontend/ (or scripts/rebuild-frontend.sh) first."
fi

# ── What to copy ─────────────────────────────────────────────────────────────
# The Dockerfile builds from the whole backend tree (COPY . .), so every backend
# app in INSTALLED_APPS must be present for migrate to succeed.

ROOT_FILES=(
    Dockerfile
    .dockerignore
    entrypoint.sh
    docker-compose.yml
    docker-compose.prod.yml
    docker-compose.proxy.yml
    requirements.txt
    # The lock, not just the requirements it was resolved from: the Dockerfile
    # copies it and installs the production closure with --require-hashes, so a
    # package without it cannot build its own image. Nothing else reveals the
    # omission — the tree looks complete and the failure arrives as a missing
    # file inside a docker build on someone else's machine.
    requirements.lock
    requirements-test.txt
    requirements-dev.txt
    constraints.txt
    manage.py
    pytest.ini
    conftest.py
    ruff.toml
    .env.example
)

# Backend apps from INSTALLED_APPS (the django.contrib.* and pip apps need no
# source copy), plus the two service config directories that are mounted rather
# than built in: borgmatic/ for the borg service and caddy/ for the optional TLS
# proxy overlay. caddy/ is not optional to copy even though the overlay is —
# epicurrents/tests/test_proxy_asset_headers.py reads the Caddyfile, so a fixture
# without it fails the suite rather than skipping it.
#
# tailscale/ holds serve.json, which docker-compose.yml bind-mounts into the
# profile-gated tailscale service. A missing bind-mount source is not an error
# the runtime reports: Docker creates the path as an empty directory, so the
# container starts with a directory where its serve config should be and the
# tailnet node comes up serving nothing.
PLATFORM_DIRS=(
    user activity annotations compute epicurrents recordings
    media notifications library federation borgmatic caddy tailscale
)

# Caches, VCS metadata, build outputs, and developer residue never belong here.
COMMON_EXCLUDES=(
    --exclude '.git'
    --exclude '.gitmodules'
    --exclude '__pycache__'
    --exclude '*.py[cod]'
    --exclude '.pytest_cache'
    --exclude '.ruff_cache'
    --exclude '.DS_Store'
    --exclude '*.sqlite3'
)
# Heavy subtrees that never belong in an add-on (project or plugin) copy: JS
# dependency trees, and the ohif-viewer submodule — a large DICOM viewer checkout
# that is gated behind its own build and unrelated to backend migrations. It
# currently lives in plugins/dicom, having moved there from projects/dicom; the
# exclude is applied to both trees so it keeps holding wherever the submodule
# ends up, and costs nothing where there is no such directory.
ADDON_EXCLUDES=(
    --exclude 'node_modules'
    --exclude 'ohif-viewer'
)
# Frontend/JS build artifacts and dependency trees — rebuilt by frontend-build.
FRONTEND_EXCLUDES=(
    --exclude 'node_modules'
    --exclude 'node_external'
    --exclude 'dist'
    --exclude 'viewer-dist'
    --exclude '.vite'
    --exclude '*.tsbuildinfo'
)

# ── Copy ─────────────────────────────────────────────────────────────────────

info "Copying Docker config + root files"
for f in "${ROOT_FILES[@]}"; do
    [ -e "$REPO_ROOT/$f" ] || die "Expected file missing from repo: $f"
    cp -p "$REPO_ROOT/$f" "$DEST/$f"
done
ok "${#ROOT_FILES[@]} root files"

info "Copying backend app trees"
for d in "${PLATFORM_DIRS[@]}"; do
    [ -d "$REPO_ROOT/$d" ] || die "Expected directory missing from repo: $d/"
    rsync -a "${COMMON_EXCLUDES[@]}" "$REPO_ROOT/$d/" "$DEST/$d/"
    ok "$d/"
done

if [ "$WITH_FRONTEND" = true ]; then
    info "Copying frontend source (excluding node_modules / dist / caches)"
    [ -d "$REPO_ROOT/frontend" ] || die "frontend/ not found in repo."
    rsync -a "${COMMON_EXCLUDES[@]}" "${FRONTEND_EXCLUDES[@]}" \
        "$REPO_ROOT/frontend/" "$DEST/frontend/"
    ok "frontend/ ($(du -sh "$DEST/frontend" | cut -f1))"
fi

if [ "$DEMO" = true ] || [ "$DIST" = true ]; then
    info "Copying compiled frontend (frontend/dist)"
    # Without --with-frontend nothing has created frontend/ yet, and rsync
    # does not create intermediate destination components (openrsync does,
    # which masks this on stock macOS).
    mkdir -p "$DEST/frontend"
    rsync -a "${COMMON_EXCLUDES[@]}" "$REPO_ROOT/frontend/dist/" "$DEST/frontend/dist/"
    ok "frontend/dist ($(du -sh "$DEST/frontend/dist" | cut -f1))"
fi
if [ "$DIST" = true ]; then
    info "Copying compiled viewer (frontend/viewer-dist)"
    rsync -a "${COMMON_EXCLUDES[@]}" "$REPO_ROOT/frontend/viewer-dist/" "$DEST/frontend/viewer-dist/"
    ok "frontend/viewer-dist ($(du -sh "$DEST/frontend/viewer-dist" | cut -f1))"
fi

ACTIVE_PROJECT=""
if [ ${#PROJECTS[@]} -gt 0 ]; then
    info "Copying projects"
    # rsync does not create intermediate destination components; see the
    # frontend/dist copy above.
    mkdir -p "$DEST/projects"
    for p in "${PROJECTS[@]}"; do
        [ -d "$REPO_ROOT/projects/$p" ] || die "Project not found: projects/$p"
        rsync -a "${COMMON_EXCLUDES[@]}" "${ADDON_EXCLUDES[@]}" \
            "$REPO_ROOT/projects/$p/" "$DEST/projects/$p/"
        ok "projects/$p/"
    done
    if [ ${#PROJECTS[@]} -eq 1 ]; then
        ACTIVE_PROJECT="${PROJECTS[0]}"
        ok "Active project: $ACTIVE_PROJECT"
    else
        ok "${#PROJECTS[@]} projects copied; none auto-activated (set EPICURRENTS_PROJECT yourself)"
    fi
fi

# projects/ is a Python package, and its marker ships even when no project does.
# The bundled Dockerfile copies the tree unconditionally, in a stage that resolves
# an absent project lock at build time — which is how "no project" stays a
# supported configuration. A COPY whose source is missing from the build context
# fails before any of that runs, so a package built without a project could not
# build its own image. Only the marker is needed: rsync and git alike drop an
# empty directory, and it is also what an active project imports as
# projects.<name>.
info "Copying the projects package marker"
mkdir -p "$DEST/projects"
[ -f "$REPO_ROOT/projects/__init__.py" ] \
    || die "projects/__init__.py is missing from the repository; the package cannot build without it."
cp -p "$REPO_ROOT/projects/__init__.py" "$DEST/projects/__init__.py"
ok "projects/__init__.py"

# ACTIVE_PLUGINS is the comma-separated EPICURRENTS_PLUGINS value the generated
# runner writes into .env. Every copied plugin is listed: unlike the single active
# project, plugins compose, so there is no choice left for the operator to make.
ACTIVE_PLUGINS=""
if [ ${#PLUGINS[@]} -gt 0 ]; then
    info "Copying plugins"
    mkdir -p "$DEST/plugins"
    for p in "${PLUGINS[@]}"; do
        [ -d "$REPO_ROOT/plugins/$p" ] || die "Plugin not found: plugins/$p"
        rsync -a "${COMMON_EXCLUDES[@]}" "${ADDON_EXCLUDES[@]}" \
            "$REPO_ROOT/plugins/$p/" "$DEST/plugins/$p/"
        ok "plugins/$p/"
    done
    # Mirror of the projects marker above: each plugin imports as plugins.<name>.
    [ -f "$REPO_ROOT/plugins/__init__.py" ] && cp -p "$REPO_ROOT/plugins/__init__.py" "$DEST/plugins/__init__.py"
    ACTIVE_PLUGINS="$(printf '%s,' "${PLUGINS[@]}")"
    ACTIVE_PLUGINS="${ACTIVE_PLUGINS%,}"
    ok "Active plugins: $ACTIVE_PLUGINS"
fi

# ── Generated runner + docs ───────────────────────────────────────────────────
# --demo / --dist ship a human start.sh + README.md (bring up and stay up). The
# default CI-fixture mode ships bootstrap-smoke.sh + FIXTURE_README (build →
# init_env → up → health → tear down).

if [ "$DEMO" = true ] || [ "$DIST" = true ]; then
    info "Writing start.sh"
    {
        echo '#!/usr/bin/env bash'
        echo "# Generated by make-bootstrap-fixture.sh — do not edit by hand."
        echo "ACTIVE_PROJECT=\"${ACTIVE_PROJECT}\""
        echo "ACTIVE_PLUGINS=\"${ACTIVE_PLUGINS}\""
        cat <<'START'

# start.sh — bring up Epicurrents and leave it running. ./start.sh --help for
# the tailnet flags.
set -euo pipefail
cd "$(dirname "$0")"

usage() {
    cat <<'USAGE'
start.sh — bring up Epicurrents and leave it running.

Usage:
  ./start.sh [--tailscale-authkey <tskey-...>] [--tailscale-hostname <name>]
             [--tailscale-mode join|serve]

Run with no arguments to start (or restart) the deployment. The tailnet flags
apply once, after the stack is up; the key is never written to disk and may be
passed in the TS_AUTHKEY environment variable instead.

  join   (default) Install Tailscale on the host. The host gets a tailnet
         address and containers get a route out — what the evidence-host log
         shipper needs.
  serve  Run a userspace Tailscale container that publishes the web UI at
         https://<name>.<tailnet>.ts.net. Inbound only; the host is untouched.
USAGE
}

TS_AUTHKEY_ARG=""
TS_HOSTNAME_ARG=""
TS_MODE="join"
# `shift 2` on a flag given as the last argument shifts past the end, which fails
# under set -e and exits with no message at all — so the count is checked first
# and the complaint names the flag.
need_value() {
    if [ "$2" -lt 2 ]; then
        echo "$1 needs a value." >&2
        exit 1
    fi
}
while [ $# -gt 0 ]; do
    case "$1" in
        --tailscale-authkey)    need_value "$1" $#; TS_AUTHKEY_ARG="$2"; shift 2 ;;
        --tailscale-authkey=*)  TS_AUTHKEY_ARG="${1#*=}"; shift ;;
        --tailscale-hostname)   need_value "$1" $#; TS_HOSTNAME_ARG="$2"; shift 2 ;;
        --tailscale-hostname=*) TS_HOSTNAME_ARG="${1#*=}"; shift ;;
        --tailscale-mode)       need_value "$1" $#; TS_MODE="$2"; shift 2 ;;
        --tailscale-mode=*)     TS_MODE="${1#*=}"; shift ;;
        -h|--help)              usage; exit 0 ;;
        *) echo "Unknown argument: $1 (try --help)" >&2; exit 1 ;;
    esac
done
# The key may also arrive in the environment, which keeps it out of argv and the
# shell history: TS_AUTHKEY=tskey-... ./start.sh
TS_AUTHKEY_ARG="${TS_AUTHKEY_ARG:-${TS_AUTHKEY:-}}"
case "$TS_MODE" in
    join|serve) ;;
    *) echo "--tailscale-mode must be 'join' or 'serve' (got '$TS_MODE')." >&2; exit 1 ;;
esac
TS_SCRIPT="./tailscale-${TS_MODE}.sh"
# Checked before the build rather than after the stack is up: a run that is going
# to fail on a missing script should fail in the first second, not ten minutes in
# with a deployment already running and the operator's auth key half-spent.
if [ -n "$TS_AUTHKEY_ARG" ] && [ ! -f "$TS_SCRIPT" ]; then
    echo "$TS_SCRIPT is not in this package — the tailnet scripts ship with a" >&2
    echo "distribution, not a demo. Install Tailscale on the host yourself, or" >&2
    echo "use a distribution package." >&2
    exit 1
fi

# ── Preflight ────────────────────────────────────────────────────────────────
# Each of these is a condition that otherwise surfaces minutes later as a message
# about something else — a BuildKit checksum error naming a ref, a permission
# denied inside a migration — with a half-built deployment already on the disk.
# The order is dependency order: no daemon check can run before docker exists.

if ! command -v docker >/dev/null 2>&1; then
    echo "docker is not installed. This package needs Docker with Compose v2 and" >&2
    echo "nothing else — no Python, Node or database." >&2
    if [ -f ./prepare-host.sh ]; then
        echo "On a fresh Linux server, run the host preparation first:" >&2
        echo "    sudo ./prepare-host.sh" >&2
        echo "It installs Docker and creates the account this deployment runs as." >&2
    else
        echo "Install Docker Engine: https://docs.docker.com/engine/install/" >&2
    fi
    exit 1
fi

# Compose v1 is a separate `docker-compose` binary and cannot read this package's
# compose files, so its presence is not a substitute for the plugin.
if ! docker compose version >/dev/null 2>&1; then
    echo "docker is installed but 'docker compose' is not. Compose v2 ships as the" >&2
    echo "docker-compose-plugin package; the standalone docker-compose v1 binary" >&2
    echo "cannot read these compose files." >&2
    exit 1
fi

DOCKER_VERSION="$(docker version --format '{{.Server.Version}}' 2>/dev/null || true)"
if [ -z "$DOCKER_VERSION" ]; then
    echo "The Docker daemon is not reachable. Check that it is running, and that" >&2
    echo "this user is in the docker group — group membership applies only to a" >&2
    echo "new login session, so it needs a fresh login after usermod." >&2
    exit 1
fi
# The same floor bootstrap.sh enforces: below 25 the volume subpath syntax in the
# compose files is accepted and then mounts the wrong thing.
case "${DOCKER_VERSION%%.*}" in
    ""|*[!0-9]*)
        echo "Warning: cannot parse the Docker Engine version ($DOCKER_VERSION); continuing." >&2
        ;;
    *)
        if [ "${DOCKER_VERSION%%.*}" -lt 25 ]; then
            echo "Docker Engine 25+ is required for volume subpath support (found $DOCKER_VERSION)." >&2
            exit 1
        fi
        ;;
esac

# The image build copies projects/ whether or not a project is active — the stage
# that reads a project's requirements resolves an absent lock at build time, which
# is what keeps "no project" a supported configuration. BuildKit fails a COPY
# whose source is missing from the context before reaching any of that, and
# reports it as a checksum error naming an internal ref, which points nowhere.
if [ ! -d projects ]; then
    echo "This package has no projects/ directory. The image build copies it" >&2
    echo "whether or not a project is active, so the build cannot start." >&2
    echo "The package was assembled incorrectly — rebuild it with" >&2
    echo "scripts/make-bootstrap-fixture.sh." >&2
    exit 1
fi

# web, celery, celery-beat and migrate all run as uid/gid 1000:1000 against a bind
# mount of this directory, so it has to be writable by that uid regardless of who
# launches the stack. Extracting a package as root produces a tree that is not,
# and the deployment then comes up and fails on its first write — far from the
# cause. Linux only: Docker Desktop maps ownership inside its own VM, where none
# of this applies, and `getent` is the same Linux probe bootstrap.sh uses.
if command -v getent >/dev/null 2>&1; then
    # Two different questions, and the ownership check below answers only the
    # second. A tree already handed to uid 1000 is writable by uid 1000, so it
    # passes that check no matter who invokes this — including root, who can write
    # anywhere. The .env generated below belongs to the invoking user, so a root
    # run leaves a root-owned .env inside a tree the deployment account and the
    # containers both need to write, and nothing fails until much later.
    if [ "$(id -u)" -eq 0 ]; then
        echo "Do not run this as root. The deployment runs as uid 1000, and the .env" >&2
        echo "this generates belongs to whoever invokes it — as root that is a file" >&2
        echo "neither the account nor the containers can write." >&2
        if [ -f ./prepare-host.sh ]; then
            echo "If the host is not prepared yet, that is the script to run as root:" >&2
            echo "    sudo ./prepare-host.sh" >&2
            echo "It creates the account and hands this directory over; then log in as" >&2
            echo "that account and run this script there." >&2
        else
            echo "Log in as the account owning this directory and run it there." >&2
        fi
        exit 1
    fi

    DIR_MODE="$(stat -c %a . 2>/dev/null || true)"
    # Numeric before arithmetic: a stat that answered something unexpected would
    # otherwise abort the script inside printf or $(( )) under set -e, turning a
    # check that exists to produce a clear message into a cryptic failure of its
    # own. An unreadable mode means the check does not run, not that the run stops.
    case "$DIR_MODE" in
        ""|*[!0-7]*) DIR_MODE="" ;;
    esac
    if [ -n "$DIR_MODE" ]; then
        DIR_UID="$(stat -c %u .)"
        DIR_GID="$(stat -c %g .)"
        # Zero-pad so the owner / group / other digits sit at fixed offsets whether
        # or not stat reported a leading setuid digit.
        DIR_MODE="$(printf "%04d" "$DIR_MODE")"
        WRITABLE=false
        if [ "$DIR_UID" = "1000" ] && [ "$(( ${DIR_MODE:1:1} & 2 ))" -ne 0 ]; then
            WRITABLE=true
        fi
        if [ "$DIR_GID" = "1000" ] && [ "$(( ${DIR_MODE:2:1} & 2 ))" -ne 0 ]; then
            WRITABLE=true
        fi
        if [ "$(( ${DIR_MODE:3:1} & 2 ))" -ne 0 ]; then
            WRITABLE=true
        fi
        if [ "$WRITABLE" = false ]; then
            echo "$(pwd) is not writable by uid 1000, which is the user every" >&2
            echo "container runs as (owner ${DIR_UID}:${DIR_GID}, mode ${DIR_MODE})." >&2
            echo "The stack would start and then fail on its first write." >&2
            if [ -f ./prepare-host.sh ]; then
                echo "Run the host preparation, which hands the package to that account:" >&2
                echo "    sudo ./prepare-host.sh" >&2
                echo "Or fix the ownership yourself:" >&2
            else
                echo "Fix the ownership and run this again:" >&2
            fi
            echo "    sudo chown -R 1000:1000 $(pwd)" >&2
            echo "Then run this script as the account holding uid 1000, so the .env" >&2
            echo "it generates belongs to the same user." >&2
            exit 1
        fi
    fi
fi

# Two modes, chosen by whether .env names a domain — the same rule bootstrap.sh
# and update.sh use, so a deployment behaves identically however it was created.
#
#   PROXY_DOMAIN empty  -> local: the development compose on 127.0.0.1. Fine for
#                          trying the platform out on a laptop; no TLS, and the
#                          Django development server rather than gunicorn.
#   PROXY_DOMAIN set    -> production: gunicorn, DJANGO_MODE=production, and the
#                          bundled Caddy terminating TLS with a certificate it
#                          obtains for that name.
#
# The domain has to resolve to this host before the production pass runs: Caddy
# asks for the certificate as it starts, and ACME validates over port 80.
#
# First run writes .env and comes up locally, because a name cannot be known
# before there is a machine to point it at. Add PROXY_DOMAIN to .env and run
# this again to switch.
read_env() {
    [ -f .env ] || return 0
    grep -E "^$1=" .env | head -1 | cut -d= -f2- | tr -d ' "' || true
}

COMPOSE=(docker compose -f docker-compose.yml)
PROXY_DOMAIN="$(read_env PROXY_DOMAIN)"
if [ -n "$PROXY_DOMAIN" ]; then
    COMPOSE+=(-f docker-compose.prod.yml -f docker-compose.proxy.yml)
    MODE="production"
else
    MODE="local"
fi

# On the first run .env does not exist yet, and docker compose evaluates the
# redis ${REDIS_PASSWORD:?} guard at config-load for every subcommand — so the
# build and init_env below need a throwaway value. Once init_env writes the real
# secret into .env we unset it so the running stack uses the real one.
NEED_INIT=false
if [ ! -f .env ]; then
    NEED_INIT=true
    export REDIS_PASSWORD="setup-placeholder"
fi

echo "==> Building the platform image (first run can take several minutes)…"
"${COMPOSE[@]}" build web

if [ "$NEED_INIT" = true ]; then
    echo "==> Generating configuration and secrets (.env)…"
    cp .env.example .env
    "${COMPOSE[@]}" run --rm --no-deps --entrypoint python \
        --user "$(id -u):$(id -g)" web manage.py init_env
    unset REDIS_PASSWORD
fi

# Activate the bundled project and plugins so migrate applies their migrations
# and their APIs mount. Idempotent: rewrites each key in place (BSD/GNU sed
# differ, avoid -i). Plugin settings merge below the project's, so the platform
# order (common < plugins < project) holds however these two lines land.
if [ -n "$ACTIVE_PROJECT" ]; then
    echo "==> Activating project: $ACTIVE_PROJECT"
    grep -v '^EPICURRENTS_PROJECT=' .env > .env.tmp && mv .env.tmp .env
    echo "EPICURRENTS_PROJECT=$ACTIVE_PROJECT" >> .env
fi
if [ -n "$ACTIVE_PLUGINS" ]; then
    echo "==> Activating plugins: $ACTIVE_PLUGINS"
    grep -v '^EPICURRENTS_PLUGINS=' .env > .env.tmp && mv .env.tmp .env
    echo "EPICURRENTS_PLUGINS=$ACTIVE_PLUGINS" >> .env
fi

if [ "$MODE" = production ]; then
    # FRONTEND_URL is the base of every password-reset link, and the platform
    # refuses to start in production while it still holds the .env.example
    # placeholder — mailing users a link to a Vite dev server nobody runs is
    # the failure it is guarding against. Behind the bundled proxy it has
    # exactly one correct value, so asking for it again only creates a way to
    # get it wrong. Anything already customised is left alone.
    if [ "$(read_env FRONTEND_URL)" = "http://localhost:5173" ]; then
        echo "==> Pointing FRONTEND_URL at https://${PROXY_DOMAIN}"
        grep -v '^FRONTEND_URL=' .env > .env.tmp && mv .env.tmp .env
        echo "FRONTEND_URL=https://${PROXY_DOMAIN}" >> .env
    fi

    # Django rejects any request whose Host is not in ALLOWED_HOSTS, so a
    # deployment that has just obtained a certificate for a name it will not
    # answer to returns 400 to every visitor — TLS working perfectly in front
    # of a wall. Appended rather than replaced: the web container's healthcheck
    # probes over loopback, so dropping 127.0.0.1 makes every probe a 400 and
    # the container reports unhealthy while serving traffic normally.
    CURRENT_HOSTS="$(read_env ALLOWED_HOSTS)"
    case ",${CURRENT_HOSTS}," in
        *",${PROXY_DOMAIN},"*) ;;
        *)
            echo "==> Adding ${PROXY_DOMAIN} to ALLOWED_HOSTS"
            grep -v '^ALLOWED_HOSTS=' .env > .env.tmp && mv .env.tmp .env
            echo "ALLOWED_HOSTS=${CURRENT_HOSTS:+${CURRENT_HOSTS},}${PROXY_DOMAIN}" >> .env
            ;;
    esac

    # The backup repository has to exist before the backup container runs, and
    # nothing else creates it: borgmatic does not initialise a missing
    # repository, it fails. Production starts the borg service, so without this
    # a distribution deployment has a backup container failing every cycle from
    # the day it is installed — visible only as a line in a log nobody reads
    # until a restore is attempted. bootstrap.sh has always done this; start.sh
    # did not, which is the entire difference between a deployment with backups
    # and one that only appears to have them. Idempotent, same check as
    # bootstrap.sh: `borg info` succeeds exactly when the repository is there.
    #
    # Skipped when BORG_PASSPHRASE is empty, which .env documents as the way to
    # turn repokey backups off. `borg init --encryption repokey` with no
    # passphrase asks for one, and `compose run` allocates a TTY by default — so
    # without this guard the documented opt-out makes start.sh hang at a prompt
    # rather than start the deployment. -T removes the TTY regardless, so a
    # future prompt from any cause fails the step instead of waiting for ever.
    if [ -z "$(read_env BORG_PASSPHRASE)" ]; then
        echo "==> Skipping backup repository init (BORG_PASSPHRASE is empty)"
    else
        # The local tier is optional — a deployment with an append-only remote
        # may not want a second copy on the disk it is protecting.
        case "$(read_env BACKUP_LOCAL_ENABLED | tr "[:upper:]" "[:lower:]")" in
            0|false|no|off)
                echo "==> Local backup repository disabled (BACKUP_LOCAL_ENABLED)"
                ;;
            *)
                echo "==> Initialising the local backup repository…"
                if "${COMPOSE[@]}" run --rm -T --entrypoint borg borg info /backup >/dev/null 2>&1; then
                    echo "    already initialised"
                else
                    "${COMPOSE[@]}" run --rm -T --entrypoint borg borg init --encryption repokey /backup
                fi
                ;;
        esac

        # And the remote, for the same reason: borgmatic does not create a
        # missing repository, it fails. Not fatal — the remote host may not
        # exist yet — because the container refuses to start with no repository
        # at all, so the case where this matters is already caught loudly.
        REMOTE_REPO="$(read_env BORG_REMOTE_REPO)"
        if [ -n "$REMOTE_REPO" ]; then
            echo "==> Initialising the remote backup repository…"
            if "${COMPOSE[@]}" run --rm -T --entrypoint borg borg info "$REMOTE_REPO" >/dev/null 2>&1; then
                echo "    already initialised"
            elif "${COMPOSE[@]}" run --rm -T --entrypoint borg borg init --encryption repokey "$REMOTE_REPO"; then
                echo "    initialised — export its key, it is not the same as the local one"
            else
                echo "    could not be initialised; do it before relying on off-host backup" >&2
            fi
        fi
    fi

    # Everything in the default set: web, workers, the beat scheduler that runs
    # the purges and the audit-integrity check, the backup container and the TLS
    # proxy. A deployment missing celery-beat looks healthy and quietly runs no
    # periodic task at all.
    echo "==> Starting the platform in production mode (TLS for ${PROXY_DOMAIN})…"
    "${COMPOSE[@]}" up -d --build
else
    echo "==> Starting the platform (web + workers)…"
    "${COMPOSE[@]}" up -d --build web celery celery-beat
fi

# After the stack, because the serve container proxies to a web service that has
# to exist. The key travels only in this process's environment.
if [ -n "$TS_AUTHKEY_ARG" ]; then
    TS_ARGS=()
    if [ -n "$TS_HOSTNAME_ARG" ]; then
        TS_ARGS=(--hostname "$TS_HOSTNAME_ARG")
    fi
    TS_AUTHKEY="$TS_AUTHKEY_ARG" bash "$TS_SCRIPT" ${TS_ARGS[@]+"${TS_ARGS[@]}"}
fi

# Both modes publish the application on 127.0.0.1:$HOST_PORT — the proxy overlay
# overrides the binding rather than removing it — so one health check serves.
PORT="$(read_env HOST_PORT)"
PORT="${PORT:-8000}"
echo "==> Waiting for the platform to become ready…"
READY=false
for _ in $(seq 1 60); do
    if curl -fsS "http://localhost:${PORT}/api/v1/health" >/dev/null 2>&1; then
        READY=true
        break
    fi
    sleep 2
done

ADMIN_USER="$(read_env ADMIN_USERNAME)"
ADMIN_USER="${ADMIN_USER:-admin}"
# Deliberately not read in production. The generated password is printed only
# on the first local run — the laptop case, where you need it to log in and
# there is no log to leak into. Every later run, and every production run,
# names the file instead: this summary goes to a terminal that is routinely
# redirected (`./start.sh > setup.log`, nohup, CI capture), and a credential
# echoed on every invocation ends up in a file nobody remembers writing.
ADMIN_PW=""
if [ "$MODE" != production ] && [ "$NEED_INIT" = true ]; then
    ADMIN_PW="$(read_env ADMIN_PASSWORD)"
fi

echo
echo "================================================================"
if [ "$READY" = true ]; then
    echo " Epicurrents is running"
else
    echo " Epicurrents did not become ready — check the logs"
fi
echo "================================================================"
if [ "$MODE" = production ]; then
    echo "  Open:       https://${PROXY_DOMAIN}/"
    echo "  Mode:       production (gunicorn + TLS)"
else
    echo "  Open:       http://localhost:${PORT}/"
    echo "  Mode:       local, no TLS — set PROXY_DOMAIN in .env and re-run"
    echo "              this script to switch to a production deployment."
fi
if [ -n "$ADMIN_PW" ]; then
    echo "  Log in as:  ${ADMIN_USER} / ${ADMIN_PW}"
else
    echo "  Log in as:  ${ADMIN_USER} — password is ADMIN_PASSWORD in .env"
fi
if [ -n "$ACTIVE_PROJECT" ]; then
    echo "  Project:    ${ACTIVE_PROJECT} (active)"
fi
if [ -n "$ACTIVE_PLUGINS" ]; then
    echo "  Plugins:    ${ACTIVE_PLUGINS} (active)"
fi
echo
echo "  Logs:        ${COMPOSE[*]} logs -f web"
echo "  Stop:        ${COMPOSE[*]} down"
echo "  Stop + wipe: ${COMPOSE[*]} down -v"
echo "================================================================"
[ "$READY" = true ] || exit 1
START
    } > "$DEST/start.sh"
    chmod +x "$DEST/start.sh"
    ok "start.sh"

    # The Docker install is defined once, in scripts/lib/install-docker.sh, and
    # bundled rather than reproduced here: bootstrap.sh sources the same file, so
    # a repository deployment and a packaged one cannot drift onto different
    # engines or a different version floor.
    info "Writing prepare-host.sh"
    mkdir -p "$DEST/lib"
    cp -p "$REPO_ROOT/scripts/lib/install-docker.sh" "$DEST/lib/install-docker.sh"
    {
        echo '#!/usr/bin/env bash'
        echo "# Generated by make-bootstrap-fixture.sh — do not edit by hand."
        cat <<'PREPARE'

# prepare-host.sh — one-time preparation of a fresh Linux server. Run as root,
# once, before ./start.sh.
#
# Two things stand between an unpacked package and a deployment, and both need
# root: Docker has to exist, and the files have to belong to the account the
# containers run as. start.sh can do neither, because it must run AS that
# unprivileged account — it writes .env as whoever invokes it, and every service
# runs as uid 1000 against a bind mount of this directory. A root-run start.sh
# would produce exactly the tree its own preflight refuses.
#
# Idempotent: run it again after changing anything and it will report what is
# already in place rather than redoing it.
#
# Usage:
#   sudo ./prepare-host.sh [--user NAME] [--no-sudoers]
#
#   --user NAME    Account to create and hand the deployment to. Default
#                  "epicurrents". Ignored when uid 1000 is already taken — see
#                  the account section below.
#   --no-sudoers   Skip the passwordless-sudo drop-in for that account.
set -euo pipefail
cd "$(dirname "$0")"

DEPLOY_USER="epicurrents"
WRITE_SUDOERS=true
KEY_WARNING=false
# `shift 2` on a flag given as the last argument shifts past the end, which fails
# under set -e and exits with no message at all — so the count is checked first
# and the complaint names the flag.
need_value() {
    if [ "$2" -lt 2 ]; then
        echo "$1 needs a value." >&2
        exit 1
    fi
}
while [ $# -gt 0 ]; do
    case "$1" in
        --user)       need_value "$1" $#; DEPLOY_USER="$2"; shift 2 ;;
        --user=*)     DEPLOY_USER="${1#*=}"; shift ;;
        --no-sudoers) WRITE_SUDOERS=false; shift ;;
        -h|--help)    sed -n '3,24p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "Unknown argument: $1 (try --help)" >&2; exit 1 ;;
    esac
done

if [ -z "$DEPLOY_USER" ]; then
    echo "--user needs an account name." >&2
    exit 1
fi

if [ "$(id -u)" -ne 0 ]; then
    echo "Run this as root: sudo ./prepare-host.sh" >&2
    echo "It installs packages and creates an account, neither of which an" >&2
    echo "unprivileged user can do. ./start.sh is the one you run afterwards," >&2
    echo "as the account created here, and never as root." >&2
    exit 1
fi

if ! command -v apt-get >/dev/null 2>&1; then
    echo "This script targets Debian/Ubuntu servers and found no apt-get." >&2
    echo "On macOS or Windows install Docker Desktop and run ./start.sh directly;" >&2
    echo "there is no separate deployment account to create there." >&2
    exit 1
fi

echo "==> Installing Docker Engine…"
# shellcheck source=/dev/null
. ./lib/install-docker.sh
install_docker_engine
DOCKER_VERSION="$(require_docker_engine_version)"
echo "    Docker Engine ${DOCKER_VERSION}"

# The deployment account is whoever holds uid 1000, and the name is negotiable
# where the uid is not: every service in the compose file runs as 1000:1000
# against a bind mount of this directory, so a tree owned by any other uid is one
# the containers cannot write. On an image that already has a uid-1000 account —
# Ubuntu cloud images ship one — creating a second account would silently give it
# 1001 and produce that exact tree, which is why an existing holder wins here
# rather than the name passed in.
echo "==> Resolving the deployment account…"
EXISTING_1000="$(getent passwd 1000 | cut -d: -f1 || true)"
if [ -n "$EXISTING_1000" ]; then
    if [ "$EXISTING_1000" != "$DEPLOY_USER" ]; then
        echo "    uid 1000 already belongs to '${EXISTING_1000}'; using that account."
        echo "    (The containers run as uid 1000, so the uid decides, not the name.)"
    else
        echo "    ${EXISTING_1000} already holds uid 1000."
    fi
    DEPLOY_USER="$EXISTING_1000"
else
    echo "    Creating ${DEPLOY_USER} with uid 1000…"
    adduser --disabled-password --gecos "" --uid 1000 "$DEPLOY_USER"
fi

DEPLOY_HOME="$(getent passwd "$DEPLOY_USER" | cut -d: -f6)"

echo "==> Docker group…"
# The group normally arrives with the docker-ce package, but an engine installed
# some other way may not have created it, and usermod against a group that does
# not exist aborts the run three lines before the chown that matters.
if ! getent group docker >/dev/null 2>&1; then
    groupadd docker
    echo "    Created the docker group."
fi
if id -nG "$DEPLOY_USER" | tr " " "\n" | grep -qx docker; then
    echo "    ${DEPLOY_USER} is already in the docker group."
else
    usermod -aG docker "$DEPLOY_USER"
    echo "    Added ${DEPLOY_USER} to the docker group."
fi

# Ownership before the conveniences below, and not after. Handing the package to
# the deployment account is the half of this script start.sh cannot do for
# itself, so it must not be reachable only by way of steps that can legitimately
# fail on a given image — an earlier version put it last, and a missing
# /etc/sudoers.d aborted the run with the tree still owned by root.
echo "==> Handing the package to ${DEPLOY_USER}…"
chown -R "${DEPLOY_USER}:${DEPLOY_USER}" .
echo "    $(pwd) is now owned by ${DEPLOY_USER}."

# Without this the account created above cannot be logged into at all on a server
# reached only by key, and the operator is left with a deployment user they can
# only become via `su` from the root session they happen to still hold.
echo "==> SSH access…"
if [ -s "${DEPLOY_HOME}/.ssh/authorized_keys" ]; then
    echo "    ${DEPLOY_USER} already has authorized_keys."
elif [ -s /root/.ssh/authorized_keys ]; then
    install -d -m 700 -o "$DEPLOY_USER" -g "$DEPLOY_USER" "${DEPLOY_HOME}/.ssh"
    install -m 600 -o "$DEPLOY_USER" -g "$DEPLOY_USER" \
        /root/.ssh/authorized_keys "${DEPLOY_HOME}/.ssh/authorized_keys"
    echo "    Copied root's authorized_keys to ${DEPLOY_USER}."
else
    KEY_WARNING=true
    echo "    No keys found to copy." >&2
fi

# --disabled-password leaves nothing for sudo to prompt on, so an unattended
# `sudo` as this account waits for a password that can never be typed. Absent
# /etc/sudoers.d means sudo itself is not installed, which is a normal state for
# a minimal image and not a reason to stop.
if [ "$WRITE_SUDOERS" = true ]; then
    echo "==> Sudo…"
    if [ ! -d /etc/sudoers.d ]; then
        echo "    sudo is not installed; skipping the passwordless-sudo drop-in."
    elif [ -f "/etc/sudoers.d/${DEPLOY_USER}" ]; then
        echo "    /etc/sudoers.d/${DEPLOY_USER} already exists."
    else
        echo "${DEPLOY_USER} ALL=(ALL) NOPASSWD:ALL" > "/etc/sudoers.d/${DEPLOY_USER}"
        chmod 440 "/etc/sudoers.d/${DEPLOY_USER}"
        echo "    Wrote /etc/sudoers.d/${DEPLOY_USER} (passwordless sudo)."
    fi
fi

echo
echo "================================================================"
echo " Host prepared"
echo "================================================================"
echo
echo "  Docker:      ${DOCKER_VERSION}"
echo "  Account:     ${DEPLOY_USER} (uid 1000, docker group)"
echo "  Deployment:  $(pwd)"
echo
echo "  Next, as ${DEPLOY_USER} rather than root — group membership applies"
echo "  only to a new session, so log in again rather than using su:"
echo
echo "      ssh ${DEPLOY_USER}@<this host>"
echo "      cd $(pwd) && ./start.sh"
if [ "$KEY_WARNING" = true ]; then
    echo
    echo "  WARNING: ${DEPLOY_USER} has no authorized_keys and root had none to"
    echo "  copy, so that ssh will not let you in. Give the account a way to log"
    echo "  in before you close this session."
fi
echo "================================================================"
PREPARE
    } > "$DEST/prepare-host.sh"
    chmod +x "$DEST/prepare-host.sh"
    ok "prepare-host.sh + lib/install-docker.sh"

    # Bundle the in-place updater so a deployed distribution can update itself
    # from a newer archive (archive mode is the only mode that works without a
    # git checkout). update.sh detects this layout via the root-level compose file.
    info "Bundling update.sh"
    cp "$REPO_ROOT/scripts/update.sh" "$DEST/update.sh"
    chmod +x "$DEST/update.sh"
    mkdir -p "$DEST/update"
    cat > "$DEST/update/README.md" <<'DROP'
# Update drop directory

Drop a newer distribution tarball here (named `epicurrents*.tar.gz`) and run
`./update.sh` from the deployment root. The newest matching archive is applied
over this deployment, preserving `.env` and your data; the database is migrated
and the containers are recreated. A pre-update snapshot (database + `.env`) is
written to `../backups/` first — undo with `./update.sh --rollback`.
DROP
    ok "update.sh + update/ drop dir"

    if [ "$DIST" = true ]; then
        # A distribution had no way onto a tailnet: bootstrap.sh carries the
        # flags and ships only in a git checkout, so a tarball deployment could
        # be updated and rolled back but never connected to anything. Both
        # scripts locate the deployment root by the compose file beside them, so
        # they need no adjustment at this level.
        info "Bundling tailnet scripts"
        for s in tailscale-join.sh tailscale-serve.sh; do
            cp -p "$REPO_ROOT/scripts/$s" "$DEST/$s"
            chmod +x "$DEST/$s"
        done
        ok "tailscale-join.sh + tailscale-serve.sh"

        # The application half of the evidence-host arrangement. The sink half —
        # Loki, Alertmanager, the rules — is not here on purpose: it belongs on a
        # different machine, built from a checkout, and a copy riding along in
        # every deployment would invite someone to run it beside the application
        # it is supposed to outlive.
        #
        # Named files rather than a directory sweep, and fatal when one is
        # missing. examples/ also accumulates deployment secrets that are
        # gitignored but present on the packager's disk — shipper-password,
        # watchdog-url, smtp-password — and an rsync of the tree would put them
        # in a tarball meant to be handed to someone else. An allowlist cannot
        # do that: a new file is absent from the package until it is named here,
        # which is the failure that gets noticed rather than the one that does not.
        info "Bundling the log-shipper overlay"
        SHIPPER_FILES=(
            examples/evidence-host/README.md
            examples/evidence-host/docker-compose.shipper.yml
            examples/evidence-host/promtail-remote.yaml
        )
        mkdir -p "$DEST/examples/evidence-host"
        for f in "${SHIPPER_FILES[@]}"; do
            [ -f "$REPO_ROOT/$f" ] || die "Expected file missing from repo: $f"
            cp -p "$REPO_ROOT/$f" "$DEST/$f"
        done
        ok "examples/evidence-host/ (${#SHIPPER_FILES[@]} files, shipper half only)"
    fi

    info "Writing README.md"
    {
        if [ "$DIST" = true ]; then
            echo "# Epicurrents — distribution package"
            echo
            if [ -n "$ACTIVE_PROJECT" ]; then
                echo "A runnable distribution of the Epicurrents platform with the **$ACTIVE_PROJECT**"
                echo "project active. It contains the backend (Django + Celery + PostgreSQL + Redis,"
                echo "all via Docker), the compiled web UI, and the signal viewer — the full"
                echo "experience for this project."
            else
                echo "A runnable distribution of the base Epicurrents platform. It contains the"
                echo "backend (Django + Celery + PostgreSQL + Redis, all via Docker), the compiled"
                echo "web UI, and the signal viewer."
            fi
        else
            echo "# Epicurrents — demo package"
            echo
            echo "A self-contained, runnable copy of the **base Epicurrents platform** for trying"
            echo "it out locally. It contains the backend (Django + Celery + PostgreSQL + Redis,"
            echo "all via Docker) and the compiled web UI. No project plugin is activated, and the"
            echo "embedded signal viewer is not bundled in this package: you can browse the base"
            echo "platform UI — sign in, the recordings list, library, profile — but opening a"
            echo "recording in the viewer will not work here."
        fi
        cat <<'COMMON'

Working with an AI assistant? Point it at this file and it can walk you through
each step.

## What you need

- **Docker** with Docker Compose v2 — Docker Desktop on macOS/Windows, or Docker
  Engine on Linux. Engine 25 or newer. Nothing else: no Python, Node, or
  database to install.
- On Linux, **a normal user account, not root**. Every container runs as uid
  1000 against this directory, so the deployment has to belong to the account
  that runs it. A tree unpacked as root is one the containers cannot write.

## Prepare the host (fresh Linux server)

On a server with no Docker — a new cloud VM, where you are root and nothing else
exists yet — run this once, as root:

```bash
sudo ./prepare-host.sh
```

It installs Docker Engine, creates the `epicurrents` account with uid 1000 and
puts it in the docker group, copies root's SSH keys across so you can log in as
it, and hands this directory over. Then log in as that account rather than using
`su`, because docker group membership applies only to a new session.

Skip this on macOS or Windows: install Docker Desktop and go straight to the next
step. Skip it too on a server that already has Docker and an account to run under.

## Start it

```bash
./start.sh
```

The first run builds the image (a few minutes), generates configuration and
random secrets, starts the stack, and prints the URL and the admin login. Open
the printed address (default http://localhost:8000/) and sign in.

The password is printed only on that first local run. Later runs — and every
production run — name `ADMIN_PASSWORD` in `.env` instead, because this summary
often ends up redirected to a file.

That first pass is deliberately local: the Django development server on
127.0.0.1, no TLS. It is the right thing for trying the platform out, and the
wrong thing to leave running on a server.

## Deploy it properly

To run this as a real deployment, give it a domain that resolves to the machine
and re-run the same script:

1. Point a DNS A record at the host.
2. Set two values in `.env`:
   - `PROXY_DOMAIN=your.domain`
   - `PROXY_ACME_EMAIL=you@example.com` — the certificate authority mails
     expiry warnings there, which on an unmonitored deployment is the only
     warning you get before the certificate lapses. It is required; the stack
     refuses to start without it.
3. `./start.sh`

The script switches on that one value, exactly as the platform's own
`bootstrap.sh` and `update.sh` do: gunicorn instead of the development server,
`DJANGO_MODE=production`, the full service set including the scheduler that runs
retention and audit-integrity jobs, and a bundled Caddy that obtains a
certificate for the name. Nothing else changes, and running it again is safe.

The domain must resolve **before** you run it — the certificate is requested as
Caddy starts, and the authority validates by connecting back on port 80. Open
80 and 443 to the internet on any firewall in front of the host.
COMMON
        if [ "$DIST" = true ]; then
            cat <<'TAILNET'

## Put it on a tailnet

A tailnet (Tailscale) gives this deployment a private address reachable from
your other machines without opening a port. Two arrangements, and they answer
different questions:

```bash
./start.sh --tailscale-authkey tskey-... --tailscale-hostname my-deployment
```

That is `--tailscale-mode join`, the default: Tailscale is installed on the
host, which gets a tailnet address and MagicDNS. It is the only arrangement in
which **containers can reach the tailnet**, because they route out through the
host — needed if you ship logs to an evidence host, federate over the tailnet,
or want to reach the machine without a public SSH port.

```bash
./start.sh --tailscale-authkey tskey-... --tailscale-mode serve
```

`serve` instead runs a Tailscale container in userspace mode that publishes the
web UI at `https://<name>.<tailnet>.ts.net`. Nothing is installed on the host,
which is the point — but it is inbound only, and no other container can use it
to get out.

Generate a key at https://login.tailscale.com/admin/settings/keys. It is used
once and never written to disk; pass it in `TS_AUTHKEY` instead of on the
command line to keep it out of your shell history. Re-running is safe: a host
already on a tailnet is left joined and only its name is reconciled.

## Ship the security log off this machine

`examples/evidence-host/` holds the application half of an arrangement where
security events are pushed to a second, separately-administered machine, so an
intruder on this host cannot edit the record of what they did. Only the
`epicurrents.security` stream is sent — never application logs, which carry
paths and tracebacks. The overlay and its reasoning are documented in
`examples/evidence-host/README.md`; building the receiving host needs the
platform repository.

The sink is not in this package. It belongs on a different machine, and a copy
riding along here would invite running it beside the deployment it exists to
outlive.
TAILNET
        fi
        cat <<'COMMONTAIL'

## Stop it

```bash
docker compose down        # stop, keep data
docker compose down -v     # stop and wipe all data
```

## Good to know

- Without `PROXY_DOMAIN` this runs Django's development server on loopback —
  fine for trying the platform out, not for exposing to the internet.
COMMONTAIL
        if [ "$DEMO" = true ]; then
            echo "- The signal viewer (opening a recording) is intentionally left out of this"
            echo "  package; the base UI is fully browsable without it."
        fi
        echo "- For a full deployment — backups, the production server, switching projects —"
        echo "  start from the main repository's getting-started guide."
    } > "$DEST/README.md"
    ok "README.md"
else
    info "Writing bootstrap-smoke.sh runner"
    {
        echo '#!/usr/bin/env bash'
        echo "# Generated by make-bootstrap-fixture.sh — do not edit by hand."
        echo "ACTIVE_PROJECT=\"${ACTIVE_PROJECT}\""
        echo "ACTIVE_PLUGINS=\"${ACTIVE_PLUGINS}\""
        echo "WITH_FRONTEND=${WITH_FRONTEND}"
        cat <<'SMOKE'

# bootstrap-smoke.sh — frontend-free-by-default end-to-end bring-up of the
# backend stack. Mirrors the backend steps of the platform's bootstrap.sh
# (build the image, generate .env with secrets, bring the stack up, verify
# health) minus the borg/prod overlay. Intended as a CI job step; exits
# non-zero on any failure and tears the stack down on the way out.
set -euo pipefail
cd "$(dirname "$0")"

COMPOSE="docker compose"
cleanup() { $COMPOSE --profile build down -v >/dev/null 2>&1 || true; }
trap cleanup EXIT

# docker compose evaluates the redis ${REDIS_PASSWORD:?} guard at config-load
# for every subcommand, so the build and the init_env run below (both before
# .env exists) need a value. Use a throwaway one for them, then unset it so the
# bring-up reads the real secret init_env writes into .env.
export REDIS_PASSWORD="bootstrap-placeholder"

echo "==> Building the web image"
$COMPOSE build web

# The web service declares `env_file: ./.env`, which compose requires to exist
# before `run`. Seed it from .env.example (all keys, placeholder secrets); then
# init_env fills the empty/placeholder secrets (SECRET_KEY, REDIS_PASSWORD,
# ADMIN_PASSWORD, BORG_PASSPHRASE, VAPID + federation keypairs) in place.
echo "==> Generating .env with secrets"
cp .env.example .env
$COMPOSE run --rm --no-deps --entrypoint python \
    --user "$(id -u):$(id -g)" web manage.py init_env

unset REDIS_PASSWORD

# Activate the copied project and plugins so migrate applies their migrations and
# their APIs mount — the point of copying an add-on into the fixture is to prove
# its migrations apply on a clean database. Rewrites each key portably (BSD/GNU
# sed differ, so avoid sed -i).
if [ -n "$ACTIVE_PROJECT" ]; then
    echo "==> Activating project: $ACTIVE_PROJECT"
    grep -v '^EPICURRENTS_PROJECT=' .env > .env.tmp && mv .env.tmp .env
    echo "EPICURRENTS_PROJECT=$ACTIVE_PROJECT" >> .env
fi
if [ -n "$ACTIVE_PLUGINS" ]; then
    echo "==> Activating plugins: $ACTIVE_PLUGINS"
    grep -v '^EPICURRENTS_PLUGINS=' .env > .env.tmp && mv .env.tmp .env
    echo "EPICURRENTS_PLUGINS=$ACTIVE_PLUGINS" >> .env
fi

# Note on the frontend: the source is copied with --with-frontend, but this
# smoke does NOT build it. The viewer is a multi-workspace monorepo whose
# clean-room build (its per-package dist/ is gitignored, so a fresh clone must
# regenerate it) is its own concern and does not reliably complete from the
# frontend-build service alone. The backend bring-up below does not need the
# bundles — the API and /api/v1/health respond without them.
if [ "$WITH_FRONTEND" = true ]; then
    echo "==> Frontend source present (not built by this smoke)."
    echo "    To build it: docker compose --profile build run --rm frontend-build"
fi

# Bring up only web + celery; depends_on pulls init-volumes, db, redis, and the
# migrate one-shot (migrations + collectstatic + createadmin) in order.
echo "==> Bringing up the stack (web + celery)"
$COMPOSE up -d --build web celery

PORT="$(grep -E '^HOST_PORT=' .env | head -1 | cut -d= -f2 | tr -d ' "' || true)"
PORT="${PORT:-8000}"
echo "==> Waiting for the health endpoint on :${PORT}"
healthy=false
for _ in $(seq 1 30); do
    if curl -fsS "http://localhost:${PORT}/api/v1/health" | grep -q '"status": *"ok"'; then
        echo "    stack healthy"
        healthy=true
        break
    fi
    sleep 2
done
if [ "$healthy" != true ]; then
    echo "ERROR: health endpoint did not become ready in time" >&2
    $COMPOSE logs --no-color --tail=200 || true
    exit 1
fi

echo "==> Smoke test passed"
SMOKE
    } > "$DEST/bootstrap-smoke.sh"
    chmod +x "$DEST/bootstrap-smoke.sh"
    ok "bootstrap-smoke.sh"

    info "Writing FIXTURE_README.md"
    {
        cat <<'README'
# Bootstrap fixture

A minimal copy of the Epicurrents platform — Docker config plus the backend
Django apps — assembled by `scripts/make-bootstrap-fixture.sh` for CI
bootstrap-smoke testing.

## Run the bring-up smoke test

```bash
./bootstrap-smoke.sh
```

Builds the image, generates `.env` with `manage.py init_env`, brings up
`web` + `celery` (which pulls `db`, `redis`, and the `migrate` one-shot via
`depends_on`), and polls `/api/v1/health`. Exits non-zero on failure and tears
the stack down on exit.

## Run the test suite

```bash
docker compose run --rm test            # platform suite on SQLite
docker compose run --rm test-postgres   # same suite against the live Postgres
```

## What was included
README
        echo
        if [ "$WITH_FRONTEND" = true ]; then
            echo "- **Frontend**: source copied, but the smoke does not build it. The viewer"
            echo "  monorepo's clean-room build is its own concern; build it explicitly with"
            echo "  \`docker compose --profile build run --rm frontend-build\` if you need the SPA."
        else
            echo "- **Frontend**: excluded. Only the API and \`/api/v1/health\` respond;"
            echo "  the SPA and viewer are absent. Re-run with \`--with-frontend\` to include."
        fi
        if [ ${#PROJECTS[@]} -eq 0 ]; then
            echo "- **Projects**: none. The base platform runs with \`EPICURRENTS_PROJECT\` blank."
        elif [ -n "$ACTIVE_PROJECT" ]; then
            echo "- **Projects**: \`$ACTIVE_PROJECT\` (activated by the runner so its migrations run)."
        else
            echo "- **Projects**: ${PROJECTS[*]} (none auto-activated — set \`EPICURRENTS_PROJECT\` in \`.env\` to pick one)."
        fi
        if [ -z "$ACTIVE_PLUGINS" ]; then
            echo "- **Plugins**: none. The base platform runs with \`EPICURRENTS_PLUGINS\` blank."
        else
            echo "- **Plugins**: \`$ACTIVE_PLUGINS\` (all activated by the runner so their migrations run)."
        fi
    } > "$DEST/FIXTURE_README.md"
    ok "FIXTURE_README.md"
fi

# ── Summary ──────────────────────────────────────────────────────────────────

echo
bold "================================================================"
if [ "$DIST" = true ]; then
    bold " Distribution package assembled"
elif [ "$DEMO" = true ]; then
    bold " Demo package assembled"
else
    bold " Fixture assembled"
fi
bold "================================================================"
echo
ok "Location: $DEST"
ok "Size:     $(du -sh "$DEST" | cut -f1)"
if [ "$DIST" = true ]; then
    ok "Frontend: compiled dist + viewer-dist bundled"
    [ -n "$ACTIVE_PROJECT" ] && ok "Project:  $ACTIVE_PROJECT (activated on start)"
    [ -n "$ACTIVE_PLUGINS" ] && ok "Plugins:  $ACTIVE_PLUGINS (activated on start)"
elif [ "$DEMO" = true ]; then
    ok "Frontend: compiled dist bundled (viewer-dist omitted)"
else
    [ "$WITH_FRONTEND" = true ] && ok "Frontend: source included"
    [ ${#PROJECTS[@]} -gt 0 ] && ok "Projects: ${PROJECTS[*]}"
    [ ${#PLUGINS[@]} -gt 0 ] && ok "Plugins:  ${PLUGINS[*]}"
fi
echo
echo "Next:"
if [ "$DEMO" = true ] || [ "$DIST" = true ]; then
    echo "  cd $DEST && ./start.sh"
    echo
    echo "  To ship it as an archive that update.sh can apply:"
    echo "    COPYFILE_DISABLE=1 tar -czf $(basename "$DEST").tar.gz -C $(dirname "$DEST") $(basename "$DEST")"
    echo
    echo "  COPYFILE_DISABLE matters only on macOS, where tar stores extended"
    echo "  attributes as ._* members that GNU tar then materialises as real"
    echo "  files on the target — enough to make the archive unrecognisable."
else
    echo "  cd $DEST && ./bootstrap-smoke.sh"
fi
