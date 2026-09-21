"""Dry-run behavioural tests for scripts/update.sh.

These exercise update.sh's own control flow with the ``fakebin`` harness:
binaries (docker, git, rsync, tar, curl) are stubbed and log every call in
order, so a test can assert which commands ran, with which flags, in which
order, for a given mode / flag combination. They catch regressions in the
*script*; the companion test_update_targets.py catches drift in the *targets*
the script depends on.

No real container, database, or archive is involved — this is a Tier 2 mocked
dry-run, like the bootstrap-script tests.
"""

import gzip as gzlib
import hashlib
import json
import os
import shutil
import subprocess
import sys

import pytest

from scripts.tests.conftest import SCRIPTS_DIR, make_env, run_script

RELEASE_SIGN = SCRIPTS_DIR / "lib" / "release_sign.py"

# A docker stub that reports the db / borg containers as running (so
# ensure_db_up does not take the "start it" branch) and succeeds otherwise.
# The `exec` branch drains stdin the way `docker compose exec -T` does: the
# rollback path streams the dump in with `gunzip -c db.sql.gz | … exec -T … psql`,
# and a stub that exits without reading would leave gunzip writing to a closed
# pipe (SIGPIPE, exit 141), failing the pipeline under `set -o pipefail`.
DOCKER_PS_RUNNING = r"""
case "$*" in
    *" ps "*) echo running ;;
    *" exec "*) cat >/dev/null 2>&1 || true ;;
esac
"""

# Re-exec the real cp so backup / .env-restore copies actually happen; the
# conftest default stubs cp to a no-op to protect the host during bootstrap.
REAL_CP = 'exec /bin/cp "$@"'


def _index_of(calls, substring):
    for i, line in enumerate(calls):
        if substring in line:
            return i
    return -1


def _docker_stub(*extra_cases):
    """A DOCKER_PS_RUNNING-shaped stub with extra ``case`` arms ahead of the defaults.

    Each arm is a complete ``pattern) body ;;`` string. Placed first so a test can
    make one compose call misbehave — a service that is not running, a step that
    fails — while everything else keeps the running-stack answers.
    """
    return "\ncase \"$*\" in\n" + "\n".join(extra_cases) + r"""
    *" ps "*) echo running ;;
    *" exec "*) cat >/dev/null 2>&1 || true ;;
esac
"""


#: Real hashing behind the harness's no-op sha256sum stub, so a package's hash
#: check runs for real: the coreutils binary where there is one, Perl's shasum
#: elsewhere (macOS).
REAL_SHA256SUM = 'if [ -x /usr/bin/sha256sum ]; then exec /usr/bin/sha256sum "$@"; fi\nexec shasum -a 256 "$@"'

#: Real tar behind a logging stub, so the call log shows what was extracted.
REAL_TAR = 'exec /usr/bin/tar "$@"'

#: An openssl that answers as OpenSSL 3 and verifies every signature.
OPENSSL_VERIFIES = 'case "$1" in version) echo "OpenSSL 3.0.13 30 Jan 2024" ;; esac'

#: An openssl that answers as OpenSSL 3 and rejects every signature.
OPENSSL_REJECTS = 'case "$1" in version) echo "OpenSSL 3.0.13 30 Jan 2024"; exit 0 ;; esac\nexit 1'

#: The system openssl of a Mac, or of an old distribution: no Ed25519.
OPENSSL_TOO_OLD = 'case "$1" in version) echo "LibreSSL 3.3.6" ;; esac'


def _deploy(fakebin, tmp_path, *, installed_version="0.1.0", **env):
    """Set up a fake deployment root: a .env plus running-container stubs."""
    make_env(tmp_path, **env)
    # update.sh refuses a tree uid 1000 cannot write, and pytest's tmp_path belongs
    # to whoever runs the suite. World-writable satisfies the check without needing
    # a uid the test cannot have — the same escape a real deployment gets.
    tmp_path.chmod(0o777)
    fakebin.stub("docker", body=DOCKER_PS_RUNNING)
    fakebin.stub("cp", body=REAL_CP)
    fakebin.stub("sha256sum", body=REAL_SHA256SUM)
    fakebin.stub("tar", body=REAL_TAR)
    if installed_version:
        (tmp_path / "epicurrents").mkdir(exist_ok=True)
        (tmp_path / "epicurrents" / "version.py").write_text(f'__version__ = "{installed_version}"\n')


def _helper(*args):
    """Run scripts/lib/release_sign.py with the suite's own interpreter, which has cryptography."""
    return subprocess.run(
        [sys.executable, str(RELEASE_SIGN), *args], check=True, capture_output=True, text=True
    ).stdout


def _sign_key(tmp_path):
    """Generate a release key pair; returns (private, public) paths."""
    key = tmp_path / "keys" / "release.key"
    _helper("keygen", str(key))
    return key, key.with_name("release.key.pub")


def _build_package(
    tmp_path,
    *,
    name="epicurrents-test",
    top="epicurrents-test",
    version="0.2.0",
    files=None,
    filelist=True,
    manifest=True,
    project="",
    plugins="",
    sign_key=None,
    min_updater_version=2,
    tar_args=(),
):
    """Build a real distribution-shaped tarball under update/ plus its sidecars.

    The tree holds a compose file and a version module, plus ``files`` (relative
    path → content), wrapped in one top-level directory the way the packager
    wraps a package. Returns the archive path; the manifest and signature sit
    beside it under the names update.sh looks for.
    """
    tree = tmp_path / "pkg-tree" / top
    tree.mkdir(parents=True)
    (tree / "docker-compose.yml").write_text("services: {}\n")
    (tree / "epicurrents").mkdir()
    (tree / "epicurrents" / "version.py").write_text(f'__version__ = "{version}"\n')
    for rel, content in (files or {}).items():
        path = tree / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    if filelist:
        (tree / "FILELIST").write_text("")
        names = sorted(str(p.relative_to(tree)) for p in tree.rglob("*") if p.is_file())
        (tree / "FILELIST").write_text("\n".join(names) + "\n")
    (tmp_path / "update").mkdir(exist_ok=True)
    archive = tmp_path / "update" / f"{name}.tar.gz"
    subprocess.run(
        ["/usr/bin/tar", "-czf", str(archive), *tar_args, "-C", str(tmp_path / "pkg-tree"), top],
        check=True,
        env={**os.environ, "COPYFILE_DISABLE": "1"},
    )
    if manifest:
        _write_manifest(archive, version=version, project=project, plugins=plugins,
                        min_updater_version=min_updater_version, sign_key=sign_key)
    return archive


def _write_manifest(archive, *, version, project="", plugins="", min_updater_version=2, sign_key=None, sha256=None):
    digest = sha256 or hashlib.sha256(archive.read_bytes()).hexdigest()
    manifest = archive.with_name(archive.name + ".manifest.json")
    _helper(
        "manifest", "--package", archive.name, "--sha256", digest, "--size", str(archive.stat().st_size),
        "--version", version, "--platform-compatible", ">=0.1,<0.2", "--project", project,
        "--plugins", plugins, "--min-updater-version", str(min_updater_version), "--out", str(manifest),
    )
    if sign_key is not None:
        signature = manifest.with_name(archive.name + ".manifest.sig")
        signature.write_text(_helper("sign", str(sign_key), str(manifest)))
    return manifest


def _progress(result):
    """The ``::`` lines of a run, in order, without the prefix."""
    return [line[2:] for line in result.stdout.splitlines() if line.startswith("::")]


def _extracted_to_disk(fakebin):
    """Whether any tar call unpacked an archive into a directory (as opposed to listing or streaming one)."""
    return any(c.startswith("tar") and " -x" in c and " -C " in c for c in fakebin.calls())


def _nothing_touched(fakebin, root):
    """The assertions every refusal shares: no extraction, no overlay, no snapshot, no flag."""
    assert not _extracted_to_disk(fakebin), "the archive was extracted"
    assert not fakebin.has_call("rsync"), "the tree was overlaid"
    assert not (root / "backups").exists(), "a snapshot was taken"
    assert not (root / "update" / "maintenance.json").exists(), "the maintenance flag went up"
    assert not fakebin.has_call("stop web"), "the stack was stopped"


def _read_flag(root):
    flag = root / "update" / "maintenance.json"
    assert flag.is_file(), "no maintenance flag at update/maintenance.json"
    return json.loads(flag.read_text())


#: The preflight reads ownership with `stat -c`, which is GNU-only — the guard is a
#: no-op on macOS by design, so its tests are too.
requires_gnu_stat = pytest.mark.skipif(
    subprocess.run(["stat", "-c", "%u", "."], capture_output=True, check=False).returncode != 0,
    reason="the ownership preflight is Linux-only (stat -c)",
)


class TestOwnershipPreflight:
    """A tree uid 1000 cannot write is a deployment that fails on its first write.

    It arrives that way from an archive built elsewhere: tar records the builder's
    uid and an update applied as root preserves it. Refusing up front turns a
    permission error hours later into one sentence before anything is touched.
    """

    @requires_gnu_stat
    def test_archive_mode_refuses_a_tree_the_container_user_cannot_write(self, fakebin, tmp_path):
        _deploy(fakebin, tmp_path)
        tmp_path.chmod(0o700)
        result = run_script("update.sh", fakebin, cwd=tmp_path)
        assert result.returncode != 0
        assert "not writable by uid 1000" in result.stderr
        assert "chown -R 1000:1000" in result.stderr

    @requires_gnu_stat
    def test_repo_mode_warns_but_proceeds(self, fakebin, tmp_path):
        # A checkout owned by a developer's own uid is a legitimate arrangement the
        # check cannot tell from a broken deployment, so it says so and continues.
        _deploy(fakebin, tmp_path)
        tmp_path.chmod(0o700)
        result = run_script("update.sh", fakebin, cwd=tmp_path, args=["--from", "repo", "--no-pull"])
        assert result.returncode == 0, result.stderr
        # warn() writes to stdout, unlike die().
        assert "not writable by uid 1000" in result.stdout

    @requires_gnu_stat
    def test_a_writable_tree_passes(self, fakebin, tmp_path):
        _deploy(fakebin, tmp_path)
        result = run_script("update.sh", fakebin, cwd=tmp_path, args=["--from", "repo", "--no-pull"])
        assert result.returncode == 0, result.stderr
        assert "not writable by uid 1000" not in result.stdout + result.stderr


#: A podman stub shaped like DOCKER_PS_RUNNING, plus the `--version` string that
#: identifies the runtime. Detection reads that name rather than trusting the command
#: name, because podman-docker installs a `docker` that is this same binary.
PODMAN_PS_RUNNING = r"""
case "$1" in
    --version) echo "podman version 5.8.2"; exit 0 ;;
esac
case "$*" in
    *" ps "*) echo running ;;
    *" exec "*) cat >/dev/null 2>&1 || true ;;
esac
"""


class TestRuntimeDetection:
    """An update has to drive the same runtime start.sh brought the stack up on.

    Picking the other one does not fail loudly — compose would talk to a daemon that
    knows nothing of these containers and cheerfully create a second, empty stack.
    """

    @requires_gnu_stat
    def test_a_docker_host_drives_docker_compose(self, fakebin, tmp_path):
        _deploy(fakebin, tmp_path)
        result = run_script("update.sh", fakebin, cwd=tmp_path, args=["--from", "repo", "--no-pull"])
        assert result.returncode == 0, result.stderr
        assert fakebin.has_call("docker compose -f docker-compose.yml")
        assert not fakebin.has_call("podman compose")

    @requires_gnu_stat
    def test_a_podman_host_drives_podman_compose_through_sudo(self, fakebin, tmp_path):
        _deploy(fakebin, tmp_path)
        fakebin.remove("docker")
        fakebin.stub("podman", body=PODMAN_PS_RUNNING)
        result = run_script("update.sh", fakebin, cwd=tmp_path, args=["--from", "repo", "--no-pull"])
        assert result.returncode == 0, result.stderr
        # The conftest sudo stub strips sudo's flags and execs the rest, so a call
        # logged as `podman compose …` is one that arrived through sudo.
        assert fakebin.has_call("podman compose -f docker-compose.yml")
        assert fakebin.has_call("sudo -E podman compose")

    @requires_gnu_stat
    def test_podman_wearing_the_docker_name_is_not_taken_for_docker(self, fakebin, tmp_path):
        _deploy(fakebin, tmp_path)
        fakebin.stub("docker", body=PODMAN_PS_RUNNING)
        fakebin.stub("podman", body=PODMAN_PS_RUNNING)
        result = run_script("update.sh", fakebin, cwd=tmp_path, args=["--from", "repo", "--no-pull"])
        assert result.returncode == 0, result.stderr
        assert fakebin.has_call("sudo -E podman compose")

    def test_no_runtime_at_all_aborts(self, fakebin, tmp_path):
        # Both, deliberately: the shared fixture stubs podman for the
        # bootstrap-podman.sh tests, so removing only docker leaves a host that
        # still has a runtime and this would pass without testing anything.
        _deploy(fakebin, tmp_path)
        fakebin.remove("docker")
        fakebin.remove("podman")
        result = run_script("update.sh", fakebin, cwd=tmp_path, args=["--from", "repo", "--no-pull"])
        assert result.returncode != 0
        assert "No container runtime found" in result.stderr


