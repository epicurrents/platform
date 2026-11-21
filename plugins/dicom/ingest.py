"""Shared DICOM ingest logic for the upload endpoint and the import command.

Both entry points follow the same shape: stage a file, prepare it, and persist
the study/series/instance rows for the uploading author. This module owns the
parsing, the de-identification step, tag extraction, and persistence so the two
callers cannot drift apart.

Preparing a file (:func:`prepare_file`) parses its header and, while
``DICOM_DEIDENTIFY_ON_INGEST`` is on, rewrites it through
``plugins/dicom/deidentify.py`` into a new file beside it. The rows are
extracted from the rewritten file, so every field and every byte the platform
serves is the de-identified one; the as-uploaded file is kept only until the
caller has copied it to the originals volume, when the preservation mode asks
for that, and deleted.

Extraction is synchronous — ``dcmread(stop_before_pixels=True)`` reads only
the header, so a full tag extraction costs microseconds once the dataset is
in memory. There is deliberately no per-file Celery indexing task: re-reading
the same file in a worker doubles the I/O and re-creates a retry surface for
no benefit.
"""

import hashlib
import logging
import os
from collections.abc import Sequence
from dataclasses import dataclass

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.utils import timezone

from plugins.dicom.models import DicomInstance, DicomSeries, DicomStudy

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _require_pydicom():
    """Import pydicom lazily so the app can load without it installed.

    Raises ``ImportError`` with a helpful message if the package is absent.
    """
    try:
        import pydicom

        return pydicom
    except ImportError:
        raise ImportError("pydicom is required for DICOM ingest. Install it with: pip install pydicom")


def _clean_str(value) -> str:
    """Return *value* as a stripped string, or empty string for None/blank."""
    if value is None:
        return ""
    return str(value).strip().replace("\x00", "")


def _clean_uid(value) -> str:
    """Strip whitespace *and* embedded spaces from a DICOM UID value."""
    cleaned = _clean_str(value)
    return "".join(cleaned.split())


def _first_value(value) -> str:
    """Return the first entry of a multi-value tag, or the value itself.

    DICOM windowing tags (WindowCenter / WindowWidth) may be single-valued or
    multi-valued. Only real sequences are unwrapped — a plain string is a
    single value, not a sequence of characters.
    """
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        value = value[0] if len(value) else None
    return _clean_str(value)


