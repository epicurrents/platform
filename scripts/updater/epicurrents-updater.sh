#!/usr/bin/env bash
# epicurrents-updater.sh — the host agent of the remote-maintenance feature.
#
# Runs as root from a systemd timer, once a minute, outside every container.
# It reads the requests the web tier wrote into the deployment's ./update/
# spool, verifies the package each one names, and drives the deployment's own
# update.sh — which is what does the update, the snapshot and the rollback.
# The web application never executes anything on the host; this agent is the
# only thing that does, and nothing it executes comes from a request. A request
# is an operation key plus a package hash; the operation is one this script
# allowlists, the package is copied out of the spool and verified as a copy,
# and update.sh is run from this agent's own root-owned copy.
#
# One job at a time. A tick either refuses or accepts a request, runs the
# update to its verification window, acts on a confirmation or a rollback
# marker, or rolls a job back whose window closed. Every state it reaches is
# written to jobs/<id>.status.json in the spool, where the platform projects
# it onto the job row, and to the system journal, which survives the database
# restore a rollback performs.
#
# Three operations. platform.update applies a package as above; a release
# that applied no migration is rolled back with --code-only, which keeps the
# database and everything written since. platform.backup takes a snapshot of
# code, database and .env without stopping anything. platform.rollback
# restores a named snapshot, the database too unless the request says to keep
# it. A release may announce the key that signs the next one; the agent
# installs that successor beside the current key and promotes it the first
# time a package verifies with it. With SELF_UPDATE=1 the agent replaces
# itself with the copy a verified package ships.
#
# Configuration: /etc/epicurrents-updater/config (see install-updater.sh).
# The full protocol is in docs/engineering-notes/remote-maintenance-design.md.
#
set -euo pipefail

AGENT_VERSION=2
PROTOCOL=1
OPERATION_UPDATE="platform.update"
OPERATION_BACKUP="platform.backup"
OPERATION_ROLLBACK="platform.rollback"
# update.sh from this version on takes --code-only and several --release-key.
UPDATER_SCRIPT_CODE_ONLY=3

CONFIG_DIR="${EPICURRENTS_UPDATER_CONFIG_DIR:-/etc/epicurrents-updater}"
LIB_DIR="${EPICURRENTS_UPDATER_LIB_DIR:-/usr/local/lib/epicurrents-updater}"
STATE_DIR="${EPICURRENTS_UPDATER_STATE_DIR:-/var/lib/epicurrents-updater}"
RUN_LOCK="${EPICURRENTS_UPDATER_LOCK_FILE:-/run/lock/epicurrents-updater.lock}"
BOOT_ID_FILE="${EPICURRENTS_UPDATER_BOOT_ID_FILE:-/proc/sys/kernel/random/boot_id}"
# The log of a job is head-truncated at this many bytes: the first lines say
# what was attempted, the last say how it ended, and the middle is the build.
LOG_CAP="${EPICURRENTS_UPDATER_LOG_CAP:-8388608}"
# Seconds between polls of the readiness probe and the worker drain. Tests set
# it to 0.
POLL_SECONDS="${EPICURRENTS_UPDATER_POLL_SECONDS:-5}"

UPDATE_SH="$LIB_DIR/update.sh"
AGENT_SELF="$LIB_DIR/epicurrents-updater.sh"
RELEASE_KEY="$CONFIG_DIR/release.pub"
# The successor a release announced, trusted beside the current key until a
# package signed with it verifies, at which point it becomes the current key.
RELEASE_KEY_NEXT="$CONFIG_DIR/release.pub.next"
# A snapshot name as update.sh writes them: a label, a UTC date and a time.
SNAPSHOT_NAME_RE='^[A-Za-z0-9][A-Za-z0-9_-]*-[0-9]{8}-[0-9]{6}$'

# Defaults the config file may override.
DEPLOY_ROOT=""
ENABLED=0
ALLOW_CHECKOUT=0
SELF_UPDATE=0
MIN_FREE_BYTES=1073741824
HEALTH_TIMEOUT=300
DRAIN_TIMEOUT=60
VERIFY_WINDOW_MINUTES=30

# ── Output ────────────────────────────────────────────────────────────────────

log() {
    # To the journal when running under systemd, to stderr otherwise; a
    # `logger` line as well so the timeline exists somewhere the database
    # restore cannot reach.
    printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2
    if command -v logger >/dev/null 2>&1; then
        logger -t epicurrents-updater -- "$*" 2>/dev/null || true
    fi
}

die() {
    log "ERROR: $*"
    exit 1
}

# ── Configuration ─────────────────────────────────────────────────────────────

load_config() {
    local config="$CONFIG_DIR/config"
    [ -f "$config" ] || return 1
    # The file is root's own, written by the installer; sourcing it is the
    # same trust /etc/default/* gets. A file anyone else can write is refused,
    # since a line in it becomes code here.
    if [ "$(id -u)" = 0 ]; then
        local mode owner
        mode="$(stat -c %a "$config" 2>/dev/null || stat -f %Lp "$config" 2>/dev/null || echo 600)"
        owner="$(stat -c %u "$config" 2>/dev/null || stat -f %u "$config" 2>/dev/null || echo 0)"
        if [ "$owner" != 0 ] || [ "$(( 8#$mode & 8#022 ))" -ne 0 ]; then
            die "$config must be owned by root and writable by nobody else (owner $owner, mode $mode)."
        fi
    fi
    # shellcheck source=/dev/null
    . "$config"
    [ -n "$DEPLOY_ROOT" ] || die "$config names no DEPLOY_ROOT."
    [ -d "$DEPLOY_ROOT" ] || die "DEPLOY_ROOT $DEPLOY_ROOT does not exist."
    [ -f "$DEPLOY_ROOT/docker-compose.yml" ] || die "DEPLOY_ROOT $DEPLOY_ROOT is not a deployment (no docker-compose.yml)."
    DEPLOY_ROOT="$(cd "$DEPLOY_ROOT" && pwd)"
    return 0
}

SPOOL=""
JOBS=""
PACKAGES=""
FLAG=""
ACTIVE_LOCK=""
TREE_OWNER="1000:1000"

locate_spool() {
    SPOOL="$DEPLOY_ROOT/update"
    JOBS="$SPOOL/jobs"
    PACKAGES="$SPOOL/packages"
    FLAG="$SPOOL/maintenance.json"
    ACTIVE_LOCK="$SPOOL/lock"
    # Whoever owns the deployment tree is who the containers run as; every file
    # this root process writes into the spool is handed to them, or the web
    # tier could never read its own job's status.
    TREE_OWNER="$(stat -c %u:%g "$DEPLOY_ROOT" 2>/dev/null || echo 1000:1000)"
    local dir
    for dir in "$SPOOL" "$JOBS" "$PACKAGES"; do
        if [ ! -d "$dir" ]; then
            mkdir -p "$dir"
            chown "$TREE_OWNER" "$dir" 2>/dev/null || true
        fi
    done
}

# ── Runtime detection, as update.sh settles it ────────────────────────────────

CONTAINER_RUNTIME=""
COMPOSE=()

