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
# The spool is one-way. The web tier can write anything there, so the agent
# never follows a link in it, never writes into a file it finds there, and
# never acts on anything it did not put there itself. What it acts on — the
# status of record of each job, the active-run lock, the job logs — lives in a
# root-only state directory; the spool receives published copies, each written
# as a fresh file with O_EXCL|O_NOFOLLOW and renamed over whatever stood at the
# name. A request is claimed by renaming it before a byte of it is read, which
# is what makes a cancel from the platform and a claim from here exclusive.
# The markers the platform writes to confirm or roll back an update are read
# for their presence only.
#
# One job at a time. A tick either refuses or accepts a request, runs the
# update to its verification window, acts on a confirmation or a rollback
# marker, or rolls a job back whose window closed. Every state it reaches is
# written to the state directory, published into the spool where the platform
# projects it onto the job row, and logged to the system journal, which
# survives the database restore a rollback performs.
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
# Everything this process creates is root's alone unless it says otherwise:
# the files it publishes into the spool set their own mode.
umask 077

AGENT_VERSION=3
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
# How many lines of update.sh output go by between two publications of the log.
LOG_PUBLISH_LINES=100

UPDATE_SH="$LIB_DIR/update.sh"
AGENT_SELF="$LIB_DIR/epicurrents-updater.sh"
RELEASE_KEY="$CONFIG_DIR/release.pub"
# The successor a release announced, trusted beside the current key until a
# package signed with it verifies, at which point it becomes the current key.
RELEASE_KEY_NEXT="$CONFIG_DIR/release.pub.next"
# The state of record, root's own. The spool holds copies of some of it.
STATE_JOBS="$STATE_DIR/jobs"
ACTIVE_LOCK="$STATE_DIR/active.lock"
STATE_FLAG="$STATE_DIR/maintenance.json"
STATE_HEARTBEAT="$STATE_DIR/agent.json"
# A snapshot name as update.sh writes them: a label, a UTC date and a time.
SNAPSHOT_NAME_RE='^[A-Za-z0-9][A-Za-z0-9_-]*-[0-9]{8}-[0-9]{6}$'
UUID_RE='^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'

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

clean() {
    # $1 = text that may carry a value from the spool. Control characters
    # dropped and the length bounded, so a request cannot forge a line in the
    # journal or the job log, or paint the operator's terminal.
    local value
    value="$(printf '%s' "$1" | LC_ALL=C tr -d '\000-\037\177')"
    printf '%s' "${value:0:1000}"
}