class TestGuards:
    def test_missing_env_aborts(self, fakebin, tmp_path):
        # No .env written → the deployment is uninitialized.
        result = run_script("update.sh", fakebin, cwd=tmp_path)
        assert result.returncode != 0
        assert "No .env" in result.stderr

    def test_unknown_argument_aborts(self, fakebin, tmp_path):
        _deploy(fakebin, tmp_path)
        result = run_script("update.sh", fakebin, cwd=tmp_path, args=["--bogus"])
        assert result.returncode != 0
        assert "Unknown argument" in result.stderr

    def test_value_flag_without_value_aborts(self, fakebin, tmp_path):
        _deploy(fakebin, tmp_path)
        result = run_script("update.sh", fakebin, cwd=tmp_path, args=["--from"])
        assert result.returncode != 0
        assert "requires a value" in result.stderr

    def test_bad_mode_aborts(self, fakebin, tmp_path):
        _deploy(fakebin, tmp_path)
        result = run_script("update.sh", fakebin, cwd=tmp_path, args=["--from", "sideways"])
        assert result.returncode != 0
        assert "Unknown --from mode" in result.stderr


class TestSharedTail:
    """The back-up → build → migrate → recreate sequence, via repo mode."""

    def test_backup_runs_before_migrations(self, fakebin, tmp_path):
        _deploy(fakebin, tmp_path)
        result = run_script("update.sh", fakebin, cwd=tmp_path, args=["--from", "repo", "--no-pull"])
        assert result.returncode == 0, result.stderr
        calls = fakebin.calls()
        dump_i = _index_of(calls, "pg_dump")
        stop_i = _index_of(calls, "stop web celery celery-beat")
        migrate_i = _index_of(calls, "manage.py migrate")
        recreate_i = _index_of(calls, "--force-recreate web celery celery-beat")
        assert -1 < dump_i < stop_i < migrate_i < recreate_i, (
            f"expected backup → stop → migrate → recreate order, got "
            f"dump={dump_i} stop={stop_i} migrate={migrate_i} recreate={recreate_i}"
        )

    def test_migrate_runs_every_update(self, fakebin, tmp_path):
        _deploy(fakebin, tmp_path)
        run_script("update.sh", fakebin, cwd=tmp_path, args=["--from", "repo", "--no-pull"])
        assert fakebin.has_call("manage.py migrate")
        assert fakebin.has_call("manage.py collectstatic")

    def test_vendored_assets_are_produced_after_migrations(self, fakebin, tmp_path):
        # Both halves of frontend/vendor. Nothing else regenerates the tree — it is
        # excluded from this script's rsync — so a deployment that loses it (a fresh
        # host, a restored snapshot) gets it back only here. Both generators read the
        # migrated schema, which fixes their position after step 5.
        _deploy(fakebin, tmp_path)
        result = run_script("update.sh", fakebin, cwd=tmp_path, args=["--from", "repo", "--no-pull"])
        assert result.returncode == 0, result.stderr
        calls = fakebin.calls()
        migrate_i = _index_of(calls, "manage.py migrate")
        pyodide_i = _index_of(calls, "manage.py vendor_pyodide")
        leadfield_i = _index_of(calls, "manage.py generate_compute_static")
        assert -1 < migrate_i < pyodide_i, "the Pyodide check must follow migrations"
        assert -1 < migrate_i < leadfield_i, "lead-field generation must follow migrations"

    def test_the_pyodide_tree_is_only_vendored_when_the_check_fails(self, fakebin, tmp_path):
        # The check is a local hash sweep and the vendoring is a 47 MiB download, so an
        # update that finds a good tree must not re-fetch it.
        _deploy(fakebin, tmp_path)
        run_script("update.sh", fakebin, cwd=tmp_path, args=["--from", "repo", "--no-pull"])
        vendoring = [call for call in fakebin.calls() if "vendor_pyodide" in call and "--check" not in call]
        assert not vendoring, f"expected no re-vendoring when --check passes, got {vendoring}"

    def test_no_backup_skips_the_dump(self, fakebin, tmp_path):
        _deploy(fakebin, tmp_path)
        result = run_script(
            "update.sh",
            fakebin,
            cwd=tmp_path,
            args=["--from", "repo", "--no-pull", "--no-backup"],
        )
        assert result.returncode == 0, result.stderr
        assert not fakebin.has_call("pg_dump")
        assert fakebin.has_call("manage.py migrate")

    def test_force_recreate_is_scoped_to_app_services(self, fakebin, tmp_path):
        # The DB must never be bounced — recreate names only the app services.
        _deploy(fakebin, tmp_path)
        run_script("update.sh", fakebin, cwd=tmp_path, args=["--from", "repo", "--no-pull"])
        recreate = [c for c in fakebin.calls() if "--force-recreate" in c]
        assert recreate, "expected a force-recreate call"
        assert all("web celery celery-beat" in c for c in recreate), recreate

    def test_health_check_polls_the_endpoint(self, fakebin, tmp_path):
        _deploy(fakebin, tmp_path)
        fakebin.stub("curl")
        run_script("update.sh", fakebin, cwd=tmp_path, args=["--from", "repo", "--no-pull"])
        assert fakebin.has_call("/api/v1/health")


class TestRepoMode:
    def test_pull_is_fast_forward_only_and_never_remaps_origin(self, fakebin, tmp_path):
        _deploy(fakebin, tmp_path)
        result = run_script("update.sh", fakebin, cwd=tmp_path, args=["--from", "repo"])
        assert result.returncode == 0, result.stderr
        assert fakebin.has_call("pull --ff-only")
        # --repo was removed; an update must never rewrite the remote URL.
        assert not fakebin.has_call("remote set-url")

    def test_builds_frontend_in_repo_mode(self, fakebin, tmp_path):
        _deploy(fakebin, tmp_path)
        run_script("update.sh", fakebin, cwd=tmp_path, args=["--from", "repo", "--no-pull"])
        assert fakebin.has_call("--profile build run --rm frontend-build")

    def test_the_active_project_is_pulled_before_anything_is_built_from_it(self, fakebin, tmp_path):
        """The project is a separate clone, not a submodule, so neither the
        platform pull nor `submodule update` reaches it. Left un-pulled it stays
        at whatever bootstrap cloned, and both builds below bake that stale tree
        in — the image its dependencies and Python source, the bundle its Vue
        plugin — without either build failing.
        """
        _deploy(fakebin, tmp_path, EPICURRENTS_PROJECT="thing")
        (tmp_path / "projects" / "thing" / ".git").mkdir(parents=True)
        result = run_script("update.sh", fakebin, cwd=tmp_path, args=["--from", "repo"])
        assert result.returncode == 0, result.stderr
        calls = fakebin.calls()
        pull = _index_of(calls, "-C projects/thing pull")
        frontend = _index_of(calls, "--profile build run --rm frontend-build")
        # The image build is the bare `compose build`; matched on the trailing
        # word so it cannot resolve to the frontend service's `--profile build`,
        # which would make this assertion about the wrong call.
        image = next((i for i, c in enumerate(calls) if c.rstrip().endswith(" build")), -1)
        assert pull >= 0, "the active project was never pulled"
        assert frontend >= 0 and image >= 0, "the builds this orders against did not run"
        assert pull < frontend, "the frontend bundle is built from a stale project checkout"
        assert pull < image, "the image is built from a stale project checkout"

    def test_a_pinned_project_is_left_alone(self, fakebin, tmp_path):
        """Pinning a project to a tag or a commit is a manual operation, and a
        detached checkout has no upstream to fast-forward. Pulling anyway would
        silently undo the pin, which is the one thing an operator who set it
        would not expect an update to do.
        """
        _deploy(fakebin, tmp_path, EPICURRENTS_PROJECT="thing")
        (tmp_path / "projects" / "thing" / ".git").mkdir(parents=True)
        fakebin.stub("git", body='case "$*" in *symbolic-full-name*) exit 1 ;; esac')
        result = run_script("update.sh", fakebin, cwd=tmp_path, args=["--from", "repo"])
        assert result.returncode == 0, result.stderr
        assert not fakebin.has_call("-C projects/thing pull"), "a pinned project must not be moved"
        assert "pinned" in result.stdout, "the operator is not told the project was skipped"

    def test_a_project_that_is_not_a_checkout_is_reported_rather_than_skipped(self, fakebin, tmp_path):
        """A directory that was copied rather than cloned cannot be pulled, and
        looks identical to an up-to-date one from the build's side. Saying so is
        the difference between an operator knowing to update it by hand and
        finding out from a stale deployment.
        """
        _deploy(fakebin, tmp_path, EPICURRENTS_PROJECT="thing")
        (tmp_path / "projects" / "thing").mkdir(parents=True)
        fakebin.stub("git", body='case "$*" in *--git-dir*) exit 1 ;; esac')
        result = run_script("update.sh", fakebin, cwd=tmp_path, args=["--from", "repo"])
        assert result.returncode == 0, result.stderr
        assert not fakebin.has_call("-C projects/thing pull")
        assert "not a git checkout" in result.stdout

    def test_no_project_pull_when_none_is_configured(self, fakebin, tmp_path):
        _deploy(fakebin, tmp_path, EPICURRENTS_PROJECT="")
        result = run_script("update.sh", fakebin, cwd=tmp_path, args=["--from", "repo"])
        assert result.returncode == 0, result.stderr
        assert not fakebin.has_call("-C projects/")


class TestArchiveMode:
    def _seed_archive(self, fakebin, tmp_path):
        # A real, unsigned package: the manifest is checked for consistency and
        # the missing signature is a warning on the manual path.
        _build_package(tmp_path)
        fakebin.stub("rsync")

    def test_overlay_sync_never_deletes_and_preserves_operator_state(self, fakebin, tmp_path):
        _deploy(fakebin, tmp_path)
        self._seed_archive(fakebin, tmp_path)
        result = run_script("update.sh", fakebin, cwd=tmp_path)
        assert result.returncode == 0, result.stderr
        rsync = [c for c in fakebin.calls() if c.startswith("rsync")]
        assert rsync, "archive mode must rsync the new tree"
        line = rsync[0]
        # The data-loss footgun guard: overlay-only, never --delete at the root.
        assert "--delete" not in line, line
        for excl in ("--exclude=/.env", "--exclude=/backups/", "--exclude=/update/"):
            assert excl in line, f"missing {excl} in: {line}"

    def test_discovers_newest_archive_in_update_dir(self, fakebin, tmp_path):
        _deploy(fakebin, tmp_path)
        self._seed_archive(fakebin, tmp_path)
        run_script("update.sh", fakebin, cwd=tmp_path)
        assert fakebin.has_call("epicurrents-test.tar.gz")

    def test_missing_archive_aborts(self, fakebin, tmp_path):
        _deploy(fakebin, tmp_path)
        (tmp_path / "update").mkdir()  # empty: nothing matching to apply
        result = run_script("update.sh", fakebin, cwd=tmp_path)
        assert result.returncode != 0
        assert "No archive" in result.stderr


