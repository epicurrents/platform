# shellcheck shell=bash
# compose.sh — the one statement of how a host script spells its compose command.
#
# Sourced, never executed, and sourcing it does nothing but define functions.
# Safe under `set -euo pipefail`, and written for bash 3.2 so it runs on macOS.
#
# Docker or Podman is settled the way scripts/update.sh settles it, which is the
# reference: podman-docker installs a `docker` that answers by execing podman,
# so the name in the version string is the discriminator, not whether the
# command exists. Podman runs rootful, because every service runs as the
# deployment account against a bind mount and only a rootful runtime maps that
# uid to the deployment's owner — through sudo unless the caller is root
# already, since a root-owned caller's host may not have sudo at all.
#
# Who sources this and who cannot. The checkout-only operator scripts do:
# switch_project.sh, enable_plugin.sh, disable_plugin.sh, rebuild-frontend.sh.
# update.sh, the updater agent and the start.sh a package carries each keep
# their own copy of the detection, because each runs as a single file with no
# lib/ beside it — update.sh at a package's root and as the agent's root-owned
# copy, and a release is applied by the previous release's update.sh, which
# would not know to install a new file first. scripts/tests/test_compose_lib.py
# runs those copies against this file for every runtime case, so they cannot
# drift from it unnoticed. Change the logic here and there together.

# Set CONTAINER_RUNTIME (docker, podman, or empty) and COMPOSE, an array holding
# the compose command with no -f flags. Always returns 0. With no runtime on
# PATH, CONTAINER_RUNTIME is empty and COMPOSE falls back to `docker compose`,
# as update.sh has it: a script that drives a container then calls
# compose_require_runtime, and one that only prints a command still has one.
compose_detect_runtime() {
    if command -v docker >/dev/null 2>&1 && ! docker --version 2>&1 | grep -qi podman; then
        CONTAINER_RUNTIME=docker
        COMPOSE=(docker compose)
    elif command -v podman >/dev/null 2>&1; then
        CONTAINER_RUNTIME=podman
        if [ "$(id -u)" = 0 ]; then
            COMPOSE=(podman compose)
        else
            COMPOSE=(sudo -E podman compose)
        fi
    else
        # shellcheck disable=SC2034  # read by the sourcing script.
        CONTAINER_RUNTIME=""
        COMPOSE=(docker compose)
    fi
    return 0
}

# Refuse, in update.sh's words, when compose_detect_runtime found no runtime.
# Returns 1 rather than exiting, so the caller decides: `compose_require_runtime
# || exit 1`. Call it before the first change a script makes.
compose_require_runtime() {
    if [ -n "${CONTAINER_RUNTIME:-}" ]; then
        return 0
    fi
    echo "Error: No container runtime found — this needs Docker Engine or Podman, either one with Compose v2." >&2
    return 1
}

# Append the compose files of a deployed stack to COMPOSE, as update.sh layers
# them: the base file and the production overlay always, and the TLS proxy
# overlay when the .env named in $1 (default ./.env) carries a PROXY_DOMAIN
# value. The file names are relative, so the caller has changed into the
# deployment root. A stack brought up with the proxy overlay has to be driven
# with it, or `up -d` treats the running caddy container as an orphan.
compose_add_production_overlays() {
    local env_file="${1:-.env}"
    COMPOSE+=(-f docker-compose.yml -f docker-compose.prod.yml)
    if [ -f "$env_file" ] && grep -qE '^PROXY_DOMAIN=[^[:space:]]' "$env_file"; then
        COMPOSE+=(-f docker-compose.proxy.yml)
    fi
    return 0
}

# Print COMPOSE as one line an operator can paste: each word shell-quoted, so a
# root path with a space in it survives the copy.
compose_command_line() {
    local word line=""
    for word in "${COMPOSE[@]}"; do
        line="$line$(printf '%q' "$word") "
    done
    printf '%s' "${line% }"
}
