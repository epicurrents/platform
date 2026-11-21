"""Contract tests for the DICOM de-identification pass in ``plugins/dicom/deidentify.py``.

Every Annex E action class on a fixture dataset, top level and nested; the platform's dummies, never the library's;
private attributes, overlays, the preamble and the file meta header; deterministic keyed UIDs; the marking
attributes; the retain option; and the version-bump discipline: a digest of the pass's definitions and of the
Annex E table is pinned against ``DICOM_DEIDENTIFICATION_VERSION``.
"""

import ast
import hashlib
import inspect
import io
import json

import pytest
from pydicom import Dataset, Sequence, dcmread
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian

from plugins.dicom import deidentify
from plugins.dicom.deidentify import (
    ANNEX_E_TABLE,
    DICOM_DEIDENTIFICATION_VERSION,
    DUMMY_DATE,
    DUMMY_TEXT,
    DeidentificationError,
    deidentify_dataset,
    deidentify_file,
    hash_uid,
)

KEY = "test-key"
STUDY_UID = "1.2.826.0.1.3680043.2.1125.1"
SERIES_UID = "1.2.826.0.1.3680043.2.1125.2"
SOP_UID = "1.2.826.0.1.3680043.2.1125.3"
REFERENCED_UID = "1.2.826.0.1.3680043.2.1125.4"
CT_IMAGE = "1.2.840.10008.5.1.4.1.1.2"

# One entry per version: the sha256 of the pass's definitions with docstrings removed, followed by the Annex E
# table it applies. Add the next version's digest in the commit that bumps DICOM_DEIDENTIFICATION_VERSION, and never
# re-pin an existing version: studies stamped with it were written by the old code.
PINNED_SOURCE_DIGESTS = {
    1: "f299d4e1cfbd98883ca00bf52e1036b75d8edfc8dca72c68c99960381b64fd66",
}


def _dataset() -> FileDataset:
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = CT_IMAGE
    meta.MediaStorageSOPInstanceUID = SOP_UID
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    meta.SourceApplicationEntityTitle = "SITE_PACS_01"
    ds = FileDataset(None, {}, file_meta=meta, preamble=b"TIFF" + b"\x01" * 124)
    ds.SOPClassUID = CT_IMAGE
    ds.SOPInstanceUID = SOP_UID
    ds.StudyInstanceUID = STUDY_UID
    ds.SeriesInstanceUID = SERIES_UID
    ds.PatientName = "Doe^Jane"
    ds.PatientID = "HOSP-123456"
    ds.PatientBirthDate = "19700412"
    ds.PatientSex = "F"
    ds.PatientAge = "055Y"
    ds.PatientWeight = "61"
    ds.OtherPatientIDs = "SSN-0001"
    ds.AccessionNumber = "ACC-77"
    ds.InstitutionName = "St Example Hospital"
    ds.InstitutionAddress = "1 Example Road"
    ds.ImageComments = "Patient Jane Doe, follow-up"
    ds.StudyDate = "20260315"
    ds.StudyTime = "142233"
    ds.ContentDate = "20260315"
    ds.AcquisitionDateTime = "20260315142233"
    ds.StudyDescription = "CT head"
    ds.SeriesDescription = "Axial 5mm"
    ds.Modality = "CT"
    ds.Manufacturer = "Vendor"
    ds.BodyPartExamined = "HEAD"
    ds.Rows = 2
    ds.Columns = 2
    ds.BitsAllocated = 16
    ds.BitsStored = 12
    ds.HighBit = 11
    ds.PixelRepresentation = 0
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.PixelData = b"\x01\x00\x02\x00\x03\x00\x04\x00"
    # A referencing sequence (X/Z/U*), a removed one (X), and one the table is silent on.
    referenced = Dataset()
    referenced.ReferencedSOPClassUID = CT_IMAGE
    referenced.ReferencedSOPInstanceUID = REFERENCED_UID
    ds.ReferencedImageSequence = Sequence([referenced])
    request = Dataset()
    request.RequestedProcedureID = "RP-1"
    ds.RequestAttributesSequence = Sequence([request])
    region = Dataset()
    region.CodeValue = "T-D1100"
    region.CodeMeaning = "Head"
    region.ReferencedSOPInstanceUID = SOP_UID
    region.PatientName = "Doe^Jane"
    region.add_new(0x00091001, "LO", "nested private")
    ds.AnatomicRegionSequence = Sequence([region])
    # A private block and an overlay plane.
    block = ds.private_block(0x0009, "VENDOR PRIVATE", create=True)
    block.add_new(0x01, "LO", "Jane Doe, MRN HOSP-123456")
    ds.add_new(0x60003000, "OW", b"\xff" * 8)
    ds.add_new(0x60004000, "LT", "Overlay: Jane Doe")
    return ds