log() {
    # To the journal when running under systemd, to stderr otherwise; a
    # `logger` line as well so the timeline exists somewhere the database
    # restore cannot reach.
    local line
    line="$(clean "$*")"
    printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$line" >&2
    if command -v logger >/dev/null 2>&1; then
        logger -t epicurrents-updater -- "$line" 2>/dev/null || true
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
    case "$DEPLOY_ROOT" in
        /*) ;;
        *) die "DEPLOY_ROOT $DEPLOY_ROOT is not an absolute path." ;;
    esac
    [ -d "$DEPLOY_ROOT" ] || die "DEPLOY_ROOT $DEPLOY_ROOT does not exist."
    [ -f "$DEPLOY_ROOT/docker-compose.yml" ] || die "DEPLOY_ROOT $DEPLOY_ROOT is not a deployment (no docker-compose.yml)."
    DEPLOY_ROOT="$(cd "$DEPLOY_ROOT" && pwd -P)"
    return 0
}

SPOOL=""
TREE_OWNER="1000:1000"

locate_spool() {
    SPOOL="$DEPLOY_ROOT/update"
    # Whoever owns the deployment tree is who the containers run as; every file
    # this root process publishes into the spool is handed to them, or the web
    # tier could never read its own job's status.
    TREE_OWNER="$(stat -c %u:%g "$DEPLOY_ROOT" 2>/dev/null || echo 1000:1000)"
    [[ "$TREE_OWNER" =~ ^[0-9]+:[0-9]+$ ]] || TREE_OWNER="1000:1000"
    mkdir -p "$STATE_JOBS" "$STATE_DIR/work"
    chmod 0700 "$STATE_DIR"
    spoolfs init || die "the spool at $SPOOL cannot be used safely; nothing was done"
}

# ── The spool, touched only through this ──────────────────────────────────────
# Every operation on a path under ./update/ goes through spoolfs, which opens
# each directory with O_NOFOLLOW relative to the one above it and never
# resolves a path by name. A symlink, a hard link, a FIFO or a directory the
# web tier left where a file belongs is refused, never followed.
#   init                 ensure update/, jobs/ and packages/ are real directories
#   publish REL SRC ...  publish each root-owned file SRC at its REL (agent.json,
#                        maintenance.json, jobs/<id>.status.json, jobs/<id>.log)
#   remove REL           unlink maintenance.json or a marker
#   present REL          0 when a marker is a regular file
#   requests             unclaimed request ids, oldest first
#   claim ID DEST        rename jobs/ID.json to jobs/ID.claimed.json, then copy
#                        it to DEST; 4 when there was nothing to claim
#   package-size SHA     the size of packages/SHA/package.tar.gz; 4 when absent
#   fetch-package SHA DIR SIZE   copy the package's three files into DIR
# Exit status: 0 done, 1 no, 3 a spool directory is unusable, 4 absent,
# 5 present but unusable; a message on stderr for anything but 0 and 1.

read -r -d '' SPOOLFS_PY <<'PY' || true
import os, re, secrets, stat, sys

UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
PUBLISHED = re.compile(rf"(agent\.json|maintenance\.json|jobs/{UUID}\.(status\.json|log))")
REMOVABLE = re.compile(rf"(maintenance\.json|jobs/{UUID}\.(verify|rollback))")
MARKER = re.compile(rf"jobs/{UUID}\.(verify|rollback)")
SHA256 = re.compile(r"[0-9a-f]{64}")
TEMP = re.compile(r"\.agent-[0-9a-f]{16}\.tmp")
CLOEXEC = getattr(os, "O_CLOEXEC", 0)
DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | CLOEXEC
READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | CLOEXEC
NEW_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | CLOEXEC
REQUEST_CAP = 64 * 1024
PACKAGE_SIDECARS = (("manifest.json", ".manifest.json", 64 * 1024), ("manifest.sig", ".manifest.sig", 4 * 1024))


class Refused(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def owner():
    uid, _, gid = os.environ["SPOOL_OWNER"].partition(":")
    return int(uid), int(gid)


def open_dir(name, parent):
    try:
        return os.open(name, DIR_FLAGS, dir_fd=parent)
    except FileNotFoundError:
        raise Refused(4, f"{name} does not exist")
    except OSError as exc:
        raise Refused(3, f"{name} is not a directory ({exc.strerror})")


def open_spool():
    root = os.open(os.environ["DEPLOY_ROOT"], os.O_RDONLY | os.O_DIRECTORY | CLOEXEC)
    try:
        return open_dir("update", root)
    finally:
        os.close(root)


def parent_of(rel):
    spool = open_spool()
    if "/" not in rel:
        return spool, rel
    sub, _, name = rel.partition("/")
    try:
        return open_dir(sub, spool), name
    finally:
        os.close(spool)


def ensure_dir(name, parent):
    created = False
    try:
        os.mkdir(name, 0o755, dir_fd=parent)
        created = True
    except FileExistsError:
        pass
    fd = open_dir(name, parent)
    if created and os.geteuid() == 0:
        os.fchown(fd, *owner())
        os.fchmod(fd, 0o755)
    return fd


def sweep_temps(fd):
    for name in os.listdir(fd):
        if TEMP.fullmatch(name):
            try:
                os.unlink(name, dir_fd=fd)
            except OSError:
                pass


def safe_read(parent, name, cap):
    try:
        fd = os.open(name, READ_FLAGS, dir_fd=parent)
    except FileNotFoundError:
        raise Refused(4, f"{name} does not exist")
    except OSError as exc:
        raise Refused(5, f"{name} cannot be opened as a plain file ({exc.strerror})")
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise Refused(5, f"{name} is not a regular file")
        if info.st_nlink != 1:
            raise Refused(5, f"{name} has {info.st_nlink} links")
        if info.st_size > cap:
            raise Refused(5, f"{name} is larger than {cap} bytes")
        chunks, total = [], 0
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > cap:
                raise Refused(5, f"{name} grew past {cap} bytes while being read")
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def write_private(path, data):
    fd = os.open(path, NEW_FLAGS, 0o600)
    with os.fdopen(fd, "wb") as out:
        out.write(data)


def cmd_init():
    root = os.open(os.environ["DEPLOY_ROOT"], os.O_RDONLY | os.O_DIRECTORY | CLOEXEC)
    try:
        spool = ensure_dir("update", root)
    finally:
        os.close(root)
    sweep_temps(spool)
    for name in ("jobs", "packages"):
        fd = ensure_dir(name, spool)
        if name == "jobs":
            sweep_temps(fd)
        os.close(fd)
    os.close(spool)


def cmd_publish(*pairs):
    if not pairs or len(pairs) % 2:
        raise Refused(5, "publish takes pairs of a name and a source")
    for index in range(0, len(pairs), 2):
        publish_one(pairs[index], pairs[index + 1])


def publish_one(rel, source):
    if not PUBLISHED.fullmatch(rel):
        raise Refused(5, f"{rel} is not a name the agent publishes")
    with open(source, "rb") as handle:
        data = handle.read()
    parent, name = parent_of(rel)
    temp = f".agent-{secrets.token_hex(8)}.tmp"
    try:
        fd = os.open(temp, NEW_FLAGS, 0o600, dir_fd=parent)
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
            if os.geteuid() == 0:
                os.fchown(fd, *owner())
            os.fchmod(fd, 0o644)
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            os.rename(temp, name, src_dir_fd=parent, dst_dir_fd=parent)
        except OSError as exc:
            raise Refused(5, f"{rel} cannot be replaced ({exc.strerror})")
    except BaseException:
        try:
            os.unlink(temp, dir_fd=parent)
        except OSError:
            pass
        raise
    finally:
        os.close(parent)


def cmd_remove(rel):
    if not REMOVABLE.fullmatch(rel):
        raise Refused(5, f"{rel} is not a name the agent removes")
    parent, name = parent_of(rel)
    try:
        os.unlink(name, dir_fd=parent)
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise Refused(5, f"{rel} cannot be removed ({exc.strerror})")
    finally:
        os.close(parent)


def cmd_present(rel):
    if not MARKER.fullmatch(rel):
        raise Refused(5, f"{rel} is not a marker")
    parent, name = parent_of(rel)
    try:
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return 1
    finally:
        os.close(parent)
    return 0 if stat.S_ISREG(info.st_mode) else 1


def cmd_requests():
    parent, _ = parent_of("jobs/x")
    found = []
    try:
        for name in os.listdir(parent):
            match = re.fullmatch(rf"({UUID})\.json", name)
            if not match:
                continue
            try:
                info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISREG(info.st_mode):
                found.append((info.st_mtime_ns, match.group(1)))
    finally:
        os.close(parent)
    for _, job_id in sorted(found):
        print(job_id)


def cmd_claim(job_id, dest):
    if not re.fullmatch(UUID, job_id):
        raise Refused(5, f"{job_id} is not a job id")
    parent, _ = parent_of("jobs/x")
    try:
        try:
            os.rename(f"{job_id}.json", f"{job_id}.claimed.json", src_dir_fd=parent, dst_dir_fd=parent)
        except FileNotFoundError:
            raise Refused(4, "the request was withdrawn")
        write_private(dest, safe_read(parent, f"{job_id}.claimed.json", REQUEST_CAP))
    finally:
        os.close(parent)


def open_package(sha):
    if not SHA256.fullmatch(sha):
        raise Refused(5, f"{sha} is not a package hash")
    packages, _ = parent_of("packages/x")
    try:
        return open_dir(sha, packages)
    finally:
        os.close(packages)


def package_fd(pkgdir):
    try:
        fd = os.open("package.tar.gz", READ_FLAGS, dir_fd=pkgdir)
    except FileNotFoundError:
        raise Refused(4, "package.tar.gz does not exist")
    except OSError as exc:
        raise Refused(5, f"package.tar.gz cannot be opened as a plain file ({exc.strerror})")
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        os.close(fd)
        raise Refused(5, "package.tar.gz is not a plain file with one link")
    return fd, info.st_size


def cmd_package_size(sha):
    pkgdir = open_package(sha)
    try:
        for name, _, _ in PACKAGE_SIDECARS:
            try:
                info = os.stat(name, dir_fd=pkgdir, follow_symlinks=False)
            except FileNotFoundError:
                raise Refused(4, f"{name} does not exist")
            if not stat.S_ISREG(info.st_mode):
                raise Refused(5, f"{name} is not a regular file")
        fd, size = package_fd(pkgdir)
        os.close(fd)
        print(size)
    finally:
        os.close(pkgdir)


def cmd_fetch_package(sha, dest, size):
    size = int(size)
    pkgdir = open_package(sha)
    try:
        for name, suffix, cap in PACKAGE_SIDECARS:
            write_private(os.path.join(dest, "package.tar.gz" + suffix), safe_read(pkgdir, name, cap))
        fd, have = package_fd(pkgdir)
        try:
            if have != size:
                raise Refused(5, f"package.tar.gz is {have} bytes, not the {size} it was a moment ago")
            out = os.open(os.path.join(dest, "package.tar.gz"), NEW_FLAGS, 0o600)
            try:
                copied = 0
                while True:
                    chunk = os.read(fd, 1024 * 1024)
                    if not chunk:
                        break
                    copied += len(chunk)
                    if copied > size:
                        raise Refused(5, "package.tar.gz grew while being copied")
                    view = memoryview(chunk)
                    while view:
                        view = view[os.write(out, view):]
            finally:
                os.close(out)
        finally:
            os.close(fd)
    finally:
        os.close(pkgdir)


COMMANDS = {
    "init": cmd_init,
    "publish": cmd_publish,
    "remove": cmd_remove,
    "present": cmd_present,
    "requests": cmd_requests,
    "claim": cmd_claim,
    "package-size": cmd_package_size,
    "fetch-package": cmd_fetch_package,
}

try:
    sys.exit(COMMANDS[sys.argv[1]](*sys.argv[2:]) or 0)
except Refused as refusal:
    print(refusal, file=sys.stderr)
    sys.exit(refusal.code)
except OSError as exc:
    print(f"{exc.strerror or exc}", file=sys.stderr)
    sys.exit(3)
PY

spoolfs() {
    DEPLOY_ROOT="$DEPLOY_ROOT" SPOOL_OWNER="$TREE_OWNER" python3 -c "$SPOOLFS_PY" "$@"
}

publish() {
    # Pairs of a name under the spool and the root-owned file it copies. A
    # failure is logged and survived: the state of record is unaffected, and
    # the next write publishes again.
    local err
    if ! err="$(spoolfs publish "$@" 2>&1)"; then
        log "could not publish $1 into the spool: $err"
        return 1
    fi
    return 0
}

marker_present() {
    # $1 = verify or rollback. Only whether the platform left one; what it
    # says is never read.
    spoolfs present "jobs/$JOB_ID.$1" 2>/dev/null
}

remove_marker() {
    spoolfs remove "jobs/$JOB_ID.$1" 2>/dev/null || log "could not remove the $1 marker of job $JOB_ID"
}

decline_marker() {
    # $1 = the marker, $2 = why it is not acted on. Said in the job's log,
    # which is published, since no status write follows to carry it.
    job_note "$2"
    remove_marker "$1"
    publish_log
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
# through a shell string, and nothing read from a file is evaluated. Both
# helpers work on root's own files only; the spool is reached through spoolfs.

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
    # string. Written to a fresh temporary file beside the target and renamed
    # into place, so a reader never sees a partial file.
    local target="$1" tmp
    shift
    tmp="$(mktemp "$(dirname "$target")/.$(basename "$target").XXXXXX")"
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

valid_snapshot() {
    [[ "$1" =~ $SNAPSHOT_NAME_RE ]]
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
    json_write "$STATE_HEARTBEAT" \
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
    publish agent.json "$STATE_HEARTBEAT" || true
}

# ── Job state ─────────────────────────────────────────────────────────────────
# One job's fields live in these globals while it is being worked on; every
# status write carries the full set, because the platform blanks a field the
# file does not carry. The file of record is STATE_JOBS/<id>.status.json;
# what the spool holds is a copy of it.

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
    printf '%s\n' "$STATE_JOBS/$JOB_ID.log"
}

job_status() {
    printf '%s\n' "$STATE_JOBS/$JOB_ID.status.json"
}

job_request() {
    # The agent's copy of the request, taken when it was claimed.
    printf '%s\n' "$STATE_JOBS/$JOB_ID.request.json"
}

publish_log() {
    [ ! -f "$(job_log)" ] || publish "jobs/$JOB_ID.log" "$(job_log)" || true
}

job_note() {
    # A line of the agent's own into the job's log, and into the journal.
    local line
    line="[updater] $(clean "$*")"
    append_log "$line"
    log "job $JOB_ID: $*"
}

append_log() {
    printf '%s\n' "$*" >> "$(job_log)"
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
    if [ -f "$(job_log)" ]; then
        publish "jobs/$JOB_ID.status.json" "$(job_status)" "jobs/$JOB_ID.log" "$(job_log)" || true
    else
        publish "jobs/$JOB_ID.status.json" "$(job_status)" || true
    fi
    log "job $JOB_ID: $JOB_STATE${JOB_REASON:+ ($JOB_REASON)}${JOB_STEP:+ step=$JOB_STEP}"
}

load_status() {
    # Read a job's status of record back into the globals, for a job that a
    # previous tick left in flight. The snapshot names are checked again on
    # the way in, since every path the rollback builds starts from them.
    local path
    path="$(job_status)"
    JOB_OPERATION="$(json_get "$path" operation)"
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
    [ -z "$JOB_SNAPSHOT" ] || valid_snapshot "$JOB_SNAPSHOT" || JOB_SNAPSHOT=""
    [ -z "$JOB_POST_SNAPSHOT" ] || valid_snapshot "$JOB_POST_SNAPSHOT" || JOB_POST_SNAPSHOT=""
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
    # $1 = phase, $2 = message, $3 = expected_until (may be empty). Non-zero
    # when the flag could not be published, which leaves the platform
    # accepting writes: an update does not start without it.
    local since
    since="$(json_get "$STATE_FLAG" since)"
    [ -n "$since" ] || since="$(now_iso)"
    json_write "$STATE_FLAG" \
        "protocol=@int:$PROTOCOL" \
        "phase=$1" \
        "job_id=$JOB_ID" \
        "since=$since" \
        "expected_until=$(opt "${3:-}")" \
        "message=$2"
    publish maintenance.json "$STATE_FLAG"
}

remove_flag() {
    rm -f "$STATE_FLAG"
    spoolfs remove maintenance.json 2>/dev/null || log "could not remove the maintenance flag from the spool"
}

boot_id() {
    cat "$BOOT_ID_FILE" 2>/dev/null || echo unknown
}

write_active_lock() {
    # Who is executing which job. Present from the claim until the job no
    # longer needs this process; a lock whose process is gone, or from another
    # boot, is what tells the next tick the previous one died mid-way. Root's
    # own: nothing in the spool can hold or forge it.
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
    [[ "$port" =~ ^[0-9]+$ ]] || port=8000
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
    version="$(clean "$(running_version)")"
    JOB_RUNNING="${version:0:32}"
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
# Which field of the job a ::snapshot= line of the run being streamed fills:
# snapshot for an update or a backup, post_snapshot for the safety snapshot
# ahead of a rollback, nothing for a rollback. Written into the status of
# record the moment the line arrives, so a run that dies after it leaves the
# next tick a snapshot to roll back to.
SNAPSHOT_INTO=""

stream_log() {
    # Reads update.sh's output line by line into the job log (head-capped),
    # follows its :: lines, and records the facts the caller needs in $FACTS.
    # Runs in the pipe's subshell, so it writes the status itself on each step
    # and leaves the rest to the facts file.
    local line size=0 capped=false key value lines=0 name
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
        lines=$((lines + 1))
        if [ $((lines % LOG_PUBLISH_LINES)) = 0 ]; then
            publish_log
        fi
        case "$line" in
            ::*)
                key="${line#::}"
                value="${key#*=}"
                key="${key%%=*}"
                case "$key" in
                    step)
                        JOB_STEP="$(clean "$value")"
                        printf '%s=%s\n' "$key" "$JOB_STEP" >> "$FACTS"
                        if [ -n "$JOB_STATE" ]; then
                            write_status "$JOB_STATE" "$JOB_REASON"
                        fi
                        ;;
                    snapshot)
                        printf '%s=%s\n' "$key" "$value" >> "$FACTS"
                        name="${value##*/}"
                        if valid_snapshot "$name"; then
                            case "$SNAPSHOT_INTO" in
                                snapshot) JOB_SNAPSHOT="$name"; write_status "$JOB_STATE" "$JOB_REASON" ;;
                                post_snapshot) JOB_POST_SNAPSHOT="$name"; write_status "$JOB_STATE" "$JOB_REASON" ;;
                            esac
                        fi
                        ;;
                    health|failed|refused|done|installed|version|migrations|key|restored)
                        printf '%s=%s\n' "$key" "$(clean "$value")" >> "$FACTS"
                        ;;
                esac
                ;;
        esac
    done
    publish_log
}

