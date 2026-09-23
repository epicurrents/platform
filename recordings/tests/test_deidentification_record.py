"""Tests for the per-recording record of the de-identification applied at ingest.

EDPB Guidelines 02/2026 paragraph 41: the controller documents the anonymisation process per
recording and keeps that documentation. The record is two columns stamped on ``RecordingMeta``, a
documentary payload sealed into the READY audit row, and the ``deidentification_report`` command
that presents them. The first class pins the bump discipline for ``DEIDENTIFICATION_VERSION``: a
digest of the two de-identification functions' source is recorded against the version, so a
behaviour change to either fails here until the constant moves with it.
"""

import ast
import hashlib
import inspect
import itertools
import json
import textwrap
from io import StringIO
from unittest.mock import patch

import pytest
from django.contrib.contenttypes.models import ContentType
from django.core.management import CommandError, call_command
from django.test import override_settings

from activity.audit import verify_change_hash
from activity.derived_state import verify_derived_state
from activity.models import Activity, ObjectChangeLog
from epicurrents.models import AccessRight
from recordings.deidentification_record import (
    DEIDENTIFICATION_RECORD_KEY,
    INGEST_OVERRIDE_SETTINGS,
    stored_deidentification_record,
)
from recordings.models import Recording, RecordingMeta
from recordings.processors.channel_labels import CHANNEL_ORDER_VERSION
from recordings.processors.edf import (
    DEIDENTIFICATION_VERSION,
    _build_clean_header,
    deidentify_signal_infos,
    process_edf_file,
)
from recordings.tasks import _save_edf_results, process_recording
from recordings.tests.test_edf_processor import _make_edfplus_file

# One entry per version the pass has had: the sha256 of the two functions' source with docstrings
# and comments removed. A behaviour change to either function changes the digest; add the next
# version's digest here in the commit that bumps DEIDENTIFICATION_VERSION, and never re-pin an
# existing version to a new digest, since recordings stamped with it were written by the old code.
PINNED_SOURCE_DIGESTS = {
    1: "0a9d581ddfab1a3f05ffd1f7b024feff1ae13a4a38302c6173b2f3707d67b4a0",
}

SECRET = b"Seizure onset, patient Doe"


def _normalised(source: str) -> str:
    """Return a function's source with its docstring dropped and formatting canonicalised."""
    node = ast.parse(textwrap.dedent(source)).body[0]
    first = node.body[0] if node.body else None
    if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
        node.body = node.body[1:]
    return ast.unparse(node)


def _source_digest(*sources: str) -> str:
    return hashlib.sha256("\n".join(_normalised(s) for s in sources).encode()).hexdigest()


def current_source_digest() -> str:
    return _source_digest(inspect.getsource(_build_clean_header), inspect.getsource(deidentify_signal_infos))


class TestVersionBumpDiscipline:
    def test_the_version_names_the_current_source(self):
        assert DEIDENTIFICATION_VERSION in PINNED_SOURCE_DIGESTS, (
            f"DEIDENTIFICATION_VERSION is {DEIDENTIFICATION_VERSION} but no source digest is pinned for it; "
            f"add {current_source_digest()!r} under that key"
        )
        assert PINNED_SOURCE_DIGESTS[DEIDENTIFICATION_VERSION] == current_source_digest(), (
            "_build_clean_header or deidentify_signal_infos changed but DEIDENTIFICATION_VERSION did not; bump the "
            f"constant and pin the new digest {current_source_digest()!r} under the new version"
        )

    def test_versions_are_never_reused(self):
        assert len(set(PINNED_SOURCE_DIGESTS.values())) == len(PINNED_SOURCE_DIGESTS)

    def test_the_digest_ignores_docstrings_and_comments(self):
        a = "def f(x):\n    return x + 1\n"
        b = 'def f(x):\n    """Explain."""\n    # and comment\n    return x + 1  # trailing\n'
        c = "def f(x):\n    return x + 2\n"
        assert _source_digest(a) == _source_digest(b)
        assert _source_digest(a) != _source_digest(c)


_stems = itertools.count(1)


def _stage(user, tmp_path, content: bytes, ext: str = ".edf"):
    stem = f"{next(_stems):032X}"
    staging = tmp_path / "staging"
    uploads = tmp_path / "uploads"
    staging.mkdir(parents=True, exist_ok=True)
    uploads.mkdir(parents=True, exist_ok=True)
    staged = staging / f"{stem}{ext}"
    staged.write_bytes(content)
    recording = Recording.objects.create(
        author=user,
        original_name="patient-doe-export" + ext,
        stored_name=f"{stem}{ext}",
        file_extension=ext,
        file_size=len(content),
        file_path=str(staged),
        file_hash=hashlib.sha256(content).hexdigest(),
        content_hash="",
        status=Recording.Status.PENDING,
    )
    return recording, staging, uploads


