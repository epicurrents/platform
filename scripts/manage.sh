#!/usr/bin/env bash
# manage.sh — run a Django management command inside the web container
#
# Usage:
#   ./scripts/manage.sh <command> [args...]
#
# Examples:
#   ./scripts/manage.sh migrate
#   ./scripts/manage.sh createsuperuser
#   ./scripts/manage.sh shell
#   ./scripts/manage.sh rollback_change 42 --user-id 1
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/.."

if [ $# -eq 0 ]; then
    echo "Usage: $0 <management-command> [args...]"
    echo "       $0 help   — list available commands"
    exit 1
fi

exec docker compose run --rm web python manage.py "$@"
