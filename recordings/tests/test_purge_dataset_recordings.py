"""The dataset withdrawal path: ``purge_dataset_recordings`` by submitted-file hash, reporting per hash."""

from __future__ import annotations

import io
import json

import pytest
from django.contrib.contenttypes.models import ContentType
from django.core.management import call_command
from django.core.management.base import CommandError
from model_bakery import baker

from activity.models import Activity, ObjectChangeLog
from library.models import Dataset, DatasetItem
from recordings.models import Recording

pytestmark = pytest.mark.django_db

DIGEST_A = "a" * 64
DIGEST_B = "b" * 64


def _recording(author, tmp_path, *, name, file_hash):
    path = tmp_path / f"{name}.edf"
    path.write_bytes(b"0" * 16)
    return baker.make(
        Recording,
        author=author,
        stored_name=f"{name:0>32}.edf",
        file_path=str(path),
        file_extension=".edf",
        file_size=16,
        file_hash=file_hash,
        status=Recording.Status.READY,
    )


def _member(dataset, recording):
    ct = ContentType.objects.get_for_model(Recording, for_concrete_model=False)
    return DatasetItem.objects.create(dataset=dataset, content_type=ct, object_id=str(recording.pk))


def _run(*args):
    out, err = io.StringIO(), io.StringIO()
    call_command("purge_dataset_recordings", *args, stdout=out, stderr=err)
    return out.getvalue(), err.getvalue()


class TestPurgeDatasetRecordings:
    def test_purges_members_and_reports_per_hash(self, make_user, tmp_path):
        author = make_user()
        pool = Dataset.objects.create(author=author, name="pool", release_gated=True)
        member = _recording(author, tmp_path, name="A", file_hash=DIGEST_A)
        _member(pool, member)
        out, _ = _run(DIGEST_A, DIGEST_B, "--format", "json")
        report = json.loads(out)
        assert report["counts"] == {"purged": 1, "not found": 1}
        assert [(row["hash"], row["status"]) for row in report["hashes"]] == [
            (DIGEST_A, "purged"),
            (DIGEST_B, "not found"),
        ]
        assert not Recording.objects.filter(pk=member.pk).exists()
        assert not (tmp_path / "A.edf").exists()
        activity = Activity.objects.get(verb="recordings.purge_dataset")
        assert activity.interface == Activity.Interface.COMMAND
        assert activity.metadata["submitted_count"] == 2
        assert ObjectChangeLog.objects.filter(activity=activity, action=ObjectChangeLog.ACTION_DELETE).exists()

    def test_a_recording_outside_a_gated_dataset_is_not_found_and_kept(self, make_user, tmp_path):
        author = make_user()
        own = _recording(author, tmp_path, name="B", file_hash=DIGEST_B)
        open_dataset = Dataset.objects.create(author=author, name="open")
        _member(open_dataset, own)
        out, _ = _run(DIGEST_B)
        assert out.startswith("not found")
        assert Recording.objects.filter(pk=own.pk).exists()
        assert (tmp_path / "B.edf").exists()

    def test_dataset_option_scopes_the_match(self, make_user, tmp_path):
        author = make_user()
        first = Dataset.objects.create(author=author, name="first", release_gated=True)
        second = Dataset.objects.create(author=author, name="second", release_gated=True)
        member = _recording(author, tmp_path, name="A", file_hash=DIGEST_A)
        _member(second, member)
        out, _ = _run("--dataset", first.object_hash, DIGEST_A)
        assert out.startswith("not found")
        assert Recording.objects.filter(pk=member.pk).exists()

    def test_dry_run_deletes_nothing(self, make_user, tmp_path):
        author = make_user()
        pool = Dataset.objects.create(author=author, name="pool", release_gated=True)
        member = _recording(author, tmp_path, name="A", file_hash=DIGEST_A)
        _member(pool, member)
        out, _ = _run(DIGEST_A, "--dry-run")
        assert out.startswith("purged") and "Dry run" in out
        assert Recording.objects.filter(pk=member.pk).exists()
        assert not Activity.objects.filter(verb="recordings.purge_dataset").exists()

    def test_hashes_file_and_invalid_lines(self, make_user, tmp_path):
        author = make_user()
        pool = Dataset.objects.create(author=author, name="pool", release_gated=True)
        member = _recording(author, tmp_path, name="A", file_hash=DIGEST_A)
        _member(pool, member)
        listing = tmp_path / "withdraw.txt"
        listing.write_text(f"{DIGEST_A.upper()}\n\nnot-a-hash\n")
        out, _ = _run("--hashes-file", str(listing), "--format", "json")
        report = json.loads(out)
        assert report["counts"] == {"purged": 1, "invalid": 1}

    def test_unlink_failure_keeps_the_row_and_fails_the_run(self, make_user, tmp_path, monkeypatch):
        author = make_user()
        pool = Dataset.objects.create(author=author, name="pool", release_gated=True)
        member = _recording(author, tmp_path, name="A", file_hash=DIGEST_A)
        _member(pool, member)
        from pathlib import Path

        def refuse(self):
            raise OSError("read-only")

        monkeypatch.setattr(Path, "unlink", refuse)
        with pytest.raises(CommandError, match="could not be purged"):
            _run(DIGEST_A)
        assert Recording.objects.filter(pk=member.pk).exists()

    def test_trashed_members_are_purged_too(self, make_user, tmp_path):
        from django.utils import timezone

        author = make_user()
        pool = Dataset.objects.create(author=author, name="pool", release_gated=True)
        member = _recording(author, tmp_path, name="A", file_hash=DIGEST_A)
        member.deleted_at = timezone.now()
        member.save(update_fields=["deleted_at"])
        _member(pool, member)
        out, _ = _run(DIGEST_A)
        assert out.startswith("purged")
        assert not Recording.objects.filter(pk=member.pk).exists()

    def test_requires_a_hash(self):
        with pytest.raises(CommandError, match="at least one hash"):
            _run()

    def test_never_touches_the_originals_volume(self, make_user, tmp_path, settings):
        originals = tmp_path / "originals"
        originals.mkdir()
        keep = originals / "keep.edf"
        keep.write_bytes(b"orig")
        settings.RECORDINGS_ORIGINALS_PATH = str(originals)
        author = make_user()
        pool = Dataset.objects.create(author=author, name="pool", release_gated=True)
        _member(pool, _recording(author, tmp_path, name="A", file_hash=DIGEST_A))
        _run(DIGEST_A)
        assert keep.read_bytes() == b"orig"


