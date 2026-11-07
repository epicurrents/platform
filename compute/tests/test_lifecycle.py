"""The post-ingest pipeline end to end: ingest, reconstruction into the artifact cache, derive jobs, and the status.

Driven through ``process_recording`` with on-commit callbacks executed, as the worker runs them, so each test sees the
dispatch the ingest actually performs. Stages and jobs are invented here; the platform registers none.
"""

import hashlib
import itertools
from pathlib import Path
from unittest.mock import patch

import pytest
from django.contrib.contenttypes.models import ContentType
from django.test import override_settings

from activity.models import ObjectChangeLog
from compute import derive
from compute.lifecycle import PROVIDER, version_path
from compute.models import ArtifactCacheEntry, DeriveJob, RecordingJob
from recordings import lifecycle
from recordings.models import Recording
from recordings.pipeline import SOURCE_VERSION_ID, Manifest, Phase, registry, stage_application
from recordings.tasks import process_recording
from recordings.testing import make_edf_bytes

_stems = itertools.count(1)


class _XorStage:
    """Flips every signal byte: a deterministic, visible change that keeps the file the same size."""

    phase = Phase.RECONSTRUCT
    requires: tuple[str, ...] = ()
    code_version = "1"
    reproducible = True

    def __init__(self, name="xq_flip", order_hint=10, enabled_by_default=True, fail=False, requires=()):
        self.name = name
        self.order_hint = order_hint
        self.enabled_by_default = enabled_by_default
        self.requires = requires
        self._fail = fail

    def params(self):
        return {}

    def transform(self, header, signals):
        if self._fail:
            raise ValueError("the stage refused this input")
        return header, bytes(b ^ 0x01 for b in signals)


class _RecordingJob:
    """A derive job that records what it read."""

    code_version = "1"

    def __init__(self, name="xq_count"):
        self.name = name
        self.calls = []

    def applies_to(self, recording):
        return True

    def run(self, recording, signal_path, version_id):
        self.calls.append((Path(signal_path).read_bytes(), version_id))


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path, settings):
    """Empty stage and job registries, this app's provider alone, and a cache under the test's directory."""
    monkeypatch.setattr(registry, "_STAGES", {})
    monkeypatch.setattr(derive, "_JOBS", {})
    monkeypatch.setattr(lifecycle, "_PROVIDERS", [PROVIDER])
    settings.COMPUTE_ARTIFACT_CACHE_PATH = str(tmp_path / "cache")


def _ingest(user, tmp_path, django_capture_on_commit_callbacks, *, run_dispatch=True):
    content = make_edf_bytes(n_channels=2, n_records=2)
    stem = f"{next(_stems):032X}"
    staging, uploads = tmp_path / "staging", tmp_path / "uploads"
    staging.mkdir(exist_ok=True)
    uploads.mkdir(exist_ok=True)
    (staging / f"{stem}.edf").write_bytes(content)
    recording = Recording.objects.create(
        author=user,
        original_name="export.edf",
        stored_name=f"{stem}.edf",
        file_extension=".edf",
        file_size=len(content),
        file_path=str(staging / f"{stem}.edf"),
        file_hash=hashlib.sha256(content).hexdigest(),
        status=Recording.Status.PENDING,
    )
    with (
        override_settings(RECORDINGS_STAGING_PATH=str(staging), RECORDINGS_UPLOAD_PATH=str(uploads)),
        patch("notifications.tasks.send_push_to_user.delay") as push,
        django_capture_on_commit_callbacks(execute=run_dispatch),
    ):
        process_recording(recording.pk)
    recording.refresh_from_db()
    return recording, push


def _signal_offset(data: bytes) -> int:
    from recordings.processors.edf import parse_edf_header

    return parse_edf_header(data).header_record_bytes


@pytest.mark.django_db
class TestNoWork:
    def test_a_recording_with_nothing_to_run_is_ready_at_the_end_of_ingest(
        self, user, tmp_path, django_capture_on_commit_callbacks
    ):
        recording, push = _ingest(user, tmp_path, django_capture_on_commit_callbacks)
        assert recording.status == Recording.Status.READY
        assert recording.ingested_at is not None
        assert not RecordingJob.objects.filter(recording=recording).exists()
        assert push.call_args.kwargs["title"] == "Recording ready"


