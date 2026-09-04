#!/usr/bin/env bash
# restore.sh — restore the PostgreSQL database (and optionally file data) from a Borg archive
#
# Usage:
#   ./scripts/restore.sh             — interactive: list archives and prompt for selection
#   ./scripts/restore.sh <archive>   — restore directly from the named archive
#
# What this script restores:
#   - PostgreSQL database via borgmatic restore
#   - Recording and media files in the data volumes (optional, prompted;
#     extracted through the borg-restore compose service, which mounts the
#     volumes read-write — the backup-side borg service mounts them ro)
#
# NOTE: The web, celery, and celery-beat containers are stopped during restore
#       and restarted afterward. The db container stays running.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/.."

bold()  { printf '\033[1m%s\033[0m\n'           "$*"; }
info()  { printf '\n\033[1;34m==> %s\033[0m\n'  "$*"; }
ok()    { printf '    \033[32m✓\033[0m  %s\n'   "$*"; }
warn()  { printf '    \033[33m!\033[0m  %s\n'   "$*"; }
die()   { printf '\n\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }
confirm() {
    printf '\n\033[1;33m%s [y/N] \033[0m' "$*"
    read -r reply
    [[ "$reply" =~ ^[Yy]$ ]]
}

APP_SERVICES="web celery celery-beat"

# Which repository holds the archives depends on which tiers the deployment
# runs; resolved once so listing and extracting cannot disagree.
# shellcheck source=scripts/lib/borg_repo.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib/borg_repo.sh"
resolve_borg_repo || die "Cannot restore: no Borg repository is configured."

# ── 1. Select archive ─────────────────────────────────────────────────────────

ARCHIVE="${1:-}"

if [ -z "$ARCHIVE" ]; then
    info "Available Borg archives"
    # Capture archive list; borg list outputs lines like:
    #   <name>                       <date>   <time>  [<size>]
    ARCHIVE_LIST="$(docker compose run --rm borg borg list --short "$BORG_REPO_TARGET" 2>/dev/null)"

    if [ -z "$ARCHIVE_LIST" ]; then
        die "No archives found in the Borg repository. Run './scripts/backup.sh' first."
    fi

    # Number the archives and display
    i=1
    while IFS= read -r line; do
        printf '  %3d)  %s\n' "$i" "$line"
        i=$((i + 1))
    done <<< "$ARCHIVE_LIST"

    echo
    printf 'Enter archive number (or press Enter to cancel): '
    read -r selection

    [ -z "$selection" ] && { echo "Cancelled."; exit 0; }
    [[ "$selection" =~ ^[0-9]+$ ]] || die "Invalid selection."

    ARCHIVE="$(sed -n "${selection}p" <<< "$ARCHIVE_LIST")"
    [ -z "$ARCHIVE" ] && die "Selection out of range."
fi

# ── 2. Confirm ────────────────────────────────────────────────────────────────

echo
bold "Archive to restore: $ARCHIVE"
warn "This will OVERWRITE the current database."
confirm "Proceed with restore?" || { echo "Cancelled."; exit 0; }

# ── 3. Stop application containers ───────────────────────────────────────────

info "Stopping application containers"
# shellcheck disable=SC2086
docker compose stop $APP_SERVICES
ok "Application containers stopped"

# ── 4. Restore PostgreSQL ─────────────────────────────────────────────────────

info "Restoring PostgreSQL from archive: $ARCHIVE"
docker compose run --rm borg borgmatic restore --archive "$ARCHIVE"
ok "Database restored"

# ── 5. Optionally restore recording files ────────────────────────────────────

echo
if confirm "Also restore recording and media files from this archive?"; then
    info "Extracting recording and media files"
    # The backup-side borg service mounts the data volumes read-only, so
    # extraction goes through the dedicated borg-restore service, which
    # mounts them read-write under /restore in the same layout the
    # archive stores (borg extract writes relative to its working
    # directory, /restore). Borg exits 1 for warnings — e.g. an archive
    # from before the media volume existed has no data/media/uploads
    # match — which must not abort the restore; only rc >= 2 is an error.
    extract_rc=0
    docker compose run --rm borg-restore \
        borg extract "${BORG_REPO_TARGET}::$ARCHIVE" data/recordings data/media/uploads \
        || extract_rc=$?
    if [ "$extract_rc" -ge 2 ]; then
        die "borg extract failed with exit code $extract_rc"
    fi
    [ "$extract_rc" -eq 1 ] && warn "borg reported warnings during extract (see output above)"
    ok "Recording and media files restored"
fi

# ── 6. Apply any pending migrations ──────────────────────────────────────────
# The restored dump is at the schema version that was live when the backup
# ran. If the codebase has migrated since (any commit between backup and
# now), Django would start against a stale schema — produces
# "column does not exist" errors or, worse, silent data corruption on the
# first write. Run `migrate` here, with the db container running but the
# app containers still stopped, to bring the schema up to the current
# codebase before traffic resumes.

info "Applying any pending migrations"
docker compose run --rm --no-deps --entrypoint python web manage.py migrate
ok "Migrations applied"

# ── 7. Restart application containers ────────────────────────────────────────

info "Restarting application containers"
# shellcheck disable=SC2086
docker compose start $APP_SERVICES
ok "Application containers restarted"

echo
ok "Restore complete from archive: $ARCHIVE"
