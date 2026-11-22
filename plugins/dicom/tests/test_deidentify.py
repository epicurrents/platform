"""Contract tests for the DICOM de-identification pass in ``plugins/dicom/deidentify.py``.

Every Annex E action class on a fixture dataset, top level and nested; the platform's dummies, never the library's;
the fallback for an attribute the table does not list, and what it leaves for rendering; a UID declared under
another VR; private attributes, overlays, the preamble and the file meta header; UIDs keyed per author, deterministic
for one and unrelated between two; the marking attributes, with Patient Identity Removed following the caller's
decision; the retain option; and the version-bump discipline: a digest of the pass's definitions, of the Annex E
table and of the upload gate's allowlist is pinned against ``DICOM_DEIDENTIFICATION_VERSION``.
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

from plugins.dicom import deidentify, ingest
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
# The uploading author's primary key, and another author's.
AUTHOR = 7
OTHER_AUTHOR = 8
STUDY_UID = "1.2.826.0.1.3680043.2.1125.1"
SERIES_UID = "1.2.826.0.1.3680043.2.1125.2"
SOP_UID = "1.2.826.0.1.3680043.2.1125.3"
REFERENCED_UID = "1.2.826.0.1.3680043.2.1125.4"
CT_IMAGE = "1.2.840.10008.5.1.4.1.1.2"

# One entry per version: the sha256 of the pass's definitions with docstrings removed, followed by the Annex E
# table it applies and by the upload gate's rule and allowlist, which decide the Patient Identity Removed stamp. Add
# the next version's digest in the commit that bumps DICOM_DEIDENTIFICATION_VERSION, and never re-pin an existing
# version: studies stamped with it were written by the old code.
PINNED_SOURCE_DIGESTS = {
    1: "fe21262ce79d5551bc538324e6d137d9fb3dc3cf8247c90cd32463fba853f736",
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


def _clean(identity_removed: bool = True, **kwargs) -> Dataset:
    return deidentify_dataset(_dataset(), key=KEY, author_id=AUTHOR, identity_removed=identity_removed, **kwargs)


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
        assert ds.StudyInstanceUID == hash_uid(STUDY_UID, KEY, AUTHOR)
        assert ds.SeriesInstanceUID == hash_uid(SERIES_UID, KEY, AUTHOR)
        assert ds.SOPInstanceUID == hash_uid(SOP_UID, KEY, AUTHOR)
        item = ds.ReferencedImageSequence[0]
        assert item.ReferencedSOPInstanceUID == hash_uid(REFERENCED_UID, KEY, AUTHOR)
        assert item.ReferencedSOPClassUID == CT_IMAGE

    def test_the_table_applies_inside_sequences_it_does_not_name(self):
        item = _clean().AnatomicRegionSequence[0]
        assert (item.CodeValue, item.CodeMeaning) == ("T-D1100", "Head")
        assert item.PatientName == ""
        assert item.ReferencedSOPInstanceUID == hash_uid(SOP_UID, KEY, AUTHOR)

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
            deidentify_dataset(_dataset(), key="", author_id=AUTHOR, identity_removed=True)

    @pytest.mark.parametrize("author_id", [None, "7", True, 7.0])
    def test_an_author_that_is_not_a_primary_key_is_refused(self, author_id):
        with pytest.raises(DeidentificationError):
            deidentify_dataset(_dataset(), key=KEY, author_id=author_id, identity_removed=True)

    def test_the_stamp_and_the_author_have_no_default(self):
        with pytest.raises(TypeError):
            deidentify_dataset(_dataset(), key=KEY, author_id=AUTHOR)
        with pytest.raises(TypeError):
            deidentify_dataset(_dataset(), key=KEY, identity_removed=True)


CONCATENATION_SOURCE_UID = "1.2.826.0.1.3680043.2.1125.5"
TALAIRACH_FRAME = "1.2.840.10008.1.4.1.1"
PRIVATE_SOP_CLASS = "1.3.12.2.1107.5.9.1"


class TestUnlistedAttributes:
    """What the pass does with a public attribute the Annex E table has no action for."""

    def _clean_with(self, **attributes) -> Dataset:
        ds = _dataset()
        for keyword, value in attributes.items():
            setattr(ds, keyword, value)
        return deidentify_dataset(ds, key=KEY, author_id=AUTHOR, identity_removed=True)

    @pytest.mark.parametrize(
        "keyword",
        [
            "SOPInstanceUIDOfConcatenationSource",
            "MultiFrameSourceSOPInstanceUID",
            "RadiopharmaceuticalAdministrationEventUID",
            "TargetFrameOfReferenceUID",
            "VolumeFrameOfReferenceUID",
            "TableFrameOfReferenceUID",
            "EquipmentFrameOfReferenceUID",
            "ReferencedColorPaletteInstanceUID",
            "ReferencedFiducialUID",
            "ContextGroupExtensionCreatorUID",
        ],
    )
    def test_an_unlisted_uid_is_replaced(self, keyword):
        ds = self._clean_with(**{keyword: CONCATENATION_SOURCE_UID})
        assert getattr(ds, keyword) == hash_uid(CONCATENATION_SOURCE_UID, KEY, AUTHOR)

    def test_an_unlisted_uid_stays_consistent_with_a_listed_one(self):
        ds = self._clean_with(VolumeFrameOfReferenceUID=REFERENCED_UID, FrameOfReferenceUID=REFERENCED_UID)
        assert ds.VolumeFrameOfReferenceUID == ds.FrameOfReferenceUID == hash_uid(REFERENCED_UID, KEY, AUTHOR)

    @pytest.mark.parametrize("keyword", ["SOPClassUID", "CodingSchemeUID", "ContextUID", "MappingResourceUID"])
    def test_a_class_or_scheme_uid_is_kept_even_when_unregistered(self, keyword):
        assert getattr(self._clean_with(**{keyword: PRIVATE_SOP_CLASS}), keyword) == PRIVATE_SOP_CLASS

    def test_every_kept_uid_tag_is_a_uid_naming_no_instance(self):
        from pydicom.datadict import DicomDictionary

        for tag in deidentify.KEPT_UID_TAGS:
            vr, _, _, _, keyword = DicomDictionary[tag]
            assert vr == "UI", keyword
            assert "Instance" not in keyword.replace("StoredInstanceTransferSyntax", ""), keyword
            assert deidentify._action_for(tag, False) is None, keyword

    def test_a_registered_value_is_kept_and_an_unregistered_one_beside_it_replaced(self):
        ds = self._clean_with(TargetFrameOfReferenceUID=TALAIRACH_FRAME)
        assert ds.TargetFrameOfReferenceUID == TALAIRACH_FRAME
        ds = self._clean_with(SOPClassesInStudy=[CT_IMAGE, PRIVATE_SOP_CLASS])
        assert list(ds.SOPClassesInStudy) == [CT_IMAGE, PRIVATE_SOP_CLASS]
        ds = self._clean_with(SelectorUIValue=[CT_IMAGE, CONCATENATION_SOURCE_UID])
        assert list(ds.SelectorUIValue) == [CT_IMAGE, hash_uid(CONCATENATION_SOURCE_UID, KEY, AUTHOR)]

    def test_a_value_under_the_standards_root_that_is_not_registered_is_replaced(self):
        ds = self._clean_with(TargetFrameOfReferenceUID="1.2.840.10008.9999.1")
        assert ds.TargetFrameOfReferenceUID == hash_uid("1.2.840.10008.9999.1", KEY, AUTHOR)

    @pytest.mark.parametrize("keyword", ["EvaluatorName", "SecondaryReviewerName"])
    def test_an_unlisted_person_name_is_emptied(self, keyword):
        assert getattr(self._clean_with(**{keyword: "Doe^Jane"}), keyword) == ""

    @pytest.mark.parametrize(
        ("keyword", "value", "dummy"),
        [
            ("DateOfGainCalibration", "20260315", DUMMY_DATE),
            ("SecondaryReviewDate", "20260315", DUMMY_DATE),
            ("SecondaryReviewTime", "142233", "000000"),
            ("TimeOfGainCalibration", "142233", "000000"),
            ("StudyUpdateDateTime", "20260315142233", "19850101000000"),
        ],
    )
    def test_an_unlisted_date_or_time_takes_the_dummy(self, keyword, value, dummy):
        assert getattr(self._clean_with(**{keyword: value}), keyword) == dummy

    @pytest.mark.parametrize(
        "keyword",
        ["DocumentTitle", "CalibrationNotes", "TextValue", "StrainDescription", "RetrieveURL", "SegmentDescription"],
    )
    def test_unlisted_free_text_is_emptied(self, keyword):
        assert getattr(self._clean_with(**{keyword: "Jane Doe, St Example Hospital"}), keyword) == ""

    def test_the_fallback_applies_inside_a_sequence(self):
        ds = _dataset()
        item = ds.AnatomicRegionSequence[0]
        item.EvaluatorName = "Doe^Jane"
        item.SOPInstanceUIDOfConcatenationSource = CONCATENATION_SOURCE_UID
        item = deidentify_dataset(ds, key=KEY, author_id=AUTHOR, identity_removed=True).AnatomicRegionSequence[0]
        assert item.EvaluatorName == ""
        assert item.SOPInstanceUIDOfConcatenationSource == hash_uid(CONCATENATION_SOURCE_UID, KEY, AUTHOR)

    def test_a_declared_vr_does_not_keep_an_unlisted_identifier(self):
        ds = _dataset()
        ds.add_new(0x00142006, "LO", "Doe^Jane")  # Evaluator Name, a PN
        ds.add_new(0x00200242, "LO", CONCATENATION_SOURCE_UID)
        ds = deidentify_dataset(ds, key=KEY, author_id=AUTHOR, identity_removed=True)
        assert ds[0x00142006].value == ""
        assert ds[0x00200242].value == hash_uid(CONCATENATION_SOURCE_UID, KEY, AUTHOR)

    def test_an_attribute_the_dictionary_does_not_know_is_removed(self):
        ds = _dataset()
        ds.add_new(0x00189999, "LO", "Jane Doe")
        ds.add_new(0x00080000, "UL", 512)
        unknown = Dataset()
        unknown.add_new(0x00189998, "LO", "Jane Doe")
        ds.add_new(0x00189997, "SQ", Sequence([unknown]))
        ds = deidentify_dataset(ds, key=KEY, author_id=AUTHOR, identity_removed=True)
        assert 0x00189999 not in ds and 0x00080000 not in ds and 0x00189997 not in ds

    def test_an_unlisted_attribute_of_unknown_vr_is_removed(self):
        ds = _dataset()
        ds.add_new(0x00181200, "DA", "20260315")
        # pydicom gives a known tag its dictionary VR on creation; a file read with that conversion off keeps UN.
        ds[0x00181200].VR = "UN"
        ds.add_new(0x00089123, "UI", CONCATENATION_SOURCE_UID)
        ds[0x00089123].VR = "UN"
        ds = deidentify_dataset(ds, key=KEY, author_id=AUTHOR, identity_removed=True)
        assert 0x00089123 not in ds
        assert 0x00181200 not in ds  # Date of Last Calibration: its X action removes it whatever the VR

    def test_unlisted_codes_strings_and_numbers_are_kept(self):
        ds = self._clean_with(SoftwareVersions="1.2.3", WindowCenterWidthExplanation="BRAIN", SliceThickness="5.0")
        assert (ds.SoftwareVersions, ds.WindowCenterWidthExplanation, ds.SliceThickness) == ("1.2.3", "BRAIN", 5.0)

    def test_an_enhanced_multiframe_dataset_keeps_what_rendering_reads(self):
        ds = _dataset()
        ds.NumberOfFrames = "2"
        ds.FrameOfReferenceUID = REFERENCED_UID
        ds.SOPInstanceUIDOfConcatenationSource = CONCATENATION_SOURCE_UID
        ds.InConcatenationNumber = 1
        ds.ImageType = ["ORIGINAL", "PRIMARY", "VOLUME", "NONE"]
        ds.PixelData = b"\x01\x00\x02\x00\x03\x00\x04\x00" * 2
        measures = Dataset()
        measures.PixelSpacing = ["0.5", "0.5"]
        measures.SliceThickness = "5.0"
        orientation = Dataset()
        orientation.ImageOrientationPatient = ["1", "0", "0", "0", "1", "0"]
        transformation = Dataset()
        transformation.RescaleSlope = "1"
        transformation.RescaleIntercept = "-1024"
        transformation.RescaleType = "HU"
        shared = Dataset()
        shared.PixelMeasuresSequence = Sequence([measures])
        shared.PlaneOrientationSequence = Sequence([orientation])
        shared.PixelValueTransformationSequence = Sequence([transformation])
        ds.SharedFunctionalGroupsSequence = Sequence([shared])
        frames = []
        for index in range(2):
            position = Dataset()
            position.ImagePositionPatient = ["0", "0", str(index * 5)]
            voi = Dataset()
            voi.WindowCenter = "40"
            voi.WindowWidth = "80"
            voi.WindowCenterWidthExplanation = "BRAIN"
            content = Dataset()
            content.DimensionIndexValues = [1, index + 1]
            content.InStackPositionNumber = index + 1
            content.StackID = "1"
            content.FrameAcquisitionDateTime = "20260315142233"
            frame = Dataset()
            frame.PlanePositionSequence = Sequence([position])
            frame.FrameVOILUTSequence = Sequence([voi])
            frame.FrameContentSequence = Sequence([content])
            frames.append(frame)
        ds.PerFrameFunctionalGroupsSequence = Sequence(frames)

        ds = deidentify_dataset(ds, key=KEY, author_id=AUTHOR, identity_removed=True)

        assert ds.SOPInstanceUIDOfConcatenationSource == hash_uid(CONCATENATION_SOURCE_UID, KEY, AUTHOR)
        assert ds.FrameOfReferenceUID == hash_uid(REFERENCED_UID, KEY, AUTHOR)
        assert (ds.NumberOfFrames, ds.Rows, ds.Columns, ds.InConcatenationNumber) == (2, 2, 2, 1)
        assert (ds.BitsAllocated, ds.BitsStored, ds.HighBit) == (16, 12, 11)
        assert (ds.PixelRepresentation, ds.SamplesPerPixel) == (0, 1)
        assert ds.PhotometricInterpretation == "MONOCHROME2"
        assert list(ds.ImageType) == ["ORIGINAL", "PRIMARY", "VOLUME", "NONE"]
        assert ds.PixelData == b"\x01\x00\x02\x00\x03\x00\x04\x00" * 2
        shared = ds.SharedFunctionalGroupsSequence[0]
        assert list(shared.PixelMeasuresSequence[0].PixelSpacing) == [0.5, 0.5]
        assert shared.PixelMeasuresSequence[0].SliceThickness == 5.0
        assert list(shared.PlaneOrientationSequence[0].ImageOrientationPatient) == [1, 0, 0, 0, 1, 0]
        transformation = shared.PixelValueTransformationSequence[0]
        assert (transformation.RescaleSlope, transformation.RescaleIntercept) == (1, -1024)
        assert transformation.RescaleType == "HU"
        for index, frame in enumerate(ds.PerFrameFunctionalGroupsSequence):
            assert list(frame.PlanePositionSequence[0].ImagePositionPatient) == [0, 0, index * 5]
            voi = frame.FrameVOILUTSequence[0]
            assert (voi.WindowCenter, voi.WindowWidth, voi.WindowCenterWidthExplanation) == (40, 80, "BRAIN")
            content = frame.FrameContentSequence[0]
            assert list(content.DimensionIndexValues) == [1, index + 1]
            assert (content.InStackPositionNumber, content.StackID) == (index + 1, "1")
            assert content.FrameAcquisitionDateTime == "19850101000000"


class TestUidUnderAnotherVr:
    """A U action on an element the file declares under a VR other than UI."""

    def test_a_string_value_is_replaced(self):
        ds = _dataset()
        ds.ReferencedImageSequence[0].add_new(0x00081155, "LO", REFERENCED_UID)
        item = deidentify_dataset(ds, key=KEY, author_id=AUTHOR, identity_removed=True).ReferencedImageSequence[0]
        assert item[0x00081155].value == hash_uid(REFERENCED_UID, KEY, AUTHOR)

    def test_every_value_of_a_multi_valued_string_is_replaced(self):
        ds = _dataset()
        ds.add_new(0x0020000E, "LO", [SERIES_UID, REFERENCED_UID])
        values = list(deidentify_dataset(ds, key=KEY, author_id=AUTHOR, identity_removed=True)[0x0020000E].value)
        assert values == [hash_uid(SERIES_UID, KEY, AUTHOR), hash_uid(REFERENCED_UID, KEY, AUTHOR)]

    @pytest.mark.parametrize(("vr", "value"), [("OB", b"1.2.826.0.1.3680043.2.1125.4"), ("UL", 1125)])
    def test_a_value_that_is_not_a_string_refuses_the_file(self, vr, value):
        ds = _dataset()
        ds.ReferencedImageSequence[0].add_new(0x00081155, vr, value)
        with pytest.raises(DeidentificationError):
            deidentify_dataset(ds, key=KEY, author_id=AUTHOR, identity_removed=True)

    def test_an_empty_value_is_left_empty(self):
        ds = _dataset()
        ds.ReferencedImageSequence[0].add_new(0x00081155, "LO", "")
        item = deidentify_dataset(ds, key=KEY, author_id=AUTHOR, identity_removed=True).ReferencedImageSequence[0]
        assert item[0x00081155].value == ""


class TestUids:
    def test_deterministic_across_calls(self):
        assert hash_uid(STUDY_UID, KEY, AUTHOR) == hash_uid(STUDY_UID, KEY, AUTHOR)
        assert _clean().StudyInstanceUID == _clean().StudyInstanceUID

    def test_depends_on_the_key(self):
        assert hash_uid(STUDY_UID, KEY, AUTHOR) != hash_uid(STUDY_UID, "another-key", AUTHOR)

    def test_two_authors_get_unrelated_replacements_for_one_uid(self):
        assert hash_uid(STUDY_UID, KEY, AUTHOR) != hash_uid(STUDY_UID, KEY, OTHER_AUTHOR)
        own = deidentify_dataset(_dataset(), key=KEY, author_id=AUTHOR, identity_removed=True)
        other = deidentify_dataset(_dataset(), key=KEY, author_id=OTHER_AUTHOR, identity_removed=True)
        for keyword in ("StudyInstanceUID", "SeriesInstanceUID", "SOPInstanceUID"):
            assert getattr(own, keyword) != getattr(other, keyword), keyword
        assert (
            own.ReferencedImageSequence[0].ReferencedSOPInstanceUID
            != other.ReferencedImageSequence[0].ReferencedSOPInstanceUID
        )

    def test_one_author_converges_across_datasets(self):
        """A second file of the same study, cleaned in another call, lands on the same study and series."""
        first = _clean()
        second_source = _dataset()
        second_source.SOPInstanceUID = REFERENCED_UID
        second = deidentify_dataset(second_source, key=KEY, author_id=AUTHOR, identity_removed=True)
        assert (first.StudyInstanceUID, first.SeriesInstanceUID) == (second.StudyInstanceUID, second.SeriesInstanceUID)
        # The first file's reference names the second file's instance, as it did before the pass.
        assert first.ReferencedImageSequence[0].ReferencedSOPInstanceUID == second.SOPInstanceUID

    def test_references_inside_one_authors_dataset_still_resolve(self):
        ds = _clean()
        assert ds.AnatomicRegionSequence[0].ReferencedSOPInstanceUID == ds.SOPInstanceUID
        assert ds.file_meta.MediaStorageSOPInstanceUID == ds.SOPInstanceUID

    def test_the_author_key_is_domain_separated(self):
        """No author's key is the deployment key, another author's, or what a UID-shaped author id would give."""
        import hashlib
        import hmac

        keys = {deidentify.author_uid_key(KEY, n) for n in (1, 11, 111)}
        assert len(keys) == 3
        assert KEY.encode() not in keys
        # An author id cannot stand in for part of a UID: the id is hashed into the key, never beside the UID.
        assert hash_uid("1.2", KEY, 11) != hash_uid("11.2", KEY, 1)
        assert hash_uid(STUDY_UID, KEY, AUTHOR) != (
            f"2.25.{int.from_bytes(hmac.new(KEY.encode(), STUDY_UID.encode(), hashlib.sha256).digest()[:16], 'big')}"
        )

    def test_the_author_id_is_written_nowhere(self):
        ds = deidentify_dataset(_dataset(), key=KEY, author_id=987654321, identity_removed=True)
        assert not [t for t in _texts(ds) if "987654321" in t]

    def test_padding_does_not_change_the_result(self):
        assert hash_uid(f" {STUDY_UID}\x00", KEY, AUTHOR) == hash_uid(STUDY_UID, KEY, AUTHOR)

    def test_shape(self):
        uid = hash_uid(STUDY_UID, KEY, AUTHOR)
        assert uid.startswith("2.25.")
        assert uid[5:].isdigit()
        assert len(uid) <= 44


class TestMarking:
    def test_identity_removed_and_method_recorded(self):
        ds = _clean()
        assert ds.PatientIdentityRemoved == "YES"
        assert f"Epicurrents DICOM pass {DICOM_DEIDENTIFICATION_VERSION}" in ds.DeidentificationMethod
        assert [i.CodeValue for i in ds.DeidentificationMethodCodeSequence] == ["113100"]

    def test_a_file_stored_on_the_uploaders_word_is_stamped_no(self):
        ds = _clean(identity_removed=False)
        assert ds.PatientIdentityRemoved == "NO"

    def test_the_method_is_recorded_whatever_the_stamp(self):
        """PS3.3 C.7.1.1: the method attributes are required with YES and may be present otherwise."""
        stamped, unstamped = _clean(), _clean(identity_removed=False, retain_patient_characteristics=True)
        assert list(unstamped.DeidentificationMethod)[:3] == list(stamped.DeidentificationMethod)
        assert [i.CodeValue for i in unstamped.DeidentificationMethodCodeSequence] == ["113100", "113108"]

    def test_the_header_is_cleaned_the_same_whatever_the_stamp(self):
        stamped, unstamped = _clean(), _clean(identity_removed=False)
        unstamped.PatientIdentityRemoved = "YES"
        assert stamped == unstamped

    @pytest.mark.parametrize("identity_removed", [True, False])
    def test_dates_are_marked_removed(self, identity_removed):
        """PS3.15 E.2: REMOVED when no Retain Longitudinal Temporal Information option is applied."""
        assert _clean(identity_removed=identity_removed).LongitudinalTemporalInformationModified == "REMOVED"

    @pytest.mark.parametrize("identity_removed", [True, False])
    def test_a_stamp_the_source_carried_is_overwritten(self, identity_removed):
        ds = _dataset()
        ds.PatientIdentityRemoved = "NO" if identity_removed else "YES"
        ds.LongitudinalTemporalInformationModified = "UNMODIFIED"
        ds.DeidentificationMethod = "Site tool 3"
        ds = deidentify_dataset(ds, key=KEY, author_id=AUTHOR, identity_removed=identity_removed)
        assert ds.PatientIdentityRemoved == ("YES" if identity_removed else "NO")
        assert ds.LongitudinalTemporalInformationModified == "REMOVED"
        assert "Site tool 3" not in ds.DeidentificationMethod

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
        deidentify_file(str(source), str(target), key=KEY, author_id=AUTHOR, identity_removed=True)
        stored = dcmread(target)
        assert stored.preamble == b"\x00" * 128
        assert stored.file_meta.MediaStorageSOPInstanceUID == stored.SOPInstanceUID == hash_uid(SOP_UID, KEY, AUTHOR)
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
            deidentify._dummy(ds, element, deidentify.author_uid_key(KEY, AUTHOR))


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
    # The upload gate decides which files the pass stamps Patient Identity Removed YES, so its rule and its
    # allowlist are part of what the version describes although they live in ingest.py.
    gate = ast.unparse(_strip_docstrings(ast.parse(inspect.getsource(ingest.pixel_review_required))))
    allowlist = json.dumps({k: sorted(v) for k, v in ingest.ACQUISITION_ALLOWLIST.items()}, sort_keys=True)
    return hashlib.sha256("\n".join([*definitions, table_text, gate, allowlist]).encode()).hexdigest()


class TestVersionDiscipline:
    def test_the_current_version_is_pinned(self):
        assert DICOM_DEIDENTIFICATION_VERSION in PINNED_SOURCE_DIGESTS, "pin the new version's digest"

    def test_the_pass_matches_its_pinned_digest(self):
        assert _pass_digest() == PINNED_SOURCE_DIGESTS[DICOM_DEIDENTIFICATION_VERSION], (
            "plugins/dicom/deidentify.py, its Annex E table or the upload gate's allowlist changed: bump DICOM_DEIDENTIFICATION_VERSION and pin "
            "the new digest, never re-pin an existing version"
        )

    def test_versions_are_consecutive(self):
        assert sorted(PINNED_SOURCE_DIGESTS) == list(range(1, DICOM_DEIDENTIFICATION_VERSION + 1))


def test_io_round_trip_needs_no_file(tmp_path):
    buf = io.BytesIO()
    _clean().save_as(buf, enforce_file_format=True)
    assert dcmread(io.BytesIO(buf.getvalue())).PatientIdentityRemoved == "YES"