class TestRollback:
    def _seed_snapshot(self, tmp_path, *, with_db=True, with_env=True, with_code=False):
        snap = tmp_path / "backups" / "pre-update-20200101-000000"
        snap.mkdir(parents=True)
        if with_db:
            with gzlib.open(snap / "db.sql.gz", "wt") as fh:
                fh.write("-- dump\nSELECT 1;\n")
        if with_env:
            (snap / ".env").write_text("DJANGO_MODE=production\nHOST_PORT=8000\n")
        if with_code:
            # A real, readable archive: the script lists it before restoring.
            tree = tmp_path / "snapshot-tree"
            tree.mkdir()
            (tree / "docker-compose.yml").write_text("services: {}\n")
            subprocess.run(["tar", "-czf", str(snap / "code.tar.gz"), "-C", str(tree), "."], check=True)
        (snap / "MANIFEST").write_text("mode=archive\n")
        return snap

    def test_restores_in_order_with_single_transaction(self, fakebin, tmp_path):
        _deploy(fakebin, tmp_path)
        self._seed_snapshot(tmp_path)
        result = run_script("update.sh", fakebin, cwd=tmp_path, args=["--rollback", "--yes"])
        assert result.returncode == 0, result.stderr
        calls = fakebin.calls()
        stop_i = _index_of(calls, "stop web celery celery-beat")
        restore_i = _index_of(calls, "psql --single-transaction")
        recreate_i = _index_of(calls, "--force-recreate web celery celery-beat")
        assert -1 < stop_i < restore_i < recreate_i, f"stop={stop_i} restore={restore_i} recreate={recreate_i}"
        # The restore must be atomic — ON_ERROR_STOP makes a failure roll back.
        assert fakebin.has_call("ON_ERROR_STOP=1")

    def test_no_snapshot_aborts(self, fakebin, tmp_path):
        _deploy(fakebin, tmp_path)
        result = run_script("update.sh", fakebin, cwd=tmp_path, args=["--rollback", "--yes"])
        assert result.returncode != 0
        assert "pre-update snapshot" in result.stderr

    def test_incomplete_snapshot_is_refused_before_touching_anything(self, fakebin, tmp_path):
        _deploy(fakebin, tmp_path)
        self._seed_snapshot(tmp_path, with_db=False)  # missing db.sql.gz
        result = run_script("update.sh", fakebin, cwd=tmp_path, args=["--rollback", "--yes"])
        assert result.returncode != 0
        assert "incomplete" in result.stderr
        # All-or-nothing: a refused rollback must not have restored or recreated.
        assert not fakebin.has_call("psql --single-transaction")
        assert not fakebin.has_call("--force-recreate")

    def test_the_restore_replaces_the_schema_inside_the_same_transaction(self, fakebin, tmp_path):
        """pg_dump --clean drops only what it dumped, so a table the failed release
        created survives a plain restore while the restored django_migrations says
        its migration never ran; the next migrate dies on "relation already
        exists" and rolls back again, forever. The stream psql receives has to
        drop and recreate the schema *before* the dump, and inside the one
        transaction, so a failed restore still leaves the schema as it was. What
        PostgreSQL then does with that stream is test_update_postgres.py's job.
        """
        _deploy(fakebin, tmp_path)
        self._seed_snapshot(tmp_path)
        capture = tmp_path / "restore-stdin.sql"
        fakebin.stub("docker", body=_docker_stub(f'*" exec "*) cat > "{capture}" ;;'))
        result = run_script("update.sh", fakebin, cwd=tmp_path, args=["--rollback", "--yes"])
        assert result.returncode == 0, result.stderr
        stream = capture.read_text()
        drop = stream.index("DROP SCHEMA IF EXISTS public CASCADE;")
        create = stream.index("CREATE SCHEMA public;")
        dump = stream.index("-- dump")
        assert stream.index("SET lock_timeout") < drop < create < dump, stream
        # One transaction for the preamble and the dump alike — a separate psql
        # call for the drop would commit an empty schema before the restore began.
        restores = [c for c in fakebin.calls() if "psql" in c]
        assert len(restores) == 1 and "--single-transaction" in restores[0], restores

    def test_the_restore_waits_a_bounded_time_for_its_locks(self, fakebin, tmp_path):
        # Anything still holding a share lock — a backup dumping the database —
        # blocks the schema drop; without a bound the rollback hangs instead of
        # failing. The bound is overridable so the real-database test can make it
        # short, and nothing else should ever set it.
        _deploy(fakebin, tmp_path)
        self._seed_snapshot(tmp_path)
        capture = tmp_path / "restore-stdin.sql"
        fakebin.stub("docker", body=_docker_stub(f'*" exec "*) cat > "{capture}" ;;'))
        run_script(
            "update.sh", fakebin, cwd=tmp_path, args=["--rollback", "--yes"],
            extra_env={"UPDATE_RESTORE_LOCK_TIMEOUT": "7s"},
        )
        assert "SET lock_timeout = '7s';" in capture.read_text()

    def test_rollback_reruns_the_post_build_steps_and_checks_health(self, fakebin, tmp_path):
        """A rollback used to rebuild the images and recreate the stack, and stop
        there. The failed release's vendored assets stayed under the restored
        code, its collected static files stayed in the volume, and nothing
        checked the result was serving — the one run where that check matters
        most. After the rebuild a rollback runs the same tail an update does.
        """
        _deploy(fakebin, tmp_path)
        self._seed_snapshot(tmp_path, with_code=True)
        # Stubbed: the real rsync --delete would sweep the harness out of tmp_path.
        fakebin.stub("rsync")
        fakebin.stub("curl")
        result = run_script("update.sh", fakebin, cwd=tmp_path, args=["--rollback", "--yes"])
        assert result.returncode == 0, result.stderr
        calls = fakebin.calls()
        restore_i = _index_of(calls, "psql --single-transaction")
        build_i = _index_of(calls, "--profile vendor build")
        static_i = _index_of(calls, "manage.py collectstatic")
        viewer_i = _index_of(calls, "vendor_viewer --check")
        pyodide_i = _index_of(calls, "vendor_pyodide --check")
        leadfield_i = _index_of(calls, "generate_compute_static")
        recreate_i = _index_of(calls, "--force-recreate web celery celery-beat")
        health_i = _index_of(calls, "/api/v1/health")
        order = [restore_i, build_i, static_i, viewer_i, pyodide_i, leadfield_i, recreate_i, health_i]
        assert all(i > -1 for i in order), f"a rollback step is missing: {order}"
        assert order == sorted(order), f"expected restore → build → static → vendor → recreate → health, got {order}"

    def test_rollback_builds_the_vendor_profile_too(self, fakebin, tmp_path):
        # The vendoring steps run from the `vendor` service's image, which a bare
        # `build` skips; they would then build it themselves, from the restored
        # code, which happens to be right but reads as a vendoring failure when
        # it is not.
        _deploy(fakebin, tmp_path)
        self._seed_snapshot(tmp_path, with_code=True)
        fakebin.stub("rsync")
        result = run_script("update.sh", fakebin, cwd=tmp_path, args=["--rollback", "--yes"])
        assert result.returncode == 0, result.stderr
        builds = [c for c in fakebin.calls() if c.rstrip().endswith(" build")]
        assert builds and all("--profile vendor" in c for c in builds), builds


class TestBorgPause:
    """borg dumps the database on its own clock, inside the container, and a dump
    in progress holds share locks on every table. A migration's ALTER TABLE queues
    behind it; the rollback's schema drop waits on it for good. The container is
    stopped for the span of either operation and started again only if it was
    running before.
    """

    def test_an_update_stops_borg_before_migrating_and_restarts_it_after(self, fakebin, tmp_path):
        _deploy(fakebin, tmp_path)
        result = run_script("update.sh", fakebin, cwd=tmp_path, args=["--from", "repo", "--no-pull"])
        assert result.returncode == 0, result.stderr
        calls = fakebin.calls()
        stop_i = _index_of(calls, "stop web celery celery-beat borg")
        migrate_i = _index_of(calls, "manage.py migrate")
        recreate_i = _index_of(calls, "--force-recreate web celery")
        start_i = _index_of(calls, "up -d borg")
        assert -1 < stop_i < migrate_i < recreate_i < start_i, (
            f"stop={stop_i} migrate={migrate_i} recreate={recreate_i} start={start_i}"
        )

    def test_a_rollback_stops_borg_before_the_restore(self, fakebin, tmp_path):
        _deploy(fakebin, tmp_path)
        TestRollback()._seed_snapshot(tmp_path)
        result = run_script("update.sh", fakebin, cwd=tmp_path, args=["--rollback", "--yes"])
        assert result.returncode == 0, result.stderr
        calls = fakebin.calls()
        stop_i = _index_of(calls, "stop web celery celery-beat borg")
        restore_i = _index_of(calls, "psql --single-transaction")
        start_i = _index_of(calls, "up -d borg")
        assert -1 < stop_i < restore_i < start_i, f"stop={stop_i} restore={restore_i} start={start_i}"

    def test_a_run_that_stopped_borg_and_died_is_repaired_by_the_next(self, fakebin, tmp_path):
        """Observed on a deployment: a rollback stopped borg, died at a later step,
        and the next run found borg stopped — indistinguishable from a deployment
        that never ran it — so backups stayed off until someone noticed. The
        stop leaves a marker that the next run honours and the restart clears.
        """
        _deploy(fakebin, tmp_path)
        fakebin.stub("docker", body=_docker_stub('*"manage.py migrate"*) exit 1 ;;'))
        result = run_script("update.sh", fakebin, cwd=tmp_path, args=["--from", "repo", "--no-pull"])
        assert result.returncode != 0
        marker = tmp_path / "update" / ".borg-was-running"
        assert marker.is_file(), "the stop must record that borg was running"

        fakebin.log.write_text("")
        fakebin.stub("docker", body=_docker_stub('*" ps borg") ;;'))
        result = run_script("update.sh", fakebin, cwd=tmp_path, args=["--from", "repo", "--no-pull"])
        assert result.returncode == 0, result.stderr
        assert fakebin.has_call("up -d borg"), "borg was left down after the repair run"
        assert not marker.exists(), "a successful restart must clear the marker"

    def test_the_marker_is_kept_when_borg_fails_to_start(self, fakebin, tmp_path):
        _deploy(fakebin, tmp_path)
        fakebin.stub("docker", body=_docker_stub('*"up -d borg"*) exit 1 ;;'))
        result = run_script("update.sh", fakebin, cwd=tmp_path, args=["--from", "repo", "--no-pull"])
        assert result.returncode == 0, result.stderr
        assert "borg did not start" in result.stdout
        assert (tmp_path / "update" / ".borg-was-running").is_file()

    def test_borg_is_not_started_on_a_deployment_that_did_not_run_it(self, fakebin, tmp_path):
        # A deployment with backups turned off has the service defined and never
        # started; an update must not be what starts it.
        _deploy(fakebin, tmp_path)
        fakebin.stub("docker", body=_docker_stub('*" ps borg") ;;'))
        result = run_script("update.sh", fakebin, cwd=tmp_path, args=["--from", "repo", "--no-pull"])
        assert result.returncode == 0, result.stderr
        assert fakebin.has_call("stop web celery celery-beat borg")
        assert not fakebin.has_call("up -d borg")


class TestSkipBeat:
    """The database scheduler fires every periodic task that came due while beat
    was down the moment it starts, so a purge that fell due during the build
    unlinks files seconds after the recreate — files a rollback restores the rows
    for and cannot bring back. --skip-beat leaves beat stopped for whoever is
    verifying the update to start.
    """

    def test_beat_is_recreated_by_default(self, fakebin, tmp_path):
        _deploy(fakebin, tmp_path)
        run_script("update.sh", fakebin, cwd=tmp_path, args=["--from", "repo", "--no-pull"])
        assert fakebin.has_call("--force-recreate web celery celery-beat")

    def test_skip_beat_leaves_it_stopped_and_says_so(self, fakebin, tmp_path):
        _deploy(fakebin, tmp_path)
        result = run_script("update.sh", fakebin, cwd=tmp_path, args=["--from", "repo", "--no-pull", "--skip-beat"])
        assert result.returncode == 0, result.stderr
        assert fakebin.has_call("stop web celery celery-beat"), "beat must still be stopped with the rest"
        started = [c for c in fakebin.calls() if " up " in c and "celery-beat" in c]
        assert not started, f"--skip-beat must not start celery-beat: {started}"
        assert fakebin.has_call("--force-recreate web celery")
        assert "up -d celery-beat" in result.stdout, "the operator is not told how to start beat"

    def test_skip_beat_applies_to_a_rollback(self, fakebin, tmp_path):
        _deploy(fakebin, tmp_path)
        TestRollback()._seed_snapshot(tmp_path)
        result = run_script("update.sh", fakebin, cwd=tmp_path, args=["--rollback", "--yes", "--skip-beat"])
        assert result.returncode == 0, result.stderr
        started = [c for c in fakebin.calls() if " up " in c and "celery-beat" in c]
        assert not started, started


class TestSnapshotOwnership:
    @requires_gnu_stat
    def test_a_snapshot_taken_by_root_is_handed_to_the_tree_owner(self, fakebin, tmp_path):
        # Observed on a deployment: a root-run update left backups/pre-update-*
        # owned by root, which the deploy account can neither prune nor restore.
        _deploy(fakebin, tmp_path)
        fakebin.stub("id", body="echo 0")
        result = run_script("update.sh", fakebin, cwd=tmp_path, args=["--from", "repo", "--no-pull"])
        assert result.returncode == 0, result.stderr
        calls = fakebin.calls()
        chowns = [i for i, c in enumerate(calls) if "chown -R" in c and "pre-update-" in c]
        assert chowns, calls
        # Once after the code archive and again after the dump: the dump lands
        # minutes later, and a single chown after the archive left it root-owned.
        dump_i = _index_of(calls, "pg_dump")
        assert dump_i > -1 and chowns[-1] > dump_i, f"no chown after the dump: chowns={chowns} dump={dump_i}"

    @requires_gnu_stat
    def test_a_named_snapshot_taken_by_root_is_handed_over_whole(self, fakebin, tmp_path):
        _deploy(fakebin, tmp_path)
        fakebin.stub("id", body="echo 0")
        result = run_script("update.sh", fakebin, cwd=tmp_path, args=["--snapshot", "post-update"])
        assert result.returncode == 0, result.stderr
        calls = fakebin.calls()
        assert _index_of(calls, "pg_dump") < max(i for i, c in enumerate(calls) if "chown -R" in c and "post-update-" in c)

    def test_an_unprivileged_run_does_not_chown(self, fakebin, tmp_path):
        _deploy(fakebin, tmp_path)
        result = run_script("update.sh", fakebin, cwd=tmp_path, args=["--from", "repo", "--no-pull"])
        assert result.returncode == 0, result.stderr
        assert not any("chown" in c and "pre-update-" in c for c in fakebin.calls())

    @requires_gnu_stat
    def test_the_installed_file_list_written_by_root_is_handed_to_the_tree_owner(self, fakebin, tmp_path):
        # Observed on a deployment: a root-run update left .epicurrents-files
        # owned by root, and a later run as the deployment account would die on
        # replacing it — after the overlay, the worst place to stop.
        _deploy(fakebin, tmp_path)
        _build_package(tmp_path)
        fakebin.stub("rsync")
        fakebin.stub("id", body="echo 0")
        result = run_script("update.sh", fakebin, cwd=tmp_path)
        assert result.returncode == 0, result.stderr
        assert any("chown" in c and ".epicurrents-files" in c for c in fakebin.calls()), fakebin.calls()


