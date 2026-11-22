"""DICOMweb search and metadata for the *dicom* plugin: the parts OHIF's ``dicomweb`` data source needs.

The endpoints in ``urls.py`` answer QIDO-RS study and series searches and WADO-RS metadata from the indexed rows,
never from the files; pixels keep coming from the WADO-URI endpoint, which the OHIF configuration names as the
data source's ``wadoUriRoot``. This module holds what they share: which studies a caller may list, how a UID
resolves to one of them, the QIDO matching rules, and the DICOM JSON encoding.

A ``StudyInstanceUID`` is unique per author, not globally: two users who upload the same study hold two copies.
With de-identification on the copies carry different UIDs, since the pass replaces a UID under its author's own
key, so one UID names one copy; stored as uploaded, the copies share the source UID. Nothing here relies on either.
DICOMweb addresses a study by UID alone, so a UID resolves to the caller's own copy, then to the lowest-keyed copy
the study listing shows them, then to the lowest-keyed copy they can read by any route (an attached parent
included). The search applies the same preference when it collapses copies to one row per UID, which is what keeps
the study a list row names and the study that opens the same one.
"""

import math

from django.contrib.contenttypes.models import ContentType
from django.db.models import Count, Prefetch, Q
from pydicom.datadict import dictionary_VR
from pydicom.dataset import Dataset

from epicurrents.models import AccessRight
from epicurrents.permissions import can_read_object
from plugins.dicom.models import DicomInstance, DicomSeries, DicomStudy

# QIDO study search parameters this endpoint matches on. Any other attribute is
# ignored rather than refused, as the standard allows a server to do.
STUDY_FILTERS = (
    "StudyInstanceUID",
    "PatientName",
    "PatientID",
    "AccessionNumber",
    "StudyDescription",
    "StudyDate",
    "ModalitiesInStudy",
)

_TEXT_FIELDS = {
    "PatientName": "patient_name",
    "PatientID": "patient_id",
    "AccessionNumber": "accession_number",
    "StudyDescription": "study_description",
}


def listed_studies(user):
    """Return the active studies the caller's study listing shows: owned, or held on a direct read grant.

    The grant rows are read here rather than through ``get_read_access_result``, one query for the whole listing,
    so the plugin's terms refusal (``apps.py``) is applied here as well: a grant carrying ``apply_middleware``
    lists no study stored without the header pass, which is what the per-object check answers for it. Per-target
    uniqueness makes the user's row the one whose terms apply.
    """
    qs = DicomStudy.objects.filter(deleted_at__isnull=True)
    if user.is_superuser:
        return qs
    ct = ContentType.objects.get_for_model(DicomStudy)
    # Sets of literals rather than a subquery: object_id is a CharField, and
    # Postgres refuses bigint IN (varchar subquery).
    plain_ids, deidentifying_ids = set(), set()
    for object_id, apply_middleware in (
        AccessRight.objects.active()
        .filter(content_type=ct, can_read=True, access_target=user)
        .values_list("object_id", "apply_middleware")
    ):
        (deidentifying_ids if apply_middleware else plain_ids).add(object_id)
    return qs.filter(
        Q(author=user) | Q(pk__in=plain_ids) | Q(pk__in=deidentifying_ids, deidentification_version__isnull=False)
    )


def resolve_study(user, study_uid: str) -> DicomStudy | None:
    """Return the copy of *study_uid* the caller reaches, or None when there is none they may read."""
    copies = list(DicomStudy.objects.filter(study_instance_uid=study_uid, deleted_at__isnull=True).order_by("pk"))
    own = next((s for s in copies if s.author_id == user.pk), None)
    if own is not None:
        return own
    listed = set(listed_studies(user).filter(study_instance_uid=study_uid).values_list("pk", flat=True))
    return next((s for s in copies if s.pk in listed), None) or next(
        (s for s in copies if can_read_object(user, s)), None
    )


