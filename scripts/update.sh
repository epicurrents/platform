#!/usr/bin/env bash
# update.sh — update a running Epicurrents deployment and recreate its stack.
#
# Two source modes, then a shared tail (back up → build → migrate → recreate),
# always on the production compose overlay.
#
#   --from archive   (default) Apply a distribution tarball. The newest
#                    ./update/epicurrents*.tar.gz is extracted over the
#                    deployment, preserving .env and runtime data. No host
#                    toolchain needed — the distribution ships prebuilt bundles.
#   --from repo      Pull from git and rebuild the frontend on the host (the
#                    path a git-checkout deployment or CI uses).
#
# Recovery:
#   --rollback       Restore the most recent pre-update snapshot — database,
#                    .env and code — rebuild the images from the restored code,
#                    re-run the static and vendoring steps, and recreate the
#                    stack. The rebuild is not optional: the image carries the
#                    code, so without it the recreate runs the new code against
#                    the restored database and re-applies the migrations being
#                    rolled back.
#
# While it runs, the script keeps a maintenance flag at update/maintenance.json
# (phase "updating" or "rolling_back"). The platform reads it to suspend
# requests for the duration, and the flag is removed at exit unless the run
# left the stack stopped, or --keep-lock says the caller owns it.
#
# Usage:
#   ./update.sh                          archive mode, newest ./update/epicurrents*.tar.gz
#   ./update.sh --archive ./foo.tar.gz   archive mode, explicit file
#   ./update.sh --from repo              repo mode, git pull + frontend build
#   ./update.sh --from repo --no-pull    repo mode, rebuild the current checkout
#   ./update.sh --no-backup              skip the pre-update snapshot (not advised —
#                                        it is what --rollback restores from)
#   ./update.sh --rollback               undo the last update (database, .env, code)
#   ./update.sh --skip-beat              leave celery-beat stopped after the recreate,
#                                        so no scheduled purge runs before the update
#                                        is verified; start it yourself afterwards
#   ./update.sh --root DIR               operate on the deployment at DIR instead of
#                                        the one this script sits in
#   ./update.sh --keep-lock              leave update/maintenance.json in place at
#                                        exit (for a caller that manages the flag)
#
set -euo pipefail

info() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
ok()   { printf '    \033[32m✓\033[0m  %s\n'  "$*"; }
warn() { printf '    \033[33m!\033[0m  %s\n'  "$*"; }
die()  { printf '\n\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

usage() {
    sed -n '2,43p' "$0" | sed 's/^#\{1,2\} \{0,1\}//'
}

require_value() {
    # $1 = flag name, $2 = the candidate value (empty if the flag was last).
    # Reject a missing or flag-like value so 'update.sh --from' fails with a
    # clear message instead of a cryptic 'shift' error under set -e.
    case "$2" in
        ""|-*) die "Option '$1' requires a value." ;;
    esac
}

# ── Arguments ─────────────────────────────────────────────────────────────────
MODE=archive
ARCHIVE=""
REF=""
PULL=true
BACKUP=true
ROLLBACK=false
ASSUME_YES=false
SKIP_BEAT=false
KEEP_LOCK=false
ROOT=""

while [ $# -gt 0 ]; do
    case "$1" in
        --from)      require_value --from "${2:-}";    MODE="$2"; shift 2 ;;
        --from=*)    MODE="${1#*=}"; shift ;;
        --archive)   require_value --archive "${2:-}"; ARCHIVE="$2"; shift 2 ;;
        --archive=*) ARCHIVE="${1#*=}"; shift ;;
        --ref)       require_value --ref "${2:-}";     REF="$2"; shift 2 ;;
        --ref=*)     REF="${1#*=}"; shift ;;
        --root)      require_value --root "${2:-}";    ROOT="$2"; shift 2 ;;
        --root=*)    ROOT="${1#*=}"; shift ;;
        --no-pull)   PULL=false; shift ;;
        --no-backup) BACKUP=false; shift ;;
        --backup)    BACKUP=true; shift ;;
        --rollback)  ROLLBACK=true; shift ;;
        --skip-beat) SKIP_BEAT=true; shift ;;
        --keep-lock) KEEP_LOCK=true; shift ;;
        --yes|-y)    ASSUME_YES=true; shift ;;
        -h|--help)   usage; exit 0 ;;
        *)           die "Unknown argument: $1 (try --help)" ;;
    esac
done

# ── Locate the deployment root ────────────────────────────────────────────────
# --root names it outright, for a copy of this script that lives outside the
# deployment (a root-owned copy an updater runs, a checkout driving a packaged
# deployment). Otherwise: in a git checkout this script lives in scripts/; in a
# distribution it is bundled at the deployment root next to start.sh. Detect by
# the compose file.
if [ -n "$ROOT" ]; then
    [ -d "$ROOT" ] || die "--root: no such directory: $ROOT"
    ROOT="$(cd "$ROOT" && pwd)"
    [ -f "$ROOT/docker-compose.yml" ] || die "--root: $ROOT does not look like a deployment (no docker-compose.yml)."
else
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    if [ -f "$SCRIPT_DIR/docker-compose.yml" ]; then
        ROOT="$SCRIPT_DIR"
    else
        ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
    fi
fi
cd "$ROOT"

