"""DICOM header de-identification at ingest: the PS3.15 Annex E basic profile with the platform's dummies.

⚠️ LOAD-BEARING — the stored file is what every serving surface returns. The study list, the study detail, the
OHIF JSON, DICOMweb, WADO-URI, sharing and attachment inheritance all read the stored rows and files, so this pass
is the only thing between an uploaded study's identifying tags and every grantee, share-token holder and reader of
an attached recording. A dropped action, a nested sequence the walk skips, a UID left as uploaded or a dummy that
reads as a real value serves the patient's identity to all of them with every surface's own tests still green. The
contract test is ``plugins/dicom/tests/test_deidentify.py``; ``DICOM_DEIDENTIFICATION_VERSION`` is pinned there to a
digest of this module's definitions, of the Annex E table it applies and of the upload gate's allowlist in
``ingest.py``, which decides the Patient Identity Removed stamp, so a behaviour change without a version bump fails
the suite. Never re-pin an existing version: studies stamped with it were written by the old code.

The tag table is the one dicom-anonymizer ships for the 2026c edition of the standard (``ANNEX_E_TABLE``). Its
action functions are not used. They apply the table to the top level of a dataset only, so an identifying attribute
inside a sequence survives; they write ``ANONYMIZED`` as their dummy, which the platform never displays (see
``docs/anonymisation-compliance.md``); and they replace UIDs with random values cached per process, which splits a
study uploaded in several requests into several studies.

What the pass does, wherever an attribute appears, top level or nested:

- **Annex E actions.** D writes a dummy, Z a zero-length value, X removes, U replaces a UID. A composite code resolves
  to the form that keeps the dataset valid without knowing the IOD: Z/D, X/D and X/Z/D to D, X/Z to Z, and X/Z/U* (a
  referencing sequence) to the sequence with its UIDs replaced.
  U on an element that is not a UID by VR replaces its string values the same way, and refuses the file when the
  value is not a string, so a UID declared under another VR is never kept as uploaded.
- **Attributes the table does not list.** A public attribute with no action is decided by its VR, the dictionary's
  where the file declares another: a UID is replaced unless its tag names a class, a transfer syntax, a coding
  scheme, a context group or a mapping resource (``KEPT_UID_TAGS``) or its value is one the standard registers;
  a person name and free text (LT, ST, UT, UC, UR) are emptied; a date or time takes the dummy; an attribute the
  dictionary does not know, or one of VR UN, is removed. Codes, short strings, numbers and pixel data are kept, which
  is what leaves the image and its geometry readable.
- **Dummies.** ``X`` for text, the recording pass's fixed date ``19850101`` and a midnight time for every date and
  time, ``0`` for numbers. Dates are dummied for Z as well as D, so no date of the source survives in any form.
- **UIDs.** ``2.25.`` followed by the decimal of the first 128 bits of HMAC-SHA256 over the source UID (PS3.5 B.2),
  under a key of the uploading author's own: ``HMAC-SHA256(DICOM_UID_KEY, "epicurrents.dicom.uid.author:<pk>")``,
  the author's primary key in decimal (:func:`author_uid_key`). Deriving a key per author, rather than hashing the
  author and the UID together, leaves no encoding in which one author's input reads as another's. Deterministic
  across requests, workers and restarts for one author, so a study that author uploads in several batches converges
  on one study and a reference inside it still names its target. Unrelated between authors: an account that uploads
  a file carrying a guessed source UID reads back its own replacement, which says nothing about the one on a study
  another author shared with it, and two authors' copies of one examination carry no equal UID. What remains is the
  author's own: the same author uploading a source UID again gets the same replacement, which is what convergence
  needs. The primary key is never written anywhere by the pass.
- **Kept.** Study and series descriptions (``KEPT``), against the profile's X: ingest runs them through the text
  hygiene check instead. With ``retain_patient_characteristics``, the attributes in ``PATIENT_CHARACTERISTICS``.
- **Removed.** Every private attribute, private creators included, and the 128-byte preamble, which a dual-format
  file uses for a header of its own.
- **Marked.** De-identification Method naming this version, the Annex E codes of what was applied to the header, and
  Longitudinal Temporal Information Modified ``REMOVED``, so a file that leaves the platform says what was done to
  it. Patient Identity Removed is ``YES`` only for a file the upload gate's acquisition allowlist passed without
  asking anyone (``ingest.pixel_review_required``), and ``NO`` for every other file: its header went through the
  same pass, but its pixels were stored on the uploader's confirmation and nothing at the platform determined that
  they carry no identity. PS3.3 C.7.1.1 requires the method attributes with ``YES`` and lets them be present
  otherwise.

The file meta header is rebuilt from the class, the new instance UID and the transfer syntax, which drops the source
application entity titles and any private information it carried. Pixel data is untouched: text burned into the
image is not a header attribute, and the upload gate asks the author to confirm it is absent for every file outside
its allowlist (``ingest.py``).
"""

