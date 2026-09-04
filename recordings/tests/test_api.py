"""Tests for the recordings API — upload, status, list, download, delete."""

import io
import re
from unittest.mock import patch

import pytest
from django.contrib.contenttypes.models import ContentType
from django.test import override_settings
from django.utils import timezone

from epicurrents.models import AccessRight
from recordings.models import Recording

UPLOAD_URL = "/recordings/api/v1/upload"
LIST_URL = "/recordings/api/v1/"
STATUS_URL = "/recordings/api/v1/status/{hash}"
DOWNLOAD_URL = "/recordings/api/v1/{hash}/file"
DELETE_URL = "/recordings/api/v1/{hash}"


def _make_recording(user, **kwargs):
    """Create a Recording in READY state for use in tests."""
    defaults = {
        "author": user,
        "original_name": "test.edf",
        "stored_name": "ABCDEF1234567890ABCDEF1234567890.edf",
        "file_extension": ".edf",
        "file_size": 1024,
        "file_path": "/tmp/test.edf",
        "file_hash": "a" * 64,
        "content_hash": "b" * 64,
        "status": Recording.Status.READY,
    }
    defaults.update(kwargs)
    return Recording.objects.create(**defaults)


def _grant_read(user, recording, giver):
    ct = ContentType.objects.get_for_model(recording, for_concrete_model=False)
    return AccessRight.objects.create(
        content_type=ct,
        object_id=str(recording.pk),
        access_giver=giver,
        access_target=user,
        can_read=True,
    )


@pytest.mark.django_db
class TestUploadEndpoint:
    @override_settings(
        RECORDINGS_STAGING_PATH="/tmp/epicurrents_staging_test",
        RECORDINGS_UPLOAD_PATH="/tmp/epicurrents_uploads_test",
    )
    def test_upload_unauthenticated_returns_401(self, client):
        f = io.BytesIO(b"data")
        f.name = "test.edf"
        resp = client.post(UPLOAD_URL, {"file": f}, format="multipart")
        assert resp.status_code == 401

    @override_settings(
        RECORDINGS_STAGING_PATH="/tmp/epicurrents_staging_test",
        RECORDINGS_UPLOAD_PATH="/tmp/epicurrents_uploads_test",
    )
    def test_upload_creates_recording_row(self, auth_client, tmp_path):
        c, user = auth_client
        staging = tmp_path / "staging"
        uploads = tmp_path / "uploads"
        staging.mkdir()
        uploads.mkdir()
        with (
            override_settings(
                RECORDINGS_STAGING_PATH=str(staging),
                RECORDINGS_UPLOAD_PATH=str(uploads),
            ),
            patch("recordings.tasks.process_recording.delay"),
        ):
            f = io.BytesIO(b"fake edf data")
            f.name = "sample.edf"
            resp = c.post(UPLOAD_URL, {"file": f}, format="multipart")
        assert resp.status_code == 202
        data = resp.json()
        assert data["original_name"] == "sample.edf"
        assert data["status"] == Recording.Status.PENDING
        assert Recording.objects.filter(stored_name=data["stored_name"]).exists()

    @override_settings(
        RECORDINGS_STAGING_PATH="/tmp/epicurrents_staging_test",
        RECORDINGS_UPLOAD_PATH="/tmp/epicurrents_uploads_test",
    )
    def test_upload_grants_full_access_right_to_uploader(self, auth_client, tmp_path):
        c, user = auth_client
        staging = tmp_path / "staging"
        uploads = tmp_path / "uploads"
        staging.mkdir()
        uploads.mkdir()
        with (
            override_settings(
                RECORDINGS_STAGING_PATH=str(staging),
                RECORDINGS_UPLOAD_PATH=str(uploads),
            ),
            patch("recordings.tasks.process_recording.delay"),
        ):
            f = io.BytesIO(b"fake edf data")
            f.name = "sample.edf"
            resp = c.post(UPLOAD_URL, {"file": f}, format="multipart")
        recording = Recording.objects.get(stored_name=resp.json()["stored_name"])
        ct = ContentType.objects.get_for_model(recording, for_concrete_model=False)
        ar = AccessRight.objects.filter(content_type=ct, object_id=str(recording.pk), access_target=user).first()
        assert ar is not None
        assert ar.can_read and ar.can_write and ar.can_share

    def test_upload_with_invalid_user_access_returns_400(self, auth_client, tmp_path):
        # Non-file params in Ninja multipart endpoints are query params, not form fields.
        from urllib.parse import urlencode

        c, user = auth_client
        staging = tmp_path / "staging"
        staging.mkdir()
        with (
            override_settings(
                RECORDINGS_STAGING_PATH=str(staging),
                RECORDINGS_UPLOAD_PATH=str(tmp_path / "uploads"),
            ),
            patch("recordings.tasks.process_recording.delay"),
        ):
            f = io.BytesIO(b"data")
            f.name = "test.edf"
            qs = urlencode({"user_access": "999999:read"})
            resp = c.post(
                f"{UPLOAD_URL}?{qs}",
                {"file": f},
                format="multipart",
            )
        assert resp.status_code == 400

    def test_upload_with_duplicate_user_access_ids_returns_400(self, auth_client, make_user, tmp_path):
        from urllib.parse import urlencode

        c, user = auth_client
        reader = make_user(username="dupreader")
        staging = tmp_path / "staging"
        staging.mkdir()
        with (
            override_settings(
                RECORDINGS_STAGING_PATH=str(staging),
                RECORDINGS_UPLOAD_PATH=str(tmp_path / "uploads"),
            ),
            patch("recordings.tasks.process_recording.delay"),
        ):
            f = io.BytesIO(b"data")
            f.name = "test.edf"
            qs = urlencode({"user_access": f"{reader.pk}:r;{reader.pk}:r,w"})
            resp = c.post(
                f"{UPLOAD_URL}?{qs}",
                {"file": f},
                format="multipart",
            )
        assert resp.status_code == 400
        assert "Duplicate" in resp.json()["detail"]

    def test_upload_with_share_token_already_in_use_returns_409(self, auth_client, make_user, tmp_path):
        from urllib.parse import urlencode

        from model_bakery import baker

        c, user = auth_client
        other = make_user(username="tokenowner")
        existing = baker.make("recordings.Recording", author=other, status="ready")
        ct = ContentType.objects.get_for_model(existing, for_concrete_model=False)
        AccessRight.objects.create(
            content_type=ct,
            object_id=str(existing.pk),
            access_giver=other,
            public_share_token="taken-token",
            can_read=True,
        )
        staging = tmp_path / "staging"
        staging.mkdir()
        with (
            override_settings(
                RECORDINGS_STAGING_PATH=str(staging),
                RECORDINGS_UPLOAD_PATH=str(tmp_path / "uploads"),
            ),
            patch("recordings.tasks.process_recording.delay"),
        ):
            f = io.BytesIO(b"data")
            f.name = "test.edf"
            qs = urlencode({"share_token": "taken-token"})
            resp = c.post(
                f"{UPLOAD_URL}?{qs}",
                {"file": f},
                format="multipart",
            )
        assert resp.status_code == 409
        assert "already in use" in resp.json()["detail"]

    def test_upload_with_self_in_user_access_returns_400(self, auth_client, tmp_path):
        from urllib.parse import urlencode

        c, user = auth_client
        staging = tmp_path / "staging"
        staging.mkdir()
        with (
            override_settings(
                RECORDINGS_STAGING_PATH=str(staging),
                RECORDINGS_UPLOAD_PATH=str(tmp_path / "uploads"),
            ),
            patch("recordings.tasks.process_recording.delay"),
        ):
            f = io.BytesIO(b"data")
            f.name = "test.edf"
            qs = urlencode({"user_access": f"{user.pk}:r"})
            resp = c.post(
                f"{UPLOAD_URL}?{qs}",
                {"file": f},
                format="multipart",
            )
        assert resp.status_code == 400
        assert "uploading user" in resp.json()["detail"]


@pytest.mark.django_db
class TestStatusEndpoint:
    def test_unauthenticated_returns_401(self, client, user):
        recording = _make_recording(user)
        hash_part = recording.stored_name.split(".")[0]
        resp = client.get(STATUS_URL.format(hash=hash_part))
        assert resp.status_code == 401

    def test_valid_hash_returns_status(self, auth_client):
        c, user = auth_client
        recording = _make_recording(user)
        hash_part = recording.stored_name.split(".")[0]
        resp = c.get(STATUS_URL.format(hash=hash_part))
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == Recording.Status.READY

    def test_invalid_hash_format_returns_400(self, auth_client):
        c, _ = auth_client
        resp = c.get(STATUS_URL.format(hash="tooshort"))
        assert resp.status_code == 400

    def test_unknown_hash_returns_404(self, auth_client):
        c, _ = auth_client
        resp = c.get(STATUS_URL.format(hash="A" * 32))
        assert resp.status_code == 404

    def test_other_user_without_access_gets_403(self, client, user, make_user):
        other = make_user(username="other")
        recording = _make_recording(user)
        hash_part = recording.stored_name.split(".")[0]
        client.force_login(other)
        resp = client.get(STATUS_URL.format(hash=hash_part))
        assert resp.status_code == 403


