"""Linked recordings: files registered in place by ``import_recordings --link`` and never written by the platform."""

from __future__ import annotations

import hashlib
import os
import time
from io import StringIO
from pathlib import Path

import pytest
from django.contrib.contenttypes.models import ContentType
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings
from django.utils import timezone

from recordings.deidentification_record import (
    LINKED_SOURCE_DEIDENTIFICATION,
    stored_deidentification_record,
)
from recordings.linked import linked_file_change, unlink_recording_file
from recordings.metadata import MetadataRefreshError, refresh_signal_metadata
from recordings.models import ImportJob, ImportJobFile, Recording, RecordingMeta, SignalInfo
from recordings.testing import make_edf_bytes

pytestmark = pytest.mark.django_db

# Out of canonical order on purpose: the ingest pass would put Fp1 before Fp2, and a linked file keeps its own.
LABELS = ("Fp2", "Fp1", "Cz")


def _edf_bytes(labels=LABELS, n_records: int = 2) -> bytes:
    data = bytearray(make_edf_bytes(len(labels), n_records))
    for i, label in enumerate(labels):
        data[256 + i * 16 : 256 + (i + 1) * 16] = label.ljust(16).encode("ascii")
    return bytes(data)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def linked_root(tmp_path):
    root = tmp_path / "linked"
    (root / "study").mkdir(parents=True)
    with override_settings(
        RECORDINGS_LINKED_ROOTS=[str(root)],
        RECORDINGS_UPLOAD_PATH=str(tmp_path / "uploads"),
    ):
        yield root


@pytest.fixture
def source(linked_root):
    path = linked_root / "study" / "rec.edf"
    path.write_bytes(_edf_bytes())
    # Dated in the past, as a file in an existing archive is.
    past = time.time() - 3600
    os.utime(path, (past, past))
    return path


def _link(source_path, username, **extra):
    out = StringIO()
    options = {"structure": "recursive", "link": True, "source_deidentified": True, **extra}
    call_command("import_recordings", str(source_path), username=username, stdout=out, stderr=out, **options)
    return out.getvalue()


def _linked(superuser, linked_root) -> Recording:
    _link(linked_root, superuser.username)
    return Recording.objects.get(author=superuser)


def _meta(recording) -> RecordingMeta:
    ct = ContentType.objects.get_for_model(recording, for_concrete_model=False)
    return RecordingMeta.objects.get(content_type=ct, object_id=str(recording.pk))


def _hash(recording) -> str:
    return recording.stored_name.split(".", 1)[0]