detect_runtime() {
    if command -v docker >/dev/null 2>&1 && ! docker --version 2>&1 | grep -qi podman; then
        CONTAINER_RUNTIME=docker
        COMPOSE=(docker compose -f docker-compose.yml -f docker-compose.prod.yml)
    elif command -v podman >/dev/null 2>&1; then
        CONTAINER_RUNTIME=podman
        if [ "$(id -u)" = 0 ]; then
            COMPOSE=(podman compose -f docker-compose.yml -f docker-compose.prod.yml)
        else
            COMPOSE=(sudo -E podman compose -f docker-compose.yml -f docker-compose.prod.yml)
        fi
    else
        return 1
    fi
    if [ -f "$DEPLOY_ROOT/.env" ] && grep -qE '^PROXY_DOMAIN=[^[:space:]]' "$DEPLOY_ROOT/.env"; then
        COMPOSE+=(-f docker-compose.proxy.yml)
    fi
    return 0
}

env_value() {
    # $1 = key in the deployment's .env. Quotes and surrounding spaces dropped.
    grep -E "^$1=" "$DEPLOY_ROOT/.env" 2>/dev/null | sed -n '1{s/^[^=]*=//; s/^[[:space:]]*//; s/[[:space:]]*$//; s/^"\(.*\)"$/\1/; s/^'"'"'\(.*\)'"'"'$/\1/; p;}'
}

# ── JSON, through python3 ─────────────────────────────────────────────────────
# A minimal host ships python3 and not jq. Values pass as arguments, never
# through a shell string, and nothing read from a file is evaluated.

json_get() {
    # $1 = file, $2 = key, $3 = optional sub-key of an object value. Prints the
    # value as a string; nothing for null, absent or unreadable.
    python3 - "$1" "$2" "${3:-}" <<'PY' 2>/dev/null || true
import json, sys
path, key, sub = sys.argv[1:4]
try:
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
except (OSError, ValueError):
    sys.exit(0)
if not isinstance(data, dict):
    sys.exit(0)
value = data.get(key)
if sub:
    value = value.get(sub) if isinstance(value, dict) else None
if value is None or isinstance(value, (dict, list)):
    sys.exit(0)
if isinstance(value, bool):
    print("true" if value else "false")
else:
    print(value)
PY
}

json_write() {
    # $1 = target file, then key=value pairs. A value of @null, @true, @false,
    # @int:N, @list:a,b or @json:<document> is typed; everything else is a
    # string. Written to a temporary file beside the target, handed to the
    # tree owner, then renamed into place, so a reader never sees a partial
    # file and the deployment account can always read what root wrote.
    local target="$1" tmp
    shift
    tmp="$target.tmp"
    python3 - "$tmp" "$@" <<'PY'
import json, sys
out = sys.argv[1]
data = {}
for pair in sys.argv[2:]:
    key, _, raw = pair.partition("=")
    if raw == "@null":
        value = None
    elif raw == "@true":
        value = True
    elif raw == "@false":
        value = False
    elif raw.startswith("@int:"):
        value = int(raw[5:])
    elif raw.startswith("@list:"):
        value = [item for item in raw[6:].split(",") if item]
    elif raw.startswith("@json:"):
        try:
            value = json.loads(raw[6:])
        except ValueError:
            value = None
    else:
        value = raw
    data[key] = value
with open(out, "w", encoding="utf-8") as fh:
    json.dump(data, fh, sort_keys=True, indent=2)
    fh.write("\n")
PY
    chown "$TREE_OWNER" "$tmp" 2>/dev/null || true
    mv -f "$tmp" "$target"
}

now_iso() {
    date -u +%Y-%m-%dT%H:%M:%SZ
}

deadline_iso() {
    # $1 = minutes from now.
    python3 -c 'import datetime, sys; print((datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=int(sys.argv[1]))).strftime("%Y-%m-%dT%H:%M:%SZ"))' "$1"
}

is_past() {
    # $1 = ISO-8601 timestamp. 0 when it lies in the past.
    python3 -c 'import datetime, sys
value = sys.argv[1].replace("Z", "+00:00")
try:
    when = datetime.datetime.fromisoformat(value)
except ValueError:
    sys.exit(1)
if when.tzinfo is None:
    when = when.replace(tzinfo=datetime.timezone.utc)
sys.exit(0 if when <= datetime.datetime.now(datetime.timezone.utc) else 1)' "$1"
}

sha256_of() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | awk '{print $1}'
    else
        shasum -a 256 "$1" | awk '{print $1}'
    fi
}

key_id_of() {
    # $1 = a PEM public key. The identifier release_sign.py prints: the leading
    # hex of the SHA-256 of the raw 32-byte key, which ends the DER encoding.
    # Nothing when the file is absent or the host cannot decode it.
    local tmp id=""
    [ -f "$1" ] || return 0
    command -v openssl >/dev/null 2>&1 || return 0
    tmp="$(mktemp)"
    if openssl pkey -pubin -in "$1" -outform DER -out "$tmp" 2>/dev/null && [ "$(wc -c < "$tmp" | tr -d ' ')" -ge 32 ]; then
        id="$(tail -c 32 "$tmp" | sha256_of /dev/stdin 2>/dev/null | cut -c1-16 || true)"
    fi
    rm -f "$tmp"
    printf '%s' "$id"
}

updater_script_version() {
    # The UPDATER_SCRIPT_VERSION of the agent's own copy of update.sh; 0 when
    # it declares none.
    local version
    version="$(sed -n '/^UPDATER_SCRIPT_VERSION=/{s/^UPDATER_SCRIPT_VERSION=\([0-9]*\)$/\1/p;q;}' "$UPDATE_SH" 2>/dev/null || true)"
    [[ "$version" =~ ^[0-9]+$ ]] || version=0
    printf '%s' "$version"
}

updater_supports_code_only() {
    [ "$(updater_script_version)" -ge "$UPDATER_SCRIPT_CODE_ONLY" ]
}

KEY_ARGS=()

set_key_args() {
    # The --release-key arguments update.sh gets: the current key and, while a
    # successor is installed, that one too.
    KEY_ARGS=(--release-key "$RELEASE_KEY")
    [ ! -f "$RELEASE_KEY_NEXT" ] || KEY_ARGS+=(--release-key "$RELEASE_KEY_NEXT")
}

snapshots_json() {
    # The snapshots under the deployment's backups/, newest first, as a JSON
    # list for the heartbeat: name, when it was taken, the version its code
    # carries, whether it holds a code archive, and what the update after it
    # did to the schema. Read from each MANIFEST; nothing there is executed.
    python3 - "$DEPLOY_ROOT/backups" <<'PY' 2>/dev/null || printf '[]'
import json, os, re, sys
root = sys.argv[1]
pattern = re.compile(r"^([A-Za-z0-9][A-Za-z0-9_-]*)-(\d{8})-(\d{6})$")
rows = []
try:
    names = os.listdir(root)
except OSError:
    names = []
for name in names:
    match = pattern.match(name)
    path = os.path.join(root, name)
    if not match or not os.path.isdir(path):
        continue
    if not (os.path.isfile(os.path.join(path, "db.sql.gz")) and os.path.isfile(os.path.join(path, ".env"))):
        continue
    manifest = {}
    try:
        with open(os.path.join(path, "MANIFEST"), encoding="utf-8") as fh:
            for line in fh:
                key, sep, value = line.rstrip("\n").partition("=")
                if sep:
                    manifest[key] = value
    except OSError:
        pass
    date, time = match.group(2), match.group(3)
    migrations = manifest.get("migrations")
    rows.append({
        "name": name,
        "taken_at": f"{date[:4]}-{date[4:6]}-{date[6:]}T{time[:2]}:{time[2:4]}:{time[4:]}Z",
        "version": manifest.get("version") or None,
        "code": os.path.isfile(os.path.join(path, "code.tar.gz")),
        "migrations": migrations if migrations in ("none", "applied") else None,
    })
rows.sort(key=lambda row: row["taken_at"], reverse=True)
print(json.dumps(rows[:50]))
PY
}

