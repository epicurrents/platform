"""Recordings API v1 — upload, status, list, download, delete, patch, and slice.

⚠️ LOAD-BEARING — serving-pipeline parity for PHI sanitization.
``_build_serve_pipeline`` is the single construction site for the
middleware pipeline applied to every byte-serving path (full download,
range request, time-range slice, and the peer download-size
computation). The hazard is divergence, not absence: a serving path
that builds its own pipeline can anonymise the header while leaking
clinical annotation text, and every locally-written test for that path
still passes. Two rules keep the paths in sync:

1. Every byte-serving code path with ``apply_middleware=True`` calls
   ``_build_serve_pipeline()`` — never a hand-rolled
   ``MiddlewarePipeline([...])``.
2. A new byte-serving endpoint must be added to the request-shape list
   in the contract test so parity is asserted end-to-end.

See AGENTS.md → *Load-bearing files* before modifying. Contract tests
are in ``recordings/tests/test_serve_pipeline_parity.py`` (per-shape
sanitization parity for middleware callers, raw-bytes parity for
authors, and a source scan rejecting pipeline construction outside
``_build_serve_pipeline``).

Endpoints
---------
POST   /upload              Upload an EDF/BDF file; returns 202 and enqueues processing.
GET    /                    List recordings visible to the caller (author/access right).
GET    /status/{hash}       Lightweight processing-status poll.
GET    /{hash}              Full recording metadata including format meta and signals.
GET    /{hash}/slice        Metadata scoped to a time-range slice.
GET    /{hash}/annotations  Annotation bundles (Annotation objects) for a recording.
GET    /{hash}/file         Download file with HTTP Range support.
GET    /{hash}/file/slice   Download a time-range slice as a valid EDF/BDF file.
DELETE /{hash}              Soft-delete (move to recycle bin).
PATCH  /{hash}              Update editable metadata (display_name, modality).

The recording's own URL serves its metadata and ``/file`` serves the bytes, so
``GET``, ``PATCH`` and ``DELETE`` on ``/{hash}`` all address the same thing. The
media app is laid out the same way.

Authentication
--------------
All endpoints except ``/{hash}`` and ``/{hash}/file`` (when ``share_token`` is
supplied) require session authentication or a ``FederatedBearer`` JWT.
"""

import hashlib
import logging
import math
import re
import secrets
from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.db.models import Q, prefetch_related_objects
from django.http import FileResponse, HttpResponse, JsonResponse, StreamingHttpResponse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.utils.http import content_disposition_header
from ninja import File, NinjaAPI, Query, Schema, UploadedFile
from ninja.errors import HttpError

from activity.audit import log_activity
from epicurrents.api.schemas import AccessRightOut, access_right_out
from epicurrents.auth import enforce_session_csrf
from epicurrents.models import AccessRight
from epicurrents.offload import offload_file_response
from epicurrents.permissions import (
    can_modify_object,
    can_read_object,
    ensure_can_write_object,
    get_federated_read_access_result,
    get_read_access_result,
)
from federation.audit import log_federation_access
from federation.auth import try_federation_auth
from federation.limits import QuotaExceeded, check_peer_download_limits
from recordings.models import Recording, stored_original_name
from recordings.pipelines import get_converter

logger = logging.getLogger(__name__)

api = NinjaAPI(
    title="Recordings API",
    version="1",
    urls_namespace="recordings-api-v1",
    docs_url=settings.API_DOCS_URL,
    openapi_url=settings.API_OPENAPI_URL,
)


class AnnotationRef(Schema):
    """Minimal annotation reference embedded in recording responses."""

    object_hash: str


class InterruptionRefOut(Schema):
    """Interruption reference with its timing, embedded in recording responses.

    ``start`` and ``duration`` are seconds on the data-position timeline (gap-exclusive) — the
    same time base the Interruption rows store and the viewer's signal cache uses — so a client
    can seed a complete, trusted gap table from the recording metadata alone, without a second
    fetch. On discontinuous recordings the viewer needs that table to allow random access:
    without it, navigation is clamped to the span whose gaps it has discovered by decoding.
    """

    object_hash: str
    start: float
    duration: float


class SignalInfoOut(Schema):
    """Per-channel (signal) metadata from the EDF/BDF header."""

    index: int
    label: str
    sample_count: int
    sampling_rate: float
    is_annotation_channel: bool
    signal_type: str = ""
    physical_unit: str = ""
    physical_min: float = 0.0
    physical_max: float = 0.0
    digital_min: int = 0
    digital_max: int = 0
    transducer_type: str = ""
    prefiltering: str = ""
    # Author-private originals of the ingest-cleaned channel fields (see
    # deidentify_signal_infos); null for every other caller, like original_name.
    # source_index is the channel's position in the uploaded file before the
    # canonical reorder — the template order is part of the site fingerprint.
    source_label: str | None = None
    source_transducer_type: str | None = None
    source_prefiltering: str | None = None
    source_index: int | None = None


class RecordingMetaOut(Schema):
    """Parsed format metadata from the EDF/BDF header, available after processing."""

    format: str
    duration: float
    data_record_count: int
    data_record_duration: float
    signal_count: int
    discontinuous: bool
    # Ingest-time montage-shape assessment: 'referential' / 'bipolar' / 'mixed' /
    # 'unknown', plus the count of channels the canonicaliser could not resolve.
    # Content-free; served to every reader.
    channel_layout: str = "unknown"
    unresolved_channel_count: int = 0
    # Canonical channel-order spec version the stored file follows; 0 = unordered.
    channel_order_version: int = 0
    signals: list[SignalInfoOut] = []


class RecordingUploadOut(Schema):
    """Response payload returned immediately after upload (status=pending).

    The upload endpoint is author-only by definition, so ``original_name`` is
    returned unconditionally here — the uploader is always the author.
    """

    original_name: str
    display_name: str
    stored_name: str
    file_extension: str
    file_size: int
    file_hash: str
    status: str


class TrashedCollectionRef(Schema):
    """The trashed collection a root-surfaced recording will return to on restore."""

    id: int
    name: str


class RecordingOut(Schema):
    """List item schema for visible recording objects.

    ``original_name`` and ``processing_error`` are returned **only to the
    recording's author and superusers** — every other surface (grantees,
    share-token holders, federated peers) sees ``null`` for both.  The error
    string can carry filesystem paths and library stack traces; the
    filename can carry PHI.  Use ``display_name`` for any grantee-visible
    label; it is always populated (defaulting to the ``stored_name`` hash
    prefix when the author has not set a custom name).
    """

    hash: str
    original_name: str | None = None
    display_name: str
    has_custom_name: bool = False
    processing_error: str | None = None
    file_extension: str
    file_size: int
    file_hash: str
    content_hash: str
    status: str
    modality: str = ""
    created_at: datetime
    deleted_at: datetime | None = None
    meta: RecordingMetaOut | None = None
    events: list[AnnotationRef] = []
    interruptions: list[InterruptionRefOut] = []
    labels: list[AnnotationRef] = []
    trashed_collection: TrashedCollectionRef | None = None
    """Set when this recording is surfaced at the library root only because its
    sole collection is in the trash — the collection it will drop back into if
    that collection is restored. Only populated on the ``uncollected`` listing,
    and only for a collection the caller authored (the name is author-private);
    null otherwise."""
    download_size: int | None = None
    """Post-pipeline byte count that the server will actually transmit to this
    requester.  Populated only when the request is authenticated as a federated
    peer and the ``AccessRight`` grant has ``apply_middleware=True``.  Absent
    (``null``) for regular users or when the server pipeline is a no-op.

    Federated FUSE mounts use this value as the baseline for ``st_size`` so
    that they do not need to independently recompute the server's transform
    output size.  When present, ``download_size`` may equal ``file_size``
    (isometric pipeline) or differ (future signal pipelines)."""


class SlicedEventOut(Schema):
    """Event with timestamp and duration relative to the slice start, clamped to the slice window."""

    object_hash: str
    name: str
    timestamp: float
    duration: float | None = None
    value: dict | list | str | int | float | None = None


class SlicedInterruptionOut(Schema):
    """Interruption with timestamp and duration relative to the slice start, clamped to the slice window."""

    object_hash: str
    timestamp: float
    duration: float


class RecordingSliceOut(Schema):
    """Metadata response for a time-range slice of a recording.

    ``meta.duration`` and ``meta.data_record_count`` reflect the actual
    record-aligned slice window.  Events and interruptions are filtered to
    those overlapping the slice and have their timestamps shifted to be
    relative to the slice start (and durations clamped at the slice
    boundaries).  Labels and free-form annotation bundles are not included
    because they cannot be reliably scoped to a sub-range.
    """

    hash: str
    original_name: str | None = None
    display_name: str
    has_custom_name: bool = False
    file_extension: str
    file_size: int
    file_hash: str
    content_hash: str
    status: str
    modality: str = ""
    created_at: datetime
    deleted_at: datetime | None = None
    meta: RecordingMetaOut | None = None
    t_start: float
    t_end: float
    events: list[SlicedEventOut] = []
    interruptions: list[SlicedInterruptionOut] = []


class RecordingStatusOut(Schema):
    """Lightweight status response for polling after upload."""

    status: str


class RecordingPatchIn(Schema):
    """Payload for partial metadata update of a recording.

    Only fields present in the request body are updated.  The underlying file,
    the original filename, and all parsed EDF/BDF data are immutable; only
    the grantee-visible display name and modality may be changed via this
    endpoint.

    Set ``display_name`` to an empty string to clear it and revert to the
    default (``stored_name`` hash prefix).
    """

    display_name: str | None = None
    modality: str | None = None


def _require_auth(request):
    """Return authenticated user or raise 401 for unauthenticated requests."""

    user = getattr(request, "user", None)
    if not user or not user.is_authenticated:
        raise HttpError(401, "Authentication credentials were not provided")
    enforce_session_csrf(request)
    return user


# Extensions the upload endpoint accepts. EDF/BDF are stored as-is; anything else
# has to have a registered converter, which a project can add or remove through
# RECORDING_CONVERTERS. The check is not only about processing: ``stored_name`` is
# a hex token plus this suffix, and that name reaches the reverse proxy as a URI
# in the offload path (epicurrents/offload.py), so it must not be arbitrary
# client input. Django's UploadedFile strips path separators but keeps "?", "#"
# and spaces.
_EDF_EXTENSIONS = frozenset({".edf", ".bdf"})


def _download_filename(recording) -> str:
    """Return the Content-Disposition filename for a recording download.

    Combines the grantee-visible display name with the file extension so
    the file is recognisable on disk after download (e.g. ``ABCD1234.edf``).
    Falls back to the bare display name when ``file_extension`` is empty.
    """
    name = _resolve_display_name(recording)
    ext = recording.file_extension or ""
    if ext and not name.endswith(ext):
        return f"{name}{ext}"
    return name


def _resolve_display_name(recording) -> str:
    """Return the grantee-visible display name for *recording*.

    Falls back to the first 8 chars of ``stored_name`` (uppercase hex) when
    ``display_name`` is empty.  ``stored_name`` is generated at upload time
    and is stable across the recording's lifetime — unlike ``content_hash``,
    which the platform rewrites whenever it anonymises the file in place.
    """
    name = (recording.display_name or "").strip()
    if name:
        return name
    return (recording.stored_name or "")[:8].upper()


