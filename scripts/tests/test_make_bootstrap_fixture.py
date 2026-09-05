"""Dry-run tests for scripts/make-bootstrap-fixture.sh.

The fixture builder copies real files (rsync / cp) rather than invoking
binaries, so these run the script for real against a tmp destination and
assert on the assembled tree — no fakebin needed. The full --with-frontend
copy (~200 MB) is deliberately not exercised here; the frontend exclude
behaviour is covered by asserting the excludes on the cheaper default copy.
"""

import os
import re
import shutil
import subprocess

from scripts.tests.conftest import REPO_ROOT, SCRIPTS_DIR, requires_built_frontend, requires_rsync

FIXTURE = SCRIPTS_DIR / "make-bootstrap-fixture.sh"

pytestmark = requires_rsync

PLATFORM_APPS = [
    "user",
    "activity",
    "annotations",
    "compute",
    "epicurrents",
    "recordings",
    "media",
    "notifications",
    "library",
    "federation",
]


def _run(dest, *args):
    return subprocess.run(
        ["bash", str(FIXTURE), str(dest), *args],
        check=False,
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )


def _copied_projects(dest):
    """Return the project trees under a package's projects/, ignoring the package marker.

    projects/ always exists in an assembled package, so "no project was copied"
    is a statement about its contents rather than its presence.
    """
    root = dest / "projects"
    if not root.is_dir():
        return []
    return sorted(child.name for child in root.iterdir() if child.is_dir())


def _stub_path(tmp_path, **stubs):
    """Build a directory of stub executables and return a PATH that finds them first.

    The generated start.sh probes the host before it builds anything, so a test aimed
    at a later check has to answer those probes rather than inherit whatever the
    machine running the suite happens to have installed.
    """
    bindir = tmp_path / "stubbin"
    bindir.mkdir(exist_ok=True)
    for name, body in stubs.items():
        stub = bindir / name
        stub.write_text("#!/bin/sh\n" + body + "\n")
        stub.chmod(0o755)
    return f"{bindir}:/usr/bin:/bin"


# Answers the two docker probes and fails anything else with a marker, so a test can
# tell "reached the build" from "stopped at a check" without a daemon anywhere.
_DOCKER_STUB = """
case "$1 $2" in
    "compose version") echo "Docker Compose version v2.29.0"; exit 0 ;;
esac
case "$1" in
    version) echo "28.1.1"; exit 0 ;;
esac
echo "STUB-DOCKER $*" >&2
exit 9
"""


def _stat_stub(uid, gid, mode):
    return f"""
case "$2" in
    %a) echo {mode} ;;
    %u) echo {uid} ;;
    %g) echo {gid} ;;
esac
exit 0
"""


def _activated_plugins(runner_body):
    """Return the plugin names a generated runner writes into EPICURRENTS_PLUGINS.

    Parsed out of the runner's ``ACTIVE_PLUGINS="a,b"`` header rather than matched
    as a literal, so a test can assert that a specific plugin is activated without
    pinning the whole set — plugins compose, and the repo will grow more of them.
    """
    for line in runner_body.splitlines():
        if line.startswith("ACTIVE_PLUGINS="):
            value = line.split("=", 1)[1].strip().strip('"')
            return [p for p in value.split(",") if p]
    raise AssertionError("the generated runner does not declare ACTIVE_PLUGINS")


class TestDefaultFixture:
    """No flags → backend platform parts only, no frontend, no projects."""

    def test_assembles_core_tree(self, tmp_path):
        dest = tmp_path / "fx"
        result = _run(dest)
        assert result.returncode == 0, result.stderr
        for f in (
            "Dockerfile",
            "docker-compose.yml",
            "docker-compose.prod.yml",
            "entrypoint.sh",
            "manage.py",
            ".env.example",
            "requirements.txt",
        ):
            assert (dest / f).is_file(), f"missing {f}"
        for app in PLATFORM_APPS:
            assert (dest / app).is_dir(), f"missing app {app}"
            assert list((dest / app / "migrations").glob("0*.py")), f"{app} migrations"

    def test_excludes_frontend_projects_plugins_and_caches(self, tmp_path):
        dest = tmp_path / "fx"
        assert _run(dest).returncode == 0
        assert not (dest / "frontend").exists()
        assert _copied_projects(dest) == []
        assert not (dest / "plugins").exists()
        assert not list(dest.rglob("__pycache__"))
        assert not list(dest.rglob("*.pyc"))
        assert not list(dest.rglob(".git"))

    def test_generates_runner_and_readme(self, tmp_path):
        dest = tmp_path / "fx"
        assert _run(dest).returncode == 0
        runner = dest / "bootstrap-smoke.sh"
        assert runner.is_file()
        body = runner.read_text()
        assert 'ACTIVE_PROJECT=""' in body
        assert _activated_plugins(body) == []
        assert "WITH_FRONTEND=false" in body
        assert (dest / "FIXTURE_README.md").is_file()


