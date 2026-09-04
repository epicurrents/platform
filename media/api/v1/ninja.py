"""Media API v1 — upload, list, detail, file download, patch, soft delete.

Endpoints
---------
POST   /upload          Upload a media file; returns the created row immediately.
GET    /                List media files visible to the caller.
GET    /{hash}          Metadata for a single media file (with ``is_supported`` flag).
GET    /{hash}/file     Download the raw file bytes; 410 when the extension is no
                        longer in the current project's allowlist.
PATCH  /{hash}          Update display_name, media_type, or recording attachment.
DELETE /{hash}          Soft-delete (sets ``deleted_at``).

Authentication
--------------
Write endpoints (upload, patch, soft delete) and the list endpoint require
session authentication. The two read endpoints (detail, file download)
accept three modes in the same precedence order as the recordings API:

1. Django session — the caller is the platform user.
2. ``FederatedBearer`` JWT — the caller is a trusted federated peer; the
   access check uses :func:`get_federated_read_access_result`.
3. ``?share_token=<token>`` query param — anonymous; the AccessRight
   row carrying that token grants read.

De-identification
-----------------
``original_name`` is author-private (same rule as Recording — see
``_can_see_original_name``). URLs use ``content_hash`` rather than the
integer PK to avoid leaking creation order.

Allowlist semantics
-------------------
``MEDIA_ALLOWED_UPLOAD_EXTENSIONS`` is consulted at both upload and
download:

- Upload rejects extensions outside the list with 400 so a project that
  doesn't expect markdown can never receive one by accident.
- Download returns 410 when an existing file's extension has fallen out of
  the list (typically because the operator switched to a project with a
  narrower allowlist). The metadata endpoints still list the row with
  ``is_supported: false`` so the frontend can show it greyed out.
"""

import hashlib
import logging
import mimetypes
import re
import secrets
import shutil
from pathlib import Path

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.db.models import Q
from django.http import FileResponse, HttpResponse, StreamingHttpResponse
from django.utils import timezone
from django.utils.http import content_disposition_header
from ninja import File, NinjaAPI, Query, Schema, UploadedFile
from ninja.errors import HttpError

from activity.audit import log_activity
from epicurrents.auth import enforce_session_csrf
from epicurrents.models import AccessRight
from epicurrents.permissions import (
    can_read_object,
    ensure_can_write_object,
    get_federated_read_access_result,
)
from federation.audit import log_federation_access
from federation.auth import try_federation_auth
from media.models import MediaFile
from recordings.models import Recording

logger = logging.getLogger(__name__)

api = NinjaAPI(
    title="Media API",
    version="1",
    urls_namespace="media-api-v1",
    docs_url=settings.API_DOCS_URL,
    openapi_url=settings.API_OPENAPI_URL,
)


# ── Schemas ───────────────────────────────────────────────────────────────────


class MediaAttachmentOut(Schema):
    """Identifies the parent object a media file is attached to.

    ``type`` is the lowercased model name (``"recording"`` today; future
    targets such as ``"collection"`` or ``"dataset"`` slot in here without
    schema changes). ``id`` is the public identifier appropriate for that
    type — the content hash for recordings.
    """

    type: str
    id: str


class MediaFileOut(Schema):
    """Public-facing media-file metadata."""

    content_hash: str
    media_type: str
    display_name: str
    file_extension: str
    file_size: int
    # True when the file's extension is in the live MEDIA_ALLOWED_UPLOAD_EXTENSIONS.
    # The frontend greys out unsupported rows; the file-download endpoint
    # rejects them with 410.
    is_supported: bool
    # Parent object, when attached and the target still exists. A stale
    # generic-FK pair (target purged) surfaces as null.
    attached_to: MediaAttachmentOut | None
    # Position in seconds of the media on the parent's timeline (video/audio
    # start offset, image pin point). Null for non-time-aligned media.
    time_offset: float | None
    created_at: str
    modified_at: str


class MediaFileDetailOut(MediaFileOut):
    """Detail view adds author-private fields when the caller is allowed."""

    # Visible only to the author and superusers; null otherwise (PHI-bearing).
    original_name: str | None