@pytest.mark.django_db
class TestStatusAtTheEndOfIngest:
    def test_an_outstanding_stage_holds_the_recording_in_processing(
        self, user, tmp_path, django_capture_on_commit_callbacks
    ):
        registry.register_reconstruction_stage(_XorStage())
        recording, push = _ingest(user, tmp_path, django_capture_on_commit_callbacks, run_dispatch=False)
        assert recording.status == Recording.Status.PROCESSING
        assert recording.ingested_at is not None
        assert not push.called

    def test_derive_jobs_alone_make_the_recording_available(self, user, tmp_path, django_capture_on_commit_callbacks):
        derive.register_derive_job(_RecordingJob())
        recording, _push = _ingest(user, tmp_path, django_capture_on_commit_callbacks, run_dispatch=False)
        assert recording.status == Recording.Status.AVAILABLE

    def test_a_provider_that_cannot_plan_does_not_fail_the_ingest(
        self, user, tmp_path, django_capture_on_commit_callbacks
    ):
        with patch.object(PROVIDER, "plan", side_effect=RuntimeError("broken registry")):
            recording, _push = _ingest(user, tmp_path, django_capture_on_commit_callbacks)
        assert recording.status == Recording.Status.READY
        assert Path(recording.file_path).exists()


@pytest.mark.django_db
class TestReconstruction:
    def test_the_default_version_is_built_into_the_cache_and_the_source_is_untouched(
        self, user, tmp_path, django_capture_on_commit_callbacks
    ):
        stage = _XorStage()
        registry.register_reconstruction_stage(stage)
        recording, push = _ingest(user, tmp_path, django_capture_on_commit_callbacks)

        source = Path(recording.file_path).read_bytes()
        assert hashlib.sha256(source).hexdigest() == recording.stored_hash
        manifest = Manifest(recording.stored_hash).with_stage(stage_application(stage))
        (job,) = RecordingJob.objects.filter(recording=recording)
        assert job.state == RecordingJob.State.DONE
        assert job.output_version_id == manifest.version_id
        assert job.input_digest == recording.stored_hash

        entry = ArtifactCacheEntry.objects.get(recording=recording, version_id=manifest.version_id)
        built = Path(entry.storage_path).read_bytes()
        offset = _signal_offset(source)
        assert built[:offset] == source[:offset]
        assert built[offset:] == bytes(b ^ 0x01 for b in source[offset:])
        assert recording.status == Recording.Status.READY
        assert push.call_args.kwargs["title"] == "Recording ready"

    def test_the_version_is_created_in_the_audit_trail_with_its_manifest(
        self, user, tmp_path, django_capture_on_commit_callbacks
    ):
        registry.register_reconstruction_stage(_XorStage())
        recording, _push = _ingest(user, tmp_path, django_capture_on_commit_callbacks)
        entry = ArtifactCacheEntry.objects.get(recording=recording)
        row = ObjectChangeLog.objects.get(
            content_type=ContentType.objects.get_for_model(ArtifactCacheEntry),
            object_id=str(entry.pk),
            action=ObjectChangeLog.ACTION_CREATE,
        )
        assert row.extra_payload["version_id"] == entry.version_id
        assert row.extra_payload["version_manifest"]["source_hash"] == recording.stored_hash
        assert [s["stage"] for s in row.extra_payload["version_manifest"]["stages"]] == ["xq_flip"]

    def test_stages_run_in_order_each_on_the_last_ones_output(self, user, tmp_path, django_capture_on_commit_callbacks):
        registry.register_reconstruction_stage(_XorStage("xq_first", order_hint=10))
        registry.register_reconstruction_stage(_XorStage("xq_second", order_hint=20, requires=("xq_first",)))
        recording, _push = _ingest(user, tmp_path, django_capture_on_commit_callbacks)
        first, second = RecordingJob.objects.filter(recording=recording).order_by("order")
        assert (first.stage_name, second.stage_name) == ("xq_first", "xq_second")
        assert second.input_digest != first.input_digest
        # Two flips cancel out, which is how the test sees that the second stage read the first one's output.
        entry = ArtifactCacheEntry.objects.get(recording=recording)
        assert Path(entry.storage_path).read_bytes() == Path(recording.file_path).read_bytes()

    def test_a_failed_stage_settles_the_recording_without_failing_it(
        self, user, tmp_path, django_capture_on_commit_callbacks
    ):
        registry.register_reconstruction_stage(_XorStage("xq_first", order_hint=10, fail=True))
        registry.register_reconstruction_stage(_XorStage("xq_second", order_hint=20))
        job = _RecordingJob()
        derive.register_derive_job(job)
        recording, _push = _ingest(user, tmp_path, django_capture_on_commit_callbacks)

        states = dict(RecordingJob.objects.filter(recording=recording).values_list("stage_name", "state"))
        assert states == {"xq_first": RecordingJob.State.FAILED, "xq_second": RecordingJob.State.SKIPPED}
        assert "refused this input" in RecordingJob.objects.get(recording=recording, stage_name="xq_first").error
        assert DeriveJob.objects.get(recording=recording).state == DeriveJob.State.SKIPPED
        assert job.calls == []
        assert not ArtifactCacheEntry.objects.filter(recording=recording).exists()
        assert recording.status == Recording.Status.READY

    def test_a_disabled_stage_is_skipped_and_the_default_version_is_the_source(
        self, user, tmp_path, django_capture_on_commit_callbacks
    ):
        registry.register_reconstruction_stage(_XorStage(enabled_by_default=False))
        job = _RecordingJob()
        derive.register_derive_job(job)
        recording, _push = _ingest(user, tmp_path, django_capture_on_commit_callbacks)
        assert RecordingJob.objects.get(recording=recording).state == RecordingJob.State.SKIPPED
        assert job.calls == [(Path(recording.file_path).read_bytes(), SOURCE_VERSION_ID)]
        assert recording.status == Recording.Status.READY