class TestRootFlag:
    """The script derives its root from its own location, which is wrong for a
    copy run from outside the deployment: it resolves to the copy's parent and
    snapshots, syncs and locks the wrong tree. --root names the deployment.
    """

    def _deployment(self, tmp_path):
        deploy = tmp_path / "deploy"
        deploy.mkdir()
        deploy.chmod(0o777)
        make_env(deploy)
        (deploy / "docker-compose.yml").write_text("services: {}\n")
        return deploy

    def test_root_points_every_step_at_the_named_deployment(self, fakebin, tmp_path):
        deploy = self._deployment(tmp_path)
        agent = tmp_path / "agent"
        agent.mkdir()
        fakebin.stub("docker", body=DOCKER_PS_RUNNING)
        fakebin.stub("cp", body=REAL_CP)
        result = run_script(
            "update.sh", fakebin, cwd=agent,
            args=["--root", str(deploy), "--from", "repo", "--no-pull", "--keep-lock"],
        )
        assert result.returncode == 0, result.stderr
        snapshots = list((deploy / "backups").glob("pre-update-*"))
        assert len(snapshots) == 1 and (snapshots[0] / "db.sql.gz").is_file(), snapshots
        assert (deploy / "update" / "maintenance.json").is_file()
        assert not (agent / "backups").exists(), "the snapshot landed beside the script instead of in the deployment"
        assert not (agent / "update").exists()

    def test_root_without_a_compose_file_is_refused(self, fakebin, tmp_path):
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        make_env(elsewhere)
        result = run_script("update.sh", fakebin, cwd=tmp_path, args=["--root", str(elsewhere)])
        assert result.returncode != 0
        assert "does not look like a deployment" in result.stderr

    def test_root_that_does_not_exist_is_refused(self, fakebin, tmp_path):
        result = run_script("update.sh", fakebin, cwd=tmp_path, args=["--root", str(tmp_path / "nope")])
        assert result.returncode != 0
        assert "no such directory" in result.stderr


class TestMaintenanceFlag:
    """update/maintenance.json is up for the whole run, so the platform can
    decline writes that a rollback would lose — the dump is taken in step 2 and
    the services keep running through the image build. A file rather than a row,
    because the rollback restores the database and would erase a row mid-way.
    """

    def test_the_flag_is_present_during_the_run_and_gone_after(self, fakebin, tmp_path):
        _deploy(fakebin, tmp_path)
        seen = tmp_path / "flag-during-migrate.json"
        # The stub runs with the deployment as its working directory, so it can
        # copy the flag out while the script is between steps.
        fakebin.stub("docker", body=_docker_stub(f'*"manage.py migrate"*) /bin/cp update/maintenance.json "{seen}" ;;'))
        result = run_script("update.sh", fakebin, cwd=tmp_path, args=["--from", "repo", "--no-pull"])
        assert result.returncode == 0, result.stderr
        flag = json.loads(seen.read_text())
        assert flag["phase"] == "updating"
        assert flag["protocol"] == 1
        assert flag["job_id"] is None and flag["expected_until"] is None
        assert flag["since"].endswith("Z") and "T" in flag["since"]
        assert flag["message"]
        assert not (tmp_path / "update" / "maintenance.json").exists(), "the flag outlived a successful run"

    def test_the_flag_goes_up_before_the_code_snapshot(self, fakebin, tmp_path):
        # Step 0 is the first thing that touches the tree; the flag precedes it.
        body = (SCRIPTS_DIR / "update.sh").read_text()
        assert body.index("write_maintenance_flag updating") < body.index('snap="$BACKUP_DIR/pre-update-$stamp"')

    def test_a_rollback_announces_itself_as_rolling_back(self, fakebin, tmp_path):
        _deploy(fakebin, tmp_path)
        TestRollback()._seed_snapshot(tmp_path)
        result = run_script("update.sh", fakebin, cwd=tmp_path, args=["--rollback", "--yes", "--keep-lock"])
        assert result.returncode == 0, result.stderr
        assert _read_flag(tmp_path)["phase"] == "rolling_back"

    def test_keep_lock_leaves_the_flag_after_success(self, fakebin, tmp_path):
        _deploy(fakebin, tmp_path)
        result = run_script("update.sh", fakebin, cwd=tmp_path, args=["--from", "repo", "--no-pull", "--keep-lock"])
        assert result.returncode == 0, result.stderr
        assert _read_flag(tmp_path)["phase"] == "updating"

    def test_keep_lock_never_overwrites_a_flag_the_caller_wrote(self, fakebin, tmp_path):
        # A caller that manages the flag carries its own fields in it (a job id,
        # an expected end); the script's own flag would erase them.
        _deploy(fakebin, tmp_path)
        (tmp_path / "update").mkdir()
        theirs = '{"protocol": 1, "phase": "updating", "job_id": "abc", "since": "x", "expected_until": null, "message": "m"}\n'
        (tmp_path / "update" / "maintenance.json").write_text(theirs)
        result = run_script("update.sh", fakebin, cwd=tmp_path, args=["--from", "repo", "--no-pull", "--keep-lock"])
        assert result.returncode == 0, result.stderr
        assert (tmp_path / "update" / "maintenance.json").read_text() == theirs

    def test_a_stale_flag_is_replaced_without_keep_lock(self, fakebin, tmp_path):
        _deploy(fakebin, tmp_path)
        (tmp_path / "update").mkdir()
        (tmp_path / "update" / "maintenance.json").write_text('{"phase": "rolling_back", "job_id": "old"}\n')
        seen = tmp_path / "flag-during-migrate.json"
        fakebin.stub("docker", body=_docker_stub(f'*"manage.py migrate"*) /bin/cp update/maintenance.json "{seen}" ;;'))
        result = run_script("update.sh", fakebin, cwd=tmp_path, args=["--from", "repo", "--no-pull"])
        assert result.returncode == 0, result.stderr
        assert json.loads(seen.read_text())["phase"] == "updating"

    def test_a_failure_before_the_stack_is_stopped_removes_the_flag(self, fakebin, tmp_path):
        # Nothing changed and the platform is still serving, so the flag would
        # only lock everyone out of a working deployment.
        _deploy(fakebin, tmp_path)
        fakebin.stub("docker", body=_docker_stub('*"--profile vendor build"*) exit 1 ;;'))
        result = run_script("update.sh", fakebin, cwd=tmp_path, args=["--from", "repo", "--no-pull"])
        assert result.returncode != 0
        assert not fakebin.has_call("stop web celery")
        assert not (tmp_path / "update" / "maintenance.json").exists()

    def test_a_failure_after_the_stack_is_stopped_leaves_the_flag_and_says_so(self, fakebin, tmp_path):
        # The stack is down and not coming back on its own; the flag is what
        # tells anyone who reaches the platform why.
        _deploy(fakebin, tmp_path)
        fakebin.stub("docker", body=_docker_stub('*"manage.py migrate"*) exit 1 ;;'))
        result = run_script("update.sh", fakebin, cwd=tmp_path, args=["--from", "repo", "--no-pull"])
        assert result.returncode != 0
        assert fakebin.has_call("stop web celery")
        assert _read_flag(tmp_path)["phase"] == "updating"
        assert "Leaving the maintenance flag in place" in result.stdout

    def test_a_failed_restore_leaves_the_flag(self, fakebin, tmp_path):
        _deploy(fakebin, tmp_path)
        TestRollback()._seed_snapshot(tmp_path)
        fakebin.stub("docker", body=_docker_stub('*"psql --single-transaction"*) cat >/dev/null; exit 1 ;;'))
        result = run_script("update.sh", fakebin, cwd=tmp_path, args=["--rollback", "--yes"])
        assert result.returncode != 0
        assert "restore FAILED" in result.stderr
        assert _read_flag(tmp_path)["phase"] == "rolling_back"

    def test_the_flag_directory_survives_both_directions(self):
        # The update overlay and the rollback replace both exclude update/, and
        # the code snapshot leaves it out, so neither direction can touch the flag.
        body = (SCRIPTS_DIR / "update.sh").read_text()
        assert body.count("--exclude='/update/'") == 1, "archive overlay no longer excludes update/"
        assert body.count('--exclude="update/"') == 1, "rollback replace no longer excludes update/"
        assert body.count('--exclude="./update"') == 1, "code snapshot no longer excludes update/"


class TestUpdateShProxyOverlay:
    """update.sh must select the same compose overlays bootstrap.sh brought the stack up with.

    If it does not, `up -d` sees the running caddy container as an orphan and the
    deployment loses its TLS terminator partway through an update.
    """

    def test_overlay_selected_when_proxy_domain_is_set(self, fakebin, tmp_path):
        _deploy(fakebin, tmp_path, PROXY_DOMAIN="eeg.example.com")
        result = run_script("update.sh", fakebin, cwd=tmp_path, args=["--from", "repo", "--no-pull", "--no-backup"])
        assert result.returncode == 0, result.stderr
        assert fakebin.has_call("docker-compose.proxy.yml")

    def test_overlay_omitted_when_proxy_domain_is_empty(self, fakebin, tmp_path):
        _deploy(fakebin, tmp_path, PROXY_DOMAIN="")
        result = run_script("update.sh", fakebin, cwd=tmp_path, args=["--from", "repo", "--no-pull", "--no-backup"])
        assert result.returncode == 0, result.stderr
        assert not fakebin.has_call("docker-compose.proxy.yml")


class TestProxyAssetContinuity:
    """An archive update must not take the SPA offline while Django looks healthy.

    The bundles are served off bind mounts by caddy, and a running container
    holds the mount it started with. Replacing frontend/dist wholesale gives the
    new tree a new inode, leaves caddy pointed at the old one, and every asset
    404s — invisible to anyone with a warm cache, because the hashed bundles are
    served `immutable`. It shipped that way and the outage went a day unnoticed.
    """

    def test_dist_directories_are_emptied_not_replaced(self, fakebin, tmp_path):
        # The inode has to survive, or a caddy that is not restarted serves 404s.
        body = (SCRIPTS_DIR / "update.sh").read_text()
        assert "-mindepth 1 -delete" in body
        replace = body.index("for d in frontend/dist frontend/viewer-dist; do")
        window = body[replace : replace + 700]
        assert "find" in window, "the dist directories are still being removed wholesale"

    def test_caddy_is_recreated_when_the_proxy_is_in_use(self, fakebin, tmp_path):
        _deploy(fakebin, tmp_path, PROXY_DOMAIN="example.test")
        result = run_script("update.sh", fakebin, cwd=tmp_path, args=["--from", "repo", "--no-pull"])
        assert result.returncode == 0, result.stderr
        assert fakebin.has_call("--force-recreate caddy")

    def test_caddy_is_not_touched_without_the_proxy_overlay(self, fakebin, tmp_path):
        # A deployment terminating TLS elsewhere has no caddy service, and
        # naming one would fail the step on an otherwise healthy update.
        _deploy(fakebin, tmp_path, PROXY_DOMAIN="")
        result = run_script("update.sh", fakebin, cwd=tmp_path, args=["--from", "repo", "--no-pull"])
        assert result.returncode == 0, result.stderr
        assert not fakebin.has_call("caddy")

    def test_the_database_is_still_never_recreated(self, fakebin, tmp_path):
        # Adding caddy to the recreate set widened it for the first time; the
        # property that actually matters is that db and redis stay untouched.
        _deploy(fakebin, tmp_path, PROXY_DOMAIN="example.test")
        run_script("update.sh", fakebin, cwd=tmp_path, args=["--from", "repo", "--no-pull"])
        for call in [c for c in fakebin.calls() if "--force-recreate" in c]:
            assert " db" not in call and " redis" not in call, call

    def test_an_asset_is_verified_not_just_the_health_endpoint(self, fakebin, tmp_path):
        # /api/v1/health returns 200 from a deployment whose SPA cannot load at
        # all, so passing it is not evidence the update worked.
        body = (SCRIPTS_DIR / "update.sh").read_text()
        assert "/assets/" in body
        health_at = body.index("api/v1/health")
        asset_at = body.index("Verifying the SPA bundle is servable")
        assert health_at < asset_at, "the asset check must follow the health check, not replace it"


class TestUpdateShViewerEdition:
    """The pinned viewer edition is checked every update and fetched only on drift."""

    def test_the_edition_is_checked_after_the_image_is_rebuilt(self, fakebin, tmp_path):
        # Production gives `vendor` no /code bind, so it reads the pin baked into the
        # image. Checking before the rebuild would install the edition the *previous*
        # pin named and never revisit it, which a pull that moves the pin makes routine.
        _deploy(fakebin, tmp_path)
        result = run_script("update.sh", fakebin, cwd=tmp_path, args=["--from", "repo", "--no-pull"])
        assert result.returncode == 0, result.stderr
        calls = fakebin.calls()
        check = _index_of(calls, "vendor_viewer --check")
        build = _index_of(calls, "--profile vendor build")
        assert check > -1, "the viewer pin must be checked on every update"
        assert -1 < build < check, f"expected the image build at {build} before the check at {check}"

    def test_the_edition_is_only_fetched_when_the_check_fails(self, fakebin, tmp_path):
        # The check is a local stamp comparison and the fetch is a multi-megabyte
        # download, so an update that finds a matching tree must not re-fetch it.
        _deploy(fakebin, tmp_path)
        run_script("update.sh", fakebin, cwd=tmp_path, args=["--from", "repo", "--no-pull"])
        fetches = [call for call in fakebin.calls() if "vendor_viewer" in call and "--check" not in call]
        assert not fetches, f"expected no re-fetch when --check passes, got {fetches}"

    def test_the_edition_is_installed_unprivileged(self, fakebin, tmp_path):
        # viewer-dist is inside the code snapshot a rollback restores with rsync as the
        # deploy user, so root-owned files here would leave a tree it cannot overwrite.
        _deploy(fakebin, tmp_path)
        run_script("update.sh", fakebin, cwd=tmp_path, args=["--from", "repo", "--no-pull"])
        call = next(c for c in fakebin.calls() if "vendor_viewer" in c)
        assert "--user 1000:1000" in call, call


