"""API tests for the dicom plugin.

Covers the upload contract (per-file accept/reject reporting, duplicates,
replacement of stranded rows, per-author study isolation), study listing and
detail access, OHIF JSON generation, WADO streaming with per-author
disambiguation, sharing, soft delete, and the audit-trail verb per endpoint.
"""

import json

import pytest
from django.contrib.contenttypes.models import ContentType
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client

from activity.models import Activity
from epicurrents.models import AccessRight
from plugins.dicom.models import DicomInstance, DicomStudy
from plugins.dicom.tests.conftest import STUDIES_URL, UPLOAD_URL, WADO_URL

STUDY_UID = "1.2.826.0.1.5555001"
SERIES_UID = "1.2.826.0.1.5555001.1"
SOP_UID = "1.2.826.0.1.5555001.1.1"


def _upload(client, *payloads, **extra_params):
    """POST the given DICOM byte payloads as one multipart batch."""
    files = [
        SimpleUploadedFile(f"file{i}.dcm", data, content_type="application/dicom") for i, data in enumerate(payloads)
    ]
    url = UPLOAD_URL
    if extra_params:
        query = "&".join(f"{k}={v}" for k, v in extra_params.items())
        url = f"{url}?{query}"
    return client.post(url, {"files": files})


@pytest.mark.django_db
class TestUpload:
    def test_single_file_creates_ready_study(self, auth_client, make_dicom_bytes, dicom_dirs):
        client, user = auth_client
        upload_dir, staging_dir = dicom_dirs

        response = _upload(client, make_dicom_bytes(study_uid=STUDY_UID, series_uid=SERIES_UID, sop_uid=SOP_UID))
        assert response.status_code == 200
        body = response.json()
        assert body["accepted"] == 1
        assert body["rejected"] == 0
        assert len(body["studies"]) == 1
        assert body["files"][0]["accepted"] is True

        study = DicomStudy.objects.get(content_hash=body["studies"][0]["hash"])
        assert study.author == user
        assert study.study_instance_uid == STUDY_UID
        assert study.num_instances == 1
        assert study.patient_name == "Doe^John"

        inst = DicomInstance.objects.get(sop_instance_uid=SOP_UID)
        assert inst.status == DicomInstance.Status.READY
        assert (upload_dir / inst.stored_name).exists()
        assert not any(staging_dir.iterdir())

    def test_author_gets_self_access_right(self, auth_client, make_dicom_bytes):
        client, user = auth_client
        response = _upload(client, make_dicom_bytes())
        study = DicomStudy.objects.get(content_hash=response.json()["studies"][0]["hash"])
        ct = ContentType.objects.get_for_model(study, for_concrete_model=False)
        right = AccessRight.objects.get(content_type=ct, object_id=str(study.pk))
        assert right.access_target == user
        assert right.can_read and right.can_write and right.can_share

    def test_multiple_files_same_study_aggregate(self, auth_client, make_dicom_bytes):
        client, _ = auth_client
        response = _upload(
            client,
            make_dicom_bytes(study_uid=STUDY_UID, series_uid=SERIES_UID, sop_uid=f"{SERIES_UID}.1"),
            make_dicom_bytes(study_uid=STUDY_UID, series_uid=SERIES_UID, sop_uid=f"{SERIES_UID}.2"),
            make_dicom_bytes(study_uid=STUDY_UID, series_uid=f"{STUDY_UID}.2", sop_uid=f"{STUDY_UID}.2.1"),
        )
        body = response.json()
        assert body["accepted"] == 3
        assert len(body["studies"]) == 1
        assert body["studies"][0]["instances_added"] == 3
        study = DicomStudy.objects.get(content_hash=body["studies"][0]["hash"])
        assert study.num_instances == 3
        assert study.series.count() == 2

    def test_invalid_file_rejected_with_400(self, auth_client):
        client, _ = auth_client
        response = _upload(client, b"this is not a dicom file")
        assert response.status_code == 400
        assert DicomStudy.objects.count() == 0
        assert DicomInstance.objects.count() == 0

    def test_mixed_batch_reports_per_file(self, auth_client, make_dicom_bytes):
        client, _ = auth_client
        response = _upload(client, make_dicom_bytes(), b"garbage bytes")
        assert response.status_code == 200
        body = response.json()
        assert body["accepted"] == 1
        assert body["rejected"] == 1
        rejected = [f for f in body["files"] if not f["accepted"]]
        assert rejected and rejected[0]["error"]

    def test_duplicate_ready_instance_rejected(self, auth_client, make_dicom_bytes):
        client, _ = auth_client
        payload = make_dicom_bytes(study_uid=STUDY_UID, series_uid=SERIES_UID, sop_uid=SOP_UID)
        assert _upload(client, payload).status_code == 200
        response = _upload(client, payload)
        assert response.status_code == 400  # sole file rejected as duplicate
        assert DicomInstance.objects.filter(sop_instance_uid=SOP_UID).count() == 1

    def test_reupload_replaces_failed_instance(self, auth_client, make_dicom_bytes, dicom_dirs):
        client, _ = auth_client
        upload_dir, _ = dicom_dirs
        payload = make_dicom_bytes(study_uid=STUDY_UID, series_uid=SERIES_UID, sop_uid=SOP_UID)
        _upload(client, payload)
        inst = DicomInstance.objects.get(sop_instance_uid=SOP_UID)
        old_stored_name = inst.stored_name
        inst.status = DicomInstance.Status.FAILED
        inst.save(update_fields=["status"])

        response = _upload(client, payload)
        assert response.status_code == 200
        inst.refresh_from_db()
        assert inst.status == DicomInstance.Status.READY
        assert inst.stored_name != old_stored_name
        assert not (upload_dir / old_stored_name).exists()
        assert (upload_dir / inst.stored_name).exists()

    def test_same_uid_by_two_users_creates_independent_copies(self, auth_client, make_user, make_dicom_bytes):
        """Regression: a second user's upload must never attach to the first
        user's study (the StudyInstanceUID hijack)."""
        client_a, user_a = auth_client
        payload_kwargs = {"study_uid": STUDY_UID, "series_uid": SERIES_UID, "sop_uid": SOP_UID}
        hash_a = _upload(client_a, make_dicom_bytes(**payload_kwargs)).json()["studies"][0]["hash"]

        user_b = make_user()
        client_b = Client()
        client_b.force_login(user_b)
        hash_b = _upload(client_b, make_dicom_bytes(**payload_kwargs)).json()["studies"][0]["hash"]

        assert hash_a != hash_b
        assert DicomStudy.objects.filter(study_instance_uid=STUDY_UID).count() == 2
        study_a = DicomStudy.objects.get(content_hash=hash_a)
        study_b = DicomStudy.objects.get(content_hash=hash_b)
        assert study_a.author == user_a
        assert study_b.author == user_b
        # Each user's copy carries exactly their own instance.
        assert DicomInstance.objects.filter(series__study=study_a).count() == 1
        assert DicomInstance.objects.filter(series__study=study_b).count() == 1
        # Neither sees the other's copy.
        assert client_b.get(f"{STUDIES_URL}{hash_a}/").status_code == 404
        assert client_a.get(f"{STUDIES_URL}{hash_b}/").status_code == 404

    def test_too_many_files_rejected(self, auth_client, settings):
        client, _ = auth_client
        settings.DICOM_MAX_UPLOAD_FILES = 1
        response = _upload(client, b"a", b"b")
        assert response.status_code == 400

    def test_requires_auth(self, db, make_dicom_bytes):
        response = _upload(Client(), make_dicom_bytes())
        assert response.status_code == 401


