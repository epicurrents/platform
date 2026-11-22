"""API endpoints for the *dicom* plugin.

Mounted at ``/plugin/dicom/api/v1/`` by the plugin URL loader in
``epicurrents/urls.py``.

Endpoints
---------
POST   /dicom/upload/                          Upload one or more DICOM files.
GET    /dicom/studies/                         List studies visible to the caller.
GET    /dicom/studies/{hash}/                  Study detail with series summary.
GET    /dicom/studies/{hash}/ohif-json/        DICOMweb JSON for the OHIF viewer.
DELETE /dicom/studies/{hash}/                  Trash a study (soft delete).
GET    /dicom/wado/                            WADO-URI — stream one DICOM instance file.
GET    /dicom/studies/{hash}/access/           List a study's access rights.
POST   /dicom/studies/{hash}/access/           Grant a user an access right.
DELETE /dicom/studies/{hash}/access/{id}/      Revoke one access right.
GET    /dicomweb/studies                       QIDO-RS study search (see dicomweb.py).
GET    /dicomweb/studies/{uid}/series          QIDO-RS series of a study.
GET    /dicomweb/studies/{uid}/metadata        WADO-RS metadata of a study.
GET    /dicomweb/studies/{uid}/series/{uid}/metadata   WADO-RS metadata of a series.

Upload parses every file's header synchronously, rewrites it through the
de-identification pass while ``DICOM_DEIDENTIFY_ON_INGEST`` is on, and creates
the real study/series/instance rows before responding, so the returned study
hashes are immediately valid for the other endpoints. See
``plugins/dicom/ingest.py`` for the shared prepare/persist logic.
"""

import hashlib
import json
import logging
import math
import os
import shutil
import uuid
from datetime import datetime

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.db import IntegrityError, transaction
from django.db.models import Prefetch
from django.http import FileResponse, HttpResponse
from django.utils import timezone
from ninja import File, NinjaAPI, Query, Schema, UploadedFile
from ninja.errors import HttpError

from activity.audit import log_activity
from epicurrents.api.schemas import AccessRightOut, access_right_out
from epicurrents.assessment import assessment_visible
from epicurrents.auth import enforce_session_csrf
from epicurrents.granting import (
    conferrable_rights,
    ensure_can_confer,
    ensure_can_manage_access,
    ensure_can_revoke,
    held_grant_ids,
    share_refusal,
)
from epicurrents.models import AccessRight
from epicurrents.permissions import (
    can_read_object,
    can_write_object,
    ensure_can_write_object,
)
from epicurrents.text_hygiene import NameWarningOut, name_warnings
from plugins.dicom import dicomweb
from plugins.dicom.ingest import (
    DeidentificationFailed,
    DeidentificationUnavailable,
    MissingUidsError,
    PixelReviewRequired,
    deidentification_enabled,
    persist_instance,
    prepare_file,
    preserve_original,
    refresh_study_aggregates,
    require_uid_key,
)
from plugins.dicom.models import DicomInstance, DicomStudy
from recordings.preservation import REASON_ALL, REASON_FAILED, should_preserve_failed, should_preserve_original

logger = logging.getLogger(__name__)

api = NinjaAPI(
    title="DICOM Plugin API",
    version="1",
    urls_namespace="dicom-api-v1",
)


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------


def _require_auth(request):
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated:
        raise HttpError(401, "Authentication required.")
    # Route session-cookie callers through the shared CSRF chokepoint. No-op
    # for safe methods and for non-session callers; see epicurrents.auth.
    enforce_session_csrf(request)
    return user


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class DicomSeriesOut(Schema):
    """Series descriptor for the study detail response."""

    series_instance_uid: str
    series_description: str
    series_number: str
    series_date: str
    modality: str
    slice_thickness: str
    instance_count: int


class DicomStudyOut(Schema):
    """Study list item."""

    hash: str
    study_instance_uid: str
    study_date: str
    study_time: str
    study_description: str
    patient_name: str
    patient_id: str
    patient_sex: str
    patient_age: str
    accession_number: str
    num_instances: int
    modalities: str
    created_at: datetime
    is_author: bool
    can_share: bool
    #: The de-identification pass every file of the study was rewritten by, or null when any is stored as uploaded.
    deidentification_version: int | None = None


class DicomStudyDetailOut(DicomStudyOut):
    """Study detail with its series."""

    series: list[DicomSeriesOut]


class DicomUploadStudyOut(Schema):
    """One study touched by an upload batch."""

    hash: str
    study_instance_uid: str
    instances_added: int


class DicomUploadFileOut(Schema):
    """Per-file outcome of an upload batch."""

    #: The file's position in the request's ``files``; the report is not in request order.
    index: int
    filename: str
    accepted: bool
    study_hash: str | None = None
    error: str | None = None
    #: A machine-readable reason for a refusal the client can act on: ``burned_in_confirmation_required``.
    code: str | None = None


class DicomNameWarningOut(NameWarningOut):
    """A text-hygiene warning on a study or series description the upload stored, kept as written."""

    study_hash: str


class DicomUploadOut(Schema):
    """Response after a batch upload — real study hashes, per-file report, description warnings."""

    studies: list[DicomUploadStudyOut]
    files: list[DicomUploadFileOut]
    accepted: int
    rejected: int
    warnings: list[DicomNameWarningOut] = []