# ── The heartbeat ─────────────────────────────────────────────────────────────

write_heartbeat() {
    local enabled=@false self_update=@false key_id next_key_id
    [ "$ENABLED" = 1 ] && enabled=@true
    [ "$SELF_UPDATE" = 1 ] && self_update=@true
    key_id="$(key_id_of "$RELEASE_KEY")"
    next_key_id="$(key_id_of "$RELEASE_KEY_NEXT")"
    json_write "$SPOOL/agent.json" \
        "protocol=@int:$PROTOCOL" \
        "version=$AGENT_VERSION" \
        "enabled=$enabled" \
        "self_update=$self_update" \
        "runtime=${CONTAINER_RUNTIME:-@null}" \
        "last_run=$(now_iso)" \
        "capabilities=@list:$OPERATION_UPDATE,$OPERATION_BACKUP,$OPERATION_ROLLBACK" \
        "updater_script=$(updater_script_version)" \
        "key_id=$(opt "$key_id")" \
        "next_key_id=$(opt "$next_key_id")" \
        "snapshots=@json:$(snapshots_json)"
}

# ── Job state ─────────────────────────────────────────────────────────────────
# One job's fields live in these globals while it is being worked on; every
# status write carries the full set, because the platform blanks a field the
# file does not carry.

JOB_ID=""
JOB_OPERATION=""
JOB_STATE=""
JOB_REASON=""
JOB_STEP=""
JOB_STARTED_AT=""
JOB_FINISHED_AT=""
JOB_DEADLINE=""
JOB_SNAPSHOT=""
JOB_POST_SNAPSHOT=""
JOB_INSTALLED_BEFORE=""
JOB_TARGET=""
JOB_RUNNING=""
JOB_RETRIES=0
JOB_WINDOW=""
# What the update did to the schema: none, applied, or empty when unknown.
JOB_MIGRATIONS=""

job_log() {
    printf '%s\n' "$JOBS/$JOB_ID.log"
}

job_status() {
    printf '%s\n' "$JOBS/$JOB_ID.status.json"
}

job_note() {
    # A line of the agent's own into the job's log, and into the journal.
    local line="[updater] $*"
    append_log "$line"
    log "job $JOB_ID: $*"
}

append_log() {
    local path
    path="$(job_log)"
    if [ ! -f "$path" ]; then
        : > "$path"
        chown "$TREE_OWNER" "$path" 2>/dev/null || true
    fi
    printf '%s\n' "$*" >> "$path"
}

opt() {
    # A JSON string or null for an empty value.
    if [ -n "$1" ]; then
        printf '%s' "$1"
    else
        printf '@null'
    fi
}

migrations_applied_json() {
    # JOB_MIGRATIONS as the status file spells it: a boolean, or null when
    # the run never got as far as migrating.
    case "$JOB_MIGRATIONS" in
        none) printf '@false' ;;
        applied) printf '@true' ;;
        *) printf '@null' ;;
    esac
}

write_status() {
    # $1 = state, $2 = reason (may be empty). Stamps updated_at from this
    # clock; the platform applies a status only when it is newer than the one
    # it has, so the stamp must move on every write.
    JOB_STATE="$1"
    JOB_REASON="${2:-}"
    json_write "$(job_status)" \
        "protocol=@int:$PROTOCOL" \
        "job_id=$JOB_ID" \
        "operation=$JOB_OPERATION" \
        "state=$JOB_STATE" \
        "reason=$JOB_REASON" \
        "step=$JOB_STEP" \
        "updated_at=$(now_iso)" \
        "started_at=$(opt "$JOB_STARTED_AT")" \
        "finished_at=$(opt "$JOB_FINISHED_AT")" \
        "verify_deadline=$(opt "$JOB_DEADLINE")" \
        "snapshot=$JOB_SNAPSHOT" \
        "post_snapshot=$JOB_POST_SNAPSHOT" \
        "migrations_applied=$(migrations_applied_json)" \
        "agent_version=$AGENT_VERSION" \
        "installed_version_before=$JOB_INSTALLED_BEFORE" \
        "target_version=$JOB_TARGET" \
        "running_version=$JOB_RUNNING" \
        "retries=@int:$JOB_RETRIES"
    log "job $JOB_ID: $JOB_STATE${JOB_REASON:+ ($JOB_REASON)}${JOB_STEP:+ step=$JOB_STEP}"
}

load_status() {
    # Read a job's own status file back into the globals, for a job that a
    # previous tick left in flight.
    local path
    path="$(job_status)"
    JOB_OPERATION="$(json_get "$path" operation)"
    # A status file from before operations were recorded is an update's.
    [ -n "$JOB_OPERATION" ] || JOB_OPERATION="$OPERATION_UPDATE"
    JOB_STATE="$(json_get "$path" state)"
    JOB_REASON="$(json_get "$path" reason)"
    JOB_STEP="$(json_get "$path" step)"
    JOB_STARTED_AT="$(json_get "$path" started_at)"
    JOB_FINISHED_AT="$(json_get "$path" finished_at)"
    JOB_DEADLINE="$(json_get "$path" verify_deadline)"
    JOB_SNAPSHOT="$(json_get "$path" snapshot)"
    JOB_POST_SNAPSHOT="$(json_get "$path" post_snapshot)"
    JOB_INSTALLED_BEFORE="$(json_get "$path" installed_version_before)"
    JOB_TARGET="$(json_get "$path" target_version)"
    JOB_RUNNING="$(json_get "$path" running_version)"
    JOB_RETRIES="$(json_get "$path" retries)"
    [[ "$JOB_RETRIES" =~ ^[0-9]+$ ]] || JOB_RETRIES=0
    case "$(json_get "$path" migrations_applied)" in
        false) JOB_MIGRATIONS=none ;;
        true) JOB_MIGRATIONS=applied ;;
        *) JOB_MIGRATIONS="" ;;
    esac
}

reset_job() {
    JOB_ID="$1"
    JOB_OPERATION="" JOB_STATE="" JOB_REASON="" JOB_STEP="" JOB_STARTED_AT="" JOB_FINISHED_AT="" JOB_DEADLINE=""
    JOB_SNAPSHOT="" JOB_POST_SNAPSHOT="" JOB_INSTALLED_BEFORE="" JOB_TARGET="" JOB_RUNNING=""
    JOB_RETRIES=0 JOB_WINDOW="" JOB_MIGRATIONS=""
}

finish() {
    # $1 = terminal state, $2 = reason.
    JOB_FINISHED_AT="$(now_iso)"
    write_status "$1" "${2:-}"
}

# ── The flag and the active-run lock ──────────────────────────────────────────

write_flag() {
    # $1 = phase, $2 = message, $3 = expected_until (may be empty).
    local since
    since="$(json_get "$FLAG" since)"
    [ -n "$since" ] || since="$(now_iso)"
    json_write "$FLAG" \
        "protocol=@int:$PROTOCOL" \
        "phase=$1" \
        "job_id=$JOB_ID" \
        "since=$since" \
        "expected_until=$(opt "${3:-}")" \
        "message=$2"
}

remove_flag() {
    rm -f "$FLAG"
}

boot_id() {
    cat "$BOOT_ID_FILE" 2>/dev/null || echo unknown
}