import hashlib
import hmac

from dicomanonymizer.dicomfields_selector import dicom_anonymization_database_selector
from pydicom import Dataset, Sequence, dcmread
from pydicom.datadict import DicomDictionary
from pydicom.dataset import FileMetaDataset
from pydicom.multival import MultiValue
from pydicom.tag import Tag
from pydicom.uid import UID

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
_FREE_TEXT_VRS = frozenset({"LT", "ST", "UT", "UC", "UR"})
#: The VRs the fallback acts on when the table is silent; every other VR is kept.
_FALLBACK_VRS = frozenset({"UI", "PN", "UN"}) | _FREE_TEXT_VRS | frozenset(_DATE_DUMMIES)

#: UID attributes the table does not list and the fallback keeps as uploaded: each names a SOP class, a transfer
#: syntax, a coding scheme, a context group or a mapping resource, never an instance, study, series, device or
#: event. Every other unlisted UID is replaced unless its value is registered by the standard.
KEPT_UID_TAGS = frozenset(
    {
        Tag(0x0004, 0x1510),  # Referenced SOP Class UID in File
        Tag(0x0004, 0x1512),  # Referenced Transfer Syntax UID in File
        Tag(0x0004, 0x151A),  # Referenced Related General SOP Class UID in File
        Tag(0x0008, 0x0016),  # SOP Class UID
        Tag(0x0008, 0x001A),  # Related General SOP Class UID
        Tag(0x0008, 0x001B),  # Original Specialized SOP Class UID
        Tag(0x0008, 0x0062),  # SOP Classes in Study
        Tag(0x0008, 0x010C),  # Coding Scheme UID
        Tag(0x0008, 0x0117),  # Context UID
        Tag(0x0008, 0x0118),  # Mapping Resource UID
        Tag(0x0008, 0x040E),  # Stored Instance Transfer Syntax UID
        Tag(0x0008, 0x1150),  # Referenced SOP Class UID
        Tag(0x0008, 0x115A),  # SOP Classes Supported
        Tag(0x0008, 0x3002),  # Available Transfer Syntax UID
        Tag(0x0034, 0x0003),  # Flow Transfer Syntax UID
        Tag(0x0400, 0x0010),  # MAC Calculation Transfer Syntax UID
        Tag(0x0400, 0x0510),  # Encrypted Content Transfer Syntax UID
        Tag(0x3010, 0x0052),  # Pertinent SOP Classes in Study
        Tag(0x3010, 0x0053),  # Pertinent SOP Classes in Series
    }
)

#: Domain separation for the per-author UID key, so the derivation shares no input with any other use of the key.
_AUTHOR_KEY_LABEL = b"epicurrents.dicom.uid.author:"

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


def author_uid_key(key: str, author_id: int) -> bytes:
    """Return the UID key of the author with primary key *author_id*, derived from ``DICOM_UID_KEY``.

    Raises :class:`DeidentificationError` for an empty key or an *author_id* that is not an integer, so no file is
    ever rewritten under a key shared between authors.
    """
    if not key:
        raise DeidentificationError("DICOM_UID_KEY is not set.")
    if isinstance(author_id, bool) or not isinstance(author_id, int):
        raise DeidentificationError("UID replacement needs the uploading author's primary key.")
    return hmac.new(key.encode(), _AUTHOR_KEY_LABEL + str(author_id).encode(), hashlib.sha256).digest()


def _replacement_uid(uid: str, uid_key: bytes) -> str:
    """Return the replacement for *uid* under an author's *uid_key*: ``2.25.`` and 128 bits of HMAC-SHA256."""
    source = "".join(str(uid).replace("\x00", "").split())
    digest = hmac.new(uid_key, source.encode(), hashlib.sha256).digest()
    return f"2.25.{int.from_bytes(digest[:16], 'big')}"


def hash_uid(uid: str, key: str, author_id: int) -> str:
    """Return the replacement for *uid* as uploaded by the author *author_id*, at most 44 characters.

    *key* is ``DICOM_UID_KEY``. The same UID gives the same result for one author and unrelated results for two.
    """
    return _replacement_uid(uid, author_uid_key(key, author_id))


def _is_registered_uid(value: str) -> bool:
    """Return True for a UID the standard itself registers: a SOP class, a transfer syntax, a well-known frame."""
    uid = UID("".join(str(value).replace("\x00", "").split()))
    return not uid.is_private and uid.name != uid


