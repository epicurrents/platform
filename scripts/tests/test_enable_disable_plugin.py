"""Dry-run tests for scripts/enable_plugin.sh and scripts/disable_plugin.sh.

Exercises the .env / frontend/.env list editing (add, remove, idempotency,
empty-list and absent-key edge cases), the dicom submodule checkout, the
migrate invocation with the explicit plugin-list override, and the
abort-before-env-write behaviour on a failed migrate. The compose command
follows the runtime as ``scripts/lib/compose.sh`` settles it: Docker Engine,
rootful Podman, Podman answering as ``docker``, and no runtime at all, which
enabling refuses and disabling, which drives no container, does not. A root
path with a space in it stays one argument. ``rebuild-frontend.sh``, which
both end by calling, restarts the web container through the same helper. See
``conftest.py`` for the fakebin pattern.
"""

from scripts.tests.conftest import run_script

ENABLE = "enable_plugin.sh"
DISABLE = "disable_plugin.sh"
REBUILD = "rebuild-frontend.sh"

PODMAN_AS_DOCKER = '[ "$1" = "--version" ] && echo "podman version 5.8.2"'
# The call log joins arguments with spaces, so it cannot tell a split path from a
# whole one; this stub does, by refusing a compose file argument that is not a file.
COMPOSE_FILE_MUST_EXIST = 'if [ "$1" = compose ] && [ ! -f "$3" ]; then exit 1; fi'


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

    def test_docker_engine_runs_compose_through_docker(self, fakebin, tmp_path):
        _make_repo(tmp_path)
        result = run_script(ENABLE, fakebin, cwd=tmp_path, args=["dicom"])
        assert result.returncode == 0, result.stderr
        compose = f"docker compose -f {tmp_path}/docker-compose.yml"
        assert fakebin.has_call(f"{compose} up -d db redis")
        assert fakebin.has_call(
            f"{compose} run --rm --no-deps -e EPICURRENTS_PLUGINS=dicom web python manage.py migrate"
        )
        assert not any(call.startswith(("podman", "sudo")) for call in fakebin.calls())
        assert f"    {compose} up -d" in result.stdout

    def test_podman_alone_runs_compose_through_rootful_podman(self, fakebin, tmp_path):
        _make_repo(tmp_path)
        fakebin.remove("docker")
        result = run_script(ENABLE, fakebin, cwd=tmp_path, args=["dicom"])
        assert result.returncode == 0, result.stderr
        compose = f"podman compose -f {tmp_path}/docker-compose.yml"
        assert fakebin.has_call(f"sudo -E {compose} up -d db redis")
        assert fakebin.has_call(
            f"sudo -E {compose} run --rm --no-deps -e EPICURRENTS_PLUGINS=dicom web python manage.py migrate"
        )
        assert not any(call.startswith("docker") for call in fakebin.calls())
        assert f"    sudo -E {compose} up -d" in result.stdout
        assert "EPICURRENTS_PLUGINS=dicom" in (tmp_path / ".env").read_text()

    def test_a_docker_that_is_podman_is_not_trusted_by_name(self, fakebin, tmp_path):
        _make_repo(tmp_path)
        fakebin.stub("docker", body=PODMAN_AS_DOCKER)
        result = run_script(ENABLE, fakebin, cwd=tmp_path, args=["dicom"])
        assert result.returncode == 0, result.stderr
        assert fakebin.has_call("sudo -E podman compose -f")
        assert not fakebin.has_call("docker compose")

    def test_a_root_run_drives_podman_without_sudo(self, fakebin, tmp_path):
        _make_repo(tmp_path)
        fakebin.remove("docker")
        fakebin.stub("id", body="echo 0")
        result = run_script(ENABLE, fakebin, cwd=tmp_path, args=["dicom"])
        assert result.returncode == 0, result.stderr
        assert fakebin.has_call(f"podman compose -f {tmp_path}/docker-compose.yml up -d db redis")
        assert not any(call.startswith("sudo") for call in fakebin.calls())

    def test_no_runtime_is_refused_before_anything_is_fetched_or_edited(self, fakebin, tmp_path):
        _make_repo(tmp_path, env="SECRET_KEY=abc\n")
        fakebin.remove("docker")
        fakebin.remove("podman")
        result = run_script(ENABLE, fakebin, cwd=tmp_path, args=["dicom"])
        assert result.returncode == 1
        assert "No container runtime found" in result.stderr
        assert (tmp_path / ".env").read_text() == "SECRET_KEY=abc\n"
        assert not (tmp_path / "frontend" / ".env").exists()
        assert not fakebin.has_call("git")

    def test_a_root_path_with_a_space_stays_one_argument(self, fakebin, tmp_path):
        root = tmp_path / "my deployment"
        root.mkdir()
        _make_repo(root)
        (root / "docker-compose.yml").write_text("services: {}\n")
        fakebin.stub("docker", body=COMPOSE_FILE_MUST_EXIST)
        result = run_script(ENABLE, fakebin, cwd=root, args=["dicom"])
        assert result.returncode == 0, result.stderr
        assert fakebin.has_call(f"docker compose -f {root}/docker-compose.yml up -d db redis")
        assert "EPICURRENTS_PLUGINS=dicom" in (root / ".env").read_text()
        # The printed restart command quotes the path, so it can be pasted.
        assert "my\\ deployment/docker-compose.yml up -d" in result.stdout


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

    def test_docker_engine_is_named_in_the_restart_command(self, fakebin, tmp_path):
        _make_repo(tmp_path, env="EPICURRENTS_PLUGINS=dicom\n", frontend_env="VITE_PLUGINS=dicom\n")
        result = run_script(DISABLE, fakebin, cwd=tmp_path, args=["dicom"])
        assert result.returncode == 0, result.stderr
        assert f"    docker compose -f {tmp_path}/docker-compose.yml up -d" in result.stdout

    def test_podman_alone_is_named_in_the_restart_command(self, fakebin, tmp_path):
        _make_repo(tmp_path, env="EPICURRENTS_PLUGINS=dicom\n", frontend_env="VITE_PLUGINS=dicom\n")
        fakebin.remove("docker")
        result = run_script(DISABLE, fakebin, cwd=tmp_path, args=["dicom"])
        assert result.returncode == 0, result.stderr
        assert f"    sudo -E podman compose -f {tmp_path}/docker-compose.yml up -d" in result.stdout
        assert "docker compose" not in result.stdout

    def test_a_docker_that_is_podman_is_not_trusted_by_name(self, fakebin, tmp_path):
        _make_repo(tmp_path, env="EPICURRENTS_PLUGINS=dicom\n", frontend_env="VITE_PLUGINS=dicom\n")
        fakebin.stub("docker", body=PODMAN_AS_DOCKER)
        result = run_script(DISABLE, fakebin, cwd=tmp_path, args=["dicom"])
        assert result.returncode == 0, result.stderr
        assert "    sudo -E podman compose -f" in result.stdout
        assert "docker compose" not in result.stdout

    def test_no_runtime_still_disables_and_names_the_fallback(self, fakebin, tmp_path):
        # Disabling drives no container, so a host without a runtime is not refused,
        # as update.sh does not refuse --check-archive there.
        _make_repo(tmp_path, env="EPICURRENTS_PLUGINS=dicom\n", frontend_env="VITE_PLUGINS=dicom\n")
        fakebin.remove("docker")
        fakebin.remove("podman")
        result = run_script(DISABLE, fakebin, cwd=tmp_path, args=["dicom"])
        assert result.returncode == 0, result.stderr
        assert "EPICURRENTS_PLUGINS=\n" in (tmp_path / ".env").read_text()
        assert f"    docker compose -f {tmp_path}/docker-compose.yml up -d" in result.stdout

    def test_it_runs_no_compose_command_itself(self, fakebin, tmp_path):
        _make_repo(tmp_path, env="EPICURRENTS_PLUGINS=dicom\n", frontend_env="VITE_PLUGINS=dicom\n")
        result = run_script(DISABLE, fakebin, cwd=tmp_path, args=["dicom"])
        assert result.returncode == 0, result.stderr
        assert not fakebin.has_call("compose")

    def test_a_root_path_with_a_space_is_quoted_in_the_restart_command(self, fakebin, tmp_path):
        root = tmp_path / "my deployment"
        root.mkdir()
        _make_repo(root, env="EPICURRENTS_PLUGINS=dicom,other\n", frontend_env="VITE_PLUGINS=dicom,other\n")
        result = run_script(DISABLE, fakebin, cwd=root, args=["dicom"])
        assert result.returncode == 0, result.stderr
        assert "EPICURRENTS_PLUGINS=other" in (root / ".env").read_text()
        assert "my\\ deployment/docker-compose.yml up -d" in result.stdout