def _spooled(dataset, contributor, tmp_path, *, name, file_hash, status=None):
    from recordings.models import SubmissionFile, SubmissionLedger

    ledger = SubmissionLedger.objects.get_or_create(dataset=dataset, contributor=contributor)[0]
    path = tmp_path / f"{name}.spool"
    path.write_bytes(b"0" * 16)
    return SubmissionFile.objects.create(
        ledger=ledger,
        stored_name=f"{name:0>32}.edf",
        file_extension=".edf",
        file_path=str(path),
        file_size=16,
        file_hash=file_hash,
        sidecar_hash="0" * 64,
        status=status or SubmissionFile.Status.PENDING,
    )


class TestSpooledSubmissions:
    """A file still in a pool's spool is withdrawn by the same hash, at any stage."""

    @pytest.mark.parametrize("status", ["pending", "failed"])
    def test_a_spooled_file_is_purged_and_reported(self, make_user, tmp_path, status):
        from recordings.models import SubmissionFile

        author = make_user()
        pool = Dataset.objects.create(author=author, name="pool", release_gated=True)
        row = _spooled(pool, make_user(), tmp_path, name="S", file_hash=DIGEST_A, status=status)
        out, _ = _run(DIGEST_A, "--format", "json")
        report = json.loads(out)
        assert report["hashes"] == [{"hash": DIGEST_A, "status": "purged", "count": 1}]
        assert not SubmissionFile.objects.filter(pk=row.pk).exists()
        assert not (tmp_path / "S.spool").exists()
        activity = Activity.objects.get(verb="recordings.purge_dataset")
        assert ObjectChangeLog.objects.filter(activity=activity, action=ObjectChangeLog.ACTION_DELETE).exists()

    def test_spool_and_member_under_one_hash_are_both_purged(self, make_user, tmp_path):
        author = make_user()
        pool = Dataset.objects.create(author=author, name="pool", release_gated=True)
        _member(pool, _recording(author, tmp_path, name="A", file_hash=DIGEST_A))
        _spooled(pool, make_user(), tmp_path, name="S", file_hash=DIGEST_A)
        report = json.loads(_run(DIGEST_A, "--format", "json")[0])
        assert report["hashes"] == [{"hash": DIGEST_A, "status": "purged", "count": 2}]

    def test_dry_run_counts_the_spool_and_deletes_nothing(self, make_user, tmp_path):
        from recordings.models import SubmissionFile

        author = make_user()
        pool = Dataset.objects.create(author=author, name="pool", release_gated=True)
        _spooled(pool, make_user(), tmp_path, name="S", file_hash=DIGEST_A)
        out, _ = _run(DIGEST_A, "--dry-run")
        assert out.startswith("purged")
        assert SubmissionFile.objects.count() == 1
        assert (tmp_path / "S.spool").exists()

    def test_dataset_option_scopes_the_spool(self, make_user, tmp_path):
        from recordings.models import SubmissionFile

        author = make_user()
        first = Dataset.objects.create(author=author, name="first", release_gated=True)
        second = Dataset.objects.create(author=author, name="second", release_gated=True)
        _spooled(second, make_user(), tmp_path, name="S", file_hash=DIGEST_A)
        out, _ = _run("--dataset", first.object_hash, DIGEST_A)
        assert out.startswith("not found")
        assert SubmissionFile.objects.count() == 1
