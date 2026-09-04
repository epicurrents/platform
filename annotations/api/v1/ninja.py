"""Annotations API — CRUD endpoints for all annotation types and codes.

Endpoints (mounted at ``/annotations/api/v1/``)
------------------------------------------------
GET/POST      /annotations/           List or create Annotation (bundle) objects.
GET           /annotations/mine       Caller's annotations across all targets.
PATCH/DELETE  /annotations/{hash}     Update or delete a specific annotation.

GET/POST      /events/                List or create Event objects.
GET           /events/mine            Caller's events across all targets.
PATCH/DELETE  /events/{hash}          Update or delete a specific event.

GET/POST      /interruptions/         List or create Interruption objects.
GET           /interruptions/mine     Caller's interruptions across all targets.
PATCH/DELETE  /interruptions/{hash}   Update or delete a specific interruption.

GET/POST      /labels/                List or create Label objects.
GET           /labels/mine            Caller's labels across all targets.
PATCH/DELETE  /labels/{hash}          Update or delete a specific label.

POST          /codes/                 Attach a Code to an Event, Interruption, or Label.
PATCH/DELETE  /codes/{id}             Update or delete a specific code.

GET           /export                 Bulk export of events / labels as JSON or CSV.
GET           /export/annotators      Staff-only annotator roster (id-to-identity mapping).

GET           /content-types          List available content types (for target lookup).
GET           /health                 Health check.

All write operations require auth and write access to the annotation's target object.
List operations require auth and read access to the target object.
"""

import json

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.http import HttpResponse
from django.utils import timezone
from django.utils.http import content_disposition_header
from ninja import NinjaAPI, Query, Router, Schema
from ninja.errors import HttpError

from activity.audit import log_activity
from annotations import export as annotation_export
from annotations.models import Annotation, Code, Event, Interruption, Label
from annotations.vocabularies import validate_code
from epicurrents.auth import enforce_session_csrf
from epicurrents.permissions import (
    can_annotate_object,
    can_modify_object,
    can_read_object,
)
from epicurrents.security_log import get_client_ip, log_security_event

api = NinjaAPI(
    title="Annotations API",
    version="1",
    urls_namespace="annotations-api-v1",
    docs_url=settings.API_DOCS_URL,
    openapi_url=settings.API_OPENAPI_URL,
)

annotations_router = Router()
events_router = Router()
interruptions_router = Router()
labels_router = Router()
codes_router = Router()
export_router = Router()

# ── Shared helpers ──────────────────────────────────────────────────────────


def _require_auth(request):
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated:
        raise HttpError(401, "Authentication credentials were not provided")
    enforce_session_csrf(request)
    return user


def _resolve_by_hash(model, object_hash: str, user, *, prefetch=()):
    """Resolve a CRUD-by-hash lookup deterministically for the caller.

    ``object_hash`` is unique only per (target, concrete model), so the same
    hash can legitimately exist on several rows authored by different users.
    Preferring the caller's own row keeps another author from pre-claiming a
    hash value on their own target and capturing this caller's PATCH/DELETE
    resolution — a lockout, not a write; ``can_modify_object`` still gates
    the mutation either way.
    """
    qs = model.objects.filter(object_hash=object_hash)
    if prefetch:
        qs = qs.prefetch_related(*prefetch)
    return qs.filter(author=user).first() or qs.first()


def _resolve_target_or_404(content_type_id: int, object_id: str):
    content_type = ContentType.objects.filter(id=content_type_id).first()
    if content_type is None:
        raise HttpError(404, "Target content type not found")

    model_class = content_type.model_class()
    if model_class is None:
        raise HttpError(404, "Target model is not available")

    target = model_class.objects.filter(pk=object_id).first()
    if target is None:
        raise HttpError(404, "Target object not found")

    return content_type, target


def _list_for_target(
    request,
    model_class,
    content_type_id: int,
    object_id: str,
    author_id,
    limit: int,
    offset: int,
):
    """Return ``(rows, target)`` for the requested parent and annotation type.

    The caller uses ``target`` to populate ``log_activity(target=...)`` since
    the parent — not the annotation list — is the audit-trail subject.
    """
    user = _require_auth(request)
    content_type, target = _resolve_target_or_404(content_type_id, object_id)

    if not can_read_object(user=user, obj=target):
        raise HttpError(403, "You do not have permission to view this object")

    queryset = model_class.objects.filter(
        target_content_type=content_type,
        target_object_id=str(object_id),
    )
    if author_id is not None:
        queryset = queryset.filter(author_id=author_id)

    if hasattr(model_class, "codes"):
        queryset = queryset.prefetch_related("codes")

    rows = list(queryset.order_by("-created_at")[offset : offset + limit])
    return rows, target


