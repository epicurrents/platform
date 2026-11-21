"""Ingest tests for DICOM de-identification: what the upload endpoint and ``index_dicom`` store.

The pass itself is pinned in ``test_deidentify.py``. These tests cover the wiring: the stored file and the rows hold
the cleaned values, the two digests describe the two files, a study uploaded in several requests converges on one,
the as-uploaded digest reaches no response, the originals volume receives the source under the preservation mode,
the setting off stores bytes unchanged, and an empty key refuses before anything is staged.
"""

import hashlib
import io
import json

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import CommandError, call_command
from pydicom import dcmread

from plugins.dicom.deidentify import DICOM_DEIDENTIFICATION_VERSION
from plugins.dicom.ingest import refresh_study_aggregates
from plugins.dicom.models import DicomInstance, DicomStudy
from plugins.dicom.tests.conftest import STUDIES_URL, UPLOAD_URL, WADO_URL, stored_uid

STUDY_UID = "1.2.826.0.1.6666001"
SERIES_UID = "1.2.826.0.1.6666001.1"
DICOMWEB = "/plugin/dicom/api/v1/dicomweb"


def _upload(client, *payloads):
    files = [SimpleUploadedFile(f"f{i}.dcm", data, content_type="application/dicom") for i, data in enumerate(payloads)]
    return client.post(UPLOAD_URL, {"files": files})


def _payload(make_dicom_bytes, n=1, **extra):
    return make_dicom_bytes(
        study_uid=STUDY_UID,
        series_uid=SERIES_UID,
        sop_uid=f"{SERIES_UID}.{n}",
        patient_name="Roe^Richard",
        patient_id="MRN-4242",
        extra=extra or None,
    )