def one_per_uid(user, studies) -> list[DicomStudy]:
    """Collapse copies of the same study to the one ``resolve_study`` would open for the caller."""
    chosen: dict[str, DicomStudy] = {}
    for study in sorted(studies, key=lambda s: s.pk):
        current = chosen.get(study.study_instance_uid)
        if current is None or (study.author_id == user.pk and current.author_id != user.pk):
            chosen[study.study_instance_uid] = study
    return list(chosen.values())


def _text_pattern(value: str) -> str | None:
    """Return the substring a QIDO text match asks for, or None for a match-everything value.

    OHIF wraps a typed filter in ``*`` when the source supports wildcards; a
    value with ``*`` or ``?`` anywhere else is matched on its literal parts.
    """
    stripped = value.replace("?", "*").strip("*")
    if not stripped:
        return None
    return max(stripped.split("*"), key=len)


def _date_range(value: str) -> tuple[str, str] | None:
    """Parse a QIDO date or date range (``YYYYMMDD``, ``YYYYMMDD-YYYYMMDD``, open at either end)."""
    value = value.strip()
    if not value:
        return None
    if "-" not in value:
        return value, value
    start, _, end = value.partition("-")
    return start.strip() or "00000000", end.strip() or "99999999"


def _split(value: str) -> list[str]:
    """Split a multi-valued query parameter, accepting both the DICOM backslash and a comma."""
    return [v.strip() for v in value.replace("\\", ",").split(",") if v.strip()]


def search_studies(user, params) -> list[DicomStudy]:
    """Return one copy per study the caller may list, filtered by the QIDO parameters in *params*."""
    qs = listed_studies(user)
    if params.get("StudyInstanceUID"):
        qs = qs.filter(study_instance_uid__in=_split(params["StudyInstanceUID"]))
    for key, field in _TEXT_FIELDS.items():
        pattern = _text_pattern(params.get(key, ""))
        if pattern:
            qs = qs.filter(**{f"{field}__icontains": pattern})
    date_range = _date_range(params.get("StudyDate", ""))
    if date_range:
        qs = qs.filter(study_date__gte=date_range[0], study_date__lte=date_range[1])
    studies = list(qs)
    wanted = set(_split(params.get("ModalitiesInStudy", "")))
    if wanted:
        studies = [s for s in studies if wanted & set(_split(s.modalities))]
    return sorted(one_per_uid(user, studies), key=lambda s: (s.study_date, s.study_time), reverse=True)


def _finite_numbers(values) -> bool:
    """Return True when every DS / IS value parses as a finite number."""
    for value in values if isinstance(values, (list, tuple)) else [values]:
        try:
            if not math.isfinite(float(value)):
                return False
        except (TypeError, ValueError):
            return False
    return True


def to_json(attributes: dict) -> dict:
    """Encode keyword → value pairs as DICOM JSON, leaving out every empty or unusable value.

    pydicom supplies each keyword's VR and the JSON form of its type: person
    names become ``{"Alphabetic": …}``, DS and IS strings become numbers. A
    value that does not parse for its VR is dropped on its own, rather than
    failing the response — one malformed header must not make a study
    unloadable. That includes ``NaN`` and ``inf``, which parse but encode to
    JSON no browser accepts.
    """
    ds = Dataset()
    for keyword, value in attributes.items():
        if value is None or value == "" or value == []:
            continue
        if dictionary_VR(keyword) in ("DS", "IS") and not _finite_numbers(value):
            continue
        try:
            setattr(ds, keyword, value)
        except (TypeError, ValueError):
            continue
    return ds.to_json_dict()


def _study_attributes(study: DicomStudy) -> dict:
    return {
        "StudyInstanceUID": study.study_instance_uid,
        "StudyDate": study.study_date,
        "StudyTime": study.study_time,
        "StudyDescription": study.study_description,
        "AccessionNumber": study.accession_number,
        "PatientName": study.patient_name,
        "PatientID": study.patient_id,
        "PatientBirthDate": study.patient_birth_date,
        "PatientSex": study.patient_sex,
        "PatientAge": study.patient_age,
    }


