"""Shared DICOM ingest logic for the upload endpoint and the import command.

Both entry points follow the same shape: stage a file, parse its header with
pydicom, and persist the study/series/instance rows for the uploading author.
This module owns the parsing, tag extraction, and persistence so the two
callers cannot drift apart.

Extraction is synchronous — ``dcmread(stop_before_pixels=True)`` reads only
the header, so a full tag extraction costs microseconds once the dataset is
in memory. There is deliberately no per-file Celery indexing task: re-reading
the same file in a worker doubles the I/O and re-creates a retry surface for
no benefit.
"""

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass

from django.contrib.contenttypes.models import ContentType

from plugins.dicom.models import DicomInstance, DicomSeries, DicomStudy

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
) -> PersistResult:
    """Persist the study/series/instance rows for one parsed DICOM file.

    The study is scoped to *author* — the same StudyInstanceUID uploaded by
    another user resolves to that user's own copy, never someone else's. When
    a study row is created, the author receives a self-``AccessRight`` with
    full rights, mirroring the recordings upload contract.

    ``status`` is the initial instance status: the upload endpoint passes
    ``PENDING`` (the file moves to final storage after commit), the import
    command passes ``READY`` (the file is copied before persisting).

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

    instance_fields = extract_instance_fields(ds)
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
    """Recompute ``num_instances`` and ``modalities`` for *study* from the DB."""
    count = DicomInstance.objects.filter(
        series__study=study,
        status=DicomInstance.Status.READY,
    ).count()
    modality_values = (
        DicomSeries.objects.filter(study=study).exclude(modality="").values_list("modality", flat=True).distinct()
    )
    study.num_instances = count
    study.modalities = ",".join(sorted(set(modality_values)))
    study.save(update_fields=["num_instances", "modalities", "modified_at"])