def _has_custom_display_name(recording) -> bool:
    """Return True when the author has set an explicit display name.

    Distinguishes a custom label from the hash-prefix fallback that
    ``_resolve_display_name`` returns when ``display_name`` is empty, so a
    client can keep showing the author-private ``original_name`` until a
    grantee-safe label exists.
    """
    return bool((recording.display_name or "").strip())


def _can_see_original_name(user, recording, fed) -> bool:
    """Return True when the caller may see ``Recording.original_name``.

    The original filename can carry PHI (``MRN_12345_routine.edf`` and
    similar), so it is returned only to the recording's author and to
    superusers.  Grantees, share-token holders, and federated peers see the
    display name only.
    """
    if fed is not None:
        return False
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_superuser", False):
        return True
    return getattr(recording, "author_id", None) == user.pk


def _failed_hidden_for_caller(recording, user, fed) -> bool:
    """Return True when *recording* is FAILED and the caller is not author/superuser.

    A FAILED upload is visible only to its author and to superusers — everyone
    else (local grantees, federated peers, share-token holders) sees it as if
    it did not exist.  Callers that hit this should respond 404 rather than
    surfacing the failure to grantees.

    The read-visibility gate in ``recordings/permissions.py`` enforces the same
    rule inside the permission resolver itself; this helper is what gives the
    recording surfaces their 404 response shape (the resolver's denial reads as
    403) and stands as defence-in-depth beneath the gate.
    """
    if recording.status != Recording.Status.FAILED:
        return False
    if fed is not None:
        return True
    if user is None or not getattr(user, "is_authenticated", False):
        return True
    if getattr(user, "is_superuser", False):
        return False
    return recording.author_id != user.pk


def _require_auth_or_federated(request):
    """Require either session or FederatedBearer authentication.

    Returns ``(user, None)`` when the request carries a valid Django session,
    or ``(None, (peer, remote_user_id))`` when it carries a valid
    ``FederatedBearer`` JWT from a trusted peer.

    Raises 401 when neither auth method is present or valid.  Session auth
    takes priority: if both a session cookie and a FederatedBearer header are
    present the session is used.
    """
    user = getattr(request, "user", None)
    if user and user.is_authenticated:
        enforce_session_csrf(request)
        return user, None

    fed = _try_federated_auth(request)
    if fed is not None:
        return None, fed

    raise HttpError(401, "Authentication credentials were not provided")


def _try_federated_auth(request):
    """Non-destructive ``FederatedBearer`` JWT check.

    Returns ``(FederatedPeer, remote_user_id)`` when the request carries a
    valid token from a trusted peer; ``None`` otherwise.  Thin wrapper around
    :func:`federation.auth.try_federation_auth` — the local name is preserved
    so existing tests can mock ``recordings.api.v1.ninja._try_federated_auth``
    directly.
    """
    return try_federation_auth(request)


def _compute_download_sizes_for_peer(recordings, peer, remote_user_id, meta_by_pk):
    """Compute server-side post-pipeline file sizes for a list of recordings.

    For each recording where the requesting peer's ``AccessRight`` has
    ``apply_middleware=True``, returns the byte count that the server will
    actually transmit after the pipeline is applied.  Recordings without that
    flag, or where the pipeline is isometric (no size change), return the raw
    ``file_size``.

    Returns a ``{recording.pk: download_size}`` dict.

    Cost
    ----
    Size-preserving pipelines (default ``[AnonymizeEDFHeader, StripAnnotationTextMiddleware]``):
        Free — file size is unchanged, returned directly.

    Signal pipelines (:class:`~federation.middleware.EDFSignalMiddleware`):
        Pure DB query — ``RecordingMeta.signals`` (prefetched by the caller)
        provides per-channel sample counts needed to compute the output record
        size without any filesystem reads.

    Full-file pipelines (:class:`~federation.middleware.EDFFullFileMiddleware`):
        ``compute_output_size()`` is called; free for simple multipliers.
    """
    recording_ct = ContentType.objects.get_for_model(Recording, for_concrete_model=False)

    # One batch query: which recordings does this peer access with apply_middleware?
    middleware_object_ids = set(
        AccessRight.objects.filter(
            federated_peer=peer,
            can_read=True,
            apply_middleware=True,
            content_type=recording_ct,
            object_id__in=[str(r.pk) for r in recordings],
        )
        .filter(Q(remote_user_id="") | Q(remote_user_id=remote_user_id))
        .values_list("object_id", flat=True)
    )

    # Server-side pipeline for the API scope — identical to what download_recording uses.
    pipeline = _build_serve_pipeline()

    result = {}
    for recording in recordings:
        if str(recording.pk) not in middleware_object_ids:
            result[recording.pk] = recording.file_size
            continue

        ext = (recording.file_extension or "").lower()
        if ext not in (".edf", ".bdf"):
            # Non-EDF files are never transformed; size is always unchanged.
            result[recording.pk] = recording.file_size
            continue

        meta = (meta_by_pk or {}).get(recording.pk)
        if meta is None or pipeline.is_empty:
            result[recording.pk] = recording.file_size
            continue

        header_size = 256 * (1 + meta.signal_count)

        if pipeline.is_size_preserving:
            # Pipeline does not change file size (isometric or size-invariant
            # signal transforms such as StripAnnotationTextMiddleware).
            result[recording.pk] = recording.file_size

        elif pipeline.has_signal_middleware:
            # Derive output size from DB signal infos — no filesystem read needed.
            try:
                signal_infos = sorted(meta.signals.all(), key=lambda s: s.index)
                bps = 3 if (meta.format or "").lower().startswith("bdf") else 2
                ctx = pipeline.build_signal_context_from_infos(signal_infos, bps, meta.data_record_count, header_size)
                result[recording.pk] = ctx.output_file_size
            except Exception as exc:
                logger.warning(
                    "Could not compute signal pipeline download_size for recording %s: %s",
                    recording.pk,
                    exc,
                )
                result[recording.pk] = recording.file_size

        else:
            # Full-file pipeline: delegate to compute_output_size.
            result[recording.pk] = pipeline.compute_output_size(recording.file_size, header_size)

    return result


def _parse_access_flags(raw_rights: str) -> tuple[bool, bool, bool, bool]:
    """Parse compact access-right string into read/write/share/middleware booleans.

    Recognised tokens (comma- or pipe-separated):
    - ``r`` / ``read``       → can_read
    - ``w`` / ``write``      → can_write
    - ``s`` / ``share``      → can_share
    - ``m`` / ``middleware`` → apply_middleware (pipe EDF content through the
                               configured middleware pipeline before serving)
    """

    normalized = (raw_rights or "").strip().lower().replace("|", ",")
    if not normalized:
        return True, False, False, False

    tokens = {item.strip() for item in normalized.split(",") if item.strip()}
    can_read = bool(tokens & {"r", "read"})
    can_write = bool(tokens & {"w", "write"})
    can_share = bool(tokens & {"s", "share"})
    apply_middleware = bool(tokens & {"m", "middleware"})

    if not (can_read or can_write or can_share):
        raise HttpError(
            400,
            "Invalid access rights. Use read/write/share (or r/w/s), optionally with middleware (m).",
        )

    return can_read, can_write, can_share, apply_middleware


def _parse_target_access_list(
    raw_value: str | None, target_label: str
) -> list[tuple[int, tuple[bool, bool, bool, bool]]]:
    """Parse semicolon-delimited target access list of form '<id>:<rights>'."""

    text = (raw_value or "").strip()
    if not text:
        return []

    assignments = []
    for chunk in [part.strip() for part in text.split(";") if part.strip()]:
        if ":" not in chunk:
            raise HttpError(
                400,
                f"Invalid {target_label} format: '{chunk}'. Expected '<id>:<rights>'.",
            )
        id_part, rights_part = chunk.split(":", 1)
        try:
            target_id = int(id_part.strip())
        except ValueError as exc:
            raise HttpError(400, f"Invalid {target_label} id: '{id_part}'.") from exc

        assignments.append((target_id, _parse_access_flags(rights_part)))

    seen: set[int] = set()
    duplicates: set[int] = set()
    for target_id, _ in assignments:
        if target_id in seen:
            duplicates.add(target_id)
        seen.add(target_id)
    if duplicates:
        # One AccessRight row per (object, target) — enforced by a database
        # constraint, so a repeated id would otherwise surface as a 500 at
        # create time. Refuse it here with the ids named.
        raise HttpError(400, f"Duplicate ids in {target_label}: {sorted(duplicates)}")

    return assignments


def _parse_optional_expiration(raw_value: str | None):
    """Parse optional ISO datetime string and normalize into aware datetime."""

    text = (raw_value or "").strip()
    if not text:
        return None

    parsed = parse_datetime(text)
    if parsed is None:
        raise HttpError(400, "Invalid share token expiration datetime. Use ISO format.")

    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, timezone.get_current_timezone())
    return parsed


def _serve_recording_file(request, file_path: Path, filename: str):
    """Serve a recording file with HTTP Range request support (RFC 7233).

    Returns 206 Partial Content when the caller supplies a ``Range: bytes=``
    header, or a plain 200 FileResponse for the full file.  The
    ``Accept-Ranges: bytes`` header is always included so clients know they
    may request ranges without a prior 416 probe.
    """
    file_size = file_path.stat().st_size
    range_header = request.META.get("HTTP_RANGE", "").strip()

    if not range_header:
        response = FileResponse(
            file_path.open("rb"),
            as_attachment=True,
            filename=filename,
            content_type="application/octet-stream",
        )
        response["Accept-Ranges"] = "bytes"
        response["Content-Length"] = str(file_size)
        return response

    # Parse "bytes=start-end"  (end is optional → means through EOF)
    match = re.fullmatch(r"bytes=(\d+)-(\d*)", range_header)
    if not match:
        response = HttpResponse(status=416)
        response["Content-Range"] = f"bytes */{file_size}"
        return response

    start = int(match.group(1))
    end = int(match.group(2)) if match.group(2) else file_size - 1

    if start > end or start >= file_size:
        response = HttpResponse(status=416)
        response["Content-Range"] = f"bytes */{file_size}"
        return response

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

    response = StreamingHttpResponse(_stream(), status=206, content_type="application/octet-stream")
    response["Content-Range"] = f"bytes {start}-{end}/{file_size}"
    response["Content-Length"] = str(length)
    response["Accept-Ranges"] = "bytes"
    response["Content-Disposition"] = content_disposition_header(as_attachment=True, filename=filename)
    return response


def _build_serve_pipeline():
    """Return the API-scope middleware pipeline used for download and size computation.

    Always applies :class:`~federation.middleware.AnonymizeEDFHeader` followed
    by :class:`~federation.middleware.StripAnnotationTextMiddleware` so that
    clinical annotation text is never transmitted to ``apply_middleware``
    consumers (federated peers and other grantees).

    :func:`_serve_recording_with_middleware`,
    :func:`_compute_download_sizes_for_peer`, and
    :func:`_serve_recording_slice` all call this function so that the
    pipeline stays in sync across every serving path.
    """
    from federation.middleware import (
        AnonymizeEDFHeader,
        MiddlewarePipeline,
        StripAnnotationTextMiddleware,
    )

    return MiddlewarePipeline([AnonymizeEDFHeader(), StripAnnotationTextMiddleware()]).for_scope("api")