@pytest.mark.django_db
class TestUploadAttachment:
    @pytest.fixture
    def recording(self, user):
        from model_bakery import baker

        from recordings.models import Recording

        return baker.make(
            Recording,
            author=user,
            status=Recording.Status.READY,
            content_hash="R" * 64,
        )

    def test_attaches_study_to_recording(self, auth_client, recording, make_dicom_bytes):
        client, _ = auth_client
        response = _upload(
            client,
            make_dicom_bytes(),
            attached_to_type="recording",
            attached_to_id=recording.content_hash,
        )
        assert response.status_code == 200
        study = DicomStudy.objects.get(content_hash=response.json()["studies"][0]["hash"])
        assert study.attachment == recording

    def test_attachment_requires_write_access_to_parent(self, auth_client, make_user, make_dicom_bytes):
        from model_bakery import baker

        from recordings.models import Recording

        client, _ = auth_client
        other = make_user()
        foreign = baker.make(
            Recording,
            author=other,
            status=Recording.Status.READY,
            content_hash="S" * 64,
        )
        response = _upload(
            client,
            make_dicom_bytes(),
            attached_to_type="recording",
            attached_to_id=foreign.content_hash,
        )
        assert response.status_code == 403
        assert DicomStudy.objects.count() == 0

    def test_attachment_rejected_for_multi_study_batch(self, auth_client, recording, make_dicom_bytes):
        client, _ = auth_client
        response = _upload(
            client,
            make_dicom_bytes(study_uid="1.2.3.1"),
            make_dicom_bytes(study_uid="1.2.3.2"),
            attached_to_type="recording",
            attached_to_id=recording.content_hash,
        )
        assert response.status_code == 400
        # The whole transaction rolled back — no partial state.
        assert DicomStudy.objects.count() == 0
        assert DicomInstance.objects.count() == 0


