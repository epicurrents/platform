"""DICOM header de-identification at ingest: the PS3.15 Annex E basic profile with the platform's dummies.

⚠️ LOAD-BEARING — the stored file is what every serving surface returns. The study list, the study detail, the
OHIF JSON, DICOMweb, WADO-URI, sharing and attachment inheritance all read the stored rows and files, so this pass
is the only thing between an uploaded study's identifying tags and every grantee, share-token holder and reader of
an attached recording. A dropped action, a nested sequence the walk skips, a UID left as uploaded or a dummy that
reads as a real value serves the patient's identity to all of them with every surface's own tests still green. The
contract test is ``plugins/dicom/tests/test_deidentify.py``; ``DICOM_DEIDENTIFICATION_VERSION`` is pinned there to a
digest of this module's definitions and of the Annex E table it applies, so a behaviour change without a version
bump fails the suite. Never re-pin an existing version: studies stamped with it were written by the old code.

The tag table is the one dicom-anonymizer ships for the 2026c edition of the standard (``ANNEX_E_TABLE``). Its
action functions are not used. They apply the table to the top level of a dataset only, so an identifying attribute
inside a sequence survives; they write ``ANONYMIZED`` as their dummy, which the platform never displays (see
``docs/anonymisation-compliance.md``); and they replace UIDs with random values cached per process, which splits a
study uploaded in several requests into several studies.

What the pass does, wherever an attribute appears, top level or nested:

- **Annex E actions.** D writes a dummy, Z a zero-length value, X removes, U replaces a UID. A composite code resolves
  to the form that keeps the dataset valid without knowing the IOD: Z/D, X/D and X/Z/D to D, X/Z to Z, and X/Z/U* (a
  referencing sequence) to the sequence with its UIDs replaced.
- **Dummies.** ``X`` for text, the recording pass's fixed date ``19850101`` and a midnight time for every date and
  time, ``0`` for numbers. Dates are dummied for Z as well as D, so no date of the source survives in any form.
- **UIDs.** ``2.25.`` followed by the decimal of the first 128 bits of HMAC-SHA256 over the source UID under
  ``DICOM_UID_KEY`` (PS3.5 B.2). Deterministic across requests, workers and restarts, so a study uploaded in several
  batches converges on one study, and the key keeps the source UID from being recomputed out of the result.
- **Kept.** Study and series descriptions (``KEPT``), against the profile's X: ingest runs them through the text
  hygiene check instead. With ``retain_patient_characteristics``, the attributes in ``PATIENT_CHARACTERISTICS``.
- **Removed.** Every private attribute, private creators included, and the 128-byte preamble, which a dual-format
  file uses for a header of its own.
- **Marked.** Patient Identity Removed, De-identification Method naming this version, and the Annex E codes of what
  was applied, so a file that leaves the platform says what was done to it.

The file meta header is rebuilt from the class, the new instance UID and the transfer syntax, which drops the source
application entity titles and any private information it carried. Pixel data is untouched: text burned into the
image is not a header attribute, and the upload gate asks the author to confirm it is absent (``ingest.py``).
"""

import hashlib
import hmac

from dicomanonymizer.dicomfields_selector import dicom_anonymization_database_selector
from pydicom import Dataset, Sequence, dcmread
from pydicom.dataset import FileMetaDataset
from pydicom.tag import Tag

#: Bumped with every change to what the pass writes. Stamped on each instance it rewrites.
DICOM_DEIDENTIFICATION_VERSION = 1

#: The dicom-anonymizer database module holding the Annex E table, named by the standard's edition.
ANNEX_E_TABLE = "dicomfields_2026c"

DUMMY_TEXT = "X"
DUMMY_DATE = "19850101"
DUMMY_TIME = "000000"
DUMMY_DATETIME = "19850101000000"

#: Kept although the profile removes them: the study and series descriptions, decision 4 of the plan.
KEPT = frozenset({Tag(0x0008, 0x1030), Tag(0x0008, 0x103E)})