write_active_lock() {
    # Who is executing which job. Present only while this process is inside an
    # update or a rollback; a lock whose process is gone, or from another boot,
    # is what tells the next tick the previous one died mid-way.
    json_write "$ACTIVE_LOCK" \
        "protocol=@int:$PROTOCOL" \
        "pid=@int:$$" \
        "boot_id=$(boot_id)" \
        "job_id=$JOB_ID" \
        "since=$(now_iso)"
}

remove_active_lock() {
    rm -f "$ACTIVE_LOCK"
}

# ── The stack ─────────────────────────────────────────────────────────────────

compose() {
    (cd "$DEPLOY_ROOT" && "${COMPOSE[@]}" "$@")
}

stop_beat() {
    # The scheduler fires every purge that came due while it was down the
    # moment it starts, and a purge unlinks files a rollback restores the rows
    # for and cannot bring back. Down from acceptance to the terminal state.
    job_note "stopping celery-beat"
    compose stop celery-beat >>"$(job_log)" 2>&1 || job_note "celery-beat did not stop cleanly; continuing"
}

start_beat() {
    job_note "starting celery-beat"
    compose up -d celery-beat >>"$(job_log)" 2>&1 || job_note "celery-beat did not start; start it by hand: ${COMPOSE[*]} up -d celery-beat"
}

drain_worker() {
    # An upload dispatched just before the lock went up may still be moving a
    # file from staging to permanent storage; the dump must not be taken under
    # it. Bounded, because a stuck task is not a reason to never update.
    local waited=0 active
    job_note "draining the worker (up to ${DRAIN_TIMEOUT}s)"
    while [ "$waited" -lt "$DRAIN_TIMEOUT" ]; do
        active="$(compose exec -T celery celery -A epicurrents inspect active -j 2>/dev/null || true)"
        if worker_idle "$active"; then
            job_note "worker idle"
            return 0
        fi
        sleep "$POLL_SECONDS"
        waited=$((waited + POLL_SECONDS))
        [ "$POLL_SECONDS" -gt 0 ] || waited=$DRAIN_TIMEOUT
    done
    job_note "worker still busy after ${DRAIN_TIMEOUT}s; continuing"
    return 0
}

worker_idle() {
    # $1 = the JSON `inspect active -j` printed. Idle when every worker's list
    # is empty, and when the worker did not answer at all (it is down, which
    # is idle enough).
    python3 -c 'import json, sys
raw = sys.argv[1].strip()
start = raw.find("{")
if start < 0:
    sys.exit(0)
try:
    data = json.loads(raw[start:])
except ValueError:
    sys.exit(0)
sys.exit(0 if not any(data.values()) else 1)' "$1"
}

running_version() {
    # The version the web container is serving, read from the one module that
    # carries it. The version endpoint is authenticated by design.
    compose exec -T web python -c 'from epicurrents.version import __version__; print(__version__)' 2>/dev/null | tr -d '[:space:]' || true
}

gate() {
    # $1 = the version that must be serving, or empty to accept whatever
    # serves. 0 when the readiness probe answers 200 within HEALTH_TIMEOUT
    # and the running version is the expected one.
    local port waited=0 code version
    port="$(env_value HOST_PORT)"
    port="${port:-8000}"
    job_note "waiting for the platform to become ready (up to ${HEALTH_TIMEOUT}s)"
    while true; do
        code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "http://localhost:${port}/api/v1/ready" 2>/dev/null || true)"
        if [ "$code" = 200 ]; then
            break
        fi
        if [ "$waited" -ge "$HEALTH_TIMEOUT" ]; then
            job_note "readiness probe did not answer 200 within ${HEALTH_TIMEOUT}s (last answer: ${code:-none})"
            return 1
        fi
        sleep "$POLL_SECONDS"
        waited=$((waited + POLL_SECONDS))
        [ "$POLL_SECONDS" -gt 0 ] || waited=$HEALTH_TIMEOUT
    done
    job_note "readiness probe answered 200"
    version="$(running_version)"
    JOB_RUNNING="$version"
    if [ -z "$version" ]; then
        job_note "could not read the running version from the web container"
        return 1
    fi
    if [ -n "$1" ] && [ "$version" != "$1" ]; then
        job_note "the platform is serving version $version, expected $1"
        return 1
    fi
    job_note "the platform is serving version $version"
    return 0
}

# ── Running update.sh ─────────────────────────────────────────────────────────

FACTS=""

stream_log() {
    # Reads update.sh's output line by line into the job log (head-capped),
    # follows its :: lines, and records the facts the caller needs in $FACTS.
    # Runs in the pipe's subshell, so it writes the status itself on each step
    # and leaves the rest to the facts file.
    local line size=0 capped=false key value
    while IFS= read -r line; do
        if [ "$capped" = false ]; then
            size=$((size + ${#line} + 1))
            if [ "$size" -gt "$LOG_CAP" ]; then
                capped=true
                append_log "[updater] log truncated at $LOG_CAP bytes; the rest of the output is in the journal"
            else
                append_log "$line"
            fi
        fi
        case "$line" in
            ::*)
                key="${line#::}"
                value="${key#*=}"
                key="${key%%=*}"
                case "$key" in
                    step)
                        JOB_STEP="$value"
                        printf '%s=%s\n' "$key" "$value" >> "$FACTS"
                        if [ -n "$JOB_STATE" ]; then
                            write_status "$JOB_STATE" "$JOB_REASON"
                        fi
                        ;;
                    snapshot|health|failed|refused|done|installed|version|migrations|key|restored)
                        printf '%s=%s\n' "$key" "$value" >> "$FACTS"
                        ;;
                esac
                ;;
        esac
    done
}

fact() {
    # $1 = key. The last value recorded for it, or nothing.
    sed -n "s/^$1=//p" "$FACTS" 2>/dev/null | tail -1
}

run_update_sh() {
    # $@ = arguments. Runs the agent's own copy against the deployment root,
    # streaming into the job log. Returns update.sh's exit status.
    local rc
    FACTS="$(mktemp)"
    append_log "[updater] $(now_iso) update.sh $*"
    set +e
    "$UPDATE_SH" --root "$DEPLOY_ROOT" "$@" 2>&1 | stream_log
    rc=${PIPESTATUS[0]}
    set -e
    # The steps were followed in the pipe's subshell; the last one is what
    # the job's status keeps.
    local last
    last="$(fact step)"
    [ -z "$last" ] || JOB_STEP="$last"
    return "$rc"
}

# ── Accepting a request ───────────────────────────────────────────────────────

refuse() {
    # $1 = reason token, $2 = message. A refused request changes nothing and
    # leaves no work copy.
    job_note "refused: $2"
    rm -rf "${STATE_DIR:?}/work/$JOB_ID"
    finish failed "refused_$1"
}

free_bytes() {
    df -Pk "$DEPLOY_ROOT" | awk 'NR==2{print $4 * 1024}'
}

