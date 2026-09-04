#!/usr/bin/env bash
# backup.sh — trigger an on-demand Borg backup and prune old archives
#
# Usage:
#   ./scripts/backup.sh            — create backup and apply retention policy
#   ./scripts/backup.sh --list     — list existing archives only, no backup
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/.."

info()  { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
ok()    { printf '    \033[32m✓\033[0m  %s\n'  "$*"; }
die()   { printf '\n\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

# ── Verify borg container / borg repo exist ───────────────────────────────────

docker compose ps --services 2>/dev/null | grep -q '^borg$' \
    || die "No 'borg' service found in docker-compose.yml."

# ── List only ─────────────────────────────────────────────────────────────────

if [ "${1:-}" = "--list" ]; then
    # shellcheck source=scripts/lib/borg_repo.sh
    . "$(dirname "${BASH_SOURCE[0]}")/lib/borg_repo.sh"
    resolve_borg_repo || die "Nothing to list."
    info "Existing Borg archives ($BORG_REPO_TARGET)"
    docker compose run --rm borg borg list "$BORG_REPO_TARGET"
    exit 0
fi

# ── Create backup ─────────────────────────────────────────────────────────────

info "Creating Borg backup"
docker compose run --rm borg borgmatic create --stats --list
ok "Backup complete"

# ── Prune old archives ────────────────────────────────────────────────────────

info "Pruning archives according to retention policy"
docker compose run --rm borg borgmatic prune --stats --list
ok "Prune complete"

info "Done"