class TestProjectsPackageMarker:
    """projects/ ships in every package, because the bundled Dockerfile copies it."""

    def test_marker_ships_without_a_project(self, tmp_path):
        dest = tmp_path / "fx"
        assert _run(dest).returncode == 0
        assert (dest / "projects" / "__init__.py").is_file()
        assert _copied_projects(dest) == []

    def test_every_dockerfile_copy_source_is_in_the_package(self, tmp_path):
        # The general form of the bug the marker fixes: BuildKit rejects a COPY whose
        # source is absent from the context, reporting it as a checksum error naming
        # an internal ref, so the assembled tree looks correct right up until an
        # operator's first build. Asserted on the default mode because --demo and
        # --dist only add to it, and every COPY source here is a backend path.
        dest = tmp_path / "fx"
        assert _run(dest).returncode == 0
        missing = []
        for line in (dest / "Dockerfile").read_text().splitlines():
            stripped = line.strip()
            if not stripped.startswith("COPY "):
                continue
            words = stripped.split()[1:]
            if any(w.startswith("--from=") for w in words):
                # Sourced from an earlier build stage, not from the package.
                continue
            sources = [w for w in words[:-1] if not w.startswith("--")]
            missing += [src for src in sources if src != "." and not (dest / src).exists()]
        assert not missing, f"Dockerfile copies paths the package does not ship: {missing}"


class TestWithProject:
    """--with-project copies the project tree and the runner activates it."""

    def test_single_project_is_activated(self, tmp_path):
        dest = tmp_path / "fx"
        result = _run(dest, "--with-project", "example")
        assert result.returncode == 0, result.stderr
        assert (dest / "projects" / "example" / "apps.py").is_file()
        assert (dest / "projects" / "__init__.py").is_file()
        assert 'ACTIVE_PROJECT="example"' in (dest / "bootstrap-smoke.sh").read_text()

    def test_unknown_project_fails(self, tmp_path):
        result = _run(tmp_path / "fx", "--with-project", "does-not-exist")
        assert result.returncode != 0


class TestWithPlugin:
    """--with-plugin copies a plugin tree and the runner activates all of them.

    Plugins are the other add-on kind: several can be enabled at once, so the
    runner sets the comma-separated EPICURRENTS_PLUGINS rather than picking one.
    DICOM is the plugin these tests use because it is where the ohif-viewer
    submodule lives — it moved here from projects/dicom, which is what made the
    exclusion test below fail against a path that no longer existed.
    """

    def test_single_plugin_is_activated(self, tmp_path):
        dest = tmp_path / "fx"
        result = _run(dest, "--with-plugin", "dicom")
        assert result.returncode == 0, result.stderr
        copied = dest / "plugins" / "dicom"
        assert copied.is_dir()
        # Assert a real copy happened without naming a file inside the plugin:
        # its internal layout is the plugin's business, and pinning one here is
        # exactly the coupling that broke this suite when DICOM moved.
        assert list(copied.rglob("*.py")), "plugin copied but contains no Python"
        runner = (dest / "bootstrap-smoke.sh").read_text()
        assert _activated_plugins(runner) == ["dicom"]

    def test_ohif_submodule_never_copied(self, tmp_path):
        # A large DICOM viewer submodule gated behind its own build, irrelevant to
        # the backend migrations this fixture exists to exercise.
        dest = tmp_path / "fx"
        assert _run(dest, "--with-plugin", "dicom").returncode == 0
        assert not (dest / "plugins" / "dicom" / "ohif-viewer").exists()
        assert not list(dest.rglob("node_modules"))

    def test_with_plugins_activates_everything_copied(self, tmp_path):
        # --with-plugins expands to every plugins/* tree; whatever it copies must
        # also be activated, or the fixture would ship migrations it never applies.
        dest = tmp_path / "fx"
        assert _run(dest, "--with-plugins").returncode == 0
        copied = sorted(p.name for p in (dest / "plugins").iterdir() if p.is_dir())
        assert copied, "no plugins copied"
        assert _activated_plugins((dest / "bootstrap-smoke.sh").read_text()) == copied

    def test_unknown_plugin_fails(self, tmp_path):
        result = _run(tmp_path / "fx", "--with-plugin", "does-not-exist")
        assert result.returncode != 0

    def test_project_and_plugin_compose(self, tmp_path):
        dest = tmp_path / "fx"
        result = _run(dest, "--with-project", "example", "--with-plugin", "dicom")
        assert result.returncode == 0, result.stderr
        body = (dest / "bootstrap-smoke.sh").read_text()
        assert 'ACTIVE_PROJECT="example"' in body
        assert _activated_plugins(body) == ["dicom"]