fact() {
    # $1 = key. The last value recorded for it, or nothing.
    sed -n "s/^$1=//p" "$FACTS" 2>/dev/null | tail -1
}

snapshot_fact() {
    # The last ::snapshot= of the run, as a name, or nothing when it is not
    # the shape update.sh writes.
    local name
    name="$(fact snapshot)"
    name="${name##*/}"
    if valid_snapshot "$name"; then
        printf '%s' "$name"
    fi
}

run_update_sh() {
    # $@ = arguments. Runs the agent's own copy against the deployment root,
    # streaming into the job log. Returns update.sh's exit status.
    local rc
    rm -f "$FACTS"
    FACTS="$(mktemp "$STATE_DIR/work/.facts.XXXXXX")"
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

# ── Claiming and accepting a request ──────────────────────────────────────────

refuse() {
    # $1 = reason token, $2 = message. A refused request changes nothing and
    # leaves no work copy.
    job_note "refused: $2"
    rm -rf "${STATE_DIR:?}/work/$JOB_ID"
    remove_active_lock
    finish failed "refused_$1"
}

free_bytes() {
    # $1 = a path. Free bytes on its filesystem.
    df -Pk "$1" | awk 'NR==2{print $4 * 1024}'
}

device_of() {
    stat -c %d "$1" 2>/dev/null || stat -f %d "$1" 2>/dev/null || echo "$1"
}

disk_ok() {
    # $1 = bytes needed under DEPLOY_ROOT, $2 = bytes needed under STATE_DIR.
    # One filesystem holding both has to hold the sum.
    local deploy state
    deploy="$(free_bytes "$DEPLOY_ROOT")"
    state="$(free_bytes "$STATE_DIR")"
    if [ "$(device_of "$DEPLOY_ROOT")" = "$(device_of "$STATE_DIR")" ]; then
        [ "${deploy:-0}" -ge $(($1 + $2)) ]
    else
        [ "${deploy:-0}" -ge "$1" ] && [ "${state:-0}" -ge "$2" ]
    fi
}

claim_request() {
    # Takes the request out of the platform's reach before reading a byte of
    # it: once renamed, a cancel finds nothing to withdraw. 0 when claimed; 1
    # when there was nothing to claim, or the claimed file was unusable (then
    # the job has been failed).
    local err rc=0
    # The lock first: a run that dies between the claim and its first status
    # leaves the next tick a lock naming this job, which fails it as stale
    # rather than leaving a claimed request nobody owns.
    write_active_lock
    err="$(spoolfs claim "$JOB_ID" "$(job_request)" 2>&1)" || rc=$?
    case "$rc" in
        0) ;;
        4)
            log "request $JOB_ID was withdrawn before it could be claimed"
            remove_active_lock
            return 1
            ;;
        *)
            JOB_OPERATION=unknown
            refuse request "the request file is not a plain readable file: $err"
            return 1
            ;;
    esac
    local operation
    operation="$(json_get "$(job_request)" operation)"
    case "$operation" in
        "$OPERATION_UPDATE"|"$OPERATION_BACKUP"|"$OPERATION_ROLLBACK") JOB_OPERATION="$operation" ;;
        *) JOB_OPERATION=unknown ;;
    esac
    if [ "$(json_get "$(job_request)" job_id)" != "$JOB_ID" ]; then
        refuse request "the request names a different job id"
        return 1
    fi
    # Picked up: the platform can see the request is no longer withdrawable.
    JOB_STEP=check
    write_status requested ""
    return 0
}