accept_or_refuse() {
    # $1 = the request file. Every check a request must pass before anything
    # is touched, in the order that matters: the checks every operation
    # shares, then the operation's own.
    local request="$1" protocol operation
    protocol="$(json_get "$request" protocol)"
    operation="$(json_get "$request" operation)"
    case "$operation" in
        "$OPERATION_UPDATE"|"$OPERATION_BACKUP"|"$OPERATION_ROLLBACK") JOB_OPERATION="$operation" ;;
        *) JOB_OPERATION="${operation:-unknown}" ;;
    esac

    if [ "$ENABLED" != 1 ]; then
        refuse disabled "the agent is installed but not enabled (ENABLED=1 in $CONFIG_DIR/config)"
        return
    fi
    if [ "$protocol" != "$PROTOCOL" ]; then
        refuse protocol "the request carries protocol '${protocol:-none}'; this agent speaks $PROTOCOL"
        return
    fi
    case "$operation" in
        "$OPERATION_UPDATE"|"$OPERATION_BACKUP"|"$OPERATION_ROLLBACK") ;;
        *)
            refuse operation "operation '${operation:-none}' is not one this agent carries out"
            return
            ;;
    esac
    if [ -e "$DEPLOY_ROOT/.git" ] && [ "$ALLOW_CHECKOUT" != 1 ]; then
        refuse checkout "$DEPLOY_ROOT is a git checkout; remote operations are for distribution deployments (ALLOW_CHECKOUT=1 overrides)"
        return
    fi
    case "$operation" in
        "$OPERATION_BACKUP") accept_backup ;;
        "$OPERATION_ROLLBACK") accept_rollback "$request" ;;
        *) accept_update "$request" ;;
    esac
}

accept_backup() {
    local free
    free="$(free_bytes)"
    if [ "${free:-0}" -lt "$MIN_FREE_BYTES" ]; then
        refuse disk "$free bytes free under $DEPLOY_ROOT; a snapshot needs at least $MIN_FREE_BYTES"
        return
    fi
    JOB_INSTALLED_BEFORE="$(installed_tree_version)"
    write_status accepted ""
    run_backup
}

accept_rollback() {
    # $1 = the request file. The snapshot is named by a shape this agent
    # checks and must exist as a restorable directory; whether the database
    # is restored with the code is the request's choice, restore by default.
    local request="$1" snapshot restore_db free
    snapshot="$(json_get "$request" args snapshot)"
    restore_db="$(json_get "$request" args restore_database)"
    if ! [[ "$snapshot" =~ $SNAPSHOT_NAME_RE ]]; then
        refuse snapshot "the request names no snapshot"
        return
    fi
    if [ ! -f "$DEPLOY_ROOT/backups/$snapshot/db.sql.gz" ] || [ ! -f "$DEPLOY_ROOT/backups/$snapshot/.env" ]; then
        refuse snapshot "no complete snapshot named $snapshot under $DEPLOY_ROOT/backups"
        return
    fi
    if [ "$restore_db" = false ] && [ ! -f "$DEPLOY_ROOT/backups/$snapshot/code.tar.gz" ]; then
        refuse snapshot "snapshot $snapshot holds no code archive, so there is nothing to restore while keeping the database"
        return
    fi
    if [ "$restore_db" = false ] && ! updater_supports_code_only; then
        refuse code_only "the agent's update.sh (version $(updater_script_version)) cannot keep the database on a rollback; apply a release carrying update.sh $UPDATER_SCRIPT_CODE_ONLY or newer first"
        return
    fi
    free="$(free_bytes)"
    if [ "${free:-0}" -lt "$MIN_FREE_BYTES" ]; then
        refuse disk "$free bytes free under $DEPLOY_ROOT; a rollback needs at least $MIN_FREE_BYTES for its safety snapshot"
        return
    fi
    JOB_SNAPSHOT="$snapshot"
    JOB_INSTALLED_BEFORE="$(installed_tree_version)"
    write_status accepted ""
    run_standalone_rollback "$restore_db"
}

installed_tree_version() {
    sed -n '/^__version__ = "/{s/^__version__ = "\([^"]*\)".*/\1/p;q;}' "$DEPLOY_ROOT/epicurrents/version.py" 2>/dev/null || true
}

accept_update() {
    # $1 = the request file.
    local request="$1" sha pkgdir work copy have free size need rc token key
    sha="$(json_get "$request" args package_sha256)"
    JOB_WINDOW="$(json_get "$request" args verify_window_minutes)"
    if ! [[ "$sha" =~ ^[0-9a-f]{64}$ ]]; then
        refuse hash "the request names no package hash"
        return
    fi
    pkgdir="$PACKAGES/$sha"
    if [ ! -f "$pkgdir/package.tar.gz" ] || [ ! -f "$pkgdir/manifest.json" ] || [ ! -f "$pkgdir/manifest.sig" ]; then
        refuse hash "no uploaded package has hash $sha"
        return
    fi

    # Out of the uid-1000 tree before anything reads it as a package: the
    # copy is what gets verified, and the copy is what gets applied.
    work="$STATE_DIR/work/$JOB_ID"
    rm -rf "$work"
    mkdir -p "$work"
    copy="$work/package.tar.gz"
    cp "$pkgdir/package.tar.gz" "$copy"
    cp "$pkgdir/manifest.json" "$copy.manifest.json"
    cp "$pkgdir/manifest.sig" "$copy.manifest.sig"
    have="$(sha256_of "$copy")"
    if [ "$have" != "$sha" ]; then
        refuse hash "the package's sha256 is $have, the request names $sha"
        return
    fi

    size="$(wc -c < "$copy" | tr -d ' ')"
    free="$(free_bytes)"
    need=$((size * 2 + MIN_FREE_BYTES))
    if [ "${free:-0}" -lt "$need" ]; then
        refuse disk "$free bytes free under $DEPLOY_ROOT; an update needs $need (twice the package plus $MIN_FREE_BYTES)"
        return
    fi

    job_note "checking the package ($sha)"
    set_key_args
    run_update_sh --check-archive "$copy" --require-signature "${KEY_ARGS[@]}" --require-newer && rc=0 || rc=$?
    if [ "$rc" -ne 0 ]; then
        token="$(fact refused)"
        refuse "${token:-check}" "update.sh refused the package: $(fact failed)"
        return
    fi
    JOB_INSTALLED_BEFORE="$(fact installed)"
    JOB_TARGET="$(fact version)"
    # The package verified. A signature by the successor key is what settles
    # a rotation: from here on that key is the current one. And a manifest
    # may announce the next successor, which the signature just vouched for.
    key="$(fact key)"
    if [ -n "$key" ] && [ "$key" = "$RELEASE_KEY_NEXT" ] && [ -f "$RELEASE_KEY_NEXT" ]; then
        mv -f "$RELEASE_KEY_NEXT" "$RELEASE_KEY"
        job_note "release key rotated: the package is signed with the successor key (id $(key_id_of "$RELEASE_KEY")), which is now the current key"
    fi
    install_successor_key "$copy.manifest.json"
    write_status accepted ""
    run_update
}

install_successor_key() {
    # $1 = the verified manifest copy. The successor's PEM travels inside the
    # signed manifest, so its bytes are as trusted as the package. Installed
    # beside the current key, root-owned; the current key stays in force
    # until a package signed with the successor verifies.
    local manifest="$1" pem id have tmp
    pem="$(json_get "$manifest" successor_key)"
    [ -n "$pem" ] || return 0
    id="$(json_get "$manifest" successor_key_id)"
    case "$pem" in
        *"BEGIN PUBLIC KEY"*) ;;
        *)
            job_note "the manifest announces a successor key that is not a PEM public key; ignored"
            return 0
            ;;
    esac
    if [ "$(cat "$RELEASE_KEY" 2>/dev/null)" = "$pem" ]; then
        return 0
    fi
    if [ -f "$RELEASE_KEY_NEXT" ] && [ "$(cat "$RELEASE_KEY_NEXT")" = "$pem" ]; then
        return 0
    fi
    tmp="$(mktemp "$CONFIG_DIR/.release.pub.XXXXXX")"
    printf '%s\n' "$pem" > "$tmp"
    chmod 0600 "$tmp"
    have="$(key_id_of "$tmp")"
    if [ -n "$have" ] && [ -n "$id" ] && [ "$have" != "$id" ]; then
        rm -f "$tmp"
        job_note "the manifest's successor key has id $have but claims $id; not installed"
        return 0
    fi
    mv -f "$tmp" "$RELEASE_KEY_NEXT"
    job_note "successor release key installed (id ${have:-${id:-unknown}}); the next package may be signed with it"
    return 0
}