#: Kept when the deployment retains patient characteristics: sex, age, size and weight.
PATIENT_CHARACTERISTICS = frozenset(
    {Tag(0x0010, 0x0040), Tag(0x0010, 0x1010), Tag(0x0010, 0x1020), Tag(0x0010, 0x1030)}
)

_CATEGORY_ACTIONS = {
    "D_TAGS": "D",
    "Z_TAGS": "Z",
    "X_TAGS": "X",
    "U_TAGS": "U",
    "Z_D_TAGS": "D",
    "X_Z_TAGS": "Z",
    "X_D_TAGS": "D",
    "X_Z_D_TAGS": "D",
    "X_Z_U_STAR_TAGS": "U*",
}

_TEXT_VRS = frozenset({"AE", "CS", "LO", "LT", "PN", "SH", "ST", "UC", "UR", "UT"})
_BINARY_NUMBER_VRS = frozenset({"FD", "FL", "SL", "SS", "SV", "UL", "US", "UV", "AT"})
_BYTES_VRS = frozenset({"OB", "OD", "OF", "OL", "OV", "OW", "UN"})
_DATE_DUMMIES = {"DA": DUMMY_DATE, "TM": DUMMY_TIME, "DT": DUMMY_DATETIME}

_BASIC_PROFILE = ("113100", "Basic Application Confidentiality Profile")
_RETAIN_CHARACTERISTICS = ("113108", "Retain Patient Characteristics Option")


class DeidentificationError(ValueError):
    """Raised for an attribute the pass cannot de-identify; the file is refused rather than stored as uploaded."""


def _load_table() -> tuple[dict[int, str], list[tuple[int, int, int, int, str]]]:
    """Return the Annex E table as exact tag → action, plus the masked repeating-group entries."""
    database = dicom_anonymization_database_selector(ANNEX_E_TABLE)
    exact: dict[int, str] = {}
    masked: list[tuple[int, int, int, int, str]] = []
    for category, action in _CATEGORY_ACTIONS.items():
        for entry in database[category]:
            if len(entry) == 4:
                masked.append((*entry, action))
            else:
                exact[Tag(*entry)] = action
    return exact, masked


_EXACT_ACTIONS, _MASKED_ACTIONS = _load_table()


def _action_for(tag: int, retain_patient_characteristics: bool) -> str | None:
    """Return the action for *tag*: an Annex E code, ``K`` for a kept attribute, or None when the table is silent."""
    if tag in KEPT or (retain_patient_characteristics and tag in PATIENT_CHARACTERISTICS):
        return "K"
    action = _EXACT_ACTIONS.get(tag)
    if action is not None:
        return action
    group, element = tag >> 16, tag & 0xFFFF
    for g, e, group_mask, element_mask, masked_action in _MASKED_ACTIONS:
        if group & group_mask == g & group_mask and element & element_mask == e & element_mask:
            return masked_action
    return None


def hash_uid(uid: str, key: str) -> str:
    """Return the replacement for *uid* under *key*: ``2.25.`` and 128 bits of HMAC-SHA256, at most 44 characters."""
    source = "".join(str(uid).replace("\x00", "").split())
    digest = hmac.new(key.encode(), source.encode(), hashlib.sha256).digest()
    return f"2.25.{int.from_bytes(digest[:16], 'big')}"


def _replace_uids(element, key: str) -> None:
    if element.VM > 1:
        element.value = [hash_uid(v, key) for v in element.value]
    elif element.value:
        element.value = hash_uid(element.value, key)


