#!/usr/bin/env bash
# Disable an Epicurrents plugin.
#
# Usage: scripts/disable_plugin.sh <plugin-name>
#
# Removes <plugin-name> from EPICURRENTS_PLUGINS in .env and VITE_PLUGINS in
# frontend/.env, then rebuilds the frontend so the plugin's frontend tree is
# dropped from the bundle.
#
# This does NOT drop the plugin's database tables — disabling is reversible and
# a re-enable expects its data intact. To remove a plugin's data permanently,
# handle that explicitly out of band. Keeps db and redis untouched.

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

# Return the comma-separated value of KEY from FILE (empty if absent/blank).
read_list() {
    grep -E "^$1=" "$2" 2>/dev/null | head -1 | cut -d= -f2- | tr -d ' "'"'"'' || true
}

# Remove PLUGIN from a comma-separated list, preserving the order of the rest.
list_remove() {
    local current="$1" out="" item
    # Early return on an empty list: `read -ra items <<< ""` leaves the array
    # unset under bash 3.2 (macOS), and "${items[@]}" then trips `set -u`.
    if [[ -z "$current" ]]; then
        printf ''
        return 0
    fi
    local items=()
    IFS=',' read -ra items <<< "$current"
    for item in "${items[@]}"; do
        [[ -z "$item" || "$item" == "$PLUGIN" ]] && continue
        [[ -z "$out" ]] && out="$item" || out="$out,$item"
    done
    printf '%s' "$out"
}

# Set KEY=VALUE in FILE in place. No-op when the key is absent. Rewrites via a
# temp file rather than sed -i, whose in-place flag is incompatible between
# BSD (macOS) and GNU (Linux) sed.
set_kv() {
    local key="$1" value="$2" file="$3" tmp
    grep -qE "^$key=" "$file" 2>/dev/null || return 0
    tmp="$(mktemp)"
    awk -v key="$key" -v value="$value" \
        'index($0, key "=") == 1 { print key "=" value; next } { print }' \
        "$file" > "$tmp"
    mv "$tmp" "$file"
}

echo "==> Disabling plugin: $PLUGIN"
echo ""

NEW_BACKEND=$(list_remove "$(read_list EPICURRENTS_PLUGINS "$ENV_FILE")")
NEW_FRONTEND=$(list_remove "$(read_list VITE_PLUGINS "$FRONTEND_ENV")")

echo "--- Step 1/2: Updating environment files..."
set_kv EPICURRENTS_PLUGINS "$NEW_BACKEND" "$ENV_FILE"
set_kv VITE_PLUGINS "$NEW_FRONTEND" "$FRONTEND_ENV"
echo "    .env:          EPICURRENTS_PLUGINS=$NEW_BACKEND"
echo "    frontend/.env: VITE_PLUGINS=$NEW_FRONTEND"
echo ""

echo "--- Step 2/2: Rebuilding frontend..."
"$SCRIPT_DIR/rebuild-frontend.sh"
echo ""

echo "==> Plugin '$PLUGIN' disabled (its data was left intact). Restart the stack:"
echo "    docker compose -f $ROOT/docker-compose.yml up -d"