class TestVendoringIsNotFatal:
    """The vendoring steps run after the services are stopped. A failure that
    aborted there left the stack down over an asset tree — observed on a
    deployment whose project names no Pyodide asset path — so each step now
    reports and the run goes on to recreate the stack and check its health.
    """

    def test_a_pyodide_failure_still_recreates_the_stack(self, fakebin, tmp_path):
        _deploy(fakebin, tmp_path)
        fakebin.stub("docker", body=_docker_stub('*"vendor_pyodide"*) exit 1 ;;'))
        fakebin.stub("curl")
        result = run_script("update.sh", fakebin, cwd=tmp_path, args=["--from", "repo", "--no-pull"])
        assert result.returncode == 0, result.stderr
        assert fakebin.has_call("--force-recreate web celery")
        assert fakebin.has_call("/api/v1/health")
        assert "Pyodide runtime was not vendored" in result.stdout
        assert "vendoring step(s) failed" in result.stdout, "the summary must repeat the failure"
        assert not (tmp_path / "update" / "maintenance.json").exists()

    def test_every_vendoring_step_is_attempted_after_one_fails(self, fakebin, tmp_path):
        _deploy(fakebin, tmp_path)
        fakebin.stub("docker", body=_docker_stub('*"vendor_viewer"*) exit 1 ;;'))
        result = run_script("update.sh", fakebin, cwd=tmp_path, args=["--from", "repo", "--no-pull"])
        assert result.returncode == 0, result.stderr
        assert fakebin.has_call("vendor_pyodide --check")
        assert fakebin.has_call("generate_compute_static")
        assert "viewer edition was not installed" in result.stdout

    def test_a_rollback_survives_a_vendoring_failure(self, fakebin, tmp_path):
        _deploy(fakebin, tmp_path)
        TestRollback()._seed_snapshot(tmp_path, with_code=True)
        fakebin.stub("rsync")
        fakebin.stub("docker", body=_docker_stub('*"generate_compute_static"*) exit 1 ;;'))
        result = run_script("update.sh", fakebin, cwd=tmp_path, args=["--rollback", "--yes"])
        assert result.returncode == 0, result.stderr
        assert fakebin.has_call("--force-recreate web celery")
        assert "Rollback complete" in result.stdout
        assert "lead fields were not generated" in result.stdout


def _real_openssl3():
    """Path of an OpenSSL 3 binary on this machine, or None.

    The harness PATH ends in the system directories, and macOS ships LibreSSL
    there, so the one test that exercises real Ed25519 verification looks for a
    Homebrew or distribution OpenSSL 3 explicitly.
    """
    candidates = ["/opt/homebrew/bin/openssl", "/usr/local/bin/openssl", shutil.which("openssl"), "/usr/bin/openssl"]
    for candidate in candidates:
        if not candidate or not os.access(candidate, os.X_OK):
            continue
        version = subprocess.run([candidate, "version"], capture_output=True, text=True, check=False).stdout
        if version.startswith("OpenSSL 3"):
            return candidate
    return None


class TestArchiveVerification:
    """A package is checked before a byte of it is extracted, and a refused one
    leaves no trace: no snapshot, no flag, no overlay. The signature binds the
    manifest, the manifest binds the tarball by hash, and the manifest says
    what the package is — version, project, plugins — so a package for the
    wrong deployment is refused before the snapshot rather than discovered
    from a broken stack.
    """

    def _signed(self, fakebin, tmp_path, **kwargs):
        _deploy(fakebin, tmp_path, **{k: v for k, v in kwargs.items() if k in ("EPICURRENTS_PROJECT", "EPICURRENTS_PLUGINS")})
        key, pub = _sign_key(tmp_path)
        shutil.copy(pub, tmp_path / "RELEASE_KEY.pub")
        build = {k: v for k, v in kwargs.items() if k not in ("EPICURRENTS_PROJECT", "EPICURRENTS_PLUGINS")}
        archive = _build_package(tmp_path, sign_key=key, **build)
        fakebin.stub("rsync")
        fakebin.stub("openssl", body=OPENSSL_VERIFIES)
        return archive, key

    def test_a_signed_package_verifies_and_applies(self, fakebin, tmp_path):
        self._signed(fakebin, tmp_path)
        result = run_script("update.sh", fakebin, cwd=tmp_path, args=["--require-signature", "--require-newer"])
        assert result.returncode == 0, result.stderr
        assert fakebin.has_call("pkeyutl -verify")
        assert "signature=verified" in _progress(result)
        assert "version=0.2.0" in _progress(result) and "installed=0.1.0" in _progress(result)
        assert fakebin.has_call("rsync")

    def test_the_signature_is_checked_before_anything_is_extracted(self, fakebin, tmp_path):
        self._signed(fakebin, tmp_path)
        calls = []
        run_script("update.sh", fakebin, cwd=tmp_path)
        calls = fakebin.calls()
        verify_i = _index_of(calls, "pkeyutl -verify")
        extract_i = next((i for i, c in enumerate(calls) if c.startswith("tar") and " -x" in c and " -C " in c), -1)
        assert -1 < verify_i < extract_i, f"verify={verify_i} extract={extract_i}"

    def test_a_refusal_names_its_reason_before_the_failure_line(self, fakebin, tmp_path):
        # The host agent keys on the token; the free text after ::failed= is for people.
        archive, _ = self._signed(fakebin, tmp_path)
        fakebin.stub("openssl", body=OPENSSL_REJECTS)
        result = run_script("update.sh", fakebin, cwd=tmp_path, args=["--check-archive", str(archive)])
        assert result.returncode != 0
        lines = _progress(result)
        assert "refused=signature" in lines
        assert lines.index("refused=signature") < next(i for i, l in enumerate(lines) if l.startswith("failed="))
        assert not _extracted_to_disk(fakebin)

    def test_a_package_that_is_not_newer_is_refused_by_name(self, fakebin, tmp_path):
        _deploy(fakebin, tmp_path, installed_version="0.2.0")
        archive = _build_package(tmp_path)
        result = run_script("update.sh", fakebin, cwd=tmp_path, args=["--check-archive", str(archive), "--require-newer"])
        assert result.returncode != 0
        assert "refused=version_not_newer" in _progress(result)

    def test_a_bad_signature_is_refused_and_nothing_is_touched(self, fakebin, tmp_path):
        self._signed(fakebin, tmp_path)
        fakebin.stub("openssl", body=OPENSSL_REJECTS)
        result = run_script("update.sh", fakebin, cwd=tmp_path)
        assert result.returncode != 0
        assert "does NOT verify" in result.stderr
        assert any(line.startswith("failed=") for line in _progress(result))
        _nothing_touched(fakebin, tmp_path)

    def test_a_real_ed25519_signature_verifies_through_openssl(self, fakebin, tmp_path):
        # The stubs above answer for openssl; this one lets the real binary check
        # a signature the Python helper made, which is the pairing a deployment
        # host runs: cryptography signs on the packaging machine, OpenSSL 3
        # verifies on the host.
        openssl = _real_openssl3()
        if openssl is None:
            pytest.skip("no OpenSSL 3 on this machine")
        archive, _ = self._signed(fakebin, tmp_path)
        fakebin.stub("openssl", body=f'exec {openssl} "$@"')
        result = run_script("update.sh", fakebin, cwd=tmp_path, args=["--check-archive", str(archive)])
        assert result.returncode == 0, result.stderr
        assert "signature=verified" in _progress(result)
        # And a flipped byte in the manifest is what a bad signature looks like.
        manifest = archive.with_name(archive.name + ".manifest.json")
        manifest.write_text(manifest.read_text().replace('"version": "0.2.0"', '"version": "0.2.1"'))
        result = run_script("update.sh", fakebin, cwd=tmp_path, args=["--check-archive", str(archive)])
        assert result.returncode != 0
        assert "does NOT verify" in result.stderr

    def test_a_tampered_archive_fails_the_hash_check(self, fakebin, tmp_path):
        archive, _ = self._signed(fakebin, tmp_path)
        with archive.open("ab") as fh:
            fh.write(b"\0")
        result = run_script("update.sh", fakebin, cwd=tmp_path)
        assert result.returncode != 0
        assert "does not match its manifest" in result.stderr
        _nothing_touched(fakebin, tmp_path)

    def test_an_unsigned_package_is_refused_when_a_signature_is_required(self, fakebin, tmp_path):
        _deploy(fakebin, tmp_path)
        _build_package(tmp_path)
        fakebin.stub("rsync")
        result = run_script("update.sh", fakebin, cwd=tmp_path, args=["--require-signature"])
        assert result.returncode != 0
        assert "not signed" in result.stderr
        _nothing_touched(fakebin, tmp_path)

    def test_an_unsigned_package_applies_with_a_warning_on_the_manual_path(self, fakebin, tmp_path):
        _deploy(fakebin, tmp_path)
        _build_package(tmp_path)
        fakebin.stub("rsync")
        result = run_script("update.sh", fakebin, cwd=tmp_path)
        assert result.returncode == 0, result.stderr
        assert "not signed" in result.stdout
        assert "signature=unsigned" in _progress(result)

    def test_a_package_with_no_manifest_still_applies_but_cannot_be_required(self, fakebin, tmp_path):
        _deploy(fakebin, tmp_path)
        _build_package(tmp_path, manifest=False)
        fakebin.stub("rsync")
        result = run_script("update.sh", fakebin, cwd=tmp_path)
        assert result.returncode == 0, result.stderr
        assert "No manifest" in result.stdout
        assert "manifest=none" in _progress(result)
        # The version still comes out of the tarball itself.
        assert "version=0.2.0" in _progress(result)
        result = run_script("update.sh", fakebin, cwd=tmp_path, args=["--require-signature"])
        assert result.returncode != 0
        assert "No manifest" in result.stderr

    def test_a_signed_package_without_a_key_on_the_host_warns_or_refuses(self, fakebin, tmp_path):
        self._signed(fakebin, tmp_path)
        (tmp_path / "RELEASE_KEY.pub").unlink()
        result = run_script("update.sh", fakebin, cwd=tmp_path)
        assert result.returncode == 0, result.stderr
        assert "no release key" in result.stdout
        assert "signature=unverifiable" in _progress(result)
        result = run_script("update.sh", fakebin, cwd=tmp_path, args=["--require-signature"])
        assert result.returncode != 0
        assert "no release key" in result.stderr

    def test_release_key_names_the_key_to_verify_against(self, fakebin, tmp_path):
        _, key = self._signed(fakebin, tmp_path)
        (tmp_path / "RELEASE_KEY.pub").unlink()
        elsewhere = tmp_path / "keys" / "release.key.pub"
        result = run_script(
            "update.sh", fakebin, cwd=tmp_path, args=["--require-signature", "--release-key", str(elsewhere)]
        )
        assert result.returncode == 0, result.stderr
        assert fakebin.has_call(f"-inkey {elsewhere}")

    def test_a_host_that_cannot_verify_says_so(self, fakebin, tmp_path):
        # LibreSSL cannot do Ed25519 and neither can a python3 without
        # cryptography: the signature is reported as unverifiable, which
        # --require-signature refuses.
        self._signed(fakebin, tmp_path)
        fakebin.stub("openssl", body=OPENSSL_TOO_OLD)
        fakebin.stub("python3", exit_code=1)
        result = run_script("update.sh", fakebin, cwd=tmp_path)
        assert result.returncode == 0, result.stderr
        assert "nothing on this host can check" in result.stdout
        assert "signature=unverifiable" in _progress(result)
        result = run_script("update.sh", fakebin, cwd=tmp_path, args=["--require-signature"])
        assert result.returncode != 0
        assert "nothing on this host can check" in result.stderr

    def test_a_package_for_another_project_is_refused(self, fakebin, tmp_path):
        self._signed(fakebin, tmp_path, EPICURRENTS_PROJECT="edu", project="research")
        result = run_script("update.sh", fakebin, cwd=tmp_path)
        assert result.returncode != 0
        assert "built for project 'research'" in result.stderr and "runs 'edu'" in result.stderr
        _nothing_touched(fakebin, tmp_path)

    def test_a_base_package_over_a_project_deployment_is_refused(self, fakebin, tmp_path):
        self._signed(fakebin, tmp_path, EPICURRENTS_PROJECT="edu", project="")
        result = run_script("update.sh", fakebin, cwd=tmp_path)
        assert result.returncode != 0
        assert "built for project '<none>'" in result.stderr

    def test_a_different_plugin_set_is_refused_and_order_does_not_matter(self, fakebin, tmp_path):
        self._signed(fakebin, tmp_path, EPICURRENTS_PLUGINS="dicom,other", plugins="other,dicom")
        result = run_script("update.sh", fakebin, cwd=tmp_path)
        assert result.returncode == 0, result.stderr
        shutil.rmtree(tmp_path / "pkg-tree")
        _write_manifest(tmp_path / "update" / "epicurrents-test.tar.gz", version="0.2.0", plugins="dicom",
                        sign_key=tmp_path / "keys" / "release.key")
        result = run_script("update.sh", fakebin, cwd=tmp_path)
        assert result.returncode != 0
        assert "carries plugins 'dicom'" in result.stderr

    def test_a_package_needing_a_newer_updater_is_refused(self, fakebin, tmp_path):
        self._signed(fakebin, tmp_path, min_updater_version=99)
        result = run_script("update.sh", fakebin, cwd=tmp_path)
        assert result.returncode != 0
        assert "needs update.sh version 99" in result.stderr
        _nothing_touched(fakebin, tmp_path)

    def test_require_newer_refuses_the_same_version_and_a_downgrade(self, fakebin, tmp_path):
        self._signed(fakebin, tmp_path, version="0.1.0")
        result = run_script("update.sh", fakebin, cwd=tmp_path, args=["--require-newer"])
        assert result.returncode != 0
        assert "the same as the installed release" in result.stderr
        _nothing_touched(fakebin, tmp_path)
        shutil.rmtree(tmp_path / "pkg-tree")
        _write_manifest(tmp_path / "update" / "epicurrents-test.tar.gz", version="0.0.9",
                        sign_key=tmp_path / "keys" / "release.key")
        result = run_script("update.sh", fakebin, cwd=tmp_path, args=["--require-newer"])
        assert result.returncode != 0
        assert "OLDER" in result.stderr

    def test_without_require_newer_the_same_version_is_a_warning(self, fakebin, tmp_path):
        self._signed(fakebin, tmp_path, version="0.1.0")
        result = run_script("update.sh", fakebin, cwd=tmp_path)
        assert result.returncode == 0, result.stderr
        assert "the same as the installed release" in result.stdout

    def test_a_ten_sorts_after_a_nine(self, fakebin, tmp_path):
        # Numeric per component, not lexical: 0.1.10 is newer than 0.1.9.
        _deploy(fakebin, tmp_path, installed_version="0.1.9")
        _build_package(tmp_path, version="0.1.10")
        fakebin.stub("rsync")
        result = run_script("update.sh", fakebin, cwd=tmp_path, args=["--require-newer"])
        assert result.returncode == 0, result.stderr