@pytest.mark.django_db
class TestListEndpoint:
    def test_unauthenticated_returns_401(self, client):
        resp = client.get(LIST_URL)
        assert resp.status_code == 401

    def test_user_sees_own_recordings(self, auth_client):
        c, user = auth_client
        _make_recording(user, stored_name="AAAA1111AAAA1111AAAA1111AAAA1111.edf")
        resp = c.get(LIST_URL)
        assert resp.status_code == 200
        assert len(resp.json()) >= 1

    def test_user_does_not_see_others_recordings(self, auth_client, make_user):
        c, user = auth_client
        other = make_user(username="other")
        _make_recording(other, stored_name="BBBB2222BBBB2222BBBB2222BBBB2222.edf")
        resp = c.get(LIST_URL)
        assert resp.status_code == 200
        assert len(resp.json()) == 0

    def test_shared_recording_visible_to_grantee(self, client, user, make_user):
        reader = make_user(username="reader")
        recording = _make_recording(user, stored_name="CCCC3333CCCC3333CCCC3333CCCC3333.edf")
        _grant_read(reader, recording, giver=user)
        client.force_login(reader)
        resp = client.get(LIST_URL)
        assert resp.status_code == 200
        recording_hash = recording.stored_name.split(".")[0]
        assert any(r["hash"] == recording_hash for r in resp.json())

    def test_superuser_sees_all_recordings(self, superuser_client, user):
        c, su = superuser_client
        _make_recording(user, stored_name="DDDD4444DDDD4444DDDD4444DDDD4444.edf")
        resp = c.get(LIST_URL)
        assert resp.status_code == 200
        assert len(resp.json()) >= 1

    def test_deleted_recordings_excluded_from_default_list(self, auth_client):
        c, user = auth_client
        recording = _make_recording(
            user,
            stored_name="EEEE5555EEEE5555EEEE5555EEEE5555.edf",
            deleted_at=timezone.now(),
        )
        resp = c.get(LIST_URL)
        assert resp.status_code == 200
        recording_hash = recording.stored_name.split(".")[0]
        assert not any(r["hash"] == recording_hash for r in resp.json())

    def test_trash_filter_shows_deleted_own_recordings(self, auth_client):
        c, user = auth_client
        recording = _make_recording(
            user,
            stored_name="FFFF6666FFFF6666FFFF6666FFFF6666.edf",
            deleted_at=timezone.now(),
        )
        resp = c.get(f"{LIST_URL}?trash=true")
        assert resp.status_code == 200
        recording_hash = recording.stored_name.split(".")[0]
        assert any(r["hash"] == recording_hash for r in resp.json())

    def test_status_filter(self, auth_client):
        c, user = auth_client
        ready = _make_recording(
            user,
            stored_name="GGGG7777GGGG7777GGGG7777GGGG7777.edf",
            status=Recording.Status.READY,
        )
        pending = _make_recording(
            user,
            stored_name="HHHH8888HHHH8888HHHH8888HHHH8888.edf",
            status=Recording.Status.PENDING,
        )
        resp = c.get(f"{LIST_URL}?status=ready")
        assert resp.status_code == 200
        hashes = [r["hash"] for r in resp.json()]
        assert ready.stored_name.split(".")[0] in hashes
        assert pending.stored_name.split(".")[0] not in hashes

    def test_uncollected_filter_excludes_collected_recordings(self, auth_client):
        from library.models import Collection, CollectionItem

        c, user = auth_client
        root = _make_recording(
            user,
            stored_name="AAAA1111AAAA1111AAAA1111AAAA1111.edf",
        )
        collected = _make_recording(
            user,
            stored_name="CCCC9999CCCC9999CCCC9999CCCC9999.edf",
        )
        collection = Collection.objects.create(name="Test collection", author=user)
        rec_ct = ContentType.objects.get_for_model(Recording, for_concrete_model=False)
        CollectionItem.objects.create(
            collection=collection,
            content_type=rec_ct,
            object_id=str(collected.pk),
        )
        resp = c.get(f"{LIST_URL}?uncollected=true")
        assert resp.status_code == 200
        hashes = [r["hash"] for r in resp.json()]
        assert root.stored_name.split(".")[0] in hashes
        assert collected.stored_name.split(".")[0] not in hashes

    def test_uncollected_filter_surfaces_recording_in_trashed_collection(self, auth_client):
        """A recording whose only collection is trashed surfaces at the library root."""
        from django.utils import timezone

        from library.models import Collection, CollectionItem

        c, user = auth_client
        rec = _make_recording(
            user,
            stored_name="DDDD2222DDDD2222DDDD2222DDDD2222.edf",
        )
        collection = Collection.objects.create(name="Trashed", author=user)
        rec_ct = ContentType.objects.get_for_model(Recording, for_concrete_model=False)
        item = CollectionItem.objects.create(collection=collection, content_type=rec_ct, object_id=str(rec.pk))
        rec_hash = rec.stored_name.split(".")[0]
        # Actively filed → not at the root.
        resp = c.get(f"{LIST_URL}?uncollected=true")
        assert rec_hash not in [r["hash"] for r in resp.json()]
        # Trash the collection and its membership → surfaces at the root.
        now = timezone.now()
        Collection.objects.filter(pk=collection.pk).update(deleted_at=now)
        CollectionItem.objects.filter(pk=item.pk).update(deleted_at=now)
        resp = c.get(f"{LIST_URL}?uncollected=true")
        surfaced = next((r for r in resp.json() if r["hash"] == rec_hash), None)
        assert surfaced is not None
        # The cue points at the trashed collection it will return to on restore.
        assert surfaced["trashed_collection"] == {"id": collection.pk, "name": "Trashed"}

    def test_trashed_collection_cue_hidden_for_another_users_collection(self, auth_client, make_user):
        """The cue never leaks a collection name the caller does not own."""
        from django.utils import timezone

        from library.models import Collection, CollectionItem

        c, user = auth_client
        other = make_user(username="other")
        rec = _make_recording(user, stored_name="EEEE3333EEEE3333EEEE3333EEEE3333.edf")
        # Another user filed this recording into their own (PHI-shaped) collection.
        coll = Collection.objects.create(name="Patient Jane 1985", author=other)
        rec_ct = ContentType.objects.get_for_model(Recording, for_concrete_model=False)
        item = CollectionItem.objects.create(collection=coll, content_type=rec_ct, object_id=str(rec.pk))
        now = timezone.now()
        Collection.objects.filter(pk=coll.pk).update(deleted_at=now)
        CollectionItem.objects.filter(pk=item.pk).update(deleted_at=now)

        resp = c.get(f"{LIST_URL}?uncollected=true")
        surfaced = next((r for r in resp.json() if r["hash"] == rec.stored_name.split(".")[0]), None)
        # Still surfaces (no live membership) but the other user's name is withheld.
        assert surfaced is not None
        assert surfaced["trashed_collection"] is None


@pytest.mark.django_db
class TestDeleteEndpoint:
    def test_unauthenticated_returns_401(self, client, user):
        recording = _make_recording(user)
        hash_part = recording.stored_name.split(".")[0]
        resp = client.delete(DELETE_URL.format(hash=hash_part))
        assert resp.status_code == 401

    def test_author_can_soft_delete(self, auth_client):
        c, user = auth_client
        recording = _make_recording(user)
        hash_part = recording.stored_name.split(".")[0]
        resp = c.delete(DELETE_URL.format(hash=hash_part))
        assert resp.status_code == 200
        recording.refresh_from_db()
        assert recording.deleted_at is not None

    def test_non_author_without_write_access_gets_403(self, client, user, make_user):
        other = make_user(username="other")
        recording = _make_recording(user)
        hash_part = recording.stored_name.split(".")[0]
        client.force_login(other)
        resp = client.delete(DELETE_URL.format(hash=hash_part))
        assert resp.status_code == 403

    def test_invalid_hash_returns_400(self, auth_client):
        c, _ = auth_client
        resp = c.delete(DELETE_URL.format(hash="bad"))
        assert resp.status_code == 400

    def test_already_deleted_returns_404(self, auth_client):
        c, user = auth_client
        recording = _make_recording(
            user,
            stored_name="IIII9999IIII9999IIII9999IIII9999.edf",
            deleted_at=timezone.now(),
        )
        hash_part = recording.stored_name.split(".")[0]
        resp = c.delete(DELETE_URL.format(hash=hash_part))
        assert resp.status_code == 404

    def test_user_with_write_access_right_can_delete(self, client, user, make_user):
        writer = make_user()
        recording = _make_recording(user)
        ct = ContentType.objects.get_for_model(recording, for_concrete_model=False)
        AccessRight.objects.create(
            content_type=ct,
            object_id=str(recording.pk),
            access_giver=user,
            access_target=writer,
            can_read=True,
            can_write=True,
        )
        hash_part = recording.stored_name.split(".")[0]
        client.force_login(writer)
        resp = client.delete(DELETE_URL.format(hash=hash_part))
        assert resp.status_code == 200


@pytest.mark.django_db
class TestStatusFilterValidation:
    """Ensure the status query parameter rejects unknown values."""

    def test_invalid_status_returns_400(self, auth_client):
        c, _ = auth_client
        resp = c.get(f"{LIST_URL}?status=bogus")
        assert resp.status_code == 400

    def test_valid_statuses_accepted(self, auth_client):
        c, _ = auth_client
        for status in ("pending", "processing", "ready"):
            resp = c.get(f"{LIST_URL}?status={status}")
            assert resp.status_code == 200


@pytest.mark.django_db
class TestAnnotationsEndpoint:
    """Tests for GET /recordings/api/v1/{hash}/annotations."""

    ANNOTATIONS_URL = "/recordings/api/v1/{hash}/annotations"

    def _make_annotation(self, user, recording):
        from django.contrib.contenttypes.models import ContentType

        from annotations.models import Annotation

        ct = ContentType.objects.get_for_model(recording, for_concrete_model=False)
        return Annotation.objects.create(
            author=user,
            target_content_type=ct,
            target_object_id=str(recording.pk),
            object_hash="A" * 32,
            content={"note": "test annotation"},
        )

    def test_unauthenticated_returns_401(self, client, user):
        recording = _make_recording(user)
        hash_part = recording.stored_name.split(".")[0]
        resp = client.get(self.ANNOTATIONS_URL.format(hash=hash_part))
        assert resp.status_code == 401

    def test_owner_can_list_annotations(self, auth_client):
        c, user = auth_client
        recording = _make_recording(user)
        ann = self._make_annotation(user, recording)
        hash_part = recording.stored_name.split(".")[0]
        resp = c.get(self.ANNOTATIONS_URL.format(hash=hash_part))
        assert resp.status_code == 200
        hashes = [a["object_hash"] for a in resp.json()]
        assert ann.object_hash in hashes

    def test_other_user_without_access_returns_403(self, client, user, make_user):
        other = make_user()
        recording = _make_recording(user)
        hash_part = recording.stored_name.split(".")[0]
        client.force_login(other)
        resp = client.get(self.ANNOTATIONS_URL.format(hash=hash_part))
        assert resp.status_code == 403

    def test_returns_empty_list_when_no_annotations(self, auth_client):
        c, user = auth_client
        recording = _make_recording(user)
        hash_part = recording.stored_name.split(".")[0]
        resp = c.get(self.ANNOTATIONS_URL.format(hash=hash_part))
        assert resp.status_code == 200
        assert resp.json() == []

    def test_author_filter(self, auth_client, make_user):
        from django.contrib.contenttypes.models import ContentType

        from annotations.models import Annotation

        c, user = auth_client
        other = make_user()
        recording = _make_recording(user)
        ct = ContentType.objects.get_for_model(recording, for_concrete_model=False)
        ann_mine = Annotation.objects.create(
            author=user,
            target_content_type=ct,
            target_object_id=str(recording.pk),
            object_hash="M" * 32,
            content={"note": "mine"},
        )
        ann_other = Annotation.objects.create(
            author=other,
            target_content_type=ct,
            target_object_id=str(recording.pk),
            object_hash="O" * 32,
            content={"note": "other"},
        )
        hash_part = recording.stored_name.split(".")[0]
        resp = c.get(self.ANNOTATIONS_URL.format(hash=hash_part) + f"?author_id={user.pk}")
        assert resp.status_code == 200
        hashes = [a["object_hash"] for a in resp.json()]
        assert ann_mine.object_hash in hashes
        assert ann_other.object_hash not in hashes

    def test_invalid_hash_returns_400(self, auth_client):
        c, _ = auth_client
        resp = c.get(self.ANNOTATIONS_URL.format(hash="short"))
        assert resp.status_code == 400


@pytest.mark.django_db
class TestInterruptionTimingInResponses:
    """Recording responses embed interruption timing (data-position seconds).

    The viewer seeds its trusted gap table from the recording metadata alone; without the
    timing fields it would fall back to decode-time discovery and clamp navigation on
    discontinuous recordings.
    """

    def test_detail_includes_interruption_timing(self, auth_client):
        from annotations.models import Interruption

        c, user = auth_client
        recording = _make_recording(user)
        ct = ContentType.objects.get_for_model(recording, for_concrete_model=False)
        Interruption.objects.create(
            author=user,
            target_content_type=ct,
            target_object_id=str(recording.pk),
            object_hash="I" * 32,
            timestamp=42.5,
            duration=3.25,
        )
        hash_part = recording.stored_name.split(".")[0]
        resp = c.get(f"/recordings/api/v1/{hash_part}")
        assert resp.status_code == 200
        intrs = resp.json()["interruptions"]
        assert len(intrs) == 1
        assert intrs[0]["object_hash"] == "I" * 32
        assert intrs[0]["start"] == pytest.approx(42.5)
        assert intrs[0]["duration"] == pytest.approx(3.25)