accept_or_refuse() {
    # Every check a request must pass before anything is touched, in the
    # order that matters: the checks every operation shares, then the
    # operation's own. The request read is the agent's own copy.
    local request protocol operation
    request="$(job_request)"
    protocol="$(json_get "$request" protocol)"
    operation="$(json_get "$request" operation)"

    if [ "$ENABLED" != 1 ]; then
        refuse disabled "the agent is installed but not enabled (ENABLED=1 in $CONFIG_DIR/config)"
        return
    fi
    if [ "$protocol" != "$PROTOCOL" ]; then
        refuse protocol "the request carries protocol '$(clean "${protocol:-none}")'; this agent speaks $PROTOCOL"
        return
    fi
    case "$operation" in
        "$OPERATION_UPDATE"|"$OPERATION_BACKUP"|"$OPERATION_ROLLBACK") ;;
        *)
            refuse operation "operation '$(clean "${operation:-none}")' is not one this agent carries out"
            return
            ;;
    esac
    if [ -e "$DEPLOY_ROOT/.git" ] && [ "$ALLOW_CHECKOUT" != 1 ]; then
        refuse checkout "$DEPLOY_ROOT is a git checkout; remote operations are for distribution deployments (ALLOW_CHECKOUT=1 overrides)"
        return
    fi
    JOB_STEP=""
    case "$operation" in
        "$OPERATION_BACKUP") accept_backup ;;
        "$OPERATION_ROLLBACK") accept_rollback "$request" ;;
        *) accept_update "$request" ;;
    esac
}

