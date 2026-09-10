#!/usr/bin/env bash
# bootstrap_plan.sh — the parts of a bootstrap run that do not depend on which
# container runtime drives it: argument parsing, the .env-derived state the later
# steps key off, and the shared tail of the progress plan.
#
# Sourced early by scripts/bootstrap.sh and scripts/bootstrap-podman.sh. Those two
# differ in how they install prerequisites and in how COMPOSE is spelled, and in
# nothing else; everything they share after that point lives here and in
# bootstrap_steps.sh. Splitting the shared half in two is what lets each caller
# put its own steps in the middle: the plan has to be complete before
# progress_begin renders it, and the prerequisites have to be installed before the
# steps that use them run.
#
# Requires progress.sh to be sourced first — die() and progress_step() come from
# there.

#: Parse the flags both bootstrap scripts accept, setting START, TS_AUTHKEY_ARG,
#: TS_HOSTNAME_ARG and TS_MODE. --help prints the calling script's own header
#: block, so each keeps its own usage text.
# shellcheck disable=SC2034  # TS_* are read by bootstrap_steps.sh, sourced later.
bootstrap_parse_args() {
    START=true
    TS_AUTHKEY_ARG=""
    TS_HOSTNAME_ARG=""
    TS_MODE="join"
    while [ $# -gt 0 ]; do
        case "$1" in
            --no-start) START=false; shift ;;
            --tailscale-authkey)    TS_AUTHKEY_ARG="${2:-}"; shift 2 ;;
            --tailscale-authkey=*)  TS_AUTHKEY_ARG="${1#*=}"; shift ;;
            --tailscale-hostname)   TS_HOSTNAME_ARG="${2:-}"; shift 2 ;;
            --tailscale-hostname=*) TS_HOSTNAME_ARG="${1#*=}"; shift ;;
            --tailscale-mode)       TS_MODE="${2:-}"; shift 2 ;;
            --tailscale-mode=*)     TS_MODE="${1#*=}"; shift ;;
            -h|--help)
                # Print the header comment block (everything up to the first
                # non-comment line), sans the shebang and leading '# '.
                awk 'NR > 1 && !/^#/ { exit } NR > 1 { sub(/^# ?/, ""); print }' "$0"
                exit 0
                ;;
            *) die "Unknown argument: $1 (try --help)" ;;
        esac
    done

    case "$TS_MODE" in
        join|serve) ;;
        *) die "--tailscale-mode must be 'join' or 'serve' (got '$TS_MODE')." ;;
    esac

}

#: Refuse to run as root. A deployment whose files are owned by root is one the
#: unprivileged containers cannot read. On the Podman path the stack is rootful
#: through sudo on each compose call, never by this script being root.
bootstrap_require_non_root() {
    if [ "$(id -u)" -eq 0 ]; then
        die "Run this script as a regular user, not root. sudo will be used where needed."
    fi
}

#: Read the deployment state the later steps branch on, out of .env.
bootstrap_env_state() {
    FIRST_RUN=false
    if [ ! -f .env ]; then
        FIRST_RUN=true
    fi

    ACTIVE_PROJECT=""
    ACTIVE_PLUGINS=""
    DICOM_ENABLED=false
    if [ "$FIRST_RUN" = false ]; then
        ACTIVE_PROJECT="$(grep -E '^EPICURRENTS_PROJECT=' .env | head -1 | cut -d= -f2 | tr -d ' "'"'"'')"
        # `|| true` so a pre-existing .env without the EPICURRENTS_PLUGINS line
        # (upgraded deployment) does not trip `set -e -o pipefail` on the failed grep.
        ACTIVE_PLUGINS="$(grep -E '^EPICURRENTS_PLUGINS=' .env | head -1 | cut -d= -f2 | tr -d ' "'"'"'' || true)"
        # DICOM ships its OHIF viewer as a `update = none` submodule; fetch it when
        # the dicom plugin is enabled. Match `dicom` as a whole comma-separated
        # entry so a name like `dicom-foo` does not trigger it.
        case ",$ACTIVE_PLUGINS," in
            *,dicom,*) DICOM_ENABLED=true ;;
        esac
    fi
}

#: Declare the progress steps every runtime shares. Each caller declares its own
#: prerequisite steps first, so those render above these in the checklist.
bootstrap_plan_common() {
    progress_step subs   "Initialise submodules (viewer + docs)"
    if [ -z "${SKIP_DEV_TOOLS_INSTALL:-}" ]; then
        progress_step devtools "Install dev tooling (git hooks + AI-tool symlinks)"
    fi
    # Declared before the image step because the clone has to land before the build:
    # the image installs the project's requirements and COPYs its source. Guarded on
    # ACTIVE_PROJECT, which is empty on the first pass — that pass ends at the .env
    # review and never reaches either step.
    if [ -n "$ACTIVE_PROJECT" ] && [ ! -d "projects/$ACTIVE_PROJECT" ]; then
        progress_step project_clone "Clone project ($ACTIVE_PROJECT)"
    fi
    progress_step image "Build the Python image"
    if [ "$FIRST_RUN" = true ]; then
        progress_step envgen "Generate .env with random secrets"
    else
        if [ "$DICOM_ENABLED" = true ]; then
            progress_step ohif "Initialise OHIF viewer submodule"
        fi
        progress_step frontend "Build frontend bundles"
        progress_step pyodide  "Vendor the Pyodide runtime"
        progress_step borg     "Initialise Borg backup repositories"
        if [ "$START" = true ]; then
            if [ -n "$ACTIVE_PROJECT" ]; then
                progress_step activate "Activate project ($ACTIVE_PROJECT)"
            fi
            progress_step up "Start the stack (production overlay)"
            progress_step leadfields "Generate the static lead fields"
            if [ -n "$TS_AUTHKEY_ARG" ]; then
                if [ "$TS_MODE" = "serve" ]; then
                    progress_step tailnet "Publish the UI on the tailnet" direct
                else
                    progress_step tailnet "Join the tailnet" direct
                fi
            fi
        fi
    fi
}

#: Render the plan and start the run.
bootstrap_begin() {
    if [ "$FIRST_RUN" = true ]; then
        progress_begin "Epicurrents bootstrap — first pass (pauses after generating .env)"
    else
        progress_begin "Epicurrents bootstrap"
    fi
}
