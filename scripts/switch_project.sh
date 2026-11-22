#!/usr/bin/env bash
# Switch the active Epicurrents project.
#
# Usage: scripts/switch_project.sh <new-project-name>
#
# The script keeps db and redis running throughout. It stops only the
# application services, runs activate/deactivate against PostgreSQL via
# compose run (Docker or Podman), rebuilds the frontend, and restarts everything.
#
# NEVER run activate_project / deactivate_project directly on the host —
# those commands would target the local SQLite dev database, not PostgreSQL,
# leaving the Docker database in an inconsistent state.

set -euo pipefail

NEW_PROJECT="${1:-}"
# An option is never a project name. Taken as one, `--help` would archive the
# active project's tables and stop the stack before failing on the name.
if [[ "$NEW_PROJECT" == "-h" || "$NEW_PROJECT" == "--help" ]]; then
    echo "Usage: $0 <new-project-name>"
    exit 0
fi
if [[ $# -ne 1 || -z "$NEW_PROJECT" || "$NEW_PROJECT" == -* ]]; then
    echo "Usage: $0 <new-project-name>" >&2
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$SCRIPT_DIR")"

# Every other bad name is refused here as well, before anything is stopped. Past
# this point a name that activate_project would reject has already cost the
# running services, the active project's deactivation and both env files.
if [[ ! "$NEW_PROJECT" =~ ^[a-z_][a-z0-9_]*$ ]]; then
    echo "Error: '$NEW_PROJECT' is not a project name (lower-case letters, digits and underscores)." >&2
    exit 1
fi
if [[ ! -d "$ROOT/projects/$NEW_PROJECT" ]]; then
    echo "Error: no project at $ROOT/projects/$NEW_PROJECT" >&2
    exit 1
fi

ENV_FILE="$ROOT/.env"
FRONTEND_ENV="$ROOT/frontend/.env"

# Portable in-place sed: BSD/macOS sed requires `-i ''` while GNU sed rejects the
# empty argument, so `sed -i ''` runs on dev macOS but breaks on the Ubuntu deploy
# host (GNU). Edit through a temp file instead, which both userlands honour. The
# result is written back into the file rather than moved over it: a move would
# leave .env with mktemp's 0600 mode, which the containers' account may not be
# able to read, and would replace a symlinked .env with a regular file.
sed_inplace() {
    local script="$1" file="$2" tmp status=0
    tmp="$(mktemp)"
    if sed "$script" "$file" > "$tmp"; then
        cat "$tmp" > "$file" || status=$?
    else
        status=$?
    fi
    rm -f "$tmp"
    return "$status"
}

# Set KEY=VALUE in FILE: replace the line where the key is present, append it
# where it is not, and create the file if it does not exist. A key missing from
# the file would otherwise leave the edit silently undone.
set_env_key() {
    local file="$1" key="$2" value="$3" escaped
    touch "$file"
    if grep -q "^${key}=" "$file"; then
        # Escape sed replacement-string metacharacters in the value. Without
        # this, `&` expands to the matched text and `\` is treated as an escape;
        # `|` is escaped too because it is the delimiter below.
        escaped=$(printf '%s' "$value" | sed -e 's/[\\&|]/\\&/g')
        sed_inplace "s|^${key}=.*|${key}=${escaped}|" "$file"
    else
        printf '%s=%s\n' "$key" "$value" >> "$file"
    fi
}

if [[ ! -f "$ENV_FILE" ]]; then
    echo "Error: $ENV_FILE not found" >&2
    exit 1
fi

# Read current project from .env. Blank or absent means the base platform with
# no project active, which is a valid state to switch from.
CURRENT_PROJECT=$(grep '^EPICURRENTS_PROJECT=' "$ENV_FILE" | cut -d= -f2- || true)

if [[ "$CURRENT_PROJECT" == "$NEW_PROJECT" ]]; then
    echo "Project is already '$NEW_PROJECT' — nothing to do."
    exit 0
fi

echo "==> Switching project: ${CURRENT_PROJECT:-(none)} → $NEW_PROJECT"
echo ""

# Docker or Podman, settled by the helper every operator script shares. Array form
# keeps a root path with a space in it one argument. Settled only here, past every
# refusal and the no-op exit, so those run nothing; a host with no runtime is
# refused before the first step, with nothing stopped and no env file edited.
# shellcheck source=scripts/lib/compose.sh
. "$SCRIPT_DIR/lib/compose.sh"
compose_detect_runtime
compose_require_runtime || exit 1
COMPOSE+=(-f "$ROOT/docker-compose.yml")

# 1. Ensure db and redis are running (idempotent)
echo "--- Step 1/6: Ensuring database is running..."
"${COMPOSE[@]}" up -d db redis
echo ""

# 2. Stop application services only
echo "--- Step 2/6: Stopping application services..."
"${COMPOSE[@]}" stop web celery celery-beat || true
echo ""

# 3. Deactivate current project against PostgreSQL
if [[ -n "$CURRENT_PROJECT" ]]; then
    echo "--- Step 3/6: Deactivating project '$CURRENT_PROJECT'..."
    "${COMPOSE[@]}" run --rm --no-deps web python manage.py deactivate_project
else
    echo "--- Step 3/6: No project active — nothing to deactivate."
fi
echo ""

# 4. Update .env files
echo "--- Step 4/6: Updating environment files..."
set_env_key "$ENV_FILE" EPICURRENTS_PROJECT "$NEW_PROJECT"
set_env_key "$FRONTEND_ENV" VITE_PROJECT "$NEW_PROJECT"
echo "    .env:          EPICURRENTS_PROJECT=${NEW_PROJECT}"
echo "    frontend/.env: VITE_PROJECT=${NEW_PROJECT}"
echo ""

# Note: DICOM is no longer a project — it migrated to a plugin. Its OHIF
# viewer submodule is fetched by scripts/enable_plugin.sh (or by bootstrap.sh
# when EPICURRENTS_PLUGINS contains `dicom`), not here.

# 5. Activate new project against PostgreSQL (reads updated .env)
echo "--- Step 5/6: Activating project '$NEW_PROJECT'..."
"${COMPOSE[@]}" run --rm --no-deps web python manage.py activate_project "$NEW_PROJECT"
echo ""

# 6. Rebuild frontend
echo "--- Step 6/6: Rebuilding frontend..."
(cd "$ROOT/frontend" && npm run build)
echo ""

# 7. Start all services (recreates containers so they pick up the new .env)
echo "--- Starting all services..."
"${COMPOSE[@]}" up -d --force-recreate
echo ""

echo "==> Done. Project is now '$NEW_PROJECT'."
