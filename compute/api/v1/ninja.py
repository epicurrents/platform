"""Compute API v1 — serve pre-computed scientific matrices to the browser.

Endpoints
---------
GET  /eeg/leadfield/
    List all cached lead field matrices (staff only).

POST /eeg/leadfield/
    Trigger computation of a new lead field matrix and cache it (staff only).
    If a matching entry already exists the request is rejected with 409 unless
    ``force=true`` is passed, in which case the existing row is replaced.

GET  /eeg/leadfield/{montage_name}/
    Return metadata for the cached lead field matching the requested montage,
    orientation, and grid resolution.  Authenticated users only.

GET  /eeg/leadfield/{montage_name}/data/
    Download the lead field matrix and source positions as a single binary
    blob (authenticated users only).

    Response body layout (all values little-endian float64, C order):
        bytes  0 … (n_channels * n_sources * n_orient * 8) - 1  →  lead field
        bytes  (n_channels * n_sources * n_orient * 8) …        →  src_pos

    Shape information is provided as response headers so the caller can
    slice the buffer without parsing:
        X-N-Channels   — int
        X-N-Sources    — int
        X-N-Orient     — int (1 = fixed, 3 = free)
        X-LeadField-Bytes — byte length of the lead field section
        X-SrcPos-Bytes    — byte length of the src_pos section

    Example JS (inside the module that calls the Pyodide script)::

        const resp = await fetch('/compute/api/v1/eeg/leadfield/standard_1020/data/');
        const nCh  = parseInt(resp.headers.get('X-N-Channels'));
        const nSrc = parseInt(resp.headers.get('X-N-Sources'));
        const nOri = parseInt(resp.headers.get('X-N-Orient'));
        const lfLen = parseInt(resp.headers.get('X-LeadField-Bytes')) / 8;
        const buf  = await resp.arrayBuffer();
        const leadField = new Float64Array(buf, 0, lfLen);
        const srcPos    = new Float64Array(buf, lfLen * 8, nSrc * 3);
"""

import logging
from datetime import datetime
from typing import Annotated, Literal

from django.conf import settings
from django.http import HttpResponse
from ninja import NinjaAPI, Query, Schema
from ninja.errors import HttpError
from pydantic import BeforeValidator, Field

from epicurrents.auth import enforce_session_csrf

logger = logging.getLogger(__name__)

api = NinjaAPI(
    title="Compute API",
    version="1",
    urls_namespace="compute-api-v1",
    docs_url=settings.API_DOCS_URL,
    openapi_url=settings.API_OPENAPI_URL,
)

# Number of orientations per source point in a lead field.
#  1 — fixed (surface-normal); standard for sLORETA / eLORETA / dSPM.
#  3 — free (x/y/z); unconstrained dipole fitting.
# Used everywhere n_orient appears on the wire so the constraint lives in one place.
# The BeforeValidator coerces query-string values ("1" / "3") to int before the
# strict Literal check runs.
NOrient = Annotated[Literal[1, 3], BeforeValidator(int)]


# ---------------------------------------------------------------------------
# Auth helpers (mirrors the pattern used across all other platform APIs)
# ---------------------------------------------------------------------------


def _require_auth(request):
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated:
        raise HttpError(401, "Authentication credentials were not provided.")
    enforce_session_csrf(request)
    return user


def _require_staff(request):
    user = _require_auth(request)
    if not (user.is_staff or user.is_superuser):
        raise HttpError(403, "Staff access required.")
    return user


def _require_superuser(request):
    user = _require_auth(request)
    if not user.is_superuser:
        raise HttpError(403, "Superuser access required.")
    return user


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class LeadFieldTriggerIn(Schema):
    """Parameters for triggering lead field computation.

    The bounds keep a single request from wedging the worker: the source
    grid grows cubically as ``grid_resolution_mm`` shrinks, and the
    computation currently runs synchronously in the request thread, so an
    unbounded value is a one-request denial of service even from a staff
    account.
    """

    montage_name: str
    grid_resolution_mm: float = Field(7.5, ge=2.0, le=50.0)
    n_orient: NOrient = 1
    sphere_radius_m: float = Field(0.09, gt=0.0, le=0.2)
    sphere_center_m: tuple[float, float, float] = (0.0, 0.0, 0.04)
    force: bool = False