def _serve_recording_with_middleware(request, file_path: Path, filename: str, recording) -> object:
    """Serve an EDF/BDF recording with the API-scope middleware pipeline applied.

    Three serving strategies, tried in order:

    1. **Signal middleware** (:class:`~federation.middleware.EDFSignalMiddleware`) —
       the header is rewritten and each data record is transformed independently.
       Only the records overlapping the requested byte range are read from disk.
       No full-file buffering.  Supports Range requests correctly even when the
       output file size differs from the input.

    2. **Isometric** (header-only
       :class:`~federation.middleware.EDFHeaderMiddleware`) — the header is
       transformed in memory and signal bytes are streamed raw.  File size is
       unchanged so Range requests work with no extra logic.

    3. **Raw fallback** — used when the pipeline is empty after scope filtering
       or the file is not EDF/BDF.  :class:`~federation.middleware.EDFFullFileMiddleware`
       pipelines also fall back here — full-file buffering on the HTTP path is
       intentionally not supported.

    When ``RecordingMeta`` is missing the function refuses with **403** and the
    structured code ``recording_unprocessed`` rather than falling back to raw
    bytes.  This is the only branch where the caller asked for anonymisation
    and the server cannot satisfy the request — serving the original here
    would leak the unrewritten EDF/BDF header to a grantee whose grant
    specifically requires middleware to apply.  Fires for ``status=FAILED``
    recordings (processing didn't write meta) and the rare partial-ingest
    race; never fires for ``status=READY`` because successful processing
    always writes meta.
    """
    pipeline = _build_serve_pipeline()

    if pipeline.is_empty:
        return _serve_recording_file(request, file_path, filename)

    ext = (recording.file_extension or "").lower()
    if ext not in (".edf", ".bdf"):
        return _serve_recording_file(request, file_path, filename)

    from recordings.models import RecordingMeta

    recording_ct = ContentType.objects.get_for_model(recording, for_concrete_model=False)
    meta = RecordingMeta.objects.filter(
        content_type=recording_ct,
        object_id=str(recording.pk),
    ).first()
    if meta is None:
        return JsonResponse(
            {
                "code": "recording_unprocessed",
                "detail": ("This recording could not be processed and cannot be served in anonymised form."),
            },
            status=403,
        )

    header_size = 256 * (1 + meta.signal_count)
    file_size = file_path.stat().st_size
    if header_size > file_size:
        return _serve_recording_file(request, file_path, filename)

    with file_path.open("rb") as f:
        raw_header = f.read(header_size)

    # ── Strategy 1: signal middleware ────────────────────────────────────────
    if pipeline.has_signal_middleware:
        from recordings.processors.edf import parse_edf_header

        n_records = parse_edf_header(raw_header).data_record_count
        ctx = pipeline.build_signal_context(raw_header, n_records)
        return _serve_signal_pipeline(request, file_path, filename, ctx, header_size)

    # ── Strategy 2: isometric (header-only) pipeline ─────────────────────────
    if pipeline.is_isometric:
        transformed_header = pipeline.apply_header(raw_header)
        return _serve_isometric_pipeline(request, file_path, filename, transformed_header, header_size, file_size)

    # ── Strategy 3: EDFFullFileMiddleware — refuse ───────────────────────────
    # Full-file buffering on the HTTP path is intentionally not supported;
    # use EDFSignalMiddleware for transforms that change record size. This
    # branch is unreachable with the default _build_serve_pipeline, but a
    # fork that configures a full-file de-identifier must get a refusal
    # here — falling back to raw bytes would silently serve un-sanitised
    # PHI with a 200 while every test on the configured middleware passes.
    logger.error(
        "EDFFullFileMiddleware is not supported on the HTTP download path; refusing to serve recording %s",
        recording.pk,
    )
    return JsonResponse(
        {
            "code": "middleware_unsupported",
            "detail": ("This recording's configured transform cannot be applied on the download path."),
        },
        status=403,
    )


def _serve_isometric_pipeline(
    request,
    file_path: Path,
    filename: str,
    transformed_header: bytes,
    header_size: int,
    file_size: int,
) -> object:
    """Serve an isometric-pipeline response: transformed header + raw signal stream."""

    range_header = request.META.get("HTTP_RANGE", "").strip()

    if not range_header:

        def _stream_full():
            yield transformed_header
            chunk_size = 64 * 1024
            with file_path.open("rb") as fh:
                fh.seek(header_size)
                while True:
                    data = fh.read(chunk_size)
                    if not data:
                        break
                    yield data

        response = StreamingHttpResponse(_stream_full(), content_type="application/octet-stream")
        response["Content-Disposition"] = content_disposition_header(as_attachment=True, filename=filename)
        response["Content-Length"] = str(file_size)
        response["Accept-Ranges"] = "bytes"
        return response

    match = re.fullmatch(r"bytes=(\d+)-(\d*)", range_header)
    if not match:
        response = HttpResponse(status=416)
        response["Content-Range"] = f"bytes */{file_size}"
        return response

    start = int(match.group(1))
    end = int(match.group(2)) if match.group(2) else file_size - 1
    if start > end or start >= file_size:
        response = HttpResponse(status=416)
        response["Content-Range"] = f"bytes */{file_size}"
        return response
    end = min(end, file_size - 1)
    length = end - start + 1

    def _stream_range():
        pos = start
        remaining = length
        if pos < header_size:
            hdr_end = min(end, header_size - 1)
            chunk = transformed_header[pos : hdr_end + 1]
            remaining -= len(chunk)
            pos = hdr_end + 1
            yield chunk
        if remaining > 0:
            chunk_size = 64 * 1024
            with file_path.open("rb") as fh:
                fh.seek(pos)
                while remaining > 0:
                    data = fh.read(min(chunk_size, remaining))
                    if not data:
                        break
                    remaining -= len(data)
                    yield data

    response = StreamingHttpResponse(_stream_range(), status=206, content_type="application/octet-stream")
    response["Content-Range"] = f"bytes {start}-{end}/{file_size}"
    response["Content-Length"] = str(length)
    response["Accept-Ranges"] = "bytes"
    response["Content-Disposition"] = content_disposition_header(as_attachment=True, filename=filename)
    return response


def _serve_signal_pipeline(
    request,
    file_path: Path,
    filename: str,
    ctx,
    original_header_size: int,
) -> object:
    """Serve a signal-middleware response using per-record transformation.

    Maps output byte ranges back to input record indices and transforms only
    the overlapping records.  The output file size may differ from the input.
    """
    output_size = ctx.output_file_size
    range_header = request.META.get("HTTP_RANGE", "").strip()

    if not range_header:

        def _stream_full():
            yield ctx.new_header
            with file_path.open("rb") as fh:
                fh.seek(original_header_size)
                for _ in range(ctx.n_records):
                    rec = fh.read(ctx.input_record_size)
                    yield ctx.transform_record(rec)

        response = StreamingHttpResponse(_stream_full(), content_type="application/octet-stream")
        response["Content-Disposition"] = content_disposition_header(as_attachment=True, filename=filename)
        response["Content-Length"] = str(output_size)
        response["Accept-Ranges"] = "bytes"
        return response

    match = re.fullmatch(r"bytes=(\d+)-(\d*)", range_header)
    if not match:
        response = HttpResponse(status=416)
        response["Content-Range"] = f"bytes */{output_size}"
        return response

    start = int(match.group(1))
    end = int(match.group(2)) if match.group(2) else output_size - 1
    if start > end or start >= output_size:
        response = HttpResponse(status=416)
        response["Content-Range"] = f"bytes */{output_size}"
        return response
    end = min(end, output_size - 1)
    length = end - start + 1

    new_hdr_size = ctx.new_header_size

    def _stream_range():
        pos = start
        remaining = length

        # Header portion.
        if pos < new_hdr_size:
            hdr_end = min(end, new_hdr_size - 1)
            chunk = ctx.new_header[pos : hdr_end + 1]
            remaining -= len(chunk)
            pos = hdr_end + 1
            yield chunk

        if remaining <= 0:
            return

        # Signal portion: map output offsets → input record indices.
        out_sig_start = pos - new_hdr_size
        out_sig_end = end - new_hdr_size
        first_rec = out_sig_start // ctx.output_record_size
        last_rec = out_sig_end // ctx.output_record_size

        transformed = bytearray()
        with file_path.open("rb") as fh:
            for rec_idx in range(first_rec, last_rec + 1):
                fh.seek(original_header_size + rec_idx * ctx.input_record_size)
                rec_bytes = fh.read(ctx.input_record_size)
                transformed += ctx.transform_record(rec_bytes)

        slice_start = out_sig_start - first_rec * ctx.output_record_size
        slice_end = slice_start + (out_sig_end - out_sig_start + 1)
        yield bytes(transformed[slice_start:slice_end])

    response = StreamingHttpResponse(_stream_range(), status=206, content_type="application/octet-stream")
    response["Content-Range"] = f"bytes {start}-{end}/{output_size}"
    response["Content-Length"] = str(length)
    response["Accept-Ranges"] = "bytes"
    response["Content-Disposition"] = content_disposition_header(as_attachment=True, filename=filename)
    return response


# EDF/BDF fixed-header layout constants (all offsets in bytes).
_NRECS_OFFSET = 236  # byte offset of the 8-char data_record_count field
_NRECS_WIDTH = 8


def _patch_record_count(header_bytes: bytes, n_records: int) -> bytes:
    """Return a copy of *header_bytes* with data_record_count set to *n_records*.

    The field at offset 236 is overwritten in-place (as ASCII, space-padded to
    8 bytes).  All other header bytes are preserved unchanged, so the function
    works on both raw and anonymised headers.
    """
    nrecs = str(n_records).ljust(_NRECS_WIDTH).encode("ascii")
    return header_bytes[:_NRECS_OFFSET] + nrecs + header_bytes[_NRECS_OFFSET + _NRECS_WIDTH :]


def _serve_recording_slice(
    file_path: Path,
    filename: str,
    first_rec: int,
    n_slice_records: int,
    header_size: int,
    input_record_size: int,
    apply_middleware: bool,
    pipeline=None,
) -> object:
    """Stream a time-range slice as a self-contained EDF/BDF file.

    Builds the slice header (patching data_record_count to *n_slice_records*),
    then streams the *n_slice_records* records starting at *first_rec*.

    Three serving strategies, mirroring the full-file download path:

    1. **Signal middleware** — per-record transform via
       :class:`~federation.middleware.EDFSignalMiddleware`; the transformed
       header from :class:`~federation.middleware.SignalPipelineContext` is
       used as the base.
    2. **Isometric** — header anonymised, signal bytes streamed raw.
    3. **Raw** — no transform; original header and records served verbatim.
    """
    from federation.middleware import MiddlewarePipeline

    with file_path.open("rb") as f:
        raw_header = f.read(header_size)

    if pipeline is None:
        if apply_middleware:
            pipeline = _build_serve_pipeline()
        else:
            pipeline = MiddlewarePipeline([])

    if pipeline.has_signal_middleware:
        from recordings.processors.edf import parse_edf_header

        n_total = parse_edf_header(raw_header).data_record_count
        ctx = pipeline.build_signal_context(raw_header, n_total)
        slice_header = _patch_record_count(ctx.new_header, n_slice_records)
        out_record_size = ctx.output_record_size

        def _iter_records():
            with file_path.open("rb") as fh:
                for r in range(first_rec, first_rec + n_slice_records):
                    fh.seek(header_size + r * input_record_size)
                    yield ctx.transform_record(fh.read(input_record_size))
    else:
        if not pipeline.is_empty:
            base_header = pipeline.apply_header(raw_header)
        else:
            base_header = raw_header
        slice_header = _patch_record_count(base_header, n_slice_records)
        out_record_size = input_record_size

        def _iter_records():
            chunk_size = 64 * 1024
            with file_path.open("rb") as fh:
                fh.seek(header_size + first_rec * input_record_size)
                remaining = n_slice_records * input_record_size
                while remaining > 0:
                    data = fh.read(min(chunk_size, remaining))
                    if not data:
                        break
                    remaining -= len(data)
                    yield data

    output_size = len(slice_header) + n_slice_records * out_record_size

    def _stream():
        yield slice_header
        yield from _iter_records()

    stem = Path(filename).stem
    ext = Path(filename).suffix.lstrip(".")
    slice_filename = f"{stem}_r{first_rec}-{first_rec + n_slice_records - 1}.{ext}"

    response = StreamingHttpResponse(_stream(), content_type="application/octet-stream")
    response["Content-Length"] = str(output_size)
    response["Content-Disposition"] = content_disposition_header(as_attachment=True, filename=slice_filename)
    return response


