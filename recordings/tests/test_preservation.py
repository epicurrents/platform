"""Tests for the recordings preservation tier write paths.

Covers:

* The ``should_preserve_*`` helpers across the three modes.
* ``write_original`` happy paths, idempotency, path-sanitisation, and
  feature-disabled short-circuit.
* ``validate_settings`` rejecting incoherent mode/path combinations.
* End-to-end wiring through ``process_recording`` — that mode ``"all"``
  preserves before any processing runs, mode ``"failed"`` preserves on
  ingest failure, and mode ``"none"`` never writes.
* ``Recording.processing_error`` populated on format-processing failure
  and surfaced to author / superuser only via the recording-detail API.
"""

import contextlib
import hashlib
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings

from epicurrents.models import AccessRight
from recordings.models import Recording
from recordings.preservation import (
    MODE_ALL,
    MODE_FAILED,
    MODE_NONE,
    REASON_ALL,
    REASON_FAILED,
    _safe_original_filename,
    should_preserve_failed,
    should_preserve_original,
    validate_settings,
    write_original,
)


def _make_recording(user, **kwargs):
    """Create a ``PENDING`` Recording for use in unit-level preservation tests."""
    defaults = {
        "author": user,
        "original_name": "patient_01.edf",
        "stored_name": "ABCDEFABCDEFABCDEFABCDEFABCDEFAB.edf",
        "file_extension": ".edf",
        "file_size": 100,
        "file_path": "/tmp/test.edf",
        "file_hash": "a" * 64,
        "content_hash": "",
        "status": Recording.Status.PENDING,
    }
    defaults.update(kwargs)
    return Recording.objects.create(**defaults)


# ────────────────────────────────────────────────────────────────────────────
# Mode-check helpers
# ────────────────────────────────────────────────────────────────────────────


@pytest.mark.django_db
class TestModeHelpers:
    """``should_preserve_*`` map cleanly onto the three modes."""

    def test_failed_helper_matches_modes(self, settings):
        for mode, expected in (
            (MODE_NONE, False),
            (MODE_FAILED, True),
            (MODE_ALL, True),
        ):
            settings.RECORDINGS_PRESERVE_MODE = mode
            assert should_preserve_failed() is expected

    def test_original_helper_matches_modes(self, settings):
        for mode, expected in (
            (MODE_NONE, False),
            (MODE_FAILED, False),
            (MODE_ALL, True),
        ):
            settings.RECORDINGS_PRESERVE_MODE = mode
            assert should_preserve_original() is expected


# ────────────────────────────────────────────────────────────────────────────
# Path sanitisation
# ────────────────────────────────────────────────────────────────────────────


class TestSafeOriginalFilename:
    """``original_name`` is user-supplied; reject anything that could escape."""

    @pytest.mark.parametrize(
        "name,expected",
        [
            ("recording.edf", "recording.edf"),
            ("../../etc/passwd", "passwd"),
            ("/etc/passwd", "passwd"),
            ("subdir/recording.edf", "recording.edf"),
            ("", "upload"),
            (".", "upload"),
            ("..", "upload"),
            ("   ", "upload"),
        ],
    )
    def test_strips_path_components(self, name, expected):
        assert _safe_original_filename(name) == expected


# ────────────────────────────────────────────────────────────────────────────
# Startup validation
# ────────────────────────────────────────────────────────────────────────────


class TestValidateSettings:
    """``apps.ready()`` delegates here; mismatched settings must blow up."""

    def test_default_mode_passes(self, settings):
        settings.RECORDINGS_PRESERVE_MODE = MODE_NONE
        settings.RECORDINGS_ORIGINALS_PATH = None
        validate_settings()  # no exception

    def test_unknown_mode_raises(self, settings):
        settings.RECORDINGS_PRESERVE_MODE = "bogus"
        with pytest.raises(ImproperlyConfigured, match="RECORDINGS_PRESERVE_MODE"):
            validate_settings()

    def test_failed_mode_without_path_raises(self, settings):
        settings.RECORDINGS_PRESERVE_MODE = MODE_FAILED
        settings.RECORDINGS_ORIGINALS_PATH = None
        with pytest.raises(ImproperlyConfigured, match="RECORDINGS_ORIGINALS_PATH"):
            validate_settings()

    def test_all_mode_without_path_raises(self, settings):
        settings.RECORDINGS_PRESERVE_MODE = MODE_ALL
        settings.RECORDINGS_ORIGINALS_PATH = None
        with pytest.raises(ImproperlyConfigured, match="RECORDINGS_ORIGINALS_PATH"):
            validate_settings()

    def test_mode_with_path_passes(self, settings, tmp_path):
        settings.RECORDINGS_PRESERVE_MODE = MODE_ALL
        settings.RECORDINGS_ORIGINALS_PATH = str(tmp_path)
        validate_settings()


# ────────────────────────────────────────────────────────────────────────────
# write_original
# ────────────────────────────────────────────────────────────────────────────


