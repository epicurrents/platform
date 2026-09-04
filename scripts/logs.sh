#!/usr/bin/env bash
# logs.sh — tail logs for one service or the whole stack
#
# Usage:
#   ./scripts/logs.sh                  — follow all services
#   ./scripts/logs.sh web              — follow a single service
#   ./scripts/logs.sh web 200          — follow with custom tail line count
#
# Available services: web, celery, celery-beat, db, redis, borg
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/.."

SERVICE="${1:-}"
LINES="${2:-100}"

if [ -n "$SERVICE" ]; then
    exec docker compose logs -f --tail="$LINES" "$SERVICE"
else
    exec docker compose logs -f --tail="$LINES"
fi