class MediaFileUploadOut(Schema):
    """Response returned immediately after a successful upload.

    The uploader is always the author, so ``original_name`` is returned
    unconditionally here.
    """

    content_hash: str
    media_type: str
    display_name: str
    original_name: str
    file_extension: str
    file_size: int
    file_hash: str
    is_supported: bool
    attached_to: MediaAttachmentOut | None
    time_offset: float | None
    created_at: str
    modified_at: str


class MediaAttachmentIn(Schema):
    """Attachment target on upload / patch.

    Set ``type`` and ``id`` to attach; omit both (or pass null at the
    parent field) to leave unattached. PATCH may pass ``type=""`` to
    explicitly detach.
    """

    type: str
    id: str


class MediaFilePatch(Schema):
    """Partial update payload.

    A field absent from the request body is left unchanged. ``time_offset``
    accepts an explicit ``null`` to clear the alignment, so the patch handler
    distinguishes "omitted" from "set to null" via ``model_fields_set`` rather
    than treating null as "unchanged".
    """

    display_name: str | None = None
    media_type: str | None = None
    # Set to a ``{type, id}`` object to attach, or to ``{type: "", id: ""}``
    # to detach. Omit the field to leave the attachment unchanged.
    attached_to: MediaAttachmentIn | None = None
    # Seconds offset of the media on the parent's timeline; null clears it.
    time_offset: float | None = None


# ── Helpers ───────────────────────────────────────────────────────────────────


def _require_auth(request):
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated:
        raise HttpError(401, "Authentication credentials were not provided")
    enforce_session_csrf(request)
    return user


def _try_federated_auth(request):
    """Non-destructive ``FederatedBearer`` JWT check.

    Returns ``(FederatedPeer, remote_user_id)`` when the request carries a
    valid token from a trusted peer; ``None`` otherwise. Local-name wrapper
    around :func:`federation.auth.try_federation_auth` so existing tests
    can mock ``media.api.v1.ninja._try_federated_auth`` directly (same
    pattern as the recordings module).
    """
    return try_federation_auth(request)


def _allowed_extensions() -> set[str]:
    """Return the current allowlist as a set of lowercase, dot-prefixed exts."""
    raw = getattr(settings, "MEDIA_ALLOWED_UPLOAD_EXTENSIONS", []) or []
    return {e.lower().strip() for e in raw if e}


def _resolve_media_root(configured: str) -> Path:
    """Resolve a media storage setting to an absolute, existing directory."""
    root = Path(configured)
    if not root.is_absolute():
        root = Path(settings.BASE_DIR) / root
    root.mkdir(parents=True, exist_ok=True)
    return root


def _is_supported(media: MediaFile) -> bool:
    """Return True when the file's extension is in the live allowlist."""
    ext = (media.file_extension or "").lower()
    return ext in _allowed_extensions()


def _can_see_original_name(user, media: MediaFile) -> bool:
    """Return True when the caller may see ``MediaFile.original_name``.

    Mirrors :func:`recordings.api.v1.ninja._can_see_original_name`. The
    original filename can carry PHI (e.g. ``MRN12345-summary.pdf``) so it is
    visible only to the author and to superusers.
    """
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_superuser", False):
        return True
    return getattr(media, "author_id", None) == user.pk


def _resolve_display_name(media: MediaFile) -> str:
    """Return the grantee-visible display name."""
    name = (media.display_name or "").strip()
    if name:
        return name
    return (media.stored_name or "")[:8].upper()


def _get_media_or_404(content_hash: str) -> MediaFile:
    """Look up a media file by its de-identified hash, excluding soft-deleted."""
    normalized = content_hash.upper()
    media = MediaFile.objects.filter(
        # Exact match: values are always written upper-case and the lookup
        # normalises, so iexact would only defeat the index.
        content_hash=normalized,
        deleted_at__isnull=True,
    ).first()
    if media is None:
        raise HttpError(404, "Media file not found")
    return media


def _get_recording_by_hash(content_hash: str) -> Recording | None:
    """Resolve a recording by its public hash, or return None when missing."""
    normalized = content_hash.upper()
    return Recording.objects.filter(
        stored_name__startswith=f"{normalized}.",
        deleted_at__isnull=True,
    ).first()