def _ingest(user, tmp_path, content: bytes | None = None, ext: str = ".edf", **task_kwargs):
    if content is None:
        content = _make_edfplus_file(tals_per_record=[[b"+0.0\x14" + SECRET + b"\x14\x00"]])
    recording, staging, uploads = _stage(user, tmp_path, content, ext=ext)
    with (
        override_settings(RECORDINGS_STAGING_PATH=str(staging), RECORDINGS_UPLOAD_PATH=str(uploads)),
        patch("notifications.tasks.send_push_to_user.delay"),
    ):
        process_recording(recording.pk, **task_kwargs)
    recording.refresh_from_db()
    return recording


def _meta(recording):
    ct = ContentType.objects.get_for_model(recording, for_concrete_model=False)
    return RecordingMeta.objects.get(content_type=ct, object_id=str(recording.pk))


def _final_row(recording):
    return (
        ObjectChangeLog.objects.filter(object_id=str(recording.pk), action=ObjectChangeLog.ACTION_MODIFY)
        .order_by("-created_at", "-pk")
        .first()
    )


def _hash(recording):
    return recording.stored_name.split(".", 1)[0]


@pytest.mark.django_db
class TestStamping:
    def test_ingest_stamps_the_current_pass_and_the_strip_decision(self, user, tmp_path):
        recording = _ingest(user, tmp_path)
        meta = _meta(recording)
        assert meta.deidentification_version == DEIDENTIFICATION_VERSION
        assert meta.annotation_text_preserved is False

    def test_preserving_annotations_is_stamped(self, user, tmp_path):
        recording = _ingest(user, tmp_path, preserve_annotations=True)
        assert _meta(recording).annotation_text_preserved is True

    @override_settings(RECORDINGS_ALLOW_PRESERVE_ANNOTATIONS=False)
    def test_a_refused_preserve_request_is_stamped_as_stripped(self, user, tmp_path):
        # The stamp records what the pass did, not what the caller asked for.
        recording = _ingest(user, tmp_path, preserve_annotations=True)
        assert _meta(recording).annotation_text_preserved is False

    def test_the_stamp_comes_from_the_result_not_the_caller(self, user, tmp_path):
        # A direct persistence call, as the import command makes, stamps from what the
        # pass reports: no separate flag exists to fall out of step with it.
        path = tmp_path / "direct.edf"
        path.write_bytes(_make_edfplus_file(tals_per_record=[[b"+0.0\x14" + SECRET + b"\x14\x00"]]))
        result = process_edf_file(path, strip_annotation_text=False)
        assert result.annotation_text_preserved is True
        recording = Recording.objects.create(
            author=user,
            stored_name="E2E2E2E2E2E2E2E2E2E2E2E2E2E2E2E2.edf",
            file_path=str(path),
            file_extension=".edf",
            file_size=path.stat().st_size,
            status=Recording.Status.READY,
        )
        _save_edf_results(recording, result)
        meta = _meta(recording)
        assert meta.annotation_text_preserved is True
        assert meta.deidentification_version == DEIDENTIFICATION_VERSION

    def test_the_import_command_stamps_the_flag(self, user, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "rec.edf").write_bytes(_make_edfplus_file(tals_per_record=[[b"+0.0\x14" + SECRET + b"\x14\x00"]]))
        with override_settings(RECORDINGS_UPLOAD_PATH=str(tmp_path / "uploads")):
            call_command(
                "import_recordings",
                str(src),
                "--preserve-annotations",
                username=user.username,
                structure="flat",
                stdout=StringIO(),
                stderr=StringIO(),
            )
        recording = Recording.objects.get(author=user)
        meta = _meta(recording)
        assert meta.deidentification_version == DEIDENTIFICATION_VERSION
        assert meta.annotation_text_preserved is True
        assert stored_deidentification_record(recording)["annotation_text_preserved"] is True

    def test_a_refresh_leaves_both_fields_alone(self, user, tmp_path):
        from recordings.metadata import refresh_signal_metadata

        recording = _ingest(user, tmp_path)
        # An older pass wrote this file, and it kept its text: a refresh reads the
        # header and cannot know either, so it must not overwrite them.
        RecordingMeta.objects.update(deidentification_version=0, annotation_text_preserved=True, signal_count=99)
        result = refresh_signal_metadata(recording)
        assert result.changed
        meta = _meta(recording)
        assert meta.deidentification_version == 0
        assert meta.annotation_text_preserved is True