# Every compose call uses the production overlay, deliberately and without a
# toggle. update.sh only ever updates an already-deployed stack, and a deployed
# stack is production by definition — development happens on a local machine
# against the plain dev compose, never by updating a remote VM in place. So there
# is no dev mode here: the overlay is a constant, not a flag. Array form keeps the
# flags from word-splitting (and shellcheck quiet).
# Docker or Podman, settled the same way the distribution's start.sh settles it:
# podman-docker installs a `docker` that answers by execing podman, so the name in
# the version string is the discriminator, not whether the command exists. Podman
# runs rootful because every service declares `user: "1000:1000"` against a bind
# mount, and only a rootful runtime maps that uid to the deployment's owner.
if command -v docker >/dev/null 2>&1 && ! docker --version 2>&1 | grep -qi podman; then
    CONTAINER_RUNTIME=docker
    COMPOSE=(docker compose -f docker-compose.yml -f docker-compose.prod.yml)
elif command -v podman >/dev/null 2>&1; then
    CONTAINER_RUNTIME=podman
    COMPOSE=(sudo -E podman compose -f docker-compose.yml -f docker-compose.prod.yml)
else
    CONTAINER_RUNTIME=""
    COMPOSE=(docker compose -f docker-compose.yml -f docker-compose.prod.yml)
fi
# The TLS proxy overlay is selected the same way bootstrap.sh selects it — a
# PROXY_DOMAIN value in .env. This is not cosmetic: a stack brought up with the
# overlay has to be updated with it too, or `up -d` treats the running caddy
# container as an orphan and the deployment loses its TLS terminator mid-update.
PROXY_ENABLED=false
if [ -f .env ] && grep -qE '^PROXY_DOMAIN=[^[:space:]]' .env; then
    COMPOSE+=(-f docker-compose.proxy.yml)
    PROXY_ENABLED=true
fi
UPDATE_DIR="./update"
BACKUP_DIR="./backups"
KEEP_BACKUPS=3
MAINTENANCE_FLAG="$UPDATE_DIR/maintenance.json"
# How long the restore waits for the locks the schema drop needs before giving
# up. Anything still connected — a backup dumping the database, a stray shell —
# holds a share lock the drop cannot get past, and without a bound the rollback
# hangs rather than failing. Overridable for tests; there is no reason to change
# it on a deployment.
RESTORE_LOCK_TIMEOUT="${UPDATE_RESTORE_LOCK_TIMEOUT:-60s}"

[ -f .env ] || die "No .env in $ROOT — initialize the deployment first (./start.sh in a distribution, or 'python manage.py init_env' in a checkout)."
[ -n "$CONTAINER_RUNTIME" ] || die "No container runtime found — this needs Docker Engine or Podman, either one with Compose v2."

# ── Helpers ───────────────────────────────────────────────────────────────────

confirm() {
    # $1 = prompt. Returns 0 to proceed. --yes bypasses the prompt.
    if [ "$ASSUME_YES" = true ]; then
        return 0
    fi
    printf '\033[1m%s [y/N] \033[0m' "$1"
    read -r reply
    [[ "${reply:-N}" =~ ^[Yy]$ ]]
}

service_running() {
    # $1 = compose service name.
    "${COMPOSE[@]}" ps "$1" 2>/dev/null | grep -q "running\|Up"
}

ensure_db_up() {
    if service_running db; then
        return 0
    fi
    warn "Database container is not running; starting it."
    "${COMPOSE[@]}" up -d db
    printf '    Waiting for PostgreSQL'
    for _ in $(seq 1 60); do
        if "${COMPOSE[@]}" exec -T db pg_isready -q 2>/dev/null; then
            printf '\n'
            return 0
        fi
        printf '.'
        sleep 1
    done
    printf '\n'
    die "PostgreSQL did not become ready within 60s."
}

# ── The maintenance flag ──────────────────────────────────────────────────────
# A file rather than a database row, because the rollback restores the database
# and would erase a row mid-way through the very operation the flag announces.
# It lives under update/, which every sync and snapshot in this script excludes,
# so neither direction of an update touches it. Written before anything else
# changes and removed by the exit trap below; a caller that manages the flag's
# lifecycle itself passes --keep-lock, and a pre-existing flag is then left as
# it is rather than overwritten, since it carries that caller's own fields.

FLAG_WRITTEN=false
SERVICES_STOPPED=false
tmp=""

write_maintenance_flag() {
    # $1 = phase, $2 = message for the people locked out.
    if [ "$KEEP_LOCK" = true ] && [ -f "$MAINTENANCE_FLAG" ]; then
        return 0
    fi
    if [ ! -d "$UPDATE_DIR" ]; then
        # A checkout deployment may not have the directory yet. Created by root
        # it would belong to root, and the account the containers run as could
        # then never write under it; hand it to whoever owns the tree. GNU stat
        # only, like the ownership preflight — elsewhere the chown is skipped.
        mkdir "$UPDATE_DIR"
        chown "$(stat -c %u:%g . 2>/dev/null)" "$UPDATE_DIR" 2>/dev/null || true
    fi
    local now
    now="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    printf '{"protocol": 1, "phase": "%s", "job_id": null, "since": "%s", "expected_until": null, "message": "%s"}\n' \
        "$1" "$now" "$2" > "$MAINTENANCE_FLAG.tmp"
    mv -f "$MAINTENANCE_FLAG.tmp" "$MAINTENANCE_FLAG"
    FLAG_WRITTEN=true
}

cleanup() {
    local rc=$?
    [ -n "$tmp" ] && rm -rf "$tmp"
    if [ "$FLAG_WRITTEN" = true ] && [ "$KEEP_LOCK" = false ]; then
        if [ "$rc" -ne 0 ] && [ "$SERVICES_STOPPED" = true ]; then
            # The stack was taken down and not brought back, so the platform is
            # not serving anyway; the flag is what tells anyone who reaches it
            # why. Removing it would replace a maintenance page with an error.
            warn "Leaving the maintenance flag in place ($MAINTENANCE_FLAG) — the stack was stopped"
            warn "and not restarted. Remove the file once the deployment is repaired."
        else
            rm -f "$MAINTENANCE_FLAG"
        fi
    fi
}
trap cleanup EXIT

