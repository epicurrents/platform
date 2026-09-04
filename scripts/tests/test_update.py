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


def _deploy(fakebin, tmp_path, **env):
    """Set up a fake deployment root: a .env plus running-container stubs."""
    make_env(tmp_path, **env)
    fakebin.stub("docker", body=DOCKER_PS_RUNNING)
    fakebin.stub("cp", body=REAL_CP)


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
    def _seed_snapshot(self, tmp_path, *, with_db=True, with_env=True):
        snap = tmp_path / "backups" / "pre-update-20200101-000000"
        snap.mkdir(parents=True)
        if with_db:
            with gzlib.open(snap / "db.sql.gz", "wt") as fh:
                fh.write("-- dump\nSELECT 1;\n")
        if with_env:
            (snap / ".env").write_text("DJANGO_MODE=production\nHOST_PORT=8000\n")
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
