"""Dry-run tests for scripts/bootstrap-podman.sh.

Exercises distro detection, the docker-compose v2 install path, the
podman-compose conflict cleanup, and the basic first-pass / second-pass
behaviour. See ``conftest.py`` for the fakebin pattern.
"""

from scripts.tests.conftest import (
    make_env,
    make_env_example,
    make_os_release,
    run_script,
)

BOOTSTRAP = "bootstrap-podman.sh"


class TestBootstrapPodmanDistroDetection:
    def test_rhel_is_accepted(self, fakebin, tmp_path):
        make_env_example(tmp_path)
        os_release = make_os_release(tmp_path, distro_id="rhel")
        result = run_script(
            BOOTSTRAP,
            fakebin,
            cwd=tmp_path,
            extra_env={"OS_RELEASE_FILE": str(os_release)},
        )
        assert result.returncode == 0, result.stderr

    def test_unsupported_distro_dies(self, fakebin, tmp_path):
        os_release = make_os_release(tmp_path, distro_id="arch")
        result = run_script(
            BOOTSTRAP,
            fakebin,
            cwd=tmp_path,
            extra_env={"OS_RELEASE_FILE": str(os_release)},
        )
        assert result.returncode != 0
        assert "RHEL-family" in result.stderr


class TestBootstrapPodmanDockerComposeInstall:
    """Verifies the docker-compose v2 install path that replaces
    podman-compose as the compose backend (see bootstrap-podman.sh
    rationale block)."""

    def test_downloads_docker_compose_when_absent(self, fakebin, tmp_path):
        make_env_example(tmp_path)
        os_release = make_os_release(tmp_path, distro_id="rhel")
        fakebin.remove("docker-compose")  # absent — script should download

        result = run_script(
            BOOTSTRAP,
            fakebin,
            cwd=tmp_path,
            extra_env={"OS_RELEASE_FILE": str(os_release)},
        )
        assert result.returncode == 0, result.stderr
        # curl from a PINNED docker/compose release path — never `latest`,
        # which would root-install whatever upstream serves that day.
        download_calls = [c for c in fakebin.calls() if "curl" in c and "docker/compose/releases" in c]
        assert download_calls
        assert all("/latest/" not in c for c in download_calls)
        # The downloaded binary is checksum-verified before installation.
        assert fakebin.has_call("sha256sum")
        assert fakebin.has_call("install -m 0755")

    def test_removes_existing_podman_compose(self, fakebin, tmp_path):
        """When podman-compose is on PATH it must be uninstalled — otherwise
        it wins the provider preference order and re-introduces the
        volume.subpath bug."""
        make_env_example(tmp_path)
        os_release = make_os_release(tmp_path, distro_id="rhel")
        # podman-compose is already stubbed by the default fixture; that's
        # what the script detects via `command -v podman-compose`.
        result = run_script(
            BOOTSTRAP,
            fakebin,
            cwd=tmp_path,
            extra_env={"OS_RELEASE_FILE": str(os_release)},
        )
        assert result.returncode == 0, result.stderr
        assert fakebin.has_call("dnf remove") and fakebin.has_call("podman-compose")

    def test_enables_podman_socket(self, fakebin, tmp_path):
        """docker-compose v2 talks to Podman over its Docker-API socket;
        that socket must be active."""
        make_env_example(tmp_path)
        os_release = make_os_release(tmp_path, distro_id="rhel")
        result = run_script(
            BOOTSTRAP,
            fakebin,
            cwd=tmp_path,
            extra_env={"OS_RELEASE_FILE": str(os_release)},
        )
        assert result.returncode == 0, result.stderr
        assert fakebin.has_call("systemctl enable --now podman.socket")


class TestBootstrapPodmanPhases:
    def test_first_pass_writes_env_and_exits(self, fakebin, tmp_path):
        make_env_example(tmp_path)
        os_release = make_os_release(tmp_path, distro_id="rhel")
        result = run_script(
            BOOTSTRAP,
            fakebin,
            cwd=tmp_path,
            extra_env={"OS_RELEASE_FILE": str(os_release)},
        )
        assert result.returncode == 0, result.stderr
        assert fakebin.has_call("manage.py init_env")
        # First pass stops before borg / compose up.
        assert not fakebin.has_call("borg init")
        assert not fakebin.has_call("up -d")

    def test_second_pass_brings_stack_up(self, fakebin, tmp_path):
        make_env(tmp_path)
        os_release = make_os_release(tmp_path, distro_id="rhel")
        result = run_script(
            BOOTSTRAP,
            fakebin,
            cwd=tmp_path,
            extra_env={"OS_RELEASE_FILE": str(os_release)},
        )
        assert result.returncode == 0, result.stderr
        assert fakebin.has_call("docker-compose.prod.yml")
        assert fakebin.has_call("up -d")

    def test_no_start_flag_skips_compose_up(self, fakebin, tmp_path):
        make_env(tmp_path)
        os_release = make_os_release(tmp_path, distro_id="rhel")
        result = run_script(
            BOOTSTRAP,
            fakebin,
            cwd=tmp_path,
            args=["--no-start"],
            extra_env={"OS_RELEASE_FILE": str(os_release)},
        )
        assert result.returncode == 0, result.stderr
        assert not fakebin.has_call("up -d")