def _build_recording_out(
    recording,
    meta_by_pk: dict | None = None,
    download_size: int | None = None,
    *,
    user=None,
    fed=None,
    trashed_collection_by_pk: dict | None = None,
) -> dict:
    """Assemble a RecordingOut-compatible dict for a single Recording instance.

    *download_size* is the server-side post-pipeline byte count for this
    specific requester.  Pass ``None`` (default) when the requester is not a
    federated peer or when the pipeline is a no-op; the field is omitted from
    the response in that case.

    *user* and *fed* control viewer-conditional fields.  When neither is
    supplied, ``original_name`` is omitted from the response — call sites
    that have already established authorship pass the *user* so the author
    sees their original filename.
    """
    meta_obj = (meta_by_pk or {}).get(recording.pk)
    can_see_author_fields = _can_see_original_name(user, recording, fed)
    out = {
        "hash": recording.stored_name.split(".", 1)[0],
        "original_name": (recording.original_name if can_see_author_fields else None),
        "display_name": _resolve_display_name(recording),
        "has_custom_name": _has_custom_display_name(recording),
        "processing_error": ((recording.processing_error or None) if can_see_author_fields else None),
        "file_extension": recording.file_extension,
        "file_size": recording.file_size,
        "file_hash": recording.file_hash,
        "content_hash": recording.content_hash,
        "status": recording.status,
        "modality": recording.modality,
        "created_at": recording.created_at,
        "deleted_at": recording.deleted_at,
        "meta": {
            "format": meta_obj.format,
            "duration": meta_obj.duration,
            "data_record_count": meta_obj.data_record_count,
            "data_record_duration": meta_obj.data_record_duration,
            "signal_count": meta_obj.signal_count,
            "discontinuous": meta_obj.discontinuous,
            "channel_layout": meta_obj.channel_layout,
            "unresolved_channel_count": meta_obj.unresolved_channel_count,
            "channel_order_version": meta_obj.channel_order_version,
            "signals": [
                {
                    "index": si.index,
                    "label": si.label,
                    "sample_count": si.sample_count,
                    "sampling_rate": si.sampling_rate,
                    "is_annotation_channel": si.is_annotation_channel,
                    "signal_type": si.signal_type,
                    "physical_unit": si.physical_unit,
                    "physical_min": si.physical_min,
                    "physical_max": si.physical_max,
                    "digital_min": si.digital_min,
                    "digital_max": si.digital_max,
                    "transducer_type": si.transducer_type,
                    "prefiltering": si.prefiltering,
                    "source_label": (si.source_label if can_see_author_fields else None),
                    "source_transducer_type": (si.source_transducer_type if can_see_author_fields else None),
                    "source_prefiltering": (si.source_prefiltering if can_see_author_fields else None),
                    "source_index": (si.source_index if can_see_author_fields else None),
                }
                for si in sorted(meta_obj.signals.all(), key=lambda s: s.index)
            ],
        }
        if meta_obj
        else None,
        "events": [{"object_hash": e.object_hash} for e in recording.events.all()],
        "interruptions": [
            {"object_hash": i.object_hash, "start": i.timestamp, "duration": i.duration or 0.0}
            for i in recording.interruptions.all()
        ],
        "labels": [{"object_hash": lbl.object_hash} for lbl in recording.labels.all()],
        "trashed_collection": (trashed_collection_by_pk or {}).get(recording.pk),
    }
    if download_size is not None:
        out["download_size"] = download_size
    return out


def _clamp_annotation(
    timestamp: float,
    duration: float | None,
    t_start: float,
    t_end: float,
) -> tuple[float, float | None]:
    """Return (sliced_timestamp, sliced_duration) relative to *t_start*, clamped to the window.

    The returned timestamp is always ≥ 0.  For annotations with a duration the
    returned duration is clamped so the annotation does not extend past *t_end*.
    Point annotations (``duration=None``) are returned unchanged except for the
    shift.
    """
    sliced_ts = max(0.0, timestamp - t_start)
    if duration is None:
        return sliced_ts, None
    eff_end = min(timestamp + duration, t_end)
    sliced_dur = max(0.0, eff_end - max(timestamp, t_start))
    return sliced_ts, sliced_dur


@api.post("/upload", response={202: RecordingUploadOut})
def upload_recording(
    request,
    file: UploadedFile = File(...),
    user_access: str | None = None,
    group_access: str | None = None,
    share_token: str | None = None,
    share_token_expires_at: str | None = None,
    share_token_apply_middleware: bool = True,
    preserve_annotations: bool = False,
    display_name: str | None = None,
):
    """Save uploaded file to staging and enqueue background processing.

    Returns 202 Accepted immediately. Poll GET /status/{hash} until
    status transitions from pending → processing → ready before downloading.

    The optional ``display_name`` parameter sets the grantee-visible label
    for the recording.  When omitted, the field is left null and responses
    fall back to a hash-prefix default; the original filename is never used
    as the display name unless the author explicitly opts in by passing it
    here (or via a later PATCH).
    """

    user = _require_auth(request)
    user_assignments = _parse_target_access_list(user_access, "user_access")
    group_assignments = _parse_target_access_list(group_access, "group_access")
    if any(target_id == user.pk for target_id, _ in user_assignments):
        # The uploader's own full-rights row is created unconditionally below;
        # a second row for the same target would violate the per-target
        # uniqueness constraint, and silently dropping the entry would hide
        # that the requested flags (e.g. apply_middleware) never apply to the
        # author. Refuse instead.
        raise HttpError(400, "user_access must not include the uploading user.")

    normalized_share_token = (share_token or "").strip() or None
    token_expiration = _parse_optional_expiration(share_token_expires_at)
    if token_expiration and not normalized_share_token:
        raise HttpError(400, "share_token_expires_at requires share_token.")
    if normalized_share_token and AccessRight.objects.filter(public_share_token=normalized_share_token).exists():
        # The token column is globally unique; without this check the collision
        # surfaces as an IntegrityError 500 after the file has been streamed.
        # Same answer as the library grant surface.
        raise HttpError(409, "This share token is already in use.")

    staging_root = Path(settings.RECORDINGS_STAGING_PATH)
    if not staging_root.is_absolute():
        staging_root = Path(settings.BASE_DIR) / staging_root
    staging_root.mkdir(parents=True, exist_ok=True)

    original_name = file.name or "upload.bin"
    # Lower-case the extension before persisting. Loader-name resolution downstream
    # (and the converter / processor pipelines) match by lower-case extension, and
    # an upload with an upper-case suffix (e.g. ``.EDF``) would otherwise round-trip
    # the upper-case form into ``file_extension`` and produce loader lookups like
    # ``eeg/EDF-file`` that don't match any registered importer.
    extension = Path(original_name).suffix.lower()
    if extension not in _EDF_EXTENSIONS and get_converter(extension) is None:
        supported = ", ".join(sorted(_EDF_EXTENSIONS))
        raise HttpError(
            400,
            f"Unsupported file type {extension or '(none)'!r}. Upload {supported}, "
            f"or a format with a registered converter.",
        )

    stored_name = f"{secrets.token_hex(16).upper()}{extension}"
    while (staging_root / stored_name).exists():
        stored_name = f"{secrets.token_hex(16).upper()}{extension}"

    staging_path = staging_root / stored_name

    max_size = getattr(settings, "RECORDINGS_MAX_UPLOAD_SIZE", 2 * 1024 * 1024 * 1024)
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

    # Validate user/group IDs before opening a transaction.
    UserModel = get_user_model()
    user_ids = [target_id for target_id, _ in user_assignments]
    if user_ids:
        existing_user_ids = set(UserModel.objects.filter(id__in=user_ids).values_list("id", flat=True))
        missing_user_ids = sorted(set(user_ids) - existing_user_ids)
        if missing_user_ids:
            staging_path.unlink(missing_ok=True)
            raise HttpError(400, f"Unknown user ids in user_access: {missing_user_ids}")

    group_ids = [target_id for target_id, _ in group_assignments]
    if group_ids:
        existing_group_ids = set(Group.objects.filter(id__in=group_ids).values_list("id", flat=True))
        missing_group_ids = sorted(set(group_ids) - existing_group_ids)
        if missing_group_ids:
            staging_path.unlink(missing_ok=True)
            raise HttpError(400, f"Unknown group ids in group_access: {missing_group_ids}")

    # Create the Recording row and all AccessRights atomically so a partial
    # failure never leaves a row without its owner's access right. Enqueue the
    # processing task only after the transaction commits so the worker can
    # always find the row.
    from recordings.tasks import process_recording

    normalized_display_name = (display_name or "").strip() or None

    # Refused rather than silently downgraded: a client wired to always send the
    # flag should find out, instead of uploading recordings that quietly differ
    # from what it believes it asked for. The prohibition itself is enforced at
    # the point of use, not here — see _process_recording_body.
    if preserve_annotations and not getattr(settings, "RECORDINGS_ALLOW_PRESERVE_ANNOTATIONS", True):
        staging_path.unlink(missing_ok=True)
        raise HttpError(400, "preserve_annotations is not permitted on this deployment.")

    # Resolved before create() so a discarded filename is never written at all
    # rather than scrubbed afterwards. The rule itself lives in
    # recordings.models, so every route that creates a Recording shares one
    # implementation — this one did not, and import_recordings kept writing real
    # patient filenames on a deployment that had turned the setting on.
    name_for_db = stored_original_name(original_name, extension)

    with transaction.atomic():
        recording = Recording.objects.create(
            author=user,
            original_name=name_for_db,
            display_name=normalized_display_name,
            stored_name=stored_name,
            file_extension=extension,
            file_size=total_size,
            file_path=str(staging_path),
            file_hash=file_hash,
            content_hash="",
            status=Recording.Status.PENDING,
        )

        recording_ct = ContentType.objects.get_for_model(recording, for_concrete_model=False)

        AccessRight.objects.create(
            content_type=recording_ct,
            object_id=str(recording.pk),
            access_giver=user,
            access_target=user,
            can_read=True,
            can_write=True,
            can_share=True,
        )

        for target_id, (
            can_read,
            can_write,
            can_share,
            apply_middleware,
        ) in user_assignments:
            AccessRight.objects.create(
                content_type=recording_ct,
                object_id=str(recording.pk),
                access_giver=user,
                access_target_id=target_id,
                can_read=can_read,
                can_write=can_write,
                can_share=can_share,
                apply_middleware=apply_middleware,
            )

        for target_id, (
            can_read,
            can_write,
            can_share,
            apply_middleware,
        ) in group_assignments:
            AccessRight.objects.create(
                content_type=recording_ct,
                object_id=str(recording.pk),
                access_giver=user,
                access_target_group_id=target_id,
                can_read=can_read,
                can_write=can_write,
                can_share=can_share,
                apply_middleware=apply_middleware,
            )

        if normalized_share_token:
            AccessRight.objects.create(
                content_type=recording_ct,
                object_id=str(recording.pk),
                access_giver=user,
                public_share_token=normalized_share_token,
                expires_at=token_expiration,
                can_read=True,
                can_write=False,
                can_share=False,
                apply_middleware=share_token_apply_middleware,
            )

        log_activity(
            verb="recordings.upload",
            target=recording,
            metadata={
                "granted_user_access_count": len(user_assignments),
                "granted_group_access_count": len(group_assignments),
                "granted_share_token": bool(normalized_share_token),
            },
        )

        recording_id = recording.pk
        _preserve = preserve_annotations
        transaction.on_commit(lambda: process_recording.delay(recording_id, preserve_annotations=_preserve))

    return 202, {
        "original_name": recording.original_name,
        "display_name": _resolve_display_name(recording),
        "stored_name": recording.stored_name,
        "file_extension": recording.file_extension,
        "file_size": recording.file_size,
        "file_hash": recording.file_hash,
        "status": recording.status,
    }