@pytest.mark.django_db
class TestWriteOriginal:
    def test_skips_when_no_originals_path(self, user, settings, tmp_path):
        """No volume mount → feature disabled, returns False silently."""
        settings.RECORDINGS_ORIGINALS_PATH = None
        source = tmp_path / "src.edf"
        source.write_bytes(b"data")
        recording = _make_recording(user)

        assert write_original(recording, source, reason=REASON_ALL) is False

    def test_writes_file_and_manifest(self, user, settings, tmp_path):
        originals = tmp_path / "originals"
        source = tmp_path / "src.edf"
        source.write_bytes(b"original bytes")
        settings.RECORDINGS_ORIGINALS_PATH = str(originals)
        recording = _make_recording(user)

        assert write_original(recording, source, reason=REASON_ALL) is True

        target_dir = originals / "ABCDEFABCDEFABCDEFABCDEFABCDEFAB"
        assert (target_dir / "patient_01.edf").read_bytes() == b"original bytes"

        manifest = json.loads((target_dir / "manifest.json").read_text())
        assert manifest["recording_pk"] == recording.pk
        assert manifest["stored_name"] == recording.stored_name
        assert manifest["original_name"] == "patient_01.edf"
        assert manifest["file_hash"] == "a" * 64
        assert manifest["file_size"] == 100
        assert manifest["author_id"] == user.pk
        assert manifest["preservation_reason"] == REASON_ALL

    def test_idempotent_returns_false_on_second_call(self, user, settings, tmp_path):
        """The second call is a no-op so mode 'all' + failure doesn't double-write."""
        originals = tmp_path / "originals"
        source = tmp_path / "src.edf"
        source.write_bytes(b"data")
        settings.RECORDINGS_ORIGINALS_PATH = str(originals)
        recording = _make_recording(user)

        assert write_original(recording, source, reason=REASON_ALL) is True
        assert write_original(recording, source, reason=REASON_FAILED) is False

        # Manifest reflects the first write (REASON_ALL); not overwritten.
        target_dir = originals / "ABCDEFABCDEFABCDEFABCDEFABCDEFAB"
        manifest = json.loads((target_dir / "manifest.json").read_text())
        assert manifest["preservation_reason"] == REASON_ALL

    def test_invalid_reason_raises(self, user, settings, tmp_path):
        settings.RECORDINGS_ORIGINALS_PATH = str(tmp_path)
        recording = _make_recording(user)
        with pytest.raises(ValueError, match="reason"):
            write_original(recording, "/dev/null", reason="bogus")

    def test_missing_source_returns_false(self, user, settings, tmp_path):
        """A missing staged file is logged and skipped, not raised."""
        settings.RECORDINGS_ORIGINALS_PATH = str(tmp_path)
        recording = _make_recording(user)
        assert write_original(recording, "/nonexistent/file.edf", reason=REASON_ALL) is False

    def test_path_traversal_in_original_name_is_sanitised(self, user, settings, tmp_path):
        """Hostile ``original_name`` must land inside the per-recording dir."""
        originals = tmp_path / "originals"
        source = tmp_path / "src.edf"
        source.write_bytes(b"hostile")
        settings.RECORDINGS_ORIGINALS_PATH = str(originals)
        recording = _make_recording(user, original_name="../../escape.edf")

        assert write_original(recording, source, reason=REASON_ALL) is True

        target_dir = originals / "ABCDEFABCDEFABCDEFABCDEFABCDEFAB"
        assert (target_dir / "escape.edf").exists()
        # Crucially: nothing escaped the per-recording directory.
        assert not (originals / "escape.edf").exists()
        assert not (originals.parent / "escape.edf").exists()

    def test_original_name_manifest_json_does_not_clobber_manifest(self, user, settings, tmp_path):
        """A file uploaded as ``manifest.json`` must not overwrite the manifest.

        Pre-fix the data file and the manifest would both target
        ``<dir>/manifest.json`` and ``_write_manifest`` would silently
        destroy the user's data with the JSON document.
        """
        originals = tmp_path / "originals"
        source = tmp_path / "src.edf"
        source.write_bytes(b"genuine user bytes")
        settings.RECORDINGS_ORIGINALS_PATH = str(originals)
        recording = _make_recording(user, original_name="manifest.json")

        assert write_original(recording, source, reason=REASON_ALL) is True

        target_dir = originals / "ABCDEFABCDEFABCDEFABCDEFABCDEFAB"
        # Data file is renamed to avoid collision.
        assert (target_dir / "original_manifest.json").read_bytes() == (b"genuine user bytes")
        # Manifest is parseable JSON and not the user's bytes.
        manifest = json.loads((target_dir / "manifest.json").read_text())
        assert manifest["preservation_reason"] == REASON_ALL

    def test_manifest_idempotency_survives_converter_rename(self, user, settings, tmp_path):
        """Mode 'all' wrote with ``.e``; converter rewrites; failure path skips.

        Recreates the .e → .edf converter sequence: ``write_original`` is
        called once with the as-uploaded name, then ``recording.original
        _name`` is mutated (mimicking the converter), then the failure-mode
        path tries again.  The idempotency check is anchored on
        ``manifest.json`` existence so the second call is a no-op and the
        manifest's ``preservation_reason`` stays ``"all"``.
        """
        originals = tmp_path / "originals"
        source = tmp_path / "src.e"
        source.write_bytes(b"original .e bytes")
        settings.RECORDINGS_ORIGINALS_PATH = str(originals)
        recording = _make_recording(user, original_name="patient.e")

        assert write_original(recording, source, reason=REASON_ALL) is True

        # Simulate the converter mutating the row before the failure handler.
        recording.original_name = "patient.edf"

        assert write_original(recording, source, reason=REASON_FAILED) is False

        target_dir = originals / "ABCDEFABCDEFABCDEFABCDEFABCDEFAB"
        manifest = json.loads((target_dir / "manifest.json").read_text())
        assert manifest["preservation_reason"] == REASON_ALL
        assert manifest["original_name"] == "patient.e"
        # The post-conversion filename never lands on disk.
        assert not (target_dir / "patient.edf").exists()
        assert (target_dir / "patient.e").exists()

    def test_original_name_override_uses_caller_supplied_name(self, user, settings, tmp_path):
        """``original_name_override`` decouples the manifest from the row.

        Used by the bulk-import command: by the time preservation runs the
        recording's ``original_name`` has been rewritten to the converted
        extension, but the bytes at ``source_path`` are still the as-uploaded
        source — the override carries the truth.
        """
        originals = tmp_path / "originals"
        source = tmp_path / "patient.e"
        source.write_bytes(b"as-uploaded bytes")
        settings.RECORDINGS_ORIGINALS_PATH = str(originals)
        recording = _make_recording(user, original_name="patient.edf")

        assert write_original(
            recording,
            source,
            reason=REASON_ALL,
            original_name_override="patient.e",
        )

        target_dir = originals / "ABCDEFABCDEFABCDEFABCDEFABCDEFAB"
        assert (target_dir / "patient.e").read_bytes() == b"as-uploaded bytes"
        manifest = json.loads((target_dir / "manifest.json").read_text())
        assert manifest["original_name"] == "patient.e"


