"""Tests for recordings.pipelines and the import_recordings management command."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings

from recordings.models import ImportJob, ImportJobFile, Recording
from recordings.pipelines import (
    HeaderPipelineOptions,
    RecordingPipeline,
    SignalPipelineOptions,
    get_pipeline,
)
from recordings.testing import make_edf_bytes

# ---------------------------------------------------------------------------
# TestGetPipeline
# ---------------------------------------------------------------------------


class TestGetPipeline:
    def test_builtin_web_returns_pipeline(self):
        p = get_pipeline("web")
        assert isinstance(p, RecordingPipeline)
        assert isinstance(p.header, HeaderPipelineOptions)
        assert isinstance(p.signals, SignalPipelineOptions)

    def test_builtin_import_returns_pipeline(self):
        p = get_pipeline("import")
        assert isinstance(p, RecordingPipeline)

    def test_defaults_strip_annotation_text(self):
        assert get_pipeline("web").header.strip_annotation_text is True
        assert get_pipeline("import").header.strip_annotation_text is True

    def test_settings_override_builtin(self):
        with override_settings(RECORDING_PIPELINES={"web": {"header": {"strip_annotation_text": True}}}):
            p = get_pipeline("web")
        assert p.header.strip_annotation_text is True

    def test_settings_add_new_label(self):
        with override_settings(RECORDING_PIPELINES={"custom": {"header": {"strip_annotation_text": True}}}):
            p = get_pipeline("custom")
        assert p.header.strip_annotation_text is True

    def test_unknown_label_raises(self):
        with override_settings(RECORDING_PIPELINES={}):
            with pytest.raises(ValueError, match="Unknown recording pipeline label"):
                get_pipeline("nonexistent")

    def test_instance_passthrough(self):
        instance = RecordingPipeline(header=HeaderPipelineOptions(strip_annotation_text=True))
        with override_settings(RECORDING_PIPELINES={"direct": instance}):
            p = get_pipeline("direct")
        assert p is instance

    def test_dotted_path_instance(self, tmp_path, monkeypatch):
        """A dotted path pointing to a RecordingPipeline instance is returned directly."""
        import sys
        import types

        mod = types.ModuleType("_test_pipelines_mod")
        mod.my_pipeline = RecordingPipeline(header=HeaderPipelineOptions(strip_annotation_text=True))
        monkeypatch.setitem(sys.modules, "_test_pipelines_mod", mod)

        with override_settings(RECORDING_PIPELINES={"dotted": "_test_pipelines_mod.my_pipeline"}):
            p = get_pipeline("dotted")
        assert p.header.strip_annotation_text is True

    def test_dotted_path_factory(self, monkeypatch):
        """A dotted path to a callable (factory) is called to produce the pipeline."""
        import sys
        import types

        def factory():
            return RecordingPipeline(header=HeaderPipelineOptions(strip_annotation_text=True))

        mod = types.ModuleType("_test_factory_mod")
        mod.make = factory
        monkeypatch.setitem(sys.modules, "_test_factory_mod", mod)

        with override_settings(RECORDING_PIPELINES={"factory": "_test_factory_mod.make"}):
            p = get_pipeline("factory")
        assert p.header.strip_annotation_text is True


# ---------------------------------------------------------------------------
# Helpers for import command tests
# ---------------------------------------------------------------------------


def _write_edf(path: Path, n_channels: int = 1, n_records: int = 1) -> Path:
    path.write_bytes(make_edf_bytes(n_channels, n_records))
    return path


def _call_import(source_path, username, **extra):
    """Invoke import_recordings via call_command, routing stdout to a string."""
    from io import StringIO

    out = StringIO()
    call_command(
        "import_recordings",
        str(source_path),
        username=username,
        stdout=out,
        stderr=out,
        **extra,
    )
    return out.getvalue()


# ---------------------------------------------------------------------------
# TestImportRecordingsCommand
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestImportRecordingsCommand:
    # ── Basic import ──────────────────────────────────────────────────────────

    def test_imports_single_file(self, user, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        _write_edf(src / "rec.edf")

        with override_settings(RECORDINGS_UPLOAD_PATH=str(tmp_path / "uploads")):
            _call_import(src, user.username, structure="flat")

        assert Recording.objects.filter(author=user, original_name="rec.edf").exists()

    def test_recording_is_ready(self, user, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        _write_edf(src / "rec.edf")

        with override_settings(RECORDINGS_UPLOAD_PATH=str(tmp_path / "uploads")):
            _call_import(src, user.username, structure="flat")

        rec = Recording.objects.get(author=user)
        assert rec.status == Recording.Status.READY
        assert rec.content_hash != ""

    def test_access_right_created(self, user, tmp_path):
        from epicurrents.models import AccessRight

        src = tmp_path / "src"
        src.mkdir()
        _write_edf(src / "rec.edf")

        with override_settings(RECORDINGS_UPLOAD_PATH=str(tmp_path / "uploads")):
            _call_import(src, user.username, structure="flat")

        rec = Recording.objects.get(author=user)
        assert (
            AccessRight.objects.filter(access_giver=user, access_target=user, can_read=True, can_write=True)
            .for_object(rec)
            .exists()
        )

    def test_job_completed(self, user, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        _write_edf(src / "rec.edf")

        with override_settings(RECORDINGS_UPLOAD_PATH=str(tmp_path / "uploads")):
            _call_import(src, user.username, structure="flat")

        job = ImportJob.objects.get(owner=user)
        assert job.status == ImportJob.Status.COMPLETED
        assert job.completed_at is not None

    def test_job_file_marked_done(self, user, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        _write_edf(src / "rec.edf")

        with override_settings(RECORDINGS_UPLOAD_PATH=str(tmp_path / "uploads")):
            _call_import(src, user.username, structure="flat")

        jf = ImportJobFile.objects.get(job__owner=user)
        assert jf.status == ImportJobFile.Status.DONE
        assert jf.recording is not None

    def test_multiple_files(self, user, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        for i in range(3):
            _write_edf(src / f"rec{i}.edf")

        with override_settings(RECORDINGS_UPLOAD_PATH=str(tmp_path / "uploads")):
            _call_import(src, user.username, structure="flat")

        assert Recording.objects.filter(author=user).count() == 3

    def test_bdf_extension_accepted(self, user, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        _write_edf(src / "rec.bdf")

        with override_settings(RECORDINGS_UPLOAD_PATH=str(tmp_path / "uploads")):
            _call_import(src, user.username, structure="flat")

        assert Recording.objects.filter(author=user, file_extension=".bdf").exists()

    # ── Sidecar JSON ──────────────────────────────────────────────────────────

    def test_sidecar_creates_import_annotation(self, user, tmp_path):
        from annotations.models import Annotation

        src = tmp_path / "src"
        src.mkdir()
        _write_edf(src / "rec.edf")
        sidecar = {"events": [{"onset": 10.0, "duration": 1.0, "label": "Spike"}]}
        (src / "rec.edf.json").write_text(json.dumps(sidecar), encoding="utf-8")

        with override_settings(RECORDINGS_UPLOAD_PATH=str(tmp_path / "uploads")):
            _call_import(src, user.username, structure="flat")

        rec = Recording.objects.get(author=user)
        anno = Annotation.objects.get(target_object_id=str(rec.pk), name="Import annotations")
        assert anno.content == sidecar

    def test_missing_sidecar_is_ignored(self, user, tmp_path):
        """Import succeeds without a sidecar — no annotation created."""
        from annotations.models import Annotation

        src = tmp_path / "src"
        src.mkdir()
        _write_edf(src / "rec.edf")

        with override_settings(RECORDINGS_UPLOAD_PATH=str(tmp_path / "uploads")):
            _call_import(src, user.username, structure="flat")

        rec = Recording.objects.get(author=user)
        assert not Annotation.objects.filter(target_object_id=str(rec.pk), name="Import annotations").exists()

    def test_malformed_sidecar_does_not_abort(self, user, tmp_path):
        """A sidecar that cannot be parsed is logged and skipped; the file still imports."""
        src = tmp_path / "src"
        src.mkdir()
        _write_edf(src / "rec.edf")
        (src / "rec.edf.json").write_text("not valid json {{{", encoding="utf-8")

        with override_settings(RECORDINGS_UPLOAD_PATH=str(tmp_path / "uploads")):
            _call_import(src, user.username, structure="flat")

        assert Recording.objects.filter(author=user).exists()

    # ── Collection creation ───────────────────────────────────────────────────

    def test_recursive_creates_collections(self, user, tmp_path):
        from library.models import Collection, CollectionItem

        src = tmp_path / "src"
        sub = src / "study1" / "patient1"
        sub.mkdir(parents=True)
        _write_edf(sub / "rec.edf")

        with override_settings(RECORDINGS_UPLOAD_PATH=str(tmp_path / "uploads")):
            _call_import(src, user.username, structure="recursive")

        assert Collection.objects.filter(author=user, name="study1").exists()
        assert Collection.objects.filter(author=user, name="patient1").exists()

        patient1 = Collection.objects.get(author=user, name="patient1")
        rec = Recording.objects.get(author=user)
        from django.contrib.contenttypes.models import ContentType

        ct = ContentType.objects.get_for_model(rec, for_concrete_model=False)
        assert CollectionItem.objects.filter(collection=patient1, content_type=ct, object_id=str(rec.pk)).exists()

    def test_recursive_collection_parent_chain(self, user, tmp_path):
        from library.models import Collection

        src = tmp_path / "src"
        (src / "a" / "b").mkdir(parents=True)
        _write_edf(src / "a" / "b" / "rec.edf")

        with override_settings(RECORDINGS_UPLOAD_PATH=str(tmp_path / "uploads")):
            _call_import(src, user.username, structure="recursive")

        a = Collection.objects.get(author=user, name="a")
        b = Collection.objects.get(author=user, name="b")
        assert b.parent == a

    def test_recursive_flat_does_not_create_collections(self, user, tmp_path):
        from library.models import Collection

        src = tmp_path / "src"
        (src / "sub").mkdir(parents=True)
        _write_edf(src / "sub" / "rec.edf")

        with override_settings(RECORDINGS_UPLOAD_PATH=str(tmp_path / "uploads")):
            _call_import(src, user.username, structure="recursive-flat")

        assert not Collection.objects.filter(author=user).exists()
        assert Recording.objects.filter(author=user).exists()

    def test_flat_ignores_subdirectory_files(self, user, tmp_path):
        src = tmp_path / "src"
        sub = src / "sub"
        sub.mkdir(parents=True)
        _write_edf(src / "root.edf")
        _write_edf(sub / "nested.edf")

        with override_settings(RECORDINGS_UPLOAD_PATH=str(tmp_path / "uploads")):
            _call_import(src, user.username, structure="flat")

        assert Recording.objects.filter(author=user).count() == 1
        assert Recording.objects.filter(author=user, original_name="root.edf").exists()

    def test_file_in_source_root_not_added_to_collection(self, user, tmp_path):
        """Files directly in source_path are not added to any collection."""
        from library.models import CollectionItem

        src = tmp_path / "src"
        src.mkdir()
        _write_edf(src / "rec.edf")

        with override_settings(RECORDINGS_UPLOAD_PATH=str(tmp_path / "uploads")):
            _call_import(src, user.username, structure="recursive")

        assert not CollectionItem.objects.filter(collection__author=user).exists()

    # ── Resume / discard ──────────────────────────────────────────────────────

    def test_requires_resume_or_discard_if_job_in_progress(self, user, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        ImportJob.objects.create(
            owner=user,
            source_path=str(src),
            status=ImportJob.Status.IN_PROGRESS,
        )
        with pytest.raises(CommandError, match="--resume"):
            _call_import(src, user.username, structure="flat")

    def test_resume_and_discard_mutually_exclusive(self, user, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        with pytest.raises(CommandError, match="mutually exclusive"):
            _call_import(src, user.username, structure="flat", resume=True, discard=True)

    def test_discard_aborts_existing_job(self, user, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        old_job = ImportJob.objects.create(
            owner=user,
            source_path=str(src),
            status=ImportJob.Status.IN_PROGRESS,
        )

        with override_settings(RECORDINGS_UPLOAD_PATH=str(tmp_path / "uploads")):
            _call_import(src, user.username, structure="flat", discard=True)

        old_job.refresh_from_db()
        assert old_job.status == ImportJob.Status.ABORTED
        # A new completed job was created.
        new_job = ImportJob.objects.get(status=ImportJob.Status.COMPLETED)
        assert new_job.pk != old_job.pk

    def test_resume_continues_job(self, user, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        _write_edf(src / "a.edf")
        _write_edf(src / "b.edf")

        uploads = tmp_path / "uploads"
        with override_settings(RECORDINGS_UPLOAD_PATH=str(uploads)):
            # First run — process only one file by pre-marking the other done.
            job = ImportJob.objects.create(
                owner=user,
                source_path=str(src),
                status=ImportJob.Status.IN_PROGRESS,
                structure=ImportJob.Structure.FLAT,
            )
            # Pre-populate both files; mark a.edf as already done.
            jf_a = ImportJobFile.objects.create(job=job, relative_path="a.edf", status=ImportJobFile.Status.DONE)
            # Resume without --reprocess → a.edf is skipped.
            _call_import(src, user.username, structure="flat", resume=True)

        job.refresh_from_db()
        assert job.status == ImportJob.Status.COMPLETED
        # b.edf was processed; a.edf was skipped.
        assert ImportJobFile.objects.filter(job=job, relative_path="b.edf", status=ImportJobFile.Status.DONE).exists()
        jf_a.refresh_from_db()
        assert jf_a.status == ImportJobFile.Status.DONE  # still done, not re-processed

    def test_reprocess_reruns_done_files(self, user, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        _write_edf(src / "rec.edf")

        uploads = tmp_path / "uploads"
        with override_settings(RECORDINGS_UPLOAD_PATH=str(uploads)):
            # Simulate a job where rec.edf is pre-marked done with no recording.
            job = ImportJob.objects.create(
                owner=user,
                source_path=str(src),
                status=ImportJob.Status.IN_PROGRESS,
                structure=ImportJob.Structure.FLAT,
            )
            ImportJobFile.objects.create(job=job, relative_path="rec.edf", status=ImportJobFile.Status.DONE)
            _call_import(src, user.username, structure="flat", resume=True, reprocess=True)

        # File was re-processed — a Recording now exists.
        assert Recording.objects.filter(author=user, original_name="rec.edf").exists()

    # ── Error handling ────────────────────────────────────────────────────────

    def test_unknown_user_raises(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        with pytest.raises(CommandError, match="User not found"):
            _call_import(src, "nobody_here", structure="flat")

    def test_unknown_pipeline_raises(self, user, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        with pytest.raises(CommandError, match="Unknown recording pipeline label"):
            _call_import(src, user.username, pipeline="nonexistent", structure="flat")

    def test_nonexistent_source_raises(self, user, tmp_path):
        with pytest.raises(CommandError, match="does not exist"):
            _call_import(tmp_path / "nowhere", user.username, structure="flat")

    def test_missing_file_at_import_time_marks_failed(self, user, tmp_path):
        src = tmp_path / "src"
        src.mkdir()

        uploads = tmp_path / "uploads"
        with override_settings(RECORDINGS_UPLOAD_PATH=str(uploads)):
            job = ImportJob.objects.create(
                owner=user,
                source_path=str(src),
                status=ImportJob.Status.IN_PROGRESS,
                structure=ImportJob.Structure.FLAT,
            )
            # Register a file that does not actually exist on disk.
            ImportJobFile.objects.create(job=job, relative_path="ghost.edf")
            _call_import(src, user.username, structure="flat", resume=True)

        jf = ImportJobFile.objects.get(job=job, relative_path="ghost.edf")
        assert jf.status == ImportJobFile.Status.FAILED
        assert "not found" in jf.error

    def test_invalid_edf_marks_failed(self, user, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        # Write a file with the right extension but garbage content.
        (src / "bad.edf").write_bytes(b"\x00" * 10)

        with override_settings(RECORDINGS_UPLOAD_PATH=str(tmp_path / "uploads")):
            _call_import(src, user.username, structure="flat")

        jf = ImportJobFile.objects.get(job__owner=user, relative_path="bad.edf")
        assert jf.status == ImportJobFile.Status.FAILED
        assert jf.error != ""
        # No Recording row should remain for a failed import.
        assert not Recording.objects.filter(author=user).exists()

    # ── Pipeline option wired through ─────────────────────────────────────────

    def test_pipeline_label_stored_on_job(self, user, tmp_path):
        src = tmp_path / "src"
        src.mkdir()

        with override_settings(RECORDINGS_UPLOAD_PATH=str(tmp_path / "uploads")):
            _call_import(src, user.username, pipeline="import", structure="flat")

        job = ImportJob.objects.get(owner=user)
        assert job.pipeline_label == "import"

    def test_custom_pipeline_override(self, user, tmp_path):
        """strip_annotation_text=True in the pipeline is honoured (file is still imported)."""
        src = tmp_path / "src"
        src.mkdir()
        _write_edf(src / "rec.edf")

        with override_settings(
            RECORDINGS_UPLOAD_PATH=str(tmp_path / "uploads"),
            RECORDING_PIPELINES={"strip": {"header": {"strip_annotation_text": True}}},
        ):
            _call_import(src, user.username, pipeline="strip", structure="flat")

        assert Recording.objects.filter(author=user).exists()


@pytest.mark.django_db
class TestImportRecordingsAudit:
    """Audit-trail shape of the `import_recordings` command.

    Covers the `with_system_activity` scope, the per-recording CREATE
    auto-attribution via signals, and the per-recording final MODIFY
    row carrying the SignalInfo digest in extra_payload.
    """

    def test_command_creates_command_interface_activity_row(self, user, tmp_path):
        from activity.models import Activity

        src = tmp_path / "src"
        src.mkdir()
        _write_edf(src / "rec.edf")

        with override_settings(RECORDINGS_UPLOAD_PATH=str(tmp_path / "uploads")):
            _call_import(src, user.username, structure="flat")

        activity = (
            Activity.objects.filter(
                verb="recordings.import",
                interface=Activity.Interface.COMMAND,
            )
            .order_by("-created_at")
            .first()
        )
        assert activity is not None
        assert activity.actor is None
        # By primary key, not by name. This asserted `owner_username` until the
        # 2026-08-26 GDPR audit found that an Activity row targeting the job is
        # reachable by no erasure path, so the username it pinned here survived
        # the account forever. See recordings/tests/test_import_audit_hygiene.py.
        assert activity.metadata["owner_id"] == user.pk
        assert "owner_username" not in activity.metadata
        assert activity.metadata["structure"] == "flat"
        assert activity.metadata["reprocess"] is False

    def test_recording_create_and_modify_rows_attribute_to_parent(self, user, tmp_path):
        from django.contrib.contenttypes.models import ContentType

        from activity.models import Activity, ObjectChangeLog

        src = tmp_path / "src"
        src.mkdir()
        _write_edf(src / "rec.edf")

        with override_settings(RECORDINGS_UPLOAD_PATH=str(tmp_path / "uploads")):
            _call_import(src, user.username, structure="flat")

        recording = Recording.objects.get(author=user)
        recording_ct = ContentType.objects.get_for_model(Recording)
        activity = Activity.objects.get(verb="recordings.import", interface=Activity.Interface.COMMAND)
        create_rows = list(
            ObjectChangeLog.objects.filter(
                content_type=recording_ct,
                object_id=str(recording.pk),
                action=ObjectChangeLog.ACTION_CREATE,
            )
        )
        modify_rows = list(
            ObjectChangeLog.objects.filter(
                content_type=recording_ct,
                object_id=str(recording.pk),
                action=ObjectChangeLog.ACTION_MODIFY,
            ).order_by("created_at")
        )
        assert len(create_rows) == 1
        assert create_rows[0].activity_id == activity.pk
        # At least the explicit PROCESSING → READY transition with digest.
        assert len(modify_rows) >= 1
        assert modify_rows[-1].activity_id == activity.pk
        assert "signal_info_digest" in modify_rows[-1].extra_payload

    def test_signal_info_digest_verifies_against_live_state(self, user, tmp_path):
        from django.contrib.contenttypes.models import ContentType

        from activity.derived_state import verify_derived_state
        from activity.models import ObjectChangeLog

        src = tmp_path / "src"
        src.mkdir()
        _write_edf(src / "rec.edf")

        with override_settings(RECORDINGS_UPLOAD_PATH=str(tmp_path / "uploads")):
            _call_import(src, user.username, structure="flat")

        recording = Recording.objects.get(author=user)
        recording_ct = ContentType.objects.get_for_model(Recording)
        final = (
            ObjectChangeLog.objects.filter(
                content_type=recording_ct,
                object_id=str(recording.pk),
                action=ObjectChangeLog.ACTION_MODIFY,
            )
            .order_by("-created_at")
            .first()
        )
        result = verify_derived_state(final)
        assert result.ok is True
        assert result.digests == {"signal_info_digest": "ok"}


@pytest.mark.django_db
class TestImportJobSingleInProgressConstraint:
    """The one-job-at-a-time invariant is schema-enforced, not just command-enforced."""

    def test_second_in_progress_job_violates_the_constraint(self, user):
        from django.db import IntegrityError

        from recordings.models import ImportJob

        ImportJob.objects.create(owner=user, source_path="/a", status=ImportJob.Status.IN_PROGRESS)
        with pytest.raises(IntegrityError):
            ImportJob.objects.create(owner=user, source_path="/b", status=ImportJob.Status.IN_PROGRESS)

    def test_finished_jobs_do_not_block_a_new_one(self, user):
        from recordings.models import ImportJob

        ImportJob.objects.create(owner=user, source_path="/a", status=ImportJob.Status.COMPLETED)
        ImportJob.objects.create(owner=user, source_path="/b", status=ImportJob.Status.ABORTED)
        job = ImportJob.objects.create(owner=user, source_path="/c", status=ImportJob.Status.IN_PROGRESS)
        assert job.pk is not None
