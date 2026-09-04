#!/usr/bin/env bash
# tailscale-join.sh — make this HOST a node on your tailnet, so the deployment
# can reach other tailnet machines and be reached by them.
#
# There are two ways to put an Epicurrents deployment on a tailnet, and they are
# not interchangeable:
#
#   tailscale-join.sh (this script)  Installs Tailscale on the host. The host
#       gets a tailnet address, MagicDNS resolution, and — the part nothing else
#       provides — a route *out*, which containers inherit through the normal
#       Docker bridge. This is what the log shipper needs to push to an evidence
#       host, and what lets you reach the deployment over the tailnet.
#
#   tailscale-serve.sh              Runs a Tailscale container in userspace mode
#       that publishes the web UI at https://<name>.<tailnet>.ts.net. Inbound
#       only, and the host is left untouched. Right when you cannot install
#       packages on the host, or on a laptop.
#
# Outbound is the asymmetry that matters. A userspace container has no TUN
# device, so it routes for nothing but itself and the host gains no tailnet
# address. There is one way around that without this script — the container can
# run an HTTP proxy (TS_OUTBOUND_HTTP_PROXY_LISTEN) that siblings point at, and
# it does work — but it carries HTTP only, so it leaves `ssh://` remote Borg
# out, and every client has to be configured for it by name. Prefer this script
# unless you cannot install packages on the host.
#
# Usage:
#   ./scripts/tailscale-join.sh --authkey <tskey-...> [--hostname <name>]
#
# The auth key may instead come from TS_AUTHKEY to keep it out of argv and the
# shell history:
#   TS_AUTHKEY=tskey-... ./scripts/tailscale-join.sh --hostname my-deployment
#
# Re-running is safe. If the host is already on a tailnet the key is not needed
# and not used; only the hostname is reconciled.
set -euo pipefail

# In a git checkout this script lives in scripts/; in a distribution it is
# bundled at the deployment root. Detect by the compose file, as update.sh does.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -f "$SCRIPT_DIR/docker-compose.yml" ]; then
    ROOT="$SCRIPT_DIR"
else
    ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
fi
cd "$ROOT"

info()  { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
ok()    { printf '    \033[32m✓\033[0m  %s\n'  "$*"; }
warn()  { printf '    \033[33m!\033[0m  %s\n'  "$*"; }
die()   { printf '\n\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

usage() {
    cat <<'EOF'
tailscale-join.sh — make this host a node on your tailnet.

Usage:
  ./scripts/tailscale-join.sh --authkey <tskey-...> [--hostname <name>]

Options:
  --authkey <key>    Tailscale auth key (or set the TS_AUTHKEY env var to keep
                     it out of argv / shell history). Never written to disk.
  --hostname <name>  Tailnet device name. Defaults to TS_HOSTNAME in .env, or
                     "epicurrents". Saved to .env so it stays stable.
  --no-install       Fail rather than installing Tailscale if it is absent.
  -h, --help         Show this help.

For inbound-only publication of the web UI without touching the host, see
scripts/tailscale-serve.sh instead.
EOF
}

AUTHKEY="${TS_AUTHKEY:-}"
HOSTNAME_ARG=""
INSTALL=true

while [ $# -gt 0 ]; do
    case "$1" in
        --authkey)    AUTHKEY="${2:-}"; shift 2 ;;
        --authkey=*)  AUTHKEY="${1#*=}"; shift ;;
        --hostname)   HOSTNAME_ARG="${2:-}"; shift 2 ;;
        --hostname=*) HOSTNAME_ARG="${1#*=}"; shift ;;
        --no-install) INSTALL=false; shift ;;
        -h|--help)    usage; exit 0 ;;
        *)            die "Unknown argument: $1 (try --help)" ;;
    esac
done

[ -f .env ] || die ".env not found — set the deployment up first (start.sh, or scripts/bootstrap.sh)."

[ "$(uname -s)" = "Linux" ] || die "This script installs the Linux package. On macOS or Windows, \
install the Tailscale app and sign in, then re-run nothing — the host is already a node."

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

# Persist the (non-secret) hostname so re-runs and the serve container agree on
# the device name. -i.bak + rm keeps the in-place edit portable across GNU/BSD sed.
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