accept_backup() {
    if ! disk_ok "$MIN_FREE_BYTES" 0; then
        refuse disk "$(free_bytes "$DEPLOY_ROOT") bytes free under $DEPLOY_ROOT; a snapshot needs at least $MIN_FREE_BYTES"
        return
    fi
    JOB_INSTALLED_BEFORE="$(installed_tree_version)"
    write_status accepted ""
    run_backup
}

accept_rollback() {
    # $1 = the request copy. The snapshot is named by a shape this agent
    # checks and must exist as a restorable directory; whether the database
    # is restored with the code is the request's choice, restore by default.
    local request="$1" snapshot restore_db
    snapshot="$(json_get "$request" args snapshot)"
    restore_db="$(json_get "$request" args restore_database)"
    if ! valid_snapshot "$snapshot"; then
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
    if ! disk_ok "$MIN_FREE_BYTES" 0; then
        refuse disk "$(free_bytes "$DEPLOY_ROOT") bytes free under $DEPLOY_ROOT; a rollback needs at least $MIN_FREE_BYTES for its safety snapshot"
        return
    fi
    JOB_SNAPSHOT="$snapshot"
    JOB_INSTALLED_BEFORE="$(installed_tree_version)"
    write_status accepted ""
    run_standalone_rollback "$restore_db"
}