def _dummy(ds: Dataset, element, key: str) -> None:
    """D: replace *element* with a non-zero-length dummy consistent with its VR."""
    vr = element.VR
    if vr == "SQ":
        for item in element.value:
            for tag in list(item.keys()):
                if tag.is_private:
                    del item[tag]
                else:
                    _dummy(item, item[tag], key)
    elif vr == "UI":
        _replace_uids(element, key)
    elif vr in _DATE_DUMMIES:
        element.value = _DATE_DUMMIES[vr]
    elif vr in _TEXT_VRS:
        element.value = DUMMY_TEXT
    elif vr == "AS":
        element.value = "000Y"
    elif vr in ("DS", "IS"):
        element.value = "0"
    elif vr in _BINARY_NUMBER_VRS:
        element.value = 0
    elif vr in _BYTES_VRS:
        element.value = b"\x00" * 8
    else:
        raise DeidentificationError(f"No dummy for VR {vr} at {element.tag}.")


def _empty(ds: Dataset, element) -> None:
    """Z: replace *element* with a zero-length value; a date or time takes the dummy instead."""
    vr = element.VR
    if vr == "SQ":
        element.value = Sequence()
    elif vr in _DATE_DUMMIES:
        element.value = _DATE_DUMMIES[vr]
    elif vr in _BYTES_VRS:
        element.value = b""
    elif vr in _BINARY_NUMBER_VRS:
        element.value = None
    else:
        element.value = ""


def _walk(ds: Dataset, key: str, retain_patient_characteristics: bool) -> None:
    """Apply the table to every attribute of *ds* and, recursively, of every sequence item it holds."""
    for tag in list(ds.keys()):
        if tag.is_private:
            del ds[tag]
            continue
        element = ds[tag]
        action = _action_for(tag, retain_patient_characteristics)
        if action == "X":
            del ds[tag]
        elif action == "Z":
            _empty(ds, element)
        elif action == "D":
            _dummy(ds, element, key)
        elif action in ("U", "U*") and element.VR == "UI":
            _replace_uids(element, key)
        elif action == "U*" and element.VR != "SQ":
            _empty(ds, element)
        elif element.VR == "SQ":
            # K, U* on a sequence, or no rule: keep the sequence and clean what it holds.
            for item in element.value:
                _walk(item, key, retain_patient_characteristics)


def _mark(ds: Dataset, retain_patient_characteristics: bool) -> None:
    """Write the attributes that say the dataset was de-identified, and how (PS3.15 E.1.1)."""
    ds.PatientIdentityRemoved = "YES"
    method = [f"Epicurrents DICOM pass {DICOM_DEIDENTIFICATION_VERSION}", "PS3.15 Annex E 2026c", "Descriptions kept"]
    codes = [_BASIC_PROFILE]
    if retain_patient_characteristics:
        method.append("Patient characteristics kept")
        codes.append(_RETAIN_CHARACTERISTICS)
    ds.DeidentificationMethod = method
    items = []
    for value, meaning in codes:
        item = Dataset()
        item.CodeValue = value
        item.CodingSchemeDesignator = "DCM"
        item.CodeMeaning = meaning
        items.append(item)
    ds.DeidentificationMethodCodeSequence = Sequence(items)


def deidentify_dataset(ds: Dataset, *, key: str, retain_patient_characteristics: bool = False) -> Dataset:
    """De-identify *ds* in place and return it. *key* is ``DICOM_UID_KEY``; it must not be empty."""
    if not key:
        raise DeidentificationError("DICOM_UID_KEY is not set.")
    _walk(ds, key, retain_patient_characteristics)
    _mark(ds, retain_patient_characteristics)
    source_meta = getattr(ds, "file_meta", None)
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = ds.SOPClassUID
    meta.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
    meta.TransferSyntaxUID = source_meta.TransferSyntaxUID
    ds.file_meta = meta
    ds.preamble = b"\x00" * 128
    return ds


def deidentify_file(source: str, target: str, *, key: str, retain_patient_characteristics: bool = False) -> None:
    """Read the DICOM file at *source* in full, de-identify it and write the result to *target*.

    *target* must not be *source*: pydicom may read deferred values from the source while writing.
    """
    ds = dcmread(source)
    deidentify_dataset(ds, key=key, retain_patient_characteristics=retain_patient_characteristics)
    ds.save_as(target, enforce_file_format=True)
