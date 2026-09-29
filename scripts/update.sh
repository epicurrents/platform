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
#                    A package's manifest and signature, when they sit beside
#                    it, are verified before anything is extracted.
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
#                    rolled back. With --code-only the database and .env are
#                    kept, which is allowed only while the set of applied
#                    migrations is the one the snapshot recorded.
#
# While it runs, the script keeps a maintenance flag at update/maintenance.json
# (phase "updating" or "rolling_back"). The platform reads it to suspend
# requests for the duration, and the flag is removed at exit unless the run
# left the stack stopped, or --keep-lock says the caller owns it.
#
# Progress is also reported on lines starting with "::" (::step=…, ::snapshot=…,
# ::health=…, ::migrations=…, ::restored=…, ::done, ::failed=…, and
# ::refused=<reason> ahead of a refusal the checks can name) for a caller that
# drives this script.
#
# Usage:
#   ./update.sh                          archive mode, newest ./update/epicurrents*.tar.gz
#   ./update.sh --archive ./foo.tar.gz   archive mode, explicit file
#   ./update.sh --check-archive FILE     verify a package — signature, hash, contents,
#                                        version — and report what an update would
#                                        prune, without touching the deployment
#   ./update.sh --require-signature      refuse a package without a signature that
#                                        verifies
#   ./update.sh --release-key PATH       public key to verify against; repeatable,
#                                        the first that verifies wins (default:
#                                        RELEASE_KEY.pub at the deployment root and
#                                        RELEASE_KEY.next.pub beside it when present)
#   ./update.sh --require-newer          refuse a package whose version is not
#                                        greater than the installed one
#   ./update.sh --allow-unsigned         apply a package without a verifying signature
#                                        although a release key is present (refused
#                                        otherwise)
#   ./update.sh --allow-downgrade        apply a package older than the installed
#                                        release (refused otherwise)
#   ./update.sh --from repo              repo mode, git pull + frontend build
#   ./update.sh --from repo --no-pull    repo mode, rebuild the current checkout
#   ./update.sh --no-backup              skip the pre-update snapshot (not advised —
#                                        it is what --rollback restores from)
#   ./update.sh --snapshot LABEL         take a snapshot (code, database, .env) named
#                                        backups/LABEL-<stamp> and exit
#   ./update.sh --rollback               undo the last update (database, .env, code)
#   ./update.sh --rollback --snapshot NAME   restore backups/NAME instead of the newest
#   ./update.sh --rollback --code-only   restore the code and rebuild, keeping the
#                                        database and .env; refused when a migration
#                                        was applied since the snapshot
#   ./update.sh --skip-beat              leave celery-beat stopped after the recreate,
#                                        so no scheduled purge runs before the update
#                                        is verified; start it yourself afterwards
#   ./update.sh --root DIR               operate on the deployment at DIR instead of
#                                        the one this script sits in
#   ./update.sh --keep-lock              leave update/maintenance.json in place at
#                                        exit (for a caller that manages the flag)
#
set -euo pipefail

# Bumped whenever a package starts relying on something an older copy of this
# script does not do; a package's manifest names the minimum it needs. 3 added
# --code-only, repeatable --release-key and the migrations record in snapshots;
# 4 made a present release key require a signature, refused downgrades, took
# the database dump before the tree changes, and exits non-zero on a failed
# health check.
UPDATER_SCRIPT_VERSION=4

info() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
ok()   { printf '    \033[32m✓\033[0m  %s\n'  "$*"; }
warn() { printf '    \033[33m!\033[0m  %s\n'  "$*"; }
# Machine-readable progress, one fact per line, for a caller that tees this
# script's output. Kept to a small vocabulary: step, snapshot, health, done,
# failed, and the facts --check-archive reports.
emit() { printf '::%s\n' "$*"; }
die()  {
    local msg="$*"
    printf '\n\033[1;31mERROR:\033[0m %s\n' "$msg" >&2
    emit "failed=${msg%%$'\n'*}"
    exit 1
}
# A refusal with a reason a caller can key on: the host agent turns the token
# into the job's failure reason, where the free text of the message would not
# survive as a stable identifier. The vocabulary is the one the agent knows —
# signature, hash, manifest, contents, updater_too_old, incompatible,
# version_not_newer, code_only, disk, locked, unfinished — and is pinned in
# scripts/tests/test_update_targets.py.
refuse() {
    emit "refused=$1"
    shift
    die "$@"
}

usage() {
    # The header comment, from line 2 to the first line that is not a comment.
    # One awk, not a sed pipeline: a reader that quits early kills the writer
    # with SIGPIPE, which pipefail turns into exit 141 from --help.
    awk 'NR == 1 { next } /^#/ { sub(/^#{1,2} ?/, ""); print; next } { exit }' "$0"
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
MODE_GIVEN=false
ARCHIVE=""
CHECK_ARCHIVE=""
REF=""
PULL=true
BACKUP=true
ROLLBACK=false
ASSUME_YES=false
SKIP_BEAT=false
KEEP_LOCK=false
REQUIRE_SIGNATURE=false
REQUIRE_NEWER=false
ALLOW_UNSIGNED=false
ALLOW_DOWNGRADE=false
RELEASE_KEYS=()
CODE_ONLY=false
SNAPSHOT=""
ROOT=""

while [ $# -gt 0 ]; do
    case "$1" in
        --from)      require_value --from "${2:-}";    MODE="$2"; MODE_GIVEN=true; shift 2 ;;
        --from=*)    MODE="${1#*=}"; MODE_GIVEN=true; shift ;;
        --archive)   require_value --archive "${2:-}"; ARCHIVE="$2"; shift 2 ;;
        --archive=*) ARCHIVE="${1#*=}"; shift ;;
        --check-archive)   require_value --check-archive "${2:-}"; CHECK_ARCHIVE="$2"; shift 2 ;;
        --check-archive=*) CHECK_ARCHIVE="${1#*=}"; shift ;;
        --release-key)     require_value --release-key "${2:-}"; RELEASE_KEYS+=("$2"); shift 2 ;;
        --release-key=*)   RELEASE_KEYS+=("${1#*=}"); shift ;;
        --code-only) CODE_ONLY=true; shift ;;
        --snapshot)  require_value --snapshot "${2:-}"; SNAPSHOT="$2"; shift 2 ;;
        --snapshot=*) SNAPSHOT="${1#*=}"; shift ;;
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
        --require-signature) REQUIRE_SIGNATURE=true; shift ;;
        --require-newer)     REQUIRE_NEWER=true; shift ;;
        --allow-unsigned)    ALLOW_UNSIGNED=true; shift ;;
        --allow-downgrade)   ALLOW_DOWNGRADE=true; shift ;;
        --yes|-y)    ASSUME_YES=true; shift ;;
        -h|--help)   usage; exit 0 ;;
        *)           die "Unknown argument: $1 (try --help)" ;;
    esac
done

# --snapshot means two things by mode: with --rollback it names the snapshot to
# restore; alone it is the snapshot-only mode. Combined with an update source it
# is ambiguous, so it is refused rather than guessed at.
SNAPSHOT_ONLY=false
if [ -n "$SNAPSHOT" ] && [ "$ROLLBACK" = false ]; then
    if [ "$MODE_GIVEN" = true ] || [ -n "$ARCHIVE" ]; then
        die "--snapshot LABEL takes a snapshot and exits; it does not combine with --from or --archive. An update always snapshots as pre-update-<stamp>."
    fi
    SNAPSHOT_ONLY=true
fi
if [ "$CODE_ONLY" = true ] && [ "$ROLLBACK" = false ]; then
    die "--code-only applies to --rollback only."