@pytest.mark.django_db
class TestStudyListingAndDetail:
    def test_list_shows_own_studies_with_is_author(self, auth_client, make_study):
        client, user = auth_client
        make_study(user)
        response = client.get(STUDIES_URL)
        assert response.status_code == 200
        items = response.json()
        assert len(items) == 1
        assert items[0]["is_author"] is True

    def test_list_hides_others_and_trashed(self, auth_client, make_user, make_study):
        from django.utils import timezone

        client, user = auth_client
        make_study(make_user())
        make_study(user, deleted_at=timezone.now())
        assert client.get(STUDIES_URL).json() == []

    def test_list_includes_granted_studies(self, auth_client, make_user, make_study):
        client, user = auth_client
        other = make_user()
        study = make_study(other)
        ct = ContentType.objects.get_for_model(study, for_concrete_model=False)
        AccessRight.objects.create(
            content_type=ct,
            object_id=str(study.pk),
            access_giver=other,
            access_target=user,
            can_read=True,
        )
        items = client.get(STUDIES_URL).json()
        assert len(items) == 1
        assert items[0]["is_author"] is False

    def test_detail_includes_series(self, auth_client, make_study):
        client, user = auth_client
        study = make_study(user, instance_count=2)
        response = client.get(f"{STUDIES_URL}{study.content_hash}/")
        assert response.status_code == 200
        body = response.json()
        assert body["hash"] == study.content_hash
        assert len(body["series"]) == 1
        assert body["series"][0]["instance_count"] == 2

    def test_detail_404_for_unknown_trashed_and_denied(self, auth_client, make_user, make_study):
        from django.utils import timezone

        client, user = auth_client
        assert client.get(f"{STUDIES_URL}{'0' * 64}/").status_code == 404
        trashed = make_study(user, deleted_at=timezone.now())
        assert client.get(f"{STUDIES_URL}{trashed.content_hash}/").status_code == 404
        foreign = make_study(make_user())
        assert client.get(f"{STUDIES_URL}{foreign.content_hash}/").status_code == 404

    def test_ohif_json_contains_wado_urls(self, auth_client, make_study):
        client, user = auth_client
        study = make_study(user)
        response = client.get(f"{STUDIES_URL}{study.content_hash}/ohif-json/")
        assert response.status_code == 200
        body = response.json()
        assert body["studies"][0]["StudyInstanceUID"] == study.study_instance_uid
        url = body["studies"][0]["series"][0]["instances"][0]["url"]
        assert url.startswith("dicomweb:")
        assert "/plugin/dicom/api/v1/dicom/wado/" in url
        assert f"studyUID={study.study_instance_uid}" in url