@pytest.mark.django_db
class TestUploadStoresTheCleanedFile:
    def test_stored_bytes_and_rows_are_de_identified(self, auth_client, make_dicom_bytes, dicom_dirs):
        client, _ = auth_client
        upload_dir, staging_dir = dicom_dirs
        source = _payload(make_dicom_bytes)
        assert _upload(client, source).status_code == 200

        inst = DicomInstance.objects.get()
        stored = (upload_dir / inst.stored_name).read_bytes()
        assert b"Roe" not in stored and b"MRN-4242" not in stored
        ds = dcmread(io.BytesIO(stored))
        assert ds.PatientIdentityRemoved == "YES"
        assert ds.StudyInstanceUID == stored_uid(STUDY_UID)

        study = inst.series.study
        assert (study.patient_name, study.patient_id) == ("", "X")
        assert study.study_date == "19850101"
        assert study.study_description == "Test study"
        assert inst.series.series_instance_uid == stored_uid(SERIES_UID)
        assert not any(staging_dir.iterdir())

    def test_the_two_digests_describe_the_two_files(self, auth_client, make_dicom_bytes, dicom_dirs):
        client, _ = auth_client
        upload_dir, _ = dicom_dirs
        source = _payload(make_dicom_bytes)
        _upload(client, source)
        inst = DicomInstance.objects.get()
        stored = (upload_dir / inst.stored_name).read_bytes()
        assert inst.source_hash == hashlib.sha256(source).hexdigest()
        assert inst.file_hash == hashlib.sha256(stored).hexdigest()
        assert inst.file_size == len(stored)
        assert inst.file_hash != inst.source_hash

    def test_version_is_stamped_on_instance_and_study(self, auth_client, make_dicom_bytes):
        client, _ = auth_client
        _upload(client, _payload(make_dicom_bytes))
        inst = DicomInstance.objects.get()
        assert inst.deidentification_version == DICOM_DEIDENTIFICATION_VERSION
        assert inst.series.study.deidentification_version == DICOM_DEIDENTIFICATION_VERSION
        assert inst.patient_characteristics_retained is False

    def test_retain_option_is_stamped(self, auth_client, make_dicom_bytes, settings):
        client, _ = auth_client
        settings.DICOM_RETAIN_PATIENT_CHARACTERISTICS = True
        _upload(client, _payload(make_dicom_bytes))
        study = DicomStudy.objects.get()
        assert study.patient_sex == "O"
        assert study.patient_characteristics_retained is True

    def test_a_split_batch_converges_on_one_study(self, auth_client, make_dicom_bytes):
        client, _ = auth_client
        _upload(client, _payload(make_dicom_bytes, 1))
        _upload(client, _payload(make_dicom_bytes, 2))
        study = DicomStudy.objects.get()
        assert study.num_instances == 2
        assert study.series.count() == 1

    def test_a_raw_instance_makes_the_study_count_as_raw(self, auth_client, make_dicom_bytes):
        client, _ = auth_client
        _upload(client, _payload(make_dicom_bytes, 1), _payload(make_dicom_bytes, 2))
        study = DicomStudy.objects.get()
        DicomInstance.objects.filter(pk=DicomInstance.objects.first().pk).update(deidentification_version=None)
        refresh_study_aggregates(study)
        assert study.deidentification_version is None

    def test_the_source_digest_reaches_no_response(self, auth_client, make_dicom_bytes):
        client, _ = auth_client
        body = _upload(client, _payload(make_dicom_bytes)).content
        inst = DicomInstance.objects.get()
        study = inst.series.study
        uid = study.study_instance_uid
        responses = [
            body,
            client.get(STUDIES_URL).content,
            client.get(f"{STUDIES_URL}{study.content_hash}/").content,
            client.get(f"{STUDIES_URL}{study.content_hash}/ohif-json/").content,
            client.get(f"{DICOMWEB}/studies").content,
            client.get(f"{DICOMWEB}/studies/{uid}/metadata").content,
            client.get(f"{DICOMWEB}/studies/{uid}/series").content,
        ]
        for content in responses:
            assert inst.source_hash.encode() not in content

    def test_wado_serves_the_cleaned_file(self, auth_client, make_dicom_bytes):
        client, _ = auth_client
        _upload(client, _payload(make_dicom_bytes))
        inst = DicomInstance.objects.get()
        study = inst.series.study
        response = client.get(
            WADO_URL,
            {
                "requestType": "WADO",
                "studyUID": study.study_instance_uid,
                "seriesUID": inst.series.series_instance_uid,
                "objectUID": inst.sop_instance_uid,
            },
        )
        assert response.status_code == 200
        assert b"Roe" not in b"".join(response.streaming_content)


@pytest.mark.django_db
class TestOriginals:
    @pytest.fixture
    def originals(self, settings, tmp_path):
        root = tmp_path / "originals"
        root.mkdir()
        settings.RECORDINGS_ORIGINALS_PATH = str(root)
        return root

    def test_mode_all_preserves_the_source(self, auth_client, make_dicom_bytes, settings, originals):
        settings.RECORDINGS_PRESERVE_MODE = "all"
        client, user = auth_client
        source = _payload(make_dicom_bytes)
        _upload(client, source)
        inst = DicomInstance.objects.get()
        directory = originals / "plugins" / "dicom" / inst.stored_name.split(".", 1)[0]
        assert (directory / "f0.dcm").read_bytes() == source
        manifest = json.loads((directory / "manifest.json").read_text())
        assert manifest["source_hash"] == inst.source_hash
        assert manifest["stored_hash"] == inst.file_hash
        assert manifest["author_id"] == user.pk
        assert manifest["preservation_reason"] == "all"
        assert STUDY_UID not in json.dumps(manifest)

    def test_mode_none_preserves_nothing(self, auth_client, make_dicom_bytes, settings, originals):
        settings.RECORDINGS_PRESERVE_MODE = "none"
        client, _ = auth_client
        _upload(client, _payload(make_dicom_bytes))
        assert not any(originals.iterdir())

    def test_a_duplicate_is_not_preserved(self, auth_client, make_dicom_bytes, settings, originals):
        settings.RECORDINGS_PRESERVE_MODE = "all"
        client, _ = auth_client
        payload = _payload(make_dicom_bytes)
        _upload(client, payload)
        _upload(client, payload)
        assert len(list((originals / "plugins" / "dicom").iterdir())) == 1

    def test_erase_user_lists_the_authors_preserved_originals(self, auth_client, make_dicom_bytes, settings, originals):
        settings.RECORDINGS_PRESERVE_MODE = "all"
        client, user = auth_client
        _upload(client, _payload(make_dicom_bytes))
        prefix = DicomInstance.objects.get().stored_name.split(".", 1)[0]
        out = io.StringIO()
        call_command("erase_user", user.username, "--yes", stdout=out)
        assert f"plugins/dicom/{prefix}" in out.getvalue()

    def test_validate_originals_passes_over_the_plugin_directory(
        self, settings, originals, auth_client, make_dicom_bytes
    ):
        settings.RECORDINGS_PRESERVE_MODE = "all"
        client, _ = auth_client
        _upload(client, _payload(make_dicom_bytes))
        out = io.StringIO()
        call_command("validate_originals", "--json", stdout=out)
        report = json.loads(out.getvalue())
        assert not report["malformed"] and not report["orphans"]


