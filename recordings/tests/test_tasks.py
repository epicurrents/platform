"""Tests for recordings Celery tasks — process_recording and purge_deleted_recordings."""

import hashlib
from pathlib import Path
from unittest.mock import patch

import pytest
from django.test import override_settings
from django.utils import timezone

from recordings.models import Recording
from recordings.tasks import process_recording, purge_deleted_recordings


def _make_pending_recording(user, staging_dir):
    """Create a Recording in PENDING state with a real staged file.

    Uses a ``.bin`` extension so the format processor is skipped and the task
    focuses on general infrastructure (file movement, status, notifications).
    EDF-specific processing is covered by test_edf_processor.py.
    """
    content = b"fake binary recording content"
    staged_file = staging_dir / "AABBCCDD11223344AABBCCDD11223344.bin"
    staged_file.write_bytes(content)
    return Recording.objects.create(
        author=user,
        original_name="test.bin",
        stored_name="AABBCCDD11223344AABBCCDD11223344.bin",
        file_extension=".bin",
        file_size=len(content),
        file_path=str(staged_file),
        file_hash=hashlib.sha256(content).hexdigest(),
        content_hash="",
        status=Recording.Status.PENDING,
    )


@pytest.mark.django_db
class TestProcessRecording:
    def test_moves_file_and_marks_ready(self, user, tmp_path):
        staging = tmp_path / "staging"
        uploads = tmp_path / "uploads"
        staging.mkdir()
        uploads.mkdir()
        recording = _make_pending_recording(user, staging)

        with (
            override_settings(
                RECORDINGS_STAGING_PATH=str(staging),
                RECORDINGS_UPLOAD_PATH=str(uploads),
            ),
            patch("notifications.tasks.send_push_to_user.delay"),
        ):
            result = process_recording(recording.pk)

        assert result["status"] == "ready"
        recording.refresh_from_db()
        assert recording.status == Recording.Status.READY
        assert recording.content_hash != ""
        assert Path(recording.file_path).exists()

    def test_unexpected_error_marks_failed_with_reason(self, user, tmp_path):
        """An unexpected (non-format) processing error preserves the row as
        FAILED with the reason in processing_error rather than deleting it,
        so an operator has something to inspect. A missing staged file is a
        convenient way to trigger the catastrophic path."""
        staging = tmp_path / "staging"
        uploads = tmp_path / "uploads"
        staging.mkdir()
        uploads.mkdir()
        recording = _make_pending_recording(user, staging)
        # Remove the staged file to simulate a missing-file error.
        Path(recording.file_path).unlink()

        with (
            override_settings(
                RECORDINGS_STAGING_PATH=str(staging),
                RECORDINGS_UPLOAD_PATH=str(uploads),
            ),
            patch("notifications.tasks.send_push_to_user.delay") as mock_push,
            pytest.raises(FileNotFoundError),
        ):
            process_recording(recording.pk)

        # Row is preserved (not deleted) and carries a diagnostic reason.
        recording.refresh_from_db()
        assert recording.status == Recording.Status.FAILED
        assert recording.processing_error.startswith("Unexpected processing error:")
        # The author is still notified of the failure.
        mock_push.assert_called_once()
        assert mock_push.call_args[1]["title"] == "Recording failed"

    def test_nonexistent_recording_is_skipped(self, user, tmp_path):
        """process_recording logs a warning and returns without raising for unknown IDs."""
        result = process_recording(999999)
        assert result is None  # task returns None when skipping

    def test_sends_push_notification_on_success(self, user, tmp_path):
        staging = tmp_path / "staging"
        uploads = tmp_path / "uploads"
        staging.mkdir()
        uploads.mkdir()
        recording = _make_pending_recording(user, staging)

        with (
            override_settings(
                RECORDINGS_STAGING_PATH=str(staging),
                RECORDINGS_UPLOAD_PATH=str(uploads),
            ),
            patch("notifications.tasks.send_push_to_user.delay") as mock_push,
        ):
            process_recording(recording.pk)

        mock_push.assert_called_once()
        call_kwargs = mock_push.call_args[1]
        assert call_kwargs["title"] == "Recording ready"
        assert call_kwargs["user_id"] == user.pk

    def test_creates_celery_interface_activity_row(self, user, tmp_path):
        from activity.models import Activity

        staging = tmp_path / "staging"
        uploads = tmp_path / "uploads"
        staging.mkdir()
        uploads.mkdir()
        recording = _make_pending_recording(user, staging)

        with (
            override_settings(
                RECORDINGS_STAGING_PATH=str(staging),
                RECORDINGS_UPLOAD_PATH=str(uploads),
            ),
            patch("notifications.tasks.send_push_to_user.delay"),
        ):
            process_recording(recording.pk)

        activity = Activity.objects.filter(
            verb="recordings.process",
            interface=Activity.Interface.CELERY,
            target_object_id=str(recording.pk),
        ).first()
        assert activity is not None, (
            "process_recording must open a with_system_activity scope so the task is visible in the audit timeline"
        )
        assert activity.actor is None
        assert activity.metadata == {
            "recording_id": recording.pk,
            "preserve_annotations": False,
        }

    def test_state_transitions_produce_chained_modify_rows(self, user, tmp_path):
        from activity.models import Activity, ObjectChangeLog

        staging = tmp_path / "staging"
        uploads = tmp_path / "uploads"
        staging.mkdir()
        uploads.mkdir()
        recording = _make_pending_recording(user, staging)

        with (
            override_settings(
                RECORDINGS_STAGING_PATH=str(staging),
                RECORDINGS_UPLOAD_PATH=str(uploads),
            ),
            patch("notifications.tasks.send_push_to_user.delay"),
        ):
            process_recording(recording.pk)

        activity = Activity.objects.get(verb="recordings.process", target_object_id=str(recording.pk))
        modify_rows = list(
            ObjectChangeLog.objects.filter(
                object_id=str(recording.pk),
                action=ObjectChangeLog.ACTION_MODIFY,
                activity=activity,
            ).order_by("created_at")
        )
        # At least two modify rows: PENDING → PROCESSING and the final
        # PROCESSING → READY transition. The `.bin` test fixture skips the
        # converter branch, so the file-metadata save() between them does
        # not run.
        assert len(modify_rows) >= 2
        # The final row carries the SignalInfo digest in extra_payload.
        final = modify_rows[-1]
        assert "signal_info_digest" in final.extra_payload
        assert isinstance(final.extra_payload["signal_info_digest"], str)
        assert len(final.extra_payload["signal_info_digest"]) == 64
        # The final row's diff must cover the actual state transition.
        # Regression guard: capturing before_state from the already-mutated
        # in-memory recording would leave file_path / status out of the
        # diff and silently misroute any rollback.
        changes = final.changes or {}
        assert "status" in changes, f"final transition row must record status change, got changes={changes}"
        # PROCESSING → READY: the first modify row already captured
        # PENDING → PROCESSING, so the persisted DB state immediately
        # before this final transition is PROCESSING.
        assert changes["status"]["from"] == "processing"
        assert changes["status"]["to"] == "ready"
        assert "content_hash" in changes
        assert changes["content_hash"]["from"] == ""

    def test_signal_info_digest_verifies_against_live_state(self, user, tmp_path):
        from activity.derived_state import verify_derived_state
        from activity.models import ObjectChangeLog

        staging = tmp_path / "staging"
        uploads = tmp_path / "uploads"
        staging.mkdir()
        uploads.mkdir()
        recording = _make_pending_recording(user, staging)

        with (
            override_settings(
                RECORDINGS_STAGING_PATH=str(staging),
                RECORDINGS_UPLOAD_PATH=str(uploads),
            ),
            patch("notifications.tasks.send_push_to_user.delay"),
        ):
            process_recording(recording.pk)

        final = (
            ObjectChangeLog.objects.filter(
                object_id=str(recording.pk),
                action=ObjectChangeLog.ACTION_MODIFY,
            )
            .order_by("-created_at")
            .first()
        )
        result = verify_derived_state(final)
        assert result.ok is True
        assert result.digests == {"signal_info_digest": "ok"}

    def test_signal_info_tamper_is_detected(self, user, tmp_path):
        """Tampering with SignalInfo (after process_recording committed)
        breaks the derived-state verification but not the chain.

        For a ``.bin`` fixture the recording has no RecordingMeta and no
        SignalInfo rows, so we synthesise one to model the tamper.
        """
        from django.contrib.contenttypes.models import ContentType

        from activity.derived_state import verify_derived_state
        from activity.models import ObjectChangeLog
        from recordings.models import RecordingMeta, SignalInfo

        staging = tmp_path / "staging"
        uploads = tmp_path / "uploads"
        staging.mkdir()
        uploads.mkdir()
        recording = _make_pending_recording(user, staging)

        with (
            override_settings(
                RECORDINGS_STAGING_PATH=str(staging),
                RECORDINGS_UPLOAD_PATH=str(uploads),
            ),
            patch("notifications.tasks.send_push_to_user.delay"),
        ):
            process_recording(recording.pk)

        # Create a SignalInfo row outside the audited scope to simulate
        # an attacker inserting derived rows after the audit row was
        # written.
        recording.refresh_from_db()
        recording_ct = ContentType.objects.get_for_model(Recording)
        meta = RecordingMeta.objects.create(
            content_type=recording_ct,
            object_id=str(recording.pk),
            format="edf",
            duration=0.0,
            data_record_count=0,
            data_record_duration=0.0,
            signal_count=0,
        )
        SignalInfo.objects.create(
            meta=meta,
            index=0,
            label="injected",
            physical_min=0.0,
            physical_max=1.0,
            digital_min=0,
            digital_max=1,
            units_per_bit=1.0,
            digital_offset=0.0,
            sample_count=0,
            sampling_rate=0.0,
        )

        final = (
            ObjectChangeLog.objects.filter(
                object_id=str(recording.pk),
                action=ObjectChangeLog.ACTION_MODIFY,
            )
            .order_by("-created_at")
            .first()
        )
        result = verify_derived_state(final)
        assert result.ok is False
        assert result.digests == {"signal_info_digest": "mismatch"}

    def test_canonical_label_excluded_from_signal_info_digest(self, user, tmp_path):
        """``canonical_label`` is a derived view of ``label`` and is excluded
        from the SignalInfo digest, so backfilling it can never raise a false
        tamper alarm. Mutating ``canonical_label`` must not change the digest;
        mutating the source ``label`` must.
        """
        from django.contrib.contenttypes.models import ContentType

        from recordings.audit_digests import compute_signal_info_digest
        from recordings.models import RecordingMeta, SignalInfo

        staging = tmp_path / "staging"
        staging.mkdir()
        recording = _make_pending_recording(user, staging)
        recording_ct = ContentType.objects.get_for_model(Recording)
        meta = RecordingMeta.objects.create(
            content_type=recording_ct,
            object_id=str(recording.pk),
            format="edf",
            duration=0.0,
            data_record_count=0,
            data_record_duration=0.0,
            signal_count=1,
        )
        signal = SignalInfo.objects.create(
            meta=meta,
            index=0,
            label="EEG T3-Ref",
            canonical_label="T7",
            physical_min=0.0,
            physical_max=1.0,
            digital_min=0,
            digital_max=1,
            units_per_bit=1.0,
            digital_offset=0.0,
            sample_count=0,
            sampling_rate=0.0,
        )
        baseline = compute_signal_info_digest(recording)

        # Mutating the derived label must NOT change the digest.
        signal.canonical_label = "P7-changed"
        signal.save(update_fields=["canonical_label"])
        assert compute_signal_info_digest(recording) == baseline

        # Mutating the source label MUST change it — the exclusion is narrow.
        signal.label = "EEG C3-Ref"
        signal.save(update_fields=["label"])
        assert compute_signal_info_digest(recording) != baseline