class GrantStudyAccessIn(Schema):
    """Payload for granting an access right on a study.

    User targets only for now; group grants are a planned extension (see the
    plugin README). There is no ``apply_middleware``: no DICOM serving path
    de-identifies, so every row is written with it off rather than claiming a
    sanitisation that does not happen.
    """

    access_target_id: int
    can_read: bool = True
    can_write: bool = False
    can_share: bool = False
    expires_at: datetime | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _upload_path() -> str:
    path = getattr(settings, "DICOM_UPLOAD_PATH", "/data/dicom")
    os.makedirs(path, exist_ok=True)
    return path


def _staging_path() -> str:
    path = getattr(settings, "DICOM_STAGING_PATH", "/data/dicom-staging")
    os.makedirs(path, exist_ok=True)
    return path


def _get_study_or_404(content_hash: str, user) -> DicomStudy:
    """Return the active study for *content_hash* if the caller can read it.

    Trashed studies, unknown hashes, and access denials all yield the same
    404 so a caller cannot probe for the existence of studies they cannot
    read.
    """
    try:
        study = DicomStudy.objects.get(content_hash=content_hash, deleted_at__isnull=True)
    except DicomStudy.DoesNotExist:
        raise HttpError(404, "Study not found.")
    if not (user.is_superuser or study.author_id == user.pk or can_read_object(user, study)):
        raise HttpError(404, "Study not found.")
    return study


def _visible_study_created_at(study: DicomStudy, user) -> datetime:
    """Return the study's upload time as *user* may see it: exact for the author and superusers, else its month.

    The recording rule (``_visible_created_at`` in ``recordings/api/v1/ninja.py``),
    for the same reason: an upload time falls close to the acquisition, and the
    pass has replaced every date in the header with a dummy.
    """
    from recordings.api.v1.ninja import _visible_created_at

    return _visible_created_at(study, exact=user.is_superuser or study.author_id == user.pk)


def _study_out(study: DicomStudy, user, *, can_share: bool | None = None) -> DicomStudyOut:
    """Serialize a study list item for *user*.

    *can_share* is the answer a listing has already worked out for the whole
    page (``_shareable_study_ids``); left out, it is resolved for this study.
    """
    if can_share is None:
        can_share = _can_share_study(user, study)
    return DicomStudyOut(
        hash=study.content_hash,
        study_instance_uid=study.study_instance_uid,
        study_date=study.study_date,
        study_time=study.study_time,
        study_description=study.study_description,
        patient_name=study.patient_name,
        patient_id=study.patient_id,
        patient_sex=study.patient_sex,
        patient_age=study.patient_age,
        accession_number=study.accession_number,
        num_instances=study.num_instances,
        modalities=study.modalities,
        created_at=_visible_study_created_at(study, user),
        is_author=study.author_id == user.pk,
        can_share=can_share,
        deidentification_version=study.deidentification_version,
    )


def _can_share_study(user, study: DicomStudy) -> bool:
    """Return True when *user* may manage access grants on *study*: author, superuser or a ``can_share`` grant."""
    rights = conferrable_rights(user, study)
    return rights.unrestricted or rights.can_share


def _shareable_study_ids(user, studies) -> set[int]:
    """Return the keys of the *studies* whose access *user* may manage, in one query for the whole listing.

    The batch form of ``_can_share_study``, and the same rule as
    ``epicurrents.granting.conferrable_rights``: the author, a superuser, or
    an active ``can_share`` row targeting the user or one of their groups.
    """
    if user.is_superuser:
        return {study.pk for study in studies}
    own = {study.pk for study in studies if study.author_id == user.pk}
    others = [str(study.pk) for study in studies if study.pk not in own]
    if not others:
        return own
    granted = (
        AccessRight.objects.active()
        .for_target(user)
        .filter(content_type=_study_ct(), object_id__in=others, can_share=True)
        .values_list("object_id", flat=True)
    )
    return own | {int(object_id) for object_id in granted}


#: How many recordings sharing one identifier an attachment lookup checks for write access.
_ATTACHMENT_CANDIDATES = 20


def _resolve_attachment(user, attached_to_type: str | None, attached_to_id: str | None):
    """Resolve the optional upload attachment target.

    Returns ``None`` when no attachment was requested. Today only
    ``type="recording"`` is supported. The target is named by the recording's
    public hash, the 32-character prefix of its stored name that every
    recording response and the attachments listing carry; the 64-character
    content fingerprint is accepted as well, and since several recordings can
    share one, the first the caller may write is taken. Write access is
    required, so a user cannot attach studies to someone else's recording, and
    a recording that does not exist and one the caller may not write both
    answer 404, so the parameter says nothing about what exists.
    """
    if not (attached_to_type or attached_to_id):
        return None
    if not (attached_to_type and attached_to_id):
        raise HttpError(400, "attached_to_type and attached_to_id must be supplied together.")
    type_key = attached_to_type.lower().strip()
    if type_key != "recording":
        raise HttpError(
            400,
            f"Unsupported attached_to_type {attached_to_type!r}. Allowed: ['recording']",
        )
    from recordings.models import Recording

    identifier = attached_to_id.strip()
    active = Recording.objects.filter(deleted_at__isnull=True).order_by("-created_at", "-pk")
    if len(identifier) == 32 and identifier.isalnum():
        normalized = identifier.upper()
        candidates = active.filter(stored_name__startswith=f"{normalized}.") or active.filter(stored_name=normalized)
    elif identifier:
        candidates = active.filter(content_hash=identifier)
    else:
        candidates = Recording.objects.none()
    recording = next((r for r in candidates[:_ATTACHMENT_CANDIDATES] if can_write_object(user, r)), None)
    if recording is None:
        raise HttpError(404, "Attachment target not found.")
    return recording