def _replace_uids(element, key: bytes, *, keep_registered: bool = False) -> None:
    """Replace every UID *element* holds, whatever VR the file declares for it.

    Raises :class:`DeidentificationError` for a value that is not a string, which cannot be a UID and would
    otherwise be kept as uploaded. With *keep_registered*, a value the standard registers is left as it is.
    """
    value = element.value
    values = list(value) if isinstance(value, (MultiValue, list, tuple)) else [value]
    values = [v for v in values if v is not None and v != ""]
    if not values:
        return
    if not all(isinstance(v, str) for v in values):
        raise DeidentificationError(f"Cannot replace the UID at {element.tag}: VR {element.VR} holds no string.")
    replaced = [v if keep_registered and _is_registered_uid(v) else _replacement_uid(v, key) for v in values]
    element.value = replaced if len(replaced) > 1 else replaced[0]


def _dummy(ds: Dataset, element, key: bytes) -> None:
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


def _walk(ds: Dataset, key: bytes, retain_patient_characteristics: bool) -> None:
    """Apply the table to every attribute of *ds* and, recursively, of every sequence item it holds.

    *key* is the author's UID key here and in every helper it calls, never ``DICOM_UID_KEY`` itself.

    An attribute the table is silent on is removed when the dictionary does not know it or its VR is UN, and is
    otherwise decided by :func:`_fallback`.
    """
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
        elif (action == "U" and element.VR != "SQ") or (action == "U*" and element.VR == "UI"):
            _replace_uids(element, key)
        elif action == "U*" and element.VR != "SQ":
            _empty(ds, element)
        elif action is None and (tag not in DicomDictionary or element.VR == "UN"):
            del ds[tag]
        elif element.VR == "SQ":
            # K, U* on a sequence, or no rule: keep the sequence and clean what it holds.
            for item in element.value:
                _walk(item, key, retain_patient_characteristics)
        elif action is None:
            _fallback(element, key)


def _fallback(element, key: bytes) -> None:
    """Decide a dictionary attribute the table has no action for by its VR, so an unlisted identifier is not kept.

    The dictionary's VR decides where the file declares another, so an explicit-VR file cannot keep a person name
    by calling it a long string.
    """
    declared = DicomDictionary[element.tag][0]
    vr = declared if declared in _FALLBACK_VRS else element.VR
    if vr == "UI":
        if element.tag not in KEPT_UID_TAGS:
            _replace_uids(element, key, keep_registered=True)
    elif vr == "PN" or vr in _FREE_TEXT_VRS:
        element.value = ""
    elif vr in _DATE_DUMMIES:
        element.value = _DATE_DUMMIES[vr]


def _mark(ds: Dataset, retain_patient_characteristics: bool, identity_removed: bool) -> None:
    """Write the attributes that say what was done to the dataset (PS3.15 E.1.1, PS3.3 C.7.1.1).

    Patient Identity Removed is ``YES`` with *identity_removed* and ``NO`` without it; a value the source carried
    is overwritten either way. The method, its codes and the temporal marking describe the header pass, which ran
    in both cases.
    """
    ds.PatientIdentityRemoved = "YES" if identity_removed else "NO"
    ds.LongitudinalTemporalInformationModified = "REMOVED"
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


def deidentify_dataset(
    ds: Dataset,
    *,
    key: str,
    author_id: int,
    identity_removed: bool,
    retain_patient_characteristics: bool = False,
) -> Dataset:
    """De-identify *ds* in place and return it.

    *key* is ``DICOM_UID_KEY``, which must not be empty, and *author_id* the primary key of the user the file is
    stored for: UIDs are replaced under that author's key. *identity_removed* is the caller's statement that the
    pixels need nobody's check, and decides the Patient Identity Removed stamp alone; it has no default, so a call
    site cannot stamp ``YES`` by leaving it out. The caller decides it from the dataset as uploaded, before this
    pass changes anything it reads.
    """
    uid_key = author_uid_key(key, author_id)
    _walk(ds, uid_key, retain_patient_characteristics)
    _mark(ds, retain_patient_characteristics, identity_removed)
    source_meta = getattr(ds, "file_meta", None)
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = ds.SOPClassUID
    meta.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
    meta.TransferSyntaxUID = source_meta.TransferSyntaxUID
    ds.file_meta = meta
    ds.preamble = b"\x00" * 128
    return ds


def deidentify_file(
    source: str,
    target: str,
    *,
    key: str,
    author_id: int,
    identity_removed: bool,
    retain_patient_characteristics: bool = False,
) -> None:
    """Read the DICOM file at *source* in full, de-identify it and write the result to *target*.

    The keyword arguments are :func:`deidentify_dataset`'s. *target* must not be *source*: pydicom may read
    deferred values from the source while writing.
    """
    ds = dcmread(source)
    deidentify_dataset(
        ds,
        key=key,
        author_id=author_id,
        identity_removed=identity_removed,
        retain_patient_characteristics=retain_patient_characteristics,
    )
    ds.save_as(target, enforce_file_format=True)