class TestLinking:
    def test_the_file_is_registered_in_place_and_left_untouched(self, superuser, linked_root, source, tmp_path):
        before = (_digest(source), source.stat().st_mtime_ns, source.stat().st_size)
        recording = _linked(superuser, linked_root)

        assert recording.storage == Recording.Storage.LINKED
        assert recording.status == Recording.Status.READY
        assert recording.file_path == str(source.resolve())
        assert recording.linked_at is not None
        assert (_digest(source), source.stat().st_mtime_ns, source.stat().st_size) == before
        uploads = tmp_path / "uploads"
        assert not uploads.exists() or not any(uploads.iterdir())

    def test_both_digests_are_the_digest_of_the_source(self, superuser, linked_root, source):
        recording = _linked(superuser, linked_root)
        assert recording.stored_hash == recording.file_hash == _digest(source)

    def test_no_pass_version_is_stamped_and_the_text_is_kept(self, superuser, linked_root, source):
        meta = _meta(_linked(superuser, linked_root))
        assert meta.deidentification_version == 0
        assert meta.channel_order_version == 0
        assert meta.annotation_text_preserved is True

    def test_channels_keep_the_file_order_with_labels_cleaned_in_the_database(self, superuser, linked_root, source):
        meta = _meta(_linked(superuser, linked_root))
        rows = list(SignalInfo.objects.filter(meta=meta).order_by("index"))
        assert [row.source_label for row in rows] == list(LABELS)
        assert [row.source_index for row in rows] == [0, 1, 2]

    def test_the_sealed_record_names_the_operator_assertion(self, superuser, linked_root, source):
        record = stored_deidentification_record(_linked(superuser, linked_root))
        assert record["storage"] == "linked"
        assert record["source_deidentification"] == LINKED_SOURCE_DEIDENTIFICATION

    def test_the_directory_tree_becomes_collections_as_on_import(self, superuser, linked_root, source):
        from library.models import CollectionItem

        recording = _linked(superuser, linked_root)
        item = CollectionItem.objects.get(object_id=str(recording.pk))
        assert item.collection.name == "study"

    def test_the_job_records_its_mode(self, superuser, linked_root, source):
        _link(linked_root, superuser.username)
        job = ImportJob.objects.get(owner=superuser)
        assert job.link is True

    @override_settings(RECORDINGS_PRESERVE_MODE="all")
    def test_no_preservation_copy_is_written(self, superuser, linked_root, source, tmp_path):
        originals = tmp_path / "originals"
        originals.mkdir()
        with override_settings(RECORDINGS_ORIGINALS_PATH=str(originals)):
            _linked(superuser, linked_root)
        assert not any(originals.iterdir())

    def test_a_file_needing_conversion_is_passed_over(self, superuser, linked_root, source):
        (linked_root / "study" / "trace.csv").write_text("t,v\n0,1\n")
        output = _link(linked_root, superuser.username)
        assert "only EDF/BDF can be linked" in output
        assert Recording.objects.filter(author=superuser).count() == 1

    def test_a_file_already_linked_is_not_linked_twice(self, superuser, linked_root, source):
        _link(linked_root, superuser.username)
        _link(linked_root, superuser.username)
        assert Recording.objects.filter(author=superuser).count() == 1
        latest = ImportJobFile.objects.order_by("-pk").first()
        assert latest.status == ImportJobFile.Status.FAILED
        assert "already linked" in latest.error

    def test_a_file_its_header_does_not_account_for_is_refused(self, superuser, linked_root, source):
        source.write_bytes(_edf_bytes() + b"\x00\x00")
        _link(linked_root, superuser.username)
        assert not Recording.objects.exists()
        assert "do not account for" in ImportJobFile.objects.get().error

    def test_a_file_dated_in_the_future_is_refused(self, superuser, linked_root, source):
        future = time.time() + 3600
        os.utime(source, (future, future))
        _link(linked_root, superuser.username)
        assert not Recording.objects.exists()
        assert "in the future" in ImportJobFile.objects.get().error


class TestLinkPreconditions:
    def test_the_operator_assertion_is_required(self, superuser, linked_root, source):
        with pytest.raises(CommandError, match="--source-deidentified"):
            _link(linked_root, superuser.username, source_deidentified=False)
        assert not ImportJob.objects.exists()

    def test_the_assertion_alone_is_refused(self, superuser, linked_root, source):
        with pytest.raises(CommandError, match="needs --link"):
            _link(linked_root, superuser.username, link=False)

    def test_the_owner_must_be_a_superuser(self, user, linked_root, source):
        with pytest.raises(CommandError, match="superuser"):
            _link(linked_root, user.username)

    def test_a_source_outside_every_root_is_refused(self, superuser, linked_root, tmp_path):
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "rec.edf").write_bytes(_edf_bytes())
        with pytest.raises(CommandError, match="not under a linked root"):
            _link(elsewhere, superuser.username)

    def test_a_symlink_out_of_the_root_is_not_followed(self, superuser, linked_root, tmp_path):
        outside = tmp_path / "outside.edf"
        outside.write_bytes(_edf_bytes())
        (linked_root / "study" / "link.edf").symlink_to(outside)
        _link(linked_root, superuser.username)
        assert not Recording.objects.exists()
        assert "not under any of RECORDINGS_LINKED_ROOTS" in ImportJobFile.objects.get().error

    def test_no_configured_root_refuses(self, superuser, tmp_path):
        with override_settings(RECORDINGS_LINKED_ROOTS=[]):
            with pytest.raises(CommandError, match="names no existing directory"):
                _link(tmp_path, superuser.username)

    @override_settings(RECORDINGS_ALLOW_PRESERVE_ANNOTATIONS=False)
    def test_a_deployment_forbidding_kept_annotation_text_cannot_link(self, superuser, linked_root, source):
        with pytest.raises(CommandError, match="RECORDINGS_ALLOW_PRESERVE_ANNOTATIONS"):
            _link(linked_root, superuser.username)

    def test_a_resumed_job_keeps_its_mode(self, superuser, linked_root, source):
        ImportJob.objects.create(owner=superuser, source_path=str(linked_root), link=False)
        with pytest.raises(CommandError, match="resume it the same way"):
            _link(linked_root, superuser.username, resume=True)


