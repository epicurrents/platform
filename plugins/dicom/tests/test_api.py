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
from plugins.dicom.tests.conftest import STUDIES_URL, UPLOAD_URL, WADO_URL, stored_uid

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
        assert study.study_instance_uid == stored_uid(STUDY_UID, user)
        assert study.num_instances == 1
        assert study.patient_name == ""

        inst = DicomInstance.objects.get(sop_instance_uid=stored_uid(SOP_UID, user))
        assert inst.status == DicomInstance.Status.READY
        assert (upload_dir / inst.stored_name).exists()
        assert not any(staging_dir.iterdir())

    def test_upload_records_the_modality_lut(self, auth_client, make_dicom_bytes):
        client, user = auth_client
        payload = make_dicom_bytes(sop_uid=SOP_UID, extra={"RescaleSlope": "1", "RescaleIntercept": "-1024"})
        assert _upload(client, payload).json()["accepted"] == 1
        inst = DicomInstance.objects.get(sop_instance_uid=stored_uid(SOP_UID, user))
        assert (inst.rescale_slope, inst.rescale_intercept) == ("1", "-1024")

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
        client, user = auth_client
        payload = make_dicom_bytes(study_uid=STUDY_UID, series_uid=SERIES_UID, sop_uid=SOP_UID)
        assert _upload(client, payload).status_code == 200
        response = _upload(client, payload)
        assert response.status_code == 400  # sole file rejected as duplicate
        assert DicomInstance.objects.filter(sop_instance_uid=stored_uid(SOP_UID, user)).count() == 1

    def test_reupload_replaces_failed_instance(self, auth_client, make_dicom_bytes, dicom_dirs):
        client, user = auth_client
        upload_dir, _ = dicom_dirs
        payload = make_dicom_bytes(study_uid=STUDY_UID, series_uid=SERIES_UID, sop_uid=SOP_UID)
        _upload(client, payload)
        inst = DicomInstance.objects.get(sop_instance_uid=stored_uid(SOP_UID, user))
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
        study_a = DicomStudy.objects.get(content_hash=hash_a)
        study_b = DicomStudy.objects.get(content_hash=hash_b)
        # Each copy carries its own author's replacement, so the two share no UID.
        assert study_a.study_instance_uid == stored_uid(STUDY_UID, user_a)
        assert study_b.study_instance_uid == stored_uid(STUDY_UID, user_b)
        assert study_a.study_instance_uid != study_b.study_instance_uid
        assert study_a.author == user_a
        assert study_b.author == user_b
        # Each user's copy carries exactly their own instance.
        assert DicomInstance.objects.filter(series__study=study_a).count() == 1
        assert DicomInstance.objects.filter(series__study=study_b).count() == 1
        # Neither sees the other's copy.
        assert client_b.get(f"{STUDIES_URL}{hash_a}/").status_code == 404
        assert client_a.get(f"{STUDIES_URL}{hash_b}/").status_code == 404

    def test_a_database_error_while_persisting_leaves_nothing_staged(
        self, auth_client, make_dicom_bytes, dicom_dirs, monkeypatch
    ):
        from django.db import DataError

        from plugins.dicom import urls

        calls = []

        def fail_on_the_second(**kwargs):
            calls.append(kwargs)
            if len(calls) == 2:
                raise DataError("value too long for type character varying(16)")
            return persist(**kwargs)

        persist = urls.persist_instance
        monkeypatch.setattr(urls, "persist_instance", fail_on_the_second)
        _, user = auth_client
        _, staging_dir = dicom_dirs
        client = Client(raise_request_exception=False)
        client.force_login(user)
        response = _upload(client, make_dicom_bytes(), make_dicom_bytes())
        assert response.status_code == 500
        assert len(calls) == 2
        # Neither the as-uploaded files nor their rewritten copies outlive the request.
        assert not any(staging_dir.iterdir())
        assert DicomStudy.objects.count() == 0

    def test_an_unexpected_failure_while_staging_leaves_nothing_staged(
        self, auth_client, make_dicom_bytes, dicom_dirs, monkeypatch
    ):
        from plugins.dicom import urls
        from plugins.dicom.ingest import DeidentificationFailed

        prepare = urls.prepare_file
        seen = []

        def refuse_the_second(source, **kwargs):
            seen.append(source)
            if len(seen) == 2:
                raise DeidentificationFailed("The file could not be de-identified.")
            return prepare(source, **kwargs)

        def preservation_breaks():
            raise RuntimeError("originals volume unavailable")

        monkeypatch.setattr(urls, "prepare_file", refuse_the_second)
        monkeypatch.setattr(urls, "should_preserve_failed", preservation_breaks)
        _, user = auth_client
        _, staging_dir = dicom_dirs
        client = Client(raise_request_exception=False)
        client.force_login(user)
        response = _upload(client, make_dicom_bytes(), make_dicom_bytes())
        assert response.status_code == 500
        # The first file had been staged and rewritten when the second failed.
        assert len(seen) == 2
        assert not any(staging_dir.iterdir())
        assert DicomStudy.objects.count() == 0

    def test_header_values_are_cut_to_their_columns(self):
        from types import SimpleNamespace

        from plugins.dicom.ingest import extract_instance_fields, extract_series_fields, extract_study_fields
        from plugins.dicom.models import DicomSeries

        series = extract_series_fields(
            SimpleNamespace(SliceThickness="0.699999988079071", SeriesDescription="d" * 300, Modality="CT")
        )
        assert series["slice_thickness"] == "0.69999998807907"
        assert len(series["slice_thickness"]) == DicomSeries._meta.get_field("slice_thickness").max_length
        assert len(series["series_description"]) == 256
        assert series["modality"] == "CT"
        study = extract_study_fields(SimpleNamespace(PatientAge="055YY", StudyDescription="s" * 300))
        assert (study["patient_age"], len(study["study_description"])) == ("055Y", 256)
        instance = extract_instance_fields(SimpleNamespace(RescaleSlope="1" * 40, Rows=512, PixelSpacing=["0.5"]))
        assert (len(instance["rescale_slope"]), instance["rows"], instance["pixel_spacing"]) == (32, 512, ["0.5"])

    def test_an_over_long_uid_is_refused_with_its_file(self):
        from types import SimpleNamespace

        from plugins.dicom.ingest import MissingUidsError, required_uids

        long_uid = "1.2." + "3" * 61
        with pytest.raises(MissingUidsError):
            required_uids(SimpleNamespace(StudyInstanceUID=long_uid, SeriesInstanceUID="1.2", SOPInstanceUID="1.3"))
        assert required_uids(
            SimpleNamespace(StudyInstanceUID=long_uid[:64], SeriesInstanceUID="1.2", SOPInstanceUID="1.3")
        ) == (long_uid[:64], "1.2", "1.3")

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
        # The same answer as for a recording that does not exist.
        assert response.status_code == 404
        assert DicomStudy.objects.count() == 0
        missing = _upload(client, make_dicom_bytes(), attached_to_type="recording", attached_to_id="T" * 64)
        assert (missing.status_code, missing.json()) == (response.status_code, response.json())

    def test_attaches_by_the_recordings_public_hash(self, auth_client, user, make_dicom_bytes):
        from model_bakery import baker

        from recordings.models import Recording

        client, _ = auth_client
        public_hash = "AB12" * 8
        recording = baker.make(Recording, author=user, status=Recording.Status.READY, stored_name=f"{public_hash}.edf")
        response = _upload(client, make_dicom_bytes(), attached_to_type="recording", attached_to_id=public_hash.lower())
        assert response.status_code == 200
        assert DicomStudy.objects.get().attachment == recording

    def test_a_shared_content_fingerprint_resolves_to_the_recording_the_caller_may_write(
        self, auth_client, user, make_user, make_dicom_bytes
    ):
        from model_bakery import baker

        from recordings.models import Recording

        client, _ = auth_client
        fingerprint = "C" * 64
        baker.make(Recording, author=make_user(), status=Recording.Status.READY, content_hash=fingerprint)
        own = baker.make(Recording, author=user, status=Recording.Status.READY, content_hash=fingerprint)
        baker.make(Recording, author=make_user(), status=Recording.Status.READY, content_hash=fingerprint)
        response = _upload(client, make_dicom_bytes(), attached_to_type="recording", attached_to_id=fingerprint)
        assert response.status_code == 200
        assert DicomStudy.objects.get().attachment == own

    def test_a_batch_of_duplicates_says_the_study_was_not_attached(self, auth_client, recording, make_dicom_bytes):
        client, _ = auth_client
        payload = make_dicom_bytes()
        assert _upload(client, payload).status_code == 200
        response = _upload(client, payload, attached_to_type="recording", attached_to_id=recording.content_hash)
        assert response.status_code == 400
        assert "not attached" in response.json()["detail"]
        assert DicomStudy.objects.get().attachment is None

    def test_duplicates_beside_a_held_file_report_the_missing_attachment(
        self, auth_client, recording, make_dicom_bytes
    ):
        client, _ = auth_client
        payload = make_dicom_bytes()
        assert _upload(client, payload).status_code == 200
        response = _upload(
            client,
            payload,
            make_dicom_bytes(modality="US"),
            attached_to_type="recording",
            attached_to_id=recording.content_hash,
        )
        assert response.status_code == 200
        reports = {r["index"]: r for r in response.json()["files"]}
        assert "not attached" in reports[0]["error"]
        assert "not attached" not in reports[1]["error"]

    def test_a_duplicate_beside_a_new_file_attaches_without_the_note(self, auth_client, recording, make_dicom_bytes):
        client, _ = auth_client
        payload = make_dicom_bytes(study_uid=STUDY_UID, series_uid=SERIES_UID, sop_uid=SOP_UID)
        assert _upload(client, payload).status_code == 200
        response = _upload(
            client,
            payload,
            make_dicom_bytes(study_uid=STUDY_UID, series_uid=SERIES_UID, sop_uid=f"{SERIES_UID}.2"),
            attached_to_type="recording",
            attached_to_id=recording.content_hash,
        )
        assert response.status_code == 200
        assert "not attached" not in json.dumps(response.json())
        assert DicomStudy.objects.get().attachment == recording

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

    def test_upload_time_is_exact_for_the_author_and_a_month_for_a_grantee(
        self, auth_client, make_user, make_superuser, make_study
    ):
        from datetime import UTC, datetime

        client, user = auth_client
        other = make_user()
        granted, own = make_study(other), make_study(user)
        uploaded = datetime(2026, 3, 17, 14, 22, 33, tzinfo=UTC)
        DicomStudy.objects.update(created_at=uploaded)
        ct = ContentType.objects.get_for_model(granted, for_concrete_model=False)
        AccessRight.objects.create(
            content_type=ct, object_id=str(granted.pk), access_giver=other, access_target=user, can_read=True
        )

        def seen(client, study):
            listed = {item["hash"]: item for item in client.get(STUDIES_URL).json()}[study.content_hash]
            detail = client.get(f"{STUDIES_URL}{study.content_hash}/").json()
            assert listed["created_at"] == detail["created_at"]
            return datetime.fromisoformat(detail["created_at"].replace("Z", "+00:00"))

        assert seen(client, granted) == datetime(2026, 3, 1, tzinfo=UTC)
        assert seen(client, own) == uploaded
        for exact_reader in (other, make_superuser()):
            exact_client = Client()
            exact_client.force_login(exact_reader)
            assert seen(exact_client, granted) == uploaded

    def test_list_says_which_studies_the_caller_may_share(self, auth_client, make_user, make_study):
        client, user = auth_client
        other = make_user()
        readable, shareable, own = make_study(other), make_study(other), make_study(user)
        ct = ContentType.objects.get_for_model(own, for_concrete_model=False)
        for study, can_share in ((readable, False), (shareable, True)):
            AccessRight.objects.create(
                content_type=ct,
                object_id=str(study.pk),
                access_giver=other,
                access_target=user,
                can_read=True,
                can_share=can_share,
            )
        listed = {item["hash"]: item["can_share"] for item in client.get(STUDIES_URL).json()}
        assert listed == {readable.content_hash: False, shareable.content_hash: True, own.content_hash: True}
        for study in (readable, shareable, own):
            detail = client.get(f"{STUDIES_URL}{study.content_hash}/").json()
            assert detail["can_share"] is listed[study.content_hash]

    def test_listing_costs_the_same_queries_whatever_its_length(self, auth_client, make_user, make_study):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        client, user = auth_client
        other = make_user()
        ct = ContentType.objects.get_for_model(DicomStudy, for_concrete_model=False)

        def grant_one():
            study = make_study(other)
            AccessRight.objects.create(
                content_type=ct, object_id=str(study.pk), access_giver=other, access_target=user, can_read=True
            )

        def cost():
            with CaptureQueriesContext(connection) as queries:
                assert client.get(STUDIES_URL).status_code == 200
            return len(queries)

        grant_one()
        cost()  # the first request also fills the content-type cache
        one = cost()
        for _ in range(4):
            grant_one()
        assert len(client.get(STUDIES_URL).json()) == 5
        assert cost() == one

    def test_detail_counts_ready_instances_per_series(self, auth_client, make_study):
        client, user = auth_client
        study = make_study(user, instance_count=3)
        DicomInstance.objects.filter(pk=DicomInstance.objects.filter(series__study=study).first().pk).update(
            status=DicomInstance.Status.FAILED
        )
        body = client.get(f"{STUDIES_URL}{study.content_hash}/").json()
        assert [s["instance_count"] for s in body["series"]] == [2]

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
        # Root-relative: a scheme and host taken from the request would be wrong behind a TLS proxy.
        assert url.startswith("dicomweb:/plugin/dicom/api/v1/dicom/wado/?")
        assert f"studyUID={study.study_instance_uid}" in url

    def test_ohif_json_leaves_out_instances_that_are_not_ready(self, auth_client, make_study):
        client, user = auth_client
        study = make_study(user, instance_count=3)
        failed = DicomInstance.objects.filter(series__study=study).first()
        DicomInstance.objects.filter(pk=failed.pk).update(status=DicomInstance.Status.FAILED)
        instances = client.get(f"{STUDIES_URL}{study.content_hash}/ohif-json/").json()["studies"][0]["series"][0][
            "instances"
        ]
        assert len(instances) == 2
        assert failed.sop_instance_uid not in {i["metadata"]["SOPInstanceUID"] for i in instances}

    def test_ohif_json_lists_each_frame_of_a_multiframe_instance(self, auth_client, make_study):
        client, user = auth_client
        study = make_study(user)
        DicomInstance.objects.filter(series__study=study).update(number_of_frames=3)
        response = client.get(f"{STUDIES_URL}{study.content_hash}/ohif-json/")
        urls = [i["url"] for i in response.json()["studies"][0]["series"][0]["instances"]]
        # ``frame`` is what OHIF's dicomjson source and the WADO-URI loader read.
        assert [u.rsplit("&", 1)[1] for u in urls] == ["frame=1", "frame=2", "frame=3"]

    def test_ohif_metadata_is_numeric_and_leaves_absent_tags_out(self, auth_client, make_study):
        client, user = auth_client
        study = make_study(user)
        DicomInstance.objects.filter(series__study=study).update(
            instance_number="7",
            pixel_spacing=["0.661468", "0.661468"],
            image_position_patient=["-158.1", "-179.0", "-75.7"],
            rescale_slope="1",
            rescale_intercept="-1024",
            window_center="",
            window_width="",
        )
        response = client.get(f"{STUDIES_URL}{study.content_hash}/ohif-json/")
        metadata = response.json()["studies"][0]["series"][0]["instances"][0]["metadata"]
        assert metadata["InstanceNumber"] == 7
        assert metadata["PixelSpacing"] == [0.661468, 0.661468]
        assert metadata["ImagePositionPatient"] == [-158.1, -179.0, -75.7]
        assert (metadata["RescaleSlope"], metadata["RescaleIntercept"]) == (1, -1024)
        # An empty window would reach OHIF as a zero-width window.
        assert "WindowCenter" not in metadata
        assert "WindowWidth" not in metadata

    def test_ohif_metadata_drops_non_finite_values(self, auth_client, make_study):
        client, user = auth_client
        study = make_study(user)
        DicomInstance.objects.filter(series__study=study).update(window_center="NaN", window_width="inf")
        response = client.get(f"{STUDIES_URL}{study.content_hash}/ohif-json/")
        # Strict parsing: a bare NaN or Infinity would make the payload unparseable in the browser.
        body = json.loads(response.content, parse_constant=lambda c: pytest.fail(f"non-JSON constant {c}"))
        metadata = body["studies"][0]["series"][0]["instances"][0]["metadata"]
        assert "WindowCenter" not in metadata
        assert "WindowWidth" not in metadata

    def test_ohif_instances_follow_numeric_instance_order(self, auth_client, make_study):
        client, user = auth_client
        study = make_study(user, instance_count=3)
        for inst, number in zip(DicomInstance.objects.filter(series__study=study).order_by("pk"), ["10", "2", ""]):
            inst.instance_number = number
            inst.save(update_fields=["instance_number"])
        response = client.get(f"{STUDIES_URL}{study.content_hash}/ohif-json/")
        instances = response.json()["studies"][0]["series"][0]["instances"]
        assert [i["metadata"].get("InstanceNumber") for i in instances] == [2, 10, None]


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
    """The study access surface, which follows the dataset grant rules through ``epicurrents.granting``."""

    def _grant_row(self, study, giver, target, **perms):
        return AccessRight.objects.create(
            content_type=ContentType.objects.get_for_model(study, for_concrete_model=False),
            object_id=str(study.pk),
            access_giver=giver,
            access_target=target,
            **({"can_read": True, "apply_middleware": False} | perms),
        )

    def _grant(self, client, study, target_id, **perms):
        return client.post(
            f"{STUDIES_URL}{study.content_hash}/access/",
            json.dumps({"access_target_id": target_id, **perms}),
            content_type="application/json",
        )

    def test_grant_gives_read_and_returns_the_row(self, auth_client, make_user, make_study):
        client, user = auth_client
        study = make_study(user)
        grantee = make_user()
        response = self._grant(client, study, grantee.pk)
        assert response.status_code == 201
        body = response.json()
        assert body["access_target_username"] == grantee.username
        assert (body["can_read"], body["can_write"], body["can_share"]) == (True, False, False)
        # No DICOM serving path de-identifies, so no row may claim one does.
        assert body["apply_middleware"] is False

        grantee_client = Client()
        grantee_client.force_login(grantee)
        assert grantee_client.get(f"{STUDIES_URL}{study.content_hash}/").status_code == 200

    def test_grant_validations(self, auth_client, make_user, make_study):
        client, user = auth_client
        study = make_study(user)
        grantee = make_user()
        assert self._grant(client, study, 999999).status_code == 400
        assert self._grant(client, study, grantee.pk, can_read=False).status_code == 400
        assert self._grant(client, study, grantee.pk).status_code == 201
        assert self._grant(client, study, grantee.pk).status_code == 409

    def test_read_only_grantee_cannot_manage_access(self, auth_client, make_user, make_study):
        client, user = auth_client
        other = make_user()
        study = make_study(other)
        self._grant_row(study, other, user)
        assert self._grant(client, study, make_user().pk).status_code == 403
        assert client.get(f"{STUDIES_URL}{study.content_hash}/access/").status_code == 403

    def test_delegate_confers_only_what_it_holds(self, auth_client, make_user, make_study):
        client, user = auth_client
        other = make_user()
        study = make_study(other)
        self._grant_row(study, other, user, can_share=True)
        assert self._grant(client, study, make_user().pk, can_write=True).status_code == 403
        assert self._grant(client, study, make_user().pk).status_code == 201

    def test_list_leaves_out_the_authors_own_row(self, auth_client, make_user, make_study):
        client, user = auth_client
        study = make_study(user)
        self._grant_row(study, user, user, can_write=True, can_share=True)
        grantee = make_user()
        self._grant(client, study, grantee.pk)
        response = client.get(f"{STUDIES_URL}{study.content_hash}/access/")
        assert response.status_code == 200
        assert [r["access_target_username"] for r in response.json()] == [grantee.username]

    def test_revoke_by_id_removes_access(self, auth_client, make_user, make_study):
        client, user = auth_client
        study = make_study(user)
        grantee = make_user()
        right_id = self._grant(client, study, grantee.pk).json()["id"]
        response = client.delete(f"{STUDIES_URL}{study.content_hash}/access/{right_id}/")
        assert response.status_code == 200

        grantee_client = Client()
        grantee_client.force_login(grantee)
        assert grantee_client.get(f"{STUDIES_URL}{study.content_hash}/").status_code == 404
        assert client.delete(f"{STUDIES_URL}{study.content_hash}/access/{right_id}/").status_code == 404

    def test_delegate_cannot_revoke_the_authors_row(self, auth_client, make_user, make_study):
        client, user = auth_client
        other = make_user()
        study = make_study(other)
        author_row = self._grant_row(study, other, other, can_write=True, can_share=True)
        self._grant_row(study, other, user, can_share=True)
        assert client.delete(f"{STUDIES_URL}{study.content_hash}/access/{author_row.pk}/").status_code == 403

    def test_registered_share_guard_refuses_the_grant(self, auth_client, make_user, make_study, monkeypatch):
        from epicurrents import granting

        client, user = auth_client
        study = make_study(user)
        monkeypatch.setitem(granting._SHARE_GUARDS, "dicom.dicomstudy", [lambda obj: "held for review"])
        response = self._grant(client, study, make_user().pk)
        assert response.status_code == 409
        assert not AccessRight.objects.filter(object_id=str(study.pk), access_target__isnull=False).exists()

    def test_held_grant_is_neither_listed_nor_revocable(self, auth_client, make_user, make_study, monkeypatch):
        from epicurrents import granting

        client, user = auth_client
        study = make_study(user)
        held = self._grant_row(study, user, make_user())
        monkeypatch.setattr(granting, "_HELD_GRANT_SOURCES", [lambda rows: {r.pk for r in rows if r.pk == held.pk}])
        assert client.get(f"{STUDIES_URL}{study.content_hash}/access/").json() == []
        assert client.delete(f"{STUDIES_URL}{study.content_hash}/access/{held.pk}/").status_code == 404
        assert AccessRight.objects.filter(pk=held.pk).exists()

    def test_listing_says_who_may_share(self, auth_client, make_user, make_study):
        client, user = auth_client
        other = make_user()
        own = make_study(user)
        read_only = make_study(other)
        delegated = make_study(other)
        self._grant_row(read_only, other, user)
        self._grant_row(delegated, other, user, can_share=True)
        listed = {s["hash"]: s["can_share"] for s in client.get(STUDIES_URL).json()}
        assert listed == {own.content_hash: True, read_only.content_hash: False, delegated.content_hash: True}


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

    def test_access_list_grant_and_revoke_verbs(self, auth_client, make_user, make_study):
        client, user = auth_client
        study = make_study(user)
        grantee = make_user()
        right = client.post(
            f"{STUDIES_URL}{study.content_hash}/access/",
            json.dumps({"access_target_id": grantee.pk}),
            content_type="application/json",
        ).json()
        assert Activity.objects.filter(verb="dicom.study.access.grant").exists()
        client.get(f"{STUDIES_URL}{study.content_hash}/access/")
        assert Activity.objects.filter(verb="dicom.study.access.list").exists()
        client.delete(f"{STUDIES_URL}{study.content_hash}/access/{right['id']}/")
        assert Activity.objects.filter(verb="dicom.study.access.revoke").exists()

    def test_wado_verb_with_target(self, auth_client, make_study):
        client, user = auth_client
        study = make_study(user)
        inst = DicomInstance.objects.get(series__study=study)
        client.get(f"{WADO_URL}?objectUID={inst.sop_instance_uid}")
        activity = Activity.objects.filter(verb="dicom.instance.download").first()
        assert activity is not None
        assert activity.target_object_id == str(inst.pk)