def _resolve_codeable_parent_or_400(content_type_id: int, object_id: str):
    content_type = ContentType.objects.filter(id=content_type_id).first()
    if content_type is None:
        raise HttpError(400, "Invalid content type")

    model_class = content_type.model_class()
    if model_class not in (Event, Interruption, Label):
        raise HttpError(400, "Codes can only be attached to Event, Interruption, or Label")

    parent = model_class.objects.filter(pk=object_id).first()
    if parent is None:
        raise HttpError(404, "Parent annotation not found")

    return parent


# ── Serialisers ─────────────────────────────────────────────────────────────


def _serialize_code(code: Code) -> dict:
    return {
        "id": code.pk,
        "content_type_id": code.content_type_id,
        "object_id": code.object_id,
        "standard": code.standard,
        "value": code.value,
        "meta": code.meta,
    }


def _serialize_base(obj) -> dict:
    return {
        "author_id": obj.author_id,
        "target_content_type_id": obj.target_content_type_id,
        "target_object_id": obj.target_object_id,
        "object_hash": obj.object_hash,
        "content_hash": obj.content_hash,
    }


def _serialize_annotation(annotation: Annotation) -> dict:
    base = _serialize_base(annotation)
    content = annotation.content
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except (json.JSONDecodeError, ValueError):
            return {**base, "value": "Invalid annotation content"}
    if isinstance(content, dict):
        return {**base, **content}
    return {**base, "value": content}


def _serialize_event(event: Event) -> dict:
    return {
        **_serialize_base(event),
        "name": event.name,
        "timestamp": event.timestamp,
        "duration": event.duration,
        "value": event.value,
        "codes": [_serialize_code(c) for c in event.codes.all()],
    }


def _serialize_interruption(interruption: Interruption) -> dict:
    return {
        **_serialize_base(interruption),
        "timestamp": interruption.timestamp,
        "duration": interruption.duration,
        "codes": [_serialize_code(c) for c in interruption.codes.all()],
    }


def _serialize_label(label: Label) -> dict:
    return {
        **_serialize_base(label),
        "name": label.name,
        "value": label.value,
        "codes": [_serialize_code(c) for c in label.codes.all()],
    }


# ── Schemas ─────────────────────────────────────────────────────────────────


class ContentTypeOut(Schema):
    id: int
    app_label: str
    model: str
    natural_key: str


class AnnotationIn(Schema):
    target_content_type_id: int
    target_object_id: str
    object_hash: str
    content: dict | list | str | int | float
    # Identifies the person making the annotation when accessed via a share
    # token.  Ignored for authenticated sessions (attribution uses the session
    # user instead).  Required — and enforced — when share_token auth is used.
    annotator: str | None = None


class AnnotationPatch(Schema):
    object_hash: str | None = None
    content: dict | list | str | int | float | None = None


class EventIn(Schema):
    target_content_type_id: int
    target_object_id: str
    object_hash: str
    name: str
    timestamp: float
    duration: float | None = None
    value: dict | list | str | int | float | None = None
    annotator: str | None = None  # see AnnotationIn.annotator


class EventPatch(Schema):
    object_hash: str | None = None
    name: str | None = None
    timestamp: float | None = None
    duration: float | None = None
    value: dict | list | str | int | float | None = None


class InterruptionIn(Schema):
    target_content_type_id: int
    target_object_id: str
    object_hash: str
    timestamp: float
    duration: float
    annotator: str | None = None  # see AnnotationIn.annotator


class InterruptionPatch(Schema):
    object_hash: str | None = None
    timestamp: float | None = None
    duration: float | None = None


class LabelIn(Schema):
    target_content_type_id: int
    target_object_id: str
    object_hash: str
    name: str
    value: dict | list | str | int | float | None = None
    annotator: str | None = None  # see AnnotationIn.annotator


class LabelPatch(Schema):
    object_hash: str | None = None
    name: str | None = None
    value: dict | list | str | int | float | None = None


