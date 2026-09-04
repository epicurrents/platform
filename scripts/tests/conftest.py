"""Fixtures for testing the bootstrap shell scripts via mocked binaries.

The ``fakebin`` fixture creates an isolated directory of stub
executables that log every invocation to a call log. Tests then run the
target script with ``PATH=<fakebin>:/usr/bin:/bin`` and assert on the
log: was the right command invoked, with the right flags, in the right
order, for the relevant code branch.

Pattern:

    def test_first_pass_writes_env(fakebin, tmp_path):
        ... # set up the repo skeleton and .env.example
        result = run_script(BOOTSTRAP, fakebin, cwd=tmp_path)
        assert result.returncode == 0
        assert any("docker compose build web" in c for c in fakebin.calls())

The default stub set covers the binaries every bootstrap script touches
(sudo, dnf, apt-get, curl, podman, docker, …). Override or add stubs
with ``fakebin.stub(name, body=...)`` when a test needs custom output
(e.g. a faked ``--version`` string for grep-driven branches).

``sudo`` is special: its default stub strips the sudo prefix and execs
the rest, so ``sudo dnf install ...`` lands at the dnf stub and shows
up in the call log as a clean ``dnf install ...`` line.

These tests are mocked dry-runs (Tier 2 in the testing plan). Real
container runtime behaviour — image pulls, volume permissions, init
order — is out of scope and requires the Tier 3 container-based
harness.
"""

from __future__ import annotations

import shlex
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"

# Some fixture-builder modes (--demo / --dist / archive) bundle the compiled
# frontend, so make-bootstrap-fixture.sh hard-requires frontend/dist. The
# pytest-only CI `test` job never builds the frontend, so skip those tests there;
# they still run locally and anywhere the frontend has been compiled.
requires_built_frontend = pytest.mark.skipif(
    not (REPO_ROOT / "frontend" / "dist" / "index.html").exists(),
    reason="frontend/dist not built — run 'npm run build' (or scripts/rebuild-frontend.sh)",
)

# The fixture builder copies with rsync, which the platform image deliberately
# lacks — it is host tooling and never runs in a container. The compose `test`
# services mirror CI on that image, so skip there; the CI runners and any
# developer host have rsync, and the tests run for real.
requires_rsync = pytest.mark.skipif(
    shutil.which("rsync") is None,
    reason="rsync not installed — make-bootstrap-fixture.sh needs it",
)


@dataclass
class FakeBin:
    """Isolated fake-binary directory plus a call log file."""

    path: Path
    log: Path
    _stubbed: set[str] = field(default_factory=set)

    def stub(self, name: str, body: str = "", exit_code: int = 0) -> None:
        """Install (or replace) a stub binary that logs its invocation.

        ``body`` is shell code that runs *after* the call is logged.
        Use it to fake conditional output (e.g. echo a version string
        so the script's grep matches).
        """
        stub = self.path / name
        log_quoted = shlex.quote(str(self.log))
        stub.write_text(
            "#!/bin/sh\n"
            f'printf "%s" "{name}" >> {log_quoted}\n'
            f'for a in "$@"; do printf " %s" "$a" >> {log_quoted}; done\n'
            f'printf "\\n" >> {log_quoted}\n'
            f"{body}\n"
            f"exit {exit_code}\n"
        )
        stub.chmod(0o755)
        self._stubbed.add(name)

    def remove(self, name: str) -> None:
        """Drop a stub so ``command -v <name>`` fails in the script."""
        (self.path / name).unlink(missing_ok=True)
        self._stubbed.discard(name)

    def calls(self) -> list[str]:
        if not self.log.exists():
            return []
        return self.log.read_text().splitlines()

    def has_call(self, substring: str) -> bool:
        return any(substring in line for line in self.calls())


@pytest.fixture
def fakebin(tmp_path: Path) -> FakeBin:
    """Empty fake-binary directory and a fresh call log."""

    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "calls.log"
    fb = FakeBin(path=bindir, log=log)

    # sudo: strip its own flags + the user/group args, then exec the rest.
    # Lands the underlying command on the call log as a clean line.
    fb.stub(
        "sudo",
        body=r"""
while [ $# -gt 0 ]; do
    case "$1" in
        -E|-s|-i|-H|-n|-S|-v|-K|-k|-l) shift ;;
        -u|-g|-p|-h|-r|-t|-U) shift 2 ;;
        --user|--group|--prompt|--host|--role|--type|--other-user) shift 2 ;;
        --) shift; break ;;
        -*) shift ;;
        *) break ;;
    esac
done
[ -n "$1" ] && exec "$@"
exit 0
""",
    )

    # Real-ish behaviours for a few commands the scripts rely on.
    fb.stub(
        "id",
        body=r"""
case "${1:-}" in
    -u) echo 1000 ;;
    -g) echo 1000 ;;
    *) echo "uid=1000(testuser) gid=1000(testuser)" ;;
esac
""",
    )
    fb.stub("hostname", body="echo testhost")
    # The bootstrap scripts target Linux hosts; pin the architecture so
    # arch-keyed branches (pinned-checksum binary installs) take the
    # x86_64 path regardless of the developer machine running the tests.
    fb.stub(
        "uname",
        body=r"""
case "${1:-}" in
    -m) echo x86_64 ;;
    *) echo Linux ;;
esac
""",
    )
    fb.stub("chown")  # no-op
    fb.stub("chmod")
    fb.stub("cp")  # avoid writing to /usr/bin etc. in install steps

    # Versioning checks: provide answers that satisfy the script's checks.
    fb.stub(
        "docker",
        body=r"""
case "$1 $2" in
    "version --format")
        echo "25.0.0"
        ;;
    *)
        ;;
esac
""",
    )
    fb.stub(
        "podman",
        body=r"""
case "$1 $2" in
    "version --format")
        echo "5.8.2"
        ;;
    "compose version")
        # bootstrap-podman.sh greps for "Docker Compose" or "docker-compose"
        # in the output to verify the v2 backend is wired in.
        echo "Docker Compose version v5.1.4"
        ;;
    "--version "*)
        echo "podman version 5.8.2"
        ;;
esac
""",
    )
    fb.stub(
        "docker-compose",
        body=r"""
case "$1" in
    "version") echo "Docker Compose version v5.1.4" ;;
esac
""",
    )

    # The rest are pure no-op loggers — presence is what matters.
    # ``sha256sum`` and ``install`` are stubbed so the checksum-verified
    # binary install in bootstrap-podman.sh dry-runs cleanly: the real
    # sha256sum would reject the fake download, and the real install
    # (with sudo stripped) would write into the host's /usr/local/bin.
    for cmd in (
        "git",
        "dnf",
        "apt-get",
        "curl",
        "systemctl",
        "podman-compose",
        "ssh-keygen",
        "subscription-manager",
        "groupadd",
        "useradd",
        "usermod",
        "getent",
        "tee",
        "nc",
        "gpg",
        "dpkg",
        "sha256sum",
        "install",
    ):
        fb.stub(cmd)

    return fb


