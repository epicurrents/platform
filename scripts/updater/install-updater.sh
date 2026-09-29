#!/usr/bin/env bash
# install-updater.sh — install the remote-maintenance host agent for one
# deployment. Run as root, once; running it again refreshes the agent and
# leaves the configuration alone.
#
# What it puts where:
#   /etc/epicurrents-updater/config        DEPLOY_ROOT, ENABLED=0, timeouts (kept on re-run)
#   /etc/epicurrents-updater/release.pub   the release public key, root-owned
#   /usr/local/lib/epicurrents-updater/    the agent and its own copy of update.sh
#   /var/lib/epicurrents-updater/          the agent's state of record (job statuses, logs,
#                                          the active-run lock), verified package copies
#   /etc/systemd/system/                   epicurrents-updater.service + .timer (one-minute timer)
#   <root>/update/{packages,jobs}          the spool, owned by the deployment account
#
# The agent stays disabled until ENABLED=1 is set in the config, or --enable is
# passed; the platform's own REMOTE_MAINTENANCE_ENABLED and REMOTE_UPDATE_ENABLED
# flags in <root>/.env are the other half of switching remote updates on.
#
# Usage:
#   sudo ./updater/install-updater.sh [--root DIR] [--key PATH] [--enable] [--allow-checkout]
#
#   --root DIR         The deployment. Default: the directory this updater/
#                      directory sits in, which is where a package unpacks it.
#   --key PATH         The release public key to trust. Default: RELEASE_KEY.pub
#                      at the deployment root, which the package shipped.
#                      Verify its id (printed at the end) out of band.
#   --enable           Write ENABLED=1 into the config.
#   --allow-checkout   Let the agent update a git checkout (ALLOW_CHECKOUT=1).
#                      Remote updates are meant for distribution deployments.
#   --self-update      Let the agent replace itself with the copy a verified
#                      package ships (SELF_UPDATE=1). Off, a newer agent is
#                      installed by re-running this installer from the package.
#   --update-sh PATH   The update.sh to install as the agent's copy. Default:
#                      update.sh beside this updater/ directory.
#
set -euo pipefail

CONFIG_DIR="${EPICURRENTS_UPDATER_CONFIG_DIR:-/etc/epicurrents-updater}"
LIB_DIR="${EPICURRENTS_UPDATER_LIB_DIR:-/usr/local/lib/epicurrents-updater}"
STATE_DIR="${EPICURRENTS_UPDATER_STATE_DIR:-/var/lib/epicurrents-updater}"
UNIT_DIR="${EPICURRENTS_UPDATER_UNIT_DIR:-/etc/systemd/system}"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_ROOT=""
KEY=""
ENABLE=false
ALLOW_CHECKOUT=false
SELF_UPDATE=false
UPDATE_SH_SOURCE=""

die() {
    printf 'ERROR: %s\n' "$*" >&2
    exit 1
}

need_value() {
    case "${2:-}" in
        ""|-*) die "Option '$1' requires a value." ;;
    esac
}

while [ $# -gt 0 ]; do
    case "$1" in
        --root)        need_value "$1" "${2:-}"; DEPLOY_ROOT="$2"; shift 2 ;;
        --root=*)      DEPLOY_ROOT="${1#*=}"; shift ;;
        --key)         need_value "$1" "${2:-}"; KEY="$2"; shift 2 ;;
        --key=*)       KEY="${1#*=}"; shift ;;
        --update-sh)   need_value "$1" "${2:-}"; UPDATE_SH_SOURCE="$2"; shift 2 ;;
        --update-sh=*) UPDATE_SH_SOURCE="${1#*=}"; shift ;;
        --enable)      ENABLE=true; shift ;;
        --allow-checkout) ALLOW_CHECKOUT=true; shift ;;
        --self-update) SELF_UPDATE=true; shift ;;
        -h|--help)
            awk 'NR == 1 { next } /^#/ { sub(/^#{1,2} ?/, ""); print; next } { exit }' "$0"
            exit 0
            ;;
        *) die "Unknown argument: $1 (try --help)" ;;
    esac
done

[ "$(id -u)" = 0 ] || die "Run this as root: sudo $0"

# ── The host ──────────────────────────────────────────────────────────────────

if ! command -v systemctl >/dev/null 2>&1 || [ ! -d "$UNIT_DIR" ]; then
    die "This host has no systemd, which the agent's timer needs. Remote updates are not supported here; the Maintenance tab's other operations still work without the agent."
