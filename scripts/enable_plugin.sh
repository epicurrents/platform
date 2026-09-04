#!/usr/bin/env bash
# Enable an Epicurrents plugin.
#
# Usage: scripts/enable_plugin.sh <plugin-name>
#
# Adds <plugin-name> to EPICURRENTS_PLUGINS in .env and VITE_PLUGINS in
# frontend/.env (both comma-separated), fetches any plugin-specific submodule
# (the dicom plugin ships its OHIF viewer this way), applies the plugin's
# migrations against PostgreSQL, and rebuilds the frontend so the plugin's
# frontend tree is compiled in.
#
# Symmetric with scripts/switch_project.sh, but a deployment may enable zero
# or more plugins, so this is additive rather than a swap. Keeps db and redis
# running throughout.
#
# NEVER run migrate directly on the host — that would target the local SQLite
# dev database, not the Docker PostgreSQL, leaving the two inconsistent.

set -euo pipefail

PLUGIN="${1:-}"
if [[ -z "$PLUGIN" ]]; then
    echo "Usage: $0 <plugin-name>" >&2
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$SCRIPT_DIR")"
ENV_FILE="$ROOT/.env"
FRONTEND_ENV="$ROOT/frontend/.env"
COMPOSE="docker compose -f $ROOT/docker-compose.yml"

if [[ ! -d "$ROOT/plugins/$PLUGIN" ]]; then
    echo "Error: plugins/$PLUGIN/ does not exist." >&2
    exit 1
fi

# Return the comma-separated value of KEY from FILE (empty if absent/blank).
read_list() {
    grep -E "^$1=" "$2" 2>/dev/null | head -1 | cut -d= -f2- | tr -d ' "'"'"'' || true
}

# Add PLUGIN to a comma-separated list, idempotently, preserving order.
list_add() {
    local current="$1"
    case ",$current," in
        *",$PLUGIN,"*) printf '%s' "$current" ;;                 # already present
        *) [[ -z "$current" ]] && printf '%s' "$PLUGIN" || printf '%s,%s' "$current" "$PLUGIN" ;;
    esac
}

# Upsert KEY=VALUE in FILE. Appends the line if the key is absent. Rewrites
# via a temp file rather than sed -i, whose in-place flag is incompatible
# between BSD (macOS) and GNU (Linux) sed.
set_kv() {
    local key="$1" value="$2" file="$3"
    if grep -qE "^$key=" "$file" 2>/dev/null; then
        local tmp
        tmp="$(mktemp)"
        awk -v key="$key" -v value="$value" \
            'index($0, key "=") == 1 { print key "=" value; next } { print }' \
            "$file" > "$tmp"
        mv "$tmp" "$file"
    else
        printf '%s=%s\n' "$key" "$value" >> "$file"
    fi
}

echo "==> Enabling plugin: $PLUGIN"
echo ""

NEW_BACKEND=$(list_add "$(read_list EPICURRENTS_PLUGINS "$ENV_FILE")")
NEW_FRONTEND=$(list_add "$(read_list VITE_PLUGINS "$FRONTEND_ENV")")

# Step 1: plugin-conditional submodules. The dicom plugin's OHIF viewer is
# marked `update = none` in .gitmodules; --checkout overrides that for this
# single invocation so it gets a real working tree.
echo "--- Step 1/4: Fetching plugin submodules (if any)..."
if [[ "$PLUGIN" == "dicom" ]]; then
    git -C "$ROOT" submodule update --init --checkout plugins/dicom/ohif-viewer
fi
echo ""

# Step 2: migrate with the new plugin list passed as an explicit override, so
# the env files are only written once the migration has succeeded — a failed
# migrate must not leave the plugin half-enabled in .env.
echo "--- Step 2/4: Ensuring database is running and applying migrations..."
$COMPOSE up -d db redis
$COMPOSE run --rm --no-deps -e EPICURRENTS_PLUGINS="$NEW_BACKEND" web python manage.py migrate
echo ""

echo "--- Step 3/4: Updating environment files..."
set_kv EPICURRENTS_PLUGINS "$NEW_BACKEND" "$ENV_FILE"
touch "$FRONTEND_ENV"
set_kv VITE_PLUGINS "$NEW_FRONTEND" "$FRONTEND_ENV"
echo "    .env:          EPICURRENTS_PLUGINS=$NEW_BACKEND"
echo "    frontend/.env: VITE_PLUGINS=$NEW_FRONTEND"
echo ""

echo "--- Step 4/4: Rebuilding frontend..."
"$SCRIPT_DIR/rebuild-frontend.sh"
echo ""

echo "==> Plugin '$PLUGIN' enabled. Restart the stack to serve it:"
echo "    $COMPOSE up -d"