def sha256_file(path: str) -> str:
    """Compute SHA-256 hex digest of a file without loading it all into RAM."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_dicom_header(path: str):
    """Read the DICOM header of *path* and return the pydicom dataset.

    Pixel data is not read. Raises whatever pydicom raises on a non-DICOM or
    corrupt file — callers translate that into a per-file rejection.
    """
    pydicom = _require_pydicom()
    return pydicom.dcmread(path, stop_before_pixels=True, force=False)


class MissingUidsError(ValueError):
    """Raised when a parsed file lacks any of the three required UIDs."""


def required_uids(ds) -> tuple[str, str, str]:
    """Return (study_uid, series_uid, sop_uid) or raise :class:`MissingUidsError`."""
    study_uid = _clean_uid(getattr(ds, "StudyInstanceUID", ""))
    series_uid = _clean_uid(getattr(ds, "SeriesInstanceUID", ""))
    sop_uid = _clean_uid(getattr(ds, "SOPInstanceUID", ""))
    if not study_uid or not series_uid or not sop_uid:
        raise MissingUidsError("Missing required UIDs (Study/Series/SOPInstance).")
    return study_uid, series_uid, sop_uid


# ---------------------------------------------------------------------------
# De-identification
# ---------------------------------------------------------------------------


class DeidentificationUnavailable(RuntimeError):
    """Raised when de-identification is on but ``DICOM_UID_KEY`` is empty, so no file can be accepted."""

    message = (
        "DICOM de-identification is on but DICOM_UID_KEY is not set. Run `python manage.py init_env` "
        "or `scripts/enable_plugin.sh dicom` to generate it, then restart."
    )

    def __init__(self):
        super().__init__(self.message)


class DeidentificationFailed(ValueError):
    """Raised when the pass cannot rewrite a parsed file; the file is refused rather than stored as uploaded."""


class PixelReviewRequired(ValueError):
    """Raised for a file that may carry text in its pixels when the author has not confirmed it carries none."""

    code = "burned_in_confirmation_required"

    def __init__(self):
        super().__init__(
            "This image may show identifying text in its pixels. Check it and confirm before uploading it."
        )


#: Modalities whose images routinely carry text rendered into the pixels: ultrasound, secondary capture, other.
BURNED_IN_MODALITIES = frozenset({"US", "SC", "OT"})

#: SOP class UID prefixes of the same kinds of object, which a screen capture or a converted ultrasound can carry
#: under another Modality: the Secondary Capture family (…1.1.7), the Ultrasound family (…1.1.6) and the retired
#: Ultrasound Multi-frame class (…1.1.3.1).
BURNED_IN_SOP_CLASS_PREFIXES = ("1.2.840.10008.5.1.4.1.1.7", "1.2.840.10008.5.1.4.1.1.6", "1.2.840.10008.5.1.4.1.1.3.1")


def pixel_review_required(ds) -> bool:
    """Return True when *ds* may carry identifying text in its pixels, which the header pass cannot remove.

    Decided from the parsed header on the server: an ultrasound, secondary
    capture or other image by Modality or by SOP class, or any image whose
    Burned In Annotation is YES. The SOP class is checked as well because a
    secondary capture keeps the Modality of what it captured.
    """
    modality = _clean_str(getattr(ds, "Modality", "")).upper()
    burned_in = _clean_str(getattr(ds, "BurnedInAnnotation", "")).upper()
    sop_class = _clean_uid(getattr(ds, "SOPClassUID", ""))
    by_class = any(sop_class == p or sop_class.startswith(f"{p}.") for p in BURNED_IN_SOP_CLASS_PREFIXES)
    return modality in BURNED_IN_MODALITIES or burned_in == "YES" or by_class


def deidentification_enabled() -> bool:
    """Return True when ingest rewrites files through the de-identification pass."""
    return getattr(settings, "DICOM_DEIDENTIFY_ON_INGEST", True)


def require_uid_key() -> str:
    """Return ``DICOM_UID_KEY``, or raise :class:`DeidentificationUnavailable` when it is empty."""
    key = getattr(settings, "DICOM_UID_KEY", "")
    if not key:
        raise DeidentificationUnavailable()
    return key


@dataclass
class PreparedFile:
    """A parsed file ready to persist: the header to extract from and the bytes to store.

    ``path`` is the file to store: the rewritten one when the pass ran, else the
    source itself. ``source_hash`` is the digest of the file as uploaded.
    ``pixel_review`` is the instance's ``DicomInstance.PixelReview`` value.
    """

    ds: object
    path: str
    file_size: int
    file_hash: str
    source_hash: str
    deidentification_version: int | None = None
    patient_characteristics_retained: bool = False
    pixel_review: str = ""


def prepare_file(
    source_path: str,
    *,
    target_path: str,
    source_hash: str | None = None,
    confirm_burned_in: bool = False,
) -> PreparedFile:
    """Parse *source_path* and, with de-identification on, rewrite it to *target_path*.

    Raises whatever pydicom raises for a file that is not DICOM,
    :class:`MissingUidsError` for one lacking the required UIDs,
    :class:`PixelReviewRequired` for one :func:`pixel_review_required` flags
    when *confirm_burned_in* is not set, and :class:`DeidentificationFailed`
    when the pass cannot rewrite it, after removing anything it wrote at
    *target_path*. With de-identification off, nothing is written, no
    confirmation is asked for, and the returned ``path`` is *source_path*.
    """
    ds = parse_dicom_header(source_path)
    required_uids(ds)
    source_hash = source_hash or sha256_file(source_path)
    if not deidentification_enabled():
        return PreparedFile(
            ds=ds,
            path=source_path,
            file_size=os.path.getsize(source_path),
            file_hash=source_hash,
            source_hash=source_hash,
        )

    from plugins.dicom.deidentify import DICOM_DEIDENTIFICATION_VERSION, deidentify_file

    review_required = pixel_review_required(ds)
    if review_required and not confirm_burned_in:
        raise PixelReviewRequired()
    key = require_uid_key()
    retain = getattr(settings, "DICOM_RETAIN_PATIENT_CHARACTERISTICS", False)
    try:
        deidentify_file(source_path, target_path, key=key, retain_patient_characteristics=retain)
        cleaned = parse_dicom_header(target_path)
        required_uids(cleaned)
    except Exception as exc:
        # The source is a parseable DICOM file, so any failure here is the pass's:
        # an attribute it has no rule for, or a dataset pydicom cannot write back.
        logger.warning("DICOM de-identification failed: %s", type(exc).__name__)
        try:
            os.remove(target_path)
        except OSError:
            pass
        raise DeidentificationFailed("The file could not be de-identified.") from exc
    return PreparedFile(
        ds=cleaned,
        path=target_path,
        file_size=os.path.getsize(target_path),
        file_hash=sha256_file(target_path),
        source_hash=source_hash,
        deidentification_version=DICOM_DEIDENTIFICATION_VERSION,
        patient_characteristics_retained=retain,
        pixel_review=DicomInstance.PixelReview.ATTESTED if review_required else DicomInstance.PixelReview.NOT_REQUIRED,
    )


def preserve_original(
    source_path: str,
    *,
    stored_name: str,
    original_name: str,
    prepared: PreparedFile | None,
    author_id: int,
    reason: str,
) -> None:
    """Copy the as-uploaded file to the originals volume under ``plugins/dicom/<stored prefix>/``.

    Called only for a file the pass rewrote or failed on; the volume is
    write-only (``recordings/preservation.py``), and the manifest names the
    stored file and the digests, never a UID or a tag value.
    """
    from recordings.preservation import write_plugin_original

    manifest = {
        "stored_name": stored_name,
        "original_name": original_name,
        "source_hash": prepared.source_hash if prepared else sha256_file(source_path),
        "file_size": os.path.getsize(source_path),
        "stored_hash": prepared.file_hash if prepared else None,
        "deidentification_version": prepared.deidentification_version if prepared else None,
        "author_id": author_id,
        "uploaded_at": timezone.now().isoformat(),
        "preservation_reason": reason,
    }
    write_plugin_original(
        "dicom",
        stored_name.split(".", 1)[0],
        source_path,
        reason=reason,
        original_name=original_name,
        manifest=manifest,
    )


# ---------------------------------------------------------------------------
# Tag extraction
# ---------------------------------------------------------------------------


def extract_study_fields(ds) -> dict:
    """Extract study-level tags from a pydicom dataset."""
    return {
        "study_date": _clean_str(getattr(ds, "StudyDate", "")),
        "study_time": _clean_str(getattr(ds, "StudyTime", "")),
        "study_description": _clean_str(getattr(ds, "StudyDescription", "")),
        "patient_name": _clean_str(getattr(ds, "PatientName", "")),
        "patient_id": _clean_str(getattr(ds, "PatientID", "")),
        "patient_birth_date": _clean_str(getattr(ds, "PatientBirthDate", "")),
        "patient_sex": _clean_str(getattr(ds, "PatientSex", "")),
        "patient_age": _clean_str(getattr(ds, "PatientAge", "")),
        "accession_number": _clean_str(getattr(ds, "AccessionNumber", "")),
    }


def extract_series_fields(ds) -> dict:
    """Extract series-level tags from a pydicom dataset."""
    return {
        "series_description": _clean_str(getattr(ds, "SeriesDescription", "")),
        "series_number": _clean_str(getattr(ds, "SeriesNumber", "")),
        "series_date": _clean_str(getattr(ds, "SeriesDate", "")),
        "modality": _clean_str(getattr(ds, "Modality", "")),
        "slice_thickness": _clean_str(getattr(ds, "SliceThickness", "")),
    }


def extract_instance_fields(ds) -> dict:
    """Extract instance-level tags (identity, geometry, windowing).

    File identity (stored_name, file_size, file_hash) and lifecycle status are
    not extracted here — the caller owns those.
    """
    pixel_spacing_raw = getattr(ds, "PixelSpacing", None)
    iop_raw = getattr(ds, "ImageOrientationPatient", None)
    ipp_raw = getattr(ds, "ImagePositionPatient", None)
    image_type_raw = getattr(ds, "ImageType", None)

    return {
        "sop_class_uid": _clean_uid(getattr(ds, "SOPClassUID", "")),
        "instance_number": _clean_str(getattr(ds, "InstanceNumber", "")),
        "columns": getattr(ds, "Columns", None),
        "rows": getattr(ds, "Rows", None),
        "photometric_interpretation": _clean_str(getattr(ds, "PhotometricInterpretation", "")),
        "bits_allocated": getattr(ds, "BitsAllocated", None),
        "bits_stored": getattr(ds, "BitsStored", None),
        "pixel_representation": getattr(ds, "PixelRepresentation", None),
        "samples_per_pixel": getattr(ds, "SamplesPerPixel", None),
        "high_bit": getattr(ds, "HighBit", None),
        "number_of_frames": (int(getattr(ds, "NumberOfFrames", 0) or 0) or None),
        "pixel_spacing": ([str(v) for v in pixel_spacing_raw] if pixel_spacing_raw else None),
        "image_orientation_patient": ([str(v) for v in iop_raw] if iop_raw else None),
        "image_position_patient": ([str(v) for v in ipp_raw] if ipp_raw else None),
        "image_type": ([str(v) for v in image_type_raw] if image_type_raw else None),
        "frame_of_reference_uid": _clean_uid(getattr(ds, "FrameOfReferenceUID", "")),
        "window_center": _first_value(getattr(ds, "WindowCenter", None)),
        "window_width": _first_value(getattr(ds, "WindowWidth", None)),
        "rescale_slope": _clean_str(getattr(ds, "RescaleSlope", "")),
        "rescale_intercept": _clean_str(getattr(ds, "RescaleIntercept", "")),
    }


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


@dataclass
class PersistResult:
    """Outcome of persisting one parsed file.

    ``outcome`` is one of:

    - ``"created"`` — a new instance row was created.
    - ``"replaced"`` — an existing non-READY row (stranded PENDING or FAILED
      from an earlier attempt) was updated in place with the new file's
      identity; ``previous_stored_name`` names the superseded file for the
      caller to unlink after commit.
    - ``"duplicate"`` — a READY instance with the same SOPInstanceUID already
      exists in this series; nothing was written.
    """

    instance: DicomInstance
    study: DicomStudy
    series: DicomSeries
    outcome: str
    study_created: bool = False
    previous_stored_name: str | None = None


def persist_instance(
    *,
    author,
    ds,
    stored_name: str,
    file_size: int,
    file_hash: str,
    status: str = DicomInstance.Status.PENDING,
    source_hash: str = "",
    deidentification_version: int | None = None,
    patient_characteristics_retained: bool = False,
    pixel_review: str = "",
) -> PersistResult:
    """Persist the study/series/instance rows for one parsed DICOM file.

    The study is scoped to *author* — the same StudyInstanceUID uploaded by
    another user resolves to that user's own copy, never someone else's. When
    a study row is created, the author receives a self-``AccessRight`` with
    full rights, mirroring the recordings upload contract.

    ``status`` is the initial instance status: the upload endpoint passes
    ``PENDING`` (the file moves to final storage after commit), the import
    command passes ``READY`` (the file is copied before persisting).
    ``source_hash``, ``deidentification_version``,
    ``patient_characteristics_retained`` and ``pixel_review`` come from
    :class:`PreparedFile`.

    The caller owns the surrounding ``transaction.atomic()`` block; raising
    :class:`MissingUidsError` (from :func:`required_uids`) or a database error
    rolls back everything the caller grouped together.
    """
    from epicurrents.models import AccessRight

    study_uid, series_uid, sop_uid = required_uids(ds)

    study_fields = extract_study_fields(ds)
    study, study_created = DicomStudy.objects.get_or_create(
        author=author,
        study_instance_uid=study_uid,
        defaults={
            "content_hash": DicomStudy.make_content_hash(author.pk, study_uid),
            **study_fields,
        },
    )
    if study_created:
        AccessRight.objects.create(
            content_type=ContentType.objects.get_for_model(study, for_concrete_model=False),
            object_id=str(study.pk),
            access_giver=author,
            access_target=author,
            can_read=True,
            can_write=True,
            can_share=True,
        )
    else:
        # Backfill blanks — a later file in the same study may carry more
        # complete tags. Never overwrite an already-populated field.
        changed = []
        for field, value in study_fields.items():
            if value and not getattr(study, field):
                setattr(study, field, value)
                changed.append(field)
        if changed:
            study.save(update_fields=[*changed, "modified_at"])

    series_fields = extract_series_fields(ds)
    series, series_created = DicomSeries.objects.get_or_create(
        study=study,
        series_instance_uid=series_uid,
        defaults=series_fields,
    )
    if not series_created:
        changed = []
        for field, value in series_fields.items():
            if value and not getattr(series, field):
                setattr(series, field, value)
                changed.append(field)
        if changed:
            series.save(update_fields=changed)

    instance_fields = {
        **extract_instance_fields(ds),
        "source_hash": source_hash,
        "deidentification_version": deidentification_version,
        "patient_characteristics_retained": patient_characteristics_retained,
        "pixel_review": pixel_review,
    }
    existing = DicomInstance.objects.filter(series=series, sop_instance_uid=sop_uid).first()

    if existing is not None and existing.status == DicomInstance.Status.READY:
        return PersistResult(
            instance=existing,
            study=study,
            series=series,
            outcome="duplicate",
            study_created=study_created,
        )

    if existing is not None:
        # Stranded PENDING (crash between commit and file move) or FAILED
        # (move failure) row from an earlier attempt — replace in place.
        previous_stored_name = existing.stored_name
        for field, value in instance_fields.items():
            setattr(existing, field, value)
        existing.stored_name = stored_name
        existing.file_size = file_size
        existing.file_hash = file_hash
        existing.status = status
        existing.error_message = ""
        existing.save()
        return PersistResult(
            instance=existing,
            study=study,
            series=series,
            outcome="replaced",
            study_created=study_created,
            previous_stored_name=previous_stored_name,
        )

    instance = DicomInstance.objects.create(
        series=series,
        sop_instance_uid=sop_uid,
        stored_name=stored_name,
        file_size=file_size,
        file_hash=file_hash,
        status=status,
        **instance_fields,
    )
    return PersistResult(
        instance=instance,
        study=study,
        series=series,
        outcome="created",
        study_created=study_created,
    )


def refresh_study_aggregates(study: DicomStudy) -> None:
    """Recompute the study's cached fields from its ready instances.

    ``deidentification_version`` is the lowest version among them, or null as
    soon as one is stored as uploaded, so a study a later batch added raw files
    to stops counting as de-identified.
    """
    ready = DicomInstance.objects.filter(series__study=study, status=DicomInstance.Status.READY)
    versions = list(ready.values_list("deidentification_version", flat=True))
    modality_values = (
        DicomSeries.objects.filter(study=study).exclude(modality="").values_list("modality", flat=True).distinct()
    )
    study.num_instances = len(versions)
    study.modalities = ",".join(sorted(set(modality_values)))
    study.deidentification_version = min(versions) if versions and None not in versions else None
    study.patient_characteristics_retained = ready.filter(patient_characteristics_retained=True).exists()
    study.save(
        update_fields=[
            "num_instances",
            "modalities",
            "deidentification_version",
            "patient_characteristics_retained",
            "modified_at",
        ]
    )