@api.get("/status/{hash}", response=RecordingStatusOut)
def recording_status(request, hash: str):
    """Return the processing status of a recording. Lightweight — suitable for polling.

    Available immediately after upload. Transitions: pending → processing → ready.
    A recording that disappeared (row deleted due to processing error) returns 404.
    """

    user, fed = _require_auth_or_federated(request)
    normalized_hash = (hash or "").strip().upper()
    if len(normalized_hash) != 32 or not normalized_hash.isalnum():
        raise HttpError(400, "Invalid recording hash.")

    recording = (
        Recording.objects.filter(stored_name__startswith=f"{normalized_hash}.", deleted_at__isnull=True)
        .only("id", "status", "author_id")
        .order_by("-created_at")
        .first()
    )
    if recording is None:
        raise HttpError(404, "Recording not found")

    if fed is not None:
        fed_peer, remote_user_id = fed
        if _failed_hidden_for_caller(recording, user, fed):
            log_federation_access(
                peer=fed_peer,
                remote_user_id=remote_user_id,
                action="recording_status",
                target=recording,
                status_code=404,
            )
            raise HttpError(404, "Recording not found")
        granted = get_federated_read_access_result(fed_peer, remote_user_id, recording).granted
        if not granted:
            log_federation_access(
                peer=fed_peer,
                remote_user_id=remote_user_id,
                action="recording_status",
                target=recording,
                status_code=403,
            )
            raise HttpError(403, "You do not have permission to view this recording")
        log_federation_access(
            peer=fed_peer,
            remote_user_id=remote_user_id,
            action="recording_status",
            target=recording,
            status_code=200,
        )
    elif _failed_hidden_for_caller(recording, user, None):
        raise HttpError(404, "Recording not found")
    elif not (
        getattr(user, "is_superuser", False)
        or recording.author_id == user.pk
        or can_read_object(user=user, obj=recording)
    ):
        raise HttpError(403, "You do not have permission to view this recording")

    if _failed_hidden_for_caller(recording, user, fed):
        raise HttpError(404, "Recording not found")

    log_activity(
        verb="recordings.status",
        target=recording,
        metadata={"status": recording.status},
    )
    return {"status": recording.status}


@api.get("/", response=list[RecordingOut])
def list_recordings(
    request,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    trash: bool = Query(False, description="List soft-deleted recordings instead of active ones"),
    status: str | None = Query(None, description="Filter by status: pending, processing, ready"),
    uncollected: bool = Query(
        False,
        description="List only recordings not in any collection (the library root)",
    ),
):
    """List recordings visible to caller based on author/admin/read-access rights.

    Pass trash=true to browse the recycle bin. Only the recording's author and
    superusers can see deleted recordings; shared access does not extend to trash.
    Pass status=ready to show only fully processed recordings. Pass
    uncollected=true to list only recordings that sit in no collection.
    """

    user, fed = _require_auth_or_federated(request)

    # Federated peers cannot browse trash — they have no concept of ownership.
    if fed is not None and trash:
        fed_peer, remote_user_id = fed
        log_federation_access(
            peer=fed_peer,
            remote_user_id=remote_user_id,
            action="list_recordings",
            target=None,
            status_code=403,
        )
        raise HttpError(403, "Federated peers cannot browse the recycle bin")

    queryset = Recording.objects.filter(deleted_at__isnull=not trash).order_by("-created_at")

    if status is not None:
        normalized_status = status.strip().lower()
        valid_statuses = {s.value for s in Recording.Status}
        if normalized_status not in valid_statuses:
            raise HttpError(
                400,
                f"Invalid status '{status}'. Use one of: {', '.join(sorted(valid_statuses))}.",
            )
        queryset = queryset.filter(status=normalized_status)

    if uncollected:
        # "Library root": recordings not filed in any *live* collection. A
        # membership in a trashed collection is soft-deleted, so a recording whose
        # only collection is in the trash surfaces at the root (and stays a
        # deletable, re-fileable object) until its collection is restored. Ids are
        # materialised and matched by pk — a NULL-deleted_at check on the joined
        # row would also drop no-membership recordings, and this avoids a
        # varchar↔int comparison on object_id.
        from library.models import CollectionItem

        recording_ct = ContentType.objects.get_for_model(Recording, for_concrete_model=False)
        live_collected_ids = list(
            CollectionItem.objects.filter(content_type=recording_ct, deleted_at__isnull=True).values_list(
                "object_id", flat=True
            )
        )
        queryset = queryset.exclude(pk__in=live_collected_ids)

    if fed is not None:
        # Federated listing: return only recordings covered by an explicit grant
        # for this peer + remote user.  A single AccessRight batch query replaces
        # the per-recording can_read_object loop used for local users.
        fed_peer, remote_user_id = fed
        recording_ct = ContentType.objects.get_for_model(Recording, for_concrete_model=False)
        now = timezone.now()
        granted_ids = set(
            AccessRight.objects.filter(
                federated_peer=fed_peer,
                can_read=True,
                content_type=recording_ct,
            )
            .filter(Q(remote_user_id="") | Q(remote_user_id=remote_user_id))
            .filter(Q(expires_at__isnull=True) | Q(expires_at__gt=now))
            .values_list("object_id", flat=True)
        )
        visible = list(
            queryset.filter(pk__in=granted_ids).exclude(status=Recording.Status.FAILED)[offset : offset + limit]
        )
    elif getattr(user, "is_superuser", False):
        visible = list(queryset[offset : offset + limit])
    elif trash:
        # Trash is personal: only show the caller's own deleted recordings.
        visible = list(queryset.filter(author=user)[offset : offset + limit])
    else:
        visible = []
        skipped_visible = 0
        for recording in queryset.iterator():
            is_author = recording.author_id == user.pk
            # FAILED uploads are hidden from grantees so processing-failure
            # surface area does not leak to anyone but the author and superusers.
            if not is_author and recording.status == Recording.Status.FAILED:
                continue
            if is_author or can_read_object(user=user, obj=recording):
                if skipped_visible < offset:
                    skipped_visible += 1
                    continue
                visible.append(recording)
            if len(visible) >= limit:
                break

    prefetch_related_objects(visible, "events", "interruptions", "labels")

    # Batch-fetch RecordingMeta (with per-channel SignalInfo) to avoid N+1 queries.
    from recordings.models import RecordingMeta

    recording_ct = ContentType.objects.get_for_model(Recording, for_concrete_model=False)
    meta_by_pk = {
        int(m.object_id): m
        for m in RecordingMeta.objects.filter(
            content_type=recording_ct,
            object_id__in=[str(r.pk) for r in visible],
        ).prefetch_related("signals")
    }

    # For federated requests, include per-recording download_size so the
    # mounting instance does not need to infer the server's pipeline output size.
    # For the default isometric pipeline (AnonymizeEDFHeader) this equals
    # file_size at zero extra cost.  Signal pipelines require one EDF header disk
    # read per affected recording — see _compute_download_sizes_for_peer.
    download_sizes: dict | None = None
    if fed is not None:
        download_sizes = _compute_download_sizes_for_peer(visible, fed_peer, remote_user_id, meta_by_pk)
        # List endpoint discloses *existence* of visible recordings, not their
        # content — one summary row per call rather than one per recording
        # listed, to keep the audit table compact.  Forensics that needs the
        # exact recording set listed at a given moment can reconstruct it from
        # the AccessRight table at that timestamp.
        log_federation_access(
            peer=fed_peer,
            remote_user_id=remote_user_id,
            action="list_recordings",
            target=None,
            status_code=200,
        )

    log_activity(
        verb="recordings.list",
        metadata={
            "limit": limit,
            "offset": offset,
            "trash": trash,
            "status_filter": status,
            "uncollected": uncollected,
            "returned_count": len(visible),
        },
    )

    # Cue: for recordings surfaced at the root only because their collection is
    # trashed, note which trashed collection they will return to on restore.
    # Scoped to the caller's own collections — the name is author-private free
    # text (PHI-shaped like original_name), and only the owner can restore it, so
    # a grantee/author/federated caller who can read the recording but not the
    # collection it was filed into by another user must not see the name.
    trashed_collection_by_pk: dict = {}
    if uncollected and visible and user is not None:
        from library.models import CollectionItem

        rec_ct = ContentType.objects.get_for_model(Recording, for_concrete_model=False)
        for membership in CollectionItem.objects.filter(
            content_type=rec_ct,
            object_id__in=[str(r.pk) for r in visible],
            deleted_at__isnull=False,
            collection__deleted_at__isnull=False,
            collection__author_id=user.pk,
        ).select_related("collection"):
            if membership.object_id.isdigit():
                trashed_collection_by_pk[int(membership.object_id)] = {
                    "id": membership.collection_id,
                    "name": membership.collection.name,
                }

    return [
        _build_recording_out(
            r,
            meta_by_pk,
            download_size=download_sizes.get(r.pk) if download_sizes else None,
            user=user,
            fed=fed,
            trashed_collection_by_pk=trashed_collection_by_pk,
        )
        for r in visible
    ]


