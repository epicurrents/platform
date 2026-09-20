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
import json
import subprocess

import pytest

from scripts.tests.conftest import SCRIPTS_DIR, make_env, run_script

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


def _deploy(fakebin, tmp_path, **env):
    """Set up a fake deployment root: a .env plus running-container stubs."""
    make_env(tmp_path, **env)
    # update.sh refuses a tree uid 1000 cannot write, and pytest's tmp_path belongs
    # to whoever runs the suite. World-writable satisfies the check without needing
    # a uid the test cannot have — the same escape a real deployment gets.
    tmp_path.chmod(0o777)
    fakebin.stub("docker", body=DOCKER_PS_RUNNING)
    fakebin.stub("cp", body=REAL_CP)


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
        (tmp_path / "update").mkdir()
        (tmp_path / "update" / "epicurrents-test.tar.gz").write_bytes(b"x")
        # tar "extracts" a package whose root carries a docker-compose.yml so the
        # archive-validity check passes without a real tarball.
        fakebin.stub(
            "tar",
            body=r"""
dir=""; prev=""
for a in "$@"; do
    [ "$prev" = "-C" ] && dir="$a"
    prev="$a"
done
mkdir -p "$dir/pkg"
: > "$dir/pkg/docker-compose.yml"
""",
        )
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