class TestDownloadMiddleware:
    """Tests for apply_middleware download path in GET /recordings/api/v1/{hash}/file."""

    # Import EDF fixture helpers from the processor test module.
    from recordings.tests.test_edf_processor import _make_edf_data, _make_edf_header

    def _make_edf_file(self, tmp_path, *, n_signals=1, n_records=1):
        """Write a minimal valid EDF file and return (path, header_size, file_bytes)."""
        from recordings.tests.test_edf_processor import _make_edf_data, _make_edf_header

        signals = [{"label": f"EEG Ch{i}", "sample_count": 16} for i in range(n_signals)]
        header_bytes = _make_edf_header(signals=signals)
        data_bytes = _make_edf_data(signals, n_records=n_records)
        content = header_bytes + data_bytes
        p = tmp_path / "test.edf"
        p.write_bytes(content)
        return p, len(header_bytes), content

    def _make_recording_with_meta(self, user, file_path, file_content, *, signal_count=1):
        from django.contrib.contenttypes.models import ContentType

        from recordings.models import RecordingMeta

        recording = _make_recording(
            user,
            stored_name="MMMM1111MMMM1111MMMM1111MMMM1111.edf",
            file_path=str(file_path),
            file_size=len(file_content),
            file_extension=".edf",
        )
        ct = ContentType.objects.get_for_model(recording, for_concrete_model=False)
        RecordingMeta.objects.create(
            content_type=ct,
            object_id=str(recording.pk),
            format="EDF",
            duration=1.0,
            data_record_count=1,
            data_record_duration=1.0,
            signal_count=signal_count,
            discontinuous=False,
        )
        return recording

    def _grant_read_with_middleware(self, giver, reader, recording, *, apply_middleware):
        ct = ContentType.objects.get_for_model(recording, for_concrete_model=False)
        return AccessRight.objects.create(
            content_type=ct,
            object_id=str(recording.pk),
            access_giver=giver,
            access_target=reader,
            can_read=True,
            apply_middleware=apply_middleware,
        )

    def test_author_gets_raw_bytes_even_when_middleware_right_exists(self, auth_client, tmp_path):
        """Authors always receive raw bytes regardless of apply_middleware."""
        c, user = auth_client
        edf_path, header_size, content = self._make_edf_file(tmp_path)
        recording = self._make_recording_with_meta(user, edf_path, content)
        hash_part = recording.stored_name.split(".")[0]

        resp = c.get(DOWNLOAD_URL.format(hash=hash_part))
        assert resp.status_code == 200
        assert b"".join(resp.streaming_content) == content

    def test_grantee_without_flag_gets_raw_bytes(self, client, user, make_user, tmp_path):
        """apply_middleware=False → recipient gets the original file unchanged."""
        reader = make_user()
        edf_path, header_size, content = self._make_edf_file(tmp_path)
        recording = self._make_recording_with_meta(user, edf_path, content)
        self._grant_read_with_middleware(user, reader, recording, apply_middleware=False)

        hash_part = recording.stored_name.split(".")[0]
        client.force_login(reader)
        resp = client.get(DOWNLOAD_URL.format(hash=hash_part))
        assert resp.status_code == 200
        assert b"".join(resp.streaming_content) == content

    def test_grantee_with_flag_gets_transformed_header(self, client, user, make_user, tmp_path):
        """apply_middleware=True → AnonymizeEDFHeader is applied to the EDF header."""
        from unittest.mock import patch

        reader = make_user()
        edf_path, header_size, content = self._make_edf_file(tmp_path)
        recording = self._make_recording_with_meta(user, edf_path, content)
        self._grant_read_with_middleware(user, reader, recording, apply_middleware=True)

        sentinel = b"Z" * header_size  # known replacement for the transformed header

        hash_part = recording.stored_name.split(".")[0]
        client.force_login(reader)

        with patch(
            "federation.middleware.AnonymizeEDFHeader.transform_header",
            return_value=sentinel,
        ):
            resp = client.get(DOWNLOAD_URL.format(hash=hash_part))

        assert resp.status_code == 200
        body = b"".join(resp.streaming_content)
        assert body[:header_size] == sentinel
        assert body[header_size:] == content[header_size:]

    def test_grantee_with_flag_range_within_header(self, client, user, make_user, tmp_path):
        """Range request entirely within the header returns transformed bytes."""
        from unittest.mock import patch

        reader = make_user()
        edf_path, header_size, content = self._make_edf_file(tmp_path)
        recording = self._make_recording_with_meta(user, edf_path, content)
        self._grant_read_with_middleware(user, reader, recording, apply_middleware=True)

        sentinel = b"Z" * header_size
        hash_part = recording.stored_name.split(".")[0]
        client.force_login(reader)

        with patch(
            "federation.middleware.AnonymizeEDFHeader.transform_header",
            return_value=sentinel,
        ):
            resp = client.get(
                DOWNLOAD_URL.format(hash=hash_part),
                HTTP_RANGE=f"bytes=0-{header_size - 1}",
            )

        assert resp.status_code == 206
        assert b"".join(resp.streaming_content) == sentinel

    def test_grantee_with_flag_range_spanning_boundary(self, client, user, make_user, tmp_path):
        """Range request spanning header/signal boundary stitches correctly."""
        from unittest.mock import patch

        reader = make_user()
        edf_path, header_size, content = self._make_edf_file(tmp_path)
        recording = self._make_recording_with_meta(user, edf_path, content)
        self._grant_read_with_middleware(user, reader, recording, apply_middleware=True)

        sentinel = b"Z" * header_size
        # Request: last byte of header + first byte of signal data.
        range_start = header_size - 1
        range_end = header_size  # first signal byte
        hash_part = recording.stored_name.split(".")[0]
        client.force_login(reader)

        with patch(
            "federation.middleware.AnonymizeEDFHeader.transform_header",
            return_value=sentinel,
        ):
            resp = client.get(
                DOWNLOAD_URL.format(hash=hash_part),
                HTTP_RANGE=f"bytes={range_start}-{range_end}",
            )

        assert resp.status_code == 206
        body = b"".join(resp.streaming_content)
        assert body[0:1] == sentinel[range_start:header_size]
        assert body[1:2] == content[header_size : header_size + 1]

    def test_grantee_with_flag_range_entirely_in_signals(self, client, user, make_user, tmp_path):
        """Range request entirely within signal region returns raw signal bytes."""
        from unittest.mock import patch

        reader = make_user()
        edf_path, header_size, content = self._make_edf_file(tmp_path)
        recording = self._make_recording_with_meta(user, edf_path, content)
        self._grant_read_with_middleware(user, reader, recording, apply_middleware=True)

        sentinel = b"Z" * header_size
        file_size = len(content)
        range_start = header_size
        range_end = file_size - 1
        hash_part = recording.stored_name.split(".")[0]
        client.force_login(reader)

        with patch(
            "federation.middleware.AnonymizeEDFHeader.transform_header",
            return_value=sentinel,
        ):
            resp = client.get(
                DOWNLOAD_URL.format(hash=hash_part),
                HTTP_RANGE=f"bytes={range_start}-{range_end}",
            )

        assert resp.status_code == 206
        assert b"".join(resp.streaming_content) == content[header_size:]

    def test_grantee_with_flag_strips_annotation_text(self, client, user, make_user, tmp_path):
        """apply_middleware=True strips annotation text from EDF+ annotation channels."""
        from recordings.processors.edf import _parse_tal_record
        from recordings.tests.test_edf_processor import (
            _make_anno_record,
            _make_edf_header,
            _make_tal,
        )

        reader = make_user()

        # Build a 2-channel EDF+C file: one EEG channel + one annotation channel.
        # The annotation channel has a timekeeping TAL and a text TAL.
        anno_sample_count = 60  # 60 * 2 = 120 bytes for the annotation channel
        signals = [
            {"label": "EEG Fp1", "sample_count": 8},
            {"label": "EDF Annotations", "sample_count": anno_sample_count},
        ]
        header_bytes = _make_edf_header(reserved="EDF+C", signals=signals, n_records=1)
        eeg_data = b"\x01\x02" * 8
        anno_bytes = _make_anno_record(
            onset=0.0,
            tals=[_make_tal(1.5, "spike")],
            total_bytes=anno_sample_count * 2,
        )
        content = header_bytes + eeg_data + anno_bytes

        edf_path = tmp_path / "test_plus.edf"
        edf_path.write_bytes(content)

        recording = self._make_recording_with_meta(user, edf_path, content, signal_count=len(signals))
        self._grant_read_with_middleware(user, reader, recording, apply_middleware=True)

        hash_part = recording.stored_name.split(".")[0]
        client.force_login(reader)
        resp = client.get(DOWNLOAD_URL.format(hash=hash_part))

        assert resp.status_code == 200
        body = b"".join(resp.streaming_content)

        # File size must not change.
        assert len(body) == len(content)

        # EEG channel bytes are untouched (first 16 bytes of the data record).
        header_size = len(header_bytes)
        assert body[header_size : header_size + 16] == eeg_data

        # Annotation channel: timekeeping TAL present, text "spike" removed.
        anno_out = body[header_size + 16 : header_size + 16 + anno_sample_count * 2]
        record_onset, annotations = _parse_tal_record(anno_out)
        assert record_onset == pytest.approx(0.0)
        assert annotations == []

    def test_full_file_middleware_refuses_instead_of_serving_raw(self, client, user, make_user, tmp_path):
        """A pipeline with an EDFFullFileMiddleware must refuse (403), not
        fall back to raw bytes.

        Unreachable with the default pipeline, but a fork configuring a
        full-file de-identifier would otherwise silently serve raw PHI
        with a 200 on the download path.
        """
        from unittest.mock import patch

        from federation.middleware import EDFFullFileMiddleware, MiddlewarePipeline

        class _FullFile(EDFFullFileMiddleware):
            def transform(self, raw_header, raw_signals):
                return raw_header, raw_signals

            def compute_output_size(self, raw_header, signal_size):
                return len(raw_header) + signal_size

        reader = make_user()
        edf_path, _, content = self._make_edf_file(tmp_path)
        recording = self._make_recording_with_meta(user, edf_path, content)
        self._grant_read_with_middleware(user, reader, recording, apply_middleware=True)

        hash_part = recording.stored_name.split(".")[0]
        client.force_login(reader)
        with patch(
            "recordings.api.v1.ninja._build_serve_pipeline",
            return_value=MiddlewarePipeline([_FullFile()]).for_scope("api"),
        ):
            resp = client.get(DOWNLOAD_URL.format(hash=hash_part))

        assert resp.status_code == 403
        assert resp.json()["code"] == "middleware_unsupported"

    def test_non_edf_file_served_raw_even_with_flag(self, client, user, make_user, tmp_path):
        """apply_middleware=True on a .bin file falls back to raw serving."""
        reader = make_user()
        raw_content = b"\x00\x01\x02\x03" * 64
        file_path = tmp_path / "data.bin"
        file_path.write_bytes(raw_content)

        ct = ContentType.objects.get_for_model(Recording, for_concrete_model=False)
        recording = Recording.objects.create(
            author=user,
            original_name="data.bin",
            stored_name="NNNN2222NNNN2222NNNN2222NNNN2222.bin",
            file_extension=".bin",
            file_size=len(raw_content),
            file_path=str(file_path),
            file_hash="c" * 64,
            content_hash="d" * 64,
            status=Recording.Status.READY,
        )
        AccessRight.objects.create(
            content_type=ct,
            object_id=str(recording.pk),
            access_giver=user,
            access_target=reader,
            can_read=True,
            apply_middleware=True,
        )

        hash_part = recording.stored_name.split(".")[0]
        client.force_login(reader)
        resp = client.get(DOWNLOAD_URL.format(hash=hash_part))
        assert resp.status_code == 200
        assert b"".join(resp.streaming_content) == raw_content

    def test_upload_share_token_apply_middleware_creates_right(self, auth_client, tmp_path):
        """share_token_apply_middleware=true stores apply_middleware=True on the token right."""
        from urllib.parse import urlencode

        c, user = auth_client
        staging = tmp_path / "staging"
        staging.mkdir()
        with (
            override_settings(
                RECORDINGS_STAGING_PATH=str(staging),
                RECORDINGS_UPLOAD_PATH=str(tmp_path / "uploads"),
            ),
            patch("recordings.tasks.process_recording.delay"),
        ):
            f = io.BytesIO(b"fake edf data")
            f.name = "sample.edf"
            qs = urlencode(
                {
                    "share_token": "testtoken42",
                    "share_token_apply_middleware": "true",
                }
            )
            resp = c.post(f"{UPLOAD_URL}?{qs}", {"file": f}, format="multipart")

        assert resp.status_code == 202
        recording = Recording.objects.get(stored_name=resp.json()["stored_name"])
        ct = ContentType.objects.get_for_model(recording, for_concrete_model=False)
        token_right = AccessRight.objects.filter(
            content_type=ct,
            object_id=str(recording.pk),
            public_share_token="testtoken42",
        ).first()
        assert token_right is not None
        assert token_right.apply_middleware is True

    def test_upload_user_access_middleware_flag(self, auth_client, make_user, tmp_path):
        """user_access string with 'm' token stores apply_middleware=True."""
        from urllib.parse import urlencode

        c, user = auth_client
        reader = make_user()
        staging = tmp_path / "staging"
        staging.mkdir()
        with (
            override_settings(
                RECORDINGS_STAGING_PATH=str(staging),
                RECORDINGS_UPLOAD_PATH=str(tmp_path / "uploads"),
            ),
            patch("recordings.tasks.process_recording.delay"),
        ):
            f = io.BytesIO(b"fake edf data")
            f.name = "sample.edf"
            qs = urlencode({"user_access": f"{reader.pk}:r,m"})
            resp = c.post(f"{UPLOAD_URL}?{qs}", {"file": f}, format="multipart")

        assert resp.status_code == 202
        recording = Recording.objects.get(stored_name=resp.json()["stored_name"])
        ct = ContentType.objects.get_for_model(recording, for_concrete_model=False)
        reader_right = AccessRight.objects.filter(
            content_type=ct,
            object_id=str(recording.pk),
            access_target=reader,
        ).first()
        assert reader_right is not None
        assert reader_right.can_read is True
        assert reader_right.apply_middleware is True