# ── Shared steps ──────────────────────────────────────────────────────────────
# Used by both the update and the rollback path, so a rollback reproduces every
# step an update runs after the image build. A rollback that skipped them left
# the failed release's vendored assets under the restored code and never
# checked the result was serving.

BORG_WAS_RUNNING=false
# Survives a run that stops borg and then dies: the next run finds borg stopped
# and cannot tell "never ran here" from "the last update stopped it", and the
# difference is whether a deployment silently loses its backups. Under update/
# for the same reason as the flag — nothing this script syncs or snapshots
# touches it.
BORG_MARKER="$UPDATE_DIR/.borg-was-running"

stop_app_services() {
    # borg as well as the application services. Its scheduler runs inside the
    # container and dumps the database on its own clock, and a dump in progress
    # holds share locks on every table: a migration's ALTER TABLE queues behind
    # it, and the rollback's schema drop waits on it indefinitely. Stopping the
    # container ends any dump with it. Whether it was running is recorded first,
    # so the restart at the end does not start a service the deployment never
    # ran.
    if service_running borg || [ -f "$BORG_MARKER" ]; then
        BORG_WAS_RUNNING=true
        mkdir -p "$UPDATE_DIR"
        : > "$BORG_MARKER"
    fi
    info "Stopping application services"
    "${COMPOSE[@]}" stop web celery celery-beat borg || true
    SERVICES_STOPPED=true
    ok "Application services stopped"
}

collect_static() {
    info "Collecting static files"
    "${COMPOSE[@]}" run --rm --no-deps web python manage.py collectstatic --no-input
    ok "Static files collected"
}

refresh_vendored_assets() {
    # None of these three is allowed to end the run. They execute after the
    # services are stopped, so a failure that aborted here left the stack down
    # over an asset tree — observed on a deployment whose project names no
    # Pyodide asset path. The platform serves without any of them; each failure
    # is reported, counted, and repeated in the summary.
    #
    # a. Install the pinned viewer edition if it drifted. A pull can move
    # frontend/viewer-pin.json, so the check runs every update; it is a local
    # stamp comparison, so the common case costs nothing. An empty pin verifies
    # clean — that is a deployment still building the edition from the checkout,
    # not a broken one.
    #
    # After the image build, not beside the frontend build: production gives the
    # `vendor` service no /code bind, so it reads the pin baked into the image,
    # and running this earlier would install the edition the *previous* pin
    # named. --user because the production overlay runs the service as root for
    # the Pyodide tree, while viewer-dist is inside the code snapshot a rollback
    # restores with rsync as the deploy user — root-owned files there would
    # survive the rollback.
    if "${COMPOSE[@]}" --profile vendor run --rm --no-deps -T --user 1000:1000 vendor \
            python manage.py vendor_viewer --check > /dev/null 2>&1; then
        ok "Viewer edition matches the pin"
    else
        info "Installing the pinned viewer edition"
        if "${COMPOSE[@]}" --profile vendor run --rm --no-deps -T --user 1000:1000 vendor \
                python manage.py vendor_viewer; then
            ok "Viewer edition installed"
        else
            vendor_failed "the viewer edition was not installed; the public viewer page will not load"
        fi
    fi

    # b. Vendor the Pyodide runtime if it is missing or incomplete. The tree is
    # excluded from this script's rsync so a deployment keeps its own copy, which
    # means an update never creates one: a fresh host, a restored snapshot, or a
    # version bump in settings all arrive here with nothing to serve. The check
    # is a local hash sweep, so the common case costs a second and the vendoring
    # runs only when it has to.
    #
    # This step and the next write through the `vendor` service: web mounts the
    # tree read-only in production, so it is the one container that cannot
    # populate it.
    if "${COMPOSE[@]}" --profile vendor run --rm --no-deps -T vendor python manage.py vendor_pyodide --check > /dev/null 2>&1; then
        ok "Pyodide runtime present"
    else
        info "Vendoring the Pyodide runtime"
        if "${COMPOSE[@]}" --profile vendor run --rm --no-deps -T vendor python manage.py vendor_pyodide; then
            ok "Pyodide runtime vendored"
        else
            vendor_failed "the Pyodide runtime was not vendored; the viewer works, its Python analysis tools do not"
        fi
    fi

    # c. Regenerate the static lead fields. The other half of the vendored tree,
    # and computed rather than downloaded, so it runs unconditionally: a couple
    # of seconds, and regenerating is the only way a change to the generator's
    # montages or grid parameters reaches the deployment. Blob filenames carry a
    # content hash, so an unchanged field keeps its name and every cached copy
    # stays valid. Needs the migrated database (it also refreshes the
    # LeadFieldCache rows the compute API serves from), which the caller has
    # ensured — by migrating, or by restoring a database that matches the code.
    info "Generating static lead fields"
    if "${COMPOSE[@]}" --profile vendor run --rm --no-deps -T vendor python manage.py generate_compute_static; then
        ok "Static lead fields generated"
    else
        vendor_failed "the static lead fields were not generated; source localisation computes each montage on the server instead"
    fi
}

VENDOR_FAILURES=()

vendor_failed() {
    # $1 = what did not happen and what that costs. Reported now and again at
    # the end, because the run continues and the recreate output buries it.
    warn "$1"
    warn "Re-run the step once the cause is fixed; the platform serves without it."
    VENDOR_FAILURES+=("$1")
}