@pytest.mark.django_db
class TestSettings:
    def test_off_stores_the_bytes_as_uploaded(self, auth_client, make_dicom_bytes, dicom_dirs, settings):
        settings.DICOM_DEIDENTIFY_ON_INGEST = False
        client, _ = auth_client
        upload_dir, staging_dir = dicom_dirs
        source = _payload(make_dicom_bytes)
        _upload(client, source)
        inst = DicomInstance.objects.get()
        assert (upload_dir / inst.stored_name).read_bytes() == source
        assert inst.file_hash == inst.source_hash == hashlib.sha256(source).hexdigest()
        assert inst.deidentification_version is None
        assert inst.series.study.study_instance_uid == STUDY_UID
        assert inst.series.study.deidentification_version is None
        assert not any(staging_dir.iterdir())

    def test_an_empty_key_refuses_before_staging(self, auth_client, make_dicom_bytes, dicom_dirs, settings):
        settings.DICOM_UID_KEY = ""
        client, _ = auth_client
        _, staging_dir = dicom_dirs
        response = _upload(client, _payload(make_dicom_bytes))
        assert response.status_code == 503
        assert "DICOM_UID_KEY" not in response.content.decode()
        assert DicomInstance.objects.count() == 0
        assert not any(staging_dir.iterdir())

    def test_an_empty_key_does_not_matter_when_off(self, auth_client, make_dicom_bytes, settings):
        settings.DICOM_UID_KEY = ""
        settings.DICOM_DEIDENTIFY_ON_INGEST = False
        client, _ = auth_client
        assert _upload(client, _payload(make_dicom_bytes)).status_code == 200

    def test_the_system_check_warns_on_an_empty_key(self, settings):
        from plugins.dicom.checks import check_uid_key

        assert check_uid_key(None) == []
        settings.DICOM_UID_KEY = ""
        assert [w.id for w in check_uid_key(None)] == ["dicom.W001"]
        settings.DICOM_DEIDENTIFY_ON_INGEST = False
        assert check_uid_key(None) == []

    def test_a_file_the_pass_cannot_rewrite_is_refused(self, auth_client, make_dicom_bytes, dicom_dirs, monkeypatch):
        from plugins.dicom import deidentify

        def fail(*args, **kwargs):
            raise deidentify.DeidentificationError("no rule")

        monkeypatch.setattr(deidentify, "deidentify_file", fail)
        client, _ = auth_client
        _, staging_dir = dicom_dirs
        response = _upload(client, _payload(make_dicom_bytes))
        assert response.status_code == 400
        assert DicomInstance.objects.count() == 0
        assert not any(staging_dir.iterdir())