# Registry of attachment target types. To add a new attachable model
# (e.g. ``collection``), register a resolver here. Each resolver returns
# ``(target, public_id_str)`` or ``(None, "")`` when the public ID does
# not resolve. The same registry is used to serialise the parent back to
# ``{type, id}`` on read.
def _resolve_recording_for_attach(user, public_id: str) -> tuple[Recording, str]:
    rec = _get_recording_by_hash(public_id)
    if rec is None:
        raise HttpError(400, "Recording to attach to not found")
    ensure_can_write_object(user=user, obj=rec)
    return rec, rec.stored_name.split(".", 1)[0]


def _resolve_recording_for_read(public_id: str) -> Recording | None:
    """Resolve a recording by public id for read-scoped attachment listing.

    No permission check here — listing a recording's attached media needs only
    read access (not the write access that attaching requires), which the
    per-row ``can_read_object`` gate (with the attachment extension) enforces on
    each returned media row.
    """
    return _get_recording_by_hash(public_id)


def _public_id_for_recording(rec: Recording) -> str:
    """Return the recording's public identifier (its content hash)."""
    stored = getattr(rec, "stored_name", "") or ""
    return stored.split(".", 1)[0] if stored else ""


_ATTACHMENT_TARGETS: dict[str, dict] = {
    "recording": {
        "model": Recording,
        "resolve_for_attach": _resolve_recording_for_attach,
        "resolve_for_read": _resolve_recording_for_read,
        "public_id": _public_id_for_recording,
    },
}


def _attachment_payload(media: MediaFile) -> dict | None:
    """Serialise the attachment back to a ``{type, id}`` pair, or None."""
    if media.attachment_content_type_id is None or not media.attachment_object_id:
        return None
    ct = media.attachment_content_type
    type_key = (ct.model or "").lower()
    spec = _ATTACHMENT_TARGETS.get(type_key)
    if spec is None:
        # Registered ContentType exists but no resolver — treat as detached
        # from the public API's perspective; the row is still queryable in
        # admin and via direct ORM access.
        return None
    target = media.attachment  # GenericFK fetches lazily
    if target is None:
        return None
    return {"type": type_key, "id": spec["public_id"](target)}


def _serialise(media: MediaFile, user, detail: bool = False) -> dict:
    """Build the response payload for a single media file."""
    base = {
        "content_hash": media.content_hash,
        "media_type": media.media_type,
        "display_name": _resolve_display_name(media),
        "file_extension": media.file_extension,
        "file_size": media.file_size,
        "is_supported": _is_supported(media),
        "attached_to": _attachment_payload(media),
        "time_offset": media.time_offset,
        "created_at": media.created_at.isoformat() if media.created_at else "",
        "modified_at": media.modified_at.isoformat() if media.modified_at else "",
    }
    if detail:
        base["original_name"] = media.original_name if _can_see_original_name(user, media) else None
    return base


def _serve_disposition(media: MediaFile) -> tuple[str, bool]:
    """Return ``(content_type, inline)`` for serving this media file.

    Video is served inline with its real MIME type so the browser's
    ``<video>`` element plays it (and can seek via Range requests); every
    other media type keeps the download-as-attachment behaviour with a
    generic type.
    """
    if media.media_type == MediaFile.MediaType.VIDEO:
        guessed, _ = mimetypes.guess_type(media.stored_name)
        return guessed or "application/octet-stream", True
    return "application/octet-stream", False