@requires_built_frontend
class TestDemoPackage:
    """--demo bundles the compiled dist and human run artifacts, no viewer-dist."""

    def test_bundles_dist_and_runner(self, tmp_path):
        dest = tmp_path / "demo"
        result = _run(dest, "--demo")
        assert result.returncode == 0, result.stderr
        assert (dest / "frontend" / "dist" / "index.html").is_file()
        runner = dest / "start.sh"
        assert runner.is_file()
        assert os.access(runner, os.X_OK)
        assert (dest / "README.md").is_file()

    def test_bundles_update_sh_and_drop_dir(self, tmp_path):
        # Archive-mode self-update (scripts/update.sh) needs the updater bundled
        # at the deployment root, executable, plus its ./update drop dir and a
        # root docker-compose.yml — update.sh's root marker and archive check.
        dest = tmp_path / "demo"
        assert _run(dest, "--demo").returncode == 0
        updater = dest / "update.sh"
        assert updater.is_file()
        assert os.access(updater, os.X_OK)
        assert (dest / "update").is_dir()
        assert (dest / "docker-compose.yml").is_file()

    def test_omits_viewer_dist_source_and_ci_artifacts(self, tmp_path):
        dest = tmp_path / "demo"
        assert _run(dest, "--demo").returncode == 0
        assert not (dest / "frontend" / "viewer-dist").exists()
        assert not (dest / "frontend" / "src").exists()
        assert not (dest / "bootstrap-smoke.sh").exists()
        assert not (dest / "FIXTURE_README.md").exists()
        assert _copied_projects(dest) == []
        assert not (dest / "plugins").exists()

    def test_incompatible_with_frontend_project_and_plugin_flags(self, tmp_path):
        assert _run(tmp_path / "a", "--demo", "--with-frontend").returncode != 0
        assert _run(tmp_path / "b", "--demo", "--with-project", "example").returncode != 0
        assert _run(tmp_path / "c", "--demo", "--with-plugin", "dicom").returncode != 0


@requires_built_frontend
class TestDistPackage:
    """--dist bundles dist + viewer-dist and activates a project."""

    def test_bundles_dist_viewer_dist_and_project(self, tmp_path):
        dest = tmp_path / "dist"
        result = _run(dest, "--dist", "--with-project", "example")
        assert result.returncode == 0, result.stderr
        assert (dest / "frontend" / "dist" / "index.html").is_file()
        assert (dest / "frontend" / "viewer-dist" / "epicurrents-lib.umd.js").is_file()
        assert (dest / "projects" / "example" / "apps.py").is_file()
        runner = (dest / "start.sh").read_text()
        assert 'ACTIVE_PROJECT="example"' in runner
        assert "# Epicurrents — distribution package" in (dest / "README.md").read_text()
        assert not (dest / "bootstrap-smoke.sh").exists()

    def test_without_project_bundles_viewer_dist_no_project(self, tmp_path):
        dest = tmp_path / "dist"
        assert _run(dest, "--dist").returncode == 0
        assert (dest / "frontend" / "viewer-dist" / "epicurrents-lib.umd.js").is_file()
        assert _copied_projects(dest) == []
        assert not (dest / "plugins").exists()
        assert 'ACTIVE_PROJECT=""' in (dest / "start.sh").read_text()

    def test_bundles_and_activates_a_plugin(self, tmp_path):
        # Unlike --demo, a distribution may carry plugins: it is the shape an
        # actual deployment runs, and a plugin's API and migrations are part of it.
        dest = tmp_path / "dist"
        result = _run(dest, "--dist", "--with-plugin", "dicom")
        assert result.returncode == 0, result.stderr
        assert (dest / "plugins" / "dicom").is_dir()
        assert not (dest / "plugins" / "dicom" / "ohif-viewer").exists()
        assert _activated_plugins((dest / "start.sh").read_text()) == ["dicom"]

    def test_demo_and_dist_mutually_exclusive(self, tmp_path):
        assert _run(tmp_path / "x", "--demo", "--dist").returncode != 0

    def test_dist_incompatible_with_frontend_source(self, tmp_path):
        assert _run(tmp_path / "y", "--dist", "--with-frontend").returncode != 0


