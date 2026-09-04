#!/usr/bin/env bash
# tailscale-serve.sh — publish this deployment's web UI on your tailnet, at
# https://<name>.<tailnet>.ts.net, without touching the host.
#
# Runs the profile-gated `tailscale` service from docker-compose.yml with a
# one-time auth key. The key is used only for this registration and is never
# written to .env or any file: once the node registers, its identity persists
# in the tailscale-state volume, so later restarts need no key. The device
# hostname IS saved to .env (TS_HOSTNAME) so it stays stable across restarts.
#
# INBOUND ONLY, and that is the whole distinction from scripts/tailscale-join.sh.
# The container runs in userspace mode with no TUN device, so it routes for
# itself and nothing else: the host does not gain a tailnet address, and no
# sibling container can reach one. If you need the log shipper to push to an
# evidence host, or anything else that leaves this machine over the tailnet, use
# tailscale-join.sh — which installs Tailscale on the host and gives containers
# a route out. The two compose: joining the host and serving the UI are separate
# questions with separate answers.
#
# Prerequisite: the deployment must be bootstrapped (.env present, stack up).
#
# Usage:
#   ./scripts/tailscale-serve.sh --authkey <tskey-...> [--hostname <name>]
#
# The auth key may instead be supplied via the TS_AUTHKEY environment variable
# to keep it out of the shell history and process arguments:
#   TS_AUTHKEY=tskey-... ./scripts/tailscale-serve.sh --hostname mybox
#
# After it comes up, check the tailnet status with:
#   docker compose --profile tailnet exec tailscale tailscale status
set -euo pipefail

# In a git checkout this script lives in scripts/; in a distribution it is
# bundled at the deployment root. Detect by the compose file, as update.sh does.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -f "$SCRIPT_DIR/docker-compose.yml" ]; then
    cd "$SCRIPT_DIR"
else
    cd "$SCRIPT_DIR/.."
fi

info()  { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
ok()    { printf '    \033[32m✓\033[0m  %s\n'  "$*"; }
warn()  { printf '    \033[33m!\033[0m  %s\n'  "$*"; }
die()   { printf '\n\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

usage() {
    cat <<'EOF'
tailscale-serve.sh — publish this deployment's web UI on your tailnet.

Usage:
  ./scripts/tailscale-serve.sh --authkey <tskey-...> [--hostname <name>]

Options:
  --authkey <key>    Tailscale auth key (or set the TS_AUTHKEY env var to keep
                     it out of argv / shell history). Single-use; never stored.
  --hostname <name>  Tailnet device name. Defaults to TS_HOSTNAME in .env, or
                     "epicurrents". Saved to .env so restarts stay stable.
  -h, --help         Show this help.

Inbound only. To give the host itself a tailnet address — and containers a route
out, which the log shipper needs — see scripts/tailscale-join.sh.
EOF
}

# The auth key defaults from the environment so a caller can avoid putting it in
# argv (visible in `ps` / shell history); --authkey overrides it.
AUTHKEY="${TS_AUTHKEY:-}"
HOSTNAME_ARG=""

while [ $# -gt 0 ]; do
    case "$1" in
        --authkey)    AUTHKEY="${2:-}"; shift 2 ;;
        --authkey=*)  AUTHKEY="${1#*=}"; shift ;;
        --hostname)   HOSTNAME_ARG="${2:-}"; shift 2 ;;
        --hostname=*) HOSTNAME_ARG="${1#*=}"; shift ;;
        -h|--help)    usage; exit 0 ;;
        *)            die "Unknown argument: $1 (try --help)" ;;
    esac
done

[ -f .env ] || die ".env not found — set the deployment up first (start.sh, or scripts/bootstrap.sh)."
[ -n "$AUTHKEY" ] || die "No auth key. Pass --authkey <tskey-...> or set TS_AUTHKEY. \
Generate one at https://login.tailscale.com/admin/settings/keys"

# Resolve the device hostname: explicit flag > existing .env value > default.
if [ -z "$HOSTNAME_ARG" ]; then
    HOSTNAME_ARG="$(grep -E '^TS_HOSTNAME=' .env | head -1 | cut -d= -f2- | tr -d ' "'"'"'' || true)"
fi
[ -n "$HOSTNAME_ARG" ] || HOSTNAME_ARG="epicurrents"

# Validated before it is written, not after. The value goes into a sed
# replacement below, where a `|` or `&` in it rewrites the .env line into
# something else entirely — and the deployment finds out later, from an
# unrelated setting that stopped parsing. Tailscale accepts only this shape
# anyway, so nothing legitimate is turned away.
case "$HOSTNAME_ARG" in
    *[!A-Za-z0-9-]* | -* | "") die "Invalid tailnet hostname '$HOSTNAME_ARG' — letters, digits and hyphens only." ;;
esac

# Persist the (non-secret) hostname so restarts and re-registration stay stable.
# -i.bak + rm keeps the in-place edit portable across GNU and BSD sed.
set_env_var() {
    local key="$1" value="$2" file=".env"
    if grep -qE "^${key}=" "$file"; then
        sed -i.bak "s|^${key}=.*|${key}=${value}|" "$file"
        rm -f "${file}.bak"
    else
        printf '%s=%s\n' "$key" "$value" >> "$file"
    fi
}
set_env_var TS_HOSTNAME "$HOSTNAME_ARG"

info "Registering '$HOSTNAME_ARG' on the tailnet"
# The auth key lives only in this command's environment — never in .env. Compose
# reads ${TS_AUTHKEY} from here for the container; TS_HOSTNAME comes from .env.
TS_AUTHKEY="$AUTHKEY" docker compose --profile tailnet up -d tailscale

ok "tailscale container started as '$HOSTNAME_ARG'"
echo
echo "Check the tailnet status:"
echo "  docker compose --profile tailnet exec tailscale tailscale status"
echo
echo "Reachable in your tailnet at:  https://${HOSTNAME_ARG}.<your-tailnet>.ts.net"
warn "Add that MagicDNS name to ALLOWED_HOSTS and enable HTTPS Certificates in the tailnet admin console."
