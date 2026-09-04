"""pytest configuration for the dicom plugin test suite.

Run with the plugin test settings:

    DJANGO_SETTINGS_MODULE=plugins.dicom.settings_test pytest plugins/dicom/tests/

``use_dicom_urlconf`` mounts the plugin API at the real
``/plugin/dicom/api/v1/`` prefix for every test; ``dicom_dirs`` points the
storage paths at a per-test tmp_path so no test touches real volumes. The
``make_dicom_bytes`` / ``make_dicom_file`` factories build minimal valid
DICOM Secondary Capture files with pydicom.
"""

import io
import uuid

import pytest

SC_SOP_CLASS_UID = "1.2.840.10008.5.1.4.1.1.7"  # Secondary Capture Image Storage


@pytest.fixture(autouse=True)
def use_dicom_urlconf(settings):
    settings.ROOT_URLCONF = "plugins.dicom.tests.urls"


@pytest.fixture(autouse=True)
def dicom_dirs(settings, tmp_path):
    """Point DICOM storage at a per-test tmp_path; returns (upload, staging)."""
    upload = tmp_path / "dicom-upload"
    staging = tmp_path / "dicom-staging"
    upload.mkdir()
    staging.mkdir()
    settings.DICOM_UPLOAD_PATH = str(upload)
    settings.DICOM_STAGING_PATH = str(staging)
    return upload, staging


def _save_dataset(ds, target) -> None:
    """Write *ds* as a proper DICOM Part-10 file across pydicom 2.x and 3.x."""
    try:
        ds.save_as(target, enforce_file_format=True)
    except TypeError:
        # pydicom 2.x — no enforce_file_format kwarg.
        ds.is_little_endian = True
        ds.is_implicit_VR = False
        ds.save_as(target, write_like_original=False)


def _build_dataset(
    *,
    study_uid: str,
    series_uid: str,
    sop_uid: str,
    patient_name: str = "Doe^John",
    patient_id: str = "P001",
    modality: str = "CT",
    extra: dict | None = None,
):
    import pydicom
    from pydicom.dataset import FileDataset, FileMetaDataset

    file_meta = FileMetaDataset()
    file_meta.MediaStorageSOPClassUID = pydicom.uid.UID(SC_SOP_CLASS_UID)
    file_meta.MediaStorageSOPInstanceUID = pydicom.uid.UID(sop_uid)
    file_meta.TransferSyntaxUID = pydicom.uid.ExplicitVRLittleEndian

    ds = FileDataset(None, {}, file_meta=file_meta, preamble=b"\x00" * 128)
    ds.SOPClassUID = SC_SOP_CLASS_UID
    ds.SOPInstanceUID = sop_uid
    ds.StudyInstanceUID = study_uid
    ds.SeriesInstanceUID = series_uid
    ds.PatientName = patient_name
    ds.PatientID = patient_id
    ds.PatientSex = "O"
    ds.Modality = modality
    ds.StudyDate = "20260101"
    ds.StudyTime = "101500"
    ds.StudyDescription = "Test study"
    ds.SeriesDescription = "Test series"
    ds.SeriesNumber = "1"
    ds.InstanceNumber = "1"
    for key, value in (extra or {}).items():
        setattr(ds, key, value)
    return ds


@pytest.fixture
def make_dicom_bytes():
    """Factory: minimal valid DICOM file content as bytes.

    UIDs default to fresh unique values per call; pass explicit ones to build
    files that belong to the same study/series.
    """

    def _make(
        study_uid: str | None = None,
        series_uid: str | None = None,
        sop_uid: str | None = None,
        **tags,
    ) -> bytes:
        ds = _build_dataset(
            study_uid=study_uid or f"1.2.826.0.1.{uuid.uuid4().int % 10**12}",
            series_uid=series_uid or f"1.2.826.0.2.{uuid.uuid4().int % 10**12}",
            sop_uid=sop_uid or f"1.2.826.0.3.{uuid.uuid4().int % 10**12}",
            **tags,
        )
        buf = io.BytesIO()
        _save_dataset(ds, buf)
        return buf.getvalue()

    return _make


@pytest.fixture
def make_dicom_file(make_dicom_bytes, tmp_path):
    """Factory: write a minimal DICOM file to disk and return its path."""

    def _make(directory=None, name: str | None = None, **kwargs):
        directory = directory or (tmp_path / "src")
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / (name or f"{uuid.uuid4().hex}.dcm")
        path.write_bytes(make_dicom_bytes(**kwargs))
        return path

    return _make


@pytest.fixture
def make_study(dicom_dirs):
    """Factory: create a READY study/series/instance tree directly in the ORM.

    Writes a small placeholder file for each instance so unlink-asserting
    tests have something on disk. Returns the DicomStudy.
    """
    from plugins.dicom.models import DicomInstance, DicomSeries, DicomStudy

    upload, _ = dicom_dirs

    def _make(
        author,
        *,
        study_uid: str | None = None,
        instance_count: int = 1,
        deleted_at=None,
        with_files: bool = True,
    ):
        study_uid = study_uid or f"1.2.826.0.1.{uuid.uuid4().int % 10**12}"
        study = DicomStudy.objects.create(
            author=author,
            study_instance_uid=study_uid,
            content_hash=DicomStudy.make_content_hash(author.pk, study_uid),
            patient_name="Doe^John",
            modalities="CT",
            num_instances=instance_count,
            deleted_at=deleted_at,
        )
        series = DicomSeries.objects.create(
            study=study,
            series_instance_uid=f"{study_uid}.1",
            modality="CT",
        )
        for i in range(instance_count):
            stored_name = f"{uuid.uuid4().hex}.dcm"
            if with_files:
                (upload / stored_name).write_bytes(b"DICMDATA")
            DicomInstance.objects.create(
                series=series,
                sop_instance_uid=f"{study_uid}.1.{i + 1}",
                stored_name=stored_name,
                file_size=8,
                status=DicomInstance.Status.READY,
            )
        return study

    return _make


UPLOAD_URL = "/plugin/dicom/api/v1/dicom/upload/"
STUDIES_URL = "/plugin/dicom/api/v1/dicom/studies/"
WADO_URL = "/plugin/dicom/api/v1/dicom/wado/"