class TestRebuildFrontend:
    """The script both plugin scripts end by calling restarts the stack through the same helper."""

    def _run(self, fakebin, root):
        (root / "frontend").mkdir(exist_ok=True)
        fakebin.stub("npm")
        return run_script(REBUILD, fakebin, cwd=root)

    def test_docker_engine_restarts_web_through_docker_compose(self, fakebin, tmp_path):
        result = self._run(fakebin, tmp_path)
        assert result.returncode == 0, result.stderr
        assert fakebin.calls()[-1] == "docker compose restart web"

    def test_podman_alone_restarts_web_through_rootful_podman(self, fakebin, tmp_path):
        fakebin.remove("docker")
        result = self._run(fakebin, tmp_path)
        assert result.returncode == 0, result.stderr
        assert fakebin.has_call("sudo -E podman compose restart web")
        assert not any(call.startswith("docker") for call in fakebin.calls())

    def test_a_docker_that_is_podman_is_not_trusted_by_name(self, fakebin, tmp_path):
        fakebin.stub("docker", body=PODMAN_AS_DOCKER)
        result = self._run(fakebin, tmp_path)
        assert result.returncode == 0, result.stderr
        assert fakebin.has_call("sudo -E podman compose restart web")
        assert not fakebin.has_call("docker compose")

    def test_no_runtime_builds_and_then_fails_at_the_restart(self, fakebin, tmp_path):
        # Unchanged from before the helper: the build is not held back by a missing
        # runtime, and the restart fails on the absent `docker`.
        fakebin.remove("docker")
        fakebin.remove("podman")
        result = self._run(fakebin, tmp_path)
        assert result.returncode != 0
        assert fakebin.has_call("npm run build")

    def test_a_root_path_with_a_space_works(self, fakebin, tmp_path):
        root = tmp_path / "my deployment"
        root.mkdir()
        result = self._run(fakebin, root)
        assert result.returncode == 0, result.stderr
        assert fakebin.has_call("docker compose restart web")