fi

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
# A file named on the command line is relative to where the command was typed,
# not to the deployment root this script is about to change into.
absolute_from_caller() {
    case "$1" in
        ""|/*) printf '%s' "$1" ;;
        *)     printf '%s/%s' "$PWD" "$1" ;;
    esac
}
ARCHIVE="$(absolute_from_caller "$ARCHIVE")"
CHECK_ARCHIVE="$(absolute_from_caller "$CHECK_ARCHIVE")"
if [ "${#RELEASE_KEYS[@]}" -gt 0 ]; then
    for i in "${!RELEASE_KEYS[@]}"; do
        RELEASE_KEYS[i]="$(absolute_from_caller "${RELEASE_KEYS[i]}")"
    done
fi
cd "$ROOT"
# The physical path, for deciding whether a path this script is about to
# delete really lies under the tree.
ROOT_REAL="$(pwd -P)"

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
# mount, and only a rootful runtime maps that uid to the deployment's owner —
# through sudo when this script is not root already, and directly when it is,
# since a root-owned updater's host may not have sudo at all.
if command -v docker >/dev/null 2>&1 && ! docker --version 2>&1 | grep -qi podman; then
    CONTAINER_RUNTIME=docker
    COMPOSE=(docker compose -f docker-compose.yml -f docker-compose.prod.yml)
elif command -v podman >/dev/null 2>&1; then
    CONTAINER_RUNTIME=podman
    if [ "$(id -u)" = 0 ]; then
        COMPOSE=(podman compose -f docker-compose.yml -f docker-compose.prod.yml)
    else
        COMPOSE=(sudo -E podman compose -f docker-compose.yml -f docker-compose.prod.yml)
    fi
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
# An update that began changing the tree and has neither completed nor been
# rolled back names its pre-update snapshot here. While it does, a new update
# is refused and that snapshot is never pruned: retrying a failed update would
# otherwise snapshot the half-updated tree each time and rotate the real
# pre-update snapshot out.
UNFINISHED_MARKER="$BACKUP_DIR/.update-unfinished"
UNFINISHED_SNAPSHOT=""
# The installed release's file list, kept so the next update can prune what it
# shipped and the new package does not. A root file, so the code snapshot
# carries it and a rollback restores it with the code it describes.
INSTALLED_FILELIST="./.epicurrents-files"
# How long the restore waits for the locks the schema drop needs before giving
# up. Anything still connected — a backup dumping the database, a stray shell —
# holds a share lock the drop cannot get past, and without a bound the rollback
# hangs rather than failing. Overridable for tests; there is no reason to change
# it on a deployment.
RESTORE_LOCK_TIMEOUT="${UPDATE_RESTORE_LOCK_TIMEOUT:-60s}"

[ -f .env ] || die "No .env in $ROOT — initialize the deployment first (./start.sh in a distribution, or 'python manage.py init_env' in a checkout)."
# --check-archive reads the package and the tree; it drives no container.
[ -n "$CHECK_ARCHIVE" ] || [ -n "$CONTAINER_RUNTIME" ] \
    || die "No container runtime found — this needs Docker Engine or Podman, either one with Compose v2."

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

env_value() {
    # $1 = key in .env. Quotes and surrounding spaces dropped; empty if absent.
    grep -E "^$1=" .env 2>/dev/null | sed -n '1{s/^[^=]*=//; s/^[[:space:]]*//; s/[[:space:]]*$//; s/^"\(.*\)"$/\1/; s/^'"'"'\(.*\)'"'"'$/\1/; p;}'
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

installed_version() {
    # The version of the code in the tree, from the one module that carries it.
    # Empty when the tree has none (a checkout that predates the module).
    sed -n '/^__version__ = "/{s/^__version__ = "\([^"]*\)".*/\1/p;q;}' "$ROOT/epicurrents/version.py" 2>/dev/null || true
}

applied_migrations() {
    # The migrations the database records as applied, one app.name per line,
    # sorted. Read from the table rather than through showmigrations, which
    # lists only the migration files the current code carries. Empty when the
    # table does not exist yet, which is a database nothing has migrated.
    # Non-zero when the database could not be asked: an empty answer then
    # would read as "nothing changed" and license a code-only rollback.
    local rows err rc=0
    err="$(mktemp)"
    # SC2016: $POSTGRES_* must expand inside the db container's shell, not here.
    # shellcheck disable=SC2016
    rows="$("${COMPOSE[@]}" exec -T db sh -c 'psql -v ON_ERROR_STOP=1 -At -F . -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT app, name FROM django_migrations ORDER BY 1, 2"' 2>"$err")" \
        || rc=$?
    if [ "$rc" -ne 0 ]; then
        if grep -q 'relation "django_migrations" does not exist' "$err"; then
            rc=0
            rows=""
        fi
    fi
    rm -f "$err"
    [ "$rc" -eq 0 ] || return 1
    [ -z "$rows" ] || printf '%s\n' "$rows" | LC_ALL=C sort
}

version_gt() {
    # $1 > $2, both plain MAJOR.MINOR.PATCH — the only shape the platform's
    # version module admits, so no pre-release ordering is needed here.
    local a b i
    IFS=. read -r -a a <<< "$1"
    IFS=. read -r -a b <<< "$2"
    for i in 0 1 2; do
        if [ "${a[i]:-0}" -gt "${b[i]:-0}" ]; then return 0; fi
        if [ "${a[i]:-0}" -lt "${b[i]:-0}" ]; then return 1; fi
    done
    return 1
}

is_version() {
    [[ "$1" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]
}

sha256_of() {
    # The caller has checked that one of the two exists: a die() here would
    # exit only the command substitution it runs in.
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | awk '{print $1}'
    else
        shasum -a 256 "$1" | awk '{print $1}'
    fi
}

require_sha256_tool() {
    command -v sha256sum >/dev/null 2>&1 || command -v shasum >/dev/null 2>&1 \
        || die "Neither sha256sum nor shasum is available; the package hash cannot be checked."
}

# ── Private working space and mutual exclusion ────────────────────────────────
# update/ is bind-mounted read-write into the web containers, so whatever sits
# there may have been put there by a compromised web tier — and this script
# may run as root. Nothing is written through a name in update/: files are
# built in a private directory and renamed into place, and an archive picked
# up from there is copied out before it is verified. The private directory
# lives under backups/, which no container mounts, so it shares the
# deployment's filesystem (a rename stays a rename) and its disk check.

PRIVATE=""

private_dir() {
    # Created on first use, root's (or the runner's) alone, removed at exit.
    if [ -z "$PRIVATE" ]; then
        mkdir -p "$BACKUP_DIR"
        PRIVATE="$(mktemp -d "$BACKUP_DIR/.update-private.XXXXXX")"
        chmod 0700 "$PRIVATE"
    fi
    printf '%s' "$PRIVATE"
}

take_run_lock() {
    # One mutating run at a time. Two runs would prune each other's snapshots
    # and interleave their overlays; the lock file is under backups/, out of
    # the web tier's reach. Held until exit. Without flock (not a deployment
    # host) the runs are the operator's to keep apart.
    command -v flock >/dev/null 2>&1 || return 0
    mkdir -p "$BACKUP_DIR"
    exec 8>"$BACKUP_DIR/.update.lock"
    flock -n 8 || refuse locked "Another update.sh is running against $ROOT. Wait for it to finish."
}

# ── The maintenance flag ──────────────────────────────────────────────────────
# A file rather than a database row, because the rollback restores the database
# and would erase a row mid-way through the very operation the flag announces.
# It lives under update/, which every sync and snapshot in this script excludes,
# so neither direction of an update touches it. Written before anything else
# changes and removed by the exit trap below; a caller that manages the flag's
# lifecycle itself passes --keep-lock, and a pre-existing flag is then left as
# it is rather than overwritten, since it carries that caller's own fields.
# Without --keep-lock a flag this run did not write is a refusal: it belongs to
# the host agent mid-job, or to a run that left the stack stopped.

FLAG_WRITTEN=false
SERVICES_STOPPED=false
WORKERS_STOPPED=false
TREE_CHANGED=false
tmp=""

flag_present() {
    [ -e "$MAINTENANCE_FLAG" ] || [ -L "$MAINTENANCE_FLAG" ]
}

refuse_foreign_flag() {
    if [ "$KEEP_LOCK" = false ] && flag_present; then
        refuse locked "A maintenance flag is already up ($MAINTENANCE_FLAG): the host agent is carrying out a job, or an earlier run left the stack stopped. Wait for the job, or remove the file once the deployment is known to be in order, then re-run."
    fi
}

write_maintenance_flag() {
    # $1 = phase, $2 = message for the people locked out.
    if [ "$KEEP_LOCK" = true ] && flag_present; then
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
    local now staged
    now="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    staged="$(private_dir)/maintenance.json"
    printf '{"protocol": 1, "phase": "%s", "job_id": null, "since": "%s", "expected_until": null, "message": "%s"}\n' \
        "$1" "$now" "$2" > "$staged"
    chmod 0644 "$staged"
    # A rename replaces whatever stands at the name — a link included — and
    # never writes through it. -T keeps a directory at the name from turning
    # the rename into a move into it; where mv has no -T, such a name is
    # refused instead.
    if mv -T "$staged" "$MAINTENANCE_FLAG" 2>/dev/null; then
        :
    elif [ -d "$MAINTENANCE_FLAG" ] || [ -L "$MAINTENANCE_FLAG" ]; then
        die "$MAINTENANCE_FLAG is a directory or a link; remove it and re-run."
    else
        mv -f "$staged" "$MAINTENANCE_FLAG"
    fi
    FLAG_WRITTEN=true
}

cleanup() {
    local rc=$?
    [ -n "$tmp" ] && rm -rf "$tmp"
    [ -n "$PRIVATE" ] && rm -rf "$PRIVATE"
    if [ "$rc" -ne 0 ] && [ "$TREE_CHANGED" = false ] && [ "$WORKERS_STOPPED" = true ] && [ "$SERVICES_STOPPED" = false ]; then
        # Stopped for the dump, and the run ended before it changed anything:
        # bring the workers back as they were.
        warn "Restarting the workers stopped for the snapshot"
        if [ "$SKIP_BEAT" = true ]; then
            "${COMPOSE[@]}" up -d celery || true
        else
            "${COMPOSE[@]}" up -d celery celery-beat || true
        fi
    fi
    if [ "$rc" -ne 0 ] && [ "$TREE_CHANGED" = true ] && [ "$ROLLBACK" = false ]; then
        warn "The run failed after it began changing the deployment. The pre-update snapshot is complete:"
        warn "  ./update.sh --rollback${UNFINISHED_SNAPSHOT:+ --snapshot $UNFINISHED_SNAPSHOT}"
        warn "restores the code, the database and .env as they were before this run."
    fi
    if [ "$FLAG_WRITTEN" = true ] && [ "$KEEP_LOCK" = false ]; then
        if [ "$rc" -ne 0 ] && { [ "$SERVICES_STOPPED" = true ] || [ "$TREE_CHANGED" = true ]; }; then
            # The stack was taken down and not brought back, or the tree is
            # half-updated, so the platform is not serving what it should; the
            # flag is what tells anyone who reaches it why. Removing it would
            # let writes onto a deployment that is about to be rolled back.
            warn "Leaving the maintenance flag in place ($MAINTENANCE_FLAG). Remove the file once the"
            warn "deployment is repaired or rolled back."
        else
            rm -f "$MAINTENANCE_FLAG"
        fi
    fi
}
trap cleanup EXIT

# ── Package verification ──────────────────────────────────────────────────────
# Everything an archive is checked for before a byte of it is extracted, in the
# order that matters: the signature first, because it is what makes the rest
# of the manifest worth reading; then the hash, which is what binds the tarball
# to the manifest; then the listing, which is read but never unpacked; then the
# versions. --check-archive runs exactly this and stops. The update path runs
# it before the snapshot, so a refused package changes nothing.

ARCHIVE_TOP=""
ARCHIVE_PREFIX=""
ARCHIVE_UNPACKED_BYTES=0
PKG_MANIFEST=""
PKG_VERSION=""
PKG_SHA256=""
INSTALLED_VERSION=""
SIGNATURE_STATE=unsigned

manifest_value() {
    # $1 = manifest path, $2 = key. Prints the value: strings unquoted, null as
    # empty, a list as comma-separated items. Relies on the shape the packager
    # writes — one key per line, lists inline — which release_sign.py fixes.
    local raw
    raw="$(sed -n "s/^  \"$2\": \(.*\)\$/\1/p" "$1")"
    raw="${raw%$'\r'}"
    raw="${raw%,}"
    case "$raw" in
        \"*\") raw="${raw#\"}"; raw="${raw%\"}" ;;
        null)  raw="" ;;
        \[*\]) raw="${raw#[}"; raw="${raw%]}"; raw="$(printf '%s' "$raw" | tr -d '" ')" ;;
    esac
    printf '%s' "$raw"
}

