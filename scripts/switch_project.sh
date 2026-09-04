#!/usr/bin/env bash
# Switch the active Epicurrents project.
#
# Usage: scripts/switch_project.sh <new-project-name>
#
# The script keeps db and redis running throughout. It stops only the
# application services, runs activate/deactivate against PostgreSQL via
# docker compose run, rebuilds the frontend, and restarts everything.
#
# NEVER run activate_project / deactivate_project directly on the host —
# those commands would target the local SQLite dev database, not PostgreSQL,
# leaving the Docker database in an inconsistent state.

set -euo pipefail

NEW_PROJECT="${1:-}"
if [[ -z "$NEW_PROJECT" ]]; then
    echo "Usage: $0 <new-project-name>" >&2
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$SCRIPT_DIR")"
ENV_FILE="$ROOT/.env"
FRONTEND_ENV="$ROOT/frontend/.env"
COMPOSE="docker compose -f $ROOT/docker-compose.yml"

# Portable in-place sed: BSD/macOS sed requires `-i ''` while GNU sed rejects the
# empty argument, so `sed -i ''` runs on dev macOS but breaks on the Ubuntu deploy
# host (GNU). Edit through a temp file instead, which both userlands honour.
sed_inplace() {
    local script="$1" file="$2" tmp
    tmp="$(mktemp)"
    sed "$script" "$file" > "$tmp" && mv "$tmp" "$file"
}

# Read current project from .env
CURRENT_PROJECT=$(grep '^EPICURRENTS_PROJECT=' "$ENV_FILE" | cut -d= -f2 || true)
if [[ -z "$CURRENT_PROJECT" ]]; then
    echo "Error: EPICURRENTS_PROJECT not set in $ENV_FILE" >&2
    exit 1
fi

if [[ "$CURRENT_PROJECT" == "$NEW_PROJECT" ]]; then
    echo "Project is already '$NEW_PROJECT' — nothing to do."
    exit 0
fi

echo "==> Switching project: $CURRENT_PROJECT → $NEW_PROJECT"
echo ""

# 1. Ensure db and redis are running (idempotent)
echo "--- Step 1/6: Ensuring database is running..."
$COMPOSE up -d db redis
echo ""

# 2. Stop application services only
echo "--- Step 2/6: Stopping application services..."
$COMPOSE stop web celery celery-beat || true
echo ""

# 3. Deactivate current project against PostgreSQL
echo "--- Step 3/6: Deactivating project '$CURRENT_PROJECT'..."
$COMPOSE run --rm --no-deps web python manage.py deactivate_project
echo ""

# 4. Update .env files (portable in-place sed — see sed_inplace above)
# Escape sed replacement-string metacharacters in the project name. Without
# this, `&` expands to the matched text and `\` is treated as an escape,
# corrupting .env for projects whose names contain either character. `|` is
# escaped too because it's the delimiter used in the s|...|...| commands
# below.
NEW_PROJECT_ESCAPED=$(printf '%s' "$NEW_PROJECT" | sed -e 's/[\\&|]/\\&/g')

echo "--- Step 4/6: Updating environment files..."
sed_inplace "s|^EPICURRENTS_PROJECT=.*|EPICURRENTS_PROJECT=${NEW_PROJECT_ESCAPED}|" "$ENV_FILE"
sed_inplace "s|^VITE_PROJECT=.*|VITE_PROJECT=${NEW_PROJECT_ESCAPED}|" "$FRONTEND_ENV"
echo "    .env:          EPICURRENTS_PROJECT=${NEW_PROJECT}"
echo "    frontend/.env: VITE_PROJECT=${NEW_PROJECT}"
echo ""

# Note: DICOM is no longer a project — it migrated to a plugin. Its OHIF
# viewer submodule is fetched by scripts/enable_plugin.sh (or by bootstrap.sh
# when EPICURRENTS_PLUGINS contains `dicom`), not here.

# 5. Activate new project against PostgreSQL (reads updated .env)
echo "--- Step 5/6: Activating project '$NEW_PROJECT'..."
$COMPOSE run --rm --no-deps web python manage.py activate_project "$NEW_PROJECT"
echo ""

# 6. Rebuild frontend
echo "--- Step 6/6: Rebuilding frontend..."
(cd "$ROOT/frontend" && npm run build)
echo ""

# 7. Start all services (recreates containers so they pick up the new .env)
echo "--- Starting all services..."
$COMPOSE up -d --force-recreate
echo ""

echo "==> Done. Project is now '$NEW_PROJECT'."
