#!/usr/bin/env bash
# install-docker.sh — Docker Engine installation, shared by the two callers that
# need it and framework-free so both can source it.
#
# Sourced, not executed. It defines functions and sets no traps, no `set -e` and
# no output framing, because its callers disagree about all three: bootstrap.sh
# wraps the call in the step/progress renderer from lib/progress.sh, while a
# distribution's prepare-host.sh has no framework at all and prints plainly.
#
# Two callers, one definition:
#   scripts/bootstrap.sh              — a cloned repository, run by a non-root
#                                       user who has sudo
#   prepare-host.sh in a distribution — a packaged deployment, run as root on a
#                                       fresh server
#
# That difference is why nothing here calls `sudo` directly. A minimal cloud
# image may not have sudo installed at all, and running it as root would fail on
# exactly the machine the distribution path targets; `_docker_sudo` resolves to
# nothing when already root.
#
# The version floor is the one platform-wide fact this file exists to keep in a
# single place: Engine 25 is where volume subpath support lands, and below it the
# compose files are accepted and mount the wrong thing.

DOCKER_MIN_MAJOR=25
DOCKER_APT_REPO="https://download.docker.com/linux/ubuntu"
DOCKER_APT_PACKAGES="docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin"

# Privilege escalation only when it is actually needed. Root gets an empty
# prefix, which is what makes this usable on an image with no sudo.
_docker_sudo() {
    if [ "$(id -u)" -eq 0 ]; then
        "$@"
    else
        sudo "$@"
    fi
}

docker_is_installed() {
    command -v docker >/dev/null 2>&1
}

# Server version as reported by the daemon, empty when it cannot be reached.
# Both spellings are tried because group membership added in this session does
# not apply until the next login, so the unprivileged call can fail on a host
# where the privileged one succeeds.
docker_server_version() {
    docker version --format '{{.Server.Version}}' 2>/dev/null \
        || _docker_sudo docker version --format '{{.Server.Version}}' 2>/dev/null \
        || true
}

# Install Docker Engine from Docker's own apt repository. Idempotent: an existing
# installation is left alone, including one from another source, since replacing
# a working engine is not this function's business.
#
# Returns non-zero without installing anything when the host is not apt-based —
# the caller decides whether that is fatal, because it is on a server and is not
# on a developer's macOS machine, where Docker Desktop is the answer.
install_docker_engine() {
    if docker_is_installed; then
        echo "Docker already installed: $(docker --version 2>/dev/null || _docker_sudo docker --version)"
        return 0
    fi

    if ! command -v apt-get >/dev/null 2>&1; then
        echo "Docker Engine is not installed and apt-get is unavailable." >&2
        echo "Install Docker manually (Docker Desktop on macOS/Windows) and run this again." >&2
        return 1
    fi

    echo "Installing Docker Engine from Docker's official repository…"

    _docker_sudo apt-get update -y -qq
    _docker_sudo apt-get install -y -qq ca-certificates curl gnupg lsb-release

    _docker_sudo install -m 0755 -d /etc/apt/keyrings
    curl -fsSL "${DOCKER_APT_REPO}/gpg" | _docker_sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    _docker_sudo chmod a+r /etc/apt/keyrings/docker.gpg

    local codename arch
    # shellcheck disable=SC1091
    codename="$(. /etc/os-release && echo "$VERSION_CODENAME")"
    arch="$(dpkg --print-architecture)"
    echo "deb [arch=${arch} signed-by=/etc/apt/keyrings/docker.gpg] ${DOCKER_APT_REPO} ${codename} stable" \
        | _docker_sudo tee /etc/apt/sources.list.d/docker.list >/dev/null

    _docker_sudo apt-get update -y -qq
    # shellcheck disable=SC2086
    _docker_sudo apt-get install -y $DOCKER_APT_PACKAGES

    _docker_sudo systemctl enable --now docker
}

# Confirm the running engine is new enough. Separated from installation because
# an engine that was already present is the case that needs checking — a fresh
# install from the repository above is current by construction.
#
# An unreachable daemon and an unparseable version are distinct failures and say
# so: the first is usually a stopped service or a group membership that has not
# taken effect yet, the second means the check itself needs revisiting.
require_docker_engine_version() {
    local version major
    version="$(docker_server_version)"

    if [ -z "$version" ]; then
        echo "The Docker daemon is not reachable. Check that it is running, and that this" >&2
        echo "user is in the docker group — group membership applies only to a new login" >&2
        echo "session, so it needs a fresh login after usermod." >&2
        return 1
    fi

    major="${version%%.*}"
    case "$major" in
        ""|*[!0-9]*)
            echo "Cannot parse the Docker Engine version ($version); skipping the version check." >&2
            # Still the version on stdout: callers capture it for their summary, and
            # an empty capture would report a blank engine rather than an odd one.
            echo "$version"
            return 0
            ;;
    esac

    if [ "$major" -lt "$DOCKER_MIN_MAJOR" ]; then
        echo "Docker Engine ${DOCKER_MIN_MAJOR}+ is required for volume subpath support (found $version)." >&2
        return 1
    fi

    echo "$version"
}