@pytest.mark.django_db
class TestPurgeDeletedRecordings:
    def test_hard_deletes_past_retention(self, user, tmp_path):
        f = tmp_path / "old.edf"
        f.write_bytes(b"data")
        recording = Recording.objects.create(
            author=user,
            original_name="old.edf",
            stored_name="OLDF1234OLDF1234OLDF1234OLDF1234.edf",
            file_extension=".edf",
            file_size=4,
            file_path=str(f),
            file_hash="x" * 64,
            content_hash="y" * 64,
            status=Recording.Status.READY,
            deleted_at=timezone.now() - timezone.timedelta(days=31),
        )
        with override_settings(RECORDINGS_TRASH_RETENTION_DAYS=30):
            result = purge_deleted_recordings()
        assert result["purged"] >= 1
        assert not Recording.objects.filter(pk=recording.pk).exists()
        assert not f.exists()

    def test_keeps_recently_deleted(self, user, tmp_path):
        f = tmp_path / "recent.edf"
        f.write_bytes(b"data")
        recording = Recording.objects.create(
            author=user,
            original_name="recent.edf",
            stored_name="RCNT1234RCNT1234RCNT1234RCNT1234.edf",
            file_extension=".edf",
            file_size=4,
            file_path=str(f),
            file_hash="x" * 64,
            content_hash="y" * 64,
            status=Recording.Status.READY,
            deleted_at=timezone.now() - timezone.timedelta(days=1),
        )
        with override_settings(RECORDINGS_TRASH_RETENTION_DAYS=30):
            purge_deleted_recordings()
        assert Recording.objects.filter(pk=recording.pk).exists()

    def test_missing_file_counted_as_error(self, user):
        recording = Recording.objects.create(
            author=user,
            original_name="gone.edf",
            stored_name="GONE1234GONE1234GONE1234GONE1234.edf",
            file_extension=".edf",
            file_size=4,
            file_path="/nonexistent/path.edf",
            file_hash="x" * 64,
            content_hash="y" * 64,
            status=Recording.Status.READY,
            deleted_at=timezone.now() - timezone.timedelta(days=31),
        )
        with override_settings(RECORDINGS_TRASH_RETENTION_DAYS=30):
            result = purge_deleted_recordings()
        # File doesn't exist → purged (file.exists() is False → unlink skipped, row still deleted)
        # The task skips unlink if file doesn't exist, then deletes the row
        assert Recording.objects.filter(pk=recording.pk).exists() is False
        assert result["purged"] >= 1

    def test_orphaned_pending_recording_is_purged(self, user, tmp_path):
        """Recordings stuck in PENDING status past the retention window are cleaned up."""
        f = tmp_path / "orphan.edf"
        f.write_bytes(b"data")
        recording = Recording.objects.create(
            author=user,
            original_name="orphan.edf",
            stored_name="ORPH1234ORPH1234ORPH1234ORPH1234.edf",
            file_extension=".edf",
            file_size=4,
            file_path=str(f),
            file_hash="x" * 64,
            content_hash="",
            status=Recording.Status.PENDING,
        )
        # Backdate the creation time past the retention cutoff
        Recording.objects.filter(pk=recording.pk).update(
            created_at=timezone.now() - timezone.timedelta(days=31),
        )
        with override_settings(RECORDINGS_TRASH_RETENTION_DAYS=30):
            result = purge_deleted_recordings()
        assert result["orphaned"] >= 1
        assert not Recording.objects.filter(pk=recording.pk).exists()

    def test_recent_pending_recording_is_not_purged(self, user, tmp_path):
        """Recordings in PENDING status within the retention window are left alone."""
        f = tmp_path / "recent_pending.edf"
        f.write_bytes(b"data")
        recording = Recording.objects.create(
            author=user,
            original_name="recent_pending.edf",
            stored_name="RCNP1234RCNP1234RCNP1234RCNP1234.edf",
            file_extension=".edf",
            file_size=4,
            file_path=str(f),
            file_hash="x" * 64,
            content_hash="",
            status=Recording.Status.PENDING,
        )
        with override_settings(RECORDINGS_TRASH_RETENTION_DAYS=30):
            purge_deleted_recordings()
        assert Recording.objects.filter(pk=recording.pk).exists()

    def test_creates_celery_activity_row(self, user):
        from activity.models import Activity

        with override_settings(RECORDINGS_TRASH_RETENTION_DAYS=30):
            purge_deleted_recordings()

        activity = (
            Activity.objects.filter(
                verb="recordings.purge",
                interface=Activity.Interface.CELERY,
            )
            .order_by("-created_at")
            .first()
        )
        assert activity is not None
        assert activity.actor is None
        assert activity.metadata == {"retention_days": 30}

    def test_purged_recording_produces_delete_audit_row(self, user, tmp_path):
        from activity.models import Activity, ObjectChangeLog

        f = tmp_path / "old.edf"
        f.write_bytes(b"data")
        recording = Recording.objects.create(
            author=user,
            original_name="old.edf",
            stored_name="OLDA1234OLDA1234OLDA1234OLDA1234.edf",
            file_extension=".edf",
            file_size=4,
            file_path=str(f),
            file_hash="x" * 64,
            content_hash="y" * 64,
            status=Recording.Status.READY,
            deleted_at=timezone.now() - timezone.timedelta(days=31),
        )
        rec_pk = recording.pk

        with override_settings(RECORDINGS_TRASH_RETENTION_DAYS=30):
            purge_deleted_recordings()

        delete_rows = list(
            ObjectChangeLog.objects.filter(
                object_id=str(rec_pk),
                action=ObjectChangeLog.ACTION_DELETE,
            )
        )
        assert len(delete_rows) == 1, (
            "purge_deleted_recordings must produce a DELETE audit row for each "
            "purged recording via the pre_delete signal inside the audited scope"
        )
        parent = Activity.objects.filter(verb="recordings.purge").latest("created_at")
        assert delete_rows[0].activity_id == parent.pk

    def test_orphan_purge_also_produces_delete_audit_row(self, user, tmp_path):
        from activity.models import Activity, ObjectChangeLog

        f = tmp_path / "orphan2.edf"
        f.write_bytes(b"data")
        recording = Recording.objects.create(
            author=user,
            original_name="orphan2.edf",
            stored_name="ORPB1234ORPB1234ORPB1234ORPB1234.edf",
            file_extension=".edf",
            file_size=4,
            file_path=str(f),
            file_hash="x" * 64,
            content_hash="",
            status=Recording.Status.PENDING,
        )
        rec_pk = recording.pk
        Recording.objects.filter(pk=rec_pk).update(
            created_at=timezone.now() - timezone.timedelta(days=31),
        )

        with override_settings(RECORDINGS_TRASH_RETENTION_DAYS=30):
            purge_deleted_recordings()

        delete_rows = list(
            ObjectChangeLog.objects.filter(
                object_id=str(rec_pk),
                action=ObjectChangeLog.ACTION_DELETE,
            )
        )
        assert len(delete_rows) == 1
        parent = Activity.objects.filter(verb="recordings.purge").latest("created_at")
        assert delete_rows[0].activity_id == parent.pk


