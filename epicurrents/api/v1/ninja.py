"""Epicurrents core v1 REST endpoints — liveness / readiness probes and editable viewer-config overrides."""

import json
import logging
import os

from django.conf import settings
from django.core.cache import cache
from django.db import connections, transaction
from ninja import NinjaAPI
from ninja.errors import HttpError

from activity.audit import log_activity
from epicurrents.auth import enforce_session_csrf
from epicurrents.models import ViewerConfigOverride
from epicurrents.project_loader import get_active_project
from epicurrents.viewer_config import (
    get_effective_viewer_config,
    get_project_overrides,
    is_valid_overrides,
    load_viewer_config_seed,
)

logger = logging.getLogger(__name__)

api = NinjaAPI(
    title="Epicurrents API",
    version="1",
    urls_namespace="epicurrents-api-v1",
    docs_url=settings.API_DOCS_URL,
    openapi_url=settings.API_OPENAPI_URL,
)


@api.get("/health")
def healthcheck(request):
    """Liveness probe + deployment-posture surface.

    Returns ``status``, ``mode`` (the active ``DJANGO_MODE``, normalised
    to one of ``"development"`` / ``"production"`` / ``"unset"``), and
    ``debug`` (the live ``settings.DEBUG`` value). The frontend reads
    the response at app boot to decide whether to render the
    "DEV MODE — not for production data" banner; ops tooling can pivot
    on the same shape. Mode and debug values are not security-sensitive
    by themselves, but the endpoint stays operational telemetry — do not
    leak any further configuration through it.

    Deliberately touches no backing service: a liveness probe answers
    "is this process still able to serve" and must not fail because a
    dependency is down, or a transient database blip restarts every web
    container at once. Dependency reachability is ``/ready``.
    """

    raw_mode = (os.environ.get("DJANGO_MODE", "") or "").lower()
    if raw_mode in ("development", "production"):
        mode = raw_mode
    else:
        mode = "unset"

    return {
        "status": "ok",
        "mode": mode,
        "debug": bool(getattr(settings, "DEBUG", False)),
    }


@api.get("/ready", response={200: dict, 503: dict})
def readiness(request):
    """Readiness probe — reports whether this process can actually serve requests.

    Checks the default database connection and the cache backend, and answers
    200 with ``{"status": "ready"}`` only when both respond. Any failure returns
    503 so a load balancer or orchestrator stops routing to this container while
    leaving it running (that is the liveness probe's call, not this one's).

    ``checks`` carries per-dependency ``"ok"`` / ``"error"`` and nothing else.
    The exception text is deliberately dropped rather than returned: connection
    errors quote host names, ports and user names, and this endpoint is
    unauthenticated. The detail goes to the container log instead.
    """

    checks = {}

    try:
        with connections["default"].cursor() as cursor:
            cursor.execute("SELECT 1")
        checks["database"] = "ok"
    except Exception:
        logger.exception("Readiness probe: database check failed")
        checks["database"] = "error"

    try:
        cache.get("epicurrents-readiness-probe")
        checks["cache"] = "ok"
    except Exception:
        logger.exception("Readiness probe: cache check failed")
        checks["cache"] = "error"

    ready = all(value == "ok" for value in checks.values())
    payload = {"status": "ready" if ready else "not ready", "checks": checks}
    return payload if ready else (503, payload)


def _require_auth(request):
    """Return the authenticated user or raise 401.

    Routes unsafe-method session callers through the CSRF chokepoint
    (``enforce_session_csrf`` is a no-op for safe methods and non-session auth).
    """
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated:
        raise HttpError(401, "Authentication credentials were not provided")
    enforce_session_csrf(request)
    return user


def _require_staff(request):
    """Return the authenticated staff (or superuser) user, or raise 403.

    Editing the deployment-wide viewer config is gated on staff rather than
    superuser by deliberate choice: instructors (staff) tune the viewer defaults
    for their deployment. This is an explicit, documented departure from the
    usual write-→-superuser rule, justified because the config is operational
    (presentation defaults), not destructive.
    """
    user = _require_auth(request)
    if not (user.is_staff or user.is_superuser):
        raise HttpError(403, "Staff access required.")
    return user


@api.get("/viewer-config")
def get_viewer_config(request):
    """Return the effective viewer config for the active project.

    ``effective`` is the project's ``viewer-config.json`` seed merged with the
    editable database overrides (overrides win) — the viewer applies it on
    launch. ``seed`` and ``overrides`` are returned separately so the staff
    editor can show the read-only seed alongside the editable overrides.
    Readable by any authenticated user because the overrides apply to everyone.
    """
    _require_auth(request)
    project = get_active_project()
    log_activity("epicurrents.viewer_config.read")
    return {
        "seed": load_viewer_config_seed(project),
        "overrides": get_project_overrides(project),
        "effective": get_effective_viewer_config(),
    }


@api.put("/viewer-config")
def update_viewer_config(request):
    """Replace the editable viewer-config overrides for the active project.

    The request body is the overrides map itself: a flat JSON object of
    dotted-path settings field to value. Staff-gated, and routed through the
    session-CSRF chokepoint via ``_require_staff`` → ``_require_auth``. Value
    types are not constrained here — the viewer validates each field against its
    settings type when applying and skips mismatches with a warning.
    """
    _require_staff(request)
    project = get_active_project()
    if not project:
        raise HttpError(400, "No active project to configure.")
    try:
        overrides = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        raise HttpError(400, "Request body must be valid JSON.")
    if not is_valid_overrides(overrides):
        raise HttpError(400, "Viewer config must be a JSON object with string field names.")
    with transaction.atomic():
        row, _ = ViewerConfigOverride.objects.update_or_create(project=project, defaults={"overrides": overrides})
    log_activity(
        "epicurrents.viewer_config.update",
        target=row,
        metadata={"fields": sorted(overrides)},
    )
    return {"overrides": row.overrides, "effective": get_effective_viewer_config()}