def _build_ohif_json(study: DicomStudy) -> dict:
    """Build the DICOMweb JSON payload consumed by OHIF's ``dicomjson`` datasource.

    The instance URL uses the WADO-URI endpoint mounted at
    ``/plugin/dicom/api/v1/dicom/wado/``. OHIF recognises the ``dicomweb:``
    prefix and strips it before making its own fetch request. The URL is
    root-relative, as the ``platform`` data source's ``wadoUriRoot`` is: an
    absolute one built from the request would say ``http:`` behind a TLS proxy
    that does not forward the scheme, and the browser would refuse it as mixed
    content. The viewer and this endpoint share an origin.
    """
    wado_base = "/plugin/dicom/api/v1/dicom/wado/"

    study_node: dict = {
        "StudyInstanceUID": study.study_instance_uid,
        "StudyDate": study.study_date,
        "StudyTime": study.study_time,
        "StudyDescription": study.study_description,
        "PatientName": study.patient_name,
        "PatientID": study.patient_id,
        "PatientBirthDate": study.patient_birth_date,
        "AccessionNumber": study.accession_number,
        "PatientAge": study.patient_age,
        "PatientSex": study.patient_sex,
        "NumInstances": study.num_instances,
        "Modalities": study.modalities,
        "series": [],
    }

    ready = Prefetch("instances", queryset=DicomInstance.objects.filter(status=DicomInstance.Status.READY))
    for series in study.series.prefetch_related(ready):
        series_node: dict = {
            "SeriesInstanceUID": series.series_instance_uid,
            "SeriesDescription": series.series_description,
            "SeriesNumber": _number(series.series_number),
            "Modality": series.modality,
            "SliceThickness": _number(series.slice_thickness),
            "instances": [],
        }

        instances = sorted(series.instances.all(), key=_instance_order)
        for inst in instances:
            wado_url = (
                f"{wado_base}?requestType=WADO"
                f"&studyUID={study.study_instance_uid}"
                f"&seriesUID={series.series_instance_uid}"
                f"&objectUID={inst.sop_instance_uid}"
            )
            frames = inst.number_of_frames or 0
            if frames > 1:
                # ``frame`` is the parameter both OHIF's dicomjson source and the
                # cornerstone WADO-URI loader read the 1-based frame index from.
                for i in range(1, frames + 1):
                    series_node["instances"].append(
                        {
                            "metadata": _instance_metadata(inst, series, study),
                            "url": f"dicomweb:{wado_url}&frame={i}",
                        }
                    )
            else:
                series_node["instances"].append(
                    {
                        "metadata": _instance_metadata(inst, series, study),
                        "url": f"dicomweb:{wado_url}",
                    }
                )

        study_node["series"].append(series_node)

    return {"studies": [study_node]}


def _number(value):
    """Return a DS/IS string as an int or float, or None when it does not parse.

    The model keeps DICOM's character-string form; OHIF does arithmetic on the
    dicomjson metadata, where a string concatenates instead of adding.
    """
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    # float() accepts "NaN" and "inf", which serialise to JSON no parser accepts:
    # one malformed header would make the whole study unloadable.
    if not math.isfinite(number):
        return None
    return int(number) if number.is_integer() and "." not in str(value) else number


def _numbers(values):
    """Return a multi-valued DS tag as a list of numbers, or None if any entry does not parse."""
    if not values:
        return None
    numbers = [_number(v) for v in values]
    return None if any(n is None for n in numbers) else numbers


def _instance_order(inst: DicomInstance):
    """Sort key putting instances in numeric InstanceNumber order, unnumbered last."""
    number = _number(inst.instance_number)
    return (number is None, number or 0, inst.sop_instance_uid)