normalise_list() {
    # A comma-separated list, sorted and deduplicated, so two spellings of the
    # same plugin set compare equal.
    printf '%s' "$1" | tr ',' '\n' | sed '/^[[:space:]]*$/d; s/^[[:space:]]*//; s/[[:space:]]*$//' | LC_ALL=C sort -u | paste -sd, - 2>/dev/null || true
}

verify_manifest_signature() {
    # $1 = manifest, $2 = base64 signature file, $3 = public key (PEM).
    # 0: verifies. 1: does not. 2: no tool on this host can check it.
    # openssl pkeyutl handles Ed25519 from OpenSSL 3; LibreSSL and 1.1 do not,
    # so a host with an older one falls back to python3 with cryptography.
    local sigbin rc
    sigbin="$(mktemp)"
    if ! base64 -d < "$2" > "$sigbin" 2>/dev/null; then
        rm -f "$sigbin"
        return 1
    fi
    if command -v openssl >/dev/null 2>&1 && openssl version 2>/dev/null | grep -qE '^OpenSSL [3-9]'; then
        if openssl pkeyutl -verify -pubin -inkey "$3" -rawin -in "$1" -sigfile "$sigbin" >/dev/null 2>&1; then
            rc=0
        else
            rc=1
        fi
        rm -f "$sigbin"
        return $rc
    fi
    if command -v python3 >/dev/null 2>&1 && python3 -c 'import cryptography' 2>/dev/null; then
        if python3 - "$1" "$sigbin" "$3" <<'PY' >/dev/null 2>&1
import sys
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
manifest, signature, key = (open(path, "rb").read() for path in sys.argv[1:4])
try:
    serialization.load_pem_public_key(key).verify(signature, manifest)
except (InvalidSignature, ValueError):
    sys.exit(1)
PY
        then
            rc=0
        else
            rc=1
        fi
        rm -f "$sigbin"
        return $rc
    fi
    rm -f "$sigbin"
    return 2
}

VERIFIED_KEY=""

verify_against_keys() {
    # $1 = manifest, $2 = signature file, then the keys to try, in order.
    # 0 when one verifies (VERIFIED_KEY names it), 1 when none does, 2 when
    # nothing on this host can check a signature. A key that is not there is
    # skipped: the successor a release announces exists only after that
    # release was applied.
    local manifest="$1" sig="$2" key rc
    shift 2
    for key in "$@"; do
        [ -f "$key" ] || continue
        verify_manifest_signature "$manifest" "$sig" "$key" && rc=0 || rc=$?
        case "$rc" in
            0) VERIFIED_KEY="$key"; return 0 ;;
            1) ;;
            *) return 2 ;;
        esac
    done
    return 1
}