@pytest.fixture
def client_as_superuser(superuser_client):
    return superuser_client[0]


class TestServing:
    def test_the_source_bytes_are_served_unaltered(self, superuser, client_as_superuser, linked_root, source):
        recording = _linked(superuser, linked_root)
        resp = client_as_superuser.get(f"/recordings/api/v1/{_hash(recording)}/file")
        assert resp.status_code == 200
        assert b"".join(resp.streaming_content) == source.read_bytes()

    def test_a_file_modified_after_linking_is_refused(self, superuser, client_as_superuser, linked_root, source):
        recording = _linked(superuser, linked_root)
        later = time.time() + 60
        os.utime(source, (later, later))
        resp = client_as_superuser.get(f"/recordings/api/v1/{_hash(recording)}/file")
        assert resp.status_code == 409
        assert "modified since it was linked" in resp.json()["detail"]

    def test_a_file_of_another_size_is_refused(self, superuser, client_as_superuser, linked_root, source):
        recording = _linked(superuser, linked_root)
        stat = source.stat()
        with source.open("ab") as fh:
            fh.write(b"\x00")
        os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        resp = client_as_superuser.get(f"/recordings/api/v1/{_hash(recording)}/file/slice", {"t_start": 0})
        assert resp.status_code == 409
        assert "changed size" in resp.json()["detail"]

    def test_the_slice_endpoint_serves_an_unchanged_file(self, superuser, client_as_superuser, linked_root, source):
        recording = _linked(superuser, linked_root)
        resp = client_as_superuser.get(f"/recordings/api/v1/{_hash(recording)}/file/slice", {"t_start": 0})
        assert resp.status_code == 200

    def test_a_file_moved_out_from_under_its_root_is_refused(self, superuser, linked_root, source, tmp_path):
        recording = _linked(superuser, linked_root)
        with override_settings(RECORDINGS_LINKED_ROOTS=[str(tmp_path / "another")]):
            assert linked_file_change(recording) == "the linked file is no longer under a linked root"

    def test_a_managed_recording_is_never_checked(self, user, tmp_path):
        recording = Recording(storage=Recording.Storage.MANAGED, file_path=str(tmp_path / "absent.edf"))
        assert linked_file_change(recording) is None


class TestRemoval:
    def test_unlinking_a_linked_recording_leaves_the_file(self, superuser, linked_root, source):
        recording = _linked(superuser, linked_root)
        assert unlink_recording_file(recording) is False
        assert source.exists()

    def test_unlinking_a_managed_recording_removes_the_file(self, tmp_path):
        path = tmp_path / "managed.edf"
        path.write_bytes(b"data")
        assert unlink_recording_file(Recording(file_path=str(path))) is True
        assert not path.exists()

    def test_purge_removes_the_rows_and_keeps_the_file(self, superuser, linked_root, source):
        from recordings.tasks import purge_deleted_recordings

        recording = _linked(superuser, linked_root)
        Recording.objects.filter(pk=recording.pk).update(deleted_at=timezone.now() - timezone.timedelta(days=31))
        with override_settings(RECORDINGS_TRASH_RETENTION_DAYS=30):
            result = purge_deleted_recordings()
        assert result["purged"] == 1
        assert not Recording.objects.filter(pk=recording.pk).exists()
        assert source.exists()

    def test_erasing_the_owner_keeps_the_file(self, make_superuser, linked_root, source):
        owner = make_superuser(username="linker")
        _link(linked_root, owner.username)
        call_command("erase_user", "linker", "--yes", stdout=StringIO())
        assert not Recording.objects.exists()
        assert source.exists()


