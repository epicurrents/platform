"""Dry-run tests for scripts/enable_plugin.sh and scripts/disable_plugin.sh.

Exercises the .env / frontend/.env list editing (add, remove, idempotency,
empty-list and absent-key edge cases), the dicom submodule checkout, the
migrate invocation with the explicit plugin-list override, and the
abort-before-env-write behaviour on a failed migrate. See ``conftest.py``
for the fakebin pattern.
"""

from scripts.tests.conftest import run_script

ENABLE = "enable_plugin.sh"
DISABLE = "disable_plugin.sh"


def _make_repo(tmp_path, *, env="", frontend_env=None, plugin="dicom"):
    """Lay out the minimal repo skeleton the plugin scripts expect."""
    (tmp_path / "plugins" / plugin).mkdir(parents=True)
    (tmp_path / ".env").write_text(env)
    if frontend_env is not None:
        (tmp_path / "frontend").mkdir(exist_ok=True)
        (tmp_path / "frontend" / ".env").write_text(frontend_env)
    else:
        (tmp_path / "frontend").mkdir(exist_ok=True)
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir(exist_ok=True)
    stub = scripts_dir / "rebuild-frontend.sh"
    stub.write_text("#!/bin/sh\nexit 0\n")
    stub.chmod(0o755)


class TestEnablePlugin:
    def test_adds_plugin_to_empty_env(self, fakebin, tmp_path):
        _make_repo(tmp_path)
        result = run_script(ENABLE, fakebin, cwd=tmp_path, args=["dicom"])
        assert result.returncode == 0, result.stderr
        assert "EPICURRENTS_PLUGINS=dicom" in (tmp_path / ".env").read_text()
        assert "VITE_PLUGINS=dicom" in (tmp_path / "frontend" / ".env").read_text()

    def test_appends_to_existing_list(self, fakebin, tmp_path):
        _make_repo(
            tmp_path,
            env="EPICURRENTS_PLUGINS=other\n",
            frontend_env="VITE_PLUGINS=other\n",
        )
        result = run_script(ENABLE, fakebin, cwd=tmp_path, args=["dicom"])
        assert result.returncode == 0, result.stderr
        assert "EPICURRENTS_PLUGINS=other,dicom" in (tmp_path / ".env").read_text()
        assert "VITE_PLUGINS=other,dicom" in (tmp_path / "frontend" / ".env").read_text()

    def test_idempotent_when_already_enabled(self, fakebin, tmp_path):
        _make_repo(
            tmp_path,
            env="EPICURRENTS_PLUGINS=dicom,other\n",
            frontend_env="VITE_PLUGINS=dicom,other\n",
        )
        result = run_script(ENABLE, fakebin, cwd=tmp_path, args=["dicom"])
        assert result.returncode == 0, result.stderr
        env = (tmp_path / ".env").read_text()
        assert env.count("dicom") == 1
        assert "EPICURRENTS_PLUGINS=dicom,other" in env

    def test_preserves_other_env_lines(self, fakebin, tmp_path):
        _make_repo(tmp_path, env="SECRET_KEY=abc\nEPICURRENTS_PLUGINS=\nDEBUG=1\n")
        result = run_script(ENABLE, fakebin, cwd=tmp_path, args=["dicom"])
        assert result.returncode == 0, result.stderr
        env = (tmp_path / ".env").read_text()
        assert "SECRET_KEY=abc" in env
        assert "DEBUG=1" in env
        assert "EPICURRENTS_PLUGINS=dicom" in env

    def test_dicom_checks_out_ohif_submodule(self, fakebin, tmp_path):
        _make_repo(tmp_path)
        run_script(ENABLE, fakebin, cwd=tmp_path, args=["dicom"])
        assert fakebin.has_call("submodule update --init --checkout plugins/dicom/ohif-viewer")

    def test_migrate_runs_with_plugin_list_override(self, fakebin, tmp_path):
        _make_repo(tmp_path)
        run_script(ENABLE, fakebin, cwd=tmp_path, args=["dicom"])
        assert fakebin.has_call("-e EPICURRENTS_PLUGINS=dicom web python manage.py migrate")

    def test_failed_migrate_leaves_env_untouched(self, fakebin, tmp_path):
        _make_repo(tmp_path)
        fakebin.stub("docker", exit_code=1)
        result = run_script(ENABLE, fakebin, cwd=tmp_path, args=["dicom"])
        assert result.returncode != 0
        assert "EPICURRENTS_PLUGINS=dicom" not in (tmp_path / ".env").read_text()

    def test_unknown_plugin_dir_fails(self, fakebin, tmp_path):
        _make_repo(tmp_path, plugin="other")
        result = run_script(ENABLE, fakebin, cwd=tmp_path, args=["dicom"])
        assert result.returncode != 0
        assert "does not exist" in result.stderr

    def test_missing_argument_fails(self, fakebin, tmp_path):
        _make_repo(tmp_path)
        result = run_script(ENABLE, fakebin, cwd=tmp_path)
        assert result.returncode != 0


class TestDisablePlugin:
    def test_removes_plugin_from_list(self, fakebin, tmp_path):
        _make_repo(
            tmp_path,
            env="EPICURRENTS_PLUGINS=dicom,other\n",
            frontend_env="VITE_PLUGINS=dicom,other\n",
        )
        result = run_script(DISABLE, fakebin, cwd=tmp_path, args=["dicom"])
        assert result.returncode == 0, result.stderr
        assert "EPICURRENTS_PLUGINS=other" in (tmp_path / ".env").read_text()
        assert "VITE_PLUGINS=other" in (tmp_path / "frontend" / ".env").read_text()

    def test_sole_entry_leaves_empty_value(self, fakebin, tmp_path):
        _make_repo(
            tmp_path,
            env="EPICURRENTS_PLUGINS=dicom\n",
            frontend_env="VITE_PLUGINS=dicom\n",
        )
        result = run_script(DISABLE, fakebin, cwd=tmp_path, args=["dicom"])
        assert result.returncode == 0, result.stderr
        assert "EPICURRENTS_PLUGINS=\n" in (tmp_path / ".env").read_text()

    def test_empty_value_does_not_crash(self, fakebin, tmp_path):
        """Regression: an empty list tripped `set -u` under bash 3.2."""
        _make_repo(
            tmp_path,
            env="EPICURRENTS_PLUGINS=\n",
            frontend_env="VITE_PLUGINS=\n",
        )
        result = run_script(DISABLE, fakebin, cwd=tmp_path, args=["dicom"])
        assert result.returncode == 0, result.stderr

    def test_absent_keys_are_a_noop(self, fakebin, tmp_path):
        """Regression: a frontend/.env without VITE_PLUGINS crashed mid-run."""
        _make_repo(tmp_path, env="SECRET_KEY=abc\n", frontend_env="")
        result = run_script(DISABLE, fakebin, cwd=tmp_path, args=["dicom"])
        assert result.returncode == 0, result.stderr
        assert (tmp_path / ".env").read_text() == "SECRET_KEY=abc\n"

    def test_preserves_other_entries_order(self, fakebin, tmp_path):
        _make_repo(
            tmp_path,
            env="EPICURRENTS_PLUGINS=a,dicom,b\n",
            frontend_env="VITE_PLUGINS=a,dicom,b\n",
        )
        result = run_script(DISABLE, fakebin, cwd=tmp_path, args=["dicom"])
        assert result.returncode == 0, result.stderr
        assert "EPICURRENTS_PLUGINS=a,b" in (tmp_path / ".env").read_text()