@pytest.mark.django_db
class TestDerive:
    def test_a_derive_job_reads_the_default_version(self, user, tmp_path, django_capture_on_commit_callbacks):
        stage = _XorStage()
        registry.register_reconstruction_stage(stage)
        job = _RecordingJob()
        derive.register_derive_job(job)
        recording, _push = _ingest(user, tmp_path, django_capture_on_commit_callbacks)

        entry = ArtifactCacheEntry.objects.get(recording=recording)
        ((data, version_id),) = job.calls
        assert version_id == entry.version_id
        assert data == Path(entry.storage_path).read_bytes()
        row = DeriveJob.objects.get(recording=recording)
        assert (row.state, row.input_version_id) == (DeriveJob.State.DONE, entry.version_id)
        assert recording.status == Recording.Status.READY

    def test_a_failed_derive_job_is_reported_on_the_job(self, user, tmp_path, django_capture_on_commit_callbacks):
        job = _RecordingJob()
        job.run = lambda *args: (_ for _ in ()).throw(RuntimeError("no impedance channel"))
        derive.register_derive_job(job)
        recording, _push = _ingest(user, tmp_path, django_capture_on_commit_callbacks)
        row = DeriveJob.objects.get(recording=recording)
        assert row.state == DeriveJob.State.FAILED
        assert "no impedance channel" in row.error
        assert recording.status == Recording.Status.READY


