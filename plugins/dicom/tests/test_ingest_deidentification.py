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
from django.test import Client
from pydicom import dcmread
from pydicom.uid import UID

from epicurrents.models import AccessRight
from plugins.dicom import ingest
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
        client, user = auth_client
        upload_dir, staging_dir = dicom_dirs
        source = _payload(make_dicom_bytes)
        assert _upload(client, source).status_code == 200

        inst = DicomInstance.objects.get()
        stored = (upload_dir / inst.stored_name).read_bytes()
        assert b"Roe" not in stored and b"MRN-4242" not in stored
        ds = dcmread(io.BytesIO(stored))
        assert ds.PatientIdentityRemoved == "YES"
        assert ds.StudyInstanceUID == stored_uid(STUDY_UID, user)

        study = inst.series.study
        assert (study.patient_name, study.patient_id) == ("", "X")
        assert study.study_date == "19850101"
        assert study.study_description == "Test study"
        assert inst.series.series_instance_uid == stored_uid(SERIES_UID, user)
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


def _share(study, giver, reader):
    from django.contrib.contenttypes.models import ContentType

    AccessRight.objects.create(
        content_type=ContentType.objects.get_for_model(study, for_concrete_model=False),
        object_id=str(study.pk),
        access_giver=giver,
        access_target=reader,
        can_read=True,
    )


