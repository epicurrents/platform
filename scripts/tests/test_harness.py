"""Self-tests for the fakebin harness itself.

The shell-script tests are only as honest as the PATH they hand the script. The
case that motivated this file: ``fakebin.remove("docker")`` used to unlink the
stub and nothing else, which masks docker on a host that has none and masks
nothing on a host that has one. Both the podman-runtime tests and the
no-runtime tests then passed on macOS and failed on a Linux CI runner, where
``/usr/bin/docker`` answered the probe the test meant to leave unanswered.

The probe binary here is ``sed`` rather than ``docker``: it is present in
``/usr/bin`` on every host that can run this suite, so these assertions exercise
the masking mechanism everywhere instead of only where a container runtime
happens to be installed.
"""

import subprocess

from scripts.tests import conftest
from scripts.tests.conftest import SYSTEM_PATH_DIRS, make_env, run_script, system_path

#: Present in /usr/bin on every supported host, so masking it is observable.
PROBE = "sed"
#: A second one, to show masking removes the named binary and not the directory.
BYSTANDER = "env"


def _resolve(name: str, path: str) -> str:
    """Resolve ``name`` the way the scripts do, with ``command -v``. Empty when unresolved."""
    result = subprocess.run(
        ["sh", "-c", f"command -v {name}"],
        env={"PATH": path},
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


class TestSystemPath:
    def test_an_unmasked_path_is_the_real_system_directories(self, tmp_path):
        assert system_path(tmp_path) == ":".join(SYSTEM_PATH_DIRS)

    def test_the_probe_resolves_on_an_unmasked_path(self, tmp_path):
        assert _resolve(PROBE, system_path(tmp_path)), f"{PROBE} is missing — the masking tests would prove nothing"

    def test_a_masked_name_stops_resolving(self, tmp_path):
        assert not _resolve(PROBE, system_path(tmp_path, {PROBE}))

    def test_masking_one_name_leaves_the_others_reachable(self, tmp_path):
        assert _resolve(BYSTANDER, system_path(tmp_path, {PROBE}))

    def test_masking_is_idempotent_across_calls(self, tmp_path):
        first = system_path(tmp_path, {PROBE})
        second = system_path(tmp_path, {PROBE})
        assert first == second
        assert not _resolve(PROBE, second)


class TestRemoveMasks:
    """``remove`` has to reach the system half of PATH, not only the stub directory."""

    def test_remove_records_the_name(self, fakebin):
        fakebin.remove(PROBE)
        assert PROBE in fakebin.removed

    def test_a_removed_name_resolves_nowhere_on_the_scripts_path(self, fakebin):
        # The PATH run_script composes, assembled the same way.
        assert _resolve(PROBE, f"{fakebin.path}:{system_path(fakebin.path, fakebin.removed)}")
        fakebin.remove(PROBE)
        assert not _resolve(PROBE, f"{fakebin.path}:{system_path(fakebin.path, fakebin.removed)}")

    def test_a_stubbed_name_wins_over_the_system_one(self, fakebin):
        fakebin.stub(PROBE)
        path = f"{fakebin.path}:{system_path(fakebin.path, fakebin.removed)}"
        assert _resolve(PROBE, path) == str(fakebin.path / PROBE)

    def test_stubbing_a_removed_name_puts_it_back(self, fakebin):
        fakebin.remove(PROBE)
        fakebin.stub(PROBE)
        assert PROBE not in fakebin.removed


class TestRunScriptAppliesTheMask:
    """The end-to-end half: run_script has to compose PATH from the mask.

    Without this, a revert of that one line is invisible on a developer machine —
    the runtime-detection tests in test_update.py would keep passing on any host
    that has no container runtime in its system directories, which is every macOS
    host (Docker Desktop installs to /usr/local/bin) and no Linux CI runner.
    """

    def test_a_removed_runtime_is_masked_even_when_the_host_has_one(self, fakebin, tmp_path, monkeypatch):
        # Stand in for a CI runner, whose /usr/bin holds a real docker and podman.
        sysdir = tmp_path / "fake-system-bin"
        sysdir.mkdir()
        for name in ("docker", "podman"):
            binary = sysdir / name
            binary.write_text("#!/bin/sh\necho 25.0.0\nexit 0\n")
            binary.chmod(0o755)
        # Patching the module this file imported works because run_script and
        # system_path come from it too. It does not extend to the fixture's
        # FakeBin class: pytest holds several distinct module objects for
        # conftest.py, and the fixture is built from a different one.
        monkeypatch.setattr(conftest, "SYSTEM_PATH_DIRS", (str(sysdir), *SYSTEM_PATH_DIRS))

        make_env(tmp_path)
        tmp_path.chmod(0o777)
        fakebin.remove("docker")
        fakebin.remove("podman")

        result = run_script("update.sh", fakebin, cwd=tmp_path, args=["--from", "repo", "--no-pull"])
        assert result.returncode != 0
        assert "No container runtime found" in result.stderr, result.stderr
