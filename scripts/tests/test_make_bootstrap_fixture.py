"""Dry-run tests for scripts/make-bootstrap-fixture.sh.

The fixture builder copies real files (rsync / cp) rather than invoking
binaries, so these run the script for real against a tmp destination and
assert on the assembled tree — no fakebin needed. The full --with-frontend
copy (~200 MB) is deliberately not exercised here; the frontend exclude
behaviour is covered by asserting the excludes on the cheaper default copy.
"""

import os
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
        assert not (dest / "projects").exists()
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
        assert not (dest / "projects").exists()
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
        assert not (dest / "projects").exists()
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