class CodeIn(Schema):
    content_type_id: int
    object_id: str
    standard: str
    value: str
    meta: dict | list | str | int | float | None = None  # mirrors Code.meta, a JSONField


class CodePatch(Schema):
    standard: str | None = None
    value: str | None = None
    meta: dict | list | str | int | float | None = None  # explicit null clears; absent leaves unchanged


# ── Top-level endpoints ──────────────────────────────────────────────────────


@api.get("/health")
def healthcheck(request):
    return {"status": "ok"}


@api.get("/content-types", response=list[ContentTypeOut])
def list_content_types(
    request,
    app_label: str | None = None,
    model: str | None = None,
    limit: int = Query(500, ge=1, le=1000),
):
    _require_auth(request)
    queryset = ContentType.objects.all().order_by("app_label", "model")

    if app_label:
        queryset = queryset.filter(app_label=app_label.strip().lower())
    if model:
        queryset = queryset.filter(model=model.strip().lower())

    rows = list(queryset[:limit])
    return [
        {
            "id": row.id,
            "app_label": row.app_label,
            "model": row.model,
            "natural_key": f"{row.app_label}.{row.model}",
        }
        for row in rows
    ]


# ── Annotation (bundle) endpoints ────────────────────────────────────────────


@annotations_router.get("/", auth=None)
def list_annotations(
    request,
    target_content_type_id: int,
    target_object_id: str,
    author_id: int | None = None,
    limit: int = Query(500, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    rows, target = _list_for_target(
        request,
        Annotation,
        target_content_type_id,
        target_object_id,
        author_id,
        limit,
        offset,
    )
    log_activity(
        verb="annotations.annotation.list",
        target=target,
        metadata={
            "author_id_filter": author_id,
            "limit": limit,
            "offset": offset,
            "returned_count": len(rows),
        },
    )
    return [_serialize_annotation(row) for row in rows]


@annotations_router.get("/mine", auth=None)
def list_my_annotations(
    request,
    limit: int = Query(500, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    user = _require_auth(request)
    rows = list(Annotation.objects.filter(author=user).order_by("-created_at")[offset : offset + limit])
    log_activity(
        verb="annotations.annotation.mine",
        metadata={"limit": limit, "offset": offset, "returned_count": len(rows)},
    )
    return [_serialize_annotation(row) for row in rows]


@annotations_router.post("/", auth=None)
def create_annotation(request, payload: AnnotationIn):
    user = _require_auth(request)
    content_type, target = _resolve_target_or_404(payload.target_content_type_id, payload.target_object_id)

    if not can_annotate_object(user=user, obj=target, annotator=payload.annotator):
        raise HttpError(403, "You do not have permission to annotate this object")

    annotation = Annotation(
        author=user,
        target_content_type=content_type,
        target_object_id=str(payload.target_object_id),
        object_hash=payload.object_hash,
        content=payload.content,
    )
    with transaction.atomic():
        annotation.full_clean()
        annotation.save()
        log_activity(verb="annotations.annotation.create", target=annotation)
    return _serialize_annotation(annotation)


@annotations_router.patch("/{object_hash}", auth=None)
def update_annotation(request, object_hash: str, payload: AnnotationPatch):
    user = _require_auth(request)
    annotation = _resolve_by_hash(Annotation, object_hash, user)
    if annotation is None:
        raise HttpError(404, "Annotation not found")

    target = annotation.target_object
    if target is None:
        raise HttpError(400, "Target object no longer exists")

    if not can_modify_object(user=user, obj=annotation):
        raise HttpError(403, "You do not have permission to modify this annotation")

    patch = payload.model_dump(exclude_unset=True)
    for field_name, field_value in patch.items():
        setattr(annotation, field_name, field_value)

    with transaction.atomic():
        annotation.full_clean()
        annotation.save()
        log_activity(
            verb="annotations.annotation.update",
            target=annotation,
            metadata={"fields_updated": sorted(patch.keys())},
        )
    return _serialize_annotation(annotation)


@annotations_router.delete("/{object_hash}", auth=None)
def delete_annotation(request, object_hash: str):
    user = _require_auth(request)
    annotation = _resolve_by_hash(Annotation, object_hash, user)
    if annotation is None:
        raise HttpError(404, "Annotation not found")

    if not can_modify_object(user=user, obj=annotation):
        raise HttpError(403, "You do not have permission to delete this annotation")

    response = {"status": "deleted", "object_hash": annotation.object_hash}
    with transaction.atomic():
        log_activity(verb="annotations.annotation.delete", target=annotation)
        annotation.delete()
    return response


# ── Event endpoints ──────────────────────────────────────────────────────────


@events_router.get("/", auth=None)
def list_events(
    request,
    target_content_type_id: int,
    target_object_id: str,
    author_id: int | None = None,
    limit: int = Query(500, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    rows, target = _list_for_target(
        request,
        Event,
        target_content_type_id,
        target_object_id,
        author_id,
        limit,
        offset,
    )
    log_activity(
        verb="annotations.event.list",
        target=target,
        metadata={
            "author_id_filter": author_id,
            "limit": limit,
            "offset": offset,
            "returned_count": len(rows),
        },
    )
    return [_serialize_event(row) for row in rows]


@events_router.get("/mine", auth=None)
def list_my_events(
    request,
    limit: int = Query(500, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    user = _require_auth(request)
    rows = list(
        Event.objects.filter(author=user).prefetch_related("codes").order_by("-created_at")[offset : offset + limit]
    )
    log_activity(
        verb="annotations.event.mine",
        metadata={"limit": limit, "offset": offset, "returned_count": len(rows)},
    )
    return [_serialize_event(row) for row in rows]


@events_router.post("/", auth=None)
def create_event(request, payload: EventIn):
    user = _require_auth(request)
    content_type, target = _resolve_target_or_404(payload.target_content_type_id, payload.target_object_id)

    if not can_annotate_object(user=user, obj=target, annotator=payload.annotator):
        raise HttpError(403, "You do not have permission to annotate this object")

    event = Event(
        author=user,
        target_content_type=content_type,
        target_object_id=str(payload.target_object_id),
        object_hash=payload.object_hash,
        name=payload.name,
        timestamp=payload.timestamp,
        duration=payload.duration,
        value=payload.value,
    )
    with transaction.atomic():
        event.full_clean()
        event.save()
        log_activity(verb="annotations.event.create", target=event)
    return _serialize_event(event)


@events_router.patch("/{object_hash}", auth=None)
def update_event(request, object_hash: str, payload: EventPatch):
    user = _require_auth(request)
    event = _resolve_by_hash(Event, object_hash, user, prefetch=("codes",))
    if event is None:
        raise HttpError(404, "Event not found")

    target = event.target_object
    if target is None:
        raise HttpError(400, "Target object no longer exists")

    if not can_modify_object(user=user, obj=event):
        raise HttpError(403, "You do not have permission to modify this event")

    patch = payload.model_dump(exclude_unset=True)
    for field_name, field_value in patch.items():
        setattr(event, field_name, field_value)

    with transaction.atomic():
        event.full_clean()
        event.save()
        log_activity(
            verb="annotations.event.update",
            target=event,
            metadata={"fields_updated": sorted(patch.keys())},
        )
    return _serialize_event(event)


@events_router.delete("/{object_hash}", auth=None)
def delete_event(request, object_hash: str):
    user = _require_auth(request)
    event = _resolve_by_hash(Event, object_hash, user)
    if event is None:
        raise HttpError(404, "Event not found")

    if not can_modify_object(user=user, obj=event):
        raise HttpError(403, "You do not have permission to delete this event")

    with transaction.atomic():
        log_activity(verb="annotations.event.delete", target=event)
        event.delete()
    return {"status": "deleted", "object_hash": object_hash}


# ── Interruption endpoints ────────────────────────────────────────────────────


@interruptions_router.get("/", auth=None)
def list_interruptions(
    request,
    target_content_type_id: int,
    target_object_id: str,
    author_id: int | None = None,
    limit: int = Query(500, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    rows, target = _list_for_target(
        request,
        Interruption,
        target_content_type_id,
        target_object_id,
        author_id,
        limit,
        offset,
    )
    log_activity(
        verb="annotations.interruption.list",
        target=target,
        metadata={
            "author_id_filter": author_id,
            "limit": limit,
            "offset": offset,
            "returned_count": len(rows),
        },
    )
    return [_serialize_interruption(row) for row in rows]


@interruptions_router.get("/mine", auth=None)
def list_my_interruptions(
    request,
    limit: int = Query(500, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    user = _require_auth(request)
    rows = list(
        Interruption.objects.filter(author=user)
        .prefetch_related("codes")
        .order_by("-created_at")[offset : offset + limit]
    )
    log_activity(
        verb="annotations.interruption.mine",
        metadata={"limit": limit, "offset": offset, "returned_count": len(rows)},
    )
    return [_serialize_interruption(row) for row in rows]


@interruptions_router.post("/", auth=None)
def create_interruption(request, payload: InterruptionIn):
    user = _require_auth(request)
    content_type, target = _resolve_target_or_404(payload.target_content_type_id, payload.target_object_id)

    if not can_annotate_object(user=user, obj=target, annotator=payload.annotator):
        raise HttpError(403, "You do not have permission to annotate this object")

    interruption = Interruption(
        author=user,
        target_content_type=content_type,
        target_object_id=str(payload.target_object_id),
        object_hash=payload.object_hash,
        timestamp=payload.timestamp,
        duration=payload.duration,
    )
    with transaction.atomic():
        interruption.full_clean()
        interruption.save()
        log_activity(verb="annotations.interruption.create", target=interruption)
    return _serialize_interruption(interruption)


@interruptions_router.patch("/{object_hash}", auth=None)
def update_interruption(request, object_hash: str, payload: InterruptionPatch):
    user = _require_auth(request)
    interruption = _resolve_by_hash(Interruption, object_hash, user, prefetch=("codes",))
    if interruption is None:
        raise HttpError(404, "Interruption not found")

    target = interruption.target_object
    if target is None:
        raise HttpError(400, "Target object no longer exists")

    if not can_modify_object(user=user, obj=interruption):
        raise HttpError(403, "You do not have permission to modify this interruption")

    patch = payload.model_dump(exclude_unset=True)
    for field_name, field_value in patch.items():
        setattr(interruption, field_name, field_value)

    with transaction.atomic():
        interruption.full_clean()
        interruption.save()
        log_activity(
            verb="annotations.interruption.update",
            target=interruption,
            metadata={"fields_updated": sorted(patch.keys())},
        )
    return _serialize_interruption(interruption)


@interruptions_router.delete("/{object_hash}", auth=None)
def delete_interruption(request, object_hash: str):
    user = _require_auth(request)
    interruption = _resolve_by_hash(Interruption, object_hash, user)
    if interruption is None:
        raise HttpError(404, "Interruption not found")

    if not can_modify_object(user=user, obj=interruption):
        raise HttpError(403, "You do not have permission to delete this interruption")

    with transaction.atomic():
        log_activity(verb="annotations.interruption.delete", target=interruption)
        interruption.delete()
    return {"status": "deleted", "object_hash": object_hash}


# ── Label endpoints ───────────────────────────────────────────────────────────


@labels_router.get("/", auth=None)
def list_labels(
    request,
    target_content_type_id: int,
    target_object_id: str,
    author_id: int | None = None,
    limit: int = Query(500, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    rows, target = _list_for_target(
        request,
        Label,
        target_content_type_id,
        target_object_id,
        author_id,
        limit,
        offset,
    )
    log_activity(
        verb="annotations.label.list",
        target=target,
        metadata={
            "author_id_filter": author_id,
            "limit": limit,
            "offset": offset,
            "returned_count": len(rows),
        },
    )
    return [_serialize_label(row) for row in rows]


@labels_router.get("/mine", auth=None)
def list_my_labels(
    request,
    limit: int = Query(500, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    user = _require_auth(request)
    rows = list(
        Label.objects.filter(author=user).prefetch_related("codes").order_by("-created_at")[offset : offset + limit]
    )
    log_activity(
        verb="annotations.label.mine",
        metadata={"limit": limit, "offset": offset, "returned_count": len(rows)},
    )
    return [_serialize_label(row) for row in rows]


@labels_router.post("/", auth=None)
def create_label(request, payload: LabelIn):
    user = _require_auth(request)
    content_type, target = _resolve_target_or_404(payload.target_content_type_id, payload.target_object_id)

    if not can_annotate_object(user=user, obj=target, annotator=payload.annotator):
        raise HttpError(403, "You do not have permission to annotate this object")

    label = Label(
        author=user,
        target_content_type=content_type,
        target_object_id=str(payload.target_object_id),
        object_hash=payload.object_hash,
        name=payload.name,
        value=payload.value,
    )
    with transaction.atomic():
        label.full_clean()
        label.save()
        log_activity(verb="annotations.label.create", target=label)
    return _serialize_label(label)


@labels_router.patch("/{object_hash}", auth=None)
def update_label(request, object_hash: str, payload: LabelPatch):
    user = _require_auth(request)
    label = _resolve_by_hash(Label, object_hash, user, prefetch=("codes",))
    if label is None:
        raise HttpError(404, "Label not found")

    target = label.target_object
    if target is None:
        raise HttpError(400, "Target object no longer exists")

    if not can_modify_object(user=user, obj=label):
        raise HttpError(403, "You do not have permission to modify this label")

    patch = payload.model_dump(exclude_unset=True)
    for field_name, field_value in patch.items():
        setattr(label, field_name, field_value)

    with transaction.atomic():
        label.full_clean()
        label.save()
        log_activity(
            verb="annotations.label.update",
            target=label,
            metadata={"fields_updated": sorted(patch.keys())},
        )
    return _serialize_label(label)


@labels_router.delete("/{object_hash}", auth=None)
def delete_label(request, object_hash: str):
    user = _require_auth(request)
    label = _resolve_by_hash(Label, object_hash, user)
    if label is None:
        raise HttpError(404, "Label not found")

    if not can_modify_object(user=user, obj=label):
        raise HttpError(403, "You do not have permission to delete this label")

    with transaction.atomic():
        log_activity(verb="annotations.label.delete", target=label)
        label.delete()
    return {"status": "deleted", "object_hash": object_hash}


# ── Code endpoints ────────────────────────────────────────────────────────────


@codes_router.post("/", auth=None)
def create_code(request, payload: CodeIn):
    user = _require_auth(request)
    parent = _resolve_codeable_parent_or_400(payload.content_type_id, payload.object_id)

    if not can_modify_object(user=user, obj=parent):
        raise HttpError(403, "You do not have permission to add codes to this annotation")

    try:
        validate_code(payload.standard, payload.value, payload.meta)
    except ValueError as exc:
        raise HttpError(422, str(exc)) from exc

    content_type = ContentType.objects.get_for_model(type(parent))
    code = Code(
        content_type=content_type,
        object_id=str(parent.pk),
        standard=payload.standard,
        value=payload.value,
        meta=payload.meta,
    )
    with transaction.atomic():
        code.save()
        log_activity(verb="annotations.code.create", target=code)
    return _serialize_code(code)


@codes_router.patch("/{code_id}", auth=None)
def update_code(request, code_id: int, payload: CodePatch):
    user = _require_auth(request)
    code = Code.objects.filter(pk=code_id).first()
    if code is None:
        raise HttpError(404, "Code not found")

    parent = code.annotation
    if parent is None:
        raise HttpError(400, "Parent annotation no longer exists")

    if not can_modify_object(user=user, obj=parent):
        raise HttpError(403, "You do not have permission to modify this code")

    patch = payload.model_dump(exclude_unset=True)
    # standard and value are non-nullable — an explicit null would otherwise reach the validator and
    # the database as None and surface as a 500. meta is nullable, so null legitimately clears it.
    for required_field in ("standard", "value"):
        if required_field in patch and patch[required_field] is None:
            raise HttpError(422, f"{required_field} cannot be null")
    for field_name, field_value in patch.items():
        setattr(code, field_name, field_value)

    try:
        validate_code(code.standard, code.value, code.meta)
    except ValueError as exc:
        raise HttpError(422, str(exc)) from exc

    with transaction.atomic():
        code.save()
        log_activity(
            verb="annotations.code.update",
            target=code,
            metadata={"fields_updated": sorted(patch.keys())},
        )
    return _serialize_code(code)


@codes_router.delete("/{code_id}", auth=None)
def delete_code(request, code_id: int):
    user = _require_auth(request)
    code = Code.objects.filter(pk=code_id).first()
    if code is None:
        raise HttpError(404, "Code not found")

    parent = code.annotation
    if parent is None:
        raise HttpError(400, "Parent annotation no longer exists")

    if not can_modify_object(user=user, obj=parent):
        raise HttpError(403, "You do not have permission to delete this code")

    with transaction.atomic():
        log_activity(verb="annotations.code.delete", target=code)
        code.delete()
    return {"status": "deleted", "id": code_id}


# ── Export endpoint ──────────────────────────────────────────────────────────


@export_router.get("", auth=None)
def export_annotations(
    request,
    types: str = Query("events,labels"),
    format: str = Query("json"),
    recording: list[str] = Query([]),
    dataset_id: int | None = Query(None),
    annotator_id: list[int] = Query([]),
    since: str | None = Query(None),
    until: str | None = Query(None),
    version_id: str | None = Query(None),
):
    """Export events and/or labels as a downloadable JSON or CSV file.

    Staff (and superusers) export across all annotators, optionally narrowed with repeated
    ``annotator_id`` parameters; every other caller gets only their own rows, enforced on the
    queryset rather than filtered afterwards. The file identifies annotators by numeric user id
    only — identity resolves via the roster endpoint below, inside the platform. CSV takes exactly
    one type per file — events and labels have different columns — so ``format=csv`` with both
    types is a 422.

    The response is an attachment rather than a JSON body, so the browser saves it directly; the
    ``no-store`` default from ``SecurityHeadersMiddleware`` still applies to it.
    """
    user = _require_auth(request)
    filters = annotation_export.parse_filters(
        types=types,
        export_format=format,
        recordings=recording,
        dataset_id=dataset_id,
        annotator_ids=annotator_id,
        since=since,
        until=until,
        version_id=version_id,
    )

    is_staff = bool(user.is_staff or user.is_superuser)
    if not is_staff and any(annotator != user.pk for annotator in filters.annotator_ids):
        # A non-staff caller naming someone else is a permission denial, not a filter miss — the
        # bare 403 would otherwise be the only trace of an attempt to read another rater's output.
        log_security_event(
            "permission.denied",
            actor_id=user.pk,
            permission="annotations.export.other_author",
            ip=get_client_ip(request),
            path=request.path,
            method=request.method,
        )
        raise HttpError(403, "Exporting another user's annotations requires staff access.")

    exported_at = timezone.now()
    result = annotation_export.build_export(caller=user, filters=filters)
    metadata = annotation_export.build_metadata(result, exported_by=user, exported_at=exported_at)

    if filters.export_format == "csv":
        body = annotation_export.render_csv(result, metadata)
        content_type = "text/csv; charset=utf-8"
    else:
        body = annotation_export.render_json(result, metadata)
        content_type = "application/json; charset=utf-8"

    # The audit row records which accounts' data left the system by opaque id only — the audit
    # trail is permanent, and this row targets no user, so ``erase_subject`` can never select it
    # to scrub. ``filters.as_metadata()`` carries no username or name by construction.
    log_activity(
        verb="annotations.export",
        metadata={
            "format": filters.export_format,
            "types": list(filters.types),
            "filters": filters.as_metadata(),
            "restricted_to_own_annotations": result.restricted_to_self,
            "returned_counts": metadata["counts"],
            "annotator_count": len(result.annotators),
            "annotator_ids": result.annotator_ids,
        },
    )

    response = HttpResponse(body, content_type=content_type)
    response["Content-Disposition"] = content_disposition_header(
        as_attachment=True,
        filename=annotation_export.export_filename(result, exported_at),
    )
    return response


@export_router.get("/annotators", auth=None)
def list_export_annotators(request):
    """List every annotator as ``{id, username, name, events, labels}``, for staff callers.

    The in-platform counterpart of the export's ``author_id`` values: exported files carry no
    personal data, so this roster is where an exporter picks the annotators to include and later
    resolves the ids in a file back to people. It stays behind staff authentication and never
    enters an export.
    """
    user = _require_auth(request)
    if not (user.is_staff or user.is_superuser):
        log_security_event(
            "permission.denied",
            actor_id=user.pk,
            permission="annotations.export.annotators",
            ip=get_client_ip(request),
            path=request.path,
            method=request.method,
        )
        raise HttpError(403, "Listing annotators requires staff access.")

    roster = annotation_export.list_annotators()
    # Count only — usernames and names must not enter the permanent audit trail.
    log_activity(verb="annotations.annotator.list", metadata={"annotator_count": len(roster)})
    return {"annotators": roster}


# ── Register routers ─────────────────────────────────────────────────────────

api.add_router("/annotations/", annotations_router)
api.add_router("/events/", events_router)
api.add_router("/interruptions/", interruptions_router)
api.add_router("/labels/", labels_router)
api.add_router("/codes/", codes_router)
api.add_router("/export", export_router)