@pytest.mark.django_db
class TestRerun:
    def test_replanning_a_ready_recording_takes_it_back_to_processing(
        self, user, tmp_path, django_capture_on_commit_callbacks
    ):
        registry.register_reconstruction_stage(_XorStage())
        recording, _push = _ingest(user, tmp_path, django_capture_on_commit_callbacks)
        assert recording.status == Recording.Status.READY

        PROVIDER.plan(recording)
        assert lifecycle.settle(recording.pk) == Recording.Status.PROCESSING
        with django_capture_on_commit_callbacks(execute=True):
            lifecycle.start(recording.pk)
        recording.refresh_from_db()
        assert recording.status == Recording.Status.READY
        assert ArtifactCacheEntry.objects.filter(recording=recording).count() == 1

    def test_a_per_recording_override_survives_a_replan(self, user, tmp_path, django_capture_on_commit_callbacks):
        registry.register_reconstruction_stage(_XorStage())
        recording, _push = _ingest(user, tmp_path, django_capture_on_commit_callbacks)
        RecordingJob.objects.filter(recording=recording).update(enabled=False)
        PROVIDER.plan(recording)
        assert RecordingJob.objects.get(recording=recording).state == RecordingJob.State.SKIPPED


@pytest.mark.django_db
class TestDeletion:
    def test_unlinking_the_recording_removes_its_versions(self, user, tmp_path, django_capture_on_commit_callbacks):
        from recordings.linked import unlink_recording_file

        registry.register_reconstruction_stage(_XorStage())
        recording, _push = _ingest(user, tmp_path, django_capture_on_commit_callbacks)
        built = Path(ArtifactCacheEntry.objects.get(recording=recording).storage_path)
        assert built.exists()
        assert built == version_path(recording, built.stem)

        unlink_recording_file(recording)
        assert not built.exists()
        assert not Path(recording.file_path).exists()

    def test_a_linked_recordings_versions_go_but_its_file_stays(
        self, user, tmp_path, django_capture_on_commit_callbacks
    ):
        from recordings.linked import unlink_recording_file

        registry.register_reconstruction_stage(_XorStage())
        recording, _push = _ingest(user, tmp_path, django_capture_on_commit_callbacks)
        built = Path(ArtifactCacheEntry.objects.get(recording=recording).storage_path)
        Recording.objects.filter(pk=recording.pk).update(storage=Recording.Storage.LINKED)
        recording.refresh_from_db()

        assert unlink_recording_file(recording) is False
        assert not built.exists()
        assert Path(recording.file_path).exists()


@pytest.mark.django_db
class TestReviewRegressions:
    def test_hypnogram_stages_wait_until_the_study_can_be_opened(
        self, user, tmp_path, django_capture_on_commit_callbacks
    ):
        """A study that starts in PROCESSING cannot be opened, so a grantee must not be able to list its stages."""
        registry.register_reconstruction_stage(_XorStage())
        scoring = [{"onset": 0.0, "duration": 30.0, "code": "W"}]
        with patch("recordings.hypnogram.write_hypnogram") as write:
            original_create = Recording.objects.create

            def create_with_scoring(**kwargs):
                return original_create(pending_scoring=scoring, **kwargs)

            with patch.object(Recording.objects, "create", side_effect=create_with_scoring):
                recording, _push = _ingest(user, tmp_path, django_capture_on_commit_callbacks, run_dispatch=False)
            assert recording.status == Recording.Status.PROCESSING
            assert not write.called
            assert recording.pending_scoring == scoring

            with django_capture_on_commit_callbacks(execute=True):
                lifecycle.start(recording.pk)
            recording.refresh_from_db()
            assert recording.status == Recording.Status.READY
            write.assert_called_once()
            assert recording.pending_scoring is None

    def test_a_linked_recording_gets_no_reconstruction_stages(self, user, tmp_path, django_capture_on_commit_callbacks):
        """Its file was never de-identified by the platform; a version would be a new copy of it."""
        registry.register_reconstruction_stage(_XorStage())
        job = _RecordingJob()
        derive.register_derive_job(job)
        recording, _push = _ingest(user, tmp_path, django_capture_on_commit_callbacks, run_dispatch=False)
        RecordingJob.objects.filter(recording=recording).delete()
        DeriveJob.objects.filter(recording=recording).delete()
        Recording.objects.filter(pk=recording.pk).update(storage=Recording.Storage.LINKED)
        recording.refresh_from_db()

        PROVIDER.plan(recording)
        assert not RecordingJob.objects.filter(recording=recording).exists()
        assert DeriveJob.objects.get(recording=recording).input_version_id == SOURCE_VERSION_ID

    def test_a_version_built_for_a_deleted_recording_leaves_no_file(
        self, user, tmp_path, django_capture_on_commit_callbacks
    ):
        from compute.lifecycle import materialise_version

        recording, _push = _ingest(user, tmp_path, django_capture_on_commit_callbacks)
        manifest = Manifest(recording.stored_hash).with_stage(stage_application(_XorStage()))
        Recording.objects.filter(pk=recording.pk).delete()

        assert materialise_version(recording, manifest, b"version bytes") is None
        assert not version_path(recording, manifest.version_id).exists()
        assert not ArtifactCacheEntry.objects.exists()


