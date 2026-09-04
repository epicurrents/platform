#!/usr/bin/env bash
# lint-shell.sh — run shellcheck + bash -n on the repository's shell scripts.
#
# Uses the host's shellcheck if available; otherwise falls back to the
# upstream Docker image so developers don't need to install shellcheck
# locally. CI installs shellcheck natively (it's a single apt package
# on Ubuntu) and skips the Docker path.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/.."

run_shellcheck() {
    if command -v shellcheck >/dev/null 2>&1; then
        shellcheck "$@"
    else
        echo "shellcheck not on PATH — running in Docker (koalaman/shellcheck:stable)" >&2
        docker run --rm -v "$(pwd):/work" -w /work \
            koalaman/shellcheck:stable "$@"
    fi
}

echo "==> shellcheck scripts/*.sh scripts/lib/*.sh borgmatic/*.sh examples/hetzner/*.sh"
run_shellcheck scripts/*.sh scripts/lib/*.sh borgmatic/*.sh examples/hetzner/*.sh
echo "==> bash -n scripts/*.sh scripts/lib/*.sh borgmatic/*.sh examples/hetzner/*.sh"
for f in scripts/*.sh scripts/lib/*.sh borgmatic/*.sh examples/hetzner/*.sh; do
    bash -n "$f"
done
echo "    ok"
