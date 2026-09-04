"""Dry-run tests for scripts/restore-drill.sh.

The drill deletes volumes on a host that may be running a real deployment, so
the property worth pinning is not that it restores correctly — that needs real
containers and is what running the drill itself proves — but that it cannot
reach outside its own scratch project while trying. A mistyped or
interpolated-empty project name is how that goes wrong silently, and it goes
wrong exactly once.

See ``conftest.py`` for the fakebin pattern. Under fakes the drill fails at its
first readback assertion (the stubs produce no marker output); the call log up to
that point, plus the trap-driven teardown, is what these assert on.
"""

import re

from scripts.tests.conftest import make_env, run_script

DRILL = "restore-drill.sh"
PROJECT = "epicurrents-restore-drill"


def _compose_calls(fakebin):
    return [c for c in fakebin.calls() if c.startswith("docker compose")]


class TestScratchProjectIsolation:
    def test_every_compose_call_names_the_drill_project(self, fakebin, tmp_path):
        make_env(tmp_path, BORG_PASSPHRASE="drill-passphrase")
        run_script(DRILL, fakebin, cwd=tmp_path)

        calls = _compose_calls(fakebin)
        assert calls, "the drill made no docker compose calls at all"
        unscoped = [c for c in calls if f"-p {PROJECT}" not in c]
        assert not unscoped, (
            f"compose calls without the drill project name would act on the host's default "
            f"project, which may be a live deployment: {unscoped}"
        )

    def test_teardown_targets_the_drill_project(self, fakebin, tmp_path):
        """`down -v` destroys volumes, so it is the single most dangerous command
        in the script and must never run unscoped."""
        make_env(tmp_path, BORG_PASSPHRASE="drill-passphrase")
        run_script(DRILL, fakebin, cwd=tmp_path)

        destructive = [c for c in _compose_calls(fakebin) if "down" in c and "-v" in c]
        assert destructive, "the drill never tore its scratch stack down"
        for call in destructive:
            assert f"-p {PROJECT}" in call

    def test_no_host_port_is_published(self, fakebin, tmp_path):
        """The drill has to be able to run alongside a live stack, and the web
        service's port mapping would collide with it."""
        make_env(tmp_path, BORG_PASSPHRASE="drill-passphrase")
        run_script(DRILL, fakebin, cwd=tmp_path)

        assert not any("--service-ports" in c for c in _compose_calls(fakebin))
        assert not any(re.search(r"\bup\b.*\bweb\b", c) for c in _compose_calls(fakebin))


class TestComposeScopingIsStructural:
    """The dry run only reaches the drill's first few compose calls before its
    readbacks fail against stubs, so asserting on the call log alone leaves most
    of the script uncovered — a bare `docker compose` added to the destroy or
    restore steps would not appear in it. Scan the source instead, the way the
    volume-removal guard below is scanned.
    """

    def _source(self):
        from scripts.tests.conftest import SCRIPTS_DIR

        return (SCRIPTS_DIR / DRILL).read_text()

    def test_compose_is_only_invoked_through_the_wrapper(self):
        source = self._source()
        body = source.split("dc() {", 1)[1].split("\n}", 1)[0]
        offenders = [
            line.strip()
            for line in source.splitlines()
            if "docker compose" in line and line not in body.splitlines() and not line.strip().startswith("#")
        ]
        # The usage comment in cleanup() prints a teardown command for the
        # operator rather than running one, so it is text, not an invocation.
        offenders = [line for line in offenders if not line.startswith("printf")]
        assert not offenders, f"compose invoked outside dc(), which is what applies the drill project name: {offenders}"

    def test_the_wrapper_pins_the_project_and_neutralises_the_backup_target(self):
        body = self._source().split("dc() {", 1)[1].split("\n}", 1)[0]
        assert '-p "$DRILL_PROJECT"' in body
        # Without these the drill reads the operator's .env and, because it runs
        # the production emitter, writes a scratch archive to the real off-host
        # repository and pings the real backup monitor.
        assert "BORG_REMOTE_REPO=''" in body
        assert "BORG_MONITOR_URL=''" in body


class TestVolumeRemovalGuard:
    """Volume removal is the one operation that reaches outside compose's own
    project scoping, so it goes through a helper that refuses any name lacking
    the drill prefix. A future edit adding a bare `docker volume rm` is the
    regression this catches.
    """

    def _source(self):
        from scripts.tests.conftest import SCRIPTS_DIR

        return (SCRIPTS_DIR / DRILL).read_text()

    def test_volume_removal_only_happens_inside_the_guard(self):
        source = self._source()
        helper_body = source.split("drop_volume() {", 1)[1].split("\n}", 1)[0]
        occurrences = source.count("docker volume rm")
        assert occurrences == 1, f"expected exactly one 'docker volume rm', found {occurrences}"
        assert "docker volume rm" in helper_body

    def test_the_guard_matches_on_the_project_prefix(self):
        helper_body = self._source().split("drop_volume() {", 1)[1].split("\n}", 1)[0]
        assert '"${DRILL_PROJECT}_"*' in helper_body
        assert "refusing to remove volume" in helper_body


class TestPreflight:
    def test_refuses_without_a_borg_passphrase(self, fakebin, tmp_path):
        """An empty passphrase means an unencrypted repo, so a pass would say
        nothing about the encrypted one production actually writes."""
        make_env(tmp_path, BORG_PASSPHRASE="")
        result = run_script(DRILL, fakebin, cwd=tmp_path)
        assert result.returncode != 0
        assert "BORG_PASSPHRASE" in result.stderr

    def test_refuses_without_an_env_file(self, fakebin, tmp_path):
        result = run_script(DRILL, fakebin, cwd=tmp_path)
        assert result.returncode != 0
        assert ".env not found" in result.stderr