@pytest.mark.django_db
class TestAuditRecord:
    @override_settings(RECORDINGS_DISCARD_ORIGINAL_NAME=True)
    def test_the_ready_row_carries_the_record(self, user, tmp_path):
        recording = _ingest(user, tmp_path)
        record = _final_row(recording).extra_payload[DEIDENTIFICATION_RECORD_KEY]
        assert record == {
            "deidentification_version": DEIDENTIFICATION_VERSION,
            "annotation_text_preserved": False,
            "channel_order_version": CHANNEL_ORDER_VERSION,
            "ingest_overrides": {
                "RECORDINGS_DISCARD_ORIGINAL_NAME": True,
                "RECORDINGS_DISCARD_EMBEDDED_ANNOTATIONS": False,
                "RECORDINGS_DISCARD_SOURCE_CHANNEL_METADATA": False,
                "RECORDINGS_ALLOW_PRESERVE_ANNOTATIONS": True,
            },
        }
        assert set(record["ingest_overrides"]) == set(INGEST_OVERRIDE_SETTINGS)

    def test_the_record_is_read_back_from_the_trail(self, user, tmp_path):
        recording = _ingest(user, tmp_path, preserve_annotations=True)
        assert stored_deidentification_record(recording)["annotation_text_preserved"] is True

    def test_a_recording_without_a_record_reads_as_none(self, user):
        from model_bakery import baker

        assert stored_deidentification_record(baker.make("recordings.Recording", author=user)) is None

    def test_the_record_verifies_as_sealed(self, user, tmp_path):
        recording = _ingest(user, tmp_path)
        result = verify_derived_state(_final_row(recording))
        assert result.ok is True
        assert result.digests == {"signal_info_digest": "ok", DEIDENTIFICATION_RECORD_KEY: "record"}

    def test_editing_the_record_breaks_the_row_hash(self, user, tmp_path):
        recording = _ingest(user, tmp_path)
        row = _final_row(recording)
        assert verify_change_hash(row) is True
        row.extra_payload[DEIDENTIFICATION_RECORD_KEY]["annotation_text_preserved"] = True
        row.save(update_fields=["extra_payload"])
        assert verify_change_hash(row) is False

    def test_a_failed_ingest_records_nothing(self, user, tmp_path):
        recording = _ingest(user, tmp_path, content=b"not an edf file at all " * 20)
        assert recording.status == Recording.Status.FAILED
        assert DEIDENTIFICATION_RECORD_KEY not in _final_row(recording).extra_payload
        assert stored_deidentification_record(recording) is None

    def test_a_format_stored_without_a_pass_records_nothing(self, user, tmp_path):
        recording = _ingest(user, tmp_path, content=b"opaque bytes", ext=".bin")
        assert recording.status == Recording.Status.READY
        assert DEIDENTIFICATION_RECORD_KEY not in _final_row(recording).extra_payload


@pytest.mark.django_db
class TestServed:
    def test_both_fields_reach_a_de_identifying_reader_on_detail_and_slice(self, client, user, make_user, tmp_path):
        recording = _ingest(user, tmp_path, preserve_annotations=True)
        reader = make_user()
        ct = ContentType.objects.get_for_model(recording, for_concrete_model=False)
        AccessRight.objects.create(
            content_type=ct, object_id=str(recording.pk), access_giver=user, access_target=reader, can_read=True
        )
        client.force_login(reader)
        base = f"/recordings/api/v1/{_hash(recording)}"
        detail = client.get(base).json()["meta"]
        assert detail["deidentification_version"] == DEIDENTIFICATION_VERSION
        assert detail["annotation_text_preserved"] is True
        sliced = client.get(f"{base}/slice?t_start=0&t_end=1").json()["meta"]
        assert sliced["deidentification_version"] == DEIDENTIFICATION_VERSION
        assert sliced["annotation_text_preserved"] is True
        assert sliced["channel_order_version"] == CHANNEL_ORDER_VERSION
        assert sliced["channel_layout"] == detail["channel_layout"]


def _run_report(*args, **kwargs) -> str:
    out = StringIO()
    call_command("deidentification_report", *args, stdout=out, stderr=out, **kwargs)
    return out.getvalue()


