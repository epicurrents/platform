"""Dry-run tests for scripts/switch_project.sh.

Starting from no active project (a blank or absent ``EPICURRENTS_PROJECT``) skips the deactivation and still writes
both env files; a named current project is deactivated first; a missing key or frontend env file is added rather than
left unedited; switching to the active project does nothing; an option, ``--help`` included, is never taken as a
project name; and a name that is no project under ``projects/``, or a second argument, is refused before any service is
touched. The compose command follows the runtime as ``scripts/lib/compose.sh`` settles it, Podman included when it
answers as ``docker``, and a host with neither is refused as update.sh refuses it; a root path with a space in it stays
one argument; and an edited env file keeps its mode and, where it is a symlink, stays one. See
``conftest.py`` for the fakebin pattern.
"""

import stat

import pytest

from scripts.tests.conftest import run_script

SWITCH = "switch_project.sh"


def _make_repo(tmp_path, *, env, frontend_env=None):
    (tmp_path / ".env").write_text(env)
    (tmp_path / "frontend").mkdir()
    (tmp_path / "projects" / "research").mkdir(parents=True)
    if frontend_env is not None:
        (tmp_path / "frontend" / ".env").write_text(frontend_env)


def _run(fakebin, tmp_path, project="research", extra=(), docker=True):
    if docker:
        fakebin.stub("docker")
    fakebin.stub("npm")
    return run_script(SWITCH, fakebin, cwd=tmp_path, args=[project, *extra])