def _serve_media_file(request, media: MediaFile, file_path: Path):
    """Serve a media file with HTTP Range support (RFC 7233).

    Returns ``(response, range_start)``. ``range_start`` is the first byte
    offset served — 0 for a full response or a ``bytes=0-`` range, the
    requested offset for a mid-file seek, and ``None`` for an unsatisfiable
    range (416). The caller logs the download only when ``range_start`` is 0
    so seeking a video does not write one audit row per seek: every playback
    session starts by fetching from byte 0, while seeks request a later offset.

    ``Accept-Ranges: bytes`` is always advertised so a client can issue ranges
    without a prior 416 probe. Video is served inline; other types download as
    an attachment (see :func:`_serve_disposition`).
    """
    content_type, inline = _serve_disposition(media)
    filename = f"{_resolve_display_name(media)}{media.file_extension}"
    file_size = file_path.stat().st_size
    range_header = request.META.get("HTTP_RANGE", "").strip()

    if not range_header:
        response = FileResponse(
            file_path.open("rb"),
            as_attachment=not inline,
            filename=filename,
            content_type=content_type,
        )
        response["Accept-Ranges"] = "bytes"
        response["Content-Length"] = str(file_size)
        return response, 0

    match = re.fullmatch(r"bytes=(\d+)-(\d*)", range_header)
    if not match:
        response = HttpResponse(status=416)
        response["Content-Range"] = f"bytes */{file_size}"
        return response, None

    start = int(match.group(1))
    end = int(match.group(2)) if match.group(2) else file_size - 1
    if start > end or start >= file_size:
        response = HttpResponse(status=416)
        response["Content-Range"] = f"bytes */{file_size}"
        return response, None

    end = min(end, file_size - 1)
    length = end - start + 1

    def _stream():
        chunk_size = 64 * 1024
        with file_path.open("rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                data = f.read(min(chunk_size, remaining))
                if not data:
                    break
                remaining -= len(data)
                yield data

    response = StreamingHttpResponse(_stream(), status=206, content_type=content_type)
    response["Content-Range"] = f"bytes {start}-{end}/{file_size}"
    response["Content-Length"] = str(length)
    response["Accept-Ranges"] = "bytes"
    response["Content-Disposition"] = content_disposition_header(as_attachment=not inline, filename=filename)
    return response, start


# ── Endpoints ─────────────────────────────────────────────────────────────────


@api.post("/upload", response=MediaFileUploadOut)
def upload_media(
    request,
    file: UploadedFile = File(...),
    media_type: str = MediaFile.MediaType.DOCUMENT,
    display_name: str | None = None,
    attached_to_type: str | None = None,
    attached_to_id: str | None = None,
    time_offset: float | None = None,
):
    """Save uploaded media file and register a :class:`MediaFile` row.

    Validates the file extension against
    :setting:`MEDIA_ALLOWED_UPLOAD_EXTENSIONS`; returns 400 when the
    extension is outside the list. Empty allowlist disables uploads
    altogether — projects must opt in via their ``settings.py``.

    ``attached_to_type`` + ``attached_to_id`` identify an optional parent
    object (today only ``type="recording"`` is registered); pass both or
    neither.
    """
    user = _require_auth(request)

    if media_type not in MediaFile.MediaType.values:
        raise HttpError(
            400,
            f"Invalid media_type. Allowed: {sorted(MediaFile.MediaType.values)}",
        )

    original_name = file.name or "upload.bin"
    extension = Path(original_name).suffix.lower()
    allowed = _allowed_extensions()
    if not allowed:
        raise HttpError(
            403,
            "Media uploads are disabled for this project. "
            "Ask the operator to populate MEDIA_ALLOWED_UPLOAD_EXTENSIONS.",
        )
    if extension not in allowed:
        raise HttpError(
            400,
            f"File extension {extension!r} is not allowed. Allowed: {sorted(allowed)}",
        )

    attach_target = None
    attach_ct = None
    if (attached_to_type or "") or (attached_to_id or ""):
        if not (attached_to_type and attached_to_id):
            raise HttpError(
                400,
                "attached_to_type and attached_to_id must be supplied together.",
            )
        type_key = attached_to_type.lower().strip()
        spec = _ATTACHMENT_TARGETS.get(type_key)
        if spec is None:
            raise HttpError(
                400,
                f"Unsupported attached_to_type {attached_to_type!r}. Allowed: {sorted(_ATTACHMENT_TARGETS)}",
            )
        attach_target, _ = spec["resolve_for_attach"](user, attached_to_id)
        attach_ct = ContentType.objects.get_for_model(attach_target, for_concrete_model=False)

    staging_root = _resolve_media_root(settings.MEDIA_STAGING_PATH)
    upload_root = _resolve_media_root(settings.MEDIA_UPLOAD_PATH)

    stored_name = f"{secrets.token_hex(16).upper()}{extension}"
    while (staging_root / stored_name).exists() or (upload_root / stored_name).exists():
        stored_name = f"{secrets.token_hex(16).upper()}{extension}"

    staging_path = staging_root / stored_name
    max_size = getattr(settings, "MEDIA_MAX_UPLOAD_SIZE", 256 * 1024 * 1024)
    file_hasher = hashlib.sha256()
    total_size = 0
    try:
        with staging_path.open("wb") as destination:
            for chunk in file.chunks():
                total_size += len(chunk)
                if total_size > max_size:
                    raise HttpError(
                        413,
                        f"File exceeds maximum upload size ({max_size // (1024 * 1024)} MB).",
                    )
                destination.write(chunk)
                file_hasher.update(chunk)
    except HttpError:
        staging_path.unlink(missing_ok=True)
        raise

    file_hash = file_hasher.hexdigest()
    # The public hash is derived from a per-row random source rather than the
    # file bytes so content-based duplicate detection (via ``file_hash``)
    # never reveals existing uploads to an attacker who knows the bytes.
    content_hash = secrets.token_hex(16).upper()

    # Move to permanent storage before the row exists, so a row never points
    # at a staging path. shutil.move handles the cross-device case when the
    # two roots are on different filesystems; in the compose stack both are
    # subdirectories of the same volume, so this is an atomic rename.
    final_path = upload_root / stored_name
    try:
        shutil.move(str(staging_path), str(final_path))
    except OSError:
        staging_path.unlink(missing_ok=True)
        raise HttpError(500, "Failed to store the uploaded file.")

    try:
        with transaction.atomic():
            media = MediaFile.objects.create(
                author=user,
                media_type=media_type,
                original_name=original_name,
                display_name=(display_name or "").strip() or None,
                stored_name=stored_name,
                file_extension=extension,
                file_size=total_size,
                file_path=str(final_path),
                file_hash=file_hash,
                content_hash=content_hash,
                attachment_content_type=attach_ct,
                attachment_object_id=str(attach_target.pk) if attach_target else "",
                time_offset=time_offset,
            )

            # Author always has read+write access; mirror the pattern used by
            # the recordings upload endpoint so the AccessRight machinery is
            # the single source of truth for grants.
            media_ct = ContentType.objects.get_for_model(media, for_concrete_model=False)
            AccessRight.objects.get_or_create(
                content_type=media_ct,
                object_id=str(media.pk),
                access_target=user,
                defaults={
                    "access_giver": user,
                    "can_read": True,
                    "can_write": True,
                },
            )
    except Exception:
        # The row never landed; don't leave an orphan file behind.
        final_path.unlink(missing_ok=True)
        raise

    log_activity(verb="media.upload", target=media)

    return {
        "content_hash": media.content_hash,
        "media_type": media.media_type,
        "display_name": _resolve_display_name(media),
        "original_name": media.original_name,
        "file_extension": media.file_extension,
        "file_size": media.file_size,
        "file_hash": media.file_hash,
        "is_supported": _is_supported(media),
        "attached_to": _attachment_payload(media),
        "time_offset": media.time_offset,
        "created_at": media.created_at.isoformat(),
        "modified_at": media.modified_at.isoformat(),
    }


@api.get("/", response=list[MediaFileOut])
def list_media(
    request,
    attached_to_type: str | None = Query(default=None),
    attached_to_id: str | None = Query(default=None),
    media_type: str | None = Query(default=None),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """List media files the caller can read.

    Filter optionally by ``attached_to_type``+``attached_to_id`` (parent
    object's type+public id; both must be supplied together) or by
    ``media_type``. Soft-deleted rows are excluded.
    """
    user = _require_auth(request)

    qs = MediaFile.objects.filter(deleted_at__isnull=True).order_by("-created_at")
    if media_type:
        qs = qs.filter(media_type=media_type)
    scoped_to_parent = False
    if (attached_to_type or "") or (attached_to_id or ""):
        if not (attached_to_type and attached_to_id):
            raise HttpError(
                400,
                "attached_to_type and attached_to_id must be supplied together.",
            )
        type_key = attached_to_type.lower().strip()
        spec = _ATTACHMENT_TARGETS.get(type_key)
        if spec is None:
            raise HttpError(400, f"Unsupported attached_to_type {attached_to_type!r}")
        # Listing a parent's attached media needs only read access on the parent,
        # not the write access that *attaching* requires — so resolve without a
        # permission check. A caller who cannot read the parent gets an empty
        # list (no parent existence is leaked) via the per-row gate below.
        target = spec["resolve_for_read"](attached_to_id)
        if target is None:
            return []
        target_ct = ContentType.objects.get_for_model(target, for_concrete_model=False)
        qs = qs.filter(
            attachment_content_type=target_ct,
            attachment_object_id=str(target.pk),
        )
        scoped_to_parent = True

    # Narrow to rows the caller could plausibly see (author, or via a direct or
    # group AccessRight row) before the per-row ``can_read_object`` check, which
    # is N+1. Skip this pre-filter when scoped to a single parent's attachments:
    # that set is already small and is typically readable purely via attachment
    # inheritance (no direct media AccessRight), so the pre-filter would wrongly
    # drop it — the per-row gate (which consults the attachment extension) is the
    # sole authority there.
    if not scoped_to_parent:
        media_ct = ContentType.objects.get_for_model(MediaFile, for_concrete_model=False)
        # Materialise the object_id list into a Python set rather than leaving it
        # a subquery: object_id is a CharField, and Postgres rejects bigint pk IN
        # (varchar subquery) with "operator does not exist: bigint = character
        # varying". As a set of literals Django coerces each value to the pk type.
        visible_via_access = set(
            AccessRight.objects.filter(
                Q(access_target=user) | Q(access_target_group__in=user.groups.all()),
                content_type=media_ct,
                can_read=True,
            ).values_list("object_id", flat=True)
        )
        qs = qs.filter(Q(author=user) | Q(pk__in=visible_via_access))

    rows = list(qs[offset : offset + limit])
    filters = {
        k: v
        for k, v in {
            "media_type": media_type,
            "attached_to_type": attached_to_type,
            "attached_to_id": attached_to_id,
        }.items()
        if v
    }
    log_activity(verb="media.list", metadata=filters or None)
    return [_serialise(m, user) for m in rows if can_read_object(user=user, obj=m)]


@api.get("/{content_hash}", response=MediaFileDetailOut)
def get_media_detail(request, content_hash: str, share_token: str | None = None):
    """Return full metadata for a single media file.

    Accepts three auth modes (session, federated, share-token) in the same
    precedence order as the recordings detail endpoint. ``share_token`` is
    a query param; pass it for anonymous access via a public share grant.
    """
    user = getattr(request, "user", None)
    fed = None
    if user and getattr(user, "is_authenticated", False):
        pass  # session auth
    else:
        fed = _try_federated_auth(request)
        if fed is None and not (share_token or "").strip():
            raise HttpError(401, "Authentication credentials were not provided")

    media = _get_media_or_404(content_hash)

    if fed is not None:
        fed_peer, remote_user_id = fed
        granted = get_federated_read_access_result(fed_peer, remote_user_id, media).granted
        if not granted:
            log_federation_access(
                peer=fed_peer,
                remote_user_id=remote_user_id,
                action="media_detail",
                target=media,
                status_code=403,
            )
            raise HttpError(403, "You do not have permission to view this media file")
        log_federation_access(
            peer=fed_peer,
            remote_user_id=remote_user_id,
            action="media_detail",
            target=media,
            status_code=200,
        )
    elif not can_read_object(user=user, obj=media, share_token=share_token):
        raise HttpError(404, "Media file not found")

    log_activity(
        verb="media.read",
        target=media,
        metadata={"share_token_used": bool((share_token or "").strip())},
    )
    return _serialise(media, user, detail=True)


@api.get("/{content_hash}/file")
def download_media(request, content_hash: str, share_token: str | None = None):
    """Stream the file bytes.

    Accepts session, federated, or share-token auth. Returns 410 when the
    extension has fallen out of the current
    :setting:`MEDIA_ALLOWED_UPLOAD_EXTENSIONS` — typically because the
    operator switched to a project that doesn't support the file type.
    """
    user = getattr(request, "user", None)
    fed = None
    if user and getattr(user, "is_authenticated", False):
        pass  # session auth
    else:
        fed = _try_federated_auth(request)
        if fed is None and not (share_token or "").strip():
            raise HttpError(401, "Authentication credentials were not provided")

    media = _get_media_or_404(content_hash)

    if fed is not None:
        fed_peer, remote_user_id = fed
        granted = get_federated_read_access_result(fed_peer, remote_user_id, media).granted
        if not granted:
            log_federation_access(
                peer=fed_peer,
                remote_user_id=remote_user_id,
                action="media_download",
                target=media,
                status_code=403,
            )
            raise HttpError(403, "You do not have permission to download this media file")
    elif not can_read_object(user=user, obj=media, share_token=share_token):
        raise HttpError(404, "Media file not found")

    if not _is_supported(media):
        if fed is not None:
            log_federation_access(
                peer=fed[0],
                remote_user_id=fed[1],
                action="media_download",
                target=media,
                status_code=410,
            )
        raise HttpError(
            410,
            "File type is not supported by the current project configuration.",
        )

    file_path = Path(media.file_path)
    if not file_path.is_file():
        logger.warning(
            "media.download.missing_file media_id=%s path=%s",
            media.pk,
            media.file_path,
        )
        raise HttpError(404, "File missing on disk")

    response, range_start = _serve_media_file(request, media, file_path)

    # Log only the first request of a playback session — a full download or a
    # ``bytes=0-`` range (both report range_start 0). A seeking video client
    # issues many mid-file range requests; logging each would flood the audit
    # trail with a row per seek. An unsatisfiable range (416, range_start None)
    # is not a successful read and is not logged either.
    if range_start == 0:
        if fed is not None:
            log_federation_access(
                peer=fed[0],
                remote_user_id=fed[1],
                action="media_download",
                target=media,
                status_code=200,
            )
        log_activity(
            verb="media.download",
            target=media,
            metadata={"share_token_used": bool((share_token or "").strip())},
        )

    return response


@api.patch("/{content_hash}", response=MediaFileDetailOut)
def patch_media(request, content_hash: str, payload: MediaFilePatch):
    """Update editable metadata. Author / superuser only."""
    user = _require_auth(request)
    media = _get_media_or_404(content_hash)
    ensure_can_write_object(user=user, obj=media)

    changes: dict = {}
    if payload.display_name is not None:
        new_name = payload.display_name.strip() or None
        if new_name != media.display_name:
            media.display_name = new_name
            changes["display_name"] = new_name
    if payload.media_type is not None:
        if payload.media_type not in MediaFile.MediaType.values:
            raise HttpError(
                400,
                f"Invalid media_type. Allowed: {sorted(MediaFile.MediaType.values)}",
            )
        if payload.media_type != media.media_type:
            media.media_type = payload.media_type
            changes["media_type"] = payload.media_type
    if payload.attached_to is not None:
        new_type = (payload.attached_to.type or "").lower().strip()
        new_id = (payload.attached_to.id or "").strip()
        if not new_type and not new_id:
            # Explicit detach.
            if media.attachment_content_type_id is not None:
                media.attachment_content_type = None
                media.attachment_object_id = ""
                changes["attached_to"] = None
        else:
            if not (new_type and new_id):
                raise HttpError(
                    400,
                    "attached_to.type and attached_to.id must be supplied together.",
                )
            spec = _ATTACHMENT_TARGETS.get(new_type)
            if spec is None:
                raise HttpError(400, f"Unsupported attached_to.type {new_type!r}")
            target, target_public = spec["resolve_for_attach"](user, new_id)
            target_ct = ContentType.objects.get_for_model(target, for_concrete_model=False)
            if media.attachment_content_type_id != target_ct.pk or media.attachment_object_id != str(target.pk):
                media.attachment_content_type = target_ct
                media.attachment_object_id = str(target.pk)
                changes["attached_to"] = {"type": new_type, "id": target_public}
    # ``time_offset`` takes null as a meaningful value (clear the
    # alignment), so presence is decided by ``model_fields_set`` rather than by
    # the value being None.
    if "time_offset" in payload.model_fields_set and payload.time_offset != media.time_offset:
        media.time_offset = payload.time_offset
        changes["time_offset"] = payload.time_offset

    if changes:
        media.save()
        log_activity(verb="media.update", target=media, metadata=changes)

    return _serialise(media, user, detail=True)


@api.delete("/{content_hash}")
def delete_media(request, content_hash: str):
    """Soft-delete a media file (sets ``deleted_at``). Author / superuser only."""
    user = _require_auth(request)
    media = _get_media_or_404(content_hash)
    ensure_can_write_object(user=user, obj=media)

    media.deleted_at = timezone.now()
    media.save(update_fields=["deleted_at"])

    log_activity(verb="media.trash", target=media)
    return {"ok": True}