class TestArchiveContents:
    """The member list is read before extraction and anything an overlay must
    not receive is refused: paths that escape the tree, links, a second
    top-level entry, and the files that belong to the deployment.
    """

    def test_a_symlink_member_is_refused(self, fakebin, tmp_path):
        _deploy(fakebin, tmp_path)
        tree = tmp_path / "pkg-tree" / "epicurrents-test"
        tree.mkdir(parents=True)
        (tree / "docker-compose.yml").write_text("services: {}\n")
        os.symlink("/etc/passwd", tree / "link")
        archive = tmp_path / "update" / "epicurrents-test.tar.gz"
        archive.parent.mkdir()
        subprocess.run(["/usr/bin/tar", "-czf", str(archive), "-C", str(tree.parent), tree.name], check=True)
        fakebin.stub("rsync")
        result = run_script("update.sh", fakebin, cwd=tmp_path)
        assert result.returncode != 0
        assert "symbolic or hard links" in result.stderr
        _nothing_touched(fakebin, tmp_path)

    def test_a_parent_reference_is_refused(self, fakebin, tmp_path):
        _deploy(fakebin, tmp_path)
        # GNU tar strips a leading ../ on its own; a hand-crafted archive does
        # not, so the member is written with Python's tarfile.
        import tarfile

        archive = tmp_path / "update" / "epicurrents-test.tar.gz"
        archive.parent.mkdir()
        with tarfile.open(archive, "w:gz") as tf:
            info = tarfile.TarInfo("epicurrents-test/docker-compose.yml")
            info.size = 0
            tf.addfile(info)
            info = tarfile.TarInfo("epicurrents-test/../escape")
            info.size = 0
            tf.addfile(info)
        fakebin.stub("rsync")
        result = run_script("update.sh", fakebin, cwd=tmp_path)
        assert result.returncode != 0
        assert "'..' component" in result.stderr
        _nothing_touched(fakebin, tmp_path)

    def test_two_top_level_entries_are_refused(self, fakebin, tmp_path):
        _deploy(fakebin, tmp_path)
        import tarfile

        archive = tmp_path / "update" / "epicurrents-test.tar.gz"
        archive.parent.mkdir()
        with tarfile.open(archive, "w:gz") as tf:
            for name in ("epicurrents-test/docker-compose.yml", "other/thing"):
                info = tarfile.TarInfo(name)
                info.size = 0
                tf.addfile(info)
        fakebin.stub("rsync")
        result = run_script("update.sh", fakebin, cwd=tmp_path)
        assert result.returncode != 0
        assert "exactly one top-level directory" in result.stderr

    def test_appledouble_members_are_named_as_the_cause(self, fakebin, tmp_path):
        _deploy(fakebin, tmp_path)
        import tarfile

        archive = tmp_path / "update" / "epicurrents-test.tar.gz"
        archive.parent.mkdir()
        with tarfile.open(archive, "w:gz") as tf:
            for name in ("epicurrents-test/docker-compose.yml", "._epicurrents-test"):
                info = tarfile.TarInfo(name)
                info.size = 0
                tf.addfile(info)
        fakebin.stub("rsync")
        result = run_script("update.sh", fakebin, cwd=tmp_path)
        assert result.returncode != 0
        assert "COPYFILE_DISABLE" in result.stderr

    def test_deployment_owned_files_in_a_package_are_refused(self, fakebin, tmp_path):
        _deploy(fakebin, tmp_path)
        _build_package(tmp_path, files={".env": "SECRET=1\n"}, manifest=False)
        fakebin.stub("rsync")
        result = run_script("update.sh", fakebin, cwd=tmp_path)
        assert result.returncode != 0
        assert ".env, .git/ or backups/" in result.stderr

    def test_a_package_packed_with_a_dot_prefix_is_accepted(self, fakebin, tmp_path):
        # `tar -C dir .` writes ./epicurrents-test/…; the one-directory rule
        # looks past the prefix.
        _deploy(fakebin, tmp_path)
        tree = tmp_path / "pkg-tree" / "epicurrents-test"
        tree.mkdir(parents=True)
        (tree / "docker-compose.yml").write_text("services: {}\n")
        archive = tmp_path / "update" / "epicurrents-test.tar.gz"
        archive.parent.mkdir()
        subprocess.run(["/usr/bin/tar", "-czf", str(archive), "-C", str(tree.parent), "."], check=True)
        fakebin.stub("rsync")
        result = run_script("update.sh", fakebin, cwd=tmp_path)
        assert result.returncode == 0, result.stderr


class TestCheckArchive:
    """--check-archive runs every check an update runs and stops: it drives no
    container, extracts nothing and writes nothing, and reports what it found
    on ``::`` lines a caller can read.
    """

    def test_makes_no_state_change_and_needs_no_runtime(self, fakebin, tmp_path):
        _deploy(fakebin, tmp_path)
        archive = _build_package(tmp_path)
        fakebin.remove("docker")
        fakebin.remove("podman")
        fakebin.stub("rsync")
        result = run_script("update.sh", fakebin, cwd=tmp_path, args=["--check-archive", str(archive)])
        assert result.returncode == 0, result.stderr
        _nothing_touched(fakebin, tmp_path)
        assert not any(c.startswith(("docker", "podman")) for c in fakebin.calls())
        lines = _progress(result)
        assert "check=ok" in lines and "version=0.2.0" in lines and "installed=0.1.0" in lines
        assert f"archive={archive}" in lines
        assert "done" not in lines, "check mode is not an update; ::done belongs to one"

    def test_reports_what_the_tree_holds_that_the_package_does_not(self, fakebin, tmp_path):
        _deploy(fakebin, tmp_path)
        archive = _build_package(tmp_path, files={"epicurrents/keep.py": ""})
        # In the tree under a directory the package populates, not in the package.
        (tmp_path / "epicurrents" / "stale.py").write_text("")
        # Protected, so not a candidate even though the package lacks it.
        (tmp_path / "static").mkdir()
        (tmp_path / "static" / "old.css").write_text("")
        result = run_script("update.sh", fakebin, cwd=tmp_path, args=["--check-archive", str(archive)])
        assert result.returncode == 0, result.stderr
        assert "orphan_candidates=1" in _progress(result)
        assert "epicurrents/stale.py" in result.stdout
        assert "old.css" not in result.stdout

    def test_a_refusal_exits_nonzero_with_the_reason(self, fakebin, tmp_path):
        _deploy(fakebin, tmp_path)
        archive = _build_package(tmp_path, version="0.1.0")
        result = run_script(
            "update.sh", fakebin, cwd=tmp_path, args=["--check-archive", str(archive), "--require-newer"]
        )
        assert result.returncode != 0
        assert "same as the installed release" in result.stderr
        assert "check=ok" not in _progress(result)


class TestOrphanPruning:
    """After the overlay, what the previous package shipped and the new one does
    not is removed — and only that. The first update with a file list records
    it and deletes nothing, because there is no previous list to subtract from.
    """

    def _installed(self, tmp_path, *files):
        for rel in files:
            path = tmp_path / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("old")
        (tmp_path / ".epicurrents-files").write_text("\n".join(sorted(files)) + "\n")

    def test_the_first_update_records_the_list_and_deletes_nothing(self, fakebin, tmp_path):
        _deploy(fakebin, tmp_path)
        (tmp_path / "epicurrents" / "gone.py").write_text("old")
        _build_package(tmp_path)
        fakebin.stub("rsync")
        result = run_script("update.sh", fakebin, cwd=tmp_path)
        assert result.returncode == 0, result.stderr
        assert (tmp_path / "epicurrents" / "gone.py").exists()
        assert "starts with the next update" in result.stdout
        recorded = (tmp_path / ".epicurrents-files").read_text().splitlines()
        assert "FILELIST" in recorded and "epicurrents/version.py" in recorded

    def test_the_next_update_prunes_old_minus_new_only(self, fakebin, tmp_path):
        _deploy(fakebin, tmp_path)
        self._installed(
            tmp_path,
            "epicurrents/version.py",
            "epicurrents/dropped/__init__.py",
            "epicurrents/dropped/tasks.py",
            "recordings/migrations/0009_removed.py",
            "docker-compose.yml",
        )
        (tmp_path / "docker-compose.override.yml").write_text("operator")   # never listed
        (tmp_path / "epicurrents" / "generated.bin").write_text("runtime")   # never listed
        _build_package(tmp_path, files={"recordings/migrations/0009_replacement.py": ""})
        fakebin.stub("rsync")
        result = run_script("update.sh", fakebin, cwd=tmp_path)
        assert result.returncode == 0, result.stderr
        assert not (tmp_path / "epicurrents" / "dropped").exists(), "the emptied package directory was not removed"
        assert not (tmp_path / "recordings" / "migrations" / "0009_removed.py").exists()
        assert (tmp_path / "epicurrents" / "version.py").exists()
        assert (tmp_path / "docker-compose.override.yml").exists(), "an operator file was pruned"
        assert (tmp_path / "epicurrents" / "generated.bin").exists(), "an unlisted file was pruned"
        assert "Pruned 3 file(s)" in result.stdout
        assert (tmp_path / ".epicurrents-files").read_text().splitlines() == sorted(
            ["FILELIST", "docker-compose.yml", "epicurrents/version.py", "recordings/migrations/0009_replacement.py"]
        )

    def test_protected_paths_are_never_pruned_even_when_listed(self, fakebin, tmp_path):
        _deploy(fakebin, tmp_path)
        self._installed(
            tmp_path,
            "epicurrents/version.py",
            "static/old.css",
            "frontend/vendor/pyodide/x.wasm",
            "recordings/converters/vendored/bin",
            "projects/mine/models.py",
            "update/README.md",
            "local/notes.md",
        )
        (tmp_path / "projects" / "mine" / ".git").mkdir()
        _build_package(tmp_path)
        fakebin.stub("rsync")
        result = run_script("update.sh", fakebin, cwd=tmp_path)
        assert result.returncode == 0, result.stderr
        for rel in (
            "static/old.css",
            "frontend/vendor/pyodide/x.wasm",
            "recordings/converters/vendored/bin",
            "projects/mine/models.py",
            "update/README.md",
            "local/notes.md",
        ):
            assert (tmp_path / rel).exists(), f"{rel} was pruned"
        assert "Pruned 0 file(s)" in result.stdout

    def test_a_copied_project_is_the_packages_and_is_pruned(self, fakebin, tmp_path):
        # Without a .git the project tree came from a package, so a file the new
        # package dropped goes.
        _deploy(fakebin, tmp_path)
        self._installed(tmp_path, "epicurrents/version.py", "projects/mine/old.py")
        _build_package(tmp_path)
        fakebin.stub("rsync")
        result = run_script("update.sh", fakebin, cwd=tmp_path)
        assert result.returncode == 0, result.stderr
        assert not (tmp_path / "projects" / "mine" / "old.py").exists()

    def test_a_package_without_a_list_prunes_nothing_and_says_so(self, fakebin, tmp_path):
        _deploy(fakebin, tmp_path)
        self._installed(tmp_path, "epicurrents/version.py", "epicurrents/dropped.py")
        _build_package(tmp_path, filelist=False)
        fakebin.stub("rsync")
        result = run_script("update.sh", fakebin, cwd=tmp_path)
        assert result.returncode == 0, result.stderr
        assert (tmp_path / "epicurrents" / "dropped.py").exists()
        assert "no FILELIST" in result.stdout

    def test_the_installed_list_travels_with_the_code_snapshot(self):
        # A root file, so the snapshot's `tar -C . .` carries it and a rollback
        # restores it with the code it describes; the excludes must not name it.
        body = (SCRIPTS_DIR / "update.sh").read_text()
        assert 'INSTALLED_FILELIST="./.epicurrents-files"' in body
        assert ".epicurrents-files" not in body.split("snapshot_code()")[1].split("}")[0]