report_vendor_failures() {
    if [ "${#VENDOR_FAILURES[@]}" -gt 0 ]; then
        echo
        warn "${#VENDOR_FAILURES[@]} vendoring step(s) failed during this run:"
        local f
        for f in "${VENDOR_FAILURES[@]}"; do
            warn "  - $f"
        done
    fi
}

recreate_stack() {
    # --force-recreate so a changed .env is re-read (the prod overlay bakes env at
    # container-creation time; a plain restart keeps the stale value). Scoped to
    # the app services — db / redis stay up so the database is never bounced.
    #
    # celery-beat is the one autonomous source of work in the stack, and its
    # database scheduler fires every periodic task that came due while it was
    # down the moment it starts. A purge that fell due during the build then
    # unlinks files within seconds of the recreate — files a rollback restores
    # the rows for and cannot bring back. --skip-beat leaves it stopped until
    # whoever is verifying the update decides it stays.
    local services=(web celery)
    if [ "$SKIP_BEAT" = false ]; then
        services+=(celery-beat)
    fi
    info "Recreating containers"
    "${COMPOSE[@]}" up -d --force-recreate "${services[@]}"
    # Caddy serves /assets/, /viewer/ and /static/ straight off bind mounts, so
    # it has to be recreated after the tree underneath it changes — a running
    # container holds the mount it was started with. Left out, an archive update
    # takes the SPA offline while Django reports healthy.
    if [ "$PROXY_ENABLED" = true ]; then
        "${COMPOSE[@]}" up -d --force-recreate caddy
    fi
    SERVICES_STOPPED=false
    if [ "$BORG_WAS_RUNNING" = true ]; then
        # `up` rather than `start`, so a .env the rollback restored is re-read.
        # A backup service that fails to come back is not a failed update: the
        # platform is serving, so say so and leave the stack up.
        if "${COMPOSE[@]}" up -d borg; then
            rm -f "$BORG_MARKER"
        else
            warn "borg did not start; check '${COMPOSE[*]} logs borg' and start it with: ${COMPOSE[*]} up -d borg"
        fi
    fi
    ok "Stack is up"
    if [ "$SKIP_BEAT" = true ]; then
        warn "celery-beat is left stopped (--skip-beat). Once the update is verified, start it with:"
        warn "  ${COMPOSE[*]} up -d celery-beat"
    fi
}

wait_for_health() {
    local port ready asset domain spa_served
    port="$(grep -E '^HOST_PORT=' .env | head -1 | cut -d= -f2- | tr -d ' "' || true)"
    port="${port:-8000}"
    info "Waiting for the platform to become ready"
    ready=false
    for _ in $(seq 1 60); do
        if curl -fsS "http://localhost:${port}/api/v1/health" >/dev/null 2>&1; then
            ready=true
            break
        fi
        sleep 2
    done
    if [ "$ready" = true ]; then
        ok "Health check passed"
    else
        warn "Health check did not pass within the timeout; check '${COMPOSE[*]} logs web'."
    fi

    # The health endpoint says Django is answering. It says nothing about whether
    # the SPA can load, and those are separable: the bundles are served off disk
    # by the proxy, so the site can be entirely unusable to a new visitor while
    # /api/v1/health returns 200. That state shipped once and went unnoticed for
    # a day, because the hashed bundles are cached `immutable` and every browser
    # that had already loaded the page kept working. Fetch a real asset, named by
    # the index.html just installed, so the check fails on exactly what a
    # first-time visitor would hit.
    if [ "$PROXY_ENABLED" = true ] && [ -f frontend/dist/index.html ]; then
        asset="$(grep -oE '/assets/[A-Za-z0-9._-]+\.js' frontend/dist/index.html | head -1 || true)"
        domain="$(grep -E '^PROXY_DOMAIN=' .env | head -1 | cut -d= -f2- | tr -d ' "' || true)"
        if [ -n "$asset" ] && [ -n "$domain" ]; then
            info "Verifying the SPA bundle is servable"
            # --resolve pins the name to this host so the check tests the local
            # proxy rather than whatever DNS points at, while still presenting
            # the SNI the certificate was issued for.
            # Retried, because the check runs seconds after caddy was recreated
            # and a starting proxy refuses the TLS handshake outright — reported
            # by curl as exit 35, which is indistinguishable here from a
            # genuinely unservable bundle. A single attempt therefore warned on
            # every successful update, and a check that cries wolf is worse than
            # no check: it trains the operator to skip the one signal that would
            # have caught the real outage this block exists to detect.
            spa_served=false
            for _ in $(seq 1 10); do
                if curl -fsS -o /dev/null --max-time 15 \
                    --resolve "${domain}:443:127.0.0.1" "https://${domain}${asset}"; then
                    spa_served=true
                    break
                fi
                sleep 3
            done
            if [ "$spa_served" = true ]; then
                ok "SPA bundle served ($asset)"
            else
                warn "The SPA bundle at $asset is NOT being served. The site will be blank for"
                warn "any visitor without a cached copy. Check that caddy restarted:"
                warn "  ${COMPOSE[*]} up -d --force-recreate caddy"
            fi
        fi
    fi
}