inspect_archive_listing() {
    # Reads the member list and refuses anything an overlay must not receive.
    # Sets ARCHIVE_TOP (the single wrapper directory) and ARCHIVE_PREFIX ("./"
    # when the archive was packed with a leading ./).
    local names verbose tops top count inner
    # Here-strings, never `printf … | grep -q`: under pipefail a grep that exits
    # on its first match kills a producer still writing with SIGPIPE, and a
    # listing longer than one stdio buffer then reads as "no match" — which
    # refused a valid package on a real host while the same check, run a
    # minute earlier, had passed.
    names="$(tar -tzf "$ARCHIVE" 2>/dev/null)" || refuse contents "Cannot read the archive: $ARCHIVE is not a gzipped tar file, or is truncated."
    [ -n "$names" ] || refuse contents "The archive is empty: $ARCHIVE"
    # Member names as tar will extract them: `a/./b` and `a//b` both land at
    # `a/b`, so the checks below look at the same spelling tar acts on.
    names="$(printf '%s\n' "$names" | sed 's|/\./|/|g; s|//*|/|g; s|/\./|/|g; s|/\.$||')"
    if grep -qE '^/|(^|/)\.\.(/|$)' <<< "$names"; then
        refuse contents "Refusing the archive: it carries an absolute path or a '..' component, which would extract outside the deployment."
    fi
    case "$names" in
        ./*) ARCHIVE_PREFIX="./" ;;
        *)   ARCHIVE_PREFIX="" ;;
    esac
    tops="$(printf '%s\n' "$names" | sed 's|^\./||; /^$/d; /^\.$/d; s|/.*||' | LC_ALL=C sort -u)"
    count="$(printf '%s\n' "$tops" | sed '/^$/d' | wc -l | tr -d ' ')"
    if [ "$count" != 1 ]; then
        if grep -q '^\._' <<< "$tops"; then
            refuse contents "Refusing the archive: it carries macOS extended-attribute members (._*). Rebuild it with COPYFILE_DISABLE=1, which the packager's --tarball does."
        fi
        refuse contents "Refusing the archive: expected exactly one top-level directory, found $count ($(printf '%s' "$tops" | tr '\n' ' ')). A distribution wraps its files in one versioned directory."
    fi
    top="$tops"
    # The paths inside the wrapper. Every member starts with the wrapper by
    # construction, so it is cut off rather than matched: a directory name is
    # data, and data built into a regular expression is a regular expression.
    inner="$(printf '%s\n' "$names" | sed 's|^\./||' | cut -s -d/ -f2- | sed '/^$/d')"
    if [ -z "$inner" ]; then
        refuse contents "Refusing the archive: its only top-level entry ($top) is not a directory."
    fi
    if grep -qE '^(\.env|\.git|backups|update|\.epicurrents-files)(/|$)' <<< "$inner"; then
        refuse contents "Refusing the archive: it carries .env, .git/, backups/, update/ or .epicurrents-files, which belong to the deployment and never to a package."
    fi
    if grep -qE '(^|/)\.git(/|$)' <<< "$names"; then
        refuse contents "Refusing the archive: it carries a .git directory."
    fi
    verbose="$(tar -tzvf "$ARCHIVE" 2>/dev/null)" || refuse contents "Cannot list the archive: $ARCHIVE"
    if grep -qE '^[lh]' <<< "$verbose"; then
        refuse contents "Refusing the archive: it carries symbolic or hard links. An overlay that follows a link can write outside the tree; the packager refuses to build one."
    fi
    # Device nodes and pipes have no place in a code tree, and an extraction run
    # as root would create them; setuid or setgid bits survive rsync -a the
    # same way and turn a file in a uid-1000 tree into something else.
    if grep -qE '^[bcp]' <<< "$verbose"; then
        refuse contents "Refusing the archive: it carries a device node or a named pipe."
    fi
    if grep -qE '^.{3}[sS]|^.{6}[sS]' <<< "$verbose"; then
        refuse contents "Refusing the archive: it carries a setuid or setgid file."
    fi
    # What the archive unpacks to, for the disk check before the overlay: the
    # size column of the verbose listing, summed.
    ARCHIVE_UNPACKED_BYTES="$(awk '{ total += $3 } END { printf "%d", total }' <<< "$verbose")"
    ARCHIVE_TOP="$top"
}

archive_member() {
    # $1 = path inside the wrapper directory. Streams the member to stdout, or
    # nothing when the archive has no such member; nothing touches the disk.
    tar -xzOf "$ARCHIVE" "${ARCHIVE_PREFIX}${ARCHIVE_TOP}/$1" 2>/dev/null || true
}

check_archive() {
    local manifest="$ARCHIVE.manifest.json" sig="$ARCHIVE.manifest.sig"
    local rc msg want have size want_size field pkg_project pkg_plugins my_project my_plugins keys present key
    info "Checking archive: $ARCHIVE"
    # The keys to try, in order. Without --release-key: the key at the
    # deployment root and, when a release announced a successor, the successor
    # beside it — the same pair the platform trusts at upload.
    keys=()
    if [ "${#RELEASE_KEYS[@]}" -gt 0 ]; then
        keys=("${RELEASE_KEYS[@]}")
    else
        keys=("$ROOT/RELEASE_KEY.pub")
        [ ! -f "$ROOT/RELEASE_KEY.next.pub" ] || keys+=("$ROOT/RELEASE_KEY.next.pub")
    fi
    present=""
    for key in "${keys[@]}"; do
        [ -f "$key" ] && present="$present${present:+, }$key"
    done
    # A deployment that holds a release key has said which packages it
    # trusts: one that does not verify against it is refused unless the
    # operator says otherwise, by flag, on this run.
    if [ -n "$present" ] && [ "$ALLOW_UNSIGNED" = false ]; then
        REQUIRE_SIGNATURE=true
    fi

    if [ -f "$manifest" ]; then
        PKG_MANIFEST="$manifest"
        if [ -f "$sig" ]; then
            if [ -z "$present" ]; then
                SIGNATURE_STATE=unverifiable
                msg="The package is signed, but there is no release key to verify it against (looked for ${keys[*]}; pass --release-key PATH)."
                [ "$REQUIRE_SIGNATURE" = false ] || refuse signature "$msg"
                warn "$msg"
            else
                verify_against_keys "$manifest" "$sig" "${keys[@]}" && rc=0 || rc=$?
                case "$rc" in
                    0)
                        SIGNATURE_STATE=verified
                        ok "Signature verified against $VERIFIED_KEY"
                        ;;
                    1)
                        refuse signature "The package signature does NOT verify against $present. Refusing it: the manifest or the signature was altered, or the package was signed with a different key."
                        ;;
                    *)
                        SIGNATURE_STATE=unverifiable
                        msg="The package is signed, but nothing on this host can check an Ed25519 signature (needs OpenSSL 3, or python3 with the cryptography package)."
                        [ "$REQUIRE_SIGNATURE" = false ] || refuse signature "$msg"
                        warn "$msg"
                        ;;
                esac
            fi
        else
            [ "$REQUIRE_SIGNATURE" = false ] || refuse signature "The package is not signed (no $sig), and a release key is present (or --require-signature is set). Pass --allow-unsigned to apply it anyway."
            warn "The package is not signed; its manifest is checked for consistency only."
        fi

        want="$(manifest_value "$manifest" sha256)"
        [ -n "$want" ] || refuse manifest "The manifest names no sha256; it is not a package manifest."
        require_sha256_tool
        have="$(sha256_of "$ARCHIVE")"
        [ "$want" = "$have" ] || refuse hash "The archive does not match its manifest: sha256 $have, manifest says $want. The file was altered or corrupted in transit."
        want_size="$(manifest_value "$manifest" size)"
        size="$(wc -c < "$ARCHIVE" | tr -d ' ')"
        [ -z "$want_size" ] || [ "$want_size" = "$size" ] || refuse hash "The archive does not match its manifest: $size bytes, manifest says $want_size."
        PKG_SHA256="$have"
        ok "Archive matches the manifest (sha256 ${have:0:12}…)"

        field="$(manifest_value "$manifest" manifest_version)"
        [ "$field" = 1 ] || refuse manifest "The manifest is version '$field'; this script understands version 1. Update update.sh first — the package carries a newer one at its root."
        field="$(manifest_value "$manifest" min_updater_version)"
        [[ "$field" =~ ^[0-9]+$ ]] || refuse manifest "The manifest's min_updater_version is not a number ('$field')."
        if [ "$field" -gt "$UPDATER_SCRIPT_VERSION" ]; then
            refuse updater_too_old "The package needs update.sh version $field and this is version $UPDATER_SCRIPT_VERSION. Replace this script with the package's copy (update.sh at its root) and re-run."
        fi
        PKG_VERSION="$(manifest_value "$manifest" version)"

        # A package for another project, or with another plugin set, applied
        # over this deployment would drop or add whole applications. The
        # manifest says what the package is; .env says what runs here.
        pkg_project="$(manifest_value "$manifest" project)"
        pkg_plugins="$(normalise_list "$(manifest_value "$manifest" plugins)")"
        my_project="$(env_value EPICURRENTS_PROJECT)"
        my_plugins="$(normalise_list "$(env_value EPICURRENTS_PLUGINS)")"
        if [ "$pkg_project" != "$my_project" ]; then
            refuse incompatible "The package is built for project '${pkg_project:-<none>}' and this deployment runs '${my_project:-<none>}'. Build a package for this deployment's project."
        fi
        if [ "$pkg_plugins" != "$my_plugins" ]; then
            refuse incompatible "The package carries plugins '${pkg_plugins:-<none>}' and this deployment runs '${my_plugins:-<none>}'. Build a package with this deployment's plugins."
        fi
        ok "Package is for project '${pkg_project:-<none>}', plugins '${pkg_plugins:-<none>}' — matches this deployment"
    else
        [ "$REQUIRE_SIGNATURE" = false ] || refuse signature "No manifest beside the archive ($manifest), and a release key is present (or --require-signature is set). A signed package ships as three files: the tarball, .manifest.json and .manifest.sig."
        warn "No manifest beside the archive ($manifest); the package cannot be verified. Its contents are still checked."
    fi

    inspect_archive_listing
    ok "Archive contents are safe to overlay (one directory: $ARCHIVE_TOP)"

    if [ -z "$PKG_VERSION" ]; then
        PKG_VERSION="$(archive_member epicurrents/version.py | sed -n '/^__version__ = "/{s/^__version__ = "\([^"]*\)".*/\1/p;q;}')"
    fi
    INSTALLED_VERSION="$(installed_version)"
    if [ -n "$PKG_VERSION" ]; then
        is_version "$PKG_VERSION" || refuse manifest "The package version '$PKG_VERSION' is not a plain MAJOR.MINOR.PATCH version."
    fi
    if [ -n "$PKG_VERSION" ] && [ -n "$INSTALLED_VERSION" ]; then
        is_version "$INSTALLED_VERSION" || refuse incompatible "The installed version '$INSTALLED_VERSION' is not a plain MAJOR.MINOR.PATCH version."
        if version_gt "$PKG_VERSION" "$INSTALLED_VERSION"; then
            ok "Package version $PKG_VERSION is newer than the installed $INSTALLED_VERSION"
        elif [ "$PKG_VERSION" = "$INSTALLED_VERSION" ]; then
            msg="The package is version $PKG_VERSION, the same as the installed release."
            [ "$REQUIRE_NEWER" = false ] || refuse version_not_newer "$msg --require-newer refuses it."
            warn "$msg"
        else
            msg="The package is version $PKG_VERSION, OLDER than the installed $INSTALLED_VERSION."
            [ "$REQUIRE_NEWER" = false ] || refuse version_not_newer "$msg --require-newer refuses a downgrade."
            [ "$ALLOW_DOWNGRADE" = true ] || refuse version_not_newer "$msg A downgrade does not migrate the database backwards; roll back to a snapshot instead, or pass --allow-downgrade."
            warn "$msg Applying it is a downgrade (--allow-downgrade); the database will not be migrated backwards."
        fi
    elif [ "$REQUIRE_NEWER" = true ]; then
        refuse version_not_newer "--require-newer: cannot compare versions (package: '${PKG_VERSION:-unknown}', installed: '${INSTALLED_VERSION:-unknown}')."
    fi

    emit "archive=$ARCHIVE"
    emit "manifest=${PKG_MANIFEST:-none}"
    emit "signature=$SIGNATURE_STATE"
    emit "sha256=${PKG_SHA256:-unknown}"
    emit "version=${PKG_VERSION:-unknown}"
    emit "installed=${INSTALLED_VERSION:-unknown}"
    emit "key=${VERIFIED_KEY:-none}"
}