# ────────────────────────────────────────────────────────────────────────────
# Integration with process_recording
# ────────────────────────────────────────────────────────────────────────────


def _make_pending_for_task(user, staging_dir, *, extension=".bin"):
    """Create a PENDING Recording with a real staged file (matches test_tasks pattern)."""
    content = b"fake binary recording content"
    stored = f"AABBCCDD11223344AABBCCDD11223344{extension}"
    staged_file = staging_dir / stored
    staged_file.write_bytes(content)
    return Recording.objects.create(
        author=user,
        original_name=f"test{extension}",
        stored_name=stored,
        file_extension=extension,
        file_size=len(content),
        file_path=str(staged_file),
        file_hash=hashlib.sha256(content).hexdigest(),
        content_hash="",
        status=Recording.Status.PENDING,
    )


@pytest.mark.django_db
class TestProcessRecordingPreservation:
    """End-to-end wiring through ``process_recording``."""

    def _run_task(self, recording, staging, uploads, originals, mode):
        from recordings.tasks import process_recording

        with (
            override_settings(
                RECORDINGS_STAGING_PATH=str(staging),
                RECORDINGS_UPLOAD_PATH=str(uploads),
                RECORDINGS_ORIGINALS_PATH=str(originals) if originals else None,
                RECORDINGS_PRESERVE_MODE=mode,
            ),
            patch("notifications.tasks.send_push_to_user.delay"),
        ):
            return process_recording(recording.pk)

    def test_mode_none_does_not_write_originals(self, user, tmp_path):
        staging = tmp_path / "staging"
        uploads = tmp_path / "uploads"
        originals = tmp_path / "originals"
        for d in (staging, uploads, originals):
            d.mkdir()
        recording = _make_pending_for_task(user, staging)

        self._run_task(recording, staging, uploads, originals, mode=MODE_NONE)

        assert list(originals.iterdir()) == []

    def test_mode_all_preserves_before_processing(self, user, tmp_path):
        """Mode 'all' writes the as-uploaded bytes to the originals volume."""
        staging = tmp_path / "staging"
        uploads = tmp_path / "uploads"
        originals = tmp_path / "originals"
        for d in (staging, uploads, originals):
            d.mkdir()
        recording = _make_pending_for_task(user, staging)
        expected_content = Path(recording.file_path).read_bytes()

        self._run_task(recording, staging, uploads, originals, mode=MODE_ALL)

        target_dir = originals / recording.stored_name.split(".")[0]
        assert (target_dir / "test.bin").read_bytes() == expected_content
        manifest = json.loads((target_dir / "manifest.json").read_text())
        assert manifest["preservation_reason"] == REASON_ALL

    def test_mode_failed_writes_only_on_failure(self, user, tmp_path):
        """Mode 'failed' must NOT preserve when processing succeeds."""
        staging = tmp_path / "staging"
        uploads = tmp_path / "uploads"
        originals = tmp_path / "originals"
        for d in (staging, uploads, originals):
            d.mkdir()
        recording = _make_pending_for_task(user, staging)  # .bin → success path

        self._run_task(recording, staging, uploads, originals, mode=MODE_FAILED)

        recording.refresh_from_db()
        assert recording.status == Recording.Status.READY
        assert list(originals.iterdir()) == []

    def test_mode_failed_preserves_when_edf_processing_fails(self, user, tmp_path):
        """Garbage EDF bytes → format failure → mode 'failed' writes the original."""
        staging = tmp_path / "staging"
        uploads = tmp_path / "uploads"
        originals = tmp_path / "originals"
        for d in (staging, uploads, originals):
            d.mkdir()
        # An .edf-extensioned file with content that does not parse as EDF
        # forces the format processor down the format_error branch.
        recording = _make_pending_for_task(user, staging, extension=".edf")

        self._run_task(recording, staging, uploads, originals, mode=MODE_FAILED)

        recording.refresh_from_db()
        assert recording.status == Recording.Status.FAILED
        assert recording.processing_error  # non-empty

        target_dir = originals / recording.stored_name.split(".")[0]
        manifest = json.loads((target_dir / "manifest.json").read_text())
        assert manifest["preservation_reason"] == REASON_FAILED

    def test_mode_all_plus_failure_keeps_single_copy(self, user, tmp_path):
        """Mode 'all' wrote at task start; the failure path is idempotent.

        Verifies the second ``write_original`` call (from the failed-mode
        branch) finds the target file already present and returns False;
        the manifest retains REASON_ALL.
        """
        staging = tmp_path / "staging"
        uploads = tmp_path / "uploads"
        originals = tmp_path / "originals"
        for d in (staging, uploads, originals):
            d.mkdir()
        recording = _make_pending_for_task(user, staging, extension=".edf")

        self._run_task(recording, staging, uploads, originals, mode=MODE_ALL)

        target_dir = originals / recording.stored_name.split(".")[0]
        manifest = json.loads((target_dir / "manifest.json").read_text())
        assert manifest["preservation_reason"] == REASON_ALL  # not overwritten

    def test_processing_error_populated_on_failure(self, user, tmp_path):
        staging = tmp_path / "staging"
        uploads = tmp_path / "uploads"
        for d in (staging, uploads):
            d.mkdir()
        recording = _make_pending_for_task(user, staging, extension=".edf")

        with (
            override_settings(
                RECORDINGS_STAGING_PATH=str(staging),
                RECORDINGS_UPLOAD_PATH=str(uploads),
                RECORDINGS_PRESERVE_MODE=MODE_NONE,
                RECORDINGS_ORIGINALS_PATH=None,
            ),
            patch("notifications.tasks.send_push_to_user.delay"),
        ):
            from recordings.tasks import process_recording

            process_recording(recording.pk)

        recording.refresh_from_db()
        assert recording.status == Recording.Status.FAILED
        assert recording.processing_error
        # The truncation safety net caps the field at 4 KB.
        assert len(recording.processing_error) <= 4096


