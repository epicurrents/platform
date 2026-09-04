#!/usr/bin/env bash
# apply-changes.sh — build the Vue frontend and restart all app containers
#
# Rebuilds the frontend assets and restarts the web server and Celery worker
# containers so that both Python and frontend code changes take effect.
#
# Usage:
#   ./scripts/apply-changes.sh
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/.."

info() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
ok()   { printf '    \033[32m✓\033[0m  %s\n'  "$*"; }
die()  { printf '\n\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

# ── 1. Build frontend ─────────────────────────────────────────────────────────

info "Building frontend"
cd frontend
npm run build:viewer
npm run build
cd ..
ok "Frontend built"

# ── 2. Apply database migrations ─────────────────────────────────────────────

info "Applying database migrations"
docker compose exec web python manage.py migrate
ok "Migrations applied"

# ── 3. Restart web container ──────────────────────────────────────────────────

info "Restarting web container"
docker compose restart web
ok "Web container restarted"

# ── 4. Restart Celery workers ─────────────────────────────────────────────────
# The celery and celery-beat containers run Python code; they must be restarted
# whenever backend task logic changes (e.g. recordings/tasks.py).

info "Restarting Celery workers"
docker compose restart celery celery-beat
ok "Celery workers restarted"