@api.get("/{hash}/slice", response=RecordingSliceOut)
def recording_detail_slice(
    request,
    hash: str,
    t_start: float = Query(
        default=0.0,
        description=(
            "Start of the requested window in seconds from the beginning of the recording. "
            "Negative values are relative to the end (e.g. -60 = last 60 s)."
        ),
    ),
    t_end: float | None = Query(
        default=None,
        description=(
            "End of the requested window in seconds. Negative values are relative to the "
            "end of the recording. Defaults to the end of the recording."
        ),
    ),
):
    """Return recording metadata scoped to a time-range slice.

    The ``meta`` fields ``duration`` and ``data_record_count`` reflect the
    record-aligned slice window.  Events and interruptions that overlap
    ``[t_start, t_end)`` are included; their timestamps are shifted to be
    relative to the slice start and durations are clamped at the slice
    boundaries.  Labels and free-form annotation bundles are excluded because
    their scope cannot be reliably inferred for a sub-range.

    Only EDF and BDF files are supported.  Other formats return **422**.
    Negative offset semantics and record alignment match ``GET /{hash}/slice``.
    """
    user, fed = _require_auth_or_federated(request)
    normalized_hash = (hash or "").strip().upper()
    if len(normalized_hash) != 32 or not normalized_hash.isalnum():
        raise HttpError(400, "Invalid recording hash.")

    recording = (
        Recording.objects.filter(stored_name__startswith=f"{normalized_hash}.", deleted_at__isnull=True)
        .order_by("-created_at")
        .first()
    )
    if recording is None:
        raise HttpError(404, "Recording not found")

    if fed is not None:
        fed_peer, remote_user_id = fed
        if _failed_hidden_for_caller(recording, user, fed):
            log_federation_access(
                peer=fed_peer,
                remote_user_id=remote_user_id,
                action="recording_detail_slice",
                target=recording,
                status_code=404,
            )
            raise HttpError(404, "Recording not found")
        granted = get_federated_read_access_result(fed_peer, remote_user_id, recording).granted
        if not granted:
            log_federation_access(
                peer=fed_peer,
                remote_user_id=remote_user_id,
                action="recording_detail_slice",
                target=recording,
                status_code=403,
            )
            raise HttpError(403, "You do not have permission to view this recording")
        log_federation_access(
            peer=fed_peer,
            remote_user_id=remote_user_id,
            action="recording_detail_slice",
            target=recording,
            status_code=200,
        )
    elif _failed_hidden_for_caller(recording, user, None):
        raise HttpError(404, "Recording not found")
    elif not (
        getattr(user, "is_superuser", False)
        or recording.author_id == user.pk
        or can_read_object(user=user, obj=recording)
    ):
        raise HttpError(403, "You do not have permission to view this recording")

    if _failed_hidden_for_caller(recording, user, fed):
        raise HttpError(404, "Recording not found")

    ext = (recording.file_extension or "").lower()
    if ext not in (".edf", ".bdf"):
        raise HttpError(422, "Time-range slicing is only supported for EDF and BDF files")

    from recordings.models import RecordingMeta

    recording_ct = ContentType.objects.get_for_model(recording, for_concrete_model=False)
    meta = RecordingMeta.objects.filter(
        content_type=recording_ct,
        object_id=str(recording.pk),
    ).first()
    if meta is None or not meta.data_record_count or not meta.data_record_duration:
        raise HttpError(422, "Recording metadata not available for time-range slicing")

    dr = float(meta.data_record_duration)
    n_records = meta.data_record_count
    total_duration = n_records * dr

    # Resolve negative offsets and apply defaults.
    if t_start < 0:
        t_start = total_duration + t_start
    if t_end is None:
        t_end = total_duration
    elif t_end < 0:
        t_end = total_duration + t_end

    t_start = max(0.0, min(float(t_start), total_duration))
    t_end = max(0.0, min(float(t_end), total_duration))

    if t_start >= t_end:
        raise HttpError(400, "t_start must be less than t_end")

    # Record-aligned slice boundaries.
    first_rec = max(0, int(t_start / dr))
    last_rec = min(n_records - 1, max(first_rec, math.ceil(t_end / dr) - 1))
    n_slice_records = last_rec - first_rec + 1
    actual_t_start = first_rec * dr
    actual_t_end = (last_rec + 1) * dr

    from annotations.models import Event, Interruption

    def _overlaps(ts: float, dur: float | None) -> bool:
        if ts >= actual_t_end:
            return False
        if dur is None:
            return ts >= actual_t_start
        return ts + dur > actual_t_start

    all_events = list(
        Event.objects.filter(
            target_content_type=recording_ct,
            target_object_id=str(recording.pk),
        ).order_by("timestamp")
    )
    slice_events = []
    for event in all_events:
        if not _overlaps(event.timestamp, event.duration):
            continue
        sliced_ts, sliced_dur = _clamp_annotation(event.timestamp, event.duration, actual_t_start, actual_t_end)
        slice_events.append(
            {
                "object_hash": event.object_hash,
                "name": event.name,
                "timestamp": sliced_ts,
                "duration": sliced_dur,
                "value": event.value,
            }
        )

    all_interruptions = list(
        Interruption.objects.filter(
            target_content_type=recording_ct,
            target_object_id=str(recording.pk),
        ).order_by("timestamp")
    )
    slice_interruptions = []
    for intr in all_interruptions:
        if not _overlaps(intr.timestamp, intr.duration):
            continue
        sliced_ts, sliced_dur = _clamp_annotation(intr.timestamp, intr.duration, actual_t_start, actual_t_end)
        slice_interruptions.append(
            {
                "object_hash": intr.object_hash,
                "timestamp": sliced_ts,
                "duration": sliced_dur,
            }
        )

    log_activity(
        verb="recordings.read.slice",
        target=recording,
        metadata={
            "t_start": actual_t_start,
            "t_end": actual_t_end,
            "event_count": len(slice_events),
            "interruption_count": len(slice_interruptions),
        },
    )

    return {
        "hash": recording.stored_name.split(".", 1)[0],
        "original_name": (recording.original_name if _can_see_original_name(user, recording, fed) else None),
        "display_name": _resolve_display_name(recording),
        "has_custom_name": _has_custom_display_name(recording),
        "file_extension": recording.file_extension,
        "file_size": recording.file_size,
        "file_hash": recording.file_hash,
        "content_hash": recording.content_hash,
        "status": recording.status,
        "modality": recording.modality,
        "created_at": recording.created_at,
        "deleted_at": recording.deleted_at,
        "meta": {
            "format": meta.format,
            "duration": n_slice_records * dr,
            "data_record_count": n_slice_records,
            "data_record_duration": meta.data_record_duration,
            "signal_count": meta.signal_count,
            "discontinuous": meta.discontinuous,
        },
        "t_start": actual_t_start,
        "t_end": actual_t_end,
        "events": slice_events,
        "interruptions": slice_interruptions,
    }