def _clean(**kwargs) -> Dataset:
    return deidentify_dataset(_dataset(), key=KEY, **kwargs)


def _texts(ds: Dataset) -> list[str]:
    return [str(e.value) for e in ds.iterall() if e.VR not in ("SQ", "OB", "OW")]


class TestActions:
    def test_z_empties_text_and_dummies_dates(self):
        ds = _clean()
        assert ds.PatientName == ""
        assert ds.AccessionNumber == ""
        assert ds.PatientBirthDate == DUMMY_DATE
        assert ds.StudyDate == DUMMY_DATE
        assert ds.StudyTime == "000000"

    def test_d_writes_the_platform_dummy(self):
        ds = _clean()
        assert ds.PatientID == DUMMY_TEXT
        assert ds.InstitutionName == DUMMY_TEXT
        assert ds.ContentDate == DUMMY_DATE
        assert ds.AcquisitionDateTime == "19850101000000"

    def test_x_removes(self):
        ds = _clean()
        for keyword in ("OtherPatientIDs", "InstitutionAddress", "ImageComments", "RequestAttributesSequence"):
            assert keyword not in ds, keyword

    def test_patient_characteristics_are_blanked_by_default(self):
        ds = _clean()
        assert ds.PatientSex == ""
        assert "PatientAge" not in ds
        assert "PatientWeight" not in ds

    def test_descriptions_and_technical_attributes_are_kept(self):
        ds = _clean()
        assert ds.StudyDescription == "CT head"
        assert ds.SeriesDescription == "Axial 5mm"
        assert (ds.Modality, ds.Manufacturer, ds.BodyPartExamined) == ("CT", "Vendor", "HEAD")
        assert ds.SOPClassUID == CT_IMAGE

    def test_pixel_data_is_untouched(self):
        assert _clean().PixelData == _dataset().PixelData

    def test_uids_are_replaced_and_references_stay_consistent(self):
        ds = _clean()
        assert ds.StudyInstanceUID == hash_uid(STUDY_UID, KEY)
        assert ds.SeriesInstanceUID == hash_uid(SERIES_UID, KEY)
        assert ds.SOPInstanceUID == hash_uid(SOP_UID, KEY)
        item = ds.ReferencedImageSequence[0]
        assert item.ReferencedSOPInstanceUID == hash_uid(REFERENCED_UID, KEY)
        assert item.ReferencedSOPClassUID == CT_IMAGE

    def test_the_table_applies_inside_sequences_it_does_not_name(self):
        item = _clean().AnatomicRegionSequence[0]
        assert (item.CodeValue, item.CodeMeaning) == ("T-D1100", "Head")
        assert item.PatientName == ""
        assert item.ReferencedSOPInstanceUID == hash_uid(SOP_UID, KEY)

    def test_private_attributes_and_overlays_are_removed_at_every_level(self):
        ds = _clean()
        assert not [e for e in ds.iterall() if e.tag.is_private]
        assert 0x60003000 not in ds and 0x60004000 not in ds

    def test_no_source_identifier_survives_anywhere(self):
        texts = " ".join(_texts(_clean()))
        for value in ("Doe", "Jane", "HOSP-123456", "SSN-0001", "ACC-77", "Example", "20260315", "19700412"):
            assert value not in texts, value

    def test_no_library_dummy_is_written(self):
        assert not [t for t in _texts(_clean()) if "anonym" in t.lower()]

    def test_retain_option_keeps_patient_characteristics_only(self):
        ds = _clean(retain_patient_characteristics=True)
        assert (ds.PatientSex, ds.PatientAge, ds.PatientWeight) == ("F", "055Y", "61")
        assert ds.PatientName == ""

    def test_empty_key_is_refused(self):
        with pytest.raises(DeidentificationError):
            deidentify_dataset(_dataset(), key="")