installed_tree_version() {
    local version
    version="$(sed -n '/^__version__ = "/{s/^__version__ = "\([^"]*\)".*/\1/p;q;}' "$DEPLOY_ROOT/epicurrents/version.py" 2>/dev/null || true)"
    clean "${version:0:32}"
}

accept_update() {
    # $1 = the request copy.
    local request="$1" sha work copy have size rc token key err
    sha="$(json_get "$request" args package_sha256)"
    JOB_WINDOW="$(json_get "$request" args verify_window_minutes)"
    if ! [[ "$sha" =~ ^[0-9a-f]{64}$ ]]; then
        refuse hash "the request names no package hash"
        return
    fi
    rc=0
    size="$(spoolfs package-size "$sha" 2>"$STATE_DIR/work/.spoolfs.err")" || rc=$?
    err="$(cat "$STATE_DIR/work/.spoolfs.err" 2>/dev/null || true)"
    rm -f "$STATE_DIR/work/.spoolfs.err"
    case "$rc" in
        0) ;;
        4) refuse hash "no uploaded package has hash $sha"; return ;;
        *) refuse hash "the package with hash $sha cannot be read safely: $err"; return ;;
    esac
    # Twice the package plus the reserve under the deployment (update.sh
    # extracts beside its snapshots, and checks the unpacked size itself),
    # and the copy under the state directory.
    if ! disk_ok $((size * 2 + MIN_FREE_BYTES)) "$size"; then
        refuse disk "$(free_bytes "$DEPLOY_ROOT") bytes free under $DEPLOY_ROOT and $(free_bytes "$STATE_DIR") under $STATE_DIR; an update of a $size-byte package needs twice the package plus $MIN_FREE_BYTES, and the package once more for the agent's copy"
        return
    fi

    # Out of the uid-1000 tree before anything reads it as a package: the
    # copy is what gets verified, and the copy is what gets applied.
    work="$STATE_DIR/work/$JOB_ID"
    rm -rf "$work"
    mkdir -p "$work"
    copy="$work/package.tar.gz"
    if ! err="$(spoolfs fetch-package "$sha" "$work" "$size" 2>&1)"; then
        refuse hash "the package with hash $sha could not be copied safely: $err"
        return
    fi
    have="$(sha256_of "$copy")"
    if [ "$have" != "$sha" ]; then
        refuse hash "the package's sha256 is $have, the request names $sha"
        return
    fi

    job_note "checking the package ($sha)"
    set_key_args
    run_update_sh --check-archive "$copy" --require-signature "${KEY_ARGS[@]}" --require-newer && rc=0 || rc=$?
    if [ "$rc" -ne 0 ]; then
        token="$(fact refused)"
        [[ "$token" =~ ^[a-z_]{1,32}$ ]] || token=check
        refuse "$token" "update.sh refused the package: $(fact failed)"
        return
    fi
    JOB_INSTALLED_BEFORE="$(fact installed)"
    JOB_TARGET="$(fact version)"
    JOB_STEP=""
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
        job_note "the manifest's successor key has id $have but claims $(clean "$id"); not installed"
        return 0
    fi
    mv -f "$tmp" "$RELEASE_KEY_NEXT"
    job_note "successor release key installed (id ${have:-${id:-unknown}}); the next package may be signed with it"
    return 0
}

# ── The update ────────────────────────────────────────────────────────────────

window_minutes() {
    local minutes="$JOB_WINDOW"
    [[ "$minutes" =~ ^[0-9]{1,5}$ ]] || minutes="$VERIFY_WINDOW_MINUTES"
    [[ "$minutes" =~ ^[0-9]{1,5}$ ]] || minutes=30
    if [ "$minutes" -lt 5 ]; then minutes=5; fi
    if [ "$minutes" -gt 1440 ]; then minutes=1440; fi
    printf '%s' "$minutes"
}

