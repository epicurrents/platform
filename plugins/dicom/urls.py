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
POST   /dicom/studies/{hash}/share/            Grant read access to another user.
DELETE /dicom/studies/{hash}/share/{username}/ Revoke a user's access.

Upload parses every file's header synchronously (pydicom, header only) and
creates the real study/series/instance rows before responding, so the returned
study hashes are immediately valid for the other endpoints. See
``plugins/dicom/ingest.py`` for the shared parse/persist logic.
"""

import hashlib
import logging
import os
import shutil
import uuid
from datetime import datetime

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.db.models import Q
from django.http import FileResponse
from django.utils import timezone
from ninja import File, NinjaAPI, Query, Schema, UploadedFile
from ninja.errors import HttpError

from activity.audit import log_activity
from epicurrents.auth import enforce_session_csrf
from epicurrents.models import AccessRight
from epicurrents.permissions import (
    can_modify_object,
    can_read_object,
    ensure_can_write_object,
)
from plugins.dicom.ingest import (
    MissingUidsError,
    parse_dicom_header,
    persist_instance,
    refresh_study_aggregates,
    required_uids,
)
from plugins.dicom.models import DicomInstance, DicomStudy

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

    filename: str
    accepted: bool
    study_hash: str | None = None
    error: str | None = None


class DicomUploadOut(Schema):
    """Response after a batch upload — real study hashes, per-file report."""

    studies: list[DicomUploadStudyOut]
    files: list[DicomUploadFileOut]
    accepted: int
    rejected: int


class ShareIn(Schema):
    """Payload for granting read access to a DICOM study."""

    username: str


class ShareOut(Schema):
    """Confirmation of a granted read access."""

    username: str
    can_read: bool


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


def _study_out(study: DicomStudy, user) -> DicomStudyOut:
    """Serialize a study list item for *user*."""
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
        created_at=study.created_at,
        is_author=study.author_id == user.pk,
    )


def _can_share_study(user, study: DicomStudy) -> bool:
    """Return True when *user* may manage access grants on *study*."""
    if can_modify_object(user=user, obj=study):
        return True
    study_ct = ContentType.objects.get_for_model(study, for_concrete_model=False)
    return (
        AccessRight.objects.active()
        .filter(
            content_type=study_ct,
            object_id=str(study.pk),
            can_share=True,
        )
        .filter(Q(access_target=user) | Q(access_target_group_id__in=list(user.groups.values_list("id", flat=True))))
        .exists()
    )


def _resolve_attachment(user, attached_to_type: str | None, attached_to_id: str | None):
    """Resolve the optional upload attachment target.

    Returns ``None`` when no attachment was requested. Today only
    ``type="recording"`` is supported; the target is resolved by its
    ``content_hash`` and requires write access, so a user cannot attach
    studies to someone else's recording.
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

    try:
        recording = Recording.objects.get(content_hash=attached_to_id, deleted_at__isnull=True)
    except Recording.DoesNotExist:
        raise HttpError(404, "Attachment target not found.")
    ensure_can_write_object(user, recording)
    return recording