# ── The update ────────────────────────────────────────────────────────────────

window_minutes() {
    local minutes="$JOB_WINDOW"
    [[ "$minutes" =~ ^[0-9]+$ ]] || minutes="$VERIFY_WINDOW_MINUTES"
    [[ "$minutes" =~ ^[0-9]+$ ]] || minutes=30
    if [ "$minutes" -lt 5 ]; then minutes=5; fi
    if [ "$minutes" -gt 1440 ]; then minutes=1440; fi
    printf '%s' "$minutes"
}

run_update() {
    local copy="$STATE_DIR/work/$JOB_ID/package.tar.gz" rc snapshot
    write_active_lock
    write_flag updating "The platform is being updated." ""
    stop_beat
    drain_worker
    JOB_STARTED_AT="$(now_iso)"
    write_status running ""
    set_key_args
    run_update_sh --archive "$copy" --require-signature "${KEY_ARGS[@]}" --require-newer \
        --skip-beat --keep-lock --yes && rc=0 || rc=$?
    snapshot="$(fact snapshot)"
    JOB_SNAPSHOT="${snapshot##*/}"
    JOB_MIGRATIONS="$(fact migrations)"
    if [ "$rc" -ne 0 ]; then
        if [ -n "$JOB_SNAPSHOT" ]; then
            job_note "update.sh failed (exit $rc) after the snapshot; rolling back"
            rollback update_failed false
        else
            job_note "update.sh failed (exit $rc) before the snapshot completed; nothing to roll back"
            settle_failed update_failed_before_snapshot
        fi
        return
    fi
    if ! gate "$JOB_TARGET"; then
        job_note "the update applied but the platform did not pass the gate; rolling back"
        rollback health_failed false
        return
    fi
    refresh_update_sh "$copy"
    refresh_agent "$copy"
    JOB_DEADLINE="$(deadline_iso "$(window_minutes)")"
    write_flag verifying "The platform was updated and is waiting for a superuser to confirm it." "$JOB_DEADLINE"
    remove_active_lock
    JOB_STEP=""
    write_status awaiting_verification ""
    job_note "awaiting verification until $JOB_DEADLINE"
}

settle_failed() {
    # $1 = reason. The stack is where update.sh left it: running the old
    # release, since nothing past the snapshot happened.
    remove_flag
    start_beat
    remove_active_lock
    rm -rf "${STATE_DIR:?}/work/$JOB_ID"
    finish failed "$1"
}

refresh_update_sh() {
    # $1 = the verified package copy. The agent's update.sh is replaced with
    # the one the package ships, so the next package finds the script it was
    # built for; the package was verified, which is the trust the copy needs.
    local top tmp
    # `|| true` inside the substitution: a package that is not a tarball is a
    # refusal update.sh already made, and here it must not end the run.
    top="$( (tar -tzf "$1" 2>/dev/null || true) | awk -F/ 'NR==1{sub(/^\.\//, ""); print $1}')"
    if [ -z "$top" ]; then
        job_note "the package cannot be listed for an update.sh; the agent keeps its copy"
        return 0
    fi
    tmp="$(mktemp)"
    if tar -xzOf "$1" "$top/update.sh" > "$tmp" 2>/dev/null && [ -s "$tmp" ] && bash -n "$tmp" 2>/dev/null; then
        chmod 0755 "$tmp"
        mv -f "$tmp" "$UPDATE_SH"
        job_note "update.sh refreshed from the package"
    else
        rm -f "$tmp"
        job_note "the package carries no usable update.sh; the agent keeps its copy"
    fi
    return 0
}

refresh_agent() {
    # $1 = the verified package copy. Opt-in (SELF_UPDATE=1): the agent
    # replaces itself with the copy the package ships when that one is newer
    # and parses. A rename onto a new inode, never a write into this file,
    # since bash reads a script as it runs it; the copy being replaced is
    # kept beside it for an operator who needs to go back. The new agent runs
    # from the next tick.
    [ "$SELF_UPDATE" = 1 ] || return 0
    local top tmp version readme
    top="$( (tar -tzf "$1" 2>/dev/null || true) | awk -F/ 'NR==1{sub(/^\.\//, ""); print $1}')"
    [ -n "$top" ] || return 0
    tmp="$(mktemp)"
    if ! tar -xzOf "$1" "$top/updater/epicurrents-updater.sh" > "$tmp" 2>/dev/null || [ ! -s "$tmp" ]; then
        rm -f "$tmp"
        job_note "the package carries no agent; this one stays"
        return 0
    fi
    if ! bash -n "$tmp" 2>/dev/null; then
        rm -f "$tmp"
        job_note "the package's agent does not parse; this one stays"
        return 0
    fi
    version="$(sed -n '/^AGENT_VERSION=/{s/^AGENT_VERSION=\([0-9]*\)$/\1/p;q;}' "$tmp")"
    if ! [[ "$version" =~ ^[0-9]+$ ]] || [ "$version" -le "$AGENT_VERSION" ]; then
        rm -f "$tmp"
        job_note "the package's agent is version ${version:-unknown}, this is $AGENT_VERSION; this one stays"
        return 0
    fi
    cp -p "$AGENT_SELF" "$AGENT_SELF.previous" 2>/dev/null || true
    chmod 0755 "$tmp"
    mv -f "$tmp" "$AGENT_SELF"
    readme="$(mktemp)"
    if tar -xzOf "$1" "$top/updater/README.md" > "$readme" 2>/dev/null && [ -s "$readme" ]; then
        chmod 0644 "$readme"
        mv -f "$readme" "$LIB_DIR/README.md"
    else
        rm -f "$readme"
    fi
    job_note "agent updated to version $version from the package; it runs from the next tick (the previous copy is $AGENT_SELF.previous)"
    return 0
}

# ── A snapshot on request ─────────────────────────────────────────────────────

run_backup() {
    # code + database + .env under backups/backup-<stamp>, without a flag or
    # a stop: the dump is one transaction and the platform keeps serving.
    local rc snapshot
    write_active_lock
    JOB_STARTED_AT="$(now_iso)"
    write_status running ""
    run_update_sh --snapshot backup && rc=0 || rc=$?
    snapshot="$(fact snapshot)"
    JOB_SNAPSHOT="${snapshot##*/}"
    remove_active_lock
    JOB_STEP=""
    if [ "$rc" -ne 0 ] || [ -z "$JOB_SNAPSHOT" ]; then
        job_note "update.sh --snapshot failed (exit $rc)"
        finish failed snapshot_failed
        return
    fi
    prune_labelled_snapshots backup "$KEEP_BACKUP_SNAPSHOTS"
    finish succeeded ""
}

# ── A rollback on request ─────────────────────────────────────────────────────