run_update() {
    local copy="$STATE_DIR/work/$JOB_ID/package.tar.gz" rc
    write_active_lock
    if ! write_flag updating "The platform is being updated." ""; then
        job_note "the maintenance flag could not be raised, so the platform would keep accepting writes; nothing was changed"
        settle_failed spool_unwritable
        return
    fi
    stop_beat
    drain_worker
    JOB_STARTED_AT="$(now_iso)"
    write_status running ""
    set_key_args
    SNAPSHOT_INTO=snapshot
    run_update_sh --archive "$copy" --require-signature "${KEY_ARGS[@]}" --require-newer \
        --skip-beat --keep-lock --yes && rc=0 || rc=$?
    SNAPSHOT_INTO=""
    JOB_SNAPSHOT="$(snapshot_fact)"
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
    write_flag verifying "The platform was updated and is waiting for a superuser to confirm it." "$JOB_DEADLINE" \
        || job_note "the maintenance flag could not be moved to verifying"
    remove_active_lock
    JOB_STEP=""
    job_note "awaiting verification until $JOB_DEADLINE"
    write_status awaiting_verification ""
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

package_top() {
    # $1 = the verified package copy. Its wrapper directory, or nothing.
    # `|| true` inside the substitution: a package that is not a tarball is a
    # refusal update.sh already made, and here it must not end the run.
    (tar -tzf "$1" 2>/dev/null || true) | awk -F/ 'NR==1{sub(/^\.\//, ""); top=$1} END{print top}'
}

refresh_update_sh() {
    # $1 = the verified package copy. The agent's update.sh is replaced with
    # the one the package ships, so the next package finds the script it was
    # built for; the package was verified, which is the trust the copy needs.
    local top tmp
    top="$(package_top "$1")"
    if [ -z "$top" ]; then
        job_note "the package cannot be listed for an update.sh; the agent keeps its copy"
        return 0
    fi
    tmp="$(mktemp "$LIB_DIR/.update.sh.XXXXXX")"
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
    top="$(package_top "$1")"
    [ -n "$top" ] || return 0
    tmp="$(mktemp "$LIB_DIR/.epicurrents-updater.sh.XXXXXX")"
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
    readme="$(mktemp "$LIB_DIR/.README.md.XXXXXX")"
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
    local rc
    JOB_STARTED_AT="$(now_iso)"
    write_status running ""
    SNAPSHOT_INTO=snapshot
    run_update_sh --snapshot backup && rc=0 || rc=$?
    SNAPSHOT_INTO=""
    JOB_SNAPSHOT="$(snapshot_fact)"
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

take_safety_snapshot() {
    # $1 = label. 0 with JOB_POST_SNAPSHOT set when one exists or was taken.
    if [ -n "$JOB_POST_SNAPSHOT" ] && [ -d "$DEPLOY_ROOT/backups/$JOB_POST_SNAPSHOT" ]; then
        job_note "keeping the safety snapshot already taken: $JOB_POST_SNAPSHOT"
        return 0
    fi
    job_note "taking a $1 snapshot"
    SNAPSHOT_INTO=post_snapshot
    if run_update_sh --snapshot "$1" --keep-lock; then
        SNAPSHOT_INTO=""
        JOB_POST_SNAPSHOT="$(snapshot_fact)"
        if [ -n "$JOB_POST_SNAPSHOT" ]; then
            job_note "$1 snapshot: $JOB_POST_SNAPSHOT"
            return 0
        fi
    fi
    SNAPSHOT_INTO=""
    return 1
}

run_standalone_rollback() {
    # $1 = whether to restore the database ("false" keeps it). A safety
    # snapshot first, so what the live database holds now is recoverable
    # from somewhere; then update.sh restores the named snapshot; then the
    # gate against whatever version the restored tree carries.
    local restore_db="$1" rc restored
    write_active_lock
    write_flag rolling_back "The platform is being rolled back to an earlier snapshot." "" \
        || job_note "the maintenance flag could not be raised; rolling back regardless"
    stop_beat
    drain_worker
    JOB_STARTED_AT="${JOB_STARTED_AT:-$(now_iso)}"
    write_status running ""
    if ! take_safety_snapshot pre-rollback; then
        job_note "the safety snapshot failed; nothing was changed"
        settle_failed snapshot_failed
        return
    fi
    if [ "$restore_db" = false ]; then
        job_note "restoring the code of $JOB_SNAPSHOT; the database is kept"
        run_update_sh --rollback --snapshot "$JOB_SNAPSHOT" --code-only --yes --skip-beat --keep-lock && rc=0 || rc=$?
        if [ "$rc" -ne 0 ] && [ "$(fact refused)" = code_only ]; then
            job_note "update.sh declined to keep the database: $(fact failed)"
            settle_failed refused_code_only
            return
        fi
    else
        job_note "restoring $JOB_SNAPSHOT"
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
    # The markers count only while the agent is enabled; the deadline counts
    # regardless, since a disabled agent must not leave the platform in its
    # verification window for ever.
    if [ "$ENABLED" = 1 ] && marker_present verify; then
        job_note "confirmed from the platform"
        settle_succeeded
    elif [ "$ENABLED" = 1 ] && marker_present rollback; then
        job_note "rollback requested from the platform"
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

newest_settled() {
    # $1 = job id. 0 when no update or rollback settled after it: a late
    # rollback of an older update would restore a tree that a newer job has
    # already replaced.
    python3 - "$STATE_JOBS" "$1" <<'PY'
import json, os, sys
root, mine = sys.argv[1:3]
newest, newest_id = "", None
for name in os.listdir(root):
    if not name.endswith(".status.json"):
        continue
    try:
        with open(os.path.join(root, name), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        continue
    if data.get("operation") not in ("platform.update", "platform.rollback"):
        continue
    if data.get("state") not in ("succeeded", "rolled_back"):
        continue
    finished = data.get("finished_at") or ""
    if finished > newest:
        newest, newest_id = finished, data.get("job_id")
sys.exit(0 if newest_id == mine else 1)
PY
}

handle_succeeded() {
    # A rollback may still be asked for after confirmation, while the
    # pre-update snapshot exists, nothing else is in flight, and no update or
    # rollback has settled since.
    [ "$ENABLED" = 1 ] || return 0
    marker_present rollback || return 0
    if in_flight_elsewhere "$JOB_ID"; then
        decline_marker rollback "late rollback refused: another job is in flight"
    elif ! newest_settled "$JOB_ID"; then
        decline_marker rollback "late rollback refused: a later update or rollback has settled since this job"
    elif [ -n "$JOB_SNAPSHOT" ] && [ -d "$DEPLOY_ROOT/backups/$JOB_SNAPSHOT" ]; then
        job_note "late rollback requested from the platform"
        rollback late true
    else
        decline_marker rollback "rollback requested, but the snapshot '${JOB_SNAPSHOT:-none}' no longer exists; nothing to roll back to"
    fi
}

# ── Rollback ──────────────────────────────────────────────────────────────────

rollback() {
    # $1 = reason, $2 = whether the platform served during a window (true when
    # the job reached awaiting_verification). The post-update snapshot is
    # what keeps that window's data recoverable, so when there was a window
    # its failure is a failed rollback; when there was none there is nothing
    # to preserve and the rollback goes ahead without it.
    local reason="$1" had_window="$2" rc
    write_active_lock
    write_flag rolling_back "The platform is being rolled back to the previous release." "" \
        || job_note "the maintenance flag could not be raised; rolling back regardless"
    remove_marker rollback
    JOB_STEP=""
    write_status rolling_back "$reason"
    if [ -z "$JOB_SNAPSHOT" ] || [ ! -d "$DEPLOY_ROOT/backups/$JOB_SNAPSHOT" ]; then
        job_note "no pre-update snapshot to restore ('${JOB_SNAPSHOT:-none}'); the rollback needs a shell"
        settle_rollback_failed
        return
    fi
    stop_beat
    drain_worker
    if ! take_safety_snapshot post-update; then
        if [ "$had_window" = true ]; then
            job_note "the post-update snapshot failed; refusing to roll back over data written since the update"
            settle_rollback_failed
            return
        fi
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
        valid_snapshot "$(basename "${dirs[i]}")" || continue
        [ ! -L "${dirs[i]}" ] || continue
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
    if ! [[ "$job" =~ $UUID_RE ]]; then
        log "stale lock names no job; removing it"
        remove_active_lock
        return 0
    fi
    if [ ! -f "$STATE_JOBS/$job.status.json" ]; then
        # Died between taking the lock and the first status: at most the
        # request was claimed, and nothing else happened.
        reset_job "$job"
        JOB_OPERATION="$(json_get "$(job_request)" operation)"
        JOB_OPERATION="$(clean "${JOB_OPERATION:-unknown}")"
        job_note "the run that claimed this job (pid ${pid:-?}) is gone before it checked anything; nothing was changed"
        remove_active_lock
        finish failed stale
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
            if [ "$JOB_STATE" = requested ]; then
                job_note "the request was interrupted while being checked; nothing was changed"
                remove_active_lock
                finish failed stale
            elif [ "$JOB_RETRIES" -lt 1 ]; then
                JOB_RETRIES=$((JOB_RETRIES + 1))
                job_note "retrying the interrupted rollback"
                run_standalone_rollback "$(json_get "$(job_request)" args restore_database)"
            else
                job_note "the rollback was interrupted and already retried once; the deployment needs a shell"
                settle_rollback_failed
            fi
            return 0
            ;;
    esac
    case "$JOB_STATE" in
        requested|accepted|running)
            if [ -n "$JOB_SNAPSHOT" ]; then
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
    # Whether any job other than $1 is in a state this agent still owns,
    # according to the agent's own records.
    local path id state
    for path in "$STATE_JOBS"/*.status.json; do
        [ -f "$path" ] || continue
        id="$(basename "$path" .status.json)"
        [ "$id" != "$1" ] || continue
        state="$(json_get "$path" state)"
        case "$state" in
            requested|accepted|running|awaiting_verification|rolling_back) return 0 ;;
        esac
    done
    return 1
}

tick() {
    local path id state
    # Requests first, oldest first, so a queue drains in order; a request
    # while another job is in flight waits for its turn. One request per
    # tick: the next is looked at once this one has reached a state that
    # lets another start.
    for id in $(spoolfs requests 2>/dev/null || true); do
        [[ "$id" =~ $UUID_RE ]] || continue
        # A request for a job the agent already knows is the platform's
        # business, not a new job; it is never taken twice.
        [ ! -f "$STATE_JOBS/$id.status.json" ] || continue
        if in_flight_elsewhere "$id"; then
            break
        fi
        reset_job "$id"
        if claim_request; then
            accept_or_refuse
        fi
        break
    done
    # Jobs in their window, or confirmed and asked to roll back after all,
    # from the agent's own records; the in-flight ones are published again
    # in case their copy in the spool went missing.
    for path in "$STATE_JOBS"/*.status.json; do
        [ -f "$path" ] || continue
        id="$(basename "$path" .status.json)"
        [[ "$id" =~ $UUID_RE ]] || continue
        reset_job "$id"
        load_status
        state="$JOB_STATE"
        case "$state" in
            requested|accepted|running|awaiting_verification|rolling_back)
                publish "jobs/$id.status.json" "$path" || true
                ;;
        esac
        [ "$JOB_OPERATION" = "$OPERATION_UPDATE" ] || continue
        case "$state" in
            awaiting_verification) handle_awaiting ;;
            succeeded) handle_succeeded ;;
        esac
    done
    prune_state
}

prune_state() {
    # The agent's copies of logs and requests of jobs finished more than 90
    # days ago; the status files stay, since they are what a late rollback is
    # checked against and they are small.
    local path id state
    for path in "$STATE_JOBS"/*.log "$STATE_JOBS"/*.request.json; do
        [ -f "$path" ] || continue
        [ -n "$(find "$path" -mtime +90 2>/dev/null)" ] || continue
        id="$(basename "$path")"
        id="${id%%.*}"
        state="$(json_get "$STATE_JOBS/$id.status.json" state)"
        case "$state" in
            succeeded|failed|rolled_back|rollback_failed|cancelled) rm -f "$path" ;;
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
    handle_stale_lock || exit 0
    tick
}

main "$@"