fi
for tool in python3 curl rsync tar; do
    command -v "$tool" >/dev/null 2>&1 || die "$tool is required on the host (the agent handles JSON with python3, probes with curl, and update.sh overlays with rsync)."
done
if command -v flock >/dev/null 2>&1; then
    :
else
    echo "    note: flock is not installed; systemd alone keeps the agent's ticks from overlapping." >&2
fi

# ── The deployment ────────────────────────────────────────────────────────────

if [ -z "$DEPLOY_ROOT" ]; then
    DEPLOY_ROOT="$(cd "$HERE/.." && pwd)"
fi
[ -d "$DEPLOY_ROOT" ] || die "--root: no such directory: $DEPLOY_ROOT"
DEPLOY_ROOT="$(cd "$DEPLOY_ROOT" && pwd)"
[ -f "$DEPLOY_ROOT/docker-compose.yml" ] || die "$DEPLOY_ROOT does not look like a deployment (no docker-compose.yml). Pass --root DIR."
[ -f "$DEPLOY_ROOT/docker-compose.prod.yml" ] || die "$DEPLOY_ROOT has no docker-compose.prod.yml; the agent updates a stack on the production overlay."

[ -n "$KEY" ] || KEY="$DEPLOY_ROOT/RELEASE_KEY.pub"
[ -f "$KEY" ] || die "No release key at $KEY. A signed package ships RELEASE_KEY.pub at its root; pass --key PATH to name another."
grep -q 'BEGIN PUBLIC KEY' "$KEY" || die "$KEY is not a PEM public key."

[ -n "$UPDATE_SH_SOURCE" ] || UPDATE_SH_SOURCE="$HERE/../update.sh"
[ -f "$UPDATE_SH_SOURCE" ] || UPDATE_SH_SOURCE="$DEPLOY_ROOT/update.sh"
[ -f "$UPDATE_SH_SOURCE" ] || die "No update.sh to install (looked beside updater/ and at $DEPLOY_ROOT/update.sh). Pass --update-sh PATH."
bash -n "$UPDATE_SH_SOURCE" || die "$UPDATE_SH_SOURCE does not parse as a shell script."

OWNER="$(stat -c %u:%g "$DEPLOY_ROOT" 2>/dev/null || echo 1000:1000)"

# ── Files ─────────────────────────────────────────────────────────────────────

echo "==> Configuration in $CONFIG_DIR"
install -d -m 0700 -o 0 -g 0 "$CONFIG_DIR"
install -m 0600 -o 0 -g 0 "$KEY" "$CONFIG_DIR/release.pub"
if [ -f "$CONFIG_DIR/config" ]; then
    echo "    config exists; leaving it as it is"
else
    {
        echo "# epicurrents-updater configuration. Read by the agent on every tick."
        echo "# The deployment this agent updates."
        echo "DEPLOY_ROOT=\"$DEPLOY_ROOT\""
        echo "# 1 to act on requests; 0 to only report a heartbeat and refuse them."
        echo "ENABLED=0"
        echo "# 1 to update a git checkout; remote updates are meant for distribution deployments."
        echo "ALLOW_CHECKOUT=0"
        echo "# 1 to let the agent replace itself with the copy a verified package ships."
        echo "SELF_UPDATE=0"
        echo "# Free space required under DEPLOY_ROOT beyond twice the package size, in bytes."
        echo "MIN_FREE_BYTES=1073741824"
        echo "# Seconds to wait for the readiness probe after an update or a rollback."
        echo "HEALTH_TIMEOUT=300"
        echo "# Seconds to wait for the worker to finish its tasks before the snapshot."
        echo "DRAIN_TIMEOUT=60"
        echo "# Minutes an update waits for a confirmation when the request names no window."
        echo "VERIFY_WINDOW_MINUTES=30"
    } > "$CONFIG_DIR/config"
    chmod 0600 "$CONFIG_DIR/config"
    echo "    config written (ENABLED=0)"
fi
if [ "$ENABLE" = true ]; then
    sed -i.bak 's/^ENABLED=.*/ENABLED=1/' "$CONFIG_DIR/config" && rm -f "$CONFIG_DIR/config.bak"
    echo "    ENABLED=1"
fi
if [ "$ALLOW_CHECKOUT" = true ]; then
    sed -i.bak 's/^ALLOW_CHECKOUT=.*/ALLOW_CHECKOUT=1/' "$CONFIG_DIR/config" && rm -f "$CONFIG_DIR/config.bak"
    echo "    ALLOW_CHECKOUT=1"