def _instance_metadata(
    inst: DicomInstance,
    series,
    study: DicomStudy,
) -> dict:
    """Return the naturalised metadata OHIF reads for *inst* instead of the file header.

    A tag the file did not carry is left out rather than sent empty: OHIF reads
    an empty WindowWidth as a zero-width window, which renders every image as a
    black-and-white threshold.
    """
    metadata = {
        "Columns": inst.columns,
        "Rows": inst.rows,
        "InstanceNumber": _number(inst.instance_number),
        "SOPClassUID": inst.sop_class_uid,
        "PhotometricInterpretation": inst.photometric_interpretation,
        "BitsAllocated": inst.bits_allocated,
        "BitsStored": inst.bits_stored,
        "PixelRepresentation": inst.pixel_representation,
        "SamplesPerPixel": inst.samples_per_pixel,
        "PixelSpacing": _numbers(inst.pixel_spacing),
        "HighBit": inst.high_bit,
        "ImageOrientationPatient": _numbers(inst.image_orientation_patient),
        "ImagePositionPatient": _numbers(inst.image_position_patient),
        "FrameOfReferenceUID": inst.frame_of_reference_uid,
        "ImageType": inst.image_type,
        "Modality": series.modality,
        "SOPInstanceUID": inst.sop_instance_uid,
        "SeriesInstanceUID": series.series_instance_uid,
        "StudyInstanceUID": study.study_instance_uid,
        "WindowCenter": _number(inst.window_center),
        "WindowWidth": _number(inst.window_width),
        "RescaleSlope": _number(inst.rescale_slope),
        "RescaleIntercept": _number(inst.rescale_intercept),
        "SeriesDate": series.series_date,
        "NumberOfFrames": inst.number_of_frames,
    }
    return {key: value for key, value in metadata.items() if value is not None and value != ""}


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@api.post(
    "/dicom/upload/",
    response=DicomUploadOut,
    summary="Upload one or more DICOM files.",
)
def upload_dicom(
    request,
    files: list[UploadedFile] = File(...),
    attached_to_type: str | None = None,
    attached_to_id: str | None = None,
    confirm_burned_in: bool = False,
):
    """Accept a batch of DICOM files, parse their headers, and persist them.

    Each file is streamed to ``DICOM_STAGING_PATH`` (hashing as it lands),
    header-parsed with pydicom, rewritten through the de-identification pass
    into a second staged file, and — when valid — persisted as real
    study/series/instance rows in one transaction. After the transaction
    commits, the as-uploaded file goes to the originals volume when the
    preservation mode asks for it and is deleted, the stored file moves to
    ``DICOM_UPLOAD_PATH`` and its instance flips to ``ready``. Unparseable
    files, files missing required UIDs, files the pass cannot rewrite,
    oversized files, and duplicates of already-ready instances are rejected
    per file and reported in the ``files`` list; they never abort the rest of
    the batch. With de-identification on and no ``DICOM_UID_KEY``, the whole
    request is refused with a 503 before anything is staged.

    With de-identification on, every file outside the acquisition allowlist
    (``ingest.pixel_review_required``: anything but an original CT, MR or PET
    acquisition carrying neither Burned In Annotation nor Recognizable Visual
    Features YES) is refused with the code
    ``burned_in_confirmation_required`` and nothing written for it, unless
    ``confirm_burned_in`` says the author has checked the batch's images. The
    client resubmits those files, found by their ``index``, with the flag set.
    A batch with nothing accepted answers 400, except when some of its files
    await that confirmation: then it answers 200 with the report.

    ``attached_to_type`` + ``attached_to_id`` optionally attach the resulting
    study to a parent object (today only ``type="recording"``, identified by
    the recording's public hash; requires write access to the recording, and
    answers 404 without it). Attachment is rejected when the batch spans more
    than one study, and is made only with a batch that adds a new file: a
    batch of duplicates says in its reports that nothing was attached.

    Whatever fails, a refusal or an unexpected error, no staged file outlives
    the request; the purge task's staging sweep covers a request that died.

    The returned study hashes are immediately valid for the study endpoints;
    ``num_instances`` reflects the batch as soon as the response is returned.
    """
    user = _require_auth(request)

    max_files = getattr(settings, "DICOM_MAX_UPLOAD_FILES", 500)
    if len(files) > max_files:
        raise HttpError(400, f"Too many files in one request (max {max_files}).")

    attach_target = _resolve_attachment(user, attached_to_type, attached_to_id)

    if deidentification_enabled():
        try:
            require_uid_key()
        except DeidentificationUnavailable as exc:
            logger.error("DICOM upload refused: %s", exc)
            raise HttpError(503, "DICOM upload is unavailable: de-identification is not configured.")

    staging = _staging_path()
    upload = _upload_path()
    max_size = getattr(settings, "DICOM_MAX_UPLOAD_FILE_SIZE", 2 * 1024**3)

    file_reports: list[DicomUploadFileOut] = []
    # Entries that passed staging + preparation and await the DB transaction:
    # (index, original filename, staging path, stored name, PreparedFile). The staging
    # path holds the bytes as uploaded; the prepared file's path the bytes to
    # store, which is the same path when nothing was rewritten.
    staged: list[tuple] = []
    # Every staging path this request has written to, so an unexpected failure can remove them all.
    staging_files: list[str] = []

    def stage(index: int, upload_file) -> None:
        """Stage and prepare one file: append it to ``staged``, or report its refusal and leave nothing staged."""
        original_name = upload_file.name or "upload.dcm"
        stored_name = f"{uuid.uuid4().hex}.dcm"
        staging_file = os.path.join(staging, stored_name)
        staging_files.append(staging_file)

        hasher = hashlib.sha256()
        total_size = 0
        oversized = False
        try:
            with open(staging_file, "wb") as f:
                for chunk in upload_file.chunks():
                    total_size += len(chunk)
                    if total_size > max_size:
                        oversized = True
                        break
                    f.write(chunk)
                    hasher.update(chunk)
        except OSError as exc:
            logger.error("Failed to stage DICOM file %s: %s", stored_name, exc)
            _unlink_quietly(staging_file)
            file_reports.append(
                DicomUploadFileOut(
                    index=index,
                    filename=original_name,
                    accepted=False,
                    error="Failed to store the uploaded file.",
                )
            )
            return

        if oversized:
            _unlink_quietly(staging_file)
            file_reports.append(
                DicomUploadFileOut(
                    index=index,
                    filename=original_name,
                    accepted=False,
                    error=f"File exceeds maximum upload size ({max_size} bytes).",
                )
            )
            return

        try:
            prepared = prepare_file(
                staging_file,
                target_path=os.path.join(staging, f"{stored_name}.deid"),
                author_id=user.pk,
                source_hash=hasher.hexdigest(),
                confirm_burned_in=confirm_burned_in,
            )
        except DeidentificationFailed as exc:
            if should_preserve_failed():
                preserve_original(
                    staging_file,
                    stored_name=stored_name,
                    original_name=original_name,
                    prepared=None,
                    author_id=user.pk,
                    reason=REASON_FAILED,
                )
            _unlink_quietly(staging_file)
            file_reports.append(DicomUploadFileOut(index=index, filename=original_name, accepted=False, error=str(exc)))
            return
        except PixelReviewRequired as exc:
            _unlink_quietly(staging_file)
            file_reports.append(
                DicomUploadFileOut(index=index, filename=original_name, accepted=False, error=str(exc), code=exc.code)
            )
            return
        except MissingUidsError as exc:
            _unlink_quietly(staging_file)
            file_reports.append(
                DicomUploadFileOut(
                    index=index,
                    filename=original_name,
                    accepted=False,
                    error=str(exc),
                )
            )
            return
        except Exception:
            _unlink_quietly(staging_file)
            file_reports.append(
                DicomUploadFileOut(
                    index=index,
                    filename=original_name,
                    accepted=False,
                    error="Not a parseable DICOM file.",
                )
            )
            return

        staged.append((index, original_name, staging_file, stored_name, prepared))

    def discard_everything_staged() -> None:
        """Remove every as-uploaded and rewritten file this request staged, whatever became of it."""
        for staged_path in staging_files:
            _unlink_quietly(staged_path)
            _unlink_quietly(f"{staged_path}.deid")

    try:
        for index, upload_file in enumerate(files):
            stage(index, upload_file)
    except Exception:
        # Anything the per-file handling did not expect: a failure reading the request body, or writing the
        # originals volume's copy. The request fails, and no as-uploaded file may outlive it in staging.
        discard_everything_staged()
        raise

    # Persist all parsed files in one transaction (multi-step write rule).
    persisted: list[tuple] = []  # (index, original_name, staging_file, PreparedFile, PersistResult)
    studies: dict[int, DicomStudy] = {}
    instances_added: dict[int, int] = {}
    duplicate_reports: list[DicomUploadFileOut] = []
    try:
        with transaction.atomic():
            for index, original_name, staging_file, stored_name, prepared in staged:
                result = persist_instance(
                    author=user,
                    ds=prepared.ds,
                    stored_name=stored_name,
                    file_size=prepared.file_size,
                    file_hash=prepared.file_hash,
                    status=DicomInstance.Status.PENDING,
                    source_hash=prepared.source_hash,
                    deidentification_version=prepared.deidentification_version,
                    patient_characteristics_retained=prepared.patient_characteristics_retained,
                    pixel_review=prepared.pixel_review,
                )
                if result.outcome == "duplicate":
                    _discard_staged(staging_file, prepared)
                    duplicate_reports.append(
                        DicomUploadFileOut(
                            index=index,
                            filename=original_name,
                            accepted=False,
                            study_hash=result.study.content_hash,
                            error="Duplicate SOPInstanceUID in this study.",
                        )
                    )
                    continue
                studies[result.study.pk] = result.study
                instances_added[result.study.pk] = instances_added.get(result.study.pk, 0) + 1
                persisted.append((index, original_name, staging_file, prepared, result))

            if attach_target is not None and persisted:
                if len(studies) != 1:
                    raise HttpError(
                        400,
                        "Attachment requires the batch to contain exactly one study.",
                    )
                study = next(iter(studies.values()))
                attach_ct = ContentType.objects.get_for_model(attach_target, for_concrete_model=False)
                already = study.attachment_content_type_id == attach_ct.pk and study.attachment_object_id == str(
                    attach_target.pk
                )
                if study.attachment_object_id and not already:
                    raise HttpError(409, "Study is already attached to another object.")
                if not already:
                    study.attachment_content_type = attach_ct
                    study.attachment_object_id = str(attach_target.pk)
                    study.save(
                        update_fields=[
                            "attachment_content_type",
                            "attachment_object_id",
                            "modified_at",
                        ]
                    )
    except Exception:
        # The transaction rolled back, on a refusal (HttpError) or on anything else, a database error for a value
        # no column takes included: remove every file still in staging. Left there, the as-uploaded bytes would sit
        # on the backed-up media volume with no row pointing at them.
        discard_everything_staged()
        raise

    # An attachment is made with the batch's new instances. A batch that brought none attaches nothing, and each
    # duplicate says so rather than leaving the caller to find the study unattached.
    not_attached = attach_target is not None and not persisted and bool(duplicate_reports)
    if not_attached:
        for report in duplicate_reports:
            report.error = f"{report.error} {_NOT_ATTACHED}"
    file_reports.extend(duplicate_reports)

    # Post-commit: move accepted files to final storage and flip to READY.
    accepted = 0
    attested = 0
    for index, original_name, staging_file, prepared, result in persisted:
        inst = result.instance
        final_path = os.path.join(upload, inst.stored_name)
        if prepared.path != staging_file:
            if should_preserve_original():
                preserve_original(
                    staging_file,
                    stored_name=inst.stored_name,
                    original_name=original_name,
                    prepared=prepared,
                    author_id=user.pk,
                    reason=REASON_ALL,
                )
            _unlink_quietly(staging_file)
        try:
            shutil.move(prepared.path, final_path)
        except OSError as exc:
            logger.error("Failed to move DICOM file %s to storage: %s", inst.stored_name, exc)
            inst.status = DicomInstance.Status.FAILED
            inst.error_message = "Failed to move the file to permanent storage."
            inst.save(update_fields=["status", "error_message", "modified_at"])
            _discard_staged(staging_file, prepared)
            file_reports.append(
                DicomUploadFileOut(
                    index=index,
                    filename=original_name,
                    accepted=False,
                    study_hash=result.study.content_hash,
                    error="Failed to move the file to permanent storage; re-upload to retry.",
                )
            )
            continue
        inst.status = DicomInstance.Status.READY
        inst.save(update_fields=["status", "modified_at"])
        if result.previous_stored_name:
            _unlink_quietly(os.path.join(upload, result.previous_stored_name))
        accepted += 1
        attested += prepared.pixel_review == DicomInstance.PixelReview.ATTESTED
        file_reports.append(
            DicomUploadFileOut(
                index=index,
                filename=original_name,
                accepted=True,
                study_hash=result.study.content_hash,
            )
        )

    for study in studies.values():
        refresh_study_aggregates(study)

    held = sum(1 for report in file_reports if report.code == PixelReviewRequired.code)
    if accepted == 0 and held == 0:
        if not_attached:
            raise HttpError(400, f"No files were accepted: every file is already stored. {_NOT_ATTACHED}")
        raise HttpError(400, "No files were accepted. See the per-file report in logs.")

    file_reports.sort(key=lambda report: report.index)
    single_study = next(iter(studies.values())) if len(studies) == 1 else None
    # Counts and the flag, never which images or what they show: the actor is
    # the row's user, and the instances carry pixel_review themselves.
    log_activity(
        verb="dicom.study.upload",
        target=single_study,
        metadata={
            "study_count": len(studies),
            "accepted": accepted,
            "rejected": len(file_reports) - accepted,
            "confirm_burned_in": confirm_burned_in,
            "burned_in_attested": attested,
            "burned_in_held": held,
        },
    )

    return DicomUploadOut(
        warnings=_description_warnings(studies.values()),
        studies=[
            DicomUploadStudyOut(
                hash=s.content_hash,
                study_instance_uid=s.study_instance_uid,
                instances_added=instances_added.get(s.pk, 0),
            )
            for s in studies.values()
        ],
        files=file_reports,
        accepted=accepted,
        rejected=len(file_reports) - accepted,
    )