class TestMetadataRefresh:
    def test_refresh_refuses_a_linked_recording(self, superuser, linked_root, source):
        recording = _linked(superuser, linked_root)
        with pytest.raises(MetadataRefreshError, match="linked"):
            refresh_signal_metadata(recording)

    def test_the_sweep_passes_linked_recordings_over(self, superuser, linked_root, source):
        _linked(superuser, linked_root)
        out = StringIO()
        call_command("refresh_signal_metadata", stdout=out, stderr=out)
        assert "Checked 0 recording(s)" in out.getvalue()


class TestReport:
    def test_the_report_names_a_linked_recording_and_flags_nothing(self, superuser, linked_root, source):
        import json

        recording = _linked(superuser, linked_root)
        out = StringIO()
        call_command("deidentification_report", "--format", "json", stdout=out)
        report = json.loads(out.getvalue())
        (row,) = [r for r in report["recordings"] if r["hash"] == _hash(recording)]
        assert row["storage"] == "linked"
        assert row["source_deidentification"] == LINKED_SOURCE_DEIDENTIFICATION
        assert row["behind"] is False
        assert report["behind_count"] == 0

    def test_the_text_report_counts_linked_recordings(self, superuser, linked_root, source):
        _linked(superuser, linked_root)
        out = StringIO()
        call_command("deidentification_report", stdout=out)
        assert "1 linked in place" in out.getvalue()
        assert "linked, asserted by operator" in out.getvalue()


class TestDigest:
    """A linked file is never rewritten, so its stored digest is the digest of the operator's source file."""

    def _grant(self, recording, giver, target):
        from epicurrents.models import AccessRight

        AccessRight.objects.create(
            content_type=ContentType.objects.get_for_model(recording, for_concrete_model=False),
            object_id=str(recording.pk),
            access_giver=giver,
            access_target=target,
            can_read=True,
        )

    def test_a_grantee_receives_no_digest(self, superuser, make_user, client, linked_root, source):
        recording = _linked(superuser, linked_root)
        grantee = make_user()
        self._grant(recording, superuser, grantee)
        client.force_login(grantee)
        resp = client.get(f"/recordings/api/v1/{_hash(recording)}")
        assert resp.status_code == 200
        assert resp.json()["stored_hash"] == ""

    def test_a_grantee_cannot_test_a_guessed_digest(self, superuser, make_user, client, linked_root, source):
        recording = _linked(superuser, linked_root)
        grantee = make_user()
        self._grant(recording, superuser, grantee)
        client.force_login(grantee)
        resp = client.get(f"/recordings/api/v1/{_hash(recording)}/file", {"expect_stored_hash": _digest(source)})
        assert resp.status_code == 400

    def test_the_author_receives_it(self, superuser, client, linked_root, source):
        recording = _linked(superuser, linked_root)
        client.force_login(superuser)
        resp = client.get(f"/recordings/api/v1/{_hash(recording)}")
        assert resp.json()["stored_hash"] == _digest(source)


class TestAuditTrail:
    def test_the_source_path_never_reaches_the_trail(self, superuser, linked_root, source):
        from activity.models import ObjectChangeLog

        recording = _linked(superuser, linked_root)
        ct = ContentType.objects.get_for_model(recording, for_concrete_model=False)
        rows = ObjectChangeLog.objects.filter(content_type=ct, object_id=str(recording.pk))
        assert rows.exists()
        for row in rows:
            written = str(row.before_state) + str(row.changes) + str(row.extra_payload)
            assert str(source.parent) not in written
            assert "file_path" in written


def test_a_linked_recording_is_named_as_an_upload_is(superuser, linked_root, source):
    import re

    assert re.fullmatch(r"[0-9A-F]{32}\.edf", _linked(superuser, linked_root).stored_name)