class TestProgressLines:
    """A caller that tees the output follows the ``::`` lines: which step is
    running, when the snapshot is complete (the point past which a rollback
    is well defined), whether the health check passed, and how the run ended.
    """

    def test_an_update_reports_its_steps_in_order_and_ends_with_done(self, fakebin, tmp_path):
        _deploy(fakebin, tmp_path)
        _build_package(tmp_path)
        fakebin.stub("rsync")
        fakebin.stub("curl")
        result = run_script("update.sh", fakebin, cwd=tmp_path)
        assert result.returncode == 0, result.stderr
        lines = _progress(result)
        steps = [line for line in lines if line.startswith("step=")]
        assert steps == [
            "step=check", "step=snapshot", "step=acquire", "step=backup", "step=build", "step=stop",
            "step=migrate", "step=static", "step=vendor", "step=recreate", "step=health",
        ], steps
        snapshot = next(line for line in lines if line.startswith("snapshot="))
        assert snapshot.startswith("snapshot=./backups/pre-update-")
        assert lines.index(snapshot) > lines.index("step=backup")
        assert lines.index(snapshot) < lines.index("step=build")
        assert "health=ok" in lines
        assert lines[-1] == "done"

    def test_a_failed_health_check_is_reported_on_a_line(self, fakebin, tmp_path):
        _deploy(fakebin, tmp_path)
        fakebin.stub("curl", exit_code=1)
        result = run_script("update.sh", fakebin, cwd=tmp_path, args=["--from", "repo", "--no-pull"])
        assert result.returncode == 0, result.stderr
        assert "health=failed" in _progress(result)

    def test_a_failure_before_the_snapshot_completes_reports_no_snapshot(self, fakebin, tmp_path):
        _deploy(fakebin, tmp_path)
        fakebin.stub("docker", body=_docker_stub('*"pg_dump"*) exit 1 ;;'))
        result = run_script("update.sh", fakebin, cwd=tmp_path, args=["--from", "repo", "--no-pull"])
        assert result.returncode != 0
        lines = _progress(result)
        assert not any(line.startswith("snapshot=") for line in lines)
        assert any(line.startswith("failed=") for line in lines)
        assert "done" not in lines

    def test_a_rollback_reports_its_steps(self, fakebin, tmp_path):
        _deploy(fakebin, tmp_path)
        TestRollback()._seed_snapshot(tmp_path, with_code=True)
        fakebin.stub("rsync")
        fakebin.stub("curl")
        result = run_script("update.sh", fakebin, cwd=tmp_path, args=["--rollback", "--yes"])
        assert result.returncode == 0, result.stderr
        steps = [line for line in _progress(result) if line.startswith("step=")]
        assert steps == [
            "step=stop", "step=restore-db", "step=restore-env", "step=restore-code", "step=build",
            "step=static", "step=vendor", "step=recreate", "step=health",
        ], steps
        assert _progress(result)[-1] == "done"


class TestNamedSnapshots:
    """--snapshot LABEL takes a snapshot and exits; --rollback --snapshot NAME
    restores that one. A snapshot named for its purpose is what a caller takes
    before rolling back, and the name is what keeps --rollback from picking it
    in place of the pre-update one.
    """

    def test_snapshot_mode_writes_a_complete_snapshot_and_changes_nothing_else(self, fakebin, tmp_path):
        _deploy(fakebin, tmp_path)
        result = run_script("update.sh", fakebin, cwd=tmp_path, args=["--snapshot", "post-update"])
        assert result.returncode == 0, result.stderr
        snaps = list((tmp_path / "backups").glob("post-update-*"))
        assert len(snaps) == 1, snaps
        snap = snaps[0]
        assert (snap / "code.tar.gz").is_file() and (snap / "db.sql.gz").is_file() and (snap / ".env").is_file()
        manifest = (snap / "MANIFEST").read_text()
        assert "mode=snapshot" in manifest and "label=post-update" in manifest and "version=0.1.0" in manifest
        lines = _progress(result)
        assert f"snapshot=./backups/{snap.name}" in lines and lines[-1] == "done"
        assert not fakebin.has_call("stop web"), "a snapshot stops nothing"
        assert not fakebin.has_call("--force-recreate")
        assert not (tmp_path / "update" / "maintenance.json").exists()
        assert f"--rollback --snapshot {snap.name}" in result.stdout

    def test_a_label_is_validated(self, fakebin, tmp_path):
        _deploy(fakebin, tmp_path)
        result = run_script("update.sh", fakebin, cwd=tmp_path, args=["--snapshot", "../escape"])
        assert result.returncode != 0
        assert "a label is letters" in result.stderr
        assert not (tmp_path / "backups").exists()

    def test_snapshot_does_not_combine_with_an_update_source(self, fakebin, tmp_path):
        _deploy(fakebin, tmp_path)
        result = run_script("update.sh", fakebin, cwd=tmp_path, args=["--snapshot", "x", "--from", "repo"])
        assert result.returncode != 0
        assert "does not combine" in result.stderr

    def test_rollback_ignores_named_snapshots_by_default(self, fakebin, tmp_path):
        _deploy(fakebin, tmp_path)
        TestRollback()._seed_snapshot(tmp_path)
        newer = tmp_path / "backups" / "post-update-20990101-000000"
        newer.mkdir()
        with gzlib.open(newer / "db.sql.gz", "wt") as fh:
            fh.write("-- post\n")
        (newer / ".env").write_text("DJANGO_MODE=production\nHOST_PORT=8000\n")
        result = run_script("update.sh", fakebin, cwd=tmp_path, args=["--rollback", "--yes"])
        assert result.returncode == 0, result.stderr
        assert "Rolling back to pre-update-20200101-000000" in result.stdout

    def test_rollback_restores_the_named_snapshot(self, fakebin, tmp_path):
        _deploy(fakebin, tmp_path)
        TestRollback()._seed_snapshot(tmp_path)
        named = tmp_path / "backups" / "post-update-20990101-000000"
        named.mkdir()
        with gzlib.open(named / "db.sql.gz", "wt") as fh:
            fh.write("-- the named one\n")
        (named / ".env").write_text("DJANGO_MODE=production\nHOST_PORT=8000\nNAMED=1\n")
        capture = tmp_path / "restore-stdin.sql"
        fakebin.stub("docker", body=_docker_stub(f'*" exec "*) cat > "{capture}" ;;'))
        result = run_script(
            "update.sh", fakebin, cwd=tmp_path, args=["--rollback", "--yes", "--snapshot", named.name]
        )
        assert result.returncode == 0, result.stderr
        assert "-- the named one" in capture.read_text()
        assert "NAMED=1" in (tmp_path / ".env").read_text()

    def test_a_missing_or_incomplete_named_snapshot_is_refused(self, fakebin, tmp_path):
        _deploy(fakebin, tmp_path)
        result = run_script("update.sh", fakebin, cwd=tmp_path, args=["--rollback", "--yes", "--snapshot", "nope"])
        assert result.returncode != 0
        assert "No snapshot named nope" in result.stderr
        half = tmp_path / "backups" / "post-update-1"
        half.mkdir(parents=True)
        (half / ".env").write_text("HOST_PORT=8000\n")
        result = run_script("update.sh", fakebin, cwd=tmp_path, args=["--rollback", "--yes", "--snapshot", half.name])
        assert result.returncode != 0
        assert "incomplete" in result.stderr
        assert not fakebin.has_call("psql")

    def test_a_snapshot_name_is_a_bare_directory_name(self, fakebin, tmp_path):
        _deploy(fakebin, tmp_path)
        result = run_script("update.sh", fakebin, cwd=tmp_path, args=["--rollback", "--yes", "--snapshot", "../x"])
        assert result.returncode != 0
        assert "names a directory under" in result.stderr


class TestCodeOnlyRollback:
    """--rollback --code-only restores the code and rebuilds, keeping the
    database and .env, and only while the migrations the database records as
    applied are the ones the snapshot recorded — checked before anything is
    touched, and refused by name so a caller can key on it.
    """

    def _seed(self, tmp_path, *, migrations="", with_code=True, record=True):
        snap = TestRollback()._seed_snapshot(tmp_path, with_code=with_code)
        if record:
            (snap / "migrations.txt").write_text(migrations)
        return snap

    def _psql_answers(self, lines: str):
        """A docker stub whose `exec … psql` prints ``lines`` (the applied migrations)."""
        return _docker_stub(f'*" psql "*) cat >/dev/null 2>&1; printf \'{lines}\' ;;')

    def test_restores_the_code_and_leaves_the_database_and_env_alone(self, fakebin, tmp_path):
        _deploy(fakebin, tmp_path)
        fakebin.stub("docker", body=self._psql_answers("a.0001\\n"))
        fakebin.stub("rsync")
        self._seed(tmp_path, migrations="a.0001\n")
        env_before = (tmp_path / ".env").read_text()
        result = run_script("update.sh", fakebin, cwd=tmp_path, args=["--rollback", "--yes", "--code-only"])
        assert result.returncode == 0, result.stderr
        lines = _progress(result)
        assert "step=restore-code" in lines and "step=build" in lines and "restored=0.1.0" in lines
        assert "step=restore-db" not in lines and "step=restore-env" not in lines
        assert not any("psql --single-transaction" in c for c in fakebin.calls())
        assert (tmp_path / ".env").read_text() == env_before
        assert "the database and .env were kept" in result.stdout
        assert lines[-1] == "done"

    def test_refuses_when_a_migration_was_applied_since_the_snapshot(self, fakebin, tmp_path):
        _deploy(fakebin, tmp_path)
        fakebin.stub("docker", body=self._psql_answers("a.0001\\na.0002\\n"))
        self._seed(tmp_path, migrations="a.0001\n")
        result = run_script("update.sh", fakebin, cwd=tmp_path, args=["--rollback", "--yes", "--code-only"])
        assert result.returncode != 0
        lines = _progress(result)
        assert "refused=code_only" in lines and lines.index("refused=code_only") < lines.index(next(l for l in lines if l.startswith("failed=")))
        assert "Migrations were applied since" in result.stderr
        assert not fakebin.has_call("stop web") and not fakebin.has_call("--force-recreate")
        assert not (tmp_path / "update" / "maintenance.json").exists()

    def test_the_record_is_compared_as_a_set_not_as_text(self, fakebin, tmp_path):
        _deploy(fakebin, tmp_path)
        fakebin.stub("docker", body=self._psql_answers("b.0001\\na.0001\\n"))
        fakebin.stub("rsync")
        self._seed(tmp_path, migrations="b.0001\na.0001\n")
        result = run_script("update.sh", fakebin, cwd=tmp_path, args=["--rollback", "--yes", "--code-only"])
        assert result.returncode == 0, result.stderr

    def test_refuses_a_snapshot_without_a_migration_record(self, fakebin, tmp_path):
        _deploy(fakebin, tmp_path)
        self._seed(tmp_path, record=False)
        result = run_script("update.sh", fakebin, cwd=tmp_path, args=["--rollback", "--yes", "--code-only"])
        assert result.returncode != 0
        assert "refused=code_only" in _progress(result) and "predates migration records" in result.stderr

    def test_needs_a_snapshot_with_code(self, fakebin, tmp_path):
        _deploy(fakebin, tmp_path)
        self._seed(tmp_path, with_code=False)
        result = run_script("update.sh", fakebin, cwd=tmp_path, args=["--rollback", "--yes", "--code-only"])
        assert result.returncode != 0 and "needs a snapshot with a code archive" in result.stderr

    def test_code_only_belongs_to_rollback(self, fakebin, tmp_path):
        _deploy(fakebin, tmp_path)
        result = run_script("update.sh", fakebin, cwd=tmp_path, args=["--code-only"])
        assert result.returncode != 0 and "applies to --rollback only" in result.stderr

    def test_a_full_rollback_still_restores_the_database_and_reports_the_version(self, fakebin, tmp_path):
        _deploy(fakebin, tmp_path)
        fakebin.stub("rsync")
        TestRollback()._seed_snapshot(tmp_path, with_code=True)
        result = run_script("update.sh", fakebin, cwd=tmp_path, args=["--rollback", "--yes"])
        assert result.returncode == 0, result.stderr
        lines = _progress(result)
        assert "step=restore-db" in lines and "restored=0.1.0" in lines