class TestUids:
    def test_deterministic_across_calls(self):
        assert hash_uid(STUDY_UID, KEY) == hash_uid(STUDY_UID, KEY)
        assert _clean().StudyInstanceUID == _clean().StudyInstanceUID

    def test_depends_on_the_key(self):
        assert hash_uid(STUDY_UID, KEY) != hash_uid(STUDY_UID, "another-key")

    def test_padding_does_not_change_the_result(self):
        assert hash_uid(f" {STUDY_UID}\x00", KEY) == hash_uid(STUDY_UID, KEY)

    def test_shape(self):
        uid = hash_uid(STUDY_UID, KEY)
        assert uid.startswith("2.25.")
        assert uid[5:].isdigit()
        assert len(uid) <= 44


class TestMarking:
    def test_identity_removed_and_method_recorded(self):
        ds = _clean()
        assert ds.PatientIdentityRemoved == "YES"
        assert f"Epicurrents DICOM pass {DICOM_DEIDENTIFICATION_VERSION}" in ds.DeidentificationMethod
        assert [i.CodeValue for i in ds.DeidentificationMethodCodeSequence] == ["113100"]

    def test_retain_option_is_recorded(self):
        ds = _clean(retain_patient_characteristics=True)
        assert [i.CodeValue for i in ds.DeidentificationMethodCodeSequence] == ["113100", "113108"]
        assert "Patient characteristics kept" in ds.DeidentificationMethod

    def test_every_method_value_fits_its_vr(self):
        ds = _clean(retain_patient_characteristics=True)
        assert all(len(v) <= 64 for v in ds.DeidentificationMethod)


class TestFile:
    def test_round_trip_through_a_file(self, tmp_path):
        source, target = tmp_path / "in.dcm", tmp_path / "out.dcm"
        _dataset().save_as(source, enforce_file_format=True)
        deidentify_file(str(source), str(target), key=KEY)
        stored = dcmread(target)
        assert stored.preamble == b"\x00" * 128
        assert stored.file_meta.MediaStorageSOPInstanceUID == stored.SOPInstanceUID == hash_uid(SOP_UID, KEY)
        assert stored.file_meta.TransferSyntaxUID == ExplicitVRLittleEndian
        assert "SourceApplicationEntityTitle" not in stored.file_meta
        assert stored.PatientName == ""
        assert stored.PixelData == _dataset().PixelData
        assert b"Doe" not in target.read_bytes()

    def test_an_unwritable_vr_is_refused(self):
        ds = _dataset()
        ds.add_new(0x00100010, "PN", "Doe^Jane")
        element = ds[0x00100010]
        element.VR = "XX"
        with pytest.raises(DeidentificationError):
            deidentify._dummy(ds, element, KEY)


def _strip_docstrings(tree: ast.Module) -> ast.Module:
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef)) and body:
            first = body[0]
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                node.body = body[1:] or [ast.Pass()]
    return tree


def _pass_digest() -> str:
    from dicomanonymizer.dicomfields_selector import dicom_anonymization_database_selector

    tree = _strip_docstrings(ast.parse(inspect.getsource(deidentify)))
    definitions = [ast.unparse(node) for node in tree.body if not isinstance(node, (ast.Import, ast.ImportFrom))]
    # The version constant moves with every bump; the digest is of what it versions.
    definitions = [d for d in definitions if not d.startswith("DICOM_DEIDENTIFICATION_VERSION")]
    table = dicom_anonymization_database_selector(ANNEX_E_TABLE)
    table_text = json.dumps({k: sorted(map(list, v)) for k, v in table.items() if k != "ALL_TAGS"}, sort_keys=True)
    return hashlib.sha256(("\n".join(definitions) + "\n" + table_text).encode()).hexdigest()


class TestVersionDiscipline:
    def test_the_current_version_is_pinned(self):
        assert DICOM_DEIDENTIFICATION_VERSION in PINNED_SOURCE_DIGESTS, "pin the new version's digest"

    def test_the_pass_matches_its_pinned_digest(self):
        assert _pass_digest() == PINNED_SOURCE_DIGESTS[DICOM_DEIDENTIFICATION_VERSION], (
            "plugins/dicom/deidentify.py or its Annex E table changed: bump DICOM_DEIDENTIFICATION_VERSION and pin "
            "the new digest, never re-pin an existing version"
        )

    def test_versions_are_consecutive(self):
        assert sorted(PINNED_SOURCE_DIGESTS) == list(range(1, DICOM_DEIDENTIFICATION_VERSION + 1))


def test_io_round_trip_needs_no_file(tmp_path):
    buf = io.BytesIO()
    _clean().save_as(buf, enforce_file_format=True)
    assert dcmread(io.BytesIO(buf.getvalue())).PatientIdentityRemoved == "YES"