def _make_recording(
    user,
    tmp_path,
    *,
    name="rec.edf",
    stored="STOR1234STOR1234STOR1234STOR1234.edf",
    status=Recording.Status.READY,
    deleted_at=None,
    content_hash="y" * 64,
):
    """Helper for the contract-test class — creates a recording row +
    on-disk file under tmp_path so unlink / exists() behaviour is real."""
    file_path = tmp_path / name
    file_path.write_bytes(b"data")
    return Recording.objects.create(
        author=user,
        original_name=name,
        stored_name=stored,
        file_extension=".edf",
        file_size=4,
        file_path=str(file_path),
        file_hash="x" * 64,
        content_hash=content_hash,
        status=status,
        deleted_at=deleted_at,
    )


@pytest.mark.django_db
class TestPurgeDeletedRecordingsContract:
    """Contract tests for the GDPR Art. 17 erasure pipeline.

    Pins the two filter shapes (soft-delete + orphan reaper), the
    file-unlink-vs-DB-delete ordering, the cutoff boundary, and the
    originals-volume non-interaction property. Each test corresponds
    to a documented invariant in the AGENTS.md load-bearing block on
    ``recordings/tasks.py``; widening any filter, swapping the ordering,
    or touching the originals volume should produce a red test here
    rather than silently shipping.
    """

    # ── Filter narrowing (the erasure must fire) ────────────────────────────

    def test_active_recording_never_purged_regardless_of_age(self, user, tmp_path):
        """A READY row with deleted_at=NULL is never reaped, even after
        the retention window has passed. Guards against a regression
        that drops the deleted_at__isnull=False clause."""
        rec = _make_recording(user, tmp_path, status=Recording.Status.READY)
        Recording.objects.filter(pk=rec.pk).update(
            created_at=timezone.now() - timezone.timedelta(days=365),
        )
        with override_settings(RECORDINGS_TRASH_RETENTION_DAYS=30):
            purge_deleted_recordings()
        assert Recording.objects.filter(pk=rec.pk).exists()
        assert Path(rec.file_path).exists()

    def test_failed_status_not_orphan_reaped(self, user, tmp_path):
        """An old FAILED recording is preserved by the orphan reaper.
        Failed processing left a file on disk the user can still
        download; widening status__in=[PENDING, PROCESSING] to include
        FAILED would silently delete it."""
        rec = _make_recording(user, tmp_path, status=Recording.Status.FAILED)
        Recording.objects.filter(pk=rec.pk).update(
            created_at=timezone.now() - timezone.timedelta(days=365),
        )
        with override_settings(RECORDINGS_TRASH_RETENTION_DAYS=30):
            purge_deleted_recordings()
        assert Recording.objects.filter(pk=rec.pk).exists()
        assert Path(rec.file_path).exists()

    def test_trashed_failed_recording_is_purged(self, user, tmp_path):
        """A FAILED recording that the user trashed is purged like a READY
        one once the retention window passes. Excluding FAILED from the
        trash branch left the preserved file (and its PHI-bearing
        original_name) outside every purge path indefinitely."""
        rec = _make_recording(
            user,
            tmp_path,
            status=Recording.Status.FAILED,
            deleted_at=timezone.now() - timezone.timedelta(days=45),
        )
        with override_settings(RECORDINGS_TRASH_RETENTION_DAYS=30):
            purge_deleted_recordings()
        assert not Recording.objects.filter(pk=rec.pk).exists()
        assert not Path(rec.file_path).exists()

    def test_recently_trashed_failed_recording_is_kept(self, user, tmp_path):
        """A trashed FAILED recording inside the retention window survives —
        the widened trash branch must still honour the cutoff."""
        rec = _make_recording(
            user,
            tmp_path,
            status=Recording.Status.FAILED,
            deleted_at=timezone.now() - timezone.timedelta(days=5),
        )
        with override_settings(RECORDINGS_TRASH_RETENTION_DAYS=30):
            purge_deleted_recordings()
        assert Recording.objects.filter(pk=rec.pk).exists()
        assert Path(rec.file_path).exists()

    def test_processing_orphan_purged_alongside_pending(self, user, tmp_path):
        """The orphan reaper catches both PENDING and PROCESSING rows
        past the cutoff. Asserting both values prevents an accidental
        narrowing to one or the other."""
        rec = _make_recording(user, tmp_path, status=Recording.Status.PROCESSING)
        Recording.objects.filter(pk=rec.pk).update(
            created_at=timezone.now() - timezone.timedelta(days=31),
        )
        with override_settings(RECORDINGS_TRASH_RETENTION_DAYS=30):
            result = purge_deleted_recordings()
        assert result["orphaned"] >= 1
        assert not Recording.objects.filter(pk=rec.pk).exists()
        assert not Path(rec.file_path).exists()

    # ── Cutoff boundary (sign + unit) ───────────────────────────────────────

    def test_cutoff_boundary_just_inside_window(self, user, tmp_path):
        """A recording trashed *just inside* the window (1 second
        younger than the cutoff) is preserved. Pins the cutoff sign so
        a regression that flips the comparison reaps recent data."""
        rec = _make_recording(
            user,
            tmp_path,
            deleted_at=timezone.now() - timezone.timedelta(days=30, seconds=-1),
        )
        with override_settings(RECORDINGS_TRASH_RETENTION_DAYS=30):
            purge_deleted_recordings()
        assert Recording.objects.filter(pk=rec.pk).exists()

    def test_cutoff_boundary_just_past_window(self, user, tmp_path):
        """A recording trashed *just past* the window (1 second older
        than the cutoff) is purged. Pins the cutoff sign + unit so a
        regression that overshoots leaves data past retention."""
        rec = _make_recording(
            user,
            tmp_path,
            deleted_at=timezone.now() - timezone.timedelta(days=30, seconds=2),
        )
        with override_settings(RECORDINGS_TRASH_RETENTION_DAYS=30):
            purge_deleted_recordings()
        assert not Recording.objects.filter(pk=rec.pk).exists()

    # ── File-unlink-vs-DB-delete atomicity ──────────────────────────────────

    def test_unlink_failure_preserves_db_row_soft_delete_path(self, user, tmp_path, monkeypatch):
        """Soft-delete path: if unlink raises, the DB row stays in
        place so the next run can retry. The half-state ``DB row gone
        but file present`` would leave PHI on the volume with no record
        in the database that it should be erased."""
        rec = _make_recording(
            user,
            tmp_path,
            deleted_at=timezone.now() - timezone.timedelta(days=31),
        )

        def raise_oserror(self):
            raise OSError("simulated I/O error")

        monkeypatch.setattr(Path, "unlink", raise_oserror)

        with override_settings(RECORDINGS_TRASH_RETENTION_DAYS=30):
            result = purge_deleted_recordings()

        assert Recording.objects.filter(pk=rec.pk).exists()
        assert Path(rec.file_path).exists()
        assert result["errors"] >= 1
        assert result["purged"] == 0

    def test_unlink_failure_preserves_db_row_orphan_path(self, user, tmp_path, monkeypatch):
        """Orphan path: same atomicity invariant as the soft-delete
        path. Regression guard for the pre-Phase-2 bug where the
        orphan branch swallowed OSError and deleted the DB row anyway,
        leaving the file orphaned on disk with nothing in the database
        to remember it."""
        rec = _make_recording(user, tmp_path, status=Recording.Status.PENDING)
        Recording.objects.filter(pk=rec.pk).update(
            created_at=timezone.now() - timezone.timedelta(days=31),
        )

        def raise_oserror(self):
            raise OSError("simulated I/O error")

        monkeypatch.setattr(Path, "unlink", raise_oserror)

        with override_settings(RECORDINGS_TRASH_RETENTION_DAYS=30):
            result = purge_deleted_recordings()

        assert Recording.objects.filter(pk=rec.pk).exists()
        assert Path(rec.file_path).exists()
        assert result["errors"] >= 1
        assert result["orphaned"] == 0

    # ── Originals volume non-interaction ────────────────────────────────────

    def test_originals_volume_is_not_touched(self, user, tmp_path):
        """The host-controlled originals volume is the operator's
        regulatory backstop. ``purge_deleted_recordings`` must never
        read or write under it — see AGENTS.md → *Originals
        preservation volume is strictly write-only*. This test sets
        the originals path to a tmp dir, drops a marker file, runs
        the purge, and asserts the marker survives."""
        originals = tmp_path / "originals"
        originals.mkdir()
        marker = originals / "MANIFEST.do_not_touch"
        marker.write_bytes(b"operator regulatory backstop")

        rec = _make_recording(
            user,
            tmp_path,
            deleted_at=timezone.now() - timezone.timedelta(days=31),
        )

        with override_settings(
            RECORDINGS_TRASH_RETENTION_DAYS=30,
            RECORDINGS_ORIGINALS_PATH=str(originals),
        ):
            purge_deleted_recordings()

        assert not Recording.objects.filter(pk=rec.pk).exists()
        assert marker.exists(), (
            "purge_deleted_recordings must never touch the originals volume — see AGENTS.md load-bearing block"
        )
        assert marker.read_bytes() == b"operator regulatory backstop"
