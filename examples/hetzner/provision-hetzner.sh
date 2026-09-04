#!/usr/bin/env bash
# ============================================================================
# provision-hetzner.sh — wait for a small Hetzner instance to come into stock
# in an EEA location, then create it with a firewall already attached.
#
# Optional and provider-specific: the evidence host does not need Hetzner, and
# nothing else here depends on this script. It exists because the machine is
# small enough to be out of stock exactly when you want one, and because
# creating it correctly has two properties worth encoding rather than
# remembering — the location must be inside the EEA, and the public SSH port
# must never be open even briefly.
#
# Adapted from a standalone availability watcher. The differences from that
# original are the two above plus an API-error check on the poll.
#
# Sizing: the stack idles. Loki receives only the epicurrents.security stream
# (a few events a day plus one heartbeat every five minutes), Caddy and
# Alertmanager are near-static, and `borg serve` uses roughly 250 MB of RAM per
# TB of repository. 4 GB is comfortable; 2 GB works. Disk is the part that
# grows, which is the argument for putting the Borg repository on a Volume
# rather than buying a larger server.
#
# Requires: curl, jq
#
# Provisions any host, not only the evidence one — pass a different config file
# for a differently-sized machine (see ENVFILE selection below).
#
# Env (or a gitignored *.env beside this script, watch.env by default):
#   HCLOUD_TOKEN   required   read+write API token
#   SSH_KEY        required for AUTO_CREATE: names or ids of keys already in
#                  the project, separated by spaces or commas. PASS EVERY KEY
#                  YOU WILL EVER WANT. Hetzner injects SSH keys at creation
#                  only — there is no API call to add one to a running server,
#                  so afterwards it means editing authorized_keys over a
#                  session you still have, and losing every key means rescue
#                  mode. At minimum: your working key, plus a recovery key
#                  kept somewhere else. Add a second administrator now if one
#                  is ever likely; retrofitting is the painful path.
#   FIREWALL       required for AUTO_CREATE: name or id of a Hetzner firewall.
#                  See "The firewall is not optional" below.
#   TYPES          default "cax11 cx23"   cheapest first; cax11 is ARM64 and
#                  every image in the stack publishes arm64, cx23 is the x86
#                  equivalent. Hetzner renames its lines between generations,
#                  so check the defaults still exist before trusting them —
#                  the startup check below reports what resolved
#   LOCATIONS      default "hel1 fsn1 nbg1"     all EEA; see the region guard
#   INTERVAL       default 60                   seconds between polls
#   NTFY_TOPIC     optional   push to https://ntfy.sh/<topic>
#   AUTO_CREATE    default 0  set 1 to create on first availability
#   SERVER_NAME    default epicurrents-evidence
#   IMAGE          default ubuntu-24.04
#   VOLUME_ID      optional   existing Volume to attach and automount.
#                  Resume only: a Volume cannot exist before its first server,
#                  so leave it unset on the first run and create the Volume
#                  afterwards for the Borg repository.
#   ALLOW_NON_EEA  default 0  see the region guard; setting it is a decision
#                  about the privacy notice, not a convenience
#
# The firewall is not optional. A server created without one answers SSH from
# the whole internet from the moment it boots, and this host exists to be the
# thing an attacker cannot reach. Create a firewall in the Hetzner console that
# permits inbound SSH from your own address only (or nothing at all, if you
# bootstrap over the console), attach it here at creation time, and open the
# tailnet afterwards. Attaching it after creation leaves a window; the whole
# point of passing it in the create call is that there isn't one.
# ============================================================================
set -euo pipefail
API=https://api.hetzner.cloud/v1

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# One script, several machines. The evidence host and the application host want
# different sizes, firewalls and names, and sourcing with `set -a` overrides
# whatever was exported on the command line — so the config has to be selected
# by file rather than by variable.
#
#   ./provision-hetzner.sh                 # watch.env beside this script
#   ./provision-hetzner.sh app-host.env    # a different machine
ENVFILE="${1:-${WATCH_ENV:-$SCRIPT_DIR/watch.env}}"
case "$ENVFILE" in
    /*) ;;
    *) ENVFILE="$SCRIPT_DIR/$ENVFILE" ;;
esac
if [ -n "${1:-}" ] && [ ! -f "$ENVFILE" ]; then
    echo "refusing: no such config file: $ENVFILE" >&2
    exit 1
fi
if [ -f "$ENVFILE" ]; then
    set -a
    # shellcheck source=/dev/null
    . "$ENVFILE"
    set +a
    echo "(loaded $ENVFILE)"
fi

: "${HCLOUD_TOKEN:?set HCLOUD_TOKEN (env var or examples/hetzner/watch.env)}"
TYPES="${TYPES:-cax11 cx23}"
LOCATIONS="${LOCATIONS:-hel1 fsn1 nbg1}"
# Accept commas as well as spaces in the list-valued settings. A config file is
# sourced, so `TYPES=cx33 cpx31` is the shell's "set a variable for one command"
# syntax: it assigns cx33 and then tries to run cpx31. Quoting is the fix and
# the examples quote, but commas cannot trigger that parse at all, so they are
# the safer form to hand someone.
TYPES="${TYPES//,/ }"
LOCATIONS="${LOCATIONS//,/ }"
INTERVAL="${INTERVAL:-60}"
AUTO_CREATE="${AUTO_CREATE:-0}"
ALLOW_NON_EEA="${ALLOW_NON_EEA:-0}"

command -v jq >/dev/null 2>&1 || {
    echo "jq required (apt install -y jq)" >&2
    exit 1
}

# Region guard. The evidence host holds security logs carrying IP addresses and
# actor ids, and backup archives of a system processing personal data. An EEA
# location is what keeps the privacy notice's international-transfer answer
# "none"; anywhere else turns it into a question needing a transfer mechanism
# and a changed notice. Fail closed, because the failure is silent otherwise:
# a server in Ashburn works exactly as well as one in Helsinki.
EEA_LOCATIONS="hel1 fsn1 nbg1"
for loc in $LOCATIONS; do
    case " $EEA_LOCATIONS " in
        *" $loc "*) continue ;;
    esac
    if [ "$ALLOW_NON_EEA" != "1" ]; then
        echo "refusing: '$loc' is not an EEA location (known EEA: $EEA_LOCATIONS)." >&2
        echo "This host stores personal data. Placing it outside the EEA requires a" >&2
        echo "transfer mechanism in the privacy notice. Set ALLOW_NON_EEA=1 only if" >&2
        echo "that has actually been arranged." >&2
        exit 1
    fi
    echo "WARNING: '$loc' is outside the EEA and ALLOW_NON_EEA=1 was set." >&2
done

auth=(-H "Authorization: Bearer $HCLOUD_TOKEN")

notify() {
    echo "$(date '+%F %T')  $1"
    if [ -n "${NTFY_TOPIC:-}" ]; then
        curl -s -H "Title: Evidence host watch" -d "$1" \
            "https://ntfy.sh/${NTFY_TOPIC}" >/dev/null 2>&1 || true
    fi
}

# Build the request with jq rather than by concatenating quoted strings: the
# key list is user-supplied and multi-valued, and hand-built JSON is where a
# stray space becomes a malformed request at three in the morning.
create_server() {
    local type="$1" loc="$2"
    jq -n \
        --arg name "${SERVER_NAME:-epicurrents-evidence}" \
        --arg type "$type" \
        --arg loc "$loc" \
        --arg image "${IMAGE:-ubuntu-24.04}" \
        --arg keys "$SSH_KEY" \
        --argjson fw "$FIREWALL_ID" \
        --arg vol "${VOLUME_ID:-}" \
        '{
            name: $name,
            server_type: $type,
            location: $loc,
            image: $image,
            ssh_keys: ($keys | split(",") | map(split(" ")) | flatten
                       | map(select(length > 0))
                       | map(if test("^[0-9]+$") then tonumber else . end)),
            firewalls: [{firewall: $fw}]
        }
        + (if $vol == "" then {} else {volumes: [($vol | tonumber)], automount: true} end)' \
    | curl -s -X POST "${auth[@]}" -H "Content-Type: application/json" \
        -d @- "$API/servers"
}

types_json=$(curl -s "${auth[@]}" "$API/server_types?per_page=100")
if ! jq -e '.server_types' >/dev/null 2>&1 <<<"$types_json"; then
    echo "server_types lookup failed: $(jq -c '.error // .' <<<"$types_json")" >&2
    exit 1
fi

# Resolve every requested type up front and say which ones exist. Hetzner
# renames its lines between generations, and the availability query below
# simply skips a name it cannot resolve — so a stale TYPES list produces a
# watcher that polls indefinitely, reports nothing, and looks healthy. Warn
# per unknown name, abort if none survive.
resolved=""
for type in $TYPES; do
    if jq -e --arg n "$type" '.server_types[]|select(.name==$n)' >/dev/null 2>&1 <<<"$types_json"; then
        resolved="$resolved $type"
    else
        echo "WARNING: server type '$type' does not exist — ignoring it." >&2
    fi
done
if [ -z "${resolved# }" ]; then
    echo "refusing: none of the requested types exist. Current names:" >&2
    jq -r '[.server_types[]|select(.deprecation==null)|.name]|sort|join(" ")' <<<"$types_json" >&2
    exit 1
fi
TYPES="${resolved# }"

# Everything AUTO_CREATE needs is checked now, not at the moment stock
# appears. A wrong key name or a missing firewall surfaces as a failed create
# — the script keeps watching, so it is not silent, but it burns the
# availability window it waited for, and stock for these types does not come
# back on demand.
FIREWALL_ID=""
if [ "$AUTO_CREATE" = "1" ]; then
    : "${SSH_KEY:?set SSH_KEY for AUTO_CREATE — see the header, and pass every key you will want}"
    : "${FIREWALL:?set FIREWALL for AUTO_CREATE — see the header}"

    keys_json=$(curl -s "${auth[@]}" "$API/ssh_keys?per_page=100")
    if ! jq -e '.ssh_keys' >/dev/null 2>&1 <<<"$keys_json"; then
        echo "ssh_keys lookup failed: $(jq -c '.error // .' <<<"$keys_json")" >&2
        exit 1
    fi
    key_count=0
    for key in ${SSH_KEY//,/ }; do
        if jq -e --arg k "$key" \
            '.ssh_keys[]|select((.name==$k) or (.id|tostring==$k))' >/dev/null 2>&1 <<<"$keys_json"; then
            key_count=$((key_count + 1))
        else
            echo "refusing: SSH key '$key' is not in this project. Known keys:" >&2
            jq -r '[.ssh_keys[].name]|join(" ")' <<<"$keys_json" >&2
            exit 1
        fi
    done
    if [ "$key_count" -lt 2 ]; then
        echo "WARNING: only one SSH key will be injected. Hetzner adds keys at" >&2
        echo "creation only, so a lost key means rescue mode. Consider passing a" >&2
        echo "recovery key kept elsewhere: SSH_KEY=\"primary recovery\"" >&2
    fi

    fw_json=$(curl -s "${auth[@]}" "$API/firewalls?per_page=100")
    FIREWALL_ID=$(jq -r --arg f "$FIREWALL" \
        'first(.firewalls[]?|select((.name==$f) or (.id|tostring==$f))|.id) // empty' <<<"$fw_json")
    if [ -z "$FIREWALL_ID" ]; then
        echo "refusing: firewall '$FIREWALL' not found. Known firewalls:" >&2
        jq -r '[.firewalls[]?.name]|join(" ")' <<<"$fw_json" >&2
        echo "Create one first — inbound TCP 22 from your address only, outbound all." >&2
        exit 1
    fi
    echo "AUTO_CREATE ready: $key_count SSH key(s), firewall '$FIREWALL' (id $FIREWALL_ID)"
fi

# Announce each distinct situation once per run rather than once per poll. A
# watch-only run sits on available stock indefinitely, and a blocked create is
# retried indefinitely by design — both would otherwise send a notification
# every INTERVAL seconds, which trains you to ignore the channel that is
# supposed to tell you the machine is ready.
announced=""
announce_once() {
    case " $announced " in
        *" $1 "*) return ;;
    esac
    announced="$announced $1"
    notify "$2"
}

echo "Watching [$TYPES] in [$LOCATIONS] every ${INTERVAL}s  (AUTO_CREATE=$AUTO_CREATE)"
while true; do
    dc_json=$(curl -s "${auth[@]}" "$API/datacenters?per_page=50")
    # An expired or wrong-scope token returns an error object, from which the
    # availability query below extracts zero hits — indistinguishable from
    # "out of stock", so the watcher would poll forever against a dead token.
    if ! jq -e '.datacenters' >/dev/null 2>&1 <<<"$dc_json"; then
        notify "poll failed: $(jq -c '.error // .' <<<"$dc_json")"
        sleep "$INTERVAL"
        continue
    fi
    for loc in $LOCATIONS; do
        for type in $TYPES; do
            id=$(jq -r --arg n "$type" \
                '.server_types[]|select(.name==$n)|.id // empty' <<<"$types_json")
            [ -z "$id" ] && continue
            hit=$(jq --arg loc "$loc" --argjson id "$id" \
                '[.datacenters[]
                  |select(.location.name==$loc)
                  |.server_types.available[]
                  |select(.==$id)]|length' <<<"$dc_json")
            if [ "${hit:-0}" -gt 0 ]; then
                announce_once "avail:$type@$loc" "AVAILABLE: $type in $loc"
                if [ "$AUTO_CREATE" = "1" ]; then
                    resp=$(create_server "$type" "$loc")
                    if jq -e '.server.id' >/dev/null 2>&1 <<<"$resp"; then
                        sid=$(jq -r '.server.id' <<<"$resp")
                        ip=$(jq -r '.server.public_net.ipv4.ip // "?"' <<<"$resp")
                        notify "CREATED $type/$loc  id=$sid  ip=$ip"
                        echo "Next: join it to the tailnet, then narrow the firewall" \
                            "to tailnet-only. See README.md -> Provisioning."
                        exit 0
                    else
                        # A per-location account restriction looks like this.
                        # Keep watching so it fires when the block lifts.
                        announce_once "blocked:$type@$loc" \
                            "create blocked (still retrying): $(jq -c '.error // .' <<<"$resp")"
                    fi
                fi
            fi
        done
    done
    sleep "$INTERVAL"
done