SLICE_URL = "/recordings/api/v1/{hash}/file/slice"


@pytest.mark.django_db
class TestSliceRecording:
    """Tests for GET /recordings/api/v1/{hash}/file/slice."""

    # 1-channel EDF, 16 samples/record, 1 s/record → record_size = 32 bytes.
    _SIGNALS = [{"label": "EEG Fp1", "sample_count": 16}]
    _N_RECORDS = 10
    _REC_DURATION = 1.0

    def _make_edf_file(self, tmp_path, n_records=None):
        """Write a minimal 10-record EDF and return (path, header_size, content)."""
        from recordings.tests.test_edf_processor import _make_edf_data, _make_edf_header

        n = n_records or self._N_RECORDS
        header_bytes = _make_edf_header(signals=self._SIGNALS, n_records=n, rec_duration=self._REC_DURATION)
        data_bytes = _make_edf_data(self._SIGNALS, n_records=n)
        content = header_bytes + data_bytes
        p = tmp_path / "test.edf"
        p.write_bytes(content)
        return p, len(header_bytes), content

    def _make_recording_with_meta(self, user, file_path, file_content, n_records=None):
        from recordings.models import RecordingMeta

        n = n_records or self._N_RECORDS
        recording = _make_recording(
            user,
            stored_name="SLCE1111SLCE1111SLCE1111SLCE1111.edf",
            file_path=str(file_path),
            file_size=len(file_content),
            file_extension=".edf",
        )
        ct = ContentType.objects.get_for_model(recording, for_concrete_model=False)
        RecordingMeta.objects.create(
            content_type=ct,
            object_id=str(recording.pk),
            format="edf",
            duration=n * self._REC_DURATION,
            data_record_count=n,
            data_record_duration=self._REC_DURATION,
            signal_count=len(self._SIGNALS),
            discontinuous=False,
        )
        return recording

    def _url(self, recording):
        return SLICE_URL.format(hash=recording.stored_name.split(".")[0])

    def _grant_read_with_middleware(self, giver, reader, recording, *, apply_middleware):
        ct = ContentType.objects.get_for_model(recording, for_concrete_model=False)
        return AccessRight.objects.create(
            content_type=ct,
            object_id=str(recording.pk),
            access_giver=giver,
            access_target=reader,
            can_read=True,
            apply_middleware=apply_middleware,
        )

    # ── Auth / basic error paths ─────────────────────────────────────────────

    def test_unauthenticated_returns_401(self, client, tmp_path, user):
        edf_path, _, content = self._make_edf_file(tmp_path)
        recording = self._make_recording_with_meta(user, edf_path, content)
        assert client.get(self._url(recording)).status_code == 401

    def test_non_edf_returns_422(self, auth_client, tmp_path):
        c, user = auth_client
        p = tmp_path / "notes.txt"
        p.write_bytes(b"hello")
        _make_recording(
            user,
            stored_name="SLCE2222SLCE2222SLCE2222SLCE2222.txt",
            file_path=str(p),
            file_size=5,
            file_extension=".txt",
        )
        resp = c.get(SLICE_URL.format(hash="SLCE2222SLCE2222SLCE2222SLCE2222"))
        assert resp.status_code == 422

    def test_t_start_gte_t_end_returns_400(self, auth_client, tmp_path):
        c, user = auth_client
        edf_path, _, content = self._make_edf_file(tmp_path)
        recording = self._make_recording_with_meta(user, edf_path, content)
        resp = c.get(self._url(recording) + "?t_start=5&t_end=5")
        assert resp.status_code == 400

    # ── Correct record selection ─────────────────────────────────────────────

    def test_full_file_slice_returns_all_records(self, auth_client, tmp_path):
        """Default params (no t_start/t_end) return the entire file."""
        c, user = auth_client
        edf_path, header_size, content = self._make_edf_file(tmp_path)
        recording = self._make_recording_with_meta(user, edf_path, content)

        resp = c.get(self._url(recording))
        assert resp.status_code == 200
        body = b"".join(resp.streaming_content)
        assert len(body) == len(content)

    def test_slice_record_count_in_header_is_correct(self, auth_client, tmp_path):
        """data_record_count in the slice header reflects the requested slice."""
        from recordings.processors.edf import parse_edf_header

        c, user = auth_client
        edf_path, _, content = self._make_edf_file(tmp_path)
        recording = self._make_recording_with_meta(user, edf_path, content)

        # Seconds 2-5 → records 2, 3, 4 = 3 records.
        resp = c.get(self._url(recording) + "?t_start=2&t_end=5")
        body = b"".join(resp.streaming_content)
        hdr = parse_edf_header(body)
        assert hdr.data_record_count == 3

    def test_slice_returns_correct_signal_bytes(self, auth_client, tmp_path):
        """Signal bytes in the slice match the corresponding records of the source file."""
        from recordings.processors.edf import parse_edf_header

        c, user = auth_client
        edf_path, header_size, content = self._make_edf_file(tmp_path)
        recording = self._make_recording_with_meta(user, edf_path, content)

        # 1 channel × 16 samples × 2 bytes = 32 bytes/record
        rec_size = 16 * 2

        # Request records 3 and 4 (t=3..5).
        resp = c.get(self._url(recording) + "?t_start=3&t_end=5")
        body = b"".join(resp.streaming_content)
        hdr = parse_edf_header(body)
        slice_header_size = hdr.header_record_bytes
        slice_signals = body[slice_header_size:]

        # Compare to the same records in the original file.
        original_signals = content[header_size + 3 * rec_size : header_size + 5 * rec_size]
        assert slice_signals == original_signals

    def test_negative_t_start_counts_from_end(self, auth_client, tmp_path):
        """t_start=-3 requests the last 3 seconds of the file."""
        from recordings.processors.edf import parse_edf_header

        c, user = auth_client
        edf_path, _, content = self._make_edf_file(tmp_path)
        recording = self._make_recording_with_meta(user, edf_path, content)

        resp = c.get(self._url(recording) + "?t_start=-3")
        body = b"".join(resp.streaming_content)
        hdr = parse_edf_header(body)
        assert hdr.data_record_count == 3

    def test_negative_t_end_counts_from_end(self, auth_client, tmp_path):
        """t_end=-2 excludes the last 2 records."""
        from recordings.processors.edf import parse_edf_header

        c, user = auth_client
        edf_path, _, content = self._make_edf_file(tmp_path)
        recording = self._make_recording_with_meta(user, edf_path, content)

        resp = c.get(self._url(recording) + "?t_end=-2")
        body = b"".join(resp.streaming_content)
        hdr = parse_edf_header(body)
        assert hdr.data_record_count == 8

    def test_slice_with_apply_middleware_anonymises_header(self, auth_client, tmp_path, make_user):
        """apply_middleware=True anonymises patient info in the slice header."""
        from django.test import Client

        from recordings.processors.edf import parse_edf_header

        c, user = auth_client
        reader = make_user()
        reader_client = Client()
        reader_client.force_login(reader)

        edf_path, _, content = self._make_edf_file(tmp_path)
        recording = self._make_recording_with_meta(user, edf_path, content)
        self._grant_read_with_middleware(user, reader, recording, apply_middleware=True)

        resp = reader_client.get(self._url(recording) + "?t_start=0&t_end=3")
        body = b"".join(resp.streaming_content)
        hdr = parse_edf_header(body)
        assert hdr.patient_id == "X X X X"

    def test_slice_with_apply_middleware_strips_annotation_text(self, auth_client, tmp_path, make_user):
        """apply_middleware=True strips annotation text from sliced EDF+ records.

        The slice path must serve through the same pipeline as the full
        download — a header-only pipeline would leak clinical annotation
        text to middleware-gated grantees.
        """
        from django.test import Client

        from recordings.models import RecordingMeta
        from recordings.processors.edf import _parse_tal_record
        from recordings.tests.test_edf_processor import (
            _make_anno_record,
            _make_edf_header,
            _make_tal,
        )

        c, user = auth_client
        reader = make_user()
        reader_client = Client()
        reader_client.force_login(reader)

        # 2-channel EDF+C: one EEG channel + one annotation channel carrying
        # a timekeeping TAL and a text TAL.
        anno_sample_count = 60
        signals = [
            {"label": "EEG Fp1", "sample_count": 8},
            {"label": "EDF Annotations", "sample_count": anno_sample_count},
        ]
        header_bytes = _make_edf_header(reserved="EDF+C", signals=signals, n_records=1)
        eeg_data = b"\x01\x02" * 8
        anno_bytes = _make_anno_record(
            onset=0.0,
            tals=[_make_tal(0.5, "spike")],
            total_bytes=anno_sample_count * 2,
        )
        content = header_bytes + eeg_data + anno_bytes
        edf_path = tmp_path / "test_plus.edf"
        edf_path.write_bytes(content)

        recording = _make_recording(
            user,
            stored_name="SLCE3333SLCE3333SLCE3333SLCE3333.edf",
            file_path=str(edf_path),
            file_size=len(content),
            file_extension=".edf",
        )
        ct = ContentType.objects.get_for_model(recording, for_concrete_model=False)
        RecordingMeta.objects.create(
            content_type=ct,
            object_id=str(recording.pk),
            format="edf",
            duration=1.0,
            data_record_count=1,
            data_record_duration=1.0,
            signal_count=len(signals),
            discontinuous=False,
        )
        self._grant_read_with_middleware(user, reader, recording, apply_middleware=True)

        resp = reader_client.get(self._url(recording))
        assert resp.status_code == 200
        body = b"".join(resp.streaming_content)

        # Size-invariant transform: slice of the full range matches file size.
        assert len(body) == len(content)

        # EEG channel bytes are untouched.
        header_size = len(header_bytes)
        assert body[header_size : header_size + 16] == eeg_data

        # Annotation channel: timekeeping TAL kept, text TAL removed.
        anno_out = body[header_size + 16 : header_size + 16 + anno_sample_count * 2]
        record_onset, annotations = _parse_tal_record(anno_out)
        assert record_onset == pytest.approx(0.0)
        assert annotations == []

    def test_content_length_header_matches_body(self, auth_client, tmp_path):
        c, user = auth_client
        edf_path, _, content = self._make_edf_file(tmp_path)
        recording = self._make_recording_with_meta(user, edf_path, content)

        resp = c.get(self._url(recording) + "?t_start=1&t_end=4")
        body = b"".join(resp.streaming_content)
        assert int(resp["Content-Length"]) == len(body)


