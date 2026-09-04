#!/usr/bin/env bash
# restore-drill.sh — prove the backup can actually be restored, against a scratch stack
#
# Usage:
#   ./scripts/restore-drill.sh            — run the drill, tear the scratch stack down
#   ./scripts/restore-drill.sh --keep     — leave the scratch stack up for inspection
#
# An untested restore is a hypothesis. This exercises the whole chain end to end:
# seed a recording file and a database row, back both up through the same borgmatic
# config production uses, destroy the database and the file, restore, and assert
# the bytes and the row came back identical.
#
# ISOLATION. Everything runs under a dedicated compose project name, which gives
# it its own network and its own volumes, namespaced away from any real
# deployment on this host. Volume removal is the one operation that escapes that
# namespacing, so it goes through drop_volume, which refuses any name lacking the
# project prefix. The drill publishes no host port, so it can run alongside a
# live stack.
#
# BORG_REMOTE_REPO and BORG_MONITOR_URL are forced empty for every container the
# drill starts. Without that the drill would read the operator's .env, and since
# it deliberately runs the production emitter it would add the real off-host
# repository to the config — depositing a scratch archive built from empty
# volumes into the production repo, or failing outright on a host where the
# remote is unreachable from a one-shot container. Either way it would also leave
# borgmatic with two repositories and no way to tell which one to restore from.
# The monitor is silenced for the same reason: a drill run must not report itself
# as a successful backup to whatever is watching for one.
#
# What a pass means: the archive contains what the restore path expects, and the
# restore path puts it back. What it does not cover, by that same isolation: the
# off-host repository, and whether anyone would notice a failing backup.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/.."

DRILL_PROJECT="epicurrents-restore-drill"
MARKER_USER="restore-drill-marker"
MARKER_FILE="restore-drill-marker.edf"
KEEP_STACK=0
case "${1:-}" in
    --keep) KEEP_STACK=1 ;;
    "")     ;;
    # A mistyped --keep must not silently become "tear the stack down".
    *)      printf '\nUnknown argument: %s\nUsage: %s [--keep]\n' "$1" "$0" >&2; exit 2 ;;
esac

bold()  { printf '\033[1m%s\033[0m\n'            "$*"; }
info()  { printf '\n\033[1;34m==> %s\033[0m\n'   "$*"; }
ok()    { printf '    \033[32m✓\033[0m  %s\n'    "$*"; }
warn()  { printf '    \033[33m!\033[0m  %s\n'    "$*"; }
die()   { printf '\n\033[1;31mFAIL:\033[0m %s\n' "$*" >&2; exit 1; }

# Every compose call goes through here, so the project name and the network
# override cannot be forgotten on one of them. COMPOSE_FILE is pinned to the base
# file alone: the proxy overlay would publish ports 80/443 and collide with a real
# deployment, and the production overlay adds nothing the drill exercises.
dc() {
    COMPOSE_FILE=docker-compose.yml \
    EPICURRENTS_NETWORK_NAME="$DRILL_PROJECT" \
    BORG_REMOTE_REPO='' \
    BORG_MONITOR_URL='' \
    BORG_SSH_KEY_PATH='' \
        docker compose -p "$DRILL_PROJECT" "$@"
}

# Refuse to remove a volume whose name does not carry the drill's project prefix.
# The drill's whole risk profile is that it deletes volumes on a host that may be
# running a real deployment, and a mistyped or interpolated-empty project name is
# how that goes wrong silently.
drop_volume() {
    local volume="$1"
    case "$volume" in
        "${DRILL_PROJECT}_"*) ;;
        *) die "refusing to remove volume '$volume': not prefixed with ${DRILL_PROJECT}_" ;;
    esac
    docker volume rm -f "$volume" > /dev/null 2>&1 || true
}

# Both readback helpers pull a sentinel-prefixed value out of the container's
# output rather than reading it whole. Django's boot warnings and compose's own
# progress lines share the stream, and an earlier version that stripped the
# output down to its digits silently read a timestamp as the row count — a drill
# that reports a number nobody produced is worse than one that fails.
# `|| true` on both helpers: they end in a pipeline, and under pipefail a
# container that exits non-zero — which is exactly what a failed restore looks
# like, the database coming back without the table — would abort the caller's
# assignment before the die that explains what went wrong. An empty result is the
# signal these are read for, so swallow the status and let the caller judge.
marker_hash() {
    { dc run --rm --no-deps --entrypoint sh celery -c \
        "sha256sum /data/recordings/${MARKER_FILE} 2>/dev/null | sed 's/^/DRILLHASH /'" 2>/dev/null \
        | sed -n 's/.*DRILLHASH \([0-9a-f]\{64\}\).*/\1/p' | tail -1; } || true
}

