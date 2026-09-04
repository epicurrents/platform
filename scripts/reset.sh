#!/usr/bin/env bash
# reset.sh — tear down the stack and destroy all volumes (development only)
#
# Runs `docker compose down -v`, which removes all containers, networks, and
# named volumes (postgres data, static files, recordings, etc.).
#
# Refuses to run unless DJANGO_MODE=development is set in .env to prevent
# accidental data loss on production hosts.
#
# Usage:
#   ./scripts/reset.sh           — destroy stack, then prompt whether to redeploy
#   ./scripts/reset.sh --yes     — destroy stack, then redeploy without prompting
#   ./scripts/reset.sh -y        — same as --yes
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/.."

die() { printf '\n\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

AUTO_REDEPLOY=false
for arg in "$@"; do
    case "$arg" in
        --yes|-y) AUTO_REDEPLOY=true ;;
        *) die "Unknown argument: $arg" ;;
    esac
done

# ── Guard: development mode only ─────────────────────────────────────────────

ENV_FILE=".env"
[ -f "$ENV_FILE" ] || die ".env not found. Nothing to reset."

DJANGO_MODE="$(grep -E '^DJANGO_MODE=' "$ENV_FILE" | cut -d= -f2 | tr -d '[:space:]')"

if [ "$DJANGO_MODE" != "development" ]; then
    die "DJANGO_MODE is '${DJANGO_MODE:-<unset>}' in .env. \
Reset is only allowed in development mode to prevent accidental data loss."
fi

# ── Confirmation ──────────────────────────────────────────────────────────────

printf '\n\033[1;33mWARNING:\033[0m This will permanently destroy:\n'
echo "  - All running containers"
echo "  - All named volumes (database, recordings, static files)"
echo "  - All Docker networks for this stack"
echo
printf '\033[1mType YES to confirm (default: No): \033[0m'
read -r reply

if [ "$reply" != "YES" ]; then
    echo "Cancelled."
    exit 0
fi

# ── Reset ─────────────────────────────────────────────────────────────────────

echo
docker compose down -v --remove-orphans
echo
printf '\033[32m✓\033[0m  Stack reset complete.\n'

# ── Redeploy ──────────────────────────────────────────────────────────────────

if [ "$AUTO_REDEPLOY" = true ]; then
    exec "$SCRIPT_DIR/update.sh" --from repo --no-pull --no-backup
fi

echo
printf '\033[1mRedeploy the stack now? [Y/n] \033[0m'
read -r redeploy

if [[ "${redeploy:-Y}" =~ ^[Yy]$ ]]; then
    exec "$SCRIPT_DIR/update.sh" --from repo --no-pull --no-backup
fi