@requires_built_frontend
class TestDistTailnet:
    """A distribution can put itself on a tailnet and ship its security log."""

    def test_bundles_both_tailnet_scripts_executable(self, tmp_path):
        dest = tmp_path / "dist"
        assert _run(dest, "--dist").returncode == 0
        for name in ("tailscale-join.sh", "tailscale-serve.sh"):
            script = dest / name
            assert script.is_file()
            assert os.access(script, os.X_OK)

    def test_tailnet_scripts_find_the_root_from_the_deployment_level(self, tmp_path):
        # In the repo they sit in scripts/ and walk up one level; bundled here they
        # sit at the root itself. Both resolve by the compose file beside them, so
        # a copy that kept the unconditional `cd ..` would operate on the parent
        # directory — outside the deployment, where there is no .env to refuse on.
        dest = tmp_path / "dist"
        assert _run(dest, "--dist").returncode == 0
        for name in ("tailscale-join.sh", "tailscale-serve.sh"):
            body = (dest / name).read_text()
            assert 'if [ -f "$SCRIPT_DIR/docker-compose.yml" ]' in body

    def test_bundles_tailscale_serve_config(self, tmp_path):
        # docker-compose.yml bind-mounts this file into the tailnet service. Docker
        # materialises a missing source as an empty directory rather than failing,
        # so its absence surfaces as a node that serves nothing.
        dest = tmp_path / "dist"
        assert _run(dest, "--dist").returncode == 0
        assert (dest / "tailscale" / "serve.json").is_file()

    def test_bundles_the_shipper_half_only(self, tmp_path):
        dest = tmp_path / "dist"
        assert _run(dest, "--dist").returncode == 0
        shipped = dest / "examples" / "evidence-host"
        assert (shipped / "docker-compose.shipper.yml").is_file()
        assert (shipped / "promtail-remote.yaml").is_file()
        assert (shipped / "README.md").is_file()
        # The sink belongs on a different machine, built from a checkout.
        assert not (shipped / "docker-compose.evidence.yml").exists()
        assert not (shipped / "loki-config.yaml").exists()
        assert not (shipped / "alertmanager.yml").exists()
        assert not (shipped / "rules.yaml").exists()

    def test_packages_no_examples_file_that_was_not_named(self, tmp_path):
        # examples/ accumulates deployment secrets that are gitignored but sitting
        # on the packager's disk — shipper-password, watchdog-url, smtp-password,
        # per-host .env files. A tarball is handed to someone else, so a sweep of
        # that tree would hand them over too. The allowlist is what prevents it;
        # this asserts nothing has since been added around it.
        dest = tmp_path / "dist"
        assert _run(dest, "--dist").returncode == 0
        packaged = {str(p.relative_to(dest)) for p in (dest / "examples").rglob("*") if p.is_file()}
        assert packaged == {
            "examples/evidence-host/README.md",
            "examples/evidence-host/docker-compose.shipper.yml",
            "examples/evidence-host/promtail-remote.yaml",
        }

    def test_demo_carries_neither(self, tmp_path):
        # A demo is a laptop package. Evidence shipping and host-level VPN
        # installs are deployment concerns and would only be noise there.
        dest = tmp_path / "demo"
        assert _run(dest, "--demo").returncode == 0
        assert not (dest / "tailscale-join.sh").exists()
        assert not (dest / "tailscale-serve.sh").exists()
        assert not (dest / "examples").exists()

    def test_production_initialises_the_backup_repository_before_starting(self, tmp_path):
        # borgmatic does not create a missing repository, it fails — so without
        # this a distribution deployment runs a backup container that fails
        # every cycle from the day it is installed, reporting only into a log
        # nobody reads until a restore is attempted. Found on a live deployment
        # whose /backup volume was empty months after install. Ordering is the
        # real property: initialising after `up` leaves the first cycles failing.
        dest = tmp_path / "dist"
        assert _run(dest, "--dist").returncode == 0
        body = (dest / "start.sh").read_text()
        init_at = body.find("borg init --encryption repokey /backup")
        up_at = body.find('"${COMPOSE[@]}" up -d --build\n')
        assert init_at != -1, "start.sh never initialises the backup repository"
        assert up_at != -1, "the production bring-up line moved; this test needs updating"
        assert init_at < up_at, "the repository must be initialised before the stack starts"
        # Idempotent, so re-running start.sh on a live deployment is safe.
        assert "borg info /backup" in body

    def test_generated_start_sh_parses_and_documents_the_flags(self, tmp_path):
        dest = tmp_path / "dist"
        assert _run(dest, "--dist").returncode == 0
        runner = dest / "start.sh"
        syntax = subprocess.run(["bash", "-n", str(runner)], check=False, capture_output=True, text=True)
        assert syntax.returncode == 0, syntax.stderr
        helped = subprocess.run(
            ["bash", str(runner), "--help"], check=False, cwd=str(dest), capture_output=True, text=True
        )
        assert helped.returncode == 0, helped.stderr
        assert "--tailscale-authkey" in helped.stdout
        assert "--tailscale-mode" in helped.stdout

    def test_start_sh_rejects_an_unknown_tailnet_mode_before_building(self, tmp_path):
        # Argument validation runs ahead of the image build so a typo costs a
        # second rather than ten minutes and a half-spent auth key.
        dest = tmp_path / "dist"
        assert _run(dest, "--dist").returncode == 0
        result = subprocess.run(
            ["bash", str(dest / "start.sh"), "--tailscale-authkey", "tskey-x", "--tailscale-mode", "sevre"],
            check=False,
            cwd=str(dest),
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0
        assert "join" in result.stderr

    def test_demo_start_sh_refuses_a_tailnet_key_it_cannot_honour(self, tmp_path):
        dest = tmp_path / "demo"
        assert _run(dest, "--demo").returncode == 0
        result = subprocess.run(
            ["bash", str(dest / "start.sh"), "--tailscale-authkey", "tskey-x"],
            check=False,
            cwd=str(dest),
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0
        assert "tailscale-join.sh" in result.stderr


@requires_built_frontend
class TestStartShPreflight:
    """start.sh checks the host before it builds; each of these fails late otherwise."""

    def _start(self, dest, path, extra_env=None):
        env = {"PATH": path, "HOME": str(dest)}
        env.update(extra_env or {})
        return subprocess.run(
            ["bash", str(dest / "start.sh")],
            check=False,
            cwd=str(dest),
            capture_output=True,
            text=True,
            env=env,
        )

    def test_refuses_when_docker_is_absent(self, tmp_path):
        dest = tmp_path / "dist"
        assert _run(dest, "--dist").returncode == 0
        empty = tmp_path / "emptybin"
        empty.mkdir()
        result = self._start(dest, f"{empty}:/usr/bin:/bin")
        assert result.returncode != 0
        assert "docker is not installed" in result.stderr

    def test_refuses_a_package_without_the_projects_directory(self, tmp_path):
        # The image build copies projects/ whether or not a project is active, and
        # BuildKit reports a missing COPY source as a checksum error naming an
        # internal ref — a message with nothing in it to act on, arriving after the
        # operator has already installed Docker and started a build.
        dest = tmp_path / "dist"
        assert _run(dest, "--dist").returncode == 0
        shutil.rmtree(dest / "projects")
        result = self._start(dest, _stub_path(tmp_path, docker=_DOCKER_STUB))
        assert result.returncode != 0
        assert "no projects/ directory" in result.stderr
        assert "STUB-DOCKER" not in result.stderr, "the build started despite the incomplete package"

    def test_refuses_a_tree_the_container_user_cannot_write(self, tmp_path):
        # Every service runs as 1000:1000 against a bind mount of the deployment, so
        # a root-owned tree — what extracting a package as root produces — brings the
        # stack up and then fails on its first write, far from the cause. stat and
        # getent are stubbed so the decision is exercised on any host; getent's
        # presence is what the script uses to mean "Linux".
        dest = tmp_path / "dist"
        assert _run(dest, "--dist").returncode == 0
        path = _stub_path(
            tmp_path,
            docker=_DOCKER_STUB,
            getent="exit 0",
            stat=_stat_stub(uid=0, gid=0, mode=755),
        )
        result = self._start(dest, path)
        assert result.returncode != 0
        assert "not writable by uid 1000" in result.stderr
        assert "chown -R 1000:1000" in result.stderr
        assert "STUB-DOCKER" not in result.stderr, "the build started on an unwritable tree"

    def test_accepts_a_tree_owned_by_the_container_user(self, tmp_path):
        # The negative case above passes vacuously if the check refuses everything.
        dest = tmp_path / "dist"
        assert _run(dest, "--dist").returncode == 0
        path = _stub_path(
            tmp_path,
            docker=_DOCKER_STUB,
            getent="exit 0",
            stat=_stat_stub(uid=1000, gid=1000, mode=755),
        )
        result = self._start(dest, path)
        assert "not writable by uid 1000" not in result.stderr
        assert "STUB-DOCKER" in result.stderr, "preflight stopped before the build"

    def test_refuses_to_run_as_root(self, tmp_path):
        # A tree already handed to uid 1000 is writable by uid 1000, so the
        # ownership check passes for root too — root can write anywhere. The .env
        # this would then generate belongs to root, inside a tree the deployment
        # account and the containers both need to write. id is stubbed because the
        # suite does not run as root.
        dest = tmp_path / "dist"
        assert _run(dest, "--dist").returncode == 0
        path = _stub_path(
            tmp_path,
            docker=_DOCKER_STUB,
            getent="exit 0",
            stat=_stat_stub(uid=1000, gid=1000, mode=755),
            id='case "$1" in -u) echo 0 ;; -g) echo 0 ;; *) echo "uid=0(root)" ;; esac',
        )
        result = self._start(dest, path)
        assert result.returncode != 0
        assert "Do not run this as root" in result.stderr
        assert "prepare-host.sh" in result.stderr
        assert "STUB-DOCKER" not in result.stderr, "the build started under root"

    def test_accepts_a_world_writable_tree_it_does_not_own(self, tmp_path):
        # Ownership is a proxy; writability is the property. A tree owned by root but
        # world-writable is one uid 1000 can write, and refusing it would be wrong.
        dest = tmp_path / "dist"
        assert _run(dest, "--dist").returncode == 0
        path = _stub_path(
            tmp_path,
            docker=_DOCKER_STUB,
            getent="exit 0",
            stat=_stat_stub(uid=0, gid=0, mode=777),
        )
        result = self._start(dest, path)
        assert "not writable by uid 1000" not in result.stderr
        assert "STUB-DOCKER" in result.stderr

    def test_refuses_a_docker_engine_below_the_compose_floor(self, tmp_path):
        # 24 accepts the volume subpath syntax in these compose files and mounts the
        # wrong thing, so the floor is a refusal rather than a warning.
        dest = tmp_path / "dist"
        assert _run(dest, "--dist").returncode == 0
        old_docker = _DOCKER_STUB.replace('echo "28.1.1"', 'echo "24.0.7"')
        result = self._start(dest, _stub_path(tmp_path, docker=old_docker))
        assert result.returncode != 0
        assert "Docker Engine 25+" in result.stderr


@requires_built_frontend
class TestPrepareHost:
    """The root-run half of a deployment, which start.sh deliberately cannot do."""

    def test_bundles_the_script_and_the_shared_installer(self, tmp_path):
        dest = tmp_path / "dist"
        assert _run(dest, "--dist").returncode == 0
        prepare = dest / "prepare-host.sh"
        assert prepare.is_file()
        assert os.access(prepare, os.X_OK), "prepare-host.sh must be executable"
        assert (dest / "lib" / "install-docker.sh").is_file()

    def test_installer_is_the_repository_copy_verbatim(self, tmp_path):
        # The point of bundling rather than generating is that a packaged
        # deployment and a cloned one install the same engine from the same
        # repository with the same version floor. A copy that has drifted from the
        # one bootstrap.sh sources gives that up while still looking bundled.
        dest = tmp_path / "dist"
        assert _run(dest, "--dist").returncode == 0
        bundled = (dest / "lib" / "install-docker.sh").read_bytes()
        assert bundled == (SCRIPTS_DIR / "lib" / "install-docker.sh").read_bytes()

    def test_bootstrap_sources_the_same_installer(self, tmp_path):
        # The other half of that property, and the direction that rots quietly:
        # bootstrap.sh could grow its own inline install again and every test here
        # would still pass.
        body = (SCRIPTS_DIR / "bootstrap.sh").read_text()
        assert 'lib/install-docker.sh"' in body
        assert "install_docker_engine" in body
        assert "docker-ce docker-ce-cli" not in body, "bootstrap.sh has re-inlined the package list"

    def test_prepare_host_sources_the_bundled_installer(self, tmp_path):
        dest = tmp_path / "dist"
        assert _run(dest, "--dist").returncode == 0
        body = (dest / "prepare-host.sh").read_text()
        assert ". ./lib/install-docker.sh" in body
        assert "docker-ce docker-ce-cli" not in body

    def test_refuses_to_run_unprivileged(self, tmp_path):
        # The suite does not run as root, so this is the branch reachable here —
        # and it is the one an operator hits by forgetting sudo.
        dest = tmp_path / "dist"
        assert _run(dest, "--dist").returncode == 0
        result = subprocess.run(
            ["bash", str(dest / "prepare-host.sh")],
            check=False,
            cwd=str(dest),
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0
        assert "Run this as root" in result.stderr
        assert "start.sh" in result.stderr, "the refusal should say which script is the unprivileged one"

    def test_a_flag_without_its_value_says_so(self, tmp_path):
        # `shift 2` past the end of the argument list fails under set -e, so the
        # script exited on the shift with nothing printed — indistinguishable from
        # a crash, for a typo.
        dest = tmp_path / "dist"
        assert _run(dest, "--dist").returncode == 0
        for script, flag in (("prepare-host.sh", "--user"), ("start.sh", "--tailscale-mode")):
            result = subprocess.run(
                ["bash", str(dest / script), flag],
                check=False,
                cwd=str(dest),
                capture_output=True,
                text=True,
            )
            assert result.returncode != 0
            assert f"{flag} needs a value" in result.stderr, f"{script} {flag} failed silently"

    def test_documents_its_flags(self, tmp_path):
        dest = tmp_path / "dist"
        assert _run(dest, "--dist").returncode == 0
        helped = subprocess.run(
            ["bash", str(dest / "prepare-host.sh"), "--help"],
            check=False,
            cwd=str(dest),
            capture_output=True,
            text=True,
        )
        assert helped.returncode == 0, helped.stderr
        assert "--user" in helped.stdout
        assert "--no-sudoers" in helped.stdout

    # ── The privileged path ──────────────────────────────────────────────────
    # Everything above tests what the script refuses to do, because the suite is
    # not root and the real run creates accounts. These stub the privileged
    # binaries and log the calls instead, which is the only way to reach the two
    # things the script exists for: the account resolution, and the ordering that
    # decides whether a partial run leaves the tree owned by root.

    def _root_env(self, fakebin, uid_1000_owner=None, groups="sudo"):
        """Make prepare-host.sh believe it is root on a Debian server.

        ``uid_1000_owner`` is the account ``getent passwd 1000`` reports, or None
        for an image where the uid is still free — the one input the account
        resolution branches on. ``groups`` is what that account already belongs
        to, which is what decides whether usermod is called.
        """
        fakebin.stub(
            "id",
            body=f"""
case "${{1:-}}" in
    -u) echo 0 ;;
    -nG) echo "{groups}" ;;
    *) echo "uid=0(root) gid=0(root)" ;;
esac
""",
        )
        owner = f'echo "{uid_1000_owner}:x:1000:1000::/home/{uid_1000_owner}:/bin/bash"' if uid_1000_owner else "exit 2"
        fakebin.stub(
            "getent",
            body=f"""
case "$1 $2" in
    "passwd 1000") {owner} ;;
    "group docker") echo "docker:x:999:" ;;
    passwd*) echo "$2:x:1000:1000::/home/$2:/bin/bash" ;;
esac
""",
        )
        fakebin.stub("adduser")

    def _prepare(self, dest, fakebin, *args):
        return subprocess.run(
            ["bash", str(dest / "prepare-host.sh"), *args],
            check=False,
            cwd=str(dest),
            env={"PATH": f"{fakebin.path}:/usr/bin:/bin", "HOME": str(dest), "LC_ALL": "C"},
            capture_output=True,
            text=True,
        )

    def test_an_existing_uid_1000_account_wins_over_the_requested_name(self, tmp_path, fakebin):
        # Ubuntu cloud images ship a uid-1000 account. Creating a second one there
        # would silently give it 1001, and every container runs as 1000 against a
        # bind mount of the deployment — so the uid decides which account is used,
        # and --user does not.
        dest = tmp_path / "dist"
        assert _run(dest, "--dist").returncode == 0
        self._root_env(fakebin, uid_1000_owner="ubuntu")

        result = self._prepare(dest, fakebin, "--user", "epicurrents", "--no-sudoers")

        assert result.returncode == 0, result.stderr
        assert not fakebin.has_call("adduser"), "created a second account beside the uid-1000 holder"
        assert fakebin.has_call("chown -R ubuntu:ubuntu ."), fakebin.calls()
        assert fakebin.has_call("usermod -aG docker ubuntu"), fakebin.calls()
        # The summary names the account either way, so the assertion is on the
        # explanation: an operator who passed --user and got something else needs to
        # be told why, or the script looks like it ignored them.
        assert "uid 1000 already belongs to" in result.stdout, result.stdout

    def test_creates_the_deployment_account_at_uid_1000_when_the_uid_is_free(self, tmp_path, fakebin):
        # The other half of the same decision: on an image with no uid-1000 account
        # the uid is requested explicitly rather than left to the next free one.
        dest = tmp_path / "dist"
        assert _run(dest, "--dist").returncode == 0
        self._root_env(fakebin, uid_1000_owner=None)

        result = self._prepare(dest, fakebin, "--user", "deployer", "--no-sudoers")

        assert result.returncode == 0, result.stderr
        created = [c for c in fakebin.calls() if c.startswith("adduser")]
        assert len(created) == 1, fakebin.calls()
        assert "--uid 1000" in created[0], created[0]
        assert created[0].endswith("deployer"), created[0]
        assert fakebin.has_call("chown -R deployer:deployer ."), fakebin.calls()

    def test_hands_the_tree_over_before_the_steps_that_can_fail(self, tmp_path, fakebin):
        # Handing the package to the deployment account is the half start.sh cannot
        # do for itself, so it must not sit behind steps that legitimately fail on a
        # given image: a host with no /etc/sudoers.d, or none of the SSH keys the
        # next step wants to copy, still has to end up with the tree handed over
        # rather than owned by root. Asserted twice — the chown lands with the sudo
        # step switched off, and it precedes both later sections in the script,
        # which is where a reordering would show up.
        dest = tmp_path / "dist"
        assert _run(dest, "--dist").returncode == 0
        self._root_env(fakebin, uid_1000_owner="ubuntu", groups="ubuntu docker")

        result = self._prepare(dest, fakebin, "--no-sudoers")

        assert result.returncode == 0, result.stderr
        assert fakebin.has_call("chown -R ubuntu:ubuntu ."), fakebin.calls()
        assert not fakebin.has_call("usermod"), "re-added an account already in the docker group"

        # Anchored on the code rather than on the section headings: the comment
        # recording the original fix names /etc/sudoers.d too, and matching that
        # would compare the wrong two positions.
        body = (dest / "prepare-host.sh").read_text()
        assert body.index("chown -R") < body.index('echo "==> SSH access') < body.index("[ ! -d /etc/sudoers.d ]")


class TestNetworkName:
    """Every package gets its own Docker network unless one is asked for by name."""

    @staticmethod
    def _network(dest):
        for line in (dest / ".env.example").read_text().splitlines():
            if line.startswith("EPICURRENTS_NETWORK_NAME="):
                return line.split("=", 1)[1]
        return None

    def test_default_is_derived_from_the_destination(self, tmp_path):
        dest = tmp_path / "pkg-alpha"
        assert _run(dest).returncode == 0
        assert self._network(dest) == "pkg-alpha"

    def test_two_packages_do_not_share_a_network(self, tmp_path):
        # The property the default exists for. The compose file names its network
        # rather than letting compose scope it per project, so a shared name means a
        # shared `db` alias: a package reaches whichever database answers, and only
        # a password mismatch stops it migrating one it does not own.
        first, second = tmp_path / "alpha", tmp_path / "beta"
        assert _run(first).returncode == 0
        assert _run(second).returncode == 0
        assert self._network(first) != self._network(second)

    def test_an_explicit_name_wins(self, tmp_path):
        dest = tmp_path / "pkg"
        assert _run(dest, "--network-name", "shared-net").returncode == 0
        assert self._network(dest) == "shared-net"

    def test_a_destination_docker_would_reject_is_sanitised(self, tmp_path):
        # Docker network names take letters, digits and _ . - only, so a directory
        # name that is fine on a filesystem can produce one compose cannot create.
        dest = tmp_path / "demo pkg (2)"
        assert _run(dest).returncode == 0
        name = self._network(dest)
        assert name is not None
        assert re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name), name

    def test_a_destination_that_sanitises_away_is_refused(self, tmp_path):
        # Fail closed rather than falling back to the shared default, which would
        # hand the package the one network the derivation exists to keep it off,
        # and say nothing about it.
        dest = tmp_path / "..."
        result = _run(dest)
        assert result.returncode != 0
        assert "--network-name" in result.stderr

    def test_an_invalid_explicit_name_is_refused(self, tmp_path):
        # Refused rather than sanitised: a name that came from a person is a request,
        # and quietly joining a different network than the one asked for is the same
        # class of surprise this default exists to prevent.
        dest = tmp_path / "pkg"
        result = _run(dest, "--network-name", "bad name!")
        assert result.returncode != 0
        assert "--network-name" in result.stderr

    def test_the_key_keeps_its_documentation(self, tmp_path):
        # The rewrite substitutes in place rather than dropping and appending the
        # key, which would separate it from the comment block explaining it.
        dest = tmp_path / "pkg"
        assert _run(dest).returncode == 0
        lines = (dest / ".env.example").read_text().splitlines()
        index = next(i for i, l in enumerate(lines) if l.startswith("EPICURRENTS_NETWORK_NAME="))
        assert any("Docker network" in l for l in lines[max(0, index - 4) : index])


@requires_built_frontend
class TestDeploymentDomain:
    """One domain reaches four keys, and a recipient has no way of knowing that."""

    @staticmethod
    def _env(dest):
        values = {}
        for line in (dest / ".env.example").read_text().splitlines():
            match = re.match(r"^([A-Z_]+)=(.*)$", line)
            if match:
                values[match.group(1)] = match.group(2)
        return values

    def _demo(self, dest, *args):
        return _run(dest, "--demo", *args)

    def test_a_domain_reaches_every_key_that_needs_it(self, tmp_path):
        dest = tmp_path / "pkg"
        result = self._demo(dest, "--proxy-domain", "eeg.example.com", "--acme-email", "ops@example.com")
        assert result.returncode == 0, result.stderr
        env = self._env(dest)
        assert env["PROXY_DOMAIN"] == "eeg.example.com"
        assert env["PROXY_ACME_EMAIL"] == "ops@example.com"
        assert env["FRONTEND_URL"] == "https://eeg.example.com"

    def test_the_host_allowlist_is_appended_to_not_replaced(self, tmp_path):
        # The web container health-checks itself over loopback, so a list without
        # 127.0.0.1 turns every probe into a 400 DisallowedHost and the container
        # reports unhealthy while serving traffic normally.
        dest = tmp_path / "pkg"
        assert self._demo(dest, "--proxy-domain", "eeg.example.com", "--acme-email", "o@e.com").returncode == 0
        hosts = self._env(dest)["ALLOWED_HOSTS"].split(",")
        assert "eeg.example.com" in hosts
        assert "127.0.0.1" in hosts
        assert "localhost" in hosts

    def test_federation_stays_off_unless_asked_for(self, tmp_path):
        # init_env generates the keypair, so the instance URL is the piece that
        # completes the trio. Naming a domain must not be what turns an
        # inter-instance auth surface on.
        dest = tmp_path / "pkg"
        assert self._demo(dest, "--proxy-domain", "eeg.example.com", "--acme-email", "o@e.com").returncode == 0
        assert self._env(dest)["FEDERATION_INSTANCE_URL"] == ""

    def test_federation_sets_the_instance_url_from_the_domain(self, tmp_path):
        dest = tmp_path / "pkg"
        result = self._demo(dest, "--proxy-domain", "eeg.example.com", "--acme-email", "o@e.com", "--federation")
        assert result.returncode == 0, result.stderr
        assert self._env(dest)["FEDERATION_INSTANCE_URL"] == "https://eeg.example.com"

    def test_a_package_without_the_flag_ships_the_defaults(self, tmp_path):
        dest = tmp_path / "pkg"
        assert self._demo(dest).returncode == 0
        env = self._env(dest)
        assert "PROXY_DOMAIN" not in env, "the proxy pair should stay commented out"
        assert env["FRONTEND_URL"] == "http://localhost:5173"
        assert env["FEDERATION_INSTANCE_URL"] == ""

    def test_a_domain_without_a_certificate_contact_is_refused(self, tmp_path):
        # The stack refuses to start with a domain and no ACME contact, so a
        # package assembled that way cannot boot at all. Better caught here than
        # on the server it was carried to.
        result = self._demo(tmp_path / "pkg", "--proxy-domain", "eeg.example.com")
        assert result.returncode != 0
        assert "--acme-email" in result.stderr

    def test_a_url_where_a_hostname_belongs_is_refused(self, tmp_path):
        # FRONTEND_URL is built as https://<domain>, so a scheme here doubles up.
        result = self._demo(tmp_path / "pkg", "--proxy-domain", "https://eeg.example.com", "--acme-email", "o@e.com")
        assert result.returncode != 0
        assert "bare hostname" in result.stderr

    def test_federation_without_a_domain_is_refused(self, tmp_path):
        result = self._demo(tmp_path / "pkg", "--federation")
        assert result.returncode != 0
        assert "--proxy-domain" in result.stderr

    def test_the_flags_are_refused_where_nothing_would_act_on_them(self, tmp_path):
        # The default fixture ships bootstrap-smoke.sh, which brings the stack up
        # locally and never consults PROXY_DOMAIN. Accepting the flag there would
        # write keys no runner reads.
        result = _run(tmp_path / "pkg", "--proxy-domain", "eeg.example.com", "--acme-email", "o@e.com")
        assert result.returncode != 0
        assert "--demo" in result.stderr


class TestGuards:
    """Argument and destination guards."""

    def test_missing_destination_fails(self):
        result = subprocess.run(
            ["bash", str(FIXTURE)],
            check=False,
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0

    def test_nonempty_dest_requires_force(self, tmp_path):
        dest = tmp_path / "fx"
        dest.mkdir()
        (dest / "leftover.txt").write_text("x")
        assert _run(dest).returncode != 0
        assert _run(dest, "--force").returncode == 0
        assert not (dest / "leftover.txt").exists()