find_latest_complete_snapshot() {
    # The newest snapshot that can actually be restored from — not simply the
    # newest. A run that dies between the code snapshot (step 0) and the
    # database dump (step 2) leaves a code-only directory behind, and that
    # directory is the newest; refusing on it would block rollback to the
    # perfectly good snapshot sitting behind it, which is the opposite of what
    # a recovery path should do when it meets damage.
    #
    # Warnings go to stderr deliberately: this runs inside a command
    # substitution, so anything on stdout becomes part of the returned path.
    # A glob rather than `ls -t`: the directory names are UTC timestamps this
    # script writes, so lexical order is chronological, and a glob cannot be
    # confused by a name containing whitespace. Iterated backwards for
    # newest-first.
    local dirs=("$BACKUP_DIR"/pre-update-*)
    local i d
    for ((i = ${#dirs[@]} - 1; i >= 0; i--)); do
        d="${dirs[i]}"
        [ -d "$d" ] || continue   # no matches: the glob stayed literal
        if [ ! -f "$d/db.sql.gz" ] || [ ! -f "$d/.env" ]; then
            warn "Skipping incomplete snapshot $(basename "$d") (no database or .env)" >&2
            continue
        fi
        if [ -f "$d/code.tar.gz" ] && ! tar -tzf "$d/code.tar.gz" >/dev/null 2>&1; then
            warn "Skipping snapshot $(basename "$d") — its code archive is unreadable" >&2
            continue
        fi
        printf '%s' "$d"
        return 0
    done
    return 1
}

prune_backups() {
    # Keep the newest $KEEP_BACKUPS pre-update snapshots; drop the rest. ls -t
    # over our own timestamped dir names is fine — no untrusted filenames here.
    # shellcheck disable=SC2012
    ls -1dt "$BACKUP_DIR"/pre-update-* 2>/dev/null | tail -n +$((KEEP_BACKUPS + 1)) | while read -r old; do
        rm -rf "$old"
    done || true
    # Drop snapshots left half-written by a run that died before the dump. They
    # can never be restored from, and they crowd out the ones that can.
    # shellcheck disable=SC2012
    ls -1dt "$BACKUP_DIR"/pre-update-* 2>/dev/null | while read -r d; do
        [ -f "$d/db.sql.gz" ] || { warn "Discarding half-written snapshot $(basename "$d")"; rm -rf "$d"; }
    done || true
}

restore_sql_preamble() {
    # What runs ahead of the dump, inside the same transaction. pg_dump --clean
    # drops only the objects it dumped, so a table the failed release created is
    # not in the dump and survives a restore — while the restored
    # django_migrations says its migration never ran. The next migrate then dies
    # on "relation already exists", after the snapshot, and rolls back again:
    # a retry loop with no way out except a shell. Dropping the schema first
    # makes the restore a replacement rather than an overlay. Inside the single
    # transaction, so a restore that fails leaves the schema exactly as it was.
    #
    # lock_timeout bounds how long the drop waits for the locks it needs; the
    # dump resets it to 0 in its own preamble, which is fine — once the drop has
    # its locks nothing else in the restore contends with anyone.
    cat <<SQL
SET lock_timeout = '$RESTORE_LOCK_TIMEOUT';
DROP SCHEMA IF EXISTS public CASCADE;
CREATE SCHEMA public;
SQL
}

# ── Rollback path ─────────────────────────────────────────────────────────────

if [ "$ROLLBACK" = true ]; then
    # shellcheck disable=SC2012  # mtime sort over our own snapshot dir names.
    # Every piece is verified before anything is touched — including that the
    # code archive actually reads, since it is restored *after* the database
    # and a truncated one would otherwise strand the deployment half rolled
    # back. An incomplete snapshot is skipped rather than fatal; see
    # find_latest_complete_snapshot.
    latest="$(find_latest_complete_snapshot || true)"
    [ -n "$latest" ] || die "No complete pre-update snapshot found under $BACKUP_DIR (a snapshot needs db.sql.gz and .env, and a readable code.tar.gz if it has one)."
    info "Rolling back to $(basename "$latest")"
    [ -f "$latest/MANIFEST" ] && cat "$latest/MANIFEST"
    confirm "Restore database + .env from this snapshot? Current data will be overwritten." \
        || die "Rollback aborted."
    write_maintenance_flag rolling_back "The platform is being rolled back to the previous release."
    ensure_db_up
    stop_app_services
    info "Restoring database (single transaction — all or nothing)"
    # --single-transaction + ON_ERROR_STOP: the schema drop and the restore commit
    # or roll back as one unit, so a failure leaves the database exactly as it
    # was rather than half-restored. On failure we stop here — .env is untouched
    # and the stack is not recreated — so the operator never lands in a
    # partially-recovered state.
    # SC2016: $POSTGRES_* must expand inside the db container's shell, not here.
    # shellcheck disable=SC2016
    if ! { restore_sql_preamble; gunzip -c "$latest/db.sql.gz"; } \
            | "${COMPOSE[@]}" exec -T db sh -c 'psql --single-transaction -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
                >/dev/null; then
        die "Database restore FAILED and was rolled back — the database is unchanged, .env was not touched, and the stack was not recreated. If the error above is a lock timeout, something is still connected to the database (a backup in progress, a shell); see pg_stat_activity. Investigate before retrying."
    fi
    ok "Database restored"
    cp "$latest/.env" ./.env
    ok ".env restored"

    # Code after the database, deliberately. A failed database restore leaves
    # everything untouched (above); a failure here leaves the old database under
    # new code, which the recreate below resolves by re-migrating — consistent,
    # if not what was asked for. The reverse order would leave new code with no
    # way back.
    if [ -f "$latest/code.tar.gz" ]; then
        info "Restoring code"
        # A replace, not an overlay. Extracting the snapshot over the tree would
        # restore every old file and delete none of the new ones — so a
        # migration added by the update survives, the recreate applies it again,
        # and a rollback of a destructive migration destroys the data it just
        # recovered. rsync --delete is what makes the tree actually match the
        # snapshot.
        #
        # The excludes are load-bearing in the other direction: --delete against
        # the deployment root would otherwise remove the snapshot being restored
        # from, the archive drop directory, and .env.
        #
        # -m on extraction: do not restore mtimes, so a root-owned path in a
        # future snapshot cannot fail an entire recovery on "Cannot utime".
        rtmp="$(mktemp -d)"
        if tar -xzmf "$latest/code.tar.gz" -C "$rtmp" \
            && rsync -a --delete \
                --exclude=".env" \
                --exclude="backups/" \
                --exclude="update/" \
                --exclude="static/" \
                --exclude="frontend/vendor/" \
                --exclude="frontend/node_modules/" \
                --exclude=".git/" \
                "$rtmp"/ ./; then
            rm -rf "$rtmp"
            ok "Code restored"
        else
            rm -rf "$rtmp"
            die "Code restore FAILED. The database and .env are already rolled back; the tree is in an unknown state. Re-apply a known archive before starting the stack."
        fi
        # The images carry the code (Dockerfile: COPY . .), so restoring the
        # tree changes nothing that runs until they are rebuilt. Every service
        # with a build section, not just web: migrate declares its own, so
        # `build web` leaves the migrate image holding the code being rolled
        # back — and the recreate below then applies the very migrations the
        # rollback just undid, to the data it just restored. Observed exactly
        # that before this line said `build` instead of `build web`. The vendor
        # profile for the same reason the update build names it: the vendoring
        # steps below run from that image.
        info "Rebuilding images from the restored code"
        "${COMPOSE[@]}" --profile vendor build
        ok "Images rebuilt"
        CODE_RESTORED=true
    else
        CODE_RESTORED=false
        warn "Snapshot $(basename "$latest") predates code snapshots — restoring data only."
        warn "The recreate below will run the current code against the restored database, and"
        warn "any migration newer than the snapshot will be re-applied. If one of them destroyed"
        warn "data, it is about to destroy it again. Re-apply a known-good archive first."
    fi

    # The same tail an update runs after its build. Static storage is not
    # manifest-based, so a stale hashed file would merely linger; the vendored
    # trees are what matter, since a newer Pyodide closure vendored by the
    # failed release would otherwise sit under the restored viewer.
    collect_static
    refresh_vendored_assets
    recreate_stack
    wait_for_health
    report_vendor_failures
    echo
    if [ "$CODE_RESTORED" = true ]; then
        ok "Rollback complete — database, .env and code restored."
    else
        warn "Rolled back the database and .env, not the code/image."
        ok "Rollback complete."
    fi
    exit 0
fi

# ── Mode validation ───────────────────────────────────────────────────────────

case "$MODE" in
    archive|repo) ;;
    *) die "Unknown --from mode: '$MODE' (expected 'archive' or 'repo')." ;;
esac

info "Updating from $MODE (backup: $([ "$BACKUP" = true ] && echo on || echo off), overlay: production)"

# ── Preflight: the tree must belong to the account the containers run as ──────
# web, celery, celery-beat and migrate all run as uid/gid 1000 against a bind mount
# of this directory, so a tree owned by anyone else comes up and then fails on its
# first write, far from the cause. The way it happens is an archive: tar records
# the *builder's* uid, and an update applied as root preserves it, so a package
# built on a laptop can hand a deployment a tree its own account cannot write.
# Checked here, before anything is touched, rather than discovered later.
#
# Linux only, which is what gating on `stat -c` amounts to: Docker Desktop maps
# ownership inside its own VM, where none of this applies. Fatal in archive mode,
# which is the case that breaks; a warning in repo mode, where a checkout owned by
# a developer's own uid is a legitimate arrangement this cannot tell apart.

if DIR_MODE="$(stat -c %a . 2>/dev/null)"; then
    DIR_UID="$(stat -c %u .)"
    DIR_GID="$(stat -c %g .)"
    DIR_MODE="$(printf '%04d' "$DIR_MODE")"
    TREE_WRITABLE=false
    if [ "$DIR_UID" = "1000" ] && [ "$(( ${DIR_MODE:1:1} & 2 ))" -ne 0 ]; then
        TREE_WRITABLE=true
    fi
    if [ "$DIR_GID" = "1000" ] && [ "$(( ${DIR_MODE:2:1} & 2 ))" -ne 0 ]; then
        TREE_WRITABLE=true
    fi
    if [ "$(( ${DIR_MODE:3:1} & 2 ))" -ne 0 ]; then
        TREE_WRITABLE=true
    fi
    if [ "$TREE_WRITABLE" = false ]; then
        if [ "$MODE" = archive ]; then
            die "$ROOT is not writable by uid 1000, the user every container runs as \
(owner ${DIR_UID}:${DIR_GID}, mode ${DIR_MODE}). The update would apply and the stack \
would then fail on its first write. Fix the ownership and re-run:
    sudo chown -R 1000:1000 $ROOT"
        else
            warn "$ROOT is owned by ${DIR_UID}:${DIR_GID} and is not writable by uid 1000,"
            warn "which every container runs as. If this is a deployment rather than a"
            warn "development checkout, fix it with: sudo chown -R 1000:1000 $ROOT"
        fi
    fi
fi

# ── 0. Snapshot the current code, BEFORE anything overwrites it ───────────────
# The maintenance flag goes up first. The database dump is taken in step 2 and
# the services keep running until step 4, with the image build in between —
# minutes on a small host — so every write in that gap is one a rollback loses.
# With the flag up, the platform declines them instead.
write_maintenance_flag updating "The platform is being updated."

# Placement is the whole point. Step 1 rsyncs the new tree over the deployment,
# so a code snapshot taken with the database in step 2 captures the *new* code
# and is worthless for rollback — the restore puts the failing version back and
# the recreate re-applies the migrations being rolled back. The database dump
# can wait for step 2 because migrations do not run until step 5; the code
# cannot.
#
# Retaining "the previous archive" instead would be cheaper and does not work:
# this script never moves, copies or records the archive it applied, so after a
# few updates nothing identifies the deployed lineage.
#
# Excluded: the snapshots themselves (which would nest), the archive drop
# directory, .env (saved with the database), git history, build caches — and
# static/ plus frontend/vendor, which the containers write as root, so an
# unprivileged restore cannot set their timestamps and tar fails the whole
# extraction on "Cannot utime". static/ is regenerated by the collectstatic step
# below; frontend/vendor is not, which is why the vendoring step re-vendors it
# whenever the tree does not match its own lock.
stamp="$(date -u +%Y%m%d-%H%M%S)"
snap="$BACKUP_DIR/pre-update-$stamp"
if [ "$BACKUP" = true ]; then
    mkdir -p "$snap"
    info "Snapshotting current code to $snap"
    if tar -czf "$snap/code.tar.gz" \
            --exclude="./backups" \
            --exclude="./update" \
            --exclude="./.env" \
            --exclude="./.git" \
            --exclude="./frontend/node_modules" \
            --exclude="./static" \
            --exclude="./frontend/vendor" \
            --exclude="__pycache__" \
            -C . . 2>/dev/null; then
        ok "Code snapshotted ($(du -h "$snap/code.tar.gz" | cut -f1))"
        # Run as root, the snapshot belongs to root, and the deploy account can
        # then neither prune it nor restore from it later. Hand it to whoever
        # owns the tree. GNU stat only, like the ownership preflight.
        if [ "$(id -u)" = 0 ]; then
            owner="$(stat -c %u:%g . 2>/dev/null || true)"
            [ -n "$owner" ] && chown -R "$owner" "$snap"
        fi
    else
        rm -rf "$snap"
        die "Code snapshot failed; aborting before any change. (Pass --no-backup to override.)"
    fi
fi

# ── 1. Acquire source ─────────────────────────────────────────────────────────

if [ "$MODE" = archive ]; then
    if [ -z "$ARCHIVE" ]; then
        # shellcheck disable=SC2012  # newest-by-mtime over a controlled glob.
        ARCHIVE="$(ls -1t "$UPDATE_DIR"/epicurrents*.tar.gz 2>/dev/null | head -1 || true)"
        [ -n "$ARCHIVE" ] || die "No archive in $UPDATE_DIR/ (looked for epicurrents*.tar.gz). Drop the distribution there or pass --archive FILE."
    fi
    [ -f "$ARCHIVE" ] || die "Archive not found: $ARCHIVE"
    command -v rsync >/dev/null 2>&1 || die "rsync is required for archive mode (apt-get install rsync)."

    info "Applying archive: $ARCHIVE"
    tmp="$(mktemp -d)"   # removed by the exit trap
    tar -xzf "$ARCHIVE" -C "$tmp"
    # Distribution tars wrap their contents in a single versioned top-level dir;
    # descend into it so the sync targets the deployment files, not the wrapper.
    src="$tmp"
    if [ ! -f "$src/docker-compose.yml" ]; then
        # Descend into the wrapper. Directories only, and AppleDouble siblings
        # excluded: macOS tar stores extended attributes as ._* members, which
        # GNU tar materialises as real files on extraction. A counting test
        # ("exactly one entry, so it is the wrapper") then sees two entries and
        # declines to descend, and the archive is rejected as malformed — so a
        # distribution built on a Mac cannot be applied on Linux, with an error
        # naming the wrong cause.
        only="$(find "$tmp" -mindepth 1 -maxdepth 1 -type d ! -name '._*' | head -1)"
        [ -n "$only" ] && src="$only"
    fi
    [ -f "$src/docker-compose.yml" ] || die "Archive does not look like an Epicurrents distribution (no docker-compose.yml at its root)."

    # Refresh the platform-owned, regenerable bundle dirs so stale content-hashed
    # chunks from prior releases don't pile up. These are the only trees we
    # actively prune; the root sync below is overlay-only (no --delete), so any
    # file the operator added that the archive doesn't carry — a
    # docker-compose.override.yml, certs, a .env.local, a .git checkout — is
    # always preserved. A denylist --delete at the deployment root would silently
    # wipe exactly those.
    # Empty these, do not replace them. `rm -rf` followed by rsync recreates the
    # directory with a NEW inode, and the running caddy container bind-mounts the
    # old one — which it keeps, now orphaned and empty, so every /assets/ and
    # /viewer/ request 404s. The site is then down for anyone without a warm
    # cache, and invisible to everyone with one, because the bundles are
    # content-hashed and served `immutable`: the operator who just ran the update
    # reloads, sees their cached copy, and concludes it worked. Caddy is
    # recreated below as well, but keeping the inode is what makes the window
    # zero rather than merely short.
    for d in frontend/dist frontend/viewer-dist; do
        if [ -d "$src/$d" ] && [ -d "${ROOT:?}/$d" ]; then
            find "${ROOT:?}/$d" -mindepth 1 -delete
        elif [ -d "$src/$d" ]; then
            rm -rf "${ROOT:?}/$d"
        fi
    done

    # Overlay the new tree. rsync replaces files via atomic rename, so
    # overwriting this running script is safe — the shell keeps reading the
    # original inode. The excludes keep operator state from being overwritten
    # even if a future archive happens to carry one of these paths.
    info "Updating files (preserving .env, data, backups, update)"
    rsync -a \
        --exclude='/.env' \
        --exclude='/backups/' \
        --exclude='/update/' \
        --exclude='/static/' \
        "$src"/ "$ROOT"/
    ok "Files updated"
else
    git rev-parse --is-inside-work-tree >/dev/null 2>&1 \
        || die "repo mode needs a git checkout. In a distribution deployment use archive mode (the default)."
    if [ "$PULL" = true ]; then
        info "Pulling latest code"
        if [ -n "$REF" ]; then
            git fetch origin --tags --prune
            git checkout "$REF"
            # Fast-forward a tracking branch to its upstream; a no-op for a tag
            # or detached SHA (no origin/<ref> to merge).
            git merge --ff-only "origin/$REF" 2>/dev/null || true
        else
            git pull --ff-only \
                || die "Cannot fast-forward the current branch — it has diverged from upstream. Resolve manually; update.sh never force-merges."
        fi
        ok "Code up to date: $(git log -1 --format='%h %s')"
        info "Updating submodules"
        git submodule update --init --recursive
        ok "Submodules up to date"
        # The active project is a separate repository cloned into projects/<name>/,
        # not a submodule, so neither the pull above nor the submodule update
        # reaches it. Without this it stays at whatever bootstrap cloned while the
        # platform moves — and the build below would bake that stale tree into the
        # image, dependencies and Python source alike, without failing.
        #
        # Only a branch tracking an upstream is moved. Pinning a project to a tag
        # or a commit is a manual operation today (see bootstrap.sh step 4c), and
        # an unconditional pull is exactly what would undo it.
        # The three states are told apart rather than collapsed, because the
        # remedy differs and each looks the same from the build's side. `rev-parse
        # --git-dir` is the repo test rather than a check for a .git directory: a
        # worktree or a submodule checkout has .git as a *file*.
        project="$(grep -E '^EPICURRENTS_PROJECT=' .env | head -1 | cut -d= -f2 | tr -d ' "'"'"'' || true)"
        if [ -n "$project" ] && [ -d "projects/$project" ]; then
            if ! git -C "projects/$project" rev-parse --git-dir > /dev/null 2>&1; then
                warn "projects/$project is not a git checkout — update it yourself before the build."
            elif ! git -C "projects/$project" rev-parse --abbrev-ref --symbolic-full-name '@{u}' > /dev/null 2>&1; then
                warn "projects/$project is pinned (no upstream branch) — leaving it as it is."
            else
                info "Pulling project $project"
                git -C "projects/$project" pull --ff-only \
                    || die "Cannot fast-forward projects/$project — it has diverged from upstream. Resolve manually; update.sh never force-merges."
                ok "Project up to date: $(git -C "projects/$project" log -1 --format='%h %s')"
            fi
        fi
    else
        info "Skipping git pull (--no-pull); rebuilding the current checkout"
    fi
    info "Building frontend bundles (Node container)"
    "${COMPOSE[@]}" --profile build run --rm frontend-build
    ok "Frontend bundles built"
fi

# ── 2. Back up before mutating the database ───────────────────────────────────

if [ "$BACKUP" = true ]; then
    ensure_db_up
    # $snap already exists and holds code.tar.gz from step 0.
    info "Backing up to $snap"
    # Local snapshot = database + .env: small, fast, and the part an update can
    # destroy. Recording / media file volumes are out of scope here — they are
    # untouched by migrations; enable borg for full data-volume backups.
    # SC2016: $POSTGRES_* must expand inside the db container's shell, not here.
    # shellcheck disable=SC2016
    if "${COMPOSE[@]}" exec -T db sh -c 'pg_dump --clean --if-exists --no-owner -U "$POSTGRES_USER" "$POSTGRES_DB"' \
            | gzip > "$snap/db.sql.gz"; then
        ok "Database dumped ($(du -h "$snap/db.sql.gz" | cut -f1))"
    else
        rm -rf "$snap"
        die "Database dump failed; aborting before any change. (Pass --no-backup to override.)"
    fi
    cp .env "$snap/.env"

    {
        echo "timestamp_utc=$stamp"
        echo "mode=$MODE"
        echo "code_snapshot=yes"
        if [ "$MODE" = archive ]; then
            echo "archive=$ARCHIVE"
        else
            echo "git_ref=$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
        fi
    } > "$snap/MANIFEST"
    ok ".env + manifest saved"

    # If borgmatic is wired up and running, take a full backup too (data volumes).
    if [ -x ./scripts/backup.sh ] && service_running borg; then
        info "Borg is enabled — taking a full backup"
        ./scripts/backup.sh || warn "Borg backup reported an error; the local snapshot is still in place."
    fi

    prune_backups
else
    warn "Skipping backup (--no-backup)."
fi

# ── 3. Build the image ────────────────────────────────────────────────────────

info "Building Docker images"
# --profile vendor so the one-shot writer used by the vendoring step is built
# here with everything else. Its image is the same one web runs, so this costs a
# cache hit and a tag; left out, the build happens inside that step instead,
# where a failure reads as a vendoring failure.
"${COMPOSE[@]}" --profile vendor build
ok "Images built"

# ── 4. Stop application services (so nothing races the schema change) ─────────

stop_app_services

# ── 5. Apply ALL pending migrations ───────────────────────────────────────────

ensure_db_up
info "Applying database migrations"
"${COMPOSE[@]}" run --rm --no-deps web python manage.py migrate
ok "Migrations applied"

# ── 6. Collect static files, refresh the vendored trees ───────────────────────

collect_static
refresh_vendored_assets

# ── 7. Recreate the application containers ────────────────────────────────────

recreate_stack

# ── 8. Health check + summary ─────────────────────────────────────────────────

wait_for_health
report_vendor_failures

echo
"${COMPOSE[@]}" ps
echo
ok "Update complete."
if [ "$BACKUP" = true ]; then
    echo "    Roll back with: ./update.sh --rollback"
fi