@pytest.mark.django_db
class TestUidsArePerAuthor:
    """The replacement UID is keyed on the uploading author, so it answers no question about another's upload."""

    @pytest.fixture
    def other(self, make_user):
        user = make_user()
        client = Client()
        client.force_login(user)
        return client, user

    def test_two_authors_uploading_one_file_share_no_uid(self, auth_client, other, make_dicom_bytes):
        client, user = auth_client
        other_client, other_user = other
        source = _payload(make_dicom_bytes)
        _upload(client, source)
        _upload(other_client, source)
        own = DicomInstance.objects.get(series__study__author=user)
        theirs = DicomInstance.objects.get(series__study__author=other_user)
        assert own.sop_instance_uid != theirs.sop_instance_uid
        assert own.series.series_instance_uid != theirs.series.series_instance_uid
        assert own.series.study.study_instance_uid != theirs.series.study.study_instance_uid
        assert own.series.study.study_instance_uid == stored_uid(STUDY_UID, user)
        assert theirs.series.study.study_instance_uid == stored_uid(STUDY_UID, other_user)
        # The same bytes in, so the as-uploaded digest is equal; it is the field no response carries.
        assert own.source_hash == theirs.source_hash
        assert own.file_hash != theirs.file_hash

    def test_a_stub_carrying_a_guessed_uid_confirms_nothing_about_a_shared_study(
        self, auth_client, other, make_dicom_bytes
    ):
        """The probe: a reader of a shared study uploads a stub with the source UIDs they suspect it had."""
        client, author = auth_client
        probe_client, prober = other
        _upload(client, _payload(make_dicom_bytes))
        shared = DicomStudy.objects.get(author=author)
        _share(shared, author, prober)
        shared_instance = DicomInstance.objects.get(series__study=shared)

        # What the prober reads of the shared study before probing.
        listed = probe_client.get(STUDIES_URL).content.decode()
        assert shared.study_instance_uid in listed

        stub = make_dicom_bytes(
            study_uid=STUDY_UID, series_uid=SERIES_UID, sop_uid=f"{SERIES_UID}.1", patient_name="X", patient_id="X"
        )
        response = _upload(probe_client, stub)
        assert response.status_code == 200
        body = response.json()
        # Accepted as a new study of the prober's own: not a duplicate of the shared one, and not joined to it.
        assert body["accepted"] == 1
        probe_study = DicomStudy.objects.get(author=prober)
        assert body["studies"][0]["hash"] == probe_study.content_hash != shared.content_hash
        assert shared.num_instances == DicomStudy.objects.get(pk=shared.pk).num_instances == 1

        probe_instance = DicomInstance.objects.get(series__study=probe_study)
        learned = "".join(
            [
                response.content.decode(),
                probe_client.get(f"{STUDIES_URL}{probe_study.content_hash}/").content.decode(),
                probe_client.get(f"{STUDIES_URL}{probe_study.content_hash}/ohif-json/").content.decode(),
                probe_client.get(f"{DICOMWEB}/studies/{probe_study.study_instance_uid}/metadata").content.decode(),
                probe_client.get(f"{DICOMWEB}/studies/{probe_study.study_instance_uid}/series").content.decode(),
            ]
        )
        for uid in (
            shared.study_instance_uid,
            shared_instance.series.series_instance_uid,
            shared_instance.sop_instance_uid,
        ):
            assert uid not in learned
        assert probe_study.study_instance_uid in learned
        # Both studies are listed to the prober as two studies, under two UIDs.
        searched = probe_client.get(f"{DICOMWEB}/studies").content.decode()
        assert shared.study_instance_uid in searched and probe_study.study_instance_uid in searched
        assert probe_instance.sop_instance_uid != shared_instance.sop_instance_uid

    def test_a_shared_study_still_opens_by_its_uid_for_a_reader_holding_a_copy(
        self, auth_client, other, make_dicom_bytes
    ):
        client, author = auth_client
        reader_client, reader = other
        source = _payload(make_dicom_bytes)
        _upload(client, source)
        _upload(reader_client, source)
        shared = DicomStudy.objects.get(author=author)
        _share(shared, author, reader)
        own = DicomStudy.objects.get(author=reader)
        for study in (shared, own):
            body = reader_client.get(f"{DICOMWEB}/studies/{study.study_instance_uid}/series")
            assert body.status_code == 200
            instance = DicomInstance.objects.get(series__study=study)
            wado = reader_client.get(WADO_URL, {"requestType": "WADO", "objectUID": instance.sop_instance_uid})
            assert wado.status_code == 200

    def test_one_authors_upload_and_import_converge(self, auth_client, make_dicom_bytes, tmp_path):
        """The command keys on the owner it is given, as the endpoint keys on the caller."""
        client, user = auth_client
        _upload(client, _payload(make_dicom_bytes, 1))
        archive = tmp_path / "archive"
        archive.mkdir()
        (archive / "second.dcm").write_bytes(_payload(make_dicom_bytes, 2))
        (archive / "first-again.dcm").write_bytes(_payload(make_dicom_bytes, 1, PatientComments="another digest"))
        out = io.StringIO()
        call_command("index_dicom", str(archive), "--user", user.username, stdout=out)
        study = DicomStudy.objects.get()
        assert study.study_instance_uid == stored_uid(STUDY_UID, user)
        assert study.num_instances == 2
        assert "1 duplicate(s)" in out.getvalue()

    def test_an_import_for_another_owner_is_a_separate_study(self, auth_client, other, make_dicom_bytes, tmp_path):
        client, user = auth_client
        _, other_user = other
        source = _payload(make_dicom_bytes)
        _upload(client, source)
        archive = tmp_path / "archive"
        archive.mkdir()
        (archive / "same.dcm").write_bytes(source)
        call_command("index_dicom", str(archive), "--user", other_user.username, stdout=io.StringIO())
        imported = DicomStudy.objects.get(author=other_user)
        assert imported.study_instance_uid == stored_uid(STUDY_UID, other_user)
        assert imported.study_instance_uid != DicomStudy.objects.get(author=user).study_instance_uid

    def test_references_between_one_authors_files_resolve_after_the_pass(
        self, auth_client, make_dicom_bytes, dicom_dirs
    ):
        from pydicom import Dataset, Sequence

        client, _ = auth_client
        upload_dir, _ = dicom_dirs
        reference = Dataset()
        reference.ReferencedSOPClassUID = "1.2.840.10008.5.1.4.1.1.2"
        reference.ReferencedSOPInstanceUID = f"{SERIES_UID}.2"
        first = _payload(make_dicom_bytes, 1, ReferencedImageSequence=Sequence([reference]))
        _upload(client, first)
        _upload(client, _payload(make_dicom_bytes, 2))
        stored = {inst.sop_instance_uid: dcmread(upload_dir / inst.stored_name) for inst in DicomInstance.objects.all()}
        referenced = [
            ds.ReferencedImageSequence[0].ReferencedSOPInstanceUID
            for ds in stored.values()
            if "ReferencedImageSequence" in ds
        ]
        assert len(referenced) == 1 and referenced[0] in stored
        assert stored[referenced[0]].SOPInstanceUID == referenced[0]


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

    def test_erase_user_tells_the_operator_to_search_the_manifests(self, user, settings, originals):
        """An original with no instance row is tied to its uploader by the manifest alone, which nothing here reads."""
        settings.RECORDINGS_PRESERVE_MODE = "failed"
        author_id = user.pk
        out = io.StringIO()
        call_command("erase_user", user.username, "--yes", stdout=out)
        line = next(line for line in out.getvalue().splitlines() if "manifest.json" in line)
        assert "plugins/dicom/*/manifest.json" in line
        assert f'"author_id": {author_id}' in line

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