@api.get("/{hash}/annotations")
def list_recording_annotations(
    request,
    hash: str,
    author_id: int | None = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """Return the annotation sets (Annotation objects) for a recording.

    Events, interruptions, and labels are included in the recording list
    response directly. This endpoint exposes the free-form Annotation bundles
    separately so callers can fetch them on demand.
    """

    user = _require_auth(request)
    normalized_hash = (hash or "").strip().upper()
    if len(normalized_hash) != 32 or not normalized_hash.isalnum():
        raise HttpError(400, "Invalid recording hash.")

    recording = (
        Recording.objects.filter(stored_name__startswith=f"{normalized_hash}.", deleted_at__isnull=True)
        .order_by("-created_at")
        .first()
    )
    if recording is None:
        raise HttpError(404, "Recording not found")

    if _failed_hidden_for_caller(recording, user, None):
        raise HttpError(404, "Recording not found")

    if not (
        getattr(user, "is_superuser", False)
        or recording.author_id == user.pk
        or can_read_object(user=user, obj=recording)
    ):
        raise HttpError(403, "You do not have permission to view this recording")

    from annotations.models import Annotation

    recording_ct = ContentType.objects.get_for_model(recording, for_concrete_model=False)
    queryset = Annotation.objects.filter(
        target_content_type=recording_ct,
        target_object_id=str(recording.pk),
    )
    if author_id is not None:
        queryset = queryset.filter(author_id=author_id)
    queryset = queryset.order_by("-created_at")

    annotations = list(queryset[offset : offset + limit])

    log_activity(
        verb="recordings.annotations.list",
        target=recording,
        metadata={
            "author_id_filter": author_id,
            "limit": limit,
            "offset": offset,
            "returned_count": len(annotations),
        },
    )

    def _serialize(ann):
        import json as _json

        base = {
            "author_id": ann.author_id,
            "object_hash": ann.object_hash,
            "content_hash": ann.content_hash,
        }
        content = ann.content
        if isinstance(content, str):
            try:
                content = _json.loads(content)
            except (ValueError, _json.JSONDecodeError):
                return {**base, "value": "Invalid annotation content"}
        if isinstance(content, dict):
            return {**base, **content}
        return {**base, "value": content}

    return [_serialize(ann) for ann in annotations]


# ── Batch operations on a literal path ───────────────────────────────────────
#
# Registered ABOVE the ``/{hash}`` routes on purpose. Ninja emits URL patterns in
# registration order and ``{hash}`` compiles to a ``str`` converter, which matches
# any single segment — a literal one included. While this lived at the end of the
# module, ``POST /set-mains`` resolved to ``download_recording`` with
# ``hash="set-mains"``, a path view that has no POST, so every call answered 405;
# resolution precedes authentication, so even the unauthenticated case answered
# 405 instead of 401. ``epicurrents/tests/test_route_shadowing.py`` enforces the
# ordering for every mounted API so it does not have to be remembered.


class BulkSetMainsIn(Schema):
    """Payload for the batch mains-frequency setter."""

    hashes: list[str]
    # Environment mains frequency in Hz (50 EU / 60 US); null clears the override
    # so the recording inherits the deployment EEG_MAINS_HZ default.
    power_line_frequency: float | None = None


class BulkSetMainsOut(Schema):
    updated: int
    skipped: int


@api.post("/set-mains", response=BulkSetMainsOut)
def bulk_set_mains(request, payload: BulkSetMainsIn):
    """Set or clear the mains-frequency override on a batch of recordings.

    ``power_line_frequency`` is the recording environment's mains frequency in Hz
    (50 EU / 60 US); ``null`` clears the override so the recording inherits the
    deployment ``EEG_MAINS_HZ`` default — the intended tool when importing a
    dataset recorded in another mains region. Invalid hashes and recordings the
    caller cannot write are counted as ``skipped`` (hidden/soft-deleted rows are
    silently ignored, mirroring the collection bulk-rename).
    """
    from epicurrents.permissions import can_write_object

    user = _require_auth(request)
    hz = payload.power_line_frequency
    if hz is not None and not (0 < hz < 10_000):
        raise HttpError(
            400,
            "power_line_frequency must be a positive frequency in Hz, or null to clear.",
        )

    updated = 0
    skipped = 0
    with transaction.atomic():
        for raw in payload.hashes:
            normalized = (raw or "").strip().upper()
            if len(normalized) != 32 or not normalized.isalnum():
                skipped += 1
                continue
            recording = (
                Recording.objects.filter(stored_name__startswith=f"{normalized}.", deleted_at__isnull=True)
                .order_by("-created_at")
                .first()
                or Recording.objects.filter(stored_name=normalized, deleted_at__isnull=True)
                .order_by("-created_at")
                .first()
            )
            if recording is None or not can_write_object(user=user, obj=recording):
                skipped += 1
                continue
            recording.power_line_frequency = hz
            recording.save(update_fields=["power_line_frequency", "modified_at"])
            log_activity(
                verb="recordings.set_mains",
                target=recording,
                metadata={"power_line_frequency": hz},
            )
            updated += 1

    return {"updated": updated, "skipped": skipped}


# Single-segment ``{hash}`` routes. Nothing below this line may be a route whose
# path is a single literal segment — ``{hash}`` would swallow it (see above).
@api.get("/{hash}", response=RecordingOut)
def recording_detail(request, hash: str, share_token: str | None = None):
    """Return full recording metadata (including format meta) for a single recording.

    Used by the viewer and other single-recording contexts that need full header
    information without downloading the file. Unauthenticated access is allowed
    when a valid *share_token* is supplied.
    """

    user = getattr(request, "user", None)
    fed = None
    if user and getattr(user, "is_authenticated", False):
        pass  # session auth
    else:
        fed = _try_federated_auth(request)
        if fed is None and not (share_token or "").strip():
            raise HttpError(401, "Authentication credentials were not provided")

    normalized_hash = (hash or "").strip().upper()
    if len(normalized_hash) != 32 or not normalized_hash.isalnum():
        raise HttpError(400, "Invalid recording hash.")

    recording = (
        Recording.objects.filter(stored_name__startswith=f"{normalized_hash}.", deleted_at__isnull=True)
        .order_by("-created_at")
        .first()
    )
    if recording is None:
        raise HttpError(404, "Recording not found")

    if fed is not None:
        fed_peer, remote_user_id = fed
        if _failed_hidden_for_caller(recording, user, fed):
            log_federation_access(
                peer=fed_peer,
                remote_user_id=remote_user_id,
                action="recording_detail",
                target=recording,
                status_code=404,
            )
            raise HttpError(404, "Recording not found")
        granted = get_federated_read_access_result(fed_peer, remote_user_id, recording).granted
        if not granted:
            log_federation_access(
                peer=fed_peer,
                remote_user_id=remote_user_id,
                action="recording_detail",
                target=recording,
                status_code=403,
            )
            raise HttpError(403, "You do not have permission to view this recording")
        log_federation_access(
            peer=fed_peer,
            remote_user_id=remote_user_id,
            action="recording_detail",
            target=recording,
            status_code=200,
        )
    elif _failed_hidden_for_caller(recording, user, None):
        raise HttpError(404, "Recording not found")
    elif not (
        (user and getattr(user, "is_superuser", False))
        or (user and getattr(user, "is_authenticated", False) and recording.author_id == user.pk)
        or can_read_object(user=user, obj=recording, share_token=share_token)
    ):
        raise HttpError(403, "You do not have permission to view this recording")

    if _failed_hidden_for_caller(recording, user, fed):
        raise HttpError(404, "Recording not found")

    prefetch_related_objects([recording], "events", "interruptions", "labels")

    from recordings.models import RecordingMeta

    recording_ct = ContentType.objects.get_for_model(Recording, for_concrete_model=False)
    meta_by_pk = {
        int(m.object_id): m
        for m in RecordingMeta.objects.filter(
            content_type=recording_ct,
            object_id=str(recording.pk),
        ).prefetch_related("signals")
    }

    log_activity(
        verb="recordings.read",
        target=recording,
        metadata={"share_token_used": bool((share_token or "").strip())},
    )
    return _build_recording_out(recording, meta_by_pk, user=user, fed=fed)


@api.get("/{hash}/file")
def download_recording(request, hash: str, share_token: str | None = None):
    """Download file content by stored recording hash, with Range request support.

    Supports ``Range: bytes=start-end`` for partial content retrieval (HTTP 206).
    Clients that do not send a Range header receive the full file (HTTP 200).
    The ``Accept-Ranges: bytes`` header is always present so viewers can seek
    without an initial probe request.  Unauthenticated access is allowed when a
    valid *share_token* is supplied.
    """

    user = getattr(request, "user", None)
    fed = None
    if user and getattr(user, "is_authenticated", False):
        pass  # session auth
    else:
        fed = _try_federated_auth(request)
        if fed is None and not (share_token or "").strip():
            raise HttpError(401, "Authentication credentials were not provided")

    normalized_hash = (hash or "").strip().upper()
    if len(normalized_hash) != 32 or not normalized_hash.isalnum():
        raise HttpError(400, "Invalid recording hash. Use 32 alphanumeric characters.")

    recording = (
        Recording.objects.filter(stored_name__startswith=f"{normalized_hash}.", deleted_at__isnull=True)
        .order_by("-created_at")
        .first()
    )
    if recording is None:
        recording = (
            Recording.objects.filter(stored_name=normalized_hash, deleted_at__isnull=True)
            .order_by("-created_at")
            .first()
        )
    if recording is None:
        raise HttpError(404, "Recording not found")

    # Determine apply_middleware from the appropriate access right.
    # Federated peers always go through their explicit grant — there is no
    # author/superuser bypass for remote callers.
    apply_middleware = False
    if fed is not None:
        fed_peer, remote_user_id = fed
        if _failed_hidden_for_caller(recording, user, fed):
            log_federation_access(
                peer=fed_peer,
                remote_user_id=remote_user_id,
                action="download_recording",
                target=recording,
                status_code=404,
            )
            raise HttpError(404, "Recording not found")
        result = get_federated_read_access_result(fed_peer, remote_user_id, recording)
        if not result.granted:
            log_federation_access(
                peer=fed_peer,
                remote_user_id=remote_user_id,
                action="download_recording",
                target=recording,
                status_code=403,
            )
            raise HttpError(403, "You do not have permission to view this recording")
        # FAILED-hidden short-circuit BEFORE the quota check so a probed
        # FAILED recording does not burn the peer's daily byte budget on a
        # response that the peer never received.
        # Per-peer rate / byte-quota check.  Charging the *full* file size
        # against the daily budget over-counts when the client uses Range to
        # fetch only part of the file; that bias is intentional — it makes
        # the budget a true bound on what the peer can pull, not on what
        # they actually consume.
        try:
            check_peer_download_limits(fed_peer, expected_bytes=int(recording.file_size or 0))
        except QuotaExceeded as exc:
            log_federation_access(
                peer=fed_peer,
                remote_user_id=remote_user_id,
                action="download_recording",
                target=recording,
                status_code=429,
            )
            raise HttpError(429, str(exc))
        log_federation_access(
            peer=fed_peer,
            remote_user_id=remote_user_id,
            action="download_recording",
            target=recording,
            status_code=200,
        )
        apply_middleware = result.apply_middleware
    elif _failed_hidden_for_caller(recording, user, None):
        raise HttpError(404, "Recording not found")
    elif (
        user
        and getattr(user, "is_authenticated", False)
        and (getattr(user, "is_superuser", False) or recording.author_id == user.pk)
    ):
        pass  # author and superusers always receive raw data
    else:
        result = get_read_access_result(user=user, obj=recording, share_token=share_token)
        if not result.granted:
            raise HttpError(403, "You do not have permission to view this recording")
        apply_middleware = result.apply_middleware

    if _failed_hidden_for_caller(recording, user, fed):
        raise HttpError(404, "Recording not found")

    if recording.status not in (Recording.Status.READY, Recording.Status.FAILED):
        raise HttpError(
            409,
            f"Recording is not yet available for download (status: {recording.status})",
        )

    file_path = Path(recording.file_path)
    if not file_path.exists() or not file_path.is_file():
        raise HttpError(404, "Recording file not found on disk")

    log_activity(
        verb="recordings.download",
        target=recording,
        metadata={"apply_middleware": apply_middleware},
    )

    download_filename = _download_filename(recording)
    # Raw grants can be handed to the reverse proxy instead of occupying a
    # gunicorn thread for the length of the transfer. Declines to None whenever
    # the capability is off, no proxy is in front, or the grant is
    # middleware-applied — in which case the bytes are computed and no file on
    # disk holds them. See epicurrents/offload.py.
    offloaded = offload_file_response(
        request,
        file_path,
        root=settings.RECORDINGS_UPLOAD_PATH,
        namespace="recordings",
        filename=download_filename,
        apply_middleware=apply_middleware,
    )
    if offloaded is not None:
        return offloaded
    if apply_middleware:
        return _serve_recording_with_middleware(request, file_path, download_filename, recording)
    return _serve_recording_file(request, file_path, download_filename)


@api.get("/{hash}/file/slice")
def slice_recording(
    request,
    hash: str,
    t_start: float = Query(
        default=0.0,
        description=(
            "Start of the requested window in seconds from the beginning of the file. "
            "Negative values are relative to the end of the file (e.g. -60 = last 60 s)."
        ),
    ),
    t_end: float | None = Query(
        default=None,
        description=(
            "End of the requested window in seconds. Negative values are relative to the "
            "end of the file. Defaults to the end of the file."
        ),
    ),
):
    """Download a time-range slice of an EDF/BDF recording as a valid EDF/BDF file.

    The response body is a self-contained EDF/BDF file whose ``data_record_count``
    header field reflects the number of records in the slice.  The full header
    (channel metadata, physical/digital ranges, etc.) is preserved so the file
    can be read by any EDF library without extra context.

    The slice is record-aligned: the returned window is the smallest set of
    whole data records that covers ``[t_start, t_end)``.

    Middleware is applied (patient-identification removal, channel dropping,
    etc.) under the same rules as the main download endpoint — the caller's
    ``AccessRight.apply_middleware`` flag controls this.

    Only EDF and BDF files are supported.  Other formats return **422**.
    """
    user, fed = _require_auth_or_federated(request)
    normalized_hash = (hash or "").strip().upper()
    if len(normalized_hash) != 32 or not normalized_hash.isalnum():
        raise HttpError(400, "Invalid recording hash. Use 32 alphanumeric characters.")

    recording = (
        Recording.objects.filter(stored_name__startswith=f"{normalized_hash}.", deleted_at__isnull=True)
        .order_by("-created_at")
        .first()
    )
    if recording is None:
        recording = (
            Recording.objects.filter(stored_name=normalized_hash, deleted_at__isnull=True)
            .order_by("-created_at")
            .first()
        )
    if recording is None:
        raise HttpError(404, "Recording not found")

    apply_middleware = False
    if fed is not None:
        fed_peer, remote_user_id = fed
        if _failed_hidden_for_caller(recording, user, fed):
            log_federation_access(
                peer=fed_peer,
                remote_user_id=remote_user_id,
                action="slice_recording",
                target=recording,
                status_code=404,
            )
            raise HttpError(404, "Recording not found")
        result = get_federated_read_access_result(fed_peer, remote_user_id, recording)
        if not result.granted:
            log_federation_access(
                peer=fed_peer,
                remote_user_id=remote_user_id,
                action="slice_recording",
                target=recording,
                status_code=403,
            )
            raise HttpError(403, "You do not have permission to view this recording")
        # FAILED-hidden short-circuit BEFORE the quota check (same reason as
        # download_recording — don't burn the peer's daily budget on a
        # response that never reaches them).
        # Per-peer rate / byte-quota check.  Charge the full file size: the
        # slice endpoint can be called repeatedly for different time ranges,
        # so bounding by per-call slice size would let a peer fetch the whole
        # file as 1000 small slices and bypass the daily budget.
        try:
            check_peer_download_limits(fed_peer, expected_bytes=int(recording.file_size or 0))
        except QuotaExceeded as exc:
            log_federation_access(
                peer=fed_peer,
                remote_user_id=remote_user_id,
                action="slice_recording",
                target=recording,
                status_code=429,
            )
            raise HttpError(429, str(exc))
        log_federation_access(
            peer=fed_peer,
            remote_user_id=remote_user_id,
            action="slice_recording",
            target=recording,
            status_code=200,
        )
        apply_middleware = result.apply_middleware
    elif _failed_hidden_for_caller(recording, user, None):
        raise HttpError(404, "Recording not found")
    elif getattr(user, "is_superuser", False) or recording.author_id == user.pk:
        pass  # author and superusers always receive raw data
    else:
        result = get_read_access_result(user=user, obj=recording)
        if not result.granted:
            raise HttpError(403, "You do not have permission to view this recording")
        apply_middleware = result.apply_middleware

    if _failed_hidden_for_caller(recording, user, fed):
        raise HttpError(404, "Recording not found")

    if recording.status not in (Recording.Status.READY, Recording.Status.FAILED):
        raise HttpError(409, f"Recording is not yet available (status: {recording.status})")

    ext = (recording.file_extension or "").lower()
    if ext not in (".edf", ".bdf"):
        raise HttpError(422, "Time-range slicing is only supported for EDF and BDF files")

    from recordings.models import RecordingMeta

    recording_ct = ContentType.objects.get_for_model(recording, for_concrete_model=False)
    meta = RecordingMeta.objects.filter(
        content_type=recording_ct,
        object_id=str(recording.pk),
    ).first()
    if meta is None or not meta.data_record_count or not meta.data_record_duration:
        raise HttpError(422, "Recording metadata not available for time-range slicing")

    file_path = Path(recording.file_path)
    if not file_path.exists() or not file_path.is_file():
        raise HttpError(404, "Recording file not found on disk")

    dr = float(meta.data_record_duration)
    n_records = meta.data_record_count
    duration = n_records * dr
    header_size = 256 * (1 + meta.signal_count)

    file_size = file_path.stat().st_size
    signal_bytes = file_size - header_size
    if signal_bytes <= 0 or signal_bytes % n_records != 0:
        raise HttpError(422, "Recording file size is inconsistent with its metadata")
    input_record_size = signal_bytes // n_records

    # Resolve negative offsets (relative to end) and apply defaults.
    if t_start < 0:
        t_start = duration + t_start
    if t_end is None:
        t_end = duration
    elif t_end < 0:
        t_end = duration + t_end

    t_start = max(0.0, min(float(t_start), duration))
    t_end = max(0.0, min(float(t_end), duration))

    if t_start >= t_end:
        raise HttpError(400, "t_start must be less than t_end")

    # Convert to record indices (record-aligned superset of [t_start, t_end)).
    first_rec = max(0, int(t_start / dr))
    last_rec = min(n_records - 1, max(first_rec, math.ceil(t_end / dr) - 1))
    n_slice_records = last_rec - first_rec + 1

    log_activity(
        verb="recordings.download.slice",
        target=recording,
        metadata={
            "t_start": first_rec * dr,
            "t_end": (last_rec + 1) * dr,
            "apply_middleware": apply_middleware,
        },
    )

    return _serve_recording_slice(
        file_path=file_path,
        filename=_download_filename(recording),
        first_rec=first_rec,
        n_slice_records=n_slice_records,
        header_size=header_size,
        input_record_size=input_record_size,
        apply_middleware=apply_middleware,
    )


@api.delete("/{hash}")
def delete_recording(request, hash: str):
    """Soft-delete a recording by moving it to the recycle bin.

    Sets deleted_at to the current timestamp. The recording is excluded from
    normal listings and downloads immediately. It can be restored by rolling back
    the resulting ObjectChangeLog entry via POST /api/v1/rollback/{change_id}.
    The file is permanently removed by the purge_deleted_recordings background task
    once the configured retention window (RECORDINGS_TRASH_RETENTION_DAYS) elapses.
    """

    user = _require_auth(request)
    normalized_hash = (hash or "").strip().upper()
    if len(normalized_hash) != 32 or not normalized_hash.isalnum():
        raise HttpError(400, "Invalid recording hash. Use 32 alphanumeric characters.")

    recording = (
        Recording.objects.filter(stored_name__startswith=f"{normalized_hash}.", deleted_at__isnull=True)
        .order_by("-created_at")
        .first()
    )
    if recording is None:
        recording = (
            Recording.objects.filter(stored_name=normalized_hash, deleted_at__isnull=True)
            .order_by("-created_at")
            .first()
        )
    if recording is None:
        raise HttpError(404, "Recording not found")

    ensure_can_write_object(user=user, obj=recording)

    with transaction.atomic():
        recording.deleted_at = timezone.now()
        recording.save(update_fields=["deleted_at", "modified_at"])
        log_activity(verb="recordings.trash", target=recording)

    return {"status": "ok"}


@api.patch("/{hash}", response=RecordingOut)
def update_recording(request, hash: str, payload: RecordingPatchIn):
    """Update a recording's editable metadata (display name and modality).

    Only fields included in the request body are updated.  The underlying
    file, the original filename, and all parsed EDF/BDF data are immutable
    — only ``display_name`` and ``modality`` may be changed via this endpoint.

    Send ``display_name=""`` to clear the field; responses will fall back to
    the ``stored_name`` hash prefix.

    Requires write access (author, superuser, or an ``AccessRight`` row with
    ``can_write=True``).
    """
    user = _require_auth(request)
    normalized_hash = (hash or "").strip().upper()
    if len(normalized_hash) != 32 or not normalized_hash.isalnum():
        raise HttpError(400, "Invalid recording hash. Use 32 alphanumeric characters.")

    recording = (
        Recording.objects.filter(stored_name__startswith=f"{normalized_hash}.", deleted_at__isnull=True)
        .order_by("-created_at")
        .first()
    )
    if recording is None:
        recording = (
            Recording.objects.filter(stored_name=normalized_hash, deleted_at__isnull=True)
            .order_by("-created_at")
            .first()
        )
    if recording is None:
        raise HttpError(404, "Recording not found")

    ensure_can_write_object(user=user, obj=recording)

    fields_updated: list[str] = []
    if payload.display_name is not None:
        normalized = payload.display_name.strip()
        # An empty string clears the field — responses fall back to the
        # hash-prefix default in _resolve_display_name.
        recording.display_name = normalized or None
        fields_updated.append("display_name")
    if payload.modality is not None:
        recording.modality = payload.modality.strip().lower()
        fields_updated.append("modality")

    with transaction.atomic():
        recording.save(update_fields=fields_updated + ["modified_at"])
        log_activity(
            verb="recordings.update",
            target=recording,
            metadata={"fields_updated": fields_updated},
        )

    return _build_recording_out(recording, user=user)


def _resolve_recording_by_hash(hash: str):
    """Resolve an active recording from a 32-character content hash, or raise.

    The same lookup is inlined at eight older call sites in this module. It is
    not extracted from them here: several sit on the byte-serving paths covered
    by the serve-pipeline contract test, and a mechanical sweep across those is
    worth its own commit rather than riding along with a new endpoint.
    """
    normalized_hash = (hash or "").strip().upper()
    if len(normalized_hash) != 32 or not normalized_hash.isalnum():
        raise HttpError(400, "Invalid recording hash. Use 32 alphanumeric characters.")

    recording = (
        Recording.objects.filter(stored_name__startswith=f"{normalized_hash}.", deleted_at__isnull=True)
        .order_by("-created_at")
        .first()
    )
    if recording is None:
        recording = (
            Recording.objects.filter(stored_name=normalized_hash, deleted_at__isnull=True)
            .order_by("-created_at")
            .first()
        )
    if recording is None:
        raise HttpError(404, "Recording not found")
    return recording


def _require_access_manager(request, hash: str):
    """Resolve a recording the caller may manage access on, or raise.

    Mirrors the collection and dataset rule: the data owner decides, where owner
    means ``can_modify_object`` (author or superuser) or the holder of a
    ``can_share`` grant. Deliberately *not* staff — who may see a recording is
    the author's call, not an operator's, and an account administrator with no
    grant of their own has no business reading the guest list.

    A FAILED recording answers 404 to everyone but its author and superusers,
    per the FAILED-hidden rule; the access list would otherwise disclose that a
    failed upload exists.
    """
    user = _require_auth(request)
    recording = _resolve_recording_by_hash(hash)
    if _failed_hidden_for_caller(recording, user, None):
        raise HttpError(404, "Recording not found")

    if can_modify_object(user=user, obj=recording):
        return user, recording

    recording_ct = ContentType.objects.get_for_model(recording, for_concrete_model=False)
    group_ids = list(user.groups.values_list("id", flat=True))
    target_filter = Q(access_target=user)
    if group_ids:
        target_filter |= Q(access_target_group_id__in=group_ids)
    has_share = (
        AccessRight.objects.filter(
            content_type=recording_ct,
            object_id=str(recording.pk),
            can_share=True,
        )
        .filter(target_filter)
        .active()
        .exists()
    )
    if not has_share:
        raise HttpError(403, "You do not have permission to manage access for this recording")
    return user, recording


@api.get("/{hash}/access/", response=list[AccessRightOut])
def list_recording_access(request, hash: str):
    """List the access rights granted on a recording.

    Includes share-token rows, whose token value is part of the response — the
    callers who reach this point are the ones who could mint or read that token
    anyway, and a share link the owner cannot see is a share link they cannot
    audit.
    """
    _user, recording = _require_access_manager(request, hash)
    recording_ct = ContentType.objects.get_for_model(recording, for_concrete_model=False)
    rights = list(
        AccessRight.objects.filter(content_type=recording_ct, object_id=str(recording.pk))
        .select_related("access_target", "access_target_group")
        .order_by("id")
    )
    log_activity(
        verb="recordings.access.list",
        target=recording,
        metadata={"returned_count": len(rights)},
    )
    return [access_right_out(right) for right in rights]


@api.delete("/{hash}/access/{right_id}/", response=dict)
def revoke_recording_access(request, hash: str, right_id: int):
    """Revoke one access right on a recording.

    Refuses to revoke the grant that targets the recording's own author. Reading
    a recording resolves through ``AccessRight`` with no author fast-path — only
    superusers get one — so the author reads their own upload solely by virtue of
    the self-grant written at upload time. Deleting it would leave them able to
    rename and delete the recording (``can_modify_object`` *does* check author)
    while unable to read or download it, and with no endpoint that creates a
    grant there would be no way back.
    """
    _user, recording = _require_access_manager(request, hash)
    recording_ct = ContentType.objects.get_for_model(recording, for_concrete_model=False)

    right = AccessRight.objects.filter(
        pk=right_id,
        content_type=recording_ct,
        object_id=str(recording.pk),
    ).first()
    if right is None:
        raise HttpError(404, "Access right not found")

    if right.access_target_id is not None and right.access_target_id == recording.author_id:
        raise HttpError(
            409,
            "This grant is what gives the recording's author read access to it. Revoking it would leave the "
            "author unable to read their own recording, with no way to restore the grant.",
        )

    target_kind = (
        "user"
        if right.access_target_id is not None
        else "group"
        if right.access_target_group_id is not None
        else "share_token"
        if right.public_share_token
        else "federated_peer"
    )
    with transaction.atomic():
        log_activity(
            verb="recordings.access.revoke",
            target=recording,
            metadata={"target_kind": target_kind},
        )
        right.delete()
    return {"status": "ok"}
