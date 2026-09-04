#!/usr/bin/env bash
# lock-requirements.sh — regenerate a hash-pinned requirements lock
#
# Usage:
#   ./scripts/lock-requirements.sh                    — write requirements.lock
#   ./scripts/lock-requirements.sh --check            — fail if that lock is stale (CI)
#   ./scripts/lock-requirements.sh --project <name>   — write projects/<name>/requirements.lock
#   ./scripts/lock-requirements.sh --project <name> --check
#
# requirements.txt pins versions but not artifacts, so two builds of the same
# commit can install different transitive trees and pip-audit reports on a
# resolution nobody will necessarily get. The lock pins every package in the
# closure to a specific set of hashes, and the Dockerfile installs it with
# --require-hashes so a substituted artifact fails the build rather than
# shipping.
#
# Runs uv inside a container pinned to the image's Python, so the resolution
# does not depend on what happens to be installed on the machine running this,
# and contributors need no uv on the host. --universal resolves across
# platforms rather than for this machine's, so the same lock installs on the
# arm64 laptop and the amd64 deploy host.
#
# --project locks the active project's extra dependencies, which live in its own
# repository and install as a further pip invocation. That resolution is not
# independent of this one and must not be run as if it were. The two closures
# overlap — numpy is in the platform's and in any project doing array work — and
# pip does not treat the overlap as a conflict: the project's install simply
# replaces whatever the platform lock put there, succeeds, and says nothing. So
# the project resolves against the platform lock's exact versions as
# constraints, not against requirements.txt, whose ranges (numpy>=2.5.1,<3) are
# precisely wide enough to let the two disagree.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/.."

PYTHON_IMAGE="python:3.14-slim"
UV_VERSION="0.12.5"
PLATFORM_LOCK="requirements.lock"
CHECK_ONLY=0
PROJECT=""

usage() { printf 'Usage: %s [--project <name>] [--check]\n' "$0" >&2; }

while [ $# -gt 0 ]; do
    case "$1" in
        --check) CHECK_ONLY=1; shift ;;
        --project)
            # Rejected up front rather than left to expand to an empty path: a
            # bare --project would otherwise resolve to projects//requirements.txt
            # and fail somewhere less obvious.
            if [ $# -lt 2 ] || [ -z "${2:-}" ]; then
                printf '\n--project needs a project name\n' >&2
                usage
                exit 2
            fi
            PROJECT="$2"; shift 2 ;;
        --project=*) PROJECT="${1#*=}"; shift ;;
        # Anything else is a typo, and the regenerate branch overwrites the lock —
        # a mistyped --check must not silently become "rewrite it".
        *) printf '\nUnknown argument: %s\n' "$1" >&2; usage; exit 2 ;;
    esac
done

info() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
ok()   { printf '    \033[32m✓\033[0m  %s\n'  "$*"; }
die()  { printf '\n\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

command -v docker > /dev/null 2>&1 || die "docker is not on PATH"

# Installed before anything is created rather than beside the first mktemp, so
# that it covers every exit in between: the constraints file is built early —
# the --check branch needs its digest — and several guards die between there and
# the resolve, each of which would otherwise leave it in the tree.
CONSTRAINTS=""
TMP=""
cleanup() { rm -f ${TMP:+"$TMP" "${TMP}.final"} ${CONSTRAINTS:+"$CONSTRAINTS"}; }
trap cleanup EXIT

# ── What is being locked ─────────────────────────────────────────────────────

if [ -n "$PROJECT" ]; then
    INPUT="projects/$PROJECT/requirements.txt"
    LOCK="projects/$PROJECT/requirements.lock"
    [ -d "projects/$PROJECT" ] || die "projects/$PROJECT/ does not exist — clone the project first"
    [ -f "$INPUT" ] || die "$INPUT does not exist; a project with no extra dependencies needs no lock"
    [ -f "$PLATFORM_LOCK" ] || die "$PLATFORM_LOCK does not exist — generate it before locking a project"
    # Created inside the tree because the container mounts $PWD and nothing else,
    # and named as a lock sibling so the .gitignore entry that covers the
    # regenerate branch's temporaries covers this too.
    #
    # The name is fixed rather than mktemp'd, which is the opposite of what a
    # scratch file usually wants. uv annotates each requirement with the file it
    # came from, so the constraints file's own name is written into the lock — a
    # random one makes every regeneration differ from the last and --check can
    # never pass. It reads as provenance in the output, so it is named for what
    # it holds. Truncating on open makes a leftover from an aborted run
    # harmless, which is the property mktemp was providing.
    CONSTRAINTS="${PLATFORM_LOCK}.platform-versions"
    # Every pinned line of the platform lock, minus the hashes: `pkg==version`
    # and its environment marker, which is exactly what a constraints file wants.
    # The version pins are what the project must resolve against; the hashes
    # belong to the platform's own install and would be rejected here.
    sed -E 's/[[:space:]]*\\$//' "$PLATFORM_LOCK" \
        | grep -E '^[a-zA-Z0-9._-]+==' > "$CONSTRAINTS"
    [ -s "$CONSTRAINTS" ] || die "$PLATFORM_LOCK yielded no version pins — is it truncated?"
else
    INPUT="requirements.txt"
    LOCK="$PLATFORM_LOCK"
fi

# The resolution is pinned to a cutoff instant rather than "now". requirements.txt
# pins ranges and most of the closure is transitive with no pin at all, so a
# re-resolve against live PyPI drifts the moment anything upstream publishes —
# which would turn --check red on pull requests that changed nothing. Regenerating
# stamps the current time and so deliberately picks up new releases; --check reads
# the stamp back out of the lock and resolves against that same instant, making it
# a comparison against the recorded state rather than against today's PyPI.
CUTOFF_MARKER="# resolved-at: "

# The digest of the version pins a project lock was resolved against, so that a
# project lock left behind by a platform relock is a --check failure rather than
# a silent downgrade at install time. Taken over the derived constraints and not
# over requirements.lock itself: the lock's own resolved-at line moves on every
# regeneration, so hashing the file would invalidate every project lock whenever
# the platform re-resolved to the same versions.
PINS_MARKER="# platform-versions: "

digest() {
    # sha256sum on Linux, shasum on macOS. Both print "<hex>  <name>".
    if command -v sha256sum > /dev/null 2>&1; then
        sha256sum "$1" | cut -d' ' -f1
    else
        shasum -a 256 "$1" | cut -d' ' -f1
    fi
}

if [ -n "$PROJECT" ]; then
    PINS_DIGEST="$(digest "$CONSTRAINTS")"
fi

if [ "$CHECK_ONLY" -eq 1 ]; then
    [ -f "$LOCK" ] || die "$LOCK does not exist — run scripts/lock-requirements.sh"
    # awk rather than `sed | head`: head closes the pipe after the first match,
    # sed takes SIGPIPE, and pipefail plus set -e would abort the script before
    # the guard below could say why. Reading to EOF avoids that entirely.
    CUTOFF="$(awk -v m="$CUTOFF_MARKER" 'index($0, m) == 1 && !found { sub(m, ""); v = $0; found = 1 } END { print v }' "$LOCK")"
    [ -n "$CUTOFF" ] || die "$LOCK carries no ${CUTOFF_MARKER}line — regenerate it"
    # The cutoff is read out of a file and then interpolated into the container's
    # shell command, so it is untrusted input on this path: on a pull request the
    # lock arrives from the branch under test, and a value carrying a quote would
    # inject shell into the CI runner's container. Constrain it to the exact
    # timestamp shape the regenerate branch writes and refuse anything else.
    case "$CUTOFF" in
        [0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z) ;;
        *) die "$LOCK has a malformed ${CUTOFF_MARKER}value; expected YYYY-MM-DDTHH:MM:SSZ" ;;
    esac
    # Reported before the diff so the message names the cause. The diff would
    # catch the same drift — every constrained package moves with the platform —
    # but it reads as "this lock is stale", which points at the project's own
    # requirements.txt rather than at the platform relock that actually moved.
    if [ -n "$PROJECT" ]; then
        STORED_PINS="$(awk -v m="$PINS_MARKER" 'index($0, m) == 1 && !f { sub(m, ""); v = $0; f = 1 } END { print v }' "$LOCK")"
        [ -n "$STORED_PINS" ] || die "$LOCK carries no ${PINS_MARKER}line — regenerate it"
        [ "$STORED_PINS" = "$PINS_DIGEST" ] || die \
            "$PLATFORM_LOCK has moved since $LOCK was resolved against it.
       Re-run: scripts/lock-requirements.sh --project $PROJECT
       Until then the project's install can silently downgrade a package the
       platform lock pins."
    fi