class TestMigrationRecords:
    """Every snapshot records the migrations the database had applied, and an
    update reports whether it applied any, which is what decides a code-only
    rollback later.
    """

    def _counting_psql(self, tmp_path, *, changes_at: int):
        """A docker stub whose psql answers grow by one migration from its ``changes_at``-th call on."""
        counter = tmp_path / "psql-calls"
        return _docker_stub(
            f'*" psql "*) cat >/dev/null 2>&1; n=$(cat "{counter}" 2>/dev/null || echo 0); n=$((n + 1)); '
            f'echo "$n" > "{counter}"; if [ "$n" -ge {changes_at} ]; then printf \'a.0001\\na.0002\\n\'; '
            f'else printf \'a.0001\\n\'; fi ;;'
        )

    def test_a_named_snapshot_records_the_applied_migrations_sorted(self, fakebin, tmp_path):
        _deploy(fakebin, tmp_path)
        fakebin.stub("docker", body=_docker_stub('*" psql "*) cat >/dev/null 2>&1; printf \'b.0001\\na.0001\\n\' ;;'))
        result = run_script("update.sh", fakebin, cwd=tmp_path, args=["--snapshot", "post-update"])
        assert result.returncode == 0, result.stderr
        snap = next((tmp_path / "backups").glob("post-update-*"))
        assert (snap / "migrations.txt").read_text() == "a.0001\nb.0001\n"
        assert any("django_migrations" in c for c in fakebin.calls())

    def test_an_update_that_migrates_says_so_in_its_snapshot(self, fakebin, tmp_path):
        _deploy(fakebin, tmp_path)
        _build_package(tmp_path)
        fakebin.stub("rsync")
        fakebin.stub("curl")
        # The snapshot reads once, the migrate step reads before and after:
        # the third answer is the one that grew.
        fakebin.stub("docker", body=self._counting_psql(tmp_path, changes_at=3))
        result = run_script("update.sh", fakebin, cwd=tmp_path)
        assert result.returncode == 0, result.stderr
        lines = _progress(result)
        assert "migrations=applied" in lines
        assert lines.index("migrations=applied") > lines.index("step=migrate")
        snap = next((tmp_path / "backups").glob("pre-update-*"))
        manifest = (snap / "MANIFEST").read_text()
        assert "migrations=applied" in manifest and "version=0.1.0" in manifest
        assert (snap / "migrations.txt").read_text() == "a.0001\n"

    def test_an_update_that_applies_nothing_says_none(self, fakebin, tmp_path):
        _deploy(fakebin, tmp_path)
        _build_package(tmp_path)
        fakebin.stub("rsync")
        fakebin.stub("curl")
        fakebin.stub("docker", body=self._counting_psql(tmp_path, changes_at=99))
        result = run_script("update.sh", fakebin, cwd=tmp_path)
        assert result.returncode == 0, result.stderr
        assert "migrations=none" in _progress(result)
        snap = next((tmp_path / "backups").glob("pre-update-*"))
        assert "migrations=none" in (snap / "MANIFEST").read_text()
        assert "can be rolled back with --code-only" in result.stdout


class TestSeveralReleaseKeys:
    """--release-key is repeatable and the first key that verifies wins; by
    default the key at the root and the successor beside it are both tried.
    A successor a release announced is what lets the next release, signed with
    it, verify on a deployment that never saw the new key by hand.
    """

    def _real_verification(self, fakebin):
        # No usable openssl, so the check goes through python3 with cryptography
        # — the suite's own interpreter — and a wrong key really fails.
        fakebin.stub("openssl", body=OPENSSL_TOO_OLD)
        fakebin.stub("python3", body=f'exec {sys.executable} "$@"')

    def _other_key(self, tmp_path):
        other = tmp_path / "keys" / "other.key"
        _helper("keygen", str(other))
        return other.with_name("other.key.pub")

    def test_the_first_key_that_verifies_is_named(self, fakebin, tmp_path):
        _deploy(fakebin, tmp_path)
        key, pub = _sign_key(tmp_path)
        wrong = self._other_key(tmp_path)
        _build_package(tmp_path, sign_key=key)
        fakebin.stub("rsync")
        self._real_verification(fakebin)
        result = run_script(
            "update.sh", fakebin, cwd=tmp_path,
            args=["--require-signature", "--release-key", str(wrong), "--release-key", str(pub)],
        )
        assert result.returncode == 0, result.stderr
        lines = _progress(result)
        assert "signature=verified" in lines and f"key={pub}" in lines

    def test_the_successor_beside_the_root_key_is_tried_by_default(self, fakebin, tmp_path):
        _deploy(fakebin, tmp_path)
        key, pub = _sign_key(tmp_path)
        shutil.copy(self._other_key(tmp_path), tmp_path / "RELEASE_KEY.pub")
        shutil.copy(pub, tmp_path / "RELEASE_KEY.next.pub")
        _build_package(tmp_path, sign_key=key)
        fakebin.stub("rsync")
        self._real_verification(fakebin)
        result = run_script("update.sh", fakebin, cwd=tmp_path, args=["--require-signature"])
        assert result.returncode == 0, result.stderr
        assert f"key={tmp_path}/RELEASE_KEY.next.pub" in _progress(result)

    def test_no_key_verifying_is_refused_by_name(self, fakebin, tmp_path):
        _deploy(fakebin, tmp_path)
        key, _ = _sign_key(tmp_path)
        shutil.copy(self._other_key(tmp_path), tmp_path / "RELEASE_KEY.pub")
        _build_package(tmp_path, sign_key=key)
        self._real_verification(fakebin)
        result = run_script("update.sh", fakebin, cwd=tmp_path, args=["--require-signature"])
        assert result.returncode != 0
        assert "refused=signature" in _progress(result) and "does NOT verify against" in result.stderr
        assert not (tmp_path / "backups").exists()

    def test_a_missing_key_among_several_is_skipped(self, fakebin, tmp_path):
        _deploy(fakebin, tmp_path)
        key, pub = _sign_key(tmp_path)
        _build_package(tmp_path, sign_key=key)
        fakebin.stub("rsync")
        self._real_verification(fakebin)
        result = run_script(
            "update.sh", fakebin, cwd=tmp_path,
            args=["--require-signature", "--release-key", str(tmp_path / "nope.pub"), "--release-key", str(pub)],
        )
        assert result.returncode == 0, result.stderr
        assert f"key={pub}" in _progress(result)


class TestPodmanAsRoot:
    @requires_gnu_stat
    def test_a_root_run_drives_podman_without_sudo(self, fakebin, tmp_path):
        # A root-owned updater's host may have no sudo at all; root needs none.
        _deploy(fakebin, tmp_path)
        fakebin.remove("docker")
        fakebin.stub("podman", body=PODMAN_PS_RUNNING)
        fakebin.stub("id", body='case "${1:-}" in -u|-g) echo 0 ;; *) echo "uid=0(root) gid=0(root)" ;; esac')
        result = run_script("update.sh", fakebin, cwd=tmp_path, args=["--from", "repo", "--no-pull"])
        assert result.returncode == 0, result.stderr
        assert fakebin.has_call("podman compose -f docker-compose.yml")
        assert not fakebin.has_call("sudo")


class TestPruningStaysInsideTheTree:
    """The installed file list is written by whoever can write the tree, which
    on a host with an updater is not who runs this script. An entry that
    escapes the tree, by `..` or through a symlinked directory, must not turn
    the prune into a deletion elsewhere.
    """

    def _lists(self, tmp_path, *old):
        (tmp_path / ".epicurrents-files").write_text("\n".join(old) + "\n")
        _build_package(tmp_path)

    def test_a_parent_reference_in_the_old_list_is_not_followed(self, fakebin, tmp_path):
        _deploy(fakebin, tmp_path)
        outside = tmp_path.parent / f"{tmp_path.name}-outside"
        outside.mkdir()
        victim = outside / "victim"
        victim.write_text("keep")
        self._lists(tmp_path, "epicurrents/version.py", f"../{outside.name}/victim", "/etc/passwd")
        fakebin.stub("rsync")
        result = run_script("update.sh", fakebin, cwd=tmp_path)
        assert result.returncode == 0, result.stderr
        assert victim.exists(), "a .. entry in the old list deleted outside the tree"
        assert "Not pruning" in result.stdout

    def test_a_symlinked_directory_in_the_old_list_is_not_followed(self, fakebin, tmp_path):
        _deploy(fakebin, tmp_path)
        outside = tmp_path.parent / f"{tmp_path.name}-outside"
        outside.mkdir()
        (outside / "hosts").write_text("keep")
        os.symlink(outside, tmp_path / "evil")
        self._lists(tmp_path, "epicurrents/version.py", "evil/hosts")
        fakebin.stub("rsync")
        result = run_script("update.sh", fakebin, cwd=tmp_path)
        assert result.returncode == 0, result.stderr
        assert (outside / "hosts").exists(), "a symlinked directory was followed out of the tree"
        assert outside.exists()

    def test_a_symlinked_directory_inside_the_tree_is_fine(self, fakebin, tmp_path):
        _deploy(fakebin, tmp_path)
        (tmp_path / "real").mkdir()
        (tmp_path / "real" / "old.py").write_text("")
        os.symlink(tmp_path / "real", tmp_path / "alias")
        self._lists(tmp_path, "epicurrents/version.py", "alias/old.py")
        fakebin.stub("rsync")
        result = run_script("update.sh", fakebin, cwd=tmp_path)
        assert result.returncode == 0, result.stderr
        assert not (tmp_path / "real" / "old.py").exists()

    def test_the_release_key_is_never_pruned(self, fakebin, tmp_path):
        # A later unsigned package lists no key; pruning it would leave every
        # --require-signature run refusing.
        _deploy(fakebin, tmp_path)
        (tmp_path / "RELEASE_KEY.pub").write_text("key")
        self._lists(tmp_path, "epicurrents/version.py", "RELEASE_KEY.pub")
        fakebin.stub("rsync")
        result = run_script("update.sh", fakebin, cwd=tmp_path)
        assert result.returncode == 0, result.stderr
        assert (tmp_path / "RELEASE_KEY.pub").exists()


class TestListingHardeningEdges:
    def _archive(self, tmp_path, members):
        import tarfile

        archive = tmp_path / "update" / "epicurrents-test.tar.gz"
        archive.parent.mkdir(exist_ok=True)
        with tarfile.open(archive, "w:gz") as tf:
            for name, kind in members:
                info = tarfile.TarInfo(name)
                if kind == "dir":
                    info.type = tarfile.DIRTYPE
                elif kind == "chr":
                    info.type = tarfile.CHRTYPE
                    info.devmajor, info.devminor = 1, 3
                elif kind == "setuid":
                    info.mode = 0o4755
                info.size = 0
                tf.addfile(info)
        return archive

    def test_a_wrapper_name_with_a_regex_metacharacter_is_accepted(self, fakebin, tmp_path):
        _deploy(fakebin, tmp_path)
        _build_package(tmp_path, top="epicurrents+dist(1)", manifest=False)
        fakebin.stub("rsync")
        result = run_script("update.sh", fakebin, cwd=tmp_path)
        assert result.returncode == 0, result.stderr

    def test_a_dot_component_does_not_hide_a_forbidden_member(self, fakebin, tmp_path):
        _deploy(fakebin, tmp_path)
        self._archive(tmp_path, [("top/docker-compose.yml", "file"), ("top/./.env", "file")])
        fakebin.stub("rsync")
        result = run_script("update.sh", fakebin, cwd=tmp_path)
        assert result.returncode != 0
        assert ".env, .git/ or backups/" in result.stderr
        self._archive(tmp_path, [("top/docker-compose.yml", "file"), ("top//backups/x", "file")])
        result = run_script("update.sh", fakebin, cwd=tmp_path)
        assert result.returncode != 0
        assert ".env, .git/ or backups/" in result.stderr

    def test_a_device_node_is_refused(self, fakebin, tmp_path):
        _deploy(fakebin, tmp_path)
        self._archive(tmp_path, [("top/docker-compose.yml", "file"), ("top/null", "chr")])
        fakebin.stub("rsync")
        result = run_script("update.sh", fakebin, cwd=tmp_path)
        assert result.returncode != 0
        assert "device node" in result.stderr
        _nothing_touched(fakebin, tmp_path)

    def test_a_setuid_file_is_refused(self, fakebin, tmp_path):
        _deploy(fakebin, tmp_path)
        self._archive(tmp_path, [("top/docker-compose.yml", "file"), ("top/bin/escalate", "setuid")])
        fakebin.stub("rsync")
        result = run_script("update.sh", fakebin, cwd=tmp_path)
        assert result.returncode != 0
        assert "setuid or setgid" in result.stderr

    def test_help_exits_zero(self, fakebin, tmp_path):
        # sed piped into a sed that quits early returned 141 under pipefail.
        _deploy(fakebin, tmp_path)
        result = run_script("update.sh", fakebin, cwd=tmp_path, args=["--help"])
        assert result.returncode == 0, result.stderr
        assert "--check-archive" in result.stdout

    def test_a_relative_archive_path_is_taken_from_where_the_command_ran(self, fakebin, tmp_path):
        # --root from another directory: the archive named on the command line
        # is relative to that directory, not to the deployment.
        deployment = tmp_path / "deployment"
        deployment.mkdir()
        (deployment / "docker-compose.yml").write_text("services: {}\n")
        _deploy(fakebin, deployment)
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        _build_package(elsewhere, manifest=False)
        result = run_script(
            "update.sh", fakebin, cwd=elsewhere,
            args=["--root", str(deployment), "--check-archive", "update/epicurrents-test.tar.gz"],
        )
        assert result.returncode == 0, result.stderr
        assert "check=ok" in _progress(result)