@pytest.mark.django_db
class TestDeleteStudy:
    def test_author_soft_deletes(self, auth_client, make_study):
        client, user = auth_client
        study = make_study(user)
        response = client.delete(f"{STUDIES_URL}{study.content_hash}/")
        assert response.status_code == 204
        study.refresh_from_db()
        assert study.deleted_at is not None
        # Hidden from every read surface immediately.
        assert client.get(f"{STUDIES_URL}{study.content_hash}/").status_code == 404

    def test_read_grantee_cannot_delete(self, auth_client, make_user, make_study):
        client, user = auth_client
        other = make_user()
        study = make_study(other)
        ct = ContentType.objects.get_for_model(study, for_concrete_model=False)
        AccessRight.objects.create(
            content_type=ct,
            object_id=str(study.pk),
            access_giver=other,
            access_target=user,
            can_read=True,
        )
        assert client.delete(f"{STUDIES_URL}{study.content_hash}/").status_code == 403

    def test_files_stay_on_disk_until_purge(self, auth_client, make_study, dicom_dirs):
        client, user = auth_client
        upload_dir, _ = dicom_dirs
        study = make_study(user)
        stored = DicomInstance.objects.get(series__study=study).stored_name
        client.delete(f"{STUDIES_URL}{study.content_hash}/")
        assert (upload_dir / stored).exists()


@pytest.mark.django_db
class TestSharing:
    def test_share_grants_read(self, auth_client, make_user, make_study):
        client, user = auth_client
        study = make_study(user)
        grantee = make_user()
        response = client.post(
            f"{STUDIES_URL}{study.content_hash}/share/",
            json.dumps({"username": grantee.username}),
            content_type="application/json",
        )
        assert response.status_code == 201

        grantee_client = Client()
        grantee_client.force_login(grantee)
        assert grantee_client.get(f"{STUDIES_URL}{study.content_hash}/").status_code == 200

    def test_share_validations(self, auth_client, make_user, make_study):
        client, user = auth_client
        study = make_study(user)
        url = f"{STUDIES_URL}{study.content_hash}/share/"

        def share(username):
            return client.post(url, json.dumps({"username": username}), content_type="application/json")

        assert share("no-such-user").status_code == 400
        assert share(user.username).status_code == 400
        grantee = make_user()
        assert share(grantee.username).status_code == 201
        assert share(grantee.username).status_code == 409

    def test_non_author_cannot_share(self, auth_client, make_user, make_study):
        client, user = auth_client
        other = make_user()
        study = make_study(other)
        ct = ContentType.objects.get_for_model(study, for_concrete_model=False)
        AccessRight.objects.create(
            content_type=ct,
            object_id=str(study.pk),
            access_giver=other,
            access_target=user,
            can_read=True,
        )
        response = client.post(
            f"{STUDIES_URL}{study.content_hash}/share/",
            json.dumps({"username": make_user().username}),
            content_type="application/json",
        )
        assert response.status_code == 403

    def test_revoke_removes_access(self, auth_client, make_user, make_study):
        client, user = auth_client
        study = make_study(user)
        grantee = make_user()
        client.post(
            f"{STUDIES_URL}{study.content_hash}/share/",
            json.dumps({"username": grantee.username}),
            content_type="application/json",
        )
        response = client.delete(f"{STUDIES_URL}{study.content_hash}/share/{grantee.username}/")
        assert response.status_code == 204

        grantee_client = Client()
        grantee_client.force_login(grantee)
        assert grantee_client.get(f"{STUDIES_URL}{study.content_hash}/").status_code == 404

    def test_revoke_unknown_user_404(self, auth_client, make_study):
        client, user = auth_client
        study = make_study(user)
        response = client.delete(f"{STUDIES_URL}{study.content_hash}/share/nobody/")
        assert response.status_code == 404