# ── Orphaned files ────────────────────────────────────────────────────────────
# The overlay never deletes, so a file a release removes stays in the tree and
# is baked into the image. The package's FILELIST names every file it ships;
# the installed release's list is kept at .epicurrents-files. After the overlay,
# old minus new is what the previous package shipped and this one does not —
# and only those are removed. A file no package listed is the operator's, or
# generated at runtime, and is never a candidate. The protect list is a second
# fence on top of that.

is_protected() {
    # $1 = path relative to the deployment root.
    case "$1" in
        .env|.env.*|RELEASE_KEY.pub|backups/*|update/*|static/*|frontend/vendor/*|frontend/node_modules/*|.git/*|local/*|testdata/*|recordings/converters/*/*)
            return 0 ;;
        projects/*/*)
            # A project checked out by the operator is theirs; a copied one is
            # the package's. `.git` may be a file in a worktree, hence -e.
            local p="${1#projects/}"
            p="${p%%/*}"
            [ -e "$ROOT/projects/$p/.git" ] && return 0
            ;;
    esac
    return 1
}

inside_tree() {
    # $1 = a path from a file list. True when it is a plain relative path — no
    # leading slash, no `..`, no `.` or empty component — whose parent directory
    # resolves, links followed, to somewhere under the deployment root. The
    # lists are written by whoever can write the tree, which is not who runs
    # this script on a host with an updater: an entry of `../etc/x`, or
    # `link/x` where `link` points outside, must not turn a prune into a
    # deletion elsewhere.
    case "$1" in
        ""|/*|.|..|./*|../*|*/.|*/..|*/./*|*/../*|*//*) return 1 ;;
    esac
    local parent
    parent="$(cd "$ROOT/$(dirname "$1")" 2>/dev/null && pwd -P)" || return 1
    case "$parent/" in
        "$ROOT_REAL/"*) return 0 ;;
    esac
    return 1
}

remove_empty_parents() {
    # $1 = path relative to the root whose parents may now be empty.
    local d
    d="$(dirname "$1")"
    while [ "$d" != "." ] && [ "$d" != "/" ]; do
        rmdir "$ROOT/$d" 2>/dev/null || break
        d="$(dirname "$d")"
    done
}

record_installed_filelist() {
    # $1 = the list to keep as the installed release's. Handed to the tree's
    # owner when this runs as root, like the snapshot: a root-owned copy could
    # not be replaced by a later run as the deployment account, which would
    # die on the copy after the overlay — the worst place to stop.
    cp "$1" "$INSTALLED_FILELIST"
    hand_to_tree_owner "$INSTALLED_FILELIST"
}

prune_orphans() {
    # $1 = the new package's FILELIST (in the extracted tree).
    local removed=0 rel
    if [ ! -f "$1" ]; then
        warn "The package carries no FILELIST; files the previous release shipped and this one dropped are not pruned."
        return 0
    fi
    if [ ! -f "$INSTALLED_FILELIST" ]; then
        record_installed_filelist "$1"
        ok "Recorded the package's file list; pruning of dropped files starts with the next update"
        return 0
    fi
    while IFS= read -r rel; do
        [ -n "$rel" ] || continue
        is_protected "$rel" && continue
        inside_tree "$rel" || { warn "Not pruning '$rel': it does not name a file inside the deployment tree."; continue; }
        if [ -f "$ROOT/$rel" ] && [ ! -L "$ROOT/$rel" ]; then
            rm -f "$ROOT/$rel"
            removed=$((removed + 1))
            remove_empty_parents "$rel"
        fi
    done < <(LC_ALL=C comm -23 <(LC_ALL=C sort -u "$INSTALLED_FILELIST") <(LC_ALL=C sort -u "$1"))
    record_installed_filelist "$1"
    ok "Pruned $removed file(s) the previous release shipped and this one does not"
}

report_orphan_candidates() {
    # For --check-archive: what is in the tree, under the directories the
    # package populates, that the package does not carry. Informational —
    # an update prunes only what the previous package listed — so an operator
    # with a shell can clean by hand. frontend/dist and viewer-dist are left
    # out: the update empties and refills those two.
    local new tops top existing candidates count shown
    new="$(archive_member FILELIST)"
    if [ -z "$new" ]; then
        warn "The package carries no FILELIST; nothing to compare the tree against."
        return 0
    fi
    tops="$(printf '%s\n' "$new" | grep '/' | sed 's|/.*||' | LC_ALL=C sort -u)"
    existing=""
    while IFS= read -r top; do
        if [ -z "$top" ] || [ ! -d "$ROOT/$top" ]; then
            continue
        fi
        # Run from the root so find prints relative paths and nothing has to
        # strip a prefix that could read as a pattern.
        existing="$existing$(cd "$ROOT" && find "$top" \( -name .git -o -name node_modules -o -name __pycache__ \
            -o -path frontend/vendor -o -path frontend/dist -o -path frontend/viewer-dist \) -prune \
            -o -type f -print)"$'\n'
    done <<< "$tops"
    candidates="$(LC_ALL=C comm -23 <(printf '%s' "$existing" | sed '/^$/d' | LC_ALL=C sort -u) <(printf '%s\n' "$new" | LC_ALL=C sort -u) \
        | while IFS= read -r rel; do is_protected "$rel" || printf '%s\n' "$rel"; done)"
    count="$(printf '%s' "$candidates" | grep -c . || true)"
    emit "orphan_candidates=$count"
    if [ "$count" = 0 ]; then
        ok "No file in the tree that the package does not carry"
        return 0
    fi
    warn "$count file(s) in the tree that the package does not carry (an update prunes only what the previous package listed):"
    shown=0
    while IFS= read -r rel; do
        [ -n "$rel" ] || continue
        if [ "$shown" -ge 40 ]; then
            warn "  … and $((count - shown)) more"
            break
        fi
        warn "  $rel"
        shown=$((shown + 1))
    done <<< "$candidates"
}

# ── --check-archive ───────────────────────────────────────────────────────────

if [ -n "$CHECK_ARCHIVE" ]; then
    ARCHIVE="$CHECK_ARCHIVE"
    [ -f "$ARCHIVE" ] || die "Archive not found: $ARCHIVE"
    check_archive
    report_orphan_candidates
    echo
    ok "Archive check passed. Nothing was changed."
    emit "check=ok"
    exit 0
fi

# ── Shared steps ──────────────────────────────────────────────────────────────
# Used by both the update and the rollback path, so a rollback reproduces every
# step an update runs after the image build. A rollback that skipped them left
# the failed release's vendored assets under the restored code and never
# checked the result was serving.

BORG_WAS_RUNNING=false
# Survives a run that stops borg and then dies: the next run finds borg stopped
# and cannot tell "never ran here" from "the last update stopped it", and the
# difference is whether a deployment silently loses its backups. Under backups/,
# which nothing this script syncs or snapshots touches and no container mounts:
# written by a script that may run as root, it must not sit where the web tier
# could have left a link at its name.
BORG_MARKER="$BACKUP_DIR/.borg-was-running"

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
        mkdir -p "$BACKUP_DIR"
        : > "$BORG_MARKER"
    fi
    emit "step=stop"
    info "Stopping application services"
    "${COMPOSE[@]}" stop web celery celery-beat borg || true
    SERVICES_STOPPED=true
    ok "Application services stopped"
}

collect_static() {
    emit "step=static"
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
    emit "step=vendor"
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
    emit "step=recreate"
    info "Recreating containers"
    "${COMPOSE[@]}" up -d --force-recreate "${services[@]}"
    SERVICES_STOPPED=false
    WORKERS_STOPPED=false
    # Caddy serves /assets/, /viewer/ and /static/ straight off bind mounts, so
    # it has to be recreated after the tree underneath it changes — a running
    # container holds the mount it was started with. Left out, an archive update
    # takes the SPA offline while Django reports healthy.
    if [ "$PROXY_ENABLED" = true ]; then
        "${COMPOSE[@]}" up -d --force-recreate caddy \
            || die "The application is up but caddy could not be recreated, so the SPA is not being served. Check '${COMPOSE[*]} logs caddy'."
    fi
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
    port="$(env_value HOST_PORT)"
    port="${port:-8000}"
    emit "step=health"
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
        emit "health=ok"
    else
        emit "health=failed"
        die "Health check did not pass within the timeout; check '${COMPOSE[*]} logs web'."
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
        domain="$(env_value PROXY_DOMAIN)"
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

# ── Snapshots ─────────────────────────────────────────────────────────────────

snapshot_complete() {
    # $1 = snapshot directory. A snapshot that can actually be restored from:
    # database and .env present, and a code archive that reads if there is one.
    # Warnings go to stderr deliberately: callers run inside a command
    # substitution, where anything on stdout becomes part of the returned path.
    if [ ! -f "$1/db.sql.gz" ] || [ ! -f "$1/.env" ]; then
        warn "Skipping incomplete snapshot $(basename "$1") (no database or .env)" >&2
        return 1
    fi
    if [ -f "$1/code.tar.gz" ] && ! tar -tzf "$1/code.tar.gz" >/dev/null 2>&1; then
        warn "Skipping snapshot $(basename "$1") — its code archive is unreadable" >&2
        return 1
    fi
    # A truncated dump decompresses to a prefix of the database, and restored
    # it would be a prefix of the database; the gzip trailer is what tells.
    if ! gzip -t "$1/db.sql.gz" 2>/dev/null; then
        warn "Skipping snapshot $(basename "$1") — its database dump is truncated or corrupt" >&2
        return 1
    fi
    return 0
}

find_latest_complete_snapshot() {
    # The newest snapshot that can actually be restored from — not simply the
    # newest. A run that dies between the code snapshot (step 0) and the
    # database dump (step 2) leaves a code-only directory behind, and that
    # directory is the newest; refusing on it would block rollback to the
    # perfectly good snapshot sitting behind it, which is the opposite of what
    # a recovery path should do when it meets damage.
    #
    # A glob rather than `ls -t`: the directory names are UTC timestamps this
    # script writes, so lexical order is chronological, and a glob cannot be
    # confused by a name containing whitespace. Iterated backwards for
    # newest-first. Only pre-update-* snapshots: a snapshot taken with
    # --snapshot LABEL is named for its purpose and restored by name.
    local dirs=("$BACKUP_DIR"/pre-update-*)
    local i d
    for ((i = ${#dirs[@]} - 1; i >= 0; i--)); do
        d="${dirs[i]}"
        [ -d "$d" ] || continue   # no matches: the glob stayed literal
        snapshot_complete "$d" || continue
        printf '%s' "$d"
        return 0
    done
    return 1
}

prune_backups() {
    # Keep the newest $KEEP_BACKUPS pre-update snapshots; drop the rest. ls -t
    # over our own timestamped dir names is fine — no untrusted filenames here.
    # Named snapshots (--snapshot LABEL) are not touched: whoever took one
    # removes it, and neither is the snapshot an unfinished update is waiting
    # to be rolled back to.
    local keep_name=""
    [ ! -f "$UNFINISHED_MARKER" ] || keep_name="$(cat "$UNFINISHED_MARKER" 2>/dev/null || true)"
    # shellcheck disable=SC2012
    ls -1dt "$BACKUP_DIR"/pre-update-* 2>/dev/null | tail -n +$((KEEP_BACKUPS + 1)) | while read -r old; do
        [ "$(basename "$old")" != "$keep_name" ] || continue
        rm -rf "$old"
    done || true
    # Drop snapshots left half-written by a run that died before the dump. They
    # can never be restored from, and they crowd out the ones that can.
    # shellcheck disable=SC2012
    ls -1dt "$BACKUP_DIR"/pre-update-* 2>/dev/null | while read -r d; do
        [ -f "$d/db.sql.gz" ] || { warn "Discarding half-written snapshot $(basename "$d")"; rm -rf "$d"; }
    done || true
}

snapshot_code() {
    # $1 = snapshot directory. The code tree as it is now, before anything
    # overwrites it.
    #
    # Excluded: the snapshots themselves (which would nest), the archive drop
    # directory, .env (saved with the database), git history, build caches — and
    # static/ plus frontend/vendor, which the containers write as root, so an
    # unprivileged restore cannot set their timestamps and tar fails the whole
    # extraction on "Cannot utime". static/ is regenerated by the collectstatic
    # step; frontend/vendor is not, which is why the vendoring step re-vendors
    # it whenever the tree does not match its own lock.
    mkdir -p "$1"
    info "Snapshotting current code to $1"
    if tar -czf "$1/code.tar.gz" \
            --exclude="./backups" \
            --exclude="./update" \
            --exclude="./.env" \
            --exclude="./.git" \
            --exclude="./frontend/node_modules" \
            --exclude="./static" \
            --exclude="./frontend/vendor" \
            --exclude="__pycache__" \
            -C . . 2>/dev/null; then
        ok "Code snapshotted ($(du -h "$1/code.tar.gz" | cut -f1))"
        hand_to_tree_owner "$1"
    else
        rm -rf "$1"
        die "Code snapshot failed; aborting before any change. (Pass --no-backup to override.)"
    fi
}

hand_to_tree_owner() {
    # $1 = a path this script wrote. Run as root, it belongs to root, and the
    # deploy account can then neither prune a snapshot nor restore from it, nor
    # replace the installed file list. Hand it to whoever owns the tree. GNU
    # stat only, like the ownership preflight. Called after each write, not
    # once per run: the database dump lands in a snapshot directory minutes
    # after the code archive did, and a single chown after the archive left
    # the dump, .env and MANIFEST root-owned.
    if [ "$(id -u)" = 0 ]; then
        local owner
        owner="$(stat -c %u:%g . 2>/dev/null || true)"
        [ -n "$owner" ] && chown -R "$owner" "$1"
    fi
    return 0
}

snapshot_database() {
    # $1 = snapshot directory, $2 = the MANIFEST body (lines) describing why.
    # Local snapshot = database + .env: small, fast, and the part an update can
    # destroy. Recording / media file volumes are out of scope here — they are
    # untouched by migrations; enable borg for full data-volume backups.
    ensure_db_up
    info "Backing up the database to $1"
    # SC2016: $POSTGRES_* must expand inside the db container's shell, not here.
    # shellcheck disable=SC2016
    if "${COMPOSE[@]}" exec -T db sh -c 'pg_dump --clean --if-exists --no-owner -U "$POSTGRES_USER" "$POSTGRES_DB"' \
            | gzip > "$1/db.sql.gz"; then
        ok "Database dumped ($(du -h "$1/db.sql.gz" | cut -f1))"
    else
        rm -rf "$1"
        die "Database dump failed; aborting before any change. (Pass --no-backup to override.)"
    fi
    if ! gzip -t "$1/db.sql.gz" 2>/dev/null; then
        rm -rf "$1"
        die "The database dump does not read back; aborting before any change."
    fi
    cp .env "$1/.env"
    # Which migrations the dump has applied, so a later rollback can tell
    # whether the database still matches the snapshot's code and keep it. A
    # snapshot whose record could not be taken has none, which is what makes
    # --code-only refuse it rather than guess.
    if ! applied_migrations > "$1/migrations.txt"; then
        rm -f "$1/migrations.txt"
        warn "Could not read the applied migrations; a rollback to this snapshot will restore the database too."
    fi
    printf '%s\n' "$2" > "$1/MANIFEST"
    hand_to_tree_owner "$1"
    ok ".env + manifest saved"
    emit "snapshot=$1"
}

restore_stream() {
    # $1 = the dump. The whole restore as one explicit transaction: BEGIN, the
    # preamble, the dump, and COMMIT only once gunzip has read the dump to its
    # end. psql --single-transaction commits at end of input, so a stream cut
    # short by a truncated or corrupt dump would commit whatever part of it
    # arrived; here a short stream ends without COMMIT, the server rolls the
    # transaction back when psql disconnects, and the non-zero status of this
    # function fails the pipeline.
    printf 'BEGIN;\n'
    restore_sql_preamble
    gunzip -c "$1" || return 1
    printf '\nCOMMIT;\n'
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

# ── Snapshot-only mode ────────────────────────────────────────────────────────
# A named snapshot, taken without changing anything: what a caller takes before
# rolling back, so that whatever the live database gained since the update is
# recoverable from somewhere. Named for its purpose so --rollback never picks
# it in place of the pre-update one.

if [ "$SNAPSHOT_ONLY" = true ]; then
    [[ "$SNAPSHOT" =~ ^[A-Za-z0-9][A-Za-z0-9_-]*$ ]] \
        || die "--snapshot: a label is letters, digits, '-' and '_' (got '$SNAPSHOT')."
    stamp="$(date -u +%Y%m%d-%H%M%S)"
    snap="$BACKUP_DIR/$SNAPSHOT-$stamp"
    [ ! -e "$snap" ] || die "Snapshot $snap already exists."
    take_run_lock
    emit "step=snapshot"
    snapshot_code "$snap"
    snapshot_database "$snap" "timestamp_utc=$stamp
mode=snapshot
label=$SNAPSHOT
code_snapshot=yes
version=$(installed_version)"
    echo
    ok "Snapshot complete: $snap"
    echo "    Restore it with: ./update.sh --rollback --snapshot $(basename "$snap")"
    emit "done"
    exit 0
fi

# ── Rollback path ─────────────────────────────────────────────────────────────

if [ "$ROLLBACK" = true ]; then
    # Every piece is verified before anything is touched — including that the
    # code archive actually reads, since it is restored *after* the database
    # and a truncated one would otherwise strand the deployment half rolled
    # back. An incomplete snapshot is skipped rather than fatal when the newest
    # is being looked for; a named one is what was asked for, so it is refused.
    if [ -n "$SNAPSHOT" ]; then
        case "$SNAPSHOT" in
            */*|.|..) die "--snapshot NAME names a directory under $BACKUP_DIR (got '$SNAPSHOT')." ;;
        esac
        latest="$BACKUP_DIR/$SNAPSHOT"
        [ -d "$latest" ] || die "No snapshot named $SNAPSHOT under $BACKUP_DIR."
        snapshot_complete "$latest" || die "Snapshot $SNAPSHOT is incomplete or unreadable and cannot be restored from."
    else
        latest="$(find_latest_complete_snapshot || true)"
        [ -n "$latest" ] || die "No complete pre-update snapshot found under $BACKUP_DIR (a snapshot needs db.sql.gz and .env, and a readable code.tar.gz if it has one)."
    fi
    info "Rolling back to $(basename "$latest")"
    [ -f "$latest/MANIFEST" ] && cat "$latest/MANIFEST"
    if [ "$CODE_ONLY" = true ]; then
        # The database is kept only while it is the one the snapshot's code ran
        # against: the set of applied migrations must not have moved since the
        # snapshot recorded it. Checked before anything is touched, so a
        # refusal changes nothing, and named so a caller can key on it.
        [ -f "$latest/code.tar.gz" ] || refuse code_only "--code-only needs a snapshot with a code archive, and $(basename "$latest") has none."
        [ -f "$latest/migrations.txt" ] || refuse code_only "Snapshot $(basename "$latest") predates migration records, so whether the database still matches its code cannot be told. Roll back without --code-only, which restores the database too."
        ensure_db_up
        current_migrations="$(applied_migrations)" \
            || refuse code_only "The applied migrations could not be read from the database, so whether it still matches the snapshot's code cannot be told. Roll back without --code-only, which restores the database too."
        if [ "$current_migrations" != "$(LC_ALL=C sed '/^$/d' "$latest/migrations.txt" | LC_ALL=C sort)" ]; then
            refuse code_only "Migrations were applied since snapshot $(basename "$latest") was taken, so its code cannot run against the current database. Roll back without --code-only, which restores the database too."
        fi
        ok "No migration was applied since the snapshot; the database and .env are kept"
        confirm "Restore the code from this snapshot? The database and .env are kept." \
            || die "Rollback aborted."
    else
        confirm "Restore database + .env from this snapshot? Current data will be overwritten." \
            || die "Rollback aborted."
    fi
    take_run_lock
    # A rollback is the repair a failed update asks for, so a flag left up by
    # that update, or by the agent, is taken over rather than refused.
    if [ "$KEEP_LOCK" = false ] && flag_present; then
        warn "A maintenance flag is already up ($MAINTENANCE_FLAG); the rollback takes it over."
        rm -f "$MAINTENANCE_FLAG"
    fi
    write_maintenance_flag rolling_back "The platform is being rolled back to the previous release."
    ensure_db_up
    stop_app_services
    if [ "$CODE_ONLY" = false ]; then
        emit "step=restore-db"
        info "Restoring database (single transaction — all or nothing)"
        # One explicit transaction + ON_ERROR_STOP: the schema drop and the
        # restore commit or roll back as one unit, and COMMIT is sent only
        # after the whole dump was read (restore_stream), so a failure leaves
        # the database exactly as it was rather than half-restored. On failure
        # we stop here — .env is untouched and the stack is not recreated — so
        # the operator never lands in a partially-recovered state.
        # SC2016: $POSTGRES_* must expand inside the db container's shell, not here.
        # shellcheck disable=SC2016
        if ! restore_stream "$latest/db.sql.gz" \
                | "${COMPOSE[@]}" exec -T db sh -c 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
                    >/dev/null; then
            die "Database restore FAILED and was rolled back — the database is unchanged, .env was not touched, and the stack was not recreated. If the error above is a lock timeout, something is still connected to the database (a backup in progress, a shell); see pg_stat_activity. Investigate before retrying."
        fi
        ok "Database restored"
        # Tasks queued before the restore name rows the restored database may
        # not hold, or hold differently; the queue is dropped rather than run
        # against them. The broker is redis, which the restore does not touch.
        if "${COMPOSE[@]}" run --rm --no-deps -T celery celery -A epicurrents purge -f >/dev/null 2>&1; then
            ok "Task queue purged"
        else
            warn "Could not purge the task queue; tasks queued before the restore may run against the restored database."
        fi
        emit "step=restore-env"
        cp "$latest/.env" ./.env
        ok ".env restored"
    fi

    # Code after the database, deliberately. A failed database restore leaves
    # everything untouched (above); a failure here leaves the old database under
    # new code, which the recreate below resolves by re-migrating — consistent,
    # if not what was asked for. The reverse order would leave new code with no
    # way back.
    if [ -f "$latest/code.tar.gz" ]; then
        emit "step=restore-code"
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
        #
        # Anchored at the root, so a directory of the same name deeper in the
        # code (a static/ inside an app) is restored like the rest. .git/ is
        # the one left unanchored on purpose: a project checked out under
        # projects/ is the operator's, and so is its history. The release keys
        # stay as they are — restoring an older one would undo a rotation and
        # refuse the next package.
        rtmp="$(private_dir)/restore"
        mkdir -p "$rtmp"
        if tar -xzmf "$latest/code.tar.gz" -C "$rtmp" \
            && rsync -a --delete \
                --exclude="/.env" \
                --exclude="/backups/" \
                --exclude="/update/" \
                --exclude="/static/" \
                --exclude="/frontend/vendor/" \
                --exclude="/frontend/node_modules/" \
                --exclude="/RELEASE_KEY.pub" \
                --exclude="/RELEASE_KEY.next.pub" \
                --exclude=".git/" \
                "$rtmp"/ ./; then
            rm -rf "$rtmp"
            ok "Code restored"
        else
            rm -rf "$rtmp"
            if [ "$CODE_ONLY" = true ]; then
                die "Code restore FAILED. The database and .env were kept as they were; the tree is in an unknown state. Re-apply a known archive before starting the stack."
            fi
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
        emit "step=build"
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

    # The version the tree now carries, for a caller that gates on it.
    emit "restored=$(installed_version)"

    # The same tail an update runs after its build. Static storage is not
    # manifest-based, so a stale hashed file would merely linger; the vendored
    # trees are what matter, since a newer Pyodide closure vendored by the
    # failed release would otherwise sit under the restored viewer.
    collect_static
    refresh_vendored_assets
    recreate_stack
    wait_for_health
    report_vendor_failures
    # Rolled back: whatever update was left unfinished is settled.
    rm -f "$UNFINISHED_MARKER"
    echo
    if [ "$CODE_ONLY" = true ]; then
        ok "Rollback complete — code restored; the database and .env were kept."
    elif [ "$CODE_RESTORED" = true ]; then
        ok "Rollback complete — database, .env and code restored."
    else
        warn "Rolled back the database and .env, not the code/image."
        ok "Rollback complete."
    fi
    emit "done"
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