@pytest.mark.django_db
class TestProcessRecordingPreservationWithConverter:
    """Phase 3 ``"failed"`` preservation for converter-bound formats.

    The bug closed by the pre_convert / convert_failed hooks: when a
    converter (e.g. ``.e`` → EDF) runs and the recording later ends up
    FAILED, the bytes preserved to the originals volume must be the
    user-uploaded source, not the converted derivative.  Two paths land
    in FAILED:

    1. Converter succeeds, format processing on the converted EDF fails.
    2. Converter itself raises.

    Both must preserve the source bytes.
    """

    _SOURCE_BYTES = b"<imaginary .xyz source bytes for preservation tests>"

    def _make_pending(self, user, staging, ext: str):
        stored = f"BBCCDDEE11223344BBCCDDEE11223344{ext}"
        staged_file = staging / stored
        staged_file.write_bytes(self._SOURCE_BYTES)
        return Recording.objects.create(
            author=user,
            original_name=f"sample{ext}",
            stored_name=stored,
            file_extension=ext,
            file_size=len(self._SOURCE_BYTES),
            file_path=str(staged_file),
            file_hash=hashlib.sha256(self._SOURCE_BYTES).hexdigest(),
            content_hash="",
            status=Recording.Status.PENDING,
        )

    def _run(self, recording, staging, uploads, originals, mode, *, converters):
        from recordings.tasks import process_recording

        with (
            override_settings(
                RECORDINGS_STAGING_PATH=str(staging),
                RECORDINGS_UPLOAD_PATH=str(uploads),
                RECORDINGS_ORIGINALS_PATH=str(originals) if originals else None,
                RECORDINGS_PRESERVE_MODE=mode,
                RECORDING_CONVERTERS=converters,
            ),
            patch("notifications.tasks.send_push_to_user.delay"),
        ):
            # The converter-raised path re-raises after preservation.
            # Tests assert on the side effects (preserved bytes,
            # cleaned-up stash); the re-raise is expected.
            with contextlib.suppress(Exception):
                return process_recording(recording.pk)

    def test_converter_succeeds_then_format_fails_preserves_source(self, user, tmp_path):
        """Converter ran + EDF processing failed → source bytes preserved (not converted EDF)."""
        staging = tmp_path / "staging"
        uploads = tmp_path / "uploads"
        originals = tmp_path / "originals"
        for d in (staging, uploads, originals):
            d.mkdir()
        recording = self._make_pending(user, staging, ext=".xyz")

        self._run(
            recording,
            staging,
            uploads,
            originals,
            mode=MODE_FAILED,
            converters={".xyz": "recordings.tests.test_convert_hooks._success_convert"},
        )

        recording.refresh_from_db()
        assert recording.status == Recording.Status.FAILED

        target_dir = originals / recording.stored_name.split(".")[0]
        manifest = json.loads((target_dir / "manifest.json").read_text())
        assert manifest["preservation_reason"] == REASON_FAILED
        # The bug: pre-fix this would have been the converter's 256-byte
        # output.  After fix: the user-uploaded source bytes.
        preserved_file = target_dir / manifest["original_name"]
        assert preserved_file.read_bytes() == self._SOURCE_BYTES
        # The manifest's original_name must reflect the source extension
        # (.xyz), not the post-conversion rewrite (.edf).
        assert manifest["original_name"].endswith(".xyz")

    def test_converter_raises_preserves_source(self, user, tmp_path):
        """Converter raised → source bytes preserved before outer cleanup deletes them."""
        staging = tmp_path / "staging"
        uploads = tmp_path / "uploads"
        originals = tmp_path / "originals"
        for d in (staging, uploads, originals):
            d.mkdir()
        recording = self._make_pending(user, staging, ext=".xyz")
        recording_pk = recording.pk
        original_stored_name = recording.stored_name

        self._run(
            recording,
            staging,
            uploads,
            originals,
            mode=MODE_FAILED,
            converters={".xyz": "recordings.tests.test_convert_hooks._raising_convert"},
        )

        # The outer except preserves the recording row as FAILED with the
        # error reason (rather than deleting it) — verify, then check that
        # source-byte preservation happened anyway.
        failed = Recording.objects.get(pk=recording_pk)
        assert failed.status == Recording.Status.FAILED
        assert failed.processing_error.startswith("Unexpected processing error:")

        target_dir = originals / original_stored_name.split(".")[0]
        manifest = json.loads((target_dir / "manifest.json").read_text())
        assert manifest["preservation_reason"] == REASON_FAILED
        preserved_file = target_dir / manifest["original_name"]
        assert preserved_file.read_bytes() == self._SOURCE_BYTES

    def test_stash_cleaned_up_after_task_completes(self, user, tmp_path):
        """No stash files left behind on disk after the task finishes."""
        from recordings.preservation import _PENDING_STASHES, _stash_dir

        staging = tmp_path / "staging"
        uploads = tmp_path / "uploads"
        originals = tmp_path / "originals"
        for d in (staging, uploads, originals):
            d.mkdir()
        recording = self._make_pending(user, staging, ext=".xyz")

        self._run(
            recording,
            staging,
            uploads,
            originals,
            mode=MODE_FAILED,
            converters={".xyz": "recordings.tests.test_convert_hooks._success_convert"},
        )

        # Stash dict empty for this recording.
        assert recording.pk not in _PENDING_STASHES
        # Stash file on disk gone (best-effort — stash dir may not exist
        # at all on a clean test, which is also fine).
        stash_file = _stash_dir() / str(recording.pk)
        assert not stash_file.exists()

    def test_mode_none_does_not_stash_for_converter(self, user, tmp_path):
        """Mode 'none' must not stash source bytes — no preservation means no work."""
        from recordings.preservation import _PENDING_STASHES

        staging = tmp_path / "staging"
        uploads = tmp_path / "uploads"
        originals = tmp_path / "originals"
        for d in (staging, uploads, originals):
            d.mkdir()
        recording = self._make_pending(user, staging, ext=".xyz")

        self._run(
            recording,
            staging,
            uploads,
            originals,
            mode=MODE_NONE,
            converters={".xyz": "recordings.tests.test_convert_hooks._success_convert"},
        )

        assert recording.pk not in _PENDING_STASHES
        assert list(originals.iterdir()) == []

    def test_mode_all_plus_converter_failure_keeps_reason_all(self, user, tmp_path):
        """Mode 'all' preserved at task start; the converter-failure path is idempotent."""
        staging = tmp_path / "staging"
        uploads = tmp_path / "uploads"
        originals = tmp_path / "originals"
        for d in (staging, uploads, originals):
            d.mkdir()
        recording = self._make_pending(user, staging, ext=".xyz")

        self._run(
            recording,
            staging,
            uploads,
            originals,
            mode=MODE_ALL,
            converters={".xyz": "recordings.tests.test_convert_hooks._success_convert"},
        )

        target_dir = originals / recording.stored_name.split(".")[0]
        manifest = json.loads((target_dir / "manifest.json").read_text())
        # Mode 'all' wrote at task start; the post-failure write is a no-op
        # because the manifest already exists.  REASON_ALL stays.
        assert manifest["preservation_reason"] == REASON_ALL