@pytest.mark.django_db
class TestWado:
    def test_author_streams_instance(self, auth_client, make_study, dicom_dirs):
        client, user = auth_client
        study = make_study(user)
        inst = DicomInstance.objects.get(series__study=study)
        response = client.get(f"{WADO_URL}?objectUID={inst.sop_instance_uid}")
        assert response.status_code == 200
        assert response["Content-Type"] == "application/dicom"
        assert b"".join(response.streaming_content) == b"DICMDATA"
        assert "Access-Control-Allow-Origin" not in response

    def test_stranger_gets_404(self, auth_client, make_user, make_study):
        client, _ = auth_client
        foreign = make_study(make_user())
        inst = DicomInstance.objects.get(series__study=foreign)
        response = client.get(f"{WADO_URL}?objectUID={inst.sop_instance_uid}")
        assert response.status_code == 404

    def test_grantee_streams_via_share(self, auth_client, make_user, make_study):
        client, user = auth_client
        other = make_user()
        study = make_study(other)
        ct = ContentType.objects.get_for_model(study, for_concrete_model=False)
        AccessRight.objects.create(
            content_type=ct,
            object_id=str(study.pk),
            access_giver=other,
            access_target=user,
            can_read=True,
        )
        inst = DicomInstance.objects.get(series__study=study)
        response = client.get(f"{WADO_URL}?objectUID={inst.sop_instance_uid}")
        assert response.status_code == 200

    def test_per_author_copies_resolve_to_own_instance(self, auth_client, make_user, make_study, dicom_dirs):
        """Two users hold copies of the same study — each caller gets their
        own copy's bytes for the shared SOP UID."""
        upload_dir, _ = dicom_dirs
        client, user = auth_client
        own = make_study(user, study_uid=STUDY_UID)
        own_inst = DicomInstance.objects.get(series__study=own)
        (upload_dir / own_inst.stored_name).write_bytes(b"OWNCOPY!")

        foreign = make_study(make_user(), study_uid=STUDY_UID)
        foreign_inst = DicomInstance.objects.get(series__study=foreign)
        assert foreign_inst.sop_instance_uid == own_inst.sop_instance_uid

        response = client.get(f"{WADO_URL}?objectUID={own_inst.sop_instance_uid}&studyUID={STUDY_UID}")
        assert response.status_code == 200
        assert b"".join(response.streaming_content) == b"OWNCOPY!"

    def test_trashed_study_instance_404(self, auth_client, make_study):
        from django.utils import timezone

        client, user = auth_client
        study = make_study(user, deleted_at=timezone.now())
        inst = DicomInstance.objects.get(series__study=study)
        response = client.get(f"{WADO_URL}?objectUID={inst.sop_instance_uid}")
        assert response.status_code == 404


@pytest.mark.django_db
class TestDicomAuditTrail:
    """One test per audit verb — the plugin mount must produce annotated
    Activity rows now that the middleware recognises /plugin/<name>/api/v1/."""

    def _last_verb(self):
        activity = Activity.objects.order_by("-id").first()
        return activity.verb if activity else None

    def test_upload_verb(self, auth_client, make_dicom_bytes):
        client, _ = auth_client
        _upload(client, make_dicom_bytes())
        assert Activity.objects.filter(verb="dicom.study.upload").exists()

    def test_list_verb(self, auth_client):
        client, _ = auth_client
        client.get(STUDIES_URL)
        assert self._last_verb() == "dicom.study.list"

    def test_read_verb(self, auth_client, make_study):
        client, user = auth_client
        study = make_study(user)
        client.get(f"{STUDIES_URL}{study.content_hash}/")
        assert self._last_verb() == "dicom.study.read"

    def test_ohif_json_verb(self, auth_client, make_study):
        client, user = auth_client
        study = make_study(user)
        client.get(f"{STUDIES_URL}{study.content_hash}/ohif-json/")
        assert self._last_verb() == "dicom.study.read.ohif_json"

    def test_trash_verb(self, auth_client, make_study):
        client, user = auth_client
        study = make_study(user)
        client.delete(f"{STUDIES_URL}{study.content_hash}/")
        assert self._last_verb() == "dicom.study.trash"

    def test_share_and_revoke_verbs(self, auth_client, make_user, make_study):
        client, user = auth_client
        study = make_study(user)
        grantee = make_user()
        client.post(
            f"{STUDIES_URL}{study.content_hash}/share/",
            json.dumps({"username": grantee.username}),
            content_type="application/json",
        )
        assert Activity.objects.filter(verb="dicom.study.access.grant").exists()
        client.delete(f"{STUDIES_URL}{study.content_hash}/share/{grantee.username}/")
        assert Activity.objects.filter(verb="dicom.study.access.revoke").exists()

    def test_wado_verb_with_target(self, auth_client, make_study):
        client, user = auth_client
        study = make_study(user)
        inst = DicomInstance.objects.get(series__study=study)
        client.get(f"{WADO_URL}?objectUID={inst.sop_instance_uid}")
        activity = Activity.objects.filter(verb="dicom.instance.download").first()
        assert activity is not None
        assert activity.target_object_id == str(inst.pk)