def stage_script(script_name: str, cwd: Path) -> Path:
    """Copy ``scripts/<script_name>`` into ``cwd/scripts/`` and return the
    new path.

    Both bootstrap scripts compute ``SCRIPT_DIR`` from their own location
    and ``cd $SCRIPT_DIR/..`` before doing anything else. Running the
    real script from ``cwd=tmp_path`` would still operate on the
    *developer's* repo, defeating the isolation. Staging copies the
    script into the temp dir so its self-discovered repo root is
    ``tmp_path``.
    """
    target_scripts = cwd / "scripts"
    target_scripts.mkdir(exist_ok=True)
    target = target_scripts / script_name
    shutil.copy(SCRIPTS_DIR / script_name, target)
    target.chmod(0o755)
    # The scripts source shared helpers from scripts/lib/ relative to their
    # own staged location, so the lib directory travels with them.
    lib_dir = SCRIPTS_DIR / "lib"
    if lib_dir.is_dir():
        shutil.copytree(lib_dir, target_scripts / "lib", dirs_exist_ok=True)
    # Empty install-dev-tools.sh as a safety net for cases where a test
    # forgets to set SKIP_DEV_TOOLS_INSTALL=1.
    (target_scripts / "install-dev-tools.sh").write_text("#!/bin/sh\nexit 0\n")
    (target_scripts / "install-dev-tools.sh").chmod(0o755)
    return target


def run_script(
    script_name: str,
    fakebin: FakeBin,
    *,
    cwd: Path,
    extra_env: dict[str, str] | None = None,
    args: list[str] | None = None,
) -> subprocess.CompletedProcess:
    """Stage and execute ``script_name`` in ``cwd`` with ``fakebin`` on PATH.

    Defaults SKIP_DEV_TOOLS_INSTALL=1 so the tests don't symlink anything
    into the developer's real .git hooks directory. Override per-test by
    passing ``extra_env={"SKIP_DEV_TOOLS_INSTALL": ""}`` when the test
    is specifically about the install step.
    """
    staged = stage_script(script_name, cwd)
    env = {
        "PATH": f"{fakebin.path}:/usr/bin:/bin",
        "HOME": str(cwd),
        "USER": "testuser",
        "LC_ALL": "C",
        "SKIP_DEV_TOOLS_INSTALL": "1",
        **(extra_env or {}),
    }
    return subprocess.run(
        ["bash", str(staged), *(args or [])],
        check=False,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
    )


_ENV_DEFAULTS = {
    "DJANGO_MODE": "production",
    "EPICURRENTS_PROJECT": "",
    "HOST_PORT": "8000",
    # Every real .env carries one, from .env.example or init_env, and the borg
    # step now keys on it: an empty value is the documented way to turn repokey
    # backups off, so a stub without the line would skip a step the test expects.
    "BORG_PASSPHRASE": "test-passphrase",
}


def _render_env(overrides: dict[str, str]) -> str:
    merged = {**_ENV_DEFAULTS, **overrides}
    return "\n".join(f"{k}={v}" for k, v in merged.items()) + "\n"


def make_env_example(cwd: Path, **overrides) -> Path:
    """Write a minimal .env.example so the script's `[ ! -f .env ]` branch
    has something to copy from. Overrides extend the baseline defaults.
    """
    example = cwd / ".env.example"
    example.write_text(_render_env(overrides))
    return example


def make_env(cwd: Path, **overrides) -> Path:
    """Write a minimal .env so the script's second-pass branch is taken.
    Overrides extend the baseline defaults (which always include HOST_PORT
    so the script's `set -e -o pipefail` on the final grep does not fire).
    """
    env_file = cwd / ".env"
    env_file.write_text(_render_env(overrides))
    return env_file


def make_os_release(cwd: Path, *, distro_id: str, version: str = "9.4") -> Path:
    """Write a fixture os-release file the bootstrap-podman script can source."""
    f = cwd / "os-release"
    f.write_text(f'ID="{distro_id}"\nVERSION_ID="{version}"\nPRETTY_NAME="Test {distro_id} {version}"\n')
    return f