_NOT_ATTACHED = "The study was not attached: an attachment is made only with a batch that adds a new file."


def _description_warnings(studies) -> list[DicomNameWarningOut]:
    """Text-hygiene warnings for the descriptions of *studies* and their series.

    The de-identification pass keeps both descriptions, and every grantee sees
    them as written, so an upload reports what looks like an identifier in one,
    as every write of a grantee-visible label does. A warning, never a refusal.
    """
    rows = []
    for study in studies:
        texts = {("study_description", study.study_description)}
        texts |= {("series_description", d) for d in study.series.values_list("series_description", flat=True)}
        for field, text in sorted(texts):
            for row in name_warnings(**{field: text}):
                rows.append(DicomNameWarningOut(study_hash=study.content_hash, **row))
    return rows


def _discard_staged(staging_file: str, prepared) -> None:
    """Remove a staged upload and, when the pass rewrote it, the rewritten copy beside it."""
    _unlink_quietly(staging_file)
    if prepared.path != staging_file:
        _unlink_quietly(prepared.path)


def _unlink_quietly(path: str) -> None:
    """Remove *path*, ignoring a missing file; log any other failure."""
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        logger.warning("Could not remove file %s: %s", path, exc)


@api.get(
    "/dicom/studies/",
    response=list[DicomStudyOut],
    summary="List DICOM studies visible to the caller.",
)
def list_studies(request):
    """Return all active studies the caller owns or holds a read grant on.

    Studies readable only through an attachment (a study attached to a shared
    recording) do not appear here — they surface through their parent object.
    """
    user = _require_auth(request)
    qs = dicomweb.listed_studies(user)

    studies = list(qs.order_by("-created_at"))
    shareable = _shareable_study_ids(user, studies)
    result = [_study_out(s, user, can_share=s.pk in shareable) for s in studies]
    log_activity(verb="dicom.study.list", metadata={"count": len(result)})
    return result