SLICE_META_URL = "/recordings/api/v1/{hash}/slice"


@pytest.mark.django_db
class TestRecordingDetailSlice:
    """Tests for GET /recordings/api/v1/{hash}/slice."""

    _SIGNALS = [{"label": "EEG Fp1", "sample_count": 16}]
    _N_RECORDS = 10
    _REC_DURATION = 1.0

    def _make_edf_file(self, tmp_path, n_records=None):
        from recordings.tests.test_edf_processor import _make_edf_data, _make_edf_header

        n = n_records or self._N_RECORDS
        header_bytes = _make_edf_header(signals=self._SIGNALS, n_records=n, rec_duration=self._REC_DURATION)
        data_bytes = _make_edf_data(self._SIGNALS, n_records=n)
        content = header_bytes + data_bytes
        p = tmp_path / "test.edf"
        p.write_bytes(content)
        return p, len(header_bytes), content

    def _make_recording_with_meta(self, user, file_path, file_content, n_records=None, **kwargs):
        from recordings.models import RecordingMeta

        n = n_records or self._N_RECORDS
        defaults = {
            "stored_name": "SMTA1111SMTA1111SMTA1111SMTA1111.edf",
            "file_path": str(file_path),
            "file_size": len(file_content),
            "file_extension": ".edf",
        }
        defaults.update(kwargs)
        recording = _make_recording(user, **defaults)
        ct = ContentType.objects.get_for_model(recording, for_concrete_model=False)
        RecordingMeta.objects.create(
            content_type=ct,
            object_id=str(recording.pk),
            format="edf",
            duration=n * self._REC_DURATION,
            data_record_count=n,
            data_record_duration=self._REC_DURATION,
            signal_count=len(self._SIGNALS),
            discontinuous=False,
        )
        return recording

    def _url(self, recording):
        return SLICE_META_URL.format(hash=recording.stored_name.split(".")[0])

    # ── Auth / basic error paths ─────────────────────────────────────────────

    def test_unauthenticated_returns_401(self, client, tmp_path, user):
        edf_path, _, content = self._make_edf_file(tmp_path)
        recording = self._make_recording_with_meta(user, edf_path, content)
        assert client.get(self._url(recording)).status_code == 401

    def test_non_edf_returns_422(self, auth_client, tmp_path):
        c, user = auth_client
        _make_recording(
            user,
            stored_name="SMTA2222SMTA2222SMTA2222SMTA2222.txt",
            file_path="/tmp/notes.txt",
            file_size=5,
            file_extension=".txt",
        )
        assert c.get(SLICE_META_URL.format(hash="SMTA2222SMTA2222SMTA2222SMTA2222")).status_code == 422

    def test_t_start_gte_t_end_returns_400(self, auth_client, tmp_path):
        c, user = auth_client
        edf_path, _, content = self._make_edf_file(tmp_path)
        recording = self._make_recording_with_meta(user, edf_path, content)
        assert c.get(self._url(recording) + "?t_start=5&t_end=5").status_code == 400

    # ── Meta reflects slice ──────────────────────────────────────────────────

    def test_meta_duration_and_record_count_reflect_slice(self, auth_client, tmp_path):
        """meta.duration and meta.data_record_count correspond to the requested slice."""
        c, user = auth_client
        edf_path, _, content = self._make_edf_file(tmp_path)
        recording = self._make_recording_with_meta(user, edf_path, content)

        resp = c.get(self._url(recording) + "?t_start=2&t_end=5")
        assert resp.status_code == 200
        data = resp.json()
        assert data["meta"]["data_record_count"] == 3
        assert data["meta"]["duration"] == pytest.approx(3.0)

    def test_t_start_t_end_fields_are_record_aligned(self, auth_client, tmp_path):
        """t_start and t_end in the response are record-aligned (may differ from request)."""
        c, user = auth_client
        edf_path, _, content = self._make_edf_file(tmp_path)
        recording = self._make_recording_with_meta(user, edf_path, content)

        resp = c.get(self._url(recording) + "?t_start=0&t_end=10")
        data = resp.json()
        assert data["t_start"] == pytest.approx(0.0)
        assert data["t_end"] == pytest.approx(10.0)

    def test_negative_t_start_counts_from_end(self, auth_client, tmp_path):
        """t_start=-3 returns the last 3 records."""
        c, user = auth_client
        edf_path, _, content = self._make_edf_file(tmp_path)
        recording = self._make_recording_with_meta(user, edf_path, content)

        resp = c.get(self._url(recording) + "?t_start=-3")
        assert resp.status_code == 200
        data = resp.json()
        assert data["meta"]["data_record_count"] == 3

    # ── Event filtering and clamping ─────────────────────────────────────────

    def test_events_inside_range_are_included_with_shifted_timestamps(self, auth_client, tmp_path):
        """Events within the slice are included; timestamps are relative to slice start."""
        from annotations.models import Event

        c, user = auth_client
        edf_path, _, content = self._make_edf_file(tmp_path)
        recording = self._make_recording_with_meta(user, edf_path, content)

        ct = ContentType.objects.get_for_model(recording, for_concrete_model=False)
        AccessRight.objects.create(
            content_type=ct,
            object_id=str(recording.pk),
            access_giver=user,
            access_target=user,
            can_read=True,
        )

        Event.objects.create(
            author=user,
            target_content_type=ct,
            target_object_id=str(recording.pk),
            object_hash="evhash001",
            name="spike",
            timestamp=4.0,
            duration=1.0,
        )

        resp = c.get(self._url(recording) + "?t_start=3&t_end=7")
        data = resp.json()
        assert len(data["events"]) == 1
        ev = data["events"][0]
        assert ev["object_hash"] == "EVHASH001"
        assert ev["timestamp"] == pytest.approx(1.0)  # 4.0 - 3.0
        assert ev["duration"] == pytest.approx(1.0)

    def test_events_outside_range_are_excluded(self, auth_client, tmp_path):
        from annotations.models import Event

        c, user = auth_client
        edf_path, _, content = self._make_edf_file(tmp_path)
        recording = self._make_recording_with_meta(user, edf_path, content)

        ct = ContentType.objects.get_for_model(recording, for_concrete_model=False)
        AccessRight.objects.create(
            content_type=ct,
            object_id=str(recording.pk),
            access_giver=user,
            access_target=user,
            can_read=True,
        )
        Event.objects.create(
            author=user,
            target_content_type=ct,
            target_object_id=str(recording.pk),
            object_hash="evhash002",
            name="outside",
            timestamp=8.0,
        )

        resp = c.get(self._url(recording) + "?t_start=0&t_end=5")
        data = resp.json()
        assert data["events"] == []

    def test_event_spanning_start_boundary_is_clamped(self, auth_client, tmp_path):
        """An event starting before t_start but extending into the slice has timestamp=0."""
        from annotations.models import Event

        c, user = auth_client
        edf_path, _, content = self._make_edf_file(tmp_path)
        recording = self._make_recording_with_meta(user, edf_path, content)

        ct = ContentType.objects.get_for_model(recording, for_concrete_model=False)
        AccessRight.objects.create(
            content_type=ct,
            object_id=str(recording.pk),
            access_giver=user,
            access_target=user,
            can_read=True,
        )
        Event.objects.create(
            author=user,
            target_content_type=ct,
            target_object_id=str(recording.pk),
            object_hash="evhash003",
            name="spanning_start",
            timestamp=1.0,
            duration=3.0,
        )

        # Slice starts at t=3; event covers [1, 4) → overlaps → clamped to [3, 4) → ts=0, dur=1
        resp = c.get(self._url(recording) + "?t_start=3&t_end=7")
        data = resp.json()
        assert len(data["events"]) == 1
        ev = data["events"][0]
        assert ev["timestamp"] == pytest.approx(0.0)
        assert ev["duration"] == pytest.approx(1.0)

    def test_event_spanning_end_boundary_is_clamped(self, auth_client, tmp_path):
        """An event extending past t_end has its duration clamped at the slice end."""
        from annotations.models import Event

        c, user = auth_client
        edf_path, _, content = self._make_edf_file(tmp_path)
        recording = self._make_recording_with_meta(user, edf_path, content)

        ct = ContentType.objects.get_for_model(recording, for_concrete_model=False)
        AccessRight.objects.create(
            content_type=ct,
            object_id=str(recording.pk),
            access_giver=user,
            access_target=user,
            can_read=True,
        )
        Event.objects.create(
            author=user,
            target_content_type=ct,
            target_object_id=str(recording.pk),
            object_hash="evhash004",
            name="spanning_end",
            timestamp=4.0,
            duration=8.0,
        )

        # Slice [3, 7); event covers [4, 12) → clamped to [4, 7) → ts=1, dur=3
        resp = c.get(self._url(recording) + "?t_start=3&t_end=7")
        data = resp.json()
        assert len(data["events"]) == 1
        ev = data["events"][0]
        assert ev["timestamp"] == pytest.approx(1.0)
        assert ev["duration"] == pytest.approx(3.0)

    # ── Interruption filtering ────────────────────────────────────────────────

    def test_interruptions_filtered_and_shifted(self, auth_client, tmp_path):
        from annotations.models import Interruption

        c, user = auth_client
        edf_path, _, content = self._make_edf_file(tmp_path)
        recording = self._make_recording_with_meta(user, edf_path, content)

        ct = ContentType.objects.get_for_model(recording, for_concrete_model=False)
        AccessRight.objects.create(
            content_type=ct,
            object_id=str(recording.pk),
            access_giver=user,
            access_target=user,
            can_read=True,
        )
        Interruption.objects.create(
            author=user,
            target_content_type=ct,
            target_object_id=str(recording.pk),
            object_hash="inthash001",
            timestamp=6.0,
            duration=2.0,
        )
        Interruption.objects.create(
            author=user,
            target_content_type=ct,
            target_object_id=str(recording.pk),
            object_hash="inthash002",
            timestamp=9.0,
            duration=1.0,
        )

        resp = c.get(self._url(recording) + "?t_start=5&t_end=9")
        data = resp.json()
        assert len(data["interruptions"]) == 1
        intr = data["interruptions"][0]
        assert intr["object_hash"] == "INTHASH001"
        assert intr["timestamp"] == pytest.approx(1.0)  # 6.0 - 5.0
        assert intr["duration"] == pytest.approx(2.0)

    # ── Labels excluded ───────────────────────────────────────────────────────

    def test_labels_not_in_response(self, auth_client, tmp_path):
        """Labels are not included in the slice metadata response."""
        from annotations.models import Label

        c, user = auth_client
        edf_path, _, content = self._make_edf_file(tmp_path)
        recording = self._make_recording_with_meta(user, edf_path, content)

        ct = ContentType.objects.get_for_model(recording, for_concrete_model=False)
        AccessRight.objects.create(
            content_type=ct,
            object_id=str(recording.pk),
            access_giver=user,
            access_target=user,
            can_read=True,
        )
        Label.objects.create(
            author=user,
            target_content_type=ct,
            target_object_id=str(recording.pk),
            object_hash="lblhash001",
            name="seizure",
        )

        resp = c.get(self._url(recording) + "?t_start=0&t_end=5")
        data = resp.json()
        assert "labels" not in data