class TestSwitchProject:
    def test_from_no_project_skips_deactivation(self, fakebin, tmp_path):
        _make_repo(tmp_path, env="EPICURRENTS_PROJECT=\nOTHER=1\n", frontend_env="VITE_PROJECT=\n")
        result = _run(fakebin, tmp_path)
        assert result.returncode == 0, result.stderr
        assert not fakebin.has_call("deactivate_project")
        assert fakebin.has_call("activate_project research")
        assert fakebin.has_call("npm run build")
        assert (tmp_path / ".env").read_text() == "EPICURRENTS_PROJECT=research\nOTHER=1\n"
        assert (tmp_path / "frontend" / ".env").read_text() == "VITE_PROJECT=research\n"

    def test_an_absent_key_and_frontend_env_are_added(self, fakebin, tmp_path):
        _make_repo(tmp_path, env="OTHER=1\n")
        result = _run(fakebin, tmp_path)
        assert result.returncode == 0, result.stderr
        assert not fakebin.has_call("deactivate_project")
        assert (tmp_path / ".env").read_text() == "OTHER=1\nEPICURRENTS_PROJECT=research\n"
        assert (tmp_path / "frontend" / ".env").read_text() == "VITE_PROJECT=research\n"

    def test_a_named_project_is_deactivated_first(self, fakebin, tmp_path):
        _make_repo(tmp_path, env="EPICURRENTS_PROJECT=example\n", frontend_env="VITE_PROJECT=example\n")
        result = _run(fakebin, tmp_path)
        assert result.returncode == 0, result.stderr
        calls = fakebin.calls()
        deactivate = next(i for i, c in enumerate(calls) if "deactivate_project" in c)
        activate = next(i for i, c in enumerate(calls) if "activate_project research" in c)
        assert deactivate < activate
        assert "EPICURRENTS_PROJECT=research" in (tmp_path / ".env").read_text()

    def test_the_active_project_is_a_no_op(self, fakebin, tmp_path):
        _make_repo(tmp_path, env="EPICURRENTS_PROJECT=research\n")
        result = _run(fakebin, tmp_path)
        assert result.returncode == 0, result.stderr
        assert "nothing to do" in result.stdout
        assert not fakebin.calls()

    def test_a_missing_env_file_is_refused(self, fakebin, tmp_path):
        (tmp_path / "frontend").mkdir()
        result = _run(fakebin, tmp_path)
        assert result.returncode == 1
        assert not fakebin.calls()

    def test_help_prints_usage_and_touches_nothing(self, fakebin, tmp_path):
        _make_repo(tmp_path, env="EPICURRENTS_PROJECT=example\n")
        result = _run(fakebin, tmp_path, project="--help")
        assert result.returncode == 0, result.stderr
        assert "Usage:" in result.stdout
        assert not fakebin.calls()
        assert (tmp_path / ".env").read_text() == "EPICURRENTS_PROJECT=example\n"

    def test_an_option_is_never_taken_as_a_project(self, fakebin, tmp_path):
        _make_repo(tmp_path, env="EPICURRENTS_PROJECT=example\n")
        result = _run(fakebin, tmp_path, project="--dry-run")
        assert result.returncode == 1
        assert "Usage:" in result.stderr
        assert not fakebin.calls()

    @pytest.mark.parametrize("name", ["reserach", "projects/research", "research=1", "re search", "Research"])
    def test_a_name_that_is_no_project_is_refused_before_anything_runs(self, fakebin, tmp_path, name):
        _make_repo(tmp_path, env="EPICURRENTS_PROJECT=example\n", frontend_env="VITE_PROJECT=example\n")
        result = _run(fakebin, tmp_path, project=name)
        assert result.returncode == 1
        assert "Error:" in result.stderr
        assert not fakebin.calls()
        assert (tmp_path / ".env").read_text() == "EPICURRENTS_PROJECT=example\n"
        assert (tmp_path / "frontend" / ".env").read_text() == "VITE_PROJECT=example\n"

    def test_an_extra_argument_is_refused_before_anything_runs(self, fakebin, tmp_path):
        _make_repo(tmp_path, env="EPICURRENTS_PROJECT=example\n", frontend_env="VITE_PROJECT=example\n")
        result = _run(fakebin, tmp_path, extra=["example"])
        assert result.returncode == 1
        assert "Usage:" in result.stderr
        assert not fakebin.calls()
        assert (tmp_path / ".env").read_text() == "EPICURRENTS_PROJECT=example\n"

    def test_podman_alone_runs_compose_through_rootful_podman(self, fakebin, tmp_path):
        _make_repo(tmp_path, env="EPICURRENTS_PROJECT=example\n", frontend_env="VITE_PROJECT=example\n")
        fakebin.remove("docker")
        result = _run(fakebin, tmp_path, docker=False)
        assert result.returncode == 0, result.stderr
        compose = f"podman compose -f {tmp_path}/docker-compose.yml"
        assert fakebin.has_call(f"sudo -E {compose} up -d db redis")
        assert fakebin.has_call(f"{compose} run --rm --no-deps web python manage.py deactivate_project")
        assert fakebin.has_call(f"{compose} run --rm --no-deps web python manage.py activate_project research")
        assert fakebin.has_call(f"{compose} up -d --force-recreate")
        assert not any(call.startswith("docker") for call in fakebin.calls())

    def test_a_docker_that_is_podman_is_not_trusted_by_name(self, fakebin, tmp_path):
        _make_repo(tmp_path, env="EPICURRENTS_PROJECT=\n")
        fakebin.stub("docker", body='[ "$1" = "--version" ] && echo "podman version 5.8.2"')
        result = _run(fakebin, tmp_path, docker=False)
        assert result.returncode == 0, result.stderr
        assert fakebin.has_call("sudo -E podman compose -f")
        assert not fakebin.has_call("docker compose")

    def test_no_runtime_is_refused_before_anything_is_edited(self, fakebin, tmp_path):
        _make_repo(tmp_path, env="EPICURRENTS_PROJECT=example\n")
        fakebin.remove("docker")
        fakebin.remove("podman")
        result = _run(fakebin, tmp_path, docker=False)
        assert result.returncode == 1
        # The refusal update.sh gives, from the helper both take the wording from.
        assert "No container runtime found" in result.stderr
        assert (tmp_path / ".env").read_text() == "EPICURRENTS_PROJECT=example\n"
        assert not fakebin.has_call("npm")
        assert not any(call.startswith(("docker", "podman", "sudo")) for call in fakebin.calls())

    def test_docker_engine_runs_every_step_through_docker_compose(self, fakebin, tmp_path):
        _make_repo(tmp_path, env="EPICURRENTS_PROJECT=example\n", frontend_env="VITE_PROJECT=example\n")
        result = _run(fakebin, tmp_path)
        assert result.returncode == 0, result.stderr
        compose = f"docker compose -f {tmp_path}/docker-compose.yml"
        assert fakebin.has_call(f"{compose} up -d db redis")
        assert fakebin.has_call(f"{compose} stop web celery celery-beat")
        assert fakebin.has_call(f"{compose} run --rm --no-deps web python manage.py deactivate_project")
        assert fakebin.has_call(f"{compose} run --rm --no-deps web python manage.py activate_project research")
        assert fakebin.has_call(f"{compose} up -d --force-recreate")
        assert not any(call.startswith(("podman", "sudo")) for call in fakebin.calls())

    def test_a_root_run_drives_podman_without_sudo(self, fakebin, tmp_path):
        _make_repo(tmp_path, env="EPICURRENTS_PROJECT=\n")
        fakebin.remove("docker")
        fakebin.stub("id", body="echo 0")
        result = _run(fakebin, tmp_path, docker=False)
        assert result.returncode == 0, result.stderr
        assert fakebin.has_call(f"podman compose -f {tmp_path}/docker-compose.yml up -d db redis")
        assert not any(call.startswith("sudo") for call in fakebin.calls())

    def test_a_root_path_with_a_space_stays_one_argument(self, fakebin, tmp_path):
        root = tmp_path / "my deployment"
        root.mkdir()
        _make_repo(root, env="EPICURRENTS_PROJECT=example\n", frontend_env="VITE_PROJECT=example\n")
        (root / "docker-compose.yml").write_text("services: {}\n")
        # The call log joins arguments with spaces, so it cannot tell a split path from a whole one;
        # the stub does, by refusing a compose file argument that is not a file.
        fakebin.stub("docker", body='if [ "$1" = compose ] && [ ! -f "$3" ]; then exit 1; fi')
        result = _run(fakebin, root, docker=False)
        assert result.returncode == 0, result.stderr
        assert fakebin.has_call(f"docker compose -f {root}/docker-compose.yml up -d --force-recreate")
        assert (root / ".env").read_text() == "EPICURRENTS_PROJECT=research\n"
        assert (root / "frontend" / ".env").read_text() == "VITE_PROJECT=research\n"

    def test_an_edited_env_file_keeps_its_mode(self, fakebin, tmp_path):
        _make_repo(tmp_path, env="EPICURRENTS_PROJECT=example\n", frontend_env="VITE_PROJECT=example\n")
        (tmp_path / ".env").chmod(0o640)
        (tmp_path / "frontend" / ".env").chmod(0o644)
        result = _run(fakebin, tmp_path)
        assert result.returncode == 0, result.stderr
        assert (tmp_path / ".env").read_text() == "EPICURRENTS_PROJECT=research\n"
        assert stat.S_IMODE((tmp_path / ".env").stat().st_mode) == 0o640
        assert stat.S_IMODE((tmp_path / "frontend" / ".env").stat().st_mode) == 0o644

    def test_a_symlinked_env_file_is_written_through(self, fakebin, tmp_path):
        _make_repo(tmp_path, env="", frontend_env="VITE_PROJECT=example\n")
        target = tmp_path / "secrets" / "platform.env"
        target.parent.mkdir()
        target.write_text("EPICURRENTS_PROJECT=example\nOTHER=1\n")
        target.chmod(0o640)
        (tmp_path / ".env").unlink()
        (tmp_path / ".env").symlink_to(target)
        result = _run(fakebin, tmp_path)
        assert result.returncode == 0, result.stderr
        assert (tmp_path / ".env").is_symlink()
        assert target.read_text() == "EPICURRENTS_PROJECT=research\nOTHER=1\n"
        assert stat.S_IMODE(target.stat().st_mode) == 0o640