def _series_attributes(series: DicomSeries) -> dict:
    return {
        "SeriesInstanceUID": series.series_instance_uid,
        "Modality": series.modality,
        "SeriesNumber": series.series_number,
        "SeriesDescription": series.series_description,
        "SeriesDate": series.series_date,
        "SliceThickness": series.slice_thickness,
    }


def _ready_instances(series: DicomSeries):
    return series.instances.filter(status=DicomInstance.Status.READY)


def series_with_counts(study: DicomStudy) -> list[DicomSeries]:
    """Return the series of *study* in their listing order, each with its ready-instance count as ``ready_instances``.

    One query for the study, where a count per series is one each.
    """
    ready = Count("instances", filter=Q(instances__status=DicomInstance.Status.READY))
    return list(study.series.annotate(ready_instances=ready))


def study_json(study: DicomStudy, series_count: int) -> dict:
    """Return the QIDO-RS study-level response object for *study*, which holds *series_count* series."""
    return to_json(
        {
            **_study_attributes(study),
            "ModalitiesInStudy": _split(study.modalities),
            "NumberOfStudyRelatedSeries": str(series_count),
            "NumberOfStudyRelatedInstances": str(study.num_instances),
        }
    )


def studies_json(studies: list[DicomStudy]) -> list[dict]:
    """Return the QIDO-RS study-level response objects for *studies*, counting their series in one query."""
    counts = dict(
        DicomSeries.objects.filter(study__in=[s.pk for s in studies])
        .order_by()
        .values_list("study_id")
        .annotate(n=Count("pk"))
    )
    return [study_json(study, counts.get(study.pk, 0)) for study in studies]


def series_json(series: DicomSeries, study: DicomStudy) -> dict:
    """Return the QIDO-RS series-level response object for *series* of *study*, as ``series_with_counts`` returns it."""
    return to_json(
        {
            **_series_attributes(series),
            "StudyInstanceUID": study.study_instance_uid,
            "NumberOfSeriesRelatedInstances": str(series.ready_instances),
        }
    )


def instance_json(inst: DicomInstance, series: DicomSeries, study: DicomStudy) -> dict:
    """Return the WADO-RS metadata object for *inst*: the study, series and instance attributes OHIF reads."""
    return to_json(
        {
            **_study_attributes(study),
            **_series_attributes(series),
            "SOPClassUID": inst.sop_class_uid,
            "SOPInstanceUID": inst.sop_instance_uid,
            "InstanceNumber": inst.instance_number,
            "Rows": inst.rows,
            "Columns": inst.columns,
            "BitsAllocated": inst.bits_allocated,
            "BitsStored": inst.bits_stored,
            "HighBit": inst.high_bit,
            "PixelRepresentation": inst.pixel_representation,
            "SamplesPerPixel": inst.samples_per_pixel,
            "PhotometricInterpretation": inst.photometric_interpretation,
            "PixelSpacing": inst.pixel_spacing or [],
            "ImageOrientationPatient": inst.image_orientation_patient or [],
            "ImagePositionPatient": inst.image_position_patient or [],
            "ImageType": inst.image_type or [],
            "FrameOfReferenceUID": inst.frame_of_reference_uid,
            "WindowCenter": inst.window_center,
            "WindowWidth": inst.window_width,
            "RescaleSlope": inst.rescale_slope,
            "RescaleIntercept": inst.rescale_intercept,
            "NumberOfFrames": str(inst.number_of_frames) if inst.number_of_frames else None,
        }
    )


def series_metadata(series: DicomSeries, study: DicomStudy) -> list[dict]:
    """Return WADO-RS metadata for every ready instance of *series*, a series of *study*."""
    return [instance_json(inst, series, study) for inst in _ready_instances(series)]


def study_metadata(study: DicomStudy) -> list[dict]:
    """Return WADO-RS metadata for every ready instance of *study*, series by series, from two queries."""
    ready = Prefetch("instances", queryset=DicomInstance.objects.filter(status=DicomInstance.Status.READY))
    return [
        instance_json(inst, series, study)
        for series in study.series.prefetch_related(ready)
        for inst in series.instances.all()
    ]