run_standalone_rollback() {
    # $1 = whether to restore the database ("false" keeps it). A safety
    # snapshot first, so what the live database holds now is recoverable
    # from somewhere; then update.sh restores the named snapshot; then the
    # gate against whatever version the restored tree carries.
    local restore_db="$1" rc post restored
    write_active_lock
    write_flag rolling_back "The platform is being rolled back to an earlier snapshot." ""
    stop_beat
    drain_worker
    JOB_STARTED_AT="$(now_iso)"
    write_status running ""
    if [ -n "$JOB_POST_SNAPSHOT" ] && [ -d "$DEPLOY_ROOT/backups/$JOB_POST_SNAPSHOT" ]; then
        job_note "keeping the safety snapshot already taken: $JOB_POST_SNAPSHOT"
    elif job_note "taking a safety snapshot before the rollback" && run_update_sh --snapshot pre-rollback --keep-lock; then
        post="$(fact snapshot)"
        JOB_POST_SNAPSHOT="${post##*/}"
        job_note "safety snapshot: $JOB_POST_SNAPSHOT"
    else
        job_note "the safety snapshot failed; nothing was changed"
        settle_failed snapshot_failed
        return
    fi
    if [ "$restore_db" = false ]; then
        job_note "restoring the code of $JOB_SNAPSHOT; the database is kept"
    else
        job_note "restoring $JOB_SNAPSHOT"
    fi
    if [ "$restore_db" = false ]; then
        run_update_sh --rollback --snapshot "$JOB_SNAPSHOT" --code-only --yes --skip-beat --keep-lock && rc=0 || rc=$?
        if [ "$rc" -ne 0 ] && [ "$(fact refused)" = code_only ]; then
            job_note "update.sh declined to keep the database: $(fact failed)"
            settle_failed refused_code_only
            return
        fi
    else
        run_update_sh --rollback --snapshot "$JOB_SNAPSHOT" --yes --skip-beat --keep-lock && rc=0 || rc=$?
    fi
    if [ "$rc" -ne 0 ]; then
        job_note "update.sh --rollback failed (exit $rc); the deployment needs a shell"
        settle_rollback_failed
        return
    fi
    restored="$(fact restored)"
    if ! gate "$restored"; then
        job_note "the rollback applied but the platform did not pass the gate; the deployment needs a shell"
        settle_rollback_failed
        return
    fi
    prune_labelled_snapshots pre-rollback "$KEEP_POST_SNAPSHOTS"
    remove_flag
    start_beat
    remove_active_lock
    JOB_STEP=""
    finish succeeded ""
}

# ── The verification window ───────────────────────────────────────────────────

handle_awaiting() {
    if [ -f "$JOBS/$JOB_ID.verify" ]; then
        job_note "confirmed by user $(json_get "$JOBS/$JOB_ID.verify" by_user_id)"
        settle_succeeded
    elif [ -f "$JOBS/$JOB_ID.rollback" ]; then
        job_note "rollback requested by user $(json_get "$JOBS/$JOB_ID.rollback" by_user_id)"
        rollback requested true
    elif [ -n "$JOB_DEADLINE" ] && is_past "$JOB_DEADLINE"; then
        job_note "the verification window closed at $JOB_DEADLINE without a confirmation"
        rollback deadline true
    fi
}

settle_succeeded() {
    remove_flag
    start_beat
    rm -rf "${STATE_DIR:?}/work/$JOB_ID"
    JOB_STEP=""
    finish succeeded ""
}

handle_succeeded() {
    # A rollback may still be asked for after confirmation, for as long as the
    # pre-update snapshot exists.
    [ -f "$JOBS/$JOB_ID.rollback" ] || return 0
    if [ -n "$JOB_SNAPSHOT" ] && [ -d "$DEPLOY_ROOT/backups/$JOB_SNAPSHOT" ]; then
        job_note "late rollback requested by user $(json_get "$JOBS/$JOB_ID.rollback" by_user_id)"
        rollback late true
    else
        job_note "rollback requested, but the snapshot '${JOB_SNAPSHOT:-none}' no longer exists; nothing to roll back to"
        rm -f "$JOBS/$JOB_ID.rollback"
    fi
}

# ── Rollback ──────────────────────────────────────────────────────────────────

rollback() {
    # $1 = reason, $2 = whether the platform served during a window (true when
    # the job reached awaiting_verification). The post-update snapshot is
    # what keeps that window's data recoverable, so when there was a window
    # its failure is a failed rollback; when there was none there is nothing
    # to preserve and the rollback goes ahead without it.
    local reason="$1" had_window="$2" rc post
    write_active_lock
    write_flag rolling_back "The platform is being rolled back to the previous release." ""
    rm -f "$JOBS/$JOB_ID.rollback"
    JOB_STEP=""
    write_status rolling_back "$reason"
    if [ -z "$JOB_SNAPSHOT" ] || [ ! -d "$DEPLOY_ROOT/backups/$JOB_SNAPSHOT" ]; then
        job_note "no pre-update snapshot to restore ('${JOB_SNAPSHOT:-none}'); the rollback needs a shell"
        settle_rollback_failed
        return
    fi
    drain_worker
    if [ -n "$JOB_POST_SNAPSHOT" ] && [ -d "$DEPLOY_ROOT/backups/$JOB_POST_SNAPSHOT" ]; then
        job_note "keeping the post-update snapshot already taken: $JOB_POST_SNAPSHOT"
    elif job_note "taking a post-update snapshot" && run_update_sh --snapshot post-update --keep-lock; then
        post="$(fact snapshot)"
        JOB_POST_SNAPSHOT="${post##*/}"
        job_note "post-update snapshot: $JOB_POST_SNAPSHOT"
    elif [ "$had_window" = true ]; then
        job_note "the post-update snapshot failed; refusing to roll back over data written since the update"
        settle_rollback_failed
        return
    else
        job_note "the post-update snapshot failed; nothing was served since the snapshot, so the rollback goes ahead"
    fi
    job_note "restoring $JOB_SNAPSHOT"
    if [ "$JOB_MIGRATIONS" = none ] && updater_supports_code_only; then
        # The release applied no migration, so the database still fits the
        # code being restored: keep it, and with it everything written since
        # the update. update.sh checks the same fact against the database
        # and may decline, in which case the database is restored after all.
        job_note "the update applied no migration; restoring the code and keeping the database"
        run_update_sh --rollback --snapshot "$JOB_SNAPSHOT" --code-only --yes --skip-beat --keep-lock && rc=0 || rc=$?
        if [ "$rc" -ne 0 ] && [ "$(fact refused)" = code_only ]; then
            job_note "update.sh declined to keep the database ($(fact failed)); restoring it too"
            run_update_sh --rollback --snapshot "$JOB_SNAPSHOT" --yes --skip-beat --keep-lock && rc=0 || rc=$?
        fi
    else
        run_update_sh --rollback --snapshot "$JOB_SNAPSHOT" --yes --skip-beat --keep-lock && rc=0 || rc=$?
    fi
    if [ "$rc" -ne 0 ]; then
        job_note "update.sh --rollback failed (exit $rc); the deployment needs a shell"
        settle_rollback_failed
        return
    fi
    if ! gate "$JOB_INSTALLED_BEFORE"; then
        job_note "the rollback applied but the platform did not pass the gate; the deployment needs a shell"
        settle_rollback_failed
        return
    fi
    prune_labelled_snapshots post-update "$KEEP_POST_SNAPSHOTS"
    remove_flag
    start_beat
    remove_active_lock
    rm -rf "${STATE_DIR:?}/work/$JOB_ID"
    JOB_STEP=""
    finish rolled_back "$reason"
}