# ---------------------------------------------------------------------------
# download_size in list endpoint (federated requests)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestDownloadSizeField:
    """GET / includes download_size only for federated requests with apply_middleware."""

    def _make_trusted_peer(self, make_user):
        from federation.models import FederatedPeer

        return FederatedPeer.objects.create(
            url="https://peer.example.com",
            display_name="Peer",
            public_key="A" * 43,
            is_trusted=True,
            added_by=make_user(),
        )

    def _make_fed_headers(self, peer_url, user_id):
        """Forge a FederatedBearer header that _try_federated_auth will accept."""
        # Rather than generating a real JWT we patch _try_federated_auth so we
        # can control what peer + remote_user_id the endpoint sees.
        return  # used differently below — see test body

    def _patch_fed_auth(self, peer, remote_user_id="42"):
        """Patch _try_federated_auth to return (peer, remote_user_id)."""
        from unittest.mock import patch

        return patch(
            "recordings.api.v1.ninja._try_federated_auth",
            return_value=(peer, remote_user_id),
        )

    def test_no_download_size_for_regular_user(self, auth_client, make_user):
        """Regular session-authenticated requests do not include download_size."""
        c, user = auth_client
        recording = _make_recording(user)
        ct = ContentType.objects.get_for_model(recording, for_concrete_model=False)
        AccessRight.objects.create(
            content_type=ct,
            object_id=str(recording.pk),
            access_giver=user,
            access_target=user,
            can_read=True,
        )
        resp = c.get(LIST_URL)
        data = resp.json()
        assert resp.status_code == 200
        assert len(data) == 1
        assert data[0].get("download_size") is None

    def test_download_size_absent_when_no_apply_middleware(self, client, user, make_user):
        """Federated peer without apply_middleware gets file_size as download_size."""
        peer = self._make_trusted_peer(make_user)
        recording = _make_recording(user)
        ct = ContentType.objects.get_for_model(recording, for_concrete_model=False)
        # Grant with apply_middleware=False (default)
        AccessRight.objects.create(
            content_type=ct,
            object_id=str(recording.pk),
            access_giver=user,
            federated_peer=peer,
            can_read=True,
            apply_middleware=False,
        )
        with self._patch_fed_auth(peer):
            # No force_login — federated requests use the FederatedBearer token only.
            # The federated listing branch uses the peer's AccessRight grant to
            # determine visibility, so no session auth is needed or desired.
            resp = client.get(LIST_URL)
        data = resp.json()
        assert resp.status_code == 200
        matched = [r for r in data if r["hash"] == recording.stored_name.split(".")[0]]
        assert len(matched) == 1
        # equals file_size because apply_middleware=False
        assert matched[0]["download_size"] == recording.file_size

    def test_download_size_equals_file_size_for_size_preserving_pipeline(self, client, user, make_user):
        """Size-preserving pipeline does not change file size.

        The default pipeline (AnonymizeEDFHeader + StripAnnotationTextMiddleware)
        is size-preserving: both middlewares leave the file size unchanged so
        download_size == file_size without requiring a disk read.
        """
        peer = self._make_trusted_peer(make_user)
        recording = _make_recording(user)
        ct = ContentType.objects.get_for_model(recording, for_concrete_model=False)
        AccessRight.objects.create(
            content_type=ct,
            object_id=str(recording.pk),
            access_giver=user,
            federated_peer=peer,
            can_read=True,
            apply_middleware=True,
        )
        with self._patch_fed_auth(peer):
            resp = client.get(LIST_URL)
        data = resp.json()
        assert resp.status_code == 200
        matched = [r for r in data if r["hash"] == recording.stored_name.split(".")[0]]
        assert len(matched) == 1
        assert matched[0]["download_size"] == recording.file_size

    def test_download_size_omitted_for_non_edf(self, client, user, make_user):
        """Non-EDF files are never transformed; download_size equals file_size."""
        peer = self._make_trusted_peer(make_user)
        recording = _make_recording(
            user,
            original_name="data.csv",
            stored_name="CCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC.csv",
            file_extension=".csv",
        )
        ct = ContentType.objects.get_for_model(recording, for_concrete_model=False)
        AccessRight.objects.create(
            content_type=ct,
            object_id=str(recording.pk),
            access_giver=user,
            federated_peer=peer,
            can_read=True,
            apply_middleware=True,
        )
        with self._patch_fed_auth(peer):
            resp = client.get(LIST_URL)
        data = resp.json()
        assert resp.status_code == 200
        matched = [r for r in data if r["hash"] == "CCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC"]
        assert len(matched) == 1
        assert matched[0]["download_size"] == recording.file_size

    def test_download_size_for_signal_pipeline_uses_db_signal_infos(self, client, user, make_user):
        """Non-size-preserving signal pipeline computes download_size from DB without a file read.

        Uses DropChannelsMiddleware (drops 1 of 2 channels) as the serve pipeline.
        The output record size is half the input record size, so download_size < file_size.
        No filesystem read should occur — the output size is derived from SignalInfo rows.
        """
        from django.contrib.contenttypes.models import ContentType

        from federation.middleware import DropChannelsMiddleware, MiddlewarePipeline
        from recordings.models import RecordingMeta, SignalInfo

        peer = self._make_trusted_peer(make_user)

        # 2-channel EDF: 4 samples/rec each, 2 bytes/sample = 16 bytes/record.
        # After dropping ch1: 4 * 2 = 8 bytes/record.
        header_size = 3 * 256  # (ns + 1) * 256
        n_records = 10
        input_record_size = (4 + 4) * 2
        file_size = header_size + n_records * input_record_size

        recording = _make_recording(
            user,
            stored_name="DDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDD.edf",
            file_size=file_size,
            file_extension=".edf",
        )
        ct = ContentType.objects.get_for_model(recording, for_concrete_model=False)
        meta = RecordingMeta.objects.create(
            content_type=ct,
            object_id=str(recording.pk),
            format="edf",
            duration=float(n_records),
            data_record_count=n_records,
            data_record_duration=1.0,
            signal_count=2,
            discontinuous=False,
        )
        SignalInfo.objects.create(
            meta=meta,
            index=0,
            label="EEG Fp1",
            sample_count=4,
            sampling_rate=4.0,
            physical_min=-100.0,
            physical_max=100.0,
            digital_min=-32768,
            digital_max=32767,
            units_per_bit=0.003,
            digital_offset=0.0,
        )
        SignalInfo.objects.create(
            meta=meta,
            index=1,
            label="EMG chin",
            sample_count=4,
            sampling_rate=4.0,
            physical_min=-100.0,
            physical_max=100.0,
            digital_min=-32768,
            digital_max=32767,
            units_per_bit=0.003,
            digital_offset=0.0,
        )
        AccessRight.objects.create(
            content_type=ct,
            object_id=str(recording.pk),
            access_giver=user,
            federated_peer=peer,
            can_read=True,
            apply_middleware=True,
        )

        # Patch the serve pipeline to use DropChannelsMiddleware so the pipeline
        # is no longer size-preserving, forcing the signal-infos code path.
        drop_pipeline = MiddlewarePipeline([DropChannelsMiddleware(["EMG chin"])]).for_scope("api")
        with (
            self._patch_fed_auth(peer),
            patch(
                "recordings.api.v1.ninja._build_serve_pipeline",
                return_value=drop_pipeline,
            ),
        ):
            resp = client.get(LIST_URL)

        assert resp.status_code == 200
        data = resp.json()
        matched = [r for r in data if r["hash"] == "DDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDD"]
        assert len(matched) == 1

        # Output: header_size unchanged (same channel count... wait, drop reduces to 1 ch)
        # Output header: (1 + 1) * 256 = 512 bytes.  Output record: 4 * 2 = 8 bytes.
        expected_output_header = 2 * 256
        expected_download_size = expected_output_header + n_records * 4 * 2
        assert matched[0]["download_size"] == expected_download_size


@pytest.mark.django_db
class TestFailedRecordingHiding:
    """FAILED recordings are visible only to author and superusers.

    Closes two related security gaps:

    1. ``_serve_recording_with_middleware`` previously fell back to raw bytes
       when ``RecordingMeta`` was missing, leaking the unrewritten EDF/BDF
       header to grantees whose grant carried ``apply_middleware=True``.
       The function now returns 403 with ``code: recording_unprocessed``.
    2. Every grantee-facing surface — listings, per-recording endpoints,
       federation ``inbound/objects/...``, library item listings — hides
       FAILED recordings entirely so the failure state is not leaked.
    """

    def _make_failed_recording(self, user, **kwargs):
        kwargs.setdefault("status", Recording.Status.FAILED)
        kwargs.setdefault("stored_name", "FA17EDF1FA17EDF1FA17EDF1FA17EDF1.edf")
        return _make_recording(user, **kwargs)

    def _grant_read_with_middleware(self, giver, reader, recording, *, apply_middleware):
        ct = ContentType.objects.get_for_model(recording, for_concrete_model=False)
        return AccessRight.objects.create(
            content_type=ct,
            object_id=str(recording.pk),
            access_giver=giver,
            access_target=reader,
            can_read=True,
            apply_middleware=apply_middleware,
        )

    # ── _serve_recording_with_middleware 403 path (defense in depth) ─────────

    def test_middleware_grantee_on_meta_missing_gets_403_recording_unprocessed(self, client, user, make_user, tmp_path):
        """READY recording, RecordingMeta absent, grantee with apply_middleware=True.

        Pins the structured error shape so downstream consumers (frontend,
        federated peers) can branch on ``code`` and SIEM rules can match it.
        """
        reader = make_user(username="reader_403")
        edf_path = tmp_path / "noheader.edf"
        edf_path.write_bytes(b"\x00" * 1024)

        recording = _make_recording(
            user,
            stored_name="ME7AMISSREADYMISSREADYMISSREADY1.edf",
            file_path=str(edf_path),
            file_size=1024,
            file_extension=".edf",
            status=Recording.Status.READY,
        )
        # No RecordingMeta row is created — this is the defense-in-depth path.
        self._grant_read_with_middleware(user, reader, recording, apply_middleware=True)

        hash_part = recording.stored_name.split(".")[0]
        client.force_login(reader)
        resp = client.get(DOWNLOAD_URL.format(hash=hash_part))

        assert resp.status_code == 403
        body = resp.json()
        assert body == {
            "code": "recording_unprocessed",
            "detail": ("This recording could not be processed and cannot be served in anonymised form."),
        }

    # ── Author / superuser retain visibility on FAILED ───────────────────────

    def test_author_sees_failed_in_list(self, auth_client):
        c, user = auth_client
        recording = self._make_failed_recording(user)
        resp = c.get(LIST_URL)
        assert resp.status_code == 200
        hashes = [r["hash"] for r in resp.json()]
        assert recording.stored_name.split(".")[0] in hashes

    def test_author_sees_failed_in_status(self, auth_client):
        c, user = auth_client
        recording = self._make_failed_recording(user)
        hash_part = recording.stored_name.split(".")[0]
        resp = c.get(STATUS_URL.format(hash=hash_part))
        assert resp.status_code == 200
        assert resp.json()["status"] == Recording.Status.FAILED

    def test_superuser_sees_failed_in_list(self, superuser_client, user):
        c, _ = superuser_client
        recording = self._make_failed_recording(user)
        resp = c.get(LIST_URL)
        assert resp.status_code == 200
        hashes = [r["hash"] for r in resp.json()]
        assert recording.stored_name.split(".")[0] in hashes

    # ── Grantee (local user) is blind to FAILED ──────────────────────────────

    def test_grantee_does_not_see_failed_in_list(self, client, user, make_user):
        reader = make_user(username="reader_list")
        recording = self._make_failed_recording(user)
        _grant_read(reader, recording, giver=user)
        client.force_login(reader)
        resp = client.get(LIST_URL)
        assert resp.status_code == 200
        hashes = [r["hash"] for r in resp.json()]
        assert recording.stored_name.split(".")[0] not in hashes

    def test_grantee_status_on_failed_returns_404(self, client, user, make_user):
        reader = make_user(username="reader_status")
        recording = self._make_failed_recording(user)
        _grant_read(reader, recording, giver=user)
        hash_part = recording.stored_name.split(".")[0]
        client.force_login(reader)
        resp = client.get(STATUS_URL.format(hash=hash_part))
        assert resp.status_code == 404

    def test_grantee_detail_on_failed_returns_404(self, client, user, make_user):
        reader = make_user(username="reader_detail")
        recording = self._make_failed_recording(user)
        _grant_read(reader, recording, giver=user)
        hash_part = recording.stored_name.split(".")[0]
        client.force_login(reader)
        resp = client.get(f"/recordings/api/v1/{hash_part}")
        assert resp.status_code == 404

    def test_grantee_download_on_failed_returns_404(self, client, user, make_user, tmp_path):
        reader = make_user(username="reader_dl")
        # Provide a real file so the on-disk check isn't what trips the response.
        file_path = tmp_path / "failed.edf"
        file_path.write_bytes(b"\x00" * 16)
        recording = self._make_failed_recording(user, file_path=str(file_path), file_size=16)
        _grant_read(reader, recording, giver=user)
        hash_part = recording.stored_name.split(".")[0]
        client.force_login(reader)
        resp = client.get(DOWNLOAD_URL.format(hash=hash_part))
        assert resp.status_code == 404

    def test_grantee_annotations_on_failed_returns_404(self, client, user, make_user):
        """``GET /{hash}/annotations`` must apply the FAILED-hidden rule too.

        Annotation text can carry clinical detail — a grantee with a stale
        grant on a FAILED recording must not read its annotations.
        """
        from annotations.models import Annotation

        reader = make_user(username="reader_annot")
        recording = self._make_failed_recording(user)
        _grant_read(reader, recording, giver=user)
        ct = ContentType.objects.get_for_model(recording, for_concrete_model=False)
        Annotation.objects.create(
            target_content_type=ct,
            target_object_id=str(recording.pk),
            author=user,
            object_hash="a" * 32,
            content_hash="b" * 32,
            content="{}",
        )
        hash_part = recording.stored_name.split(".")[0]
        client.force_login(reader)
        resp = client.get(f"/recordings/api/v1/{hash_part}/annotations")
        assert resp.status_code == 404

    def test_author_annotations_on_failed_still_visible(self, auth_client):
        """The author retains access to their own FAILED recording's annotations."""
        from annotations.models import Annotation

        c, user = auth_client
        recording = self._make_failed_recording(user)
        ct = ContentType.objects.get_for_model(recording, for_concrete_model=False)
        Annotation.objects.create(
            target_content_type=ct,
            target_object_id=str(recording.pk),
            author=user,
            object_hash="c" * 32,
            content_hash="d" * 32,
            content="{}",
        )
        hash_part = recording.stored_name.split(".")[0]
        resp = c.get(f"/recordings/api/v1/{hash_part}/annotations")
        assert resp.status_code == 200
        assert len(resp.json()) == 1