marker_row_count() {
    { dc run --rm --no-deps --entrypoint python web manage.py shell -c "
from django.contrib.auth import get_user_model
print('DRILLROWS', get_user_model().objects.filter(username='${MARKER_USER}').count())
" 2>/dev/null | sed -n 's/.*DRILLROWS \([0-9][0-9]*\).*/\1/p' | tail -1; } || true
}

cleanup() {
    if [ -n "${RESTORE_LOG:-}" ]; then
        rm -f "$RESTORE_LOG"
    fi
    if [ "$KEEP_STACK" -eq 1 ]; then
        warn "Leaving the scratch stack up (--keep). Tear it down with:"
        printf '        docker compose -p %s down -v --remove-orphans\n' "$DRILL_PROJECT"
        return
    fi
    info "Tearing down the scratch stack"
    dc down -v --remove-orphans > /dev/null 2>&1 || true
    ok "Scratch stack removed"
}
trap cleanup EXIT

# ── 0. Preflight ──────────────────────────────────────────────────────────────

info "Preflight"
command -v docker > /dev/null 2>&1 || die "docker is not on PATH"
[ -f .env ] || die ".env not found — run 'python manage.py init_env' first"

# `|| true` because a missing BORG_PASSPHRASE line makes grep exit 1, and with
# pipefail plus set -e that aborts the script here with no output at all — the
# case the message below exists for. awk reads to EOF rather than piping to head,
# which would close the pipe and reintroduce the same silent-abort shape.
BORG_PASSPHRASE_VALUE="$(awk -F= '/^BORG_PASSPHRASE=/ && !f { sub(/^BORG_PASSPHRASE=/, ""); v = $0; f = 1 } END { print v }' .env || true)"
# Strip surrounding quotes so BORG_PASSPHRASE="" reads as empty rather than as
# two quote characters, which would pass the check against an unencrypted repo.
BORG_PASSPHRASE_VALUE="${BORG_PASSPHRASE_VALUE%\"}"
BORG_PASSPHRASE_VALUE="${BORG_PASSPHRASE_VALUE#\"}"
BORG_PASSPHRASE_VALUE="${BORG_PASSPHRASE_VALUE%\'}"
BORG_PASSPHRASE_VALUE="${BORG_PASSPHRASE_VALUE#\'}"
[ -n "$BORG_PASSPHRASE_VALUE" ] || die "BORG_PASSPHRASE is unset or empty in .env; the drill needs an encrypted repo"

# A leftover drill stack from an aborted run would restore into stale state and
# report a pass that means nothing.
dc down -v --remove-orphans > /dev/null 2>&1 || true
ok "No leftover drill state"

# ── 1. Bring up the scratch stack ────────────────────────────────────────────

info "Starting scratch database and broker"
dc up -d --wait db redis
ok "db and redis healthy"

info "Applying migrations"
dc run --rm migrate > /dev/null
ok "Schema created"

# ── 2. Seed data on both sides of the backup ─────────────────────────────────
# One row and one file, because the restore path treats them completely
# differently: the database goes through pg_dump / psql, the file through borg
# extract into a read-write mount. A drill that checked only one would pass with
# the other half broken.

info "Seeding a marker row and a marker file"
dc run --rm --no-deps --entrypoint python web manage.py shell -c "
from django.contrib.auth import get_user_model
get_user_model().objects.create_user(username='${MARKER_USER}', password='drill')
" > /dev/null
ok "Marker user created"

MARKER_CONTENT="epicurrents restore drill marker payload"
dc run --rm --no-deps --entrypoint sh --user 0:0 celery -c "
printf '%s' '${MARKER_CONTENT}' > /data/recordings/${MARKER_FILE}
" > /dev/null

EXPECTED_HASH="$(marker_hash)"
[ -n "$EXPECTED_HASH" ] || die "could not hash the marker file after seeding"
ok "Marker file created (sha256 ${EXPECTED_HASH:0:12}…)"

[ "$(marker_row_count)" = "1" ] || die "the seeded marker row is not readable — the drill cannot test a restore of it"
ok "Seed verified on both halves"

# ── 3. Back up through the production config ─────────────────────────────────
# The emitter runs first because the borg service's runtime YAML is generated at
# container start by its entrypoint, which the one-shot `run` bypasses. Using the
# emitted config rather than a drill-specific one is the point: a drill against a
# hand-written config proves nothing about the config production uses.

info "Initialising the scratch Borg repository"
dc run --rm --entrypoint borg borg init --encryption repokey /backup > /dev/null
ok "Repository initialised"

info "Running a backup"
dc run --rm --entrypoint sh borg -c "
python3 /etc/borgmatic.d/emit_runtime_config.py 2>/dev/null
borgmatic --config /run/borgmatic-active create --stats
" > /dev/null
ok "Archive created"

# ── 4. Destroy both halves ───────────────────────────────────────────────────
# The database volume goes entirely (so Postgres re-runs initdb into an empty
# cluster, the closest thing to a replacement host) and the marker file is
# unlinked. The borg-data volume is deliberately untouched — it is the thing
# under test.

info "Destroying the database and the marker file"
dc stop db > /dev/null
dc rm -f db > /dev/null
drop_volume "${DRILL_PROJECT}_postgres-data"
dc run --rm --no-deps --entrypoint sh --user 0:0 celery -c "rm -f /data/recordings/${MARKER_FILE}" > /dev/null
ok "Database volume removed, marker file unlinked"

dc up -d --wait db
ok "Empty database back up"

# A drill whose destroy step quietly did nothing passes every later assertion
# while proving nothing at all, so confirm both halves are actually gone before
# claiming the restore brought them back.
[ "$(marker_row_count)" != "1" ] || die "marker row survived the destroy step — the drill would prove nothing"
[ -z "$(marker_hash)" ] || die "marker file survived the destroy step — the drill would prove nothing"
ok "Both halves confirmed gone"

# ── 5. Restore ───────────────────────────────────────────────────────────────

info "Restoring the database from the archive"
# The restore is quiet on success and verbose on failure, because its output is
# the only place the reason for a failed restore appears. RESTORE_LOG is removed
# by the exit trap rather than here, so it survives the `die` path long enough to
# be printed.
RESTORE_LOG="$(mktemp)"
if ! dc run --rm --entrypoint sh borg -c "
python3 /etc/borgmatic.d/emit_runtime_config.py 2>/dev/null
borgmatic --config /run/borgmatic-active restore --archive latest
" > "$RESTORE_LOG" 2>&1; then
    cat "$RESTORE_LOG"
    die "borgmatic restore exited non-zero — see output above"
fi
ok "Database restored"

info "Restoring the marker file"
# Named through a sentinel for the same reason the marker readbacks are: compose
# writes its own progress to this stream, and concatenating all of it into an
# archive spec is the mistake that already cost one debugging round here.
ARCHIVE_NAME="$(dc run --rm --entrypoint sh borg -c \
    "borg list --short --last 1 /backup | sed 's/^/DRILLARCHIVE /'" 2>/dev/null \
    | sed -n 's/.*DRILLARCHIVE \([^ ]*\).*/\1/p' | tail -1)"
[ -n "$ARCHIVE_NAME" ] || die "could not determine the archive name to extract"
# borg exits 1 for warnings (a path absent from an older archive, say), which
# must not read as failure; only rc >= 2 is an error.
extract_rc=0
dc run --rm borg-restore \
    borg extract "/backup::${ARCHIVE_NAME}" \
    data/recordings > /dev/null 2>&1 || extract_rc=$?
[ "$extract_rc" -ge 2 ] && die "borg extract failed with exit code $extract_rc"
[ "$extract_rc" -eq 1 ] && warn "borg reported warnings during extract"
ok "Files extracted"

# ── 6. Assert ────────────────────────────────────────────────────────────────
# Both assertions compare against what was seeded, not against "something is
# there". A restore that produces an empty file or a table with the right name
# and no rows is the failure mode worth catching.

info "Verifying the restored state"

ROW_COUNT="$(marker_row_count)"
[ "$ROW_COUNT" = "1" ] || die "marker row did not come back (found ${ROW_COUNT:-no} matching rows)"
ok "Marker row restored"

ACTUAL_HASH="$(marker_hash)"
[ -n "$ACTUAL_HASH" ] || die "marker file did not come back"
[ "$ACTUAL_HASH" = "$EXPECTED_HASH" ] || die "marker file content differs: expected $EXPECTED_HASH, got $ACTUAL_HASH"
ok "Marker file restored byte-identical"

echo
bold "Restore drill passed — database row and recording file both recovered."