@pytest.mark.django_db
class TestIndexCommand:
    def test_the_archive_is_read_and_the_store_cleaned(self, user, make_dicom_file, tmp_path, dicom_dirs):
        upload_dir, _ = dicom_dirs
        path = make_dicom_file(tmp_path / "archive", study_uid=STUDY_UID, patient_name="Roe^Richard")
        source = path.read_bytes()
        call_command("index_dicom", str(path.parent), "--user", user.username)
        assert path.read_bytes() == source
        inst = DicomInstance.objects.get()
        assert b"Roe" not in (upload_dir / inst.stored_name).read_bytes()
        assert inst.source_hash == hashlib.sha256(source).hexdigest()
        assert inst.deidentification_version == DICOM_DEIDENTIFICATION_VERSION

    def test_resume_matches_on_the_source_digest(self, user, make_dicom_file, tmp_path):
        path = make_dicom_file(tmp_path / "archive")
        call_command("index_dicom", str(path.parent), "--user", user.username)
        out = io.StringIO()
        call_command("index_dicom", str(path.parent), "--user", user.username, "--resume", stdout=out)
        assert "skipped 1 (resume)" in out.getvalue()

    def test_an_empty_key_refuses_to_start(self, user, make_dicom_file, tmp_path, settings):
        settings.DICOM_UID_KEY = ""
        path = make_dicom_file(tmp_path / "archive")
        with pytest.raises(CommandError, match="DICOM_UID_KEY"):
            call_command("index_dicom", str(path.parent), "--user", user.username)
        assert DicomInstance.objects.count() == 0


def _image(make_dicom_bytes, n, modality="CT", **extra):
    return make_dicom_bytes(
        study_uid=STUDY_UID, series_uid=SERIES_UID, sop_uid=f"{SERIES_UID}.{n}", modality=modality, extra=extra or None
    )


def _upload_confirmed(client, *payloads):
    files = [SimpleUploadedFile(f"f{i}.dcm", data, content_type="application/dicom") for i, data in enumerate(payloads)]
    return client.post(f"{UPLOAD_URL}?confirm_burned_in=true", {"files": files})