# ────────────────────────────────────────────────────────────────────────────
# API visibility of processing_error
# ────────────────────────────────────────────────────────────────────────────


@pytest.mark.django_db
class TestProcessingErrorVisibility:
    """``processing_error`` is author + superuser only; never returned elsewhere.

    Pairs with the ``original_name`` visibility rule — both can carry PHI or
    operator-only context (stack traces with paths).
    """

    DETAIL_URL = "/recordings/api/v1/{hash}"

    def _make_failed_recording(self, user, **kwargs):
        defaults = {
            "status": Recording.Status.FAILED,
            "processing_error": "EdfParseError: invalid header at offset 12",
            "stored_name": "ER12ER12ER12ER12ER12ER12ER12ER12.edf",
            "original_name": "patient_xyz.edf",
        }
        defaults.update(kwargs)
        return Recording.objects.create(
            author=user,
            file_extension=".edf",
            file_size=100,
            file_path="/tmp/missing.edf",
            file_hash="b" * 64,
            content_hash="c" * 64,
            **defaults,
        )

    def _grant_read(self, target_user, recording, giver):
        ct = ContentType.objects.get_for_model(recording, for_concrete_model=False)
        return AccessRight.objects.create(
            content_type=ct,
            object_id=str(recording.pk),
            access_giver=giver,
            access_target=target_user,
            can_read=True,
        )

    def test_author_sees_processing_error(self, auth_client):
        c, user = auth_client
        recording = self._make_failed_recording(user)
        hash_part = recording.stored_name.split(".")[0]
        resp = c.get(self.DETAIL_URL.format(hash=hash_part))
        assert resp.status_code == 200
        assert resp.json()["processing_error"] == "EdfParseError: invalid header at offset 12"

    def test_superuser_sees_processing_error(self, superuser_client, user):
        c, _ = superuser_client
        recording = self._make_failed_recording(user)
        hash_part = recording.stored_name.split(".")[0]
        resp = c.get(self.DETAIL_URL.format(hash=hash_part))
        assert resp.status_code == 200
        assert resp.json()["processing_error"] == "EdfParseError: invalid header at offset 12"

    def test_ready_recording_returns_null_processing_error(self, auth_client):
        """Successful recordings carry an empty error; the response serialises null."""
        c, user = auth_client
        recording = Recording.objects.create(
            author=user,
            original_name="ok.edf",
            stored_name="OK33OK33OK33OK33OK33OK33OK33OK33.edf",
            file_extension=".edf",
            file_size=100,
            file_path="/tmp/ok.edf",
            file_hash="d" * 64,
            content_hash="e" * 64,
            status=Recording.Status.READY,
            processing_error="",
        )
        hash_part = recording.stored_name.split(".")[0]
        resp = c.get(self.DETAIL_URL.format(hash=hash_part))
        assert resp.status_code == 200
        assert resp.json()["processing_error"] is None