@pytest.mark.django_db
class TestSweep:
    def _old(self, path, hours=48):
        import os
        import time

        stamp = time.time() - hours * 3600
        os.utime(path, (stamp, stamp))

    def test_files_no_entry_names_are_removed_after_the_grace_period(
        self, user, tmp_path, django_capture_on_commit_callbacks
    ):
        from compute.lifecycle import artifact_cache_root, sweep_artifact_cache

        registry.register_reconstruction_stage(_XorStage())
        recording, _push = _ingest(user, tmp_path, django_capture_on_commit_callbacks)
        named = Path(ArtifactCacheEntry.objects.get(recording=recording).storage_path)
        orphan_dir = artifact_cache_root() / "ORPHAN"
        orphan_dir.mkdir()
        orphan = orphan_dir / "version.edf.tmp"
        orphan.write_bytes(b"left by a killed worker")
        fresh = named.with_name("in-progress.edf.tmp")
        fresh.write_bytes(b"still being written")
        for path in (named, orphan):
            self._old(path)

        assert sweep_artifact_cache() == 1
        assert named.exists()
        assert not orphan.exists()
        assert not orphan_dir.exists()
        assert fresh.exists()


@pytest.mark.django_db
class TestRecovery:
    def test_a_stage_a_killed_worker_left_running_is_released_and_can_be_retried(
        self, user, tmp_path, django_capture_on_commit_callbacks
    ):
        from datetime import timedelta

        from django.utils import timezone

        registry.register_reconstruction_stage(_XorStage())
        recording, _push = _ingest(user, tmp_path, django_capture_on_commit_callbacks, run_dispatch=False)
        RecordingJob.objects.filter(recording=recording).update(
            state=RecordingJob.State.RUNNING, started_at=timezone.now() - timedelta(hours=7)
        )
        # Another run finds the phase held by nobody alive, fails the stage, and the recording settles.
        with django_capture_on_commit_callbacks(execute=True):
            lifecycle.start(recording.pk)
        recording.refresh_from_db()
        assert RecordingJob.objects.get(recording=recording).state == RecordingJob.State.FAILED
        assert recording.status == Recording.Status.READY

        # An unchanged request reruns a failed recording.
        assert PROVIDER.update_jobs(recording, enabled={}, order=None) is True
        lifecycle.settle(recording.pk)
        with django_capture_on_commit_callbacks(execute=True):
            lifecycle.start(recording.pk)
        assert RecordingJob.objects.get(recording=recording).state == RecordingJob.State.DONE

    def test_a_running_stage_within_the_threshold_still_holds_the_phase(
        self, user, tmp_path, django_capture_on_commit_callbacks
    ):
        from django.utils import timezone

        registry.register_reconstruction_stage(_XorStage())
        recording, _push = _ingest(user, tmp_path, django_capture_on_commit_callbacks, run_dispatch=False)
        RecordingJob.objects.filter(recording=recording).update(
            state=RecordingJob.State.RUNNING, started_at=timezone.now()
        )
        from compute.lifecycle import run_reconstruction

        assert run_reconstruction(recording.pk)["status"] == "busy"
        with pytest.raises(RuntimeError):
            PROVIDER.update_jobs(recording, enabled={}, order=None)