@pytest.mark.django_db
class TestBurnedInConfirmation:
    @pytest.mark.parametrize(
        ("modality", "extra"),
        [("US", {}), ("SC", {}), ("OT", {}), ("CT", {"BurnedInAnnotation": "YES"})],
    )
    def test_each_trigger_is_held_without_confirmation(
        self, auth_client, make_dicom_bytes, dicom_dirs, modality, extra
    ):
        client, _ = auth_client
        upload_dir, staging_dir = dicom_dirs
        response = _upload(client, _image(make_dicom_bytes, 1, modality, **extra))
        assert response.status_code == 200
        body = response.json()
        assert body["accepted"] == 0
        assert body["files"][0]["code"] == "burned_in_confirmation_required"
        assert DicomInstance.objects.count() == 0
        assert DicomStudy.objects.count() == 0
        assert not any(staging_dir.iterdir()) and not any(upload_dir.iterdir())

    @pytest.mark.parametrize(
        "sop_class", ["1.2.840.10008.5.1.4.1.1.7", "1.2.840.10008.5.1.4.1.1.7.4", "1.2.840.10008.5.1.4.1.1.6.1"]
    )
    def test_a_capture_or_ultrasound_class_is_held_whatever_its_modality(
        self, auth_client, make_dicom_bytes, sop_class
    ):
        client, _ = auth_client
        payload = make_dicom_bytes(study_uid=STUDY_UID, modality="CT", sop_class_uid=sop_class)
        assert _upload(client, payload).json()["files"][0]["code"] == "burned_in_confirmation_required"

    def test_an_image_without_a_trigger_needs_no_confirmation(self, auth_client, make_dicom_bytes):
        client, _ = auth_client
        _upload(client, _image(make_dicom_bytes, 1, "CT", BurnedInAnnotation="NO"))
        assert DicomInstance.objects.get().pixel_review == DicomInstance.PixelReview.NOT_REQUIRED

    def test_a_mixed_batch_is_partly_accepted(self, auth_client, make_dicom_bytes):
        client, _ = auth_client
        body = _upload(client, _image(make_dicom_bytes, 1), _image(make_dicom_bytes, 2, "US")).json()
        assert body["accepted"] == 1
        assert [(f["index"], f["accepted"], f["code"]) for f in body["files"]] == [
            (0, True, None),
            (1, False, "burned_in_confirmation_required"),
        ]

    def test_a_confirmed_resubmission_is_accepted_and_marked_attested(self, auth_client, make_dicom_bytes):
        client, _ = auth_client
        payload = _image(make_dicom_bytes, 1, "US")
        _upload(client, payload)
        response = _upload_confirmed(client, payload)
        assert response.json()["accepted"] == 1
        assert DicomInstance.objects.get().pixel_review == DicomInstance.PixelReview.ATTESTED

    def test_the_audit_row_carries_counts_and_the_flag_only(self, auth_client, make_dicom_bytes):
        from activity.models import Activity

        client, _ = auth_client
        _upload(client, _image(make_dicom_bytes, 1), _image(make_dicom_bytes, 2, "US"))
        activity = Activity.objects.filter(verb="dicom.study.upload").latest("pk")
        assert activity.metadata == {
            "study_count": 1,
            "accepted": 1,
            "rejected": 1,
            "confirm_burned_in": False,
            "burned_in_attested": 0,
            "burned_in_held": 1,
        }
        _upload_confirmed(client, _image(make_dicom_bytes, 2, "US"))
        activity = Activity.objects.filter(verb="dicom.study.upload").latest("pk")
        assert (activity.metadata["confirm_burned_in"], activity.metadata["burned_in_attested"]) == (True, 1)

    def test_a_batch_of_unparseable_files_is_still_a_400(self, auth_client):
        client, _ = auth_client
        assert _upload(client, b"not dicom").status_code == 400

    def test_no_confirmation_is_asked_for_with_de_identification_off(self, auth_client, make_dicom_bytes, settings):
        settings.DICOM_DEIDENTIFY_ON_INGEST = False
        client, _ = auth_client
        assert _upload(client, _image(make_dicom_bytes, 1, "US")).json()["accepted"] == 1
        assert DicomInstance.objects.get().pixel_review == ""

    def test_index_dicom_holds_until_confirmed(self, user, make_dicom_file, tmp_path):
        src = tmp_path / "archive"
        make_dicom_file(src, modality="US")
        out = io.StringIO()
        call_command("index_dicom", str(src), "--user", user.username, stdout=out)
        assert DicomInstance.objects.count() == 0
        assert "Held back 1 image(s)" in out.getvalue()
        call_command("index_dicom", str(src), "--user", user.username, "--confirm-burned-in", stdout=io.StringIO())
        assert DicomInstance.objects.get().pixel_review == DicomInstance.PixelReview.ATTESTED


@pytest.mark.django_db
class TestDescriptions:
    def test_an_identifier_in_a_description_is_warned_about_and_kept(self, auth_client, make_dicom_bytes):
        client, _ = auth_client
        payload = make_dicom_bytes(
            study_uid=STUDY_UID, extra={"StudyDescription": "Jane Doe", "SeriesDescription": "MRN 12345678"}
        )
        body = _upload(client, payload).json()
        study = DicomStudy.objects.get()
        assert study.study_description == "Jane Doe"
        assert {(w["field"], w["kind"], w["study_hash"]) for w in body["warnings"]} == {
            ("study_description", "person_name", study.content_hash),
            ("series_description", "digit_run", study.content_hash),
        }

    def test_plain_descriptions_warn_nothing(self, auth_client, make_dicom_bytes):
        client, _ = auth_client
        assert _upload(client, _payload(make_dicom_bytes)).json()["warnings"] == []

    def test_the_study_listing_says_whether_the_study_is_de_identified(self, auth_client, make_dicom_bytes):
        client, _ = auth_client
        _upload(client, _payload(make_dicom_bytes))
        assert client.get(STUDIES_URL).json()[0]["deidentification_version"] == DICOM_DEIDENTIFICATION_VERSION