# ────────────────────────────────────────────────────────────────────────────
# validate_originals management command (Phase 4)
# ────────────────────────────────────────────────────────────────────────────


def _make_preserved_dir(
    originals_root: Path,
    *,
    stored_name: str,
    original_name: str = "patient.edf",
    file_bytes: bytes = b"x" * 64,
    manifest_file_size: int | None = None,
    omit_manifest: bool = False,
    manifest_stored_name_override: str | None = None,
    dir_name_override: str | None = None,
) -> Path:
    """Materialise a per-recording directory on the originals volume.

    Mirrors ``write_original``'s layout but lets each test inject the kind
    of malformation it wants to assert against (missing manifest, name
    mismatch, etc.).
    """
    prefix = dir_name_override or stored_name.split(".", 1)[0]
    target_dir = originals_root / prefix
    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / original_name).write_bytes(file_bytes)
    if not omit_manifest:
        manifest = {
            "recording_pk": 0,
            "stored_name": manifest_stored_name_override or stored_name,
            "original_name": original_name,
            "file_hash": "f" * 64,
            "file_size": (manifest_file_size if manifest_file_size is not None else len(file_bytes)),
            "author_id": 0,
            "uploaded_at": "2026-05-30T00:00:00+00:00",
            "preservation_reason": REASON_ALL,
        }
        (target_dir / "manifest.json").write_text(json.dumps(manifest))
    return target_dir


def _make_db_recording(user, **kwargs):
    """DB-only Recording row for cross-checking against the volume."""
    defaults = {
        "original_name": "patient.edf",
        "stored_name": "V1V1V1V1V1V1V1V1V1V1V1V1V1V1V1V1.edf",
        "file_extension": ".edf",
        "file_size": 64,
        "file_path": "/tmp/nope.edf",
        "file_hash": "g" * 64,
        "content_hash": "h" * 64,
        "status": Recording.Status.READY,
    }
    defaults.update(kwargs)
    return Recording.objects.create(author=user, **defaults)


def _run_validate(settings, tmp_path, **opts):
    """Call ``manage.py validate_originals`` with --json and parse the result.

    The command exits non-zero when issues are found (so cron / CI pickups
    can branch).  Suppress ``SystemExit(1)`` so tests can still inspect the
    JSON report — any other exit code re-raises.
    """
    from io import StringIO

    from django.core.management import call_command

    out = StringIO()
    err = StringIO()
    with override_settings(RECORDINGS_ORIGINALS_PATH=str(tmp_path)):
        try:
            call_command("validate_originals", "--json", stdout=out, stderr=err, **opts)
        except SystemExit as exc:
            if exc.code not in (0, 1):
                raise
    return json.loads(out.getvalue())


