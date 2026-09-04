"""Dry-run tests for scripts/restore.sh.

The file-restore step must extract through the borg-restore compose
service (read-write data-volume mounts) rather than the backup-side borg
service, whose volumes are mounted read-only — extracting through the
latter writes into the ephemeral container layer and the restored files
vanish with ``--rm``. These tests pin the invocation shape; real volume
behaviour is Tier 3 territory.
"""

import subprocess

from scripts.tests.conftest import stage_script

RESTORE = "restore.sh"


def _run_restore(fakebin, tmp_path, *, replies: str, args=None):
    """Stage and run restore.sh, feeding ``replies`` to its prompts."""
    staged = stage_script(RESTORE, tmp_path)
    env = {
        "PATH": f"{fakebin.path}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "USER": "testuser",
        "LC_ALL": "C",
    }
    return subprocess.run(
        ["bash", str(staged), *(args or [])],
        check=False,
        cwd=tmp_path,
        env=env,
        input=replies,
        capture_output=True,
        text=True,
    )


class TestRestoreFileExtraction:
    def test_extracts_through_borg_restore_service(self, fakebin, tmp_path):
        """File restore must run in borg-restore (rw mounts), not borg (ro)."""
        result = _run_restore(fakebin, tmp_path, replies="y\ny\n", args=["archive-2026-06-11"])
        assert result.returncode == 0, result.stderr
        extract_calls = [c for c in fakebin.calls() if "borg extract" in c]
        assert extract_calls, "borg extract was never invoked"
        assert all("borg-restore" in c for c in extract_calls)

    def test_extracts_recordings_and_media_paths(self, fakebin, tmp_path):
        result = _run_restore(fakebin, tmp_path, replies="y\ny\n", args=["archive-2026-06-11"])
        assert result.returncode == 0, result.stderr
        assert fakebin.has_call("data/recordings data/media/uploads")

    def test_borg_warning_exit_code_does_not_abort(self, fakebin, tmp_path):
        """borg exits 1 for warnings (e.g. media path absent from an old
        archive); the restore must continue to the migrate step."""
        fakebin.stub(
            "docker",
            body=r"""
case "$*" in
    *"borg extract"*) exit 1 ;;
esac
""",
        )
        result = _run_restore(fakebin, tmp_path, replies="y\ny\n", args=["archive-2026-06-11"])
        assert result.returncode == 0, result.stderr
        assert fakebin.has_call("manage.py migrate")

    def test_borg_error_exit_code_aborts(self, fakebin, tmp_path):
        fakebin.stub(
            "docker",
            body=r"""
case "$*" in
    *"borg extract"*) exit 2 ;;
esac
""",
        )
        result = _run_restore(fakebin, tmp_path, replies="y\ny\n", args=["archive-2026-06-11"])
        assert result.returncode != 0
        assert not fakebin.has_call("manage.py migrate")

    def test_declining_file_restore_skips_extraction(self, fakebin, tmp_path):
        result = _run_restore(fakebin, tmp_path, replies="y\nn\n", args=["archive-2026-06-11"])
        assert result.returncode == 0, result.stderr
        assert not fakebin.has_call("borg extract")
        assert fakebin.has_call("manage.py migrate")


class TestRestoreSequencing:
    def test_migrate_runs_after_db_restore_before_app_start(self, fakebin, tmp_path):
        result = _run_restore(fakebin, tmp_path, replies="y\nn\n", args=["archive-2026-06-11"])
        assert result.returncode == 0, result.stderr
        calls = fakebin.calls()
        restore_idx = next(i for i, c in enumerate(calls) if "borgmatic restore" in c)
        migrate_idx = next(i for i, c in enumerate(calls) if "manage.py migrate" in c)
        start_idx = next(i for i, c in enumerate(calls) if "compose start" in c)
        assert restore_idx < migrate_idx < start_idx
