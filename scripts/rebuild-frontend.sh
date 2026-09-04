#!/usr/bin/env bash
# rebuild-frontend.sh — build the Vue frontend and restart the web container
#
# Usage:
#   ./scripts/rebuild-frontend.sh             — assume viewer dist/ is current, build platform only
#   ./scripts/rebuild-frontend.sh --viewer    — also rebuild the viewer's per-workspace dist outputs
#                                                via `npm run build:tsc-all` (~1–2 min). Needed after
#                                                a clean clone or any change to viewer source.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/.."

info() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
ok()   { printf '    \033[32m✓\033[0m  %s\n'  "$*"; }
die()  { printf '\n\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

REBUILD_VIEWER=false
for arg in "$@"; do
    case "$arg" in
        --viewer) REBUILD_VIEWER=true ;;
        *) die "Unknown argument: $arg" ;;
    esac
done

# ── 1. (Optional) Rebuild the viewer's per-workspace tsc outputs ─────────────
# The platform imports compiled files from viewer/{util,epicurrents}/*/dist/.
# Those dist/ dirs are gitignored, so on a fresh clone (or after touching viewer
# source), they need to be built before the platform build can resolve its
# imports. `build:tsc-all` (tsc) covers those direct library imports.
#
# It does NOT, however, cover the inlined WORKERS. The lib inlines each worker as
# ?raw text from a package's dist/workers/*.worker.js — the WEBPACK bundle, built
# by that package's `build:umd` + `copy:scripts`, not by tsc. So a change to
# worker source (e.g. pyodide-service) is invisible to build:viewer until the
# package's full build runs. We do that here for pyodide-service — the actively
# developed worker — so it is not a forgettable manual pre-step. Add other worker
# packages here if/when their source changes.

if [ "$REBUILD_VIEWER" = true ]; then
    info "Rebuilding viewer per-workspace tsc outputs"
    cd frontend/viewer
    npm run build:tsc-all
    ok "Viewer tsc outputs current"

    info "Rebuilding pyodide-service worker bundle (webpack umd + copy:scripts)"
    ( cd epicurrents/pyodide-service && npm run build )
    ok "pyodide-service worker bundle current"

    # The lib inlines interface/dist/workers/*.worker.js (via a ?raw import). That
    # directory is populated by copy:workers (umd/*.worker.js -> dist/workers/),
    # which normally runs only on start/dev, NOT on build. Without this, build:viewer
    # keeps inlining the stale worker bundle no matter how many times the package is
    # rebuilt. NB: the working copy:workers is the VIEWER-ROOT script (node
    # scripts/copy.mjs workers) — run from here (frontend/viewer), not from interface.
    info "Copying fresh umd worker bundles into interface/dist/workers (copy:workers)"
    npm run copy:workers
    ok "interface/dist/workers current"

    cd ../..
fi

# ── 2. Build frontend (viewer interface + platform) ───────────────────────────

info "Building frontend"
cd frontend
npm run build:viewer
npm run build
cd ..
ok "Frontend built"

# ── 3. Restart web container ──────────────────────────────────────────────────

info "Restarting web container"
docker compose restart web
ok "Web container restarted"