#: Every (Modality, SOP class) pair the upload gate stores without a confirmation.
ALLOWLISTED = [
    ("CT", "1.2.840.10008.5.1.4.1.1.2"),
    ("CT", "1.2.840.10008.5.1.4.1.1.2.1"),
    ("MR", "1.2.840.10008.5.1.4.1.1.4"),
    ("MR", "1.2.840.10008.5.1.4.1.1.4.1"),
    ("PT", "1.2.840.10008.5.1.4.1.1.128"),
    ("PT", "1.2.840.10008.5.1.4.1.1.130"),
]


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

    @pytest.mark.parametrize(("modality", "sop_class"), ALLOWLISTED)
    def test_each_allowlisted_acquisition_is_stored_without_confirmation(
        self, auth_client, make_dicom_bytes, dicom_dirs, modality, sop_class
    ):
        client, _ = auth_client
        upload_dir, _ = dicom_dirs
        payload = make_dicom_bytes(study_uid=STUDY_UID, modality=modality, sop_class_uid=sop_class)
        assert _upload(client, payload).json()["accepted"] == 1
        inst = DicomInstance.objects.get()
        assert inst.pixel_review == DicomInstance.PixelReview.NOT_REQUIRED
        assert dcmread(upload_dir / inst.stored_name).PatientIdentityRemoved == "YES"

    def test_the_allowlist_is_exactly_the_pairs_these_tests_cover(self):
        pairs = {(m, c) for m, classes in ingest.ACQUISITION_ALLOWLIST.items() for c in classes}
        assert pairs == set(ALLOWLISTED)
        assert all(UID(c).name.endswith("Image Storage") for _, c in pairs)

    @pytest.mark.parametrize(("modality", "sop_class"), ALLOWLISTED)
    @pytest.mark.parametrize(
        "extra",
        [
            {"BurnedInAnnotation": "YES"},
            {"BurnedInAnnotation": "yes "},
            {"RecognizableVisualFeatures": "YES"},
            {"ImageType": ["DERIVED", "PRIMARY", "AXIAL"]},
            {"ImageType": ["DERIVED", "SECONDARY", "SCREEN SAVE"]},
            {"ImageType": ["ORIGINAL", "SECONDARY"]},
            {"ImageType": ["ORIGINAL"]},
            {"ImageType": None},
        ],
        ids=["burned-in", "burned-in-padded", "recognizable", "derived", "screen-save", "secondary", "short", "absent"],
    )
    def test_an_allowlisted_modality_is_held_when_a_condition_fails(
        self, auth_client, make_dicom_bytes, modality, sop_class, extra
    ):
        client, _ = auth_client
        payload = make_dicom_bytes(study_uid=STUDY_UID, modality=modality, sop_class_uid=sop_class, extra=extra)
        assert _upload(client, payload).json()["files"][0]["code"] == "burned_in_confirmation_required"
        assert DicomInstance.objects.count() == 0

    @pytest.mark.parametrize(
        ("modality", "sop_class"),
        [
            ("XC", "1.2.840.10008.5.1.4.1.1.77.1.4"),  # VL Photographic Image
            ("ES", "1.2.840.10008.5.1.4.1.1.77.1.1"),  # VL Endoscopic Image
            ("GM", "1.2.840.10008.5.1.4.1.1.77.1.2"),  # VL Microscopic Image
            ("SM", "1.2.840.10008.5.1.4.1.1.77.1.6"),  # VL Whole Slide Microscopy Image
            ("DX", "1.2.840.10008.5.1.4.1.1.1.1"),  # Digital X-Ray Image, for presentation
            ("CR", "1.2.840.10008.5.1.4.1.1.1"),  # Computed Radiography Image
            ("XA", "1.2.840.10008.5.1.4.1.1.12.1"),  # X-Ray Angiographic Image
            ("RF", "1.2.840.10008.5.1.4.1.1.12.2"),  # X-Ray Radiofluoroscopic Image
            ("MG", "1.2.840.10008.5.1.4.1.1.1.2"),  # Digital Mammography X-Ray Image
            ("NM", "1.2.840.10008.5.1.4.1.1.20"),  # Nuclear Medicine Image
            ("US", "1.2.840.10008.5.1.4.1.1.6.1"),  # Ultrasound Image
            ("SR", "1.2.840.10008.5.1.4.1.1.88.11"),  # Basic Text SR
            ("DOC", "1.2.840.10008.5.1.4.1.1.104.1"),  # Encapsulated PDF
            ("PR", "1.2.840.10008.5.1.4.1.1.11.1"),  # Grayscale Softcopy Presentation State
            ("MR", "1.2.840.10008.5.1.4.1.1.4.2"),  # MR Spectroscopy
            ("MR", "1.2.840.10008.5.1.4.1.1.4.3"),  # Enhanced MR Color Image
            ("CT", "1.2.840.10008.5.1.4.1.1.2.2"),  # Legacy Converted Enhanced CT Image
            ("CT", "1.2.840.10008.5.1.4.1.1.4"),  # an MR class under the CT modality
            ("MR", "1.2.840.10008.5.1.4.1.1.2"),  # a CT class under the MR modality
            ("CT", "1.3.12.2.1107.5.9.1"),  # a private class
            ("", "1.2.840.10008.5.1.4.1.1.2"),  # no Modality
            ("ZZ", "1.2.840.10008.5.1.4.1.1.2"),  # a Modality nobody defines
        ],
    )
    def test_everything_off_the_allowlist_is_held(self, auth_client, make_dicom_bytes, modality, sop_class):
        client, _ = auth_client
        payload = make_dicom_bytes(study_uid=STUDY_UID, modality=modality, sop_class_uid=sop_class)
        body = _upload(client, payload).json()
        assert body["files"][0]["code"] == "burned_in_confirmation_required"
        assert DicomInstance.objects.count() == 0

    def test_a_missing_modality_attribute_is_held(self, make_dicom_bytes, tmp_path):
        from pydicom import dcmread as read

        path = tmp_path / "no-modality.dcm"
        path.write_bytes(make_dicom_bytes())
        ds = read(path)
        assert not ingest.pixel_review_required(ds)
        del ds.Modality
        assert ingest.pixel_review_required(ds)

    @pytest.mark.parametrize(
        ("modality", "sop_class", "extra"),
        [
            ("CT", "1.2.840.10008.5.1.4.1.1.2", {"BurnedInAnnotation": "YES"}),
            ("CT", "1.2.840.10008.5.1.4.1.1.2", {"ImageType": ["DERIVED", "SECONDARY", "SCREEN SAVE"]}),
            ("CT", "1.2.840.10008.5.1.4.1.1.7", {}),
            ("MR", "1.2.840.10008.5.1.4.1.1.4", {"RecognizableVisualFeatures": "YES"}),
            ("XC", "1.2.840.10008.5.1.4.1.1.77.1.4", {}),
            ("SM", "1.2.840.10008.5.1.4.1.1.77.1.6", {}),
            ("DX", "1.2.840.10008.5.1.4.1.1.1.1", {}),
            ("", "1.2.840.10008.5.1.4.1.1.2", {}),
        ],
    )
    def test_an_attested_resubmission_stores_a_held_file_as_attested_and_stamps_it_no(
        self, auth_client, make_dicom_bytes, dicom_dirs, modality, sop_class, extra
    ):
        client, _ = auth_client
        upload_dir, _ = dicom_dirs
        payload = make_dicom_bytes(study_uid=STUDY_UID, modality=modality, sop_class_uid=sop_class, extra=extra or None)
        assert _upload(client, payload).json()["accepted"] == 0
        assert _upload_confirmed(client, payload).json()["accepted"] == 1
        inst = DicomInstance.objects.get()
        assert inst.pixel_review == DicomInstance.PixelReview.ATTESTED
        stored = dcmread(upload_dir / inst.stored_name)
        assert stored.PatientIdentityRemoved == "NO"
        assert [i.CodeValue for i in stored.DeidentificationMethodCodeSequence] == ["113100"]
        assert stored.LongitudinalTemporalInformationModified == "REMOVED"
        assert stored.PatientName == ""

    def test_a_confirmation_does_not_change_what_the_allowlist_passed(self, auth_client, make_dicom_bytes, dicom_dirs):
        """The flag covers the batch; a file that needed no check is not recorded as checked."""
        client, _ = auth_client
        upload_dir, _ = dicom_dirs
        _upload_confirmed(client, _image(make_dicom_bytes, 1))
        inst = DicomInstance.objects.get()
        assert inst.pixel_review == DicomInstance.PixelReview.NOT_REQUIRED
        assert dcmread(upload_dir / inst.stored_name).PatientIdentityRemoved == "YES"

    def test_the_refusal_text_claims_no_check_by_the_platform(self, auth_client, make_dicom_bytes):
        client, _ = auth_client
        error = _upload(client, _image(make_dicom_bytes, 1, "US")).json()["files"][0]["error"]
        assert "Check it and confirm" in error
        for claim in ("verified", "scanned", "detected", "anonym"):
            assert claim not in error.lower()

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