fi
if [ "$SELF_UPDATE" = true ]; then
    if grep -q '^SELF_UPDATE=' "$CONFIG_DIR/config"; then
        sed -i.bak 's/^SELF_UPDATE=.*/SELF_UPDATE=1/' "$CONFIG_DIR/config" && rm -f "$CONFIG_DIR/config.bak"
    else
        echo "SELF_UPDATE=1" >> "$CONFIG_DIR/config"
    fi
    echo "    SELF_UPDATE=1"
fi

echo "==> Agent in $LIB_DIR"
install -d -m 0755 -o 0 -g 0 "$LIB_DIR"
install -m 0755 -o 0 -g 0 "$HERE/epicurrents-updater.sh" "$LIB_DIR/epicurrents-updater.sh"
install -m 0755 -o 0 -g 0 "$UPDATE_SH_SOURCE" "$LIB_DIR/update.sh"
if [ -f "$HERE/README.md" ]; then
    install -m 0644 -o 0 -g 0 "$HERE/README.md" "$LIB_DIR/README.md"
fi
install -d -m 0700 -o 0 -g 0 "$STATE_DIR" "$STATE_DIR/work" "$STATE_DIR/jobs"
echo "    epicurrents-updater.sh, update.sh (from $UPDATE_SH_SOURCE)"

echo "==> Spool under $DEPLOY_ROOT/update"
# Owned by the deployment account, which is what writes requests there; a
# directory Docker or root created would belong to root and the web tier could
# never write a request.
install -d -o "${OWNER%%:*}" -g "${OWNER##*:}" "$DEPLOY_ROOT/update" "$DEPLOY_ROOT/update/packages" "$DEPLOY_ROOT/update/jobs"
echo "    packages/ and jobs/ owned by $OWNER"

echo "==> systemd timer"
install -m 0644 -o 0 -g 0 "$HERE/epicurrents-updater.service" "$UNIT_DIR/epicurrents-updater.service"
install -m 0644 -o 0 -g 0 "$HERE/epicurrents-updater.timer" "$UNIT_DIR/epicurrents-updater.timer"
if [ "$LIB_DIR" != /usr/local/lib/epicurrents-updater ]; then
    sed -i.bak "s|/usr/local/lib/epicurrents-updater|$LIB_DIR|g" "$UNIT_DIR/epicurrents-updater.service" \
        && rm -f "$UNIT_DIR/epicurrents-updater.service.bak"
fi
systemctl daemon-reload
systemctl enable --now epicurrents-updater.timer
echo "    epicurrents-updater.timer enabled (every minute)"

# ── The key id, for the operator to verify out of band ────────────────────────
# The same identifier release_sign.py prints: the leading hex of the SHA-256
# of the raw 32-byte key, which sits at the end of the DER encoding.
KEY_ID=""
if command -v openssl >/dev/null 2>&1; then
    KEY_ID="$(openssl pkey -pubin -in "$KEY" -outform DER 2>/dev/null | tail -c 32 | { sha256sum 2>/dev/null || shasum -a 256; } | cut -c1-16 || true)"
fi

echo
echo "================================================================"
echo " Remote-maintenance agent installed"
echo "================================================================"
echo
echo "  Deployment:   $DEPLOY_ROOT"
echo "  Release key:  $CONFIG_DIR/release.pub${KEY_ID:+ (key id $KEY_ID)}"
echo "  Config:       $CONFIG_DIR/config"
echo "  Timer:        systemctl status epicurrents-updater.timer"
echo "  Journal:      journalctl -t epicurrents-updater"
echo
if [ -n "$KEY_ID" ]; then
    echo "  Compare the key id with the one the release's publisher gave you. A"
    echo "  package signed by any other key is refused from now on."
    echo
fi
if [ "$ENABLE" = true ]; then
    echo "  The agent acts on requests. To switch remote updates on in the platform:"
else
    echo "  The agent reports a heartbeat and refuses requests until ENABLED=1 is"
    echo "  set in $CONFIG_DIR/config. Then, in the platform:"
fi
echo
echo "      REMOTE_MAINTENANCE_ENABLED=true"
echo "      REMOTE_UPDATE_ENABLED=true"
echo
echo "  in $DEPLOY_ROOT/.env, and recreate web, celery and celery-beat."
echo "================================================================"
