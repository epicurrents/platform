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


class TestBootstrapPodmanSharedSteps:
    """The steps this script silently skipped while it sat untouched.

    It ran its own copy of the bootstrap body for six releases of drift, so a
    Podman deployment never cloned its project, vendored the Pyodide runtime,
    activated the project, generated lead fields, selected the TLS proxy overlay,
    or had its .env values checked. It did not fail while doing so — it succeeded
    into a deployment missing all of that, which is why each one is pinned here
    rather than left to the Docker suite.
    """

    def _run(self, fakebin, tmp_path, *, args=None, **env):
        make_env(tmp_path, **env)
        os_release = make_os_release(tmp_path, distro_id="rhel")
        return run_script(
            BOOTSTRAP,
            fakebin,
            cwd=tmp_path,
            args=args,
            extra_env={"OS_RELEASE_FILE": str(os_release)},
        )

    def test_compose_runs_through_sudo_podman(self, fakebin, tmp_path):
        # The reason the whole body can be shared: it names no runtime, so this is
        # the one place the Podman spelling has to be asserted. Rootful because
        # every service declares uid 1000 against a bind mount of the checkout.
        result = self._run(fakebin, tmp_path)
        assert result.returncode == 0, result.stderr
        assert fakebin.has_call("sudo -E podman compose")
        assert not fakebin.has_call("docker compose")

    def test_vendors_the_pyodide_runtime(self, fakebin, tmp_path):
        result = self._run(fakebin, tmp_path)
        assert result.returncode == 0, result.stderr
        assert fakebin.has_call("manage.py vendor_pyodide")

    def test_generates_lead_fields_after_the_stack_is_up(self, fakebin, tmp_path):
        result = self._run(fakebin, tmp_path)
        assert result.returncode == 0, result.stderr
        calls = fakebin.calls()
        up = next(i for i, call in enumerate(calls) if "up -d" in call and "db" not in call.split("up -d")[1])
        leadfields = next(i for i, call in enumerate(calls) if "generate_compute_static" in call)
        assert up < leadfields, f"expected the stack up at {up} before generation at {leadfields}"

    def test_clones_the_active_project(self, fakebin, tmp_path):
        # Reaching the refusal is the observable: before the shared body, a Podman
        # run with EPICURRENTS_PROJECT set walked straight past the clone and came
        # up without the project rather than saying it could not fetch it.
        result = self._run(fakebin, tmp_path, EPICURRENTS_PROJECT="example")
        assert result.returncode != 0
        assert "EPICURRENTS_PROJECT_REPO is not set" in result.stderr

    def test_activates_the_configured_project(self, fakebin, tmp_path):
        # With the tree already present the clone step drops out of the plan, which
        # is what lets this reach activation.
        (tmp_path / "projects" / "example").mkdir(parents=True)
        result = self._run(fakebin, tmp_path, EPICURRENTS_PROJECT="example")
        assert result.returncode == 0, result.stderr
        assert fakebin.has_call("manage.py activate_project")

    def test_proxy_overlay_added_when_proxy_domain_is_set(self, fakebin, tmp_path):
        result = self._run(fakebin, tmp_path, PROXY_DOMAIN="eeg.example.com")
        assert result.returncode == 0, result.stderr
        assert fakebin.has_call("docker-compose.proxy.yml")

    def test_proxy_overlay_omitted_when_proxy_domain_is_empty(self, fakebin, tmp_path):
        # The negative case, so the one above cannot pass by the overlay being
        # unconditional — which would hand a caddy container to a deployment that
        # terminates TLS elsewhere and cannot get a certificate for it.
        result = self._run(fakebin, tmp_path, PROXY_DOMAIN="")
        assert result.returncode == 0, result.stderr
        assert not fakebin.has_call("docker-compose.proxy.yml")
        assert fakebin.has_call("docker-compose.prod.yml")

    def test_env_value_guard_stops_a_truncating_value(self, fakebin, tmp_path):
        # `$` in a .env value is stripped by compose and the tail is lost in
        # silence, so the guard has to reach this path too.
        make_env(tmp_path)
        (tmp_path / ".env").write_text(
            (tmp_path / ".env").read_text() + "\nADMIN_PASSWORD=abc$def\n"
        )
        os_release = make_os_release(tmp_path, distro_id="rhel")
        result = run_script(
            BOOTSTRAP,
            fakebin,
            cwd=tmp_path,
            extra_env={"OS_RELEASE_FILE": str(os_release)},
        )
        assert result.returncode != 0
        assert "ADMIN_PASSWORD" in result.stdout + result.stderr


class TestBootstrapRunsAsARegularUser:
    """Both bootstraps refuse to run as root, from one shared implementation.

    Worth pinning because the check has two ways to fail and only one is loud. It
    must refuse root — a checkout owned by root is one the unprivileged containers
    cannot read — and it must not abort the run for everyone else. The original
    form was `[ "$(id -u)" -eq 0 ] && die ...` at the top level of the script,
    which is fine there and returns 1 from a function when the user is *not* root;
    moving it into one without noticing would have stopped every run under set -e.
    """

    ROOT_ID = 'case "$1" in -u) echo 0 ;; -g) echo 0 ;; *) echo "uid=0(root)" ;; esac'

    def test_podman_bootstrap_refuses_root(self, fakebin, tmp_path):
        make_env(tmp_path)
        os_release = make_os_release(tmp_path, distro_id="rhel")
        fakebin.stub("id", body=self.ROOT_ID)
        result = run_script(
            BOOTSTRAP,
            fakebin,
            cwd=tmp_path,
            extra_env={"OS_RELEASE_FILE": str(os_release)},
        )
        assert result.returncode != 0
        assert "regular user, not root" in result.stderr

    def test_docker_bootstrap_refuses_root(self, fakebin, tmp_path):
        make_env(tmp_path)
        fakebin.stub("id", body=self.ROOT_ID)
        result = run_script("bootstrap.sh", fakebin, cwd=tmp_path)
        assert result.returncode != 0
        assert "regular user, not root" in result.stderr

    def test_a_regular_user_is_not_stopped_by_the_check(self, fakebin, tmp_path):
        # The half that fails silently: the fixture's default id already reports
        # 1000, so this passes only if the check returns cleanly rather than
        # ending the run with the status of a failed test.
        make_env(tmp_path)
        result = run_script("bootstrap.sh", fakebin, cwd=tmp_path)
        assert result.returncode == 0, result.stderr
        assert "regular user, not root" not in result.stderr
