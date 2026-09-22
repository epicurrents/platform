"""The ``backfill_stored_hash`` command: stamps READY recordings that predate the field, and nothing else."""

from io import StringIO

import pytest
from django.core.management import call_command

from activity.models import Activity, ObjectChangeLog
from recordings.metadata import stored_hash_of
from recordings.models import Recording


def _make(user, tmp_path, name, *, status=Recording.Status.READY, stored_hash="", content=b"x" * 1024):
    path = tmp_path / f"{name}.edf"
    path.write_bytes(content)
    return Recording.objects.create(
        author=user,
        original_name=f"{name}.edf",
        stored_name=f"{name}.edf",
        file_extension=".edf",
        file_size=len(content),
        file_path=str(path),
        file_hash="a" * 64,
        stored_hash=stored_hash,
        status=status,
    )


def _run(*args):
    out, err = StringIO(), StringIO()
    call_command("backfill_stored_hash", *args, stdout=out, stderr=err)
    return out.getvalue(), err.getvalue()


@pytest.mark.django_db
class TestBackfillStoredHash:
    def test_stamps_a_ready_recording_lacking_a_hash(self, user, tmp_path):
        recording = _make(user, tmp_path, "A" * 32, content=b"stored bytes")
        out, _ = _run()
        recording.refresh_from_db()
        assert recording.stored_hash == stored_hash_of(tmp_path / f"{'A' * 32}.edf")
        assert "Stamped 1" in out

    def test_second_run_writes_nothing(self, user, tmp_path):
        _make(user, tmp_path, "B" * 32)
        _run()
        before = ObjectChangeLog.objects.count()
        out, _ = _run()
        assert ObjectChangeLog.objects.count() == before
        assert "Stamped 0" in out

    def test_dry_run_counts_without_writing(self, user, tmp_path):
        recording = _make(user, tmp_path, "C" * 32)
        out, _ = _run("--dry-run")
        recording.refresh_from_db()
        assert recording.stored_hash == ""
        assert "1 recording(s) would be stamped" in out

    def test_existing_hash_is_left_alone(self, user, tmp_path):
        recording = _make(user, tmp_path, "D" * 32, stored_hash="f" * 64)
        _run()
        recording.refresh_from_db()
        assert recording.stored_hash == "f" * 64

    def test_failed_and_trashed_recordings_are_not_candidates(self, user, tmp_path):
        failed = _make(user, tmp_path, "E" * 32, status=Recording.Status.FAILED)
        trashed = _make(user, tmp_path, "F" * 32)
        Recording.objects.filter(pk=trashed.pk).update(deleted_at="2026-01-01T00:00:00Z")
        out, _ = _run()
        failed.refresh_from_db()
        trashed.refresh_from_db()
        assert failed.stored_hash == ""
        assert trashed.stored_hash == ""
        assert "Stamped 0" in out

    def test_missing_file_is_skipped_and_reported(self, user, tmp_path):
        recording = _make(user, tmp_path, "G" * 32)
        (tmp_path / f"{'G' * 32}.edf").unlink()
        out, err = _run()
        recording.refresh_from_db()
        assert recording.stored_hash == ""
        assert "SKIP" in err
        assert "1 skipped" in out

    def test_run_is_audited_per_recording(self, user, tmp_path):
        recording = _make(user, tmp_path, "H" * 32)
        _run()
        activity = Activity.objects.filter(verb="recordings.stored_hash.backfill").latest("id")
        assert activity.interface == Activity.Interface.COMMAND
        assert activity.metadata == {"dry_run": False, "candidate_count": 1}
        rows = ObjectChangeLog.objects.filter(activity=activity, object_id=str(recording.pk))
        assert rows.count() == 1
        assert "stored_hash" in rows.first().changes