@pytest.mark.django_db
class TestReportCommand:
    def test_text_report_lists_every_ready_recording_and_flags_the_behind_ones(self, user, tmp_path):
        current = _ingest(user, tmp_path)
        older = _ingest(user, tmp_path / "b")
        RecordingMeta.objects.filter(object_id=str(older.pk)).update(deidentification_version=0)
        out = _run_report()
        assert _hash(current) in out and _hash(older) in out
        assert f"Current pass: de-identification v{DEIDENTIFICATION_VERSION}" in out
        assert "2 recording(s); 1 behind the current pass; 1 processed before the record existed" in out
        older_line = next(line for line in out.splitlines() if line.startswith(_hash(older)))
        assert "behind" in older_line and "unknown" in older_line
        current_line = next(line for line in out.splitlines() if line.startswith(_hash(current)))
        assert "behind" not in current_line and "stripped" in current_line
        assert "RECORDINGS_DISCARD_ORIGINAL_NAME=F" in current_line

    def test_json_report_shape(self, user, tmp_path):
        recording = _ingest(user, tmp_path, preserve_annotations=True)
        report = json.loads(_run_report("--format", "json"))
        assert report["current"]["deidentification_version"] == DEIDENTIFICATION_VERSION
        assert report["current"]["channel_order_version"] == CHANNEL_ORDER_VERSION
        assert set(report["current"]["ingest_overrides"]) == set(INGEST_OVERRIDE_SETTINGS)
        assert report["behind_count"] == 0
        [row] = report["recordings"]
        assert row["hash"] == _hash(recording)
        assert row["deidentification_version"] == DEIDENTIFICATION_VERSION
        assert row["annotation_text_preserved"] is True
        assert row["behind"] is False
        assert row["audit_record"]["ingest_overrides"]["RECORDINGS_ALLOW_PRESERVE_ANNOTATIONS"] is True

    def test_a_pre_record_row_reports_the_flag_as_unknown(self, user, tmp_path):
        _ingest(user, tmp_path)
        RecordingMeta.objects.update(deidentification_version=0, annotation_text_preserved=True)
        [row] = json.loads(_run_report("--format", "json"))["recordings"]
        assert row["deidentification_version"] == 0
        assert row["annotation_text_preserved"] is None
        assert row["behind"] is True

    def test_a_format_stored_without_a_pass_is_not_behind(self, user, tmp_path):
        _ingest(user, tmp_path, content=b"opaque bytes", ext=".bin")
        report = json.loads(_run_report("--format", "json"))
        [row] = report["recordings"]
        assert row["deidentification_version"] is None
        assert row["behind"] is False
        assert report["behind_count"] == 0
        assert "no pass applies" in _run_report()

    def test_one_recording_by_its_url_hash(self, user, tmp_path):
        wanted = _ingest(user, tmp_path)
        _ingest(user, tmp_path / "b")
        report = json.loads(_run_report("--recording", _hash(wanted).lower(), "--format", "json"))
        assert [row["hash"] for row in report["recordings"]] == [_hash(wanted)]

    def test_an_unknown_hash_is_refused(self, user, tmp_path):
        _ingest(user, tmp_path)
        with pytest.raises(CommandError, match="No READY recording"):
            _run_report("--recording", "F" * 32)
        with pytest.raises(CommandError, match="32-character"):
            _run_report("--recording", "short")

    def test_failed_and_trashed_recordings_are_left_out(self, user, tmp_path):
        failed = _ingest(user, tmp_path, content=b"not an edf file at all " * 20)
        assert failed.status == Recording.Status.FAILED
        trashed = _ingest(user, tmp_path / "b")
        from django.utils import timezone

        Recording.objects.filter(pk=trashed.pk).update(deleted_at=timezone.now())
        assert json.loads(_run_report("--format", "json"))["recordings"] == []

    def test_the_run_is_audited_as_a_command(self, user, tmp_path):
        _ingest(user, tmp_path)
        _run_report()
        activity = Activity.objects.get(verb="recordings.deidentification_report")
        assert activity.interface == Activity.Interface.COMMAND
        assert activity.metadata == {"format": "text", "recording_count": 1}

    def test_the_report_names_no_uploaded_filename(self, user, tmp_path):
        _ingest(user, tmp_path)
        for out in (_run_report(), _run_report("--format", "json")):
            assert "patient-doe" not in out
            assert SECRET.decode() not in out