@api.get(
    "/dicom/studies/{content_hash}/",
    response=DicomStudyDetailOut,
    summary="Study detail including series summary.",
)
def get_study(request, content_hash: str):
    """Return study metadata and per-series instance counts."""
    user = _require_auth(request)
    study = _get_study_or_404(content_hash, user)

    series_out = [
        DicomSeriesOut(
            series_instance_uid=s.series_instance_uid,
            series_description=s.series_description,
            series_number=s.series_number,
            series_date=s.series_date,
            modality=s.modality,
            slice_thickness=s.slice_thickness,
            instance_count=s.ready_instances,
        )
        for s in dicomweb.series_with_counts(study)
    ]

    log_activity(verb="dicom.study.read", target=study)
    base = _study_out(study, user)
    return DicomStudyDetailOut(**base.dict(), series=series_out)


@api.get(
    "/dicom/studies/{content_hash}/ohif-json/",
    summary="Generate DICOMweb JSON for the OHIF viewer.",
)
def get_ohif_json(request, content_hash: str):
    """Return the DICOMweb study JSON consumed by OHIF's ``dicomjson`` datasource.

    OHIF is pointed at this URL via its ``datasources`` configuration written
    by ``scripts/build_ohif.sh``.
    """
    user = _require_auth(request)
    study = _get_study_or_404(content_hash, user)
    log_activity(verb="dicom.study.read.ohif_json", target=study)
    return _build_ohif_json(study)


@api.delete(
    "/dicom/studies/{content_hash}/",
    response={204: None},
    summary="Trash a DICOM study (soft delete).",
)
def delete_study(request, content_hash: str):
    """Soft-delete the study; the purge task removes rows and files later.

    Requires write access (author, superuser, or a can_write grant). The
    study disappears from every read surface immediately; after
    ``DICOM_TRASH_RETENTION_DAYS`` the scheduled purge hard-deletes the rows
    and unlinks the stored files.
    """
    user = _require_auth(request)
    study = _get_study_or_404(content_hash, user)
    ensure_can_write_object(user, study)

    with transaction.atomic():
        log_activity(verb="dicom.study.trash", target=study)
        study.deleted_at = timezone.now()
        study.save(update_fields=["deleted_at", "modified_at"])

    return 204, None