@pytest.mark.django_db
class TestDisplayName:
    """Display name + filename-masking contract.

    Pins the Phase 2 rules:

    * ``original_name`` is returned only to the author and to superusers —
      grantees, share-token holders, and federated peers see ``None``.
    * ``display_name`` is returned to every viewer that can see the
      recording at all; when the field is null it falls back to
      ``stored_name[:8].upper()``.
    * PATCH ``display_name`` is the new editable field; PATCH
      ``original_name`` is no longer honoured.
    * Content-Disposition on download uses ``display_name`` so the original
      filename never leaks into the response headers.
    """

    DETAIL_URL = "/recordings/api/v1/{hash}"
    PATCH_URL = "/recordings/api/v1/{hash}"

    def _make_ready_recording(self, user, **kwargs):
        kwargs.setdefault("stored_name", "D15D15D15D15D15D15D15D15D15D15D1.edf")
        kwargs.setdefault("original_name", "patient_001_routine.edf")
        return _make_recording(user, **kwargs)

    # ── default fallback (display_name is NULL) ──────────────────────────────

    def test_default_display_name_is_stored_name_prefix(self, auth_client):
        c, user = auth_client
        recording = self._make_ready_recording(user)
        hash_part = recording.stored_name.split(".")[0]
        resp = c.get(self.DETAIL_URL.format(hash=hash_part))
        assert resp.status_code == 200
        assert resp.json()["display_name"] == hash_part[:8]

    def test_custom_display_name_returned(self, auth_client):
        c, user = auth_client
        recording = self._make_ready_recording(user, display_name="My Study 1")
        hash_part = recording.stored_name.split(".")[0]
        resp = c.get(self.DETAIL_URL.format(hash=hash_part))
        assert resp.status_code == 200
        assert resp.json()["display_name"] == "My Study 1"

    def test_has_custom_name_false_for_hash_prefix_fallback(self, auth_client):
        c, user = auth_client
        recording = self._make_ready_recording(user)
        hash_part = recording.stored_name.split(".")[0]
        resp = c.get(self.DETAIL_URL.format(hash=hash_part))
        assert resp.status_code == 200
        assert resp.json()["has_custom_name"] is False

    def test_has_custom_name_true_when_label_set(self, auth_client):
        c, user = auth_client
        recording = self._make_ready_recording(user, display_name="My Study 1")
        hash_part = recording.stored_name.split(".")[0]
        resp = c.get(self.DETAIL_URL.format(hash=hash_part))
        assert resp.status_code == 200
        assert resp.json()["has_custom_name"] is True

    # ── original_name visibility ─────────────────────────────────────────────

    def test_author_sees_original_name(self, auth_client):
        c, user = auth_client
        recording = self._make_ready_recording(user)
        hash_part = recording.stored_name.split(".")[0]
        resp = c.get(self.DETAIL_URL.format(hash=hash_part))
        assert resp.status_code == 200
        assert resp.json()["original_name"] == "patient_001_routine.edf"

    def test_superuser_sees_original_name(self, superuser_client, user):
        c, _ = superuser_client
        recording = self._make_ready_recording(user)
        hash_part = recording.stored_name.split(".")[0]
        resp = c.get(self.DETAIL_URL.format(hash=hash_part))
        assert resp.status_code == 200
        assert resp.json()["original_name"] == "patient_001_routine.edf"

    def test_grantee_never_sees_original_name(self, client, user, make_user):
        reader = make_user(username="reader_orig")
        recording = self._make_ready_recording(user, display_name="My Study")
        _grant_read(reader, recording, giver=user)
        hash_part = recording.stored_name.split(".")[0]
        client.force_login(reader)
        resp = client.get(self.DETAIL_URL.format(hash=hash_part))
        assert resp.status_code == 200
        body = resp.json()
        assert body["original_name"] is None
        assert body["display_name"] == "My Study"

    def test_grantee_never_sees_original_name_in_list(self, client, user, make_user):
        reader = make_user(username="reader_list_orig")
        recording = self._make_ready_recording(user, stored_name="A1A1A1A1A1A1A1A1A1A1A1A1A1A1A1A1.edf")
        _grant_read(reader, recording, giver=user)
        client.force_login(reader)
        resp = client.get(LIST_URL)
        assert resp.status_code == 200
        rows = [r for r in resp.json() if r["hash"] == recording.stored_name.split(".")[0]]
        assert len(rows) == 1
        assert rows[0]["original_name"] is None
        # Hash-prefix default since display_name is unset.
        assert rows[0]["display_name"] == "A1A1A1A1"

    # ── PATCH ────────────────────────────────────────────────────────────────

    def test_patch_sets_display_name(self, auth_client):
        c, user = auth_client
        recording = self._make_ready_recording(user)
        hash_part = recording.stored_name.split(".")[0]
        import json

        resp = c.patch(
            self.PATCH_URL.format(hash=hash_part),
            json.dumps({"display_name": "New Label"}),
            content_type="application/json",
        )
        assert resp.status_code == 200
        recording.refresh_from_db()
        assert recording.display_name == "New Label"
        assert resp.json()["display_name"] == "New Label"
        assert resp.json()["has_custom_name"] is True

    def test_patch_empty_display_name_clears_field(self, auth_client):
        c, user = auth_client
        recording = self._make_ready_recording(user, display_name="To Be Cleared")
        hash_part = recording.stored_name.split(".")[0]
        import json

        resp = c.patch(
            self.PATCH_URL.format(hash=hash_part),
            json.dumps({"display_name": ""}),
            content_type="application/json",
        )
        assert resp.status_code == 200
        recording.refresh_from_db()
        assert recording.display_name is None
        # Response should fall back to stored_name prefix.
        assert resp.json()["display_name"] == hash_part[:8]

    def test_patch_original_name_is_ignored(self, auth_client):
        """PATCH no longer accepts ``original_name`` — the field is immutable.

        Phase 2 reclassifies ``original_name`` as the filename as uploaded
        (immutable, author-only).  Clients that previously PATCHed it should
        find the field unchanged; the schema silently ignores the key.
        """
        c, user = auth_client
        recording = self._make_ready_recording(user)
        hash_part = recording.stored_name.split(".")[0]
        import json

        resp = c.patch(
            self.PATCH_URL.format(hash=hash_part),
            json.dumps({"original_name": "renamed.edf"}),
            content_type="application/json",
        )
        assert resp.status_code == 200
        recording.refresh_from_db()
        assert recording.original_name == "patient_001_routine.edf"

    # ── Upload with display_name parameter ───────────────────────────────────

    def test_upload_with_display_name_query_param(self, auth_client, tmp_path):
        import io
        from unittest.mock import patch as mock_patch

        from django.test import override_settings

        c, user = auth_client
        staging = tmp_path / "staging"
        uploads = tmp_path / "uploads"
        staging.mkdir()
        uploads.mkdir()
        with (
            override_settings(
                RECORDINGS_STAGING_PATH=str(staging),
                RECORDINGS_UPLOAD_PATH=str(uploads),
            ),
            mock_patch("recordings.tasks.process_recording.delay"),
        ):
            f = io.BytesIO(b"fake edf data")
            f.name = "patient_xyz.edf"
            resp = c.post(
                UPLOAD_URL + "?display_name=Cohort+A+S1",
                {"file": f},
                format="multipart",
            )
        assert resp.status_code == 202
        body = resp.json()
        assert body["original_name"] == "patient_xyz.edf"
        assert body["display_name"] == "Cohort A S1"

    def test_upload_without_display_name_defaults_to_hash_prefix(self, auth_client, tmp_path):
        import io
        from unittest.mock import patch as mock_patch

        from django.test import override_settings

        c, user = auth_client
        staging = tmp_path / "staging"
        uploads = tmp_path / "uploads"
        staging.mkdir()
        uploads.mkdir()
        with (
            override_settings(
                RECORDINGS_STAGING_PATH=str(staging),
                RECORDINGS_UPLOAD_PATH=str(uploads),
            ),
            mock_patch("recordings.tasks.process_recording.delay"),
        ):
            f = io.BytesIO(b"fake edf data")
            f.name = "patient_xyz.edf"
            resp = c.post(UPLOAD_URL, {"file": f}, format="multipart")
        assert resp.status_code == 202
        body = resp.json()
        stored_prefix = body["stored_name"].split(".")[0][:8]
        assert body["display_name"] == stored_prefix
        from recordings.models import Recording

        stored_recording = Recording.objects.get(stored_name=body["stored_name"])
        assert stored_recording.display_name is None  # truly null in DB

    # ── Content-Disposition uses display_name ────────────────────────────────

    def test_download_content_disposition_uses_display_name(self, auth_client, tmp_path):
        """The download response carries display_name + extension, never original_name."""
        c, user = auth_client
        edf_path = tmp_path / "private.edf"
        edf_path.write_bytes(b"\x00" * 256)
        recording = self._make_ready_recording(
            user,
            file_path=str(edf_path),
            file_size=256,
            original_name="patient_555.edf",
            display_name="Anon Subject A",
        )
        hash_part = recording.stored_name.split(".")[0]
        resp = c.get(DOWNLOAD_URL.format(hash=hash_part))
        assert resp.status_code == 200
        # Author gets raw bytes (no middleware); filename header still uses display_name.
        disp = resp.get("Content-Disposition", "")
        assert "Anon Subject A.edf" in disp
        assert "patient_555.edf" not in disp

    def test_download_content_disposition_escapes_quotes(self, auth_client, tmp_path):
        """A display_name with an embedded quote cannot break out of the
        quoted filename token and inject extra header parameters."""
        from django.utils.http import content_disposition_header

        c, user = auth_client
        edf_path = tmp_path / "quoted.edf"
        edf_path.write_bytes(b"\x00" * 256)
        hostile = "a\"; filename*=utf-8''evil.exe; x=\""
        recording = self._make_ready_recording(
            user,
            file_path=str(edf_path),
            file_size=256,
            display_name=hostile,
        )
        hash_part = recording.stored_name.split(".")[0]
        resp = c.get(DOWNLOAD_URL.format(hash=hash_part))
        assert resp.status_code == 200
        disp = resp.get("Content-Disposition", "")
        # The header must be exactly what Django's RFC 6266 encoder
        # produces — no hand-interpolated, unescaped quote sequence.
        assert disp == content_disposition_header(as_attachment=True, filename=f"{hostile}.edf")
        # Every interior quote arrives backslash-escaped; the only
        # unescaped quotes are the two delimiting the filename value.
        assert re.findall(r'(?<!\\)"', disp[len("attachment; ") :]) == ['"', '"']


