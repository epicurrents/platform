#!/usr/bin/env bash
# build_ohif.sh — Build the OHIF viewer for the *dicom* plugin.
#
# Usage:
#   ./scripts/build_ohif.sh
#
# What it does:
#   1. Checks out the ohif-viewer submodule at the commit the platform pins.
#   2. Installs OHIF's dependencies with pnpm (the version OHIF's package.json
#      names, through corepack when pnpm is not on PATH).
#   3. Builds the upstream viewer with PUBLIC_URL=/plugin/dicom/ohif/, so every
#      asset URL in index.html and the chunk loader points under the mount.
#   4. Copies the output to plugins/dicom/ohif-dist/ (served by Django) and puts
#      the platform's configuration in place of the build's app-config.js.
#
# Environment variables:
#   OHIF_APP_CONFIG   Path to an app-config.js to install instead of
#                     plugins/dicom/ohif-app-config.js. Its routerBasename must
#                     stay /plugin/dicom/ohif, the mount the build is made for.
#
# Prerequisites:
#   - git (with submodule support)
#   - Node.js >= 24 (OHIF 3.13's engines field)
#   - pnpm >= 12, or corepack (ships with Node) to provide it
#   - ~8 GB of free memory for the build
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
OHIF_DIR="$REPO_ROOT/plugins/dicom/ohif-viewer"
APP_DIR="$OHIF_DIR/platform/app"
DIST_DIR="$REPO_ROOT/plugins/dicom/ohif-dist"
APP_CONFIG="${OHIF_APP_CONFIG:-$REPO_ROOT/plugins/dicom/ohif-app-config.js}"
# The mount in plugins/dicom/public_urls.py. Trailing slash required: webpack
# concatenates it with asset names.
PUBLIC_URL="/plugin/dicom/ohif/"

die() { echo "ERROR: $*" >&2; exit 1; }

[ -f "$APP_CONFIG" ] || die "App config not found: $APP_CONFIG"

# ------------------------------------------------------------------
# 1. Submodule
# ------------------------------------------------------------------
echo "==> Checking out the OHIF submodule…"
cd "$REPO_ROOT"
# --checkout is required: the submodule is registered with `update = none` in
# .gitmodules (so default clones skip it), which a plain `submodule update`
# silently honours by doing nothing.
git submodule update --init --checkout plugins/dicom/ohif-viewer

# ------------------------------------------------------------------
# 2. Toolchain
# ------------------------------------------------------------------
command -v node >/dev/null 2>&1 || die "node is not on PATH (Node.js >= 24 required)."
NODE_MAJOR="$(node -p 'process.versions.node.split(".")[0]')"
[ "$NODE_MAJOR" -ge 24 ] || die "Node.js >= 24 required, found $(node --version)."

if ! command -v pnpm >/dev/null 2>&1; then
    command -v corepack >/dev/null 2>&1 || die "pnpm not found and corepack is unavailable to provide it."
    # OHIF's own scripts call `pnpm` by name, so `corepack pnpm` alone is not
    # enough: put corepack's shim on PATH for this run. The shim reads the
    # packageManager field of OHIF's package.json and runs exactly that version,
    # and nothing is installed globally.
    SHIM_DIR="$(mktemp -d)"
    trap 'rm -rf "$SHIM_DIR"' EXIT
    corepack enable --install-directory "$SHIM_DIR" pnpm
    export PATH="$SHIM_DIR:$PATH"
fi
PNPM=(pnpm)

# ------------------------------------------------------------------
# 3. Install and build
# ------------------------------------------------------------------
echo "==> Installing OHIF dependencies (this may take a few minutes)…"
cd "$OHIF_DIR"
"${PNPM[@]}" install --frozen-lockfile

echo "==> Building OHIF viewer for ${PUBLIC_URL}…"
cd "$APP_DIR"
PUBLIC_URL="$PUBLIC_URL" "${PNPM[@]}" run build:viewer

# ------------------------------------------------------------------
# 4. Install the dist
# ------------------------------------------------------------------
echo "==> Copying dist to ${DIST_DIR}…"
rm -rf "$DIST_DIR"
cp -R "$APP_DIR/dist" "$DIST_DIR"
cp "$APP_CONFIG" "$DIST_DIR/app-config.js"
# Neutralise OHIF's service worker. init-service-worker.js unregisters every
# service worker on the origin before registering its own, which on this
# platform removes the SPA's worker and with it push notification delivery; the
# worker itself imports Workbox from a Google CDN the CSP refuses. index.html
# still loads the file, so it stays as an empty module.
printf '/* Service worker disabled by scripts/build_ohif.sh; see plugins/dicom/README.md. */\n' \
    > "$DIST_DIR/init-service-worker.js"
rm -f "$DIST_DIR/sw.js"
# Source maps double the dist and are never requested by a browser without
# devtools open; a deployment that wants them can copy them over by hand.
find "$DIST_DIR" -name '*.map' -type f -delete

echo ""
echo "✓ OHIF viewer built successfully."
echo "  Dist:       $DIST_DIR"
echo "  Viewer URL: http://localhost:8000${PUBLIC_URL}viewer/dicomjson?url=<ohif-json URL>"