@pytest.mark.django_db
class TestValidateOriginalsCommand:
    """Read-only cross-check between the originals volume and the DB."""

    def test_unconfigured_path_raises_command_error(self, settings):
        from django.core.management import CommandError, call_command

        settings.RECORDINGS_ORIGINALS_PATH = None
        with pytest.raises(CommandError, match="RECORDINGS_ORIGINALS_PATH"):
            call_command("validate_originals")

    def test_nonexistent_path_raises_command_error(self, settings, tmp_path):
        from django.core.management import CommandError, call_command

        settings.RECORDINGS_ORIGINALS_PATH = str(tmp_path / "does_not_exist")
        with pytest.raises(CommandError, match="does not exist"):
            call_command("validate_originals")

    def test_clean_report_when_volume_and_db_match(self, user, settings, tmp_path):
        recording = _make_db_recording(user)
        _make_preserved_dir(tmp_path, stored_name=recording.stored_name)
        report = _run_validate(settings, tmp_path, expect_tier=MODE_ALL)
        assert report["orphans"] == []
        assert report["missing"] == []
        assert report["size_mismatches"] == []
        assert report["malformed"] == []
        assert report["directory_count"] == 1
        assert report["active_recording_count"] == 1

    def test_orphan_reported_when_dir_has_no_matching_row(self, settings, tmp_path):
        _make_preserved_dir(tmp_path, stored_name="ORPHANORPHANORPHANORPHANORPHANOR.edf")
        report = _run_validate(settings, tmp_path, expect_tier=MODE_NONE)
        assert len(report["orphans"]) == 1
        assert report["orphans"][0]["stored_name_prefix"] == "ORPHANORPHANORPHANORPHANORPHANOR"

    def test_missing_under_mode_all_reports_undirected_rows(self, user, settings, tmp_path):
        recording = _make_db_recording(user)
        # No matching directory on disk.
        report = _run_validate(settings, tmp_path, expect_tier=MODE_ALL)
        assert len(report["missing"]) == 1
        assert report["missing"][0]["recording_pk"] == recording.pk

    def test_missing_under_mode_failed_only_lists_failed_rows(self, user, settings, tmp_path):
        ready = _make_db_recording(user, stored_name="READY1234READY1234READY1234READY.edf")
        failed = _make_db_recording(
            user,
            stored_name="FAILED12FAILED12FAILED12FAILED12.edf",
            status=Recording.Status.FAILED,
        )
        report = _run_validate(settings, tmp_path, expect_tier=MODE_FAILED)
        missing_pks = {row["recording_pk"] for row in report["missing"]}
        assert ready.pk not in missing_pks
        assert failed.pk in missing_pks

    def test_missing_under_mode_none_is_empty(self, user, settings, tmp_path):
        _make_db_recording(user)
        report = _run_validate(settings, tmp_path, expect_tier=MODE_NONE)
        assert report["missing"] == []

    def test_size_mismatch_reported(self, user, settings, tmp_path):
        recording = _make_db_recording(user)
        _make_preserved_dir(
            tmp_path,
            stored_name=recording.stored_name,
            file_bytes=b"y" * 32,
            manifest_file_size=999,  # claim a size that doesn't match the file
        )
        report = _run_validate(settings, tmp_path, expect_tier=MODE_ALL)
        assert len(report["size_mismatches"]) == 1
        assert report["size_mismatches"][0]["manifest_size"] == 999
        assert report["size_mismatches"][0]["on_disk_size"] == 32

    def test_skip_size_check_omits_mismatch(self, user, settings, tmp_path):
        recording = _make_db_recording(user)
        _make_preserved_dir(
            tmp_path,
            stored_name=recording.stored_name,
            file_bytes=b"y" * 32,
            manifest_file_size=999,
        )
        report = _run_validate(settings, tmp_path, expect_tier=MODE_ALL, skip_size=True)
        assert report["size_mismatches"] == []

    def test_malformed_missing_manifest(self, settings, tmp_path):
        _make_preserved_dir(
            tmp_path,
            stored_name="MALFORM1MALFORM1MALFORM1MALFORM1.edf",
            omit_manifest=True,
        )
        report = _run_validate(settings, tmp_path, expect_tier=MODE_NONE)
        assert any(row["reason"] == "manifest_missing" for row in report["malformed"])

    def test_malformed_directory_name_mismatch(self, settings, tmp_path):
        _make_preserved_dir(
            tmp_path,
            stored_name="ACTUAL12ACTUAL12ACTUAL12ACTUAL12.edf",
            dir_name_override="WRONG123WRONG123WRONG123WRONG123",
        )
        report = _run_validate(settings, tmp_path, expect_tier=MODE_NONE)
        assert any("directory_name_mismatch" in row["reason"] for row in report["malformed"])

    def test_stray_file_at_volume_root_is_malformed(self, settings, tmp_path):
        (tmp_path / "stray.txt").write_text("not a recording")
        report = _run_validate(settings, tmp_path, expect_tier=MODE_NONE)
        assert any(row["reason"] == "not_a_directory" for row in report["malformed"])

    def test_expect_tier_override_changes_missing_calculation(self, user, settings, tmp_path):
        """The CLI flag overrides the current mode when computing missing."""
        settings.RECORDINGS_PRESERVE_MODE = MODE_NONE
        recording = _make_db_recording(user, status=Recording.Status.FAILED)
        # Current mode is "none" so by default nothing is "missing".
        report = _run_validate(settings, tmp_path)
        assert report["missing"] == []
        # --expect-tier=failed flips the expectation; recording is now missing.
        report_failed = _run_validate(settings, tmp_path, expect_tier=MODE_FAILED)
        assert any(row["recording_pk"] == recording.pk for row in report_failed["missing"])

    def test_soft_deleted_recording_excluded_from_missing(self, user, settings, tmp_path):
        from django.utils import timezone

        _make_db_recording(user, deleted_at=timezone.now())
        report = _run_validate(settings, tmp_path, expect_tier=MODE_ALL)
        assert report["missing"] == []

    def test_pending_recording_excluded_from_missing_under_all(self, user, settings, tmp_path):
        """Mode 'all' writes pre-processing, so PENDING / PROCESSING are skipped."""
        _make_db_recording(user, status=Recording.Status.PENDING)
        report = _run_validate(settings, tmp_path, expect_tier=MODE_ALL)
        assert report["missing"] == []

    def test_json_output_is_pure_json(self, user, settings, tmp_path):
        """``--json`` must emit parseable JSON even when issues are found.

        Pre-fix the command appended ``\\nvalidate_originals: issues found.``
        after the JSON, corrupting the output for downstream ``jq`` parsers.
        """
        # Force at least one issue (an orphan directory).
        _make_preserved_dir(tmp_path, stored_name="ORPHANORPHANORPHANORPHANORPHANOR.edf")
        # _run_validate already json.loads — a corrupt blob would raise.
        report = _run_validate(settings, tmp_path, expect_tier=MODE_NONE)
        assert len(report["orphans"]) == 1

    def test_nonzero_exit_when_issues_found(self, user, settings, tmp_path):
        """The command exits 1 when any category has at least one row."""
        from django.core.management import call_command

        _make_preserved_dir(tmp_path, stored_name="EXITEXITEXITEXITEXITEXITEXITEXIT.edf")
        with override_settings(RECORDINGS_ORIGINALS_PATH=str(tmp_path)), pytest.raises(SystemExit) as excinfo:
            call_command("validate_originals", "--json")
        assert excinfo.value.code == 1

    def test_zero_exit_when_clean(self, user, settings, tmp_path):
        from django.core.management import call_command

        # No directories on the volume, no DB rows expected → clean.
        with override_settings(RECORDINGS_ORIGINALS_PATH=str(tmp_path)):
            # No SystemExit means exit 0.
            call_command("validate_originals", "--json")

    def test_directory_with_manifest_but_no_data_file_is_malformed(self, user, settings, tmp_path):
        """A dir with manifest.json and no data file must be reported.

        Pre-fix this slipped past the malformed check because the previous
        only-manifest-missing branch only triggered when manifest.json was
        absent — a manifest with no data file passed silently.
        """
        target_dir = tmp_path / "NOFILE12NOFILE12NOFILE12NOFILE12"
        target_dir.mkdir()
        manifest = {
            "stored_name": "NOFILE12NOFILE12NOFILE12NOFILE12.edf",
            "original_name": "patient.edf",
            "file_size": 0,
        }
        (target_dir / "manifest.json").write_text(json.dumps(manifest))
        report = _run_validate(settings, tmp_path, expect_tier=MODE_NONE)
        assert any(row["reason"] == "data_file_missing" for row in report["malformed"])

    def test_soft_deleted_recording_listed_under_separate_category(self, user, settings, tmp_path):
        """Soft-deleted recordings with preserved files are not orphans.

        Pre-fix they appeared in the orphans category, misleading operators
        into thinking the platform had lost track of the row.  Now they
        sit in their own ``soft_deleted_preserved`` category that explains
        the per-design no-propagation behaviour.
        """
        from django.utils import timezone

        recording = _make_db_recording(user, deleted_at=timezone.now())
        _make_preserved_dir(tmp_path, stored_name=recording.stored_name)
        report = _run_validate(settings, tmp_path, expect_tier=MODE_NONE)
        assert report["orphans"] == []
        assert len(report["soft_deleted_preserved"]) == 1
        assert report["soft_deleted_preserved"][0]["recording_pk"] == recording.pk