# ── Preflight: the archive, verified before anything is touched ───────────────
# Everything a package is refused for happens here, before the flag goes up and
# before the snapshot: a refused package leaves no trace. The extraction itself
# is step 1, after the snapshot, because the snapshot must capture the tree the
# package is about to overwrite.
#
# The archive and its sidecars are copied into the private directory first, and
# the copy is what is verified and extracted. update/ is writable by the web
# tier: a file verified where it lies could be exchanged between the check and
# the extraction.

take_run_lock
refuse_foreign_flag
if [ -f "$UNFINISHED_MARKER" ]; then
    refuse unfinished "An earlier update did not finish; its pre-update snapshot is $(cat "$UNFINISHED_MARKER" 2>/dev/null || echo unknown). Roll it back first (./update.sh --rollback --snapshot <that name>), or remove $UNFINISHED_MARKER once the deployment is known to be in order."
fi

ARCHIVE_SOURCE=""
if [ "$MODE" = archive ]; then
    if [ -z "$ARCHIVE" ]; then
        # shellcheck disable=SC2012  # newest-by-mtime over a controlled glob.
        ARCHIVE="$(ls -1t "$UPDATE_DIR"/epicurrents*.tar.gz 2>/dev/null | head -1 || true)"
        [ -n "$ARCHIVE" ] || die "No archive in $UPDATE_DIR/ (looked for epicurrents*.tar.gz). Drop the distribution there or pass --archive FILE."
    fi
    { [ -f "$ARCHIVE" ] && [ ! -L "$ARCHIVE" ]; } || die "Archive not found, or not a plain file: $ARCHIVE"
    command -v rsync >/dev/null 2>&1 || die "rsync is required for archive mode (apt-get install rsync)."
    ARCHIVE_SOURCE="$ARCHIVE"
    staged_pkg="$(private_dir)/package"
    mkdir "$staged_pkg"
    cp -- "$ARCHIVE" "$staged_pkg/package.tar.gz"
    for sidecar in manifest.json manifest.sig; do
        if [ -e "$ARCHIVE.$sidecar" ] || [ -L "$ARCHIVE.$sidecar" ]; then
            { [ -f "$ARCHIVE.$sidecar" ] && [ ! -L "$ARCHIVE.$sidecar" ]; } || die "$ARCHIVE.$sidecar is not a plain file."
            cp -- "$ARCHIVE.$sidecar" "$staged_pkg/package.tar.gz.$sidecar"
        fi
    done
    ARCHIVE="$staged_pkg/package.tar.gz"
    emit "step=check"
    check_archive
    # Room for the extraction, which lives beside the snapshots, and for the
    # overlay's copy of it in the tree: the unpacked size twice, plus a margin.
    need=$((ARCHIVE_UNPACKED_BYTES * 2 + 268435456))
    free="$(df -Pk "$BACKUP_DIR" 2>/dev/null | awk 'NR==2{print $4 * 1024}' || true)"
    if [ -n "$free" ] && [ "$free" -lt "$need" ]; then
        refuse disk "$free bytes free under $BACKUP_DIR; the package unpacks to $ARCHIVE_UNPACKED_BYTES bytes and the update needs $need."
    fi