class LeadFieldMetaOut(Schema):
    """Metadata about a cached lead field (no binary data)."""

    id: int
    montage_name: str
    n_channels: int
    n_sources: int
    n_orient: int
    grid_resolution_mm: float
    sphere_radius_m: float
    sphere_center_m: tuple[float, float, float]
    channel_names: list[str]
    created_at: datetime
    updated_at: datetime


def _row_to_meta(row) -> LeadFieldMetaOut:
    return LeadFieldMetaOut(
        id=row.pk,
        montage_name=row.montage_name,
        n_channels=row.n_channels,
        n_sources=row.n_sources,
        n_orient=row.n_orient,
        grid_resolution_mm=row.grid_resolution_mm,
        sphere_radius_m=row.sphere_radius_m,
        sphere_center_m=row.sphere_center_m,
        channel_names=row.channel_names,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


@api.get("/eeg/leadfield/", response=list[LeadFieldMetaOut])
def list_lead_fields(request):
    """List all cached lead field matrices.

    Staff only.  Returns metadata for every cached entry without any binary
    data so the response stays small.
    """
    _require_staff(request)
    from compute.models import LeadFieldCache

    return [_row_to_meta(row) for row in LeadFieldCache.objects.order_by("montage_name")]


# ---------------------------------------------------------------------------
# Trigger computation (staff only)
# ---------------------------------------------------------------------------


@api.post("/eeg/leadfield/", response={200: LeadFieldMetaOut, 201: LeadFieldMetaOut})
def create_lead_field(request, payload: LeadFieldTriggerIn):
    """Compute and cache an EEG lead field matrix.

    Computation is performed synchronously in the request; for large grids
    (fine resolution, free orientation) this may take a minute or more.
    Consider wrapping in a Celery task if that becomes a problem.

    Status codes:

    * ``201`` — a fresh row was created.
    * ``200`` — an existing row was replaced (``force=true``); ``updated_at``
      reflects the new computation time, ``created_at`` is preserved.
    * ``409`` — a matching row exists and ``force`` is ``false``.
    * ``422`` — schema validation failed (e.g. ``n_orient`` not in ``{1, 3}``,
      or ``sphere_center_m`` is not a 3-element array of numbers).
    * ``400`` — MNE does not recognise the montage name.
    * ``403`` — caller is not staff (any path), or not superuser and
      ``force`` is ``true``.

    ``force=true`` replaces a row that another staff user may have computed,
    so it is treated as a superuser-only override of the default behaviour.
    Standard (initial) computation only requires staff.
    """
    _require_staff(request)
    if payload.force:
        _require_superuser(request)
    from compute.eeg.forward import compute_eeg_lead_field
    from compute.models import LeadFieldCache

    # Pre-flight check so we don't waste a minute of MNE compute when the
    # answer is obvious. A small TOCTOU window remains between this check
    # and the upsert below; if another request creates the row in that
    # gap we silently replace it with our (identical-by-construction)
    # result rather than 500ing on a UniqueConstraint violation.
    if (
        not payload.force
        and LeadFieldCache.objects.filter(
            montage_name=payload.montage_name,
            n_orient=payload.n_orient,
            grid_resolution_mm=payload.grid_resolution_mm,
        ).exists()
    ):
        raise HttpError(
            409,
            f"Lead field for montage='{payload.montage_name}', "
            f"n_orient={payload.n_orient}, "
            f"grid_resolution_mm={payload.grid_resolution_mm} already exists. "
            "Pass force=true to recompute and replace it.",
        )

    try:
        lead_field, src_pos, ch_names, _ = compute_eeg_lead_field(
            montage_name=payload.montage_name,
            grid_resolution_mm=payload.grid_resolution_mm,
            n_orient=payload.n_orient,
            sphere_radius_m=payload.sphere_radius_m,
            sphere_center_m=payload.sphere_center_m,
        )
    except ValueError as exc:
        raise HttpError(400, str(exc)) from exc
    except Exception as exc:
        logger.exception(
            "compute_eeg_lead_field failed",
            extra={
                "montage_name": payload.montage_name,
                "n_orient": payload.n_orient,
                "grid_resolution_mm": payload.grid_resolution_mm,
                "sphere_radius_m": payload.sphere_radius_m,
                "sphere_center_m": payload.sphere_center_m,
            },
        )
        raise HttpError(500, "Forward model computation failed; see server logs.") from exc

    row, created = LeadFieldCache.upsert_from_compute(
        montage_name=payload.montage_name,
        n_orient=payload.n_orient,
        grid_resolution_mm=payload.grid_resolution_mm,
        sphere_radius_m=payload.sphere_radius_m,
        sphere_center_m=payload.sphere_center_m,
        lead_field=lead_field,
        src_pos=src_pos,
        channel_names=ch_names,
    )

    return (201 if created else 200), _row_to_meta(row)


# ---------------------------------------------------------------------------
# Metadata for a single montage
# ---------------------------------------------------------------------------


@api.get("/eeg/leadfield/{montage_name}/", response=LeadFieldMetaOut)
def get_lead_field_meta(
    request,
    montage_name: str,
    n_orient: NOrient = Query(default=1),
    grid_resolution_mm: float = Query(default=7.5),
):
    """Return metadata for a cached lead field.

    Use this to verify a matrix exists before fetching the (potentially
    large) binary data, and to retrieve ``channel_names`` so the caller
    knows which channels the matrix was built for.
    """
    _require_auth(request)
    from compute.models import LeadFieldCache

    row = LeadFieldCache.objects.filter(
        montage_name=montage_name,
        n_orient=n_orient,
        grid_resolution_mm=grid_resolution_mm,
    ).first()
    if not row:
        raise HttpError(
            404,
            f"No cached lead field for montage='{montage_name}', "
            f"n_orient={n_orient}, grid_resolution_mm={grid_resolution_mm}. "
            "Ask a staff user to POST /compute/api/v1/eeg/leadfield/ to compute it.",
        )
    return _row_to_meta(row)


# ---------------------------------------------------------------------------
# Binary download
# ---------------------------------------------------------------------------


@api.get("/eeg/leadfield/{montage_name}/data/")
def download_lead_field_data(
    request,
    montage_name: str,
    n_orient: NOrient = Query(default=1),
    grid_resolution_mm: float = Query(default=7.5),
):
    """Download the lead field matrix and source positions as a binary blob.

    The response body contains two concatenated float64 arrays (little-endian,
    C order).  Use the ``X-*`` headers to locate each section:

    * bytes ``0 … X-LeadField-Bytes - 1``  →  lead field
      shape ``(n_channels, n_sources * n_orient)``
    * bytes ``X-LeadField-Bytes … end``    →  source positions
      shape ``(n_sources, 3)``

    See the module docstring for a complete JS parsing example.
    """
    _require_auth(request)
    from compute.models import LeadFieldCache

    row = LeadFieldCache.objects.filter(
        montage_name=montage_name,
        n_orient=n_orient,
        grid_resolution_mm=grid_resolution_mm,
    ).first()
    if not row:
        raise HttpError(
            404,
            f"No cached lead field for montage='{montage_name}', "
            f"n_orient={n_orient}, grid_resolution_mm={grid_resolution_mm}.",
        )

    lf_bytes: bytes = bytes(row.lead_field)
    sp_bytes: bytes = bytes(row.src_pos)
    body = lf_bytes + sp_bytes

    resp = HttpResponse(body, content_type="application/octet-stream")
    resp["X-N-Channels"] = str(row.n_channels)
    resp["X-N-Sources"] = str(row.n_sources)
    resp["X-N-Orient"] = str(row.n_orient)
    resp["X-LeadField-Bytes"] = str(len(lf_bytes))
    resp["X-SrcPos-Bytes"] = str(len(sp_bytes))
    resp["Content-Length"] = str(len(body))
    # Allow the JS fetch caller to read the custom headers cross-origin if
    # the platform is ever accessed from a different domain.
    resp["Access-Control-Expose-Headers"] = "X-N-Channels, X-N-Sources, X-N-Orient, X-LeadField-Bytes, X-SrcPos-Bytes"
    return resp