# Snapshots update.sh names for their purpose are never pruned by update.sh
# itself, so the ones this agent takes would accumulate a database dump each.
# Of the post-update and pre-rollback ones the newest two are kept: the one
# just taken, and the one before it in case the rollback was of a rollback.
# Of the backups a superuser asked for, the newest three.
KEEP_POST_SNAPSHOTS=2
KEEP_BACKUP_SNAPSHOTS=3

prune_labelled_snapshots() {
    # $1 = label, $2 = how many to keep. The names carry a UTC stamp, so the
    # glob's order is chronological.
    local label="$1" keep="$2" dirs i
    dirs=("$DEPLOY_ROOT"/backups/"$label"-*)
    [ -d "${dirs[0]}" ] || return 0
    for ((i = 0; i < ${#dirs[@]} - keep; i++)); do
        job_note "pruning old $label snapshot $(basename "${dirs[i]}")"
        rm -rf "${dirs[i]}"
    done
    return 0
}

settle_rollback_failed() {
    # The flag stays: the platform is not serving reliably, and the message
    # is what tells anyone who reaches it why. The active lock goes, since
    # nothing is running any more.
    remove_active_lock
    JOB_STEP=""
    finish rollback_failed "$JOB_REASON"
}

# ── A previous tick that died mid-way ─────────────────────────────────────────

handle_stale_lock() {
    [ -f "$ACTIVE_LOCK" ] || return 0
    local pid boot job
    pid="$(json_get "$ACTIVE_LOCK" pid)"
    boot="$(json_get "$ACTIVE_LOCK" boot_id)"
    job="$(json_get "$ACTIVE_LOCK" job_id)"
    if [ "$boot" = "$(boot_id)" ] && [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
        log "another run (pid $pid) is executing job $job; nothing to do"
        return 1
    fi
    if ! [[ "$job" =~ ^[0-9a-f-]{36}$ ]] || [ ! -f "$JOBS/$job.status.json" ]; then
        log "stale lock names no known job; removing it"
        remove_active_lock
        return 0
    fi
    reset_job "$job"
    load_status
    job_note "the run executing this job (pid ${pid:-?}, boot ${boot:-?}) is gone; the lock is stale"
    case "$JOB_OPERATION" in
        "$OPERATION_BACKUP")
            # A snapshot that did not complete is a directory update.sh
            # prunes as half-written; nothing else was touched.
            job_note "the snapshot did not complete; nothing was changed"
            remove_active_lock
            finish failed stale
            return 0
            ;;
        "$OPERATION_ROLLBACK")
            if [ "$JOB_RETRIES" -lt 1 ]; then
                JOB_RETRIES=$((JOB_RETRIES + 1))
                job_note "retrying the interrupted rollback"
                run_standalone_rollback "$(json_get "$JOBS/$JOB_ID.json" args restore_database)"
            else
                job_note "the rollback was interrupted and already retried once; the deployment needs a shell"
                settle_rollback_failed
            fi
            return 0
            ;;
    esac
    case "$JOB_STATE" in
        accepted|running)
            if [ -n "$JOB_SNAPSHOT" ] || grep -q '^::snapshot=' "$(job_log)" 2>/dev/null; then
                if [ -z "$JOB_SNAPSHOT" ]; then
                    JOB_SNAPSHOT="$(sed -n 's/^::snapshot=//p' "$(job_log)" | tail -1)"
                    JOB_SNAPSHOT="${JOB_SNAPSHOT##*/}"
                fi
                job_note "the update had taken its snapshot; rolling back to it"
                rollback stale false
            else
                job_note "the update had not taken its snapshot; nothing was changed"
                settle_failed stale
            fi
            ;;
        rolling_back)
            if [ "$JOB_RETRIES" -ge 1 ]; then
                job_note "the rollback was already retried once; the deployment needs a shell"
                settle_rollback_failed
            else
                JOB_RETRIES=$((JOB_RETRIES + 1))
                job_note "retrying the interrupted rollback"
                rollback "${JOB_REASON:-stale}" true
            fi
            ;;
        *)
            job_note "lock left behind by a job in state '$JOB_STATE'; removing it"
            remove_active_lock
            ;;
    esac
    return 0
}

# ── The tick ──────────────────────────────────────────────────────────────────

in_flight_elsewhere() {
    # Whether any job other than $1 is in a state this agent still owns.
    local path id state
    for path in "$JOBS"/*.status.json; do
        [ -f "$path" ] || continue
        id="$(basename "$path" .status.json)"
        [ "$id" != "$1" ] || continue
        state="$(json_get "$path" state)"
        case "$state" in
            accepted|running|awaiting_verification|rolling_back) return 0 ;;
        esac
    done
    return 1
}

tick() {
    local path id request status
    # Requests first, oldest first, so a queue drains in order; a request
    # while another job is in flight waits for its turn.
    # Names are UUIDs by the check below, so parsing ls is safe here; it is
    # what gives oldest-first without a stat that differs between platforms.
    # shellcheck disable=SC2010,SC2012
    for id in $(cd "$JOBS" && ls -1tr -- *.json 2>/dev/null | grep -v '\.status\.json$' | sed 's/\.json$//' || true); do
        [[ "$id" =~ ^[0-9a-f-]{36}$ ]] || continue
        request="$JOBS/$id.json"
        status="$JOBS/$id.status.json"
        if [ -f "$status" ]; then
            continue
        fi
        if [ "$(json_get "$request" job_id)" != "$id" ]; then
            log "request $id names a different job id; ignored"
            continue
        fi
        if in_flight_elsewhere "$id"; then
            continue
        fi
        reset_job "$id"
        accept_or_refuse "$request"
        # One request per tick: the next is looked at once this one has
        # reached a state that lets another start.
        break
    done
    # Jobs in their window, or confirmed and asked to roll back after all.
    for path in "$JOBS"/*.status.json; do
        [ -f "$path" ] || continue
        id="$(basename "$path" .status.json)"
        [[ "$id" =~ ^[0-9a-f-]{36}$ ]] || continue
        reset_job "$id"
        load_status
        [ "$JOB_OPERATION" = "$OPERATION_UPDATE" ] || continue
        case "$JOB_STATE" in
            awaiting_verification) handle_awaiting ;;
            succeeded) handle_succeeded ;;
        esac
    done
}

main() {
    if ! load_config; then
        log "no configuration at $CONFIG_DIR/config; nothing to do"
        exit 0
    fi
    if [ "$(id -u)" != 0 ]; then
        log "not running as root; the spool files this run writes cannot be handed to the deployment account"
    fi
    if command -v flock >/dev/null 2>&1; then
        mkdir -p "$(dirname "$RUN_LOCK")" 2>/dev/null || true
        exec 9>"$RUN_LOCK"
        if ! flock -n 9; then
            log "another run holds $RUN_LOCK; exiting"
            exit 0
        fi
    fi
    locate_spool
    [ -x "$UPDATE_SH" ] || die "$UPDATE_SH is missing or not executable; re-run install-updater.sh."
    if ! detect_runtime; then
        write_heartbeat
        die "no container runtime found (Docker Engine or Podman with Compose v2)."
    fi
    write_heartbeat
    mkdir -p "$STATE_DIR/work"
    handle_stale_lock || exit 0
    tick
}

main "$@"