fi

# ── 0. Snapshot code and database, BEFORE anything changes ────────────────────
# The maintenance flag goes up first, so the platform declines writes from here
# on; the workers, which the flag does not reach, are stopped next — a warm
# shutdown, so a task in flight finishes — and brought back by the recreate.
# Then the code and the database are snapshotted together, and ::snapshot= is
# reported, before a single file of the tree is touched. A failure from there on
# always has a complete snapshot to roll back to; a failure before it has
# changed nothing.
#
# Retaining "the previous archive" instead would be cheaper and does not work:
# this script never moves, copies or records the archive it applied, so after a
# few updates nothing identifies the deployed lineage.
write_maintenance_flag updating "The platform is being updated."
stamp="$(date -u +%Y%m%d-%H%M%S)"
snap="$BACKUP_DIR/pre-update-$stamp"
# The version the tree carries now, before the overlay replaces it: what the
# snapshot's code is, recorded in its MANIFEST the way a named snapshot does.
PRE_VERSION="$(installed_version)"
if [ "$BACKUP" = true ]; then
    info "Stopping the workers for the snapshot"
    WORKERS_STOPPED=true
    "${COMPOSE[@]}" stop -t 60 celery celery-beat || warn "The workers did not stop cleanly; continuing."
    emit "step=snapshot"
    snapshot_code "$snap"
    emit "step=backup"
    if [ "$MODE" = archive ]; then
        source_line="archive=$ARCHIVE_SOURCE