def _study_ct() -> ContentType:
    return ContentType.objects.get_for_model(DicomStudy, for_concrete_model=False)


@api.get("/dicom/studies/{content_hash}/access/", response=list[AccessRightOut])
def list_study_access_rights(request, content_hash: str):
    """List the access rights on a study other than its author's own. Requires the right to manage access."""
    user = _require_auth(request)
    study = _get_study_or_404(content_hash, user)
    ensure_can_manage_access(user, study, object_label="study", action="manage access for")
    rights = list(
        AccessRight.objects.filter(content_type=_study_ct(), object_id=str(study.pk))
        .exclude(access_target_id=study.author_id)
        .select_related("access_target", "access_target_group")
        .order_by("id")
    )
    # A grant a registered workflow holds is that workflow's to show and revoke.
    held = held_grant_ids(rights)
    rights = [right for right in rights if right.pk not in held]
    log_activity(verb="dicom.study.access.list", target=study, metadata={"returned_count": len(rights)})
    return [access_right_out(r, assessment_visible=assessment_visible(r, user, study)) for r in rights]


@api.post("/dicom/studies/{content_hash}/access/", response={201: AccessRightOut})
def grant_study_access(request, content_hash: str, payload: GrantStudyAccessIn):
    """Grant a user an access right on a study.

    Requires the author, a superuser or an active ``can_share`` grant, and a
    delegated grant may confer only rights the grantor holds (see
    ``epicurrents.granting``). A target that already holds a right on the
    study answers 409; revoke it first.
    """
    user = _require_auth(request)
    study = _get_study_or_404(content_hash, user)
    rights = ensure_can_manage_access(user, study, object_label="study", action="share")
    refusal = share_refusal(study)
    if refusal:
        raise HttpError(409, refusal)
    if not (payload.can_read or payload.can_write or payload.can_share):
        raise HttpError(400, "At least one permission flag must be true")
    ensure_can_confer(
        request,
        user,
        study,
        rights,
        can_read=payload.can_read,
        can_write=payload.can_write,
        can_share=payload.can_share,
        apply_middleware=False,
        expires_at=payload.expires_at,
    )
    if not get_user_model().objects.filter(pk=payload.access_target_id).exists():
        raise HttpError(400, f"User {payload.access_target_id} not found")

    conflict = "This user already has an access right on the study. Revoke it first."
    try:
        with transaction.atomic():
            if AccessRight.objects.filter(
                content_type=_study_ct(), object_id=str(study.pk), access_target_id=payload.access_target_id
            ).exists():
                raise HttpError(409, conflict)
            right = AccessRight.objects.create(
                content_type=_study_ct(),
                object_id=str(study.pk),
                access_giver=user,
                access_target_id=payload.access_target_id,
                can_read=payload.can_read,
                can_write=payload.can_write,
                can_share=payload.can_share,
                apply_middleware=False,
                expires_at=payload.expires_at,
            )
            log_activity(verb="dicom.study.access.grant", target=right)
    except IntegrityError as exc:
        # Race backstop: two concurrent grants for one target both pass the
        # pre-check, and the per-target uniqueness constraint rejects the loser.
        raise HttpError(409, conflict) from exc
    right = AccessRight.objects.select_related("access_target", "access_target_group").get(pk=right.pk)
    return 201, access_right_out(right, assessment_visible=True)


@api.delete("/dicom/studies/{content_hash}/access/{right_id}/")
def revoke_study_access(request, content_hash: str, right_id: int):
    """Revoke one access right on a study.

    Requires the author, a superuser or an active ``can_share`` grant; the
    author's own row is revocable only by the author or a superuser.
    """
    user = _require_auth(request)
    study = _get_study_or_404(content_hash, user)
    rights = ensure_can_manage_access(user, study, object_label="study", action="manage access for")
    right = AccessRight.objects.filter(pk=right_id, content_type=_study_ct(), object_id=str(study.pk)).first()
    if right is None or held_grant_ids([right]):
        raise HttpError(404, "Access right not found")
    ensure_can_revoke(request, user, study, right, rights)
    with transaction.atomic():
        log_activity(verb="dicom.study.access.revoke", target=right)
        right.delete()
    return {"status": "ok"}