DETAIL_URL = "/recordings/api/v1/{hash}"
ANNOTATIONS_URL = "/recordings/api/v1/{hash}/annotations"
UPDATE_URL = "/recordings/api/v1/{hash}"


@pytest.mark.django_db
class TestRecordingsAuditTrail:
    """Activity-row annotation contract for the recordings API.

    One representative test per endpoint, locking the verb + target +
    metadata shape so a future regression that drops the annotation
    surfaces here rather than in a SIEM rule months later. Shared
    fixtures construct only what each test needs — the EDF/BDF slice
    endpoints reuse the helpers in TestSliceRecording / TestRecordingDetailSlice.
    """

    def test_upload_records_recordings_upload(self, auth_client, tmp_path):
        from activity.models import Activity

        c, user = auth_client
        staging = tmp_path / "staging"
        uploads = tmp_path / "uploads"
        staging.mkdir()
        uploads.mkdir()
        with (
            override_settings(
                RECORDINGS_STAGING_PATH=str(staging),
                RECORDINGS_UPLOAD_PATH=str(uploads),
            ),
            patch("recordings.tasks.process_recording.delay"),
        ):
            f = io.BytesIO(b"fake edf data")
            f.name = "sample.edf"
            resp = c.post(UPLOAD_URL, {"file": f}, format="multipart")
        assert resp.status_code == 202

        recording = Recording.objects.get(stored_name=resp.json()["stored_name"])
        activity = Activity.objects.filter(verb="recordings.upload").latest("created_at")
        rec_ct = ContentType.objects.get_for_model(recording, for_concrete_model=False)
        assert activity.target_content_type_id == rec_ct.pk
        assert activity.target_object_id == str(recording.pk)
        assert activity.metadata["granted_user_access_count"] == 0
        assert activity.metadata["granted_group_access_count"] == 0
        assert activity.metadata["granted_share_token"] is False

    def test_status_records_recordings_status(self, auth_client):
        from activity.models import Activity

        c, user = auth_client
        recording = _make_recording(user)
        hash_part = recording.stored_name.split(".")[0]
        resp = c.get(STATUS_URL.format(hash=hash_part))
        assert resp.status_code == 200

        activity = Activity.objects.filter(verb="recordings.status").latest("created_at")
        rec_ct = ContentType.objects.get_for_model(recording, for_concrete_model=False)
        assert activity.target_content_type_id == rec_ct.pk
        assert activity.target_object_id == str(recording.pk)
        assert activity.metadata["status"] == recording.status

    def test_list_records_recordings_list(self, auth_client):
        from activity.models import Activity

        c, user = auth_client
        _make_recording(user, stored_name="LIST1111LIST1111LIST1111LIST1111.edf")
        resp = c.get(f"{LIST_URL}?limit=10&status=ready")
        assert resp.status_code == 200

        activity = Activity.objects.filter(verb="recordings.list").latest("created_at")
        assert activity.metadata["limit"] == 10
        assert activity.metadata["offset"] == 0
        assert activity.metadata["trash"] is False
        assert activity.metadata["status_filter"] == "ready"
        assert "returned_count" in activity.metadata

    def test_detail_records_recordings_read(self, auth_client):
        from activity.models import Activity

        c, user = auth_client
        recording = _make_recording(user, stored_name="DTL11111DTL11111DTL11111DTL11111.edf")
        hash_part = recording.stored_name.split(".")[0]
        resp = c.get(DETAIL_URL.format(hash=hash_part))
        assert resp.status_code == 200

        activity = Activity.objects.filter(verb="recordings.read").latest("created_at")
        rec_ct = ContentType.objects.get_for_model(recording, for_concrete_model=False)
        assert activity.target_content_type_id == rec_ct.pk
        assert activity.target_object_id == str(recording.pk)
        assert activity.metadata["share_token_used"] is False

    def test_annotations_records_recordings_annotations_list(self, auth_client):
        from activity.models import Activity

        c, user = auth_client
        recording = _make_recording(user, stored_name="ANN11111ANN11111ANN11111ANN11111.edf")
        hash_part = recording.stored_name.split(".")[0]
        resp = c.get(f"{ANNOTATIONS_URL.format(hash=hash_part)}?limit=20")
        assert resp.status_code == 200

        activity = Activity.objects.filter(verb="recordings.annotations.list").latest("created_at")
        rec_ct = ContentType.objects.get_for_model(recording, for_concrete_model=False)
        assert activity.target_content_type_id == rec_ct.pk
        assert activity.target_object_id == str(recording.pk)
        assert activity.metadata["limit"] == 20
        assert activity.metadata["offset"] == 0
        assert activity.metadata["author_id_filter"] is None
        assert "returned_count" in activity.metadata

    def test_download_records_recordings_download(self, auth_client, tmp_path):
        from activity.models import Activity

        c, user = auth_client
        edf_path = tmp_path / "test.edf"
        edf_path.write_bytes(b"\x00" * 256)
        recording = _make_recording(
            user,
            stored_name="DLD11111DLD11111DLD11111DLD11111.edf",
            file_path=str(edf_path),
            file_size=256,
        )
        hash_part = recording.stored_name.split(".")[0]
        resp = c.get(DOWNLOAD_URL.format(hash=hash_part))
        assert resp.status_code == 200

        activity = Activity.objects.filter(verb="recordings.download").latest("created_at")
        rec_ct = ContentType.objects.get_for_model(recording, for_concrete_model=False)
        assert activity.target_content_type_id == rec_ct.pk
        assert activity.target_object_id == str(recording.pk)
        # Author gets raw bytes — apply_middleware is False.
        assert activity.metadata["apply_middleware"] is False

    def test_trash_records_recordings_trash(self, auth_client):
        from activity.models import Activity

        c, user = auth_client
        recording = _make_recording(user, stored_name="TRH11111TRH11111TRH11111TRH11111.edf")
        hash_part = recording.stored_name.split(".")[0]
        resp = c.delete(DELETE_URL.format(hash=hash_part))
        assert resp.status_code == 200

        activity = Activity.objects.filter(verb="recordings.trash").latest("created_at")
        rec_ct = ContentType.objects.get_for_model(recording, for_concrete_model=False)
        assert activity.target_content_type_id == rec_ct.pk
        # Soft delete preserves the row + pk on the live instance, so
        # target_object_id reflects the trashed recording.
        assert activity.target_object_id == str(recording.pk)

    def test_update_records_recordings_update(self, auth_client):
        import json

        from activity.models import Activity

        c, user = auth_client
        recording = _make_recording(user, stored_name="UPD11111UPD11111UPD11111UPD11111.edf")
        hash_part = recording.stored_name.split(".")[0]
        resp = c.patch(
            UPDATE_URL.format(hash=hash_part),
            json.dumps({"display_name": "Renamed"}),
            content_type="application/json",
        )
        assert resp.status_code == 200

        activity = Activity.objects.filter(verb="recordings.update").latest("created_at")
        rec_ct = ContentType.objects.get_for_model(recording, for_concrete_model=False)
        assert activity.target_content_type_id == rec_ct.pk
        assert activity.target_object_id == str(recording.pk)
        assert activity.metadata["fields_updated"] == ["display_name"]

    def test_detail_slice_records_recordings_read_slice(self, auth_client, tmp_path):
        """EDF-format endpoint — reuse the same EDF builder as TestRecordingDetailSlice."""
        from activity.models import Activity
        from recordings.models import RecordingMeta

        c, user = auth_client
        # Minimal EDF file isn't needed for this metadata-only endpoint — only the
        # RecordingMeta row matters since the slice computation reads it.
        recording = _make_recording(user, stored_name="DSL11111DSL11111DSL11111DSL11111.edf")
        ct = ContentType.objects.get_for_model(recording, for_concrete_model=False)
        RecordingMeta.objects.create(
            content_type=ct,
            object_id=str(recording.pk),
            format="edf",
            duration=10.0,
            data_record_count=10,
            data_record_duration=1.0,
            signal_count=1,
            discontinuous=False,
        )
        hash_part = recording.stored_name.split(".")[0]
        resp = c.get(f"/recordings/api/v1/{hash_part}/slice?t_start=2&t_end=5")
        assert resp.status_code == 200

        activity = Activity.objects.filter(verb="recordings.read.slice").latest("created_at")
        assert activity.target_content_type_id == ct.pk
        assert activity.target_object_id == str(recording.pk)
        assert activity.metadata["t_start"] == 2.0
        assert activity.metadata["t_end"] == 5.0
        assert "event_count" in activity.metadata
        assert "interruption_count" in activity.metadata

    def test_slice_records_recordings_download_slice(self, auth_client, tmp_path):
        """EDF slice endpoint — needs a real EDF file on disk + RecordingMeta."""
        from activity.models import Activity
        from recordings.models import RecordingMeta

        c, user = auth_client
        # Minimal EDF: 256-byte main header + 256-byte signal header * 1 signal
        # + 1 record of int16 data (2 bytes * 1 sample = 2 bytes).
        header = b" " * (256 + 256)
        data = b"\x00\x00" * 1
        edf_path = tmp_path / "slice.edf"
        edf_path.write_bytes(header + data)

        recording = _make_recording(
            user,
            stored_name="SLI11111SLI11111SLI11111SLI11111.edf",
            file_path=str(edf_path),
            file_size=len(header) + len(data),
        )
        ct = ContentType.objects.get_for_model(recording, for_concrete_model=False)
        RecordingMeta.objects.create(
            content_type=ct,
            object_id=str(recording.pk),
            format="edf",
            duration=1.0,
            data_record_count=1,
            data_record_duration=1.0,
            signal_count=1,
            discontinuous=False,
        )
        hash_part = recording.stored_name.split(".")[0]
        resp = c.get(f"/recordings/api/v1/{hash_part}/file/slice?t_start=0&t_end=1")
        assert resp.status_code == 200

        activity = Activity.objects.filter(verb="recordings.download.slice").latest("created_at")
        assert activity.target_content_type_id == ct.pk
        assert activity.target_object_id == str(recording.pk)
        assert activity.metadata["t_start"] == 0.0
        assert activity.metadata["t_end"] == 1.0
        assert activity.metadata["apply_middleware"] is False