archive_sha256=${PKG_SHA256:-unknown}
package_version=${PKG_VERSION:-unknown}"
    else
        source_line="git_ref=$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
    fi
    snapshot_database "$snap" "timestamp_utc=$stamp
mode=$MODE
code_snapshot=yes
version=${PRE_VERSION:-unknown}
$source_line"

    # If borgmatic is wired up and running, take a full backup too (data volumes).
    if [ -x ./scripts/backup.sh ] && service_running borg; then
        info "Borg is enabled — taking a full backup"
        ./scripts/backup.sh || warn "Borg backup reported an error; the local snapshot is still in place."
    fi

    prune_backups
    UNFINISHED_SNAPSHOT="$(basename "$snap")"
    printf '%s\n' "$UNFINISHED_SNAPSHOT" > "$UNFINISHED_MARKER"
    hand_to_tree_owner "$UNFINISHED_MARKER"
else
    warn "Skipping backup (--no-backup)."
fi

# ── 1. Acquire source ─────────────────────────────────────────────────────────

TREE_CHANGED=true
emit "step=acquire"
if [ "$MODE" = archive ]; then
    info "Applying archive: $ARCHIVE_SOURCE"
    tmp="$(private_dir)/extract"   # removed by the exit trap
    mkdir "$tmp"
    # --no-same-owner: extracted as root, a member would otherwise keep the
    # builder's uid, which the overlay below would carry into the tree.
    tar -xzf "$ARCHIVE" --no-same-owner -C "$tmp"
    # Distribution tars wrap their contents in a single versioned top-level dir,
    # which the listing check established; descend into it so the sync targets
    # the deployment files, not the wrapper.
    src="$tmp/$ARCHIVE_TOP"
    [ -f "$src/docker-compose.yml" ] || die "Archive does not look like an Epicurrents distribution (no docker-compose.yml under $ARCHIVE_TOP/)."
    OWNER_ARGS=()
    if [ "$(id -u)" = 0 ] && tree_owner="$(stat -c %u:%g . 2>/dev/null)"; then
        OWNER_ARGS=(--chown="$tree_owner")
    fi

    # Refresh the platform-owned, regenerable bundle dirs so stale content-hashed
    # chunks from prior releases don't pile up. These are the only trees we
    # actively empty; the root sync below is overlay-only (no --delete), so any
    # file the operator added that the archive doesn't carry — a
    # docker-compose.override.yml, certs, a .env.local, a .git checkout — is
    # always preserved. A denylist --delete at the deployment root would silently
    # wipe exactly those. What a release removed is pruned afterwards, from the
    # file lists, which name only what a package shipped.
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
    # Handed to the tree's owner on the way in when this runs as root: tar
    # records the builder's uid, and a tree owned by it is one the containers
    # cannot write.
    rsync -a ${OWNER_ARGS[@]+"${OWNER_ARGS[@]}"} \
        --exclude='/.env' \
        --exclude='/backups/' \
        --exclude='/update/' \
        --exclude='/static/' \
        --exclude='/.epicurrents-files' \
        "$src"/ "$ROOT"/
    ok "Files updated"
    prune_orphans "$src/FILELIST"
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
        project="$(env_value EPICURRENTS_PROJECT)"
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


# ── 3. Build the image ────────────────────────────────────────────────────────

emit "step=build"
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
emit "step=migrate"
info "Applying database migrations"
# What the database had applied before, so the run can say whether this release
# changed the schema: a release that applied nothing can be rolled back with
# --code-only, which keeps the database and everything written since.
# A record that could not be read on either side is "unknown", which no caller
# treats as licence to keep the database.
before_ok=true
before_migrate="$(applied_migrations)" || before_ok=false
"${COMPOSE[@]}" run --rm --no-deps web python manage.py migrate
ok "Migrations applied"
if [ "$before_ok" = true ] && after_migrate="$(applied_migrations)"; then
    if [ "$before_migrate" = "$after_migrate" ]; then
        MIGRATIONS=none
        ok "No migration was applied; this release can be rolled back with --code-only"
    else
        MIGRATIONS=applied
    fi
else
    MIGRATIONS=unknown
    warn "Could not read the applied migrations; a rollback of this release will restore the database too."
fi
emit "migrations=$MIGRATIONS"
if [ "$BACKUP" = true ] && [ -f "$snap/MANIFEST" ]; then
    printf 'migrations=%s\n' "$MIGRATIONS" >> "$snap/MANIFEST"
    hand_to_tree_owner "$snap/MANIFEST"
fi

# ── 6. Collect static files, refresh the vendored trees ───────────────────────

collect_static
refresh_vendored_assets

# ── 7. Recreate the application containers ────────────────────────────────────

recreate_stack

# ── 8. Health check + summary ─────────────────────────────────────────────────

wait_for_health
report_vendor_failures
rm -f "$UNFINISHED_MARKER"
TREE_CHANGED=false

echo
"${COMPOSE[@]}" ps
echo
ok "Update complete."
if [ "$BACKUP" = true ]; then
    echo "    Roll back with: ./update.sh --rollback"
fi
emit "done"