@api.get(
    "/dicom/wado/",
    summary="WADO-URI: stream a single DICOM instance file.",
)
def wado_uri(
    request,
    objectUID: str = Query(..., description="SOPInstanceUID of the instance to retrieve."),
    studyUID: str = Query("", description="StudyInstanceUID; narrows the lookup."),
    seriesUID: str = Query("", description="SeriesInstanceUID; narrows the lookup."),
    requestType: str = Query("WADO", description="Must be WADO."),
):
    """Return the raw DICOM file for the given SOP instance.

    OHIF fetches instances via this endpoint using the URLs embedded in the
    ``/ohif-json/`` response. The ``Content-Type`` is ``application/dicom``.

    SOP UIDs are unique per series, not globally — per-author study copies
    repeat them — so the lookup collects every matching READY instance and
    picks the caller's own copy first, then the first copy the caller can
    read. Missing instances and access denials both return 404.

    Cross-origin isolation headers (COOP/COEP/CORP) needed by OHIF's WASM
    decoders are set platform-wide by
    ``epicurrents.middleware.CrossOriginIsolationMiddleware`` when
    ``ENABLE_CROSS_ORIGIN_ISOLATION`` is true.
    """
    user = _require_auth(request)

    qs = DicomInstance.objects.select_related("series__study").filter(
        sop_instance_uid=objectUID,
        status=DicomInstance.Status.READY,
        series__study__deleted_at__isnull=True,
    )
    if studyUID:
        qs = qs.filter(series__study__study_instance_uid=studyUID)
    if seriesUID:
        qs = qs.filter(series__series_instance_uid=seriesUID)
    candidates = list(qs.order_by("pk"))

    inst = next((i for i in candidates if i.series.study.author_id == user.pk), None)
    if inst is None:
        inst = next(
            (i for i in candidates if user.is_superuser or can_read_object(user, i.series.study)),
            None,
        )
    if inst is None:
        raise HttpError(404, "Instance not found.")

    upload = _upload_path()
    file_path = os.path.join(upload, inst.stored_name)
    if not os.path.isfile(file_path):
        logger.error(
            "DICOM file missing on disk: stored_name=%s instance_pk=%s",
            inst.stored_name,
            inst.pk,
        )
        raise HttpError(500, "Instance file not found on disk.")

    log_activity(
        verb="dicom.instance.download",
        target=inst,
        metadata={
            "sop_class_uid": inst.sop_class_uid,
            "frames": inst.number_of_frames,
        },
    )
    return FileResponse(
        open(file_path, "rb"),
        content_type="application/dicom",
        filename=inst.stored_name,
    )


# ---------------------------------------------------------------------------
# DICOMweb (QIDO-RS search, WADO-RS metadata) — see plugins/dicom/dicomweb.py
# ---------------------------------------------------------------------------

DICOM_JSON = "application/dicom+json"


def _dicom_json_response(body) -> HttpResponse:
    return HttpResponse(json.dumps(body), content_type=DICOM_JSON)


def _resolve_study_or_404(user, study_uid: str) -> DicomStudy:
    study = dicomweb.resolve_study(user, study_uid)
    if study is None:
        raise HttpError(404, "Study not found.")
    return study


def _series_or_404(study: DicomStudy, series_uid: str):
    series = study.series.filter(series_instance_uid=series_uid).first()
    if series is None:
        raise HttpError(404, "Series not found.")
    return series


def _paging(request) -> tuple[int, int]:
    """Read QIDO ``offset`` / ``limit``; an unreadable value falls back to the default."""

    def read(name, default):
        try:
            return max(0, int(request.GET.get(name, default)))
        except ValueError:
            return default

    return read("offset", 0), read("limit", 0)


@api.get("/dicomweb/studies", summary="QIDO-RS: search the studies the caller may list.")
def qido_studies(request):
    """Return one QIDO-RS study object per study the caller's study listing shows.

    Copies of one study held by different authors collapse to the copy a
    retrieval by that UID opens. Matching follows ``dicomweb.search_studies``;
    ``limit`` of 0 or absent means no limit.
    """
    user = _require_auth(request)
    studies = dicomweb.search_studies(user, request.GET)
    offset, limit = _paging(request)
    page = studies[offset : offset + limit] if limit else studies[offset:]
    # Filter names only: a value is whatever the caller typed, a patient's name included.
    filters = sorted(k for k in dicomweb.STUDY_FILTERS if request.GET.get(k))
    log_activity(verb="dicom.study.list.qido", metadata={"count": len(page), "filters": filters})
    return _dicom_json_response(dicomweb.studies_json(page))


@api.get("/dicomweb/studies/{study_uid}/series", summary="QIDO-RS: the series of one study.")
def qido_series(request, study_uid: str):
    """Return the QIDO-RS series objects of the study *study_uid* resolves to for the caller."""
    user = _require_auth(request)
    study = _resolve_study_or_404(user, study_uid)
    series = dicomweb.series_with_counts(study)
    log_activity(verb="dicom.series.list.qido", target=study, metadata={"count": len(series)})
    return _dicom_json_response([dicomweb.series_json(s, study) for s in series])


@api.get("/dicomweb/studies/{study_uid}/metadata", summary="WADO-RS: instance metadata of a whole study.")
def wado_study_metadata(request, study_uid: str):
    """Return WADO-RS metadata for every ready instance of the study *study_uid* resolves to."""
    user = _require_auth(request)
    study = _resolve_study_or_404(user, study_uid)
    body = dicomweb.study_metadata(study)
    log_activity(verb="dicom.study.read.metadata", target=study, metadata={"instances": len(body)})
    return _dicom_json_response(body)


@api.get(
    "/dicomweb/studies/{study_uid}/series/{series_uid}/metadata",
    summary="WADO-RS: instance metadata of one series.",
)
def wado_series_metadata(request, study_uid: str, series_uid: str):
    """Return WADO-RS metadata for every ready instance of one series of the resolved study."""
    user = _require_auth(request)
    study = _resolve_study_or_404(user, study_uid)
    series = _series_or_404(study, series_uid)
    body = dicomweb.series_metadata(series, study)
    log_activity(verb="dicom.series.read.metadata", target=series, metadata={"instances": len(body)})
    return _dicom_json_response(body)


# The platform mounts each enabled plugin with ``include("plugins.<name>.urls")``
# at ``/plugin/<name>/api/v1/``, so this module must expose ``urlpatterns``.
from django.urls import path

urlpatterns = [path("", api.urls)]