# ────────────────────────────────────────────────────────────────────────────
# import_recordings command honours preservation mode
# ────────────────────────────────────────────────────────────────────────────


@pytest.mark.django_db
class TestImportRecordingsPreservation:
    """The bulk-import command writes to the originals volume under mode 'all'."""

    def _make_edf_source(self, source_dir: Path) -> Path:
        from recordings.tests.test_edf_processor import (
            _make_edf_data,
            _make_edf_header,
        )

        signals = [{"label": "EEG Fp1", "sample_count": 16}]
        edf_bytes = _make_edf_header(signals=signals) + _make_edf_data(signals, n_records=1)
        edf_path = source_dir / "subject_001.edf"
        edf_path.write_bytes(edf_bytes)
        return edf_path

    def test_mode_none_does_not_preserve(self, user, tmp_path):
        from django.core.management import call_command

        source = tmp_path / "source"
        uploads = tmp_path / "uploads"
        originals = tmp_path / "originals"
        for d in (source, uploads, originals):
            d.mkdir()
        self._make_edf_source(source)

        with (
            override_settings(
                RECORDINGS_UPLOAD_PATH=str(uploads),
                RECORDINGS_PRESERVE_MODE=MODE_NONE,
                RECORDINGS_ORIGINALS_PATH=None,
            ),
            patch("notifications.tasks.send_push_to_user.delay"),
        ):
            call_command(
                "import_recordings",
                str(source),
                "--username",
                user.username,
                "--structure",
                "flat",
            )

        assert not originals.exists() or list(originals.iterdir()) == []

    def test_mode_all_preserves_imported_source(self, user, tmp_path):
        from django.core.management import call_command

        source = tmp_path / "source"
        uploads = tmp_path / "uploads"
        originals = tmp_path / "originals"
        for d in (source, uploads, originals):
            d.mkdir()
        src_file = self._make_edf_source(source)
        expected_bytes = src_file.read_bytes()

        with (
            override_settings(
                RECORDINGS_UPLOAD_PATH=str(uploads),
                RECORDINGS_PRESERVE_MODE=MODE_ALL,
                RECORDINGS_ORIGINALS_PATH=str(originals),
            ),
            patch("notifications.tasks.send_push_to_user.delay"),
        ):
            call_command(
                "import_recordings",
                str(source),
                "--username",
                user.username,
                "--structure",
                "flat",
            )

        # One recording should have been imported and preserved.
        recording = Recording.objects.get(author=user)
        target_dir = originals / recording.stored_name.split(".")[0]
        assert (target_dir / "subject_001.edf").read_bytes() == expected_bytes
        manifest = json.loads((target_dir / "manifest.json").read_text())
        assert manifest["preservation_reason"] == REASON_ALL
        assert manifest["recording_pk"] == recording.pk
        # Manifest records the as-uploaded filename, independent of any
        # converter-driven rename of recording.original_name.
        assert manifest["original_name"] == "subject_001.edf"