else
    CUTOFF="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
fi

if [ -n "$PROJECT" ]; then
    HEADER="# Hash-pinned dependency lock for the $PROJECT project — GENERATED, do not edit by hand.
#
# Regenerate with scripts/lock-requirements.sh --project $PROJECT after any
# change to this project's requirements.txt, and again after any regeneration of
# the platform's requirements.lock. The Dockerfile installs this as a further
# pip invocation with --require-hashes.
#
# Resolved against the platform lock's exact versions as constraints. Packages in
# both closures therefore appear here at the version the platform already
# installed, so pip finds them satisfied and leaves them alone. Resolved without
# those constraints, an overlapping package would install cleanly at a different
# version and replace the platform's — pip reports no conflict, and nothing
# downstream notices.
#
# resolved-at is the PyPI cutoff this resolution used. --check re-resolves against
# the same instant so it compares like with like; regenerating moves it forward.
# platform-versions is the digest of those constraints; --check fails when it no
# longer matches the platform lock, which is what makes the coupling above
# visible instead of expiring quietly.
${CUTOFF_MARKER}${CUTOFF}
${PINS_MARKER}${PINS_DIGEST}
"
else
    HEADER="# Hash-pinned dependency lock — GENERATED, do not edit by hand.
#
# Regenerate with scripts/lock-requirements.sh after any change to
# requirements.txt or constraints.txt. Installed by the Dockerfile with
# --require-hashes, so an artifact that does not match fails the build.
#
# Excludes requirements-test.txt (installed only in the image's test stage).
#
# resolved-at is the PyPI cutoff this resolution used. --check re-resolves against
# the same instant so it compares like with like; regenerating moves it forward.
${CUTOFF_MARKER}${CUTOFF}
"
fi

info "Resolving ${INPUT} with uv ${UV_VERSION} in ${PYTHON_IMAGE} as of ${CUTOFF}"
# Created beside the lock rather than in $TMPDIR so the final mv is a rename
# within one filesystem, and therefore atomic. Across filesystems mv degrades to
# copy-then-unlink, and an interrupt mid-write leaves a truncated lock that the
# next --check would faithfully diff against.
#
# The Xs end the template because BSD mktemp substitutes only a trailing run and
# otherwise takes the name literally, so the earlier `.XXXXXX.tmp` randomised
# under GNU and was a fixed name on macOS. Nothing depended on it, since this
# name never reaches the output — but a fixed scratch name is one concurrent run
# away from two of them writing the same file.
TMP="$(mktemp "${LOCK}.XXXXXX")"

# The constraint flag is built as a variable rather than branching the docker
# run, so both paths use one invocation and cannot drift in the flags they share.
# The paths are the container's: $PWD is mounted at /w.
CONSTRAIN=""
if [ -n "$CONSTRAINTS" ]; then
    CONSTRAIN="-c '/w/$(basename "$CONSTRAINTS")'"
fi

docker run --rm -v "$PWD":/w:ro -w /w "$PYTHON_IMAGE" sh -ec "
    pip install -q 'uv==${UV_VERSION}'
    uv pip compile --universal --generate-hashes --no-header --quiet \
        --exclude-newer '${CUTOFF}' ${CONSTRAIN} \
        -o /tmp/generated.lock '${INPUT}'
    cat /tmp/generated.lock
" > "$TMP"

[ -s "$TMP" ] || die "uv produced an empty lock"

printf '%s\n' "$HEADER" | cat - "$TMP" > "${TMP}.final"

if [ "$CHECK_ONLY" -eq 1 ]; then
    if ! diff -q "$LOCK" "${TMP}.final" > /dev/null 2>&1; then
        printf '\n'
        diff -u "$LOCK" "${TMP}.final" | head -40 || true
        rm -f "${TMP}.final"
        die "$LOCK is stale — run scripts/lock-requirements.sh${PROJECT:+ --project $PROJECT} and commit the result"
    fi
    rm -f "${TMP}.final"
    ok "$LOCK is up to date"
    exit 0
fi

mv "${TMP}.final" "$LOCK"
ok "Wrote $LOCK ($(grep -c '^[a-zA-Z0-9]' "$LOCK") packages pinned)"
