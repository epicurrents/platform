"""Dry-run tests for scripts/switch_project.sh.

Starting from no active project (a blank or absent ``EPICURRENTS_PROJECT``) skips the deactivation and still writes
both env files; a named current project is deactivated first; a missing key or frontend env file is added rather than
left unedited; switching to the active project does nothing. See ``conftest.py`` for the fakebin pattern.
"""

from scripts.tests.conftest import run_script

SWITCH = "switch_project.sh"


def _make_repo(tmp_path, *, env, frontend_env=None):
    (tmp_path / ".env").write_text(env)
    (tmp_path / "frontend").mkdir()
    if frontend_env is not None:
        (tmp_path / "frontend" / ".env").write_text(frontend_env)


def _run(fakebin, tmp_path, project="edu"):
    fakebin.stub("docker")
    fakebin.stub("npm")
    return run_script(SWITCH, fakebin, cwd=tmp_path, args=[project])


class TestSwitchProject:
    def test_from_no_project_skips_deactivation(self, fakebin, tmp_path):
        _make_repo(tmp_path, env="EPICURRENTS_PROJECT=\nOTHER=1\n", frontend_env="VITE_PROJECT=\n")
        result = _run(fakebin, tmp_path)
        assert result.returncode == 0, result.stderr
        assert not fakebin.has_call("deactivate_project")
        assert fakebin.has_call("activate_project edu")
        assert fakebin.has_call("npm run build")
        assert (tmp_path / ".env").read_text() == "EPICURRENTS_PROJECT=edu\nOTHER=1\n"
        assert (tmp_path / "frontend" / ".env").read_text() == "VITE_PROJECT=edu\n"

    def test_an_absent_key_and_frontend_env_are_added(self, fakebin, tmp_path):
        _make_repo(tmp_path, env="OTHER=1\n")
        result = _run(fakebin, tmp_path)
        assert result.returncode == 0, result.stderr
        assert not fakebin.has_call("deactivate_project")
        assert (tmp_path / ".env").read_text() == "OTHER=1\nEPICURRENTS_PROJECT=edu\n"
        assert (tmp_path / "frontend" / ".env").read_text() == "VITE_PROJECT=edu\n"

    def test_a_named_project_is_deactivated_first(self, fakebin, tmp_path):
        _make_repo(tmp_path, env="EPICURRENTS_PROJECT=example\n", frontend_env="VITE_PROJECT=example\n")
        result = _run(fakebin, tmp_path)
        assert result.returncode == 0, result.stderr
        calls = fakebin.calls()
        deactivate = next(i for i, c in enumerate(calls) if "deactivate_project" in c)
        activate = next(i for i, c in enumerate(calls) if "activate_project edu" in c)
        assert deactivate < activate
        assert "EPICURRENTS_PROJECT=edu" in (tmp_path / ".env").read_text()

    def test_the_active_project_is_a_no_op(self, fakebin, tmp_path):
        _make_repo(tmp_path, env="EPICURRENTS_PROJECT=edu\n")
        result = _run(fakebin, tmp_path)
        assert result.returncode == 0, result.stderr
        assert "nothing to do" in result.stdout
        assert not fakebin.calls()

    def test_a_missing_env_file_is_refused(self, fakebin, tmp_path):
        (tmp_path / "frontend").mkdir()
        result = _run(fakebin, tmp_path)
        assert result.returncode == 1
        assert not fakebin.calls()