# Read to EOF rather than piping into `head`: under `set -o pipefail` an early
# close makes the producer die of SIGPIPE and the whole substitution fail.
ts_version() {
    tailscale version 2>/dev/null | awk 'NR == 1 { v = $0 } END { print v }'
}

# ── Install ──────────────────────────────────────────────────────────────────

if ! command -v tailscale >/dev/null 2>&1; then
    [ "$INSTALL" = true ] || die "tailscale is not installed and --no-install was given."
    command -v curl >/dev/null 2>&1 || die "curl is required to install Tailscale."
    info "Installing Tailscale"
    # The vendor's installer, which adds the signed apt/dnf repository and pulls
    # the package from it — the same end state as doing it by hand, and it tracks
    # the repository layout across distributions so this script does not have to.
    # Piping it to a shell is the documented method; the alternative is pinning a
    # repo URL and key here that go stale silently.
    curl -fsSL https://tailscale.com/install.sh | sudo sh
    command -v tailscale >/dev/null 2>&1 || die "Tailscale install did not produce a 'tailscale' binary."
    ok "tailscale $(ts_version)"
else
    ok "tailscale already installed ($(ts_version))"
fi

# ── Join ─────────────────────────────────────────────────────────────────────

# `tailscale status` exits non-zero when the node is logged out or stopped, so it
# doubles as the already-joined test. Re-running then only reconciles the name,
# which matters because auth keys are usually single-use: consuming one on every
# run of an idempotent script would fail the second time for no reason.
if sudo tailscale status >/dev/null 2>&1; then
    ok "This host is already on a tailnet"
    # Set the name unconditionally rather than reading the current one first and
    # comparing. `set` is idempotent, and the alternative means parsing
    # `status --json` for a HostName whose position depends on how the output
    # happens to be laid out — which quietly resolves to the wrong node's name,
    # or to the string "HostName" itself, the moment that layout differs.
    info "Reconciling the node name as '$HOSTNAME_ARG'"
    sudo tailscale set --hostname="$HOSTNAME_ARG"
else
    [ -n "$AUTHKEY" ] || die "No auth key. Pass --authkey <tskey-...> or set TS_AUTHKEY. \
Generate one at https://login.tailscale.com/admin/settings/keys"

    info "Joining the tailnet as '$HOSTNAME_ARG'"
    # --accept-dns gives MagicDNS, so an evidence host can be named rather than
    # addressed by its 100.x address, which changes if the node is recreated.
    #
    # --stateful-filtering=false is the load-bearing one. With stateful filtering
    # on, the node drops inbound packets that are not replies to traffic it sent
    # itself — and a container's push to the tailnet is NATed by the host, so the
    # reply arrives looking unsolicited and is discarded. Everything appears to
    # work from the host and nothing works from a container: promtail retries
    # forever, the evidence host stays silent, and the absence rule fires.
    #
    # --ssh=false keeps Tailscale SSH off. Joining a tailnet should not quietly
    # add a second remote-access path to a host whose sshd is already governed.
    TS_ARGS=(--authkey="$AUTHKEY" --hostname="$HOSTNAME_ARG" --accept-dns=true --ssh=false)
    # Ask whether the flag exists rather than trying it and retrying on failure.
    # A failed `up` says nothing about why: an unknown flag and a spent auth key
    # look identical from here, and the retry would run a second `up` with a key
    # the first may already have consumed — two confusing errors instead of one
    # real one. Captured into a variable because `--help` exits non-zero on some
    # versions and `grep -q` would close the pipe early under `pipefail`.
    UP_HELP="$(tailscale up --help 2>&1 || true)"
    case "$UP_HELP" in
        *--stateful-filtering*) TS_ARGS+=(--stateful-filtering=false) ;;
        *)
            warn "This Tailscale has no --stateful-filtering flag."
            warn "Containers on this host may not be able to reach the tailnet."
            ;;
    esac
    sudo tailscale up "${TS_ARGS[@]}"
fi

TS_IP="$(sudo tailscale ip -4 2>/dev/null | awk 'NR == 1 { v = $0 } END { print v }' || true)"
ok "Node '$HOSTNAME_ARG'${TS_IP:+ at $TS_IP}"

echo
echo "Check the tailnet:"
echo "  sudo tailscale status"
echo
echo "Containers reach tailnet names through the host's resolver. Containers that"
echo "were already running when this script ran keep their old DNS, so restart the"
echo "stack before expecting them to resolve a MagicDNS name."