def _build_ohif_json(study: DicomStudy, request) -> dict:
    """Build the DICOMweb JSON payload consumed by OHIF's ``dicomjson`` datasource.

    The instance URL uses the WADO-URI endpoint mounted at
    ``/plugin/dicom/api/v1/dicom/wado/``. OHIF recognises the ``dicomweb:``
    prefix and strips it before making its own fetch request.
    """
    scheme = request.scheme
    host = request.get_host()
    wado_base = f"{scheme}://{host}/plugin/dicom/api/v1/dicom/wado/"

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

    for series in study.series.prefetch_related("instances").all():
        series_node: dict = {
            "SeriesInstanceUID": series.series_instance_uid,
            "SeriesDescription": series.series_description,
            "SeriesNumber": series.series_number,
            "Modality": series.modality,
            "SliceThickness": series.slice_thickness,
            "instances": [],
        }

        for inst in series.instances.filter(status=DicomInstance.Status.READY):
            wado_url = (
                f"{wado_base}?requestType=WADO"
                f"&studyUID={study.study_instance_uid}"
                f"&seriesUID={series.series_instance_uid}"
                f"&objectUID={inst.sop_instance_uid}"
            )
            frames = inst.number_of_frames or 0
            if frames > 1:
                for i in range(1, frames + 1):
                    series_node["instances"].append(
                        {
                            "metadata": _instance_metadata(inst, series, study),
                            "url": f"dicomweb:{wado_url}&frameNumber={i}",
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


def _instance_metadata(
    inst: DicomInstance,
    series,
    study: DicomStudy,
) -> dict:
    return {
        "Columns": inst.columns,
        "Rows": inst.rows,
        "InstanceNumber": inst.instance_number,
        "SOPClassUID": inst.sop_class_uid,
        "PhotometricInterpretation": inst.photometric_interpretation,
        "BitsAllocated": inst.bits_allocated,
        "BitsStored": inst.bits_stored,
        "PixelRepresentation": inst.pixel_representation,
        "SamplesPerPixel": inst.samples_per_pixel,
        "PixelSpacing": inst.pixel_spacing,
        "HighBit": inst.high_bit,
        "ImageOrientationPatient": inst.image_orientation_patient,
        "ImagePositionPatient": inst.image_position_patient,
        "FrameOfReferenceUID": inst.frame_of_reference_uid,
        "ImageType": inst.image_type,
        "Modality": series.modality,
        "SOPInstanceUID": inst.sop_instance_uid,
        "SeriesInstanceUID": series.series_instance_uid,
        "StudyInstanceUID": study.study_instance_uid,
        "WindowCenter": inst.window_center,
        "WindowWidth": inst.window_width,
        "SeriesDate": series.series_date,
        "NumberOfFrames": inst.number_of_frames,
    }


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
):
    """Accept a batch of DICOM files, parse their headers, and persist them.

    Each file is streamed to ``DICOM_STAGING_PATH`` (hashing as it lands),
    header-parsed with pydicom, and — when valid — persisted as real
    study/series/instance rows in one transaction. After the transaction
    commits, files move to ``DICOM_UPLOAD_PATH`` and their instances flip to
    ``ready``. Unparseable files, files missing required UIDs, oversized
    files, and duplicates of already-ready instances are rejected per file
    and reported in the ``files`` list; they never abort the rest of the
    batch.

    ``attached_to_type`` + ``attached_to_id`` optionally attach the resulting
    study to a parent object (today only ``type="recording"``, identified by
    its content hash; requires write access to the recording). Attachment is
    rejected when the batch spans more than one study.

    The returned study hashes are immediately valid for the study endpoints;
    ``num_instances`` reflects the batch as soon as the response is returned.
    """
    user = _require_auth(request)

    max_files = getattr(settings, "DICOM_MAX_UPLOAD_FILES", 500)
    if len(files) > max_files:
        raise HttpError(400, f"Too many files in one request (max {max_files}).")

    attach_target = _resolve_attachment(user, attached_to_type, attached_to_id)

    staging = _staging_path()
    upload = _upload_path()
    max_size = getattr(settings, "DICOM_MAX_UPLOAD_FILE_SIZE", 2 * 1024**3)

    file_reports: list[DicomUploadFileOut] = []
    # Entries that passed staging + parsing and await the DB transaction:
    # (original filename, staging path, stored name, size, sha256, dataset).
    staged: list[tuple] = []

    for upload_file in files:
        original_name = upload_file.name or "upload.dcm"
        stored_name = f"{uuid.uuid4().hex}.dcm"
        staging_file = os.path.join(staging, stored_name)

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
                    filename=original_name,
                    accepted=False,
                    error="Failed to store the uploaded file.",
                )
            )
            continue

        if oversized:
            _unlink_quietly(staging_file)
            file_reports.append(
                DicomUploadFileOut(
                    filename=original_name,
                    accepted=False,
                    error=f"File exceeds maximum upload size ({max_size} bytes).",
                )
            )
            continue

        try:
            ds = parse_dicom_header(staging_file)
            required_uids(ds)
        except MissingUidsError as exc:
            _unlink_quietly(staging_file)
            file_reports.append(
                DicomUploadFileOut(
                    filename=original_name,
                    accepted=False,
                    error=str(exc),
                )
            )
            continue
        except Exception:
            _unlink_quietly(staging_file)
            file_reports.append(
                DicomUploadFileOut(
                    filename=original_name,
                    accepted=False,
                    error="Not a parseable DICOM file.",
                )
            )
            continue

        staged.append((original_name, staging_file, stored_name, total_size, hasher.hexdigest(), ds))

    # Persist all parsed files in one transaction (multi-step write rule).
    persisted: list[tuple] = []  # (original_name, staging_file, PersistResult)
    studies: dict[int, DicomStudy] = {}
    instances_added: dict[int, int] = {}
    try:
        with transaction.atomic():
            for original_name, staging_file, stored_name, size, sha, ds in staged:
                result = persist_instance(
                    author=user,
                    ds=ds,
                    stored_name=stored_name,
                    file_size=size,
                    file_hash=sha,
                    status=DicomInstance.Status.PENDING,
                )
                if result.outcome == "duplicate":
                    _unlink_quietly(staging_file)
                    file_reports.append(
                        DicomUploadFileOut(
                            filename=original_name,
                            accepted=False,
                            study_hash=result.study.content_hash,
                            error="Duplicate SOPInstanceUID in this study.",
                        )
                    )
                    continue
                studies[result.study.pk] = result.study
                instances_added[result.study.pk] = instances_added.get(result.study.pk, 0) + 1
                persisted.append((original_name, staging_file, result))

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
    except HttpError:
        # The transaction rolled back — remove every file still in staging.
        for _, staging_file, *_rest in staged:
            _unlink_quietly(staging_file)
        raise

    # Post-commit: move accepted files to final storage and flip to READY.
    accepted = 0
    for original_name, staging_file, result in persisted:
        inst = result.instance
        final_path = os.path.join(upload, inst.stored_name)
        try:
            shutil.move(staging_file, final_path)
        except OSError as exc:
            logger.error("Failed to move DICOM file %s to storage: %s", inst.stored_name, exc)
            inst.status = DicomInstance.Status.FAILED
            inst.error_message = "Failed to move the file to permanent storage."
            inst.save(update_fields=["status", "error_message", "modified_at"])
            _unlink_quietly(staging_file)
            file_reports.append(
                DicomUploadFileOut(
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
        file_reports.append(
            DicomUploadFileOut(
                filename=original_name,
                accepted=True,
                study_hash=result.study.content_hash,
            )
        )

    for study in studies.values():
        refresh_study_aggregates(study)

    if accepted == 0:
        raise HttpError(400, "No files were accepted. See the per-file report in logs.")

    single_study = next(iter(studies.values())) if len(studies) == 1 else None
    log_activity(
        verb="dicom.study.upload",
        target=single_study,
        metadata={
            "study_count": len(studies),
            "accepted": accepted,
            "rejected": len(file_reports) - accepted,
        },
    )

    return DicomUploadOut(
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

    if user.is_superuser:
        qs = DicomStudy.objects.filter(deleted_at__isnull=True)
    else:
        ct = ContentType.objects.get_for_model(DicomStudy)
        # Materialise into a set rather than leaving it a subquery: object_id is
        # a CharField, and Postgres rejects bigint pk IN (varchar subquery) with
        # "operator does not exist: bigint = character varying". As a set of
        # literals Django coerces each value to the pk type.
        granted_ids = set(
            AccessRight.objects.active()
            .filter(content_type=ct, can_read=True, access_target=user)
            .values_list("object_id", flat=True)
        )
        qs = DicomStudy.objects.filter(deleted_at__isnull=True).filter(Q(author=user) | Q(pk__in=granted_ids))

    result = [_study_out(s, user) for s in qs.order_by("-created_at")]
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
            instance_count=s.instances.filter(status=DicomInstance.Status.READY).count(),
        )
        for s in study.series.all()
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
    return _build_ohif_json(study, request)


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


@api.post(
    "/dicom/studies/{content_hash}/share/",
    response={201: ShareOut},
    summary="Grant another user read access to a study.",
)
def share_study(request, content_hash: str, payload: ShareIn):
    """Create a can_read ``AccessRight`` on the study for *username*.

    Requires authorship, superuser status, or a can_share grant on the study.
    """
    from django.contrib.auth import get_user_model

    user = _require_auth(request)
    study = _get_study_or_404(content_hash, user)
    if not _can_share_study(user, study):
        raise HttpError(403, "You do not have permission to share this study.")

    username = (payload.username or "").strip()
    UserModel = get_user_model()
    try:
        target = UserModel.objects.get(username=username)
    except UserModel.DoesNotExist:
        raise HttpError(400, f"User {username!r} not found.")
    if target.pk == user.pk:
        raise HttpError(400, "You cannot share a study with yourself.")

    study_ct = ContentType.objects.get_for_model(study, for_concrete_model=False)
    with transaction.atomic():
        existing = (
            AccessRight.objects.active()
            .filter(
                content_type=study_ct,
                object_id=str(study.pk),
                access_target=target,
                can_read=True,
            )
            .exists()
        )
        if existing:
            raise HttpError(409, f"User {username!r} already has read access.")
        right = AccessRight.objects.create(
            content_type=study_ct,
            object_id=str(study.pk),
            access_giver=user,
            access_target=target,
            can_read=True,
        )
        log_activity(verb="dicom.study.access.grant", target=right)

    return 201, ShareOut(username=target.username, can_read=True)


@api.delete(
    "/dicom/studies/{content_hash}/share/{username}/",
    response={204: None},
    summary="Revoke a user's access to a study.",
)
def revoke_study_access(request, content_hash: str, username: str):
    """Delete every ``AccessRight`` the named user holds on the study.

    Requires authorship, superuser status, or a can_share grant.
    """
    user = _require_auth(request)
    study = _get_study_or_404(content_hash, user)
    if not _can_share_study(user, study):
        raise HttpError(403, "You do not have permission to manage access to this study.")

    study_ct = ContentType.objects.get_for_model(study, for_concrete_model=False)
    rights = list(
        AccessRight.objects.filter(
            content_type=study_ct,
            object_id=str(study.pk),
            access_target__username=username,
        )
    )
    if not rights:
        raise HttpError(404, "No access rights found for that user.")

    with transaction.atomic():
        for right in rights:
            log_activity(verb="dicom.study.access.revoke", target=right)
            right.delete()

    return 204, None


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


# The platform mounts each enabled plugin with ``include("plugins.<name>.urls")``
# at ``/plugin/<name>/api/v1/``, so this module must expose ``urlpatterns``.
from django.urls import path

urlpatterns = [path("", api.urls)]
