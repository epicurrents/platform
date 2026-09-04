"""Request-level audit-trail middleware.

⚠️ LOAD-BEARING — audit-trail coverage.
Mis-classifying a path here silently strips the audit trail off every
endpoint mounted under that path. Before changing the path matcher,
enumerate every API mount in ``epicurrents/urls.py`` and the per-app
``urls.py`` files and verify each still matches the regex; the contract
test ``epicurrents/tests/test_middleware_path_recognition.py`` is the
backstop and must stay green. See AGENTS.md → *Load-bearing files*.

``ApiActivityLoggingMiddleware`` creates an :class:`Activity` row at the
start of every API request, exposes the request's user + Activity to the
audit signals in ``activity/signals.py`` via :mod:`activity.request_context`
ContextVars, and fills in ``status_code`` + ``target_object_id`` on the way
out. The signal handlers gate ``ObjectChangeLog`` writes on
``is_audited_context()``; this middleware is one of the two entry points
that flip that flag (the other is ``activity.system_activity`` for Celery
tasks and management commands). Writes outside any audited scope (the
shell, ad-hoc scripts) are skipped — see ``activity/README.md`` →
*Known limitations* for the surrounding gaps.

What counts as an "API request" is the load-bearing decision here, because
silently mis-classifying a path leaves entire apps without an audit trail.
See :data:`_API_PATH_RE` for the recognised mount shape.

Some paths match the regex but are deliberately exempt from creating
``Activity`` rows — health checks and the public VAPID key are the
canonical examples; they operate on no user data and would drown the
data-interaction signal in operational noise. The exempt set is
configured by the ``ACTIVITY_PATH_SKIP_LIST`` setting; per-endpoint
policy is documented in
``.review/exemptions/audit-trail-completeness.md``. Adding to the skip
list is a *tightening* of the audit surface — enumerate every existing
caller of the path before adding (per AGENTS.md's "enumerate prior
matches" rule on LOAD-BEARING files).

The middleware fills in ``status_code`` on every exit and seeds
``target_object_id`` from the first ``pk`` / ``id`` / ``*_id`` URL
kwarg as a *fallback* when the endpoint did not resolve a target
itself. Two failure modes shape this contract:

- POST-with-body endpoints (no URL kwarg) that set ``target_object_id``
  via :func:`activity.audit.log_activity` would see it clobbered with
  ``None`` if the middleware unconditionally wrote at exit. This was
  the pre-2026-05-31 bug.
- Nested-resource reads (e.g. ``/inbound/objects/{ct_id}/{object_id}/``)
  have multiple ``*_id`` kwargs. The first-match URL heuristic picks
  the wrong one (``ct_id``, a content-type lookup ID), then would
  clobber the authoritative target the view resolved (the recording's
  pk). This was the 2026-06 follow-up.

The rule is therefore: the middleware writes ``target_object_id`` from
URL kwargs only when both (a) a kwarg was found AND (b) the view has
not already set it via ``log_activity``. An explicit value from the
view is always authoritative — same pattern as ``verb``.
"""

import logging
import re

from django.conf import settings
from django.db import DatabaseError
from django.db.utils import ProgrammingError
from django.http import JsonResponse

from activity.models import Activity
from activity.request_context import reset_request_context, set_request_context
from epicurrents.throttle import check_request_throttle

logger = logging.getLogger(__name__)

# Recognised API mount paths:
#   /api/v1/...               — core, user, activity, notifications, library, federation
#   /<app>/api/v1/...         — annotations, compute, recordings; also /project/api/v1/
#   /plugin/<name>/api/v1/... — enabled plugins (epicurrents/urls.py plugin loop)
# The shape is deliberately narrow: one arbitrary prefix segment, or the
# two-segment form only when the first segment is the literal ``plugin``. A
# path like /admin/foo/api/v1/bar — which would only appear if an unrelated
# section accidentally adopted an /api/v1/ subtree — does not silently start
# creating Activity rows. When adding a new top-level URL mount for an API,
# land it under /api/v1/, /<app>/api/v1/, or /plugin/<name>/api/v1/; all three
# shapes match. A new mount that diverges from this pattern must also update
# this regex AND ``test_middleware_path_recognition`` together; the regression
# test enumerates urls.py and fails if any registered API mount stops matching
# here.
_API_PATH_RE = re.compile(r"^/(?:plugin/[^/]+/|[^/]+/)?api/v\d+(?:/|$)")


class ApiActivityLoggingMiddleware:
    """Log API request activity and expose request context for audit signals."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        path = getattr(request, "path", "") or ""
        # Operational endpoints (health checks, public-key publication, etc.)
        # match _API_PATH_RE but are exempt from the data-interaction audit
        # trail. Treating them as non-API here skips both Activity-row
        # creation and the signal-handler context — neither matters for these
        # endpoints (they perform no model writes). See module docstring.
        skip_list = getattr(settings, "ACTIVITY_PATH_SKIP_LIST", ())
        is_api = bool(_API_PATH_RE.match(path)) and path not in skip_list
        actor = getattr(request, "user", None)
        if actor and not getattr(actor, "is_authenticated", False):
            actor = None

        # Identify the active project for requests to project-specific endpoints.
        # Project APIs are mounted at /project/api/v1/; the active project name
        # is read from EPICURRENTS_PROJECT (set via environment / .env).
        project = ""
        if is_api and path.startswith("/project/api/"):
            from django.conf import settings as django_settings

            project = getattr(django_settings, "EPICURRENTS_PROJECT", "") or ""

        activity = None
        if is_api:
            try:
                # ``verb`` and ``target_identifier`` are seeded here with
                # generic defaults (HTTP method lowercased, request path) so
                # the row is valid even if the view never touches them.
                # Views that want a semantic operation name in SIEM /
                # rollback contexts override both via the live Activity
                # reference returned by ``get_current_activity()`` — see
                # e.g. ``recordings/api/v1/ninja.py`` where
                # ``activity.verb = "recordings.upload"`` is set and saved
                # explicitly via ``update_fields``.
                activity = Activity.objects.create(
                    actor=actor,
                    interface=Activity.Interface.API,
                    verb=(getattr(request, "method", "") or "").lower(),
                    method=getattr(request, "method", "") or "",
                    path=path,
                    project=project,
                    status_code=None,
                    target_object_id="",
                    target_identifier=path,
                )
            except (DatabaseError, ProgrammingError):
                # Logging failures must not break user-facing requests, but a
                # WARNING gives operators a signal that the audit trail has
                # gaps to investigate.
                logger.warning(
                    "ApiActivityLoggingMiddleware: failed to create Activity row for %s %s",
                    getattr(request, "method", ""),
                    path,
                    exc_info=True,
                )
                activity = None

        tokens = set_request_context(user=actor, activity=activity, is_audited=is_api)
        try:
            response = self.get_response(request)
        finally:
            reset_request_context(tokens)

        if activity is not None:
            try:
                kwargs = getattr(getattr(request, "resolver_match", None), "kwargs", {}) or {}
                object_id = None
                for key, value in kwargs.items():
                    if key == "pk" or key == "id" or key.endswith("_id"):
                        object_id = str(value)
                        break

                # status_code always gets written on exit. ``target_object_id``
                # is only seeded from the URL kwarg when (a) a kwarg was found
                # AND (b) the view did not already set the field itself. An
                # explicit value from the view is always authoritative — see
                # the module docstring for the two failure modes this guards.
                activity.status_code = getattr(response, "status_code", None)
                update_fields = ["status_code"]
                if object_id is not None and not activity.target_object_id:
                    activity.target_object_id = object_id
                    update_fields.append("target_object_id")
                activity.save(update_fields=update_fields)
            except (DatabaseError, ProgrammingError):
                logger.warning(
                    "ApiActivityLoggingMiddleware: failed to update Activity row %d with response metadata",
                    activity.pk,
                    exc_info=True,
                )

        return response


class CrossOriginIsolationMiddleware:
    """Set COOP/COEP/CORP headers so ``SharedArrayBuffer`` is available in the browser.

    Cross-origin isolation is gated by three headers the browser checks on the
    top-level navigation:

    - ``Cross-Origin-Opener-Policy: same-origin`` — isolates the browsing
      context group from cross-origin openers/openees.
    - ``Cross-Origin-Embedder-Policy: require-corp`` — every subresource the
      page loads must declare it is OK with being embedded (via CORP) or use
      CORS. Pyodide's jsdelivr CDN sends CORP so it works under this mode.
    - ``Cross-Origin-Resource-Policy: same-origin`` — set on the responses we
      serve so other origins cannot embed them, and so same-origin loads stay
      consistent under COEP. ``same-origin`` is correct because every asset
      the Django dev server serves (HTML, viewer bundle, static, API) is
      same-origin from the page's POV.

    Without all three present, the browser sets ``crossOriginIsolated = false``
    and ``SharedArrayBuffer`` is unavailable, which disables the viewer's SAB
    signal-storage path (see ``frontend/viewer/CLAUDE.md`` → *Signal data flow*)
    and the dicom project's OHIF WASM decoders.

    The middleware is gated by ``ENABLE_CROSS_ORIGIN_ISOLATION`` so deployments
    stay at the current (header-less) behaviour until they opt in. The setting
    is read from the env var of the same name (default ``False``). A deployment
    that explicitly disables it should not be silently re-enabled by a project
    plugin, so projects document the dependency in their READMEs and rely on
    the deployment to set the env var rather than overriding the setting from
    the project's own ``settings.py``.
    """

    def __init__(self, get_response):
        self.get_response = get_response
        self.enabled = bool(getattr(settings, "ENABLE_CROSS_ORIGIN_ISOLATION", False))

    def __call__(self, request):
        response = self.get_response(request)
        if self.enabled:
            response.setdefault("Cross-Origin-Opener-Policy", "same-origin")
            response.setdefault("Cross-Origin-Embedder-Policy", "require-corp")
            response.setdefault("Cross-Origin-Resource-Policy", "same-origin")
        return response


class SecurityHeadersMiddleware:
    """Attach Content-Security-Policy and Permissions-Policy response headers.

    Both are read from settings and emitted only when configured (non-empty),
    so they stay off in development — where the Vite dev server serves the
    frontend with inline scripts and eval-based HMR that a strict CSP would
    break — and on in production, which sets the baselines in
    ``epicurrents/settings/production.py``.

    CSP is **enforced** in production (``CSP_REPORT_ONLY=False``). It shipped
    report-only while the policy had never been checked against a running
    deployment; that pass is done and the baseline permits no third-party
    origin, Pyodide included — its runtime is vendored same-origin under
    ``/vendor/pyodide/<version>/``. ``'unsafe-inline'`` remains in ``script-src``
    for the inline bootstrap scripts in the statically served ``index.html``,
    which has no per-request nonce injection point.

    A deployment that adds an origin — a project view calling out, or the dicom
    plugin's OHIF viewer, neither of which the tuning pass covered — should set
    ``CSP_REPORT_ONLY=True`` for a cycle and extend
    ``CONTENT_SECURITY_POLICY`` before trusting the default. Nothing collects
    violations server-side, so reading them means a person with the browser
    console open. See docs/operations.md → *Security headers*.

    ``setdefault`` is used so a policy set upstream (a reverse proxy, a project
    plugin) is not clobbered. Referrer-Policy is handled by Django's
    ``SecurityMiddleware`` via ``SECURE_REFERRER_POLICY`` and is not set here.
    """

    def __init__(self, get_response):
        self.get_response = get_response
        self.csp = (getattr(settings, "CONTENT_SECURITY_POLICY", "") or "").strip()
        self.csp_report_only = bool(getattr(settings, "CSP_REPORT_ONLY", True))
        self.permissions_policy = (getattr(settings, "PERMISSIONS_POLICY", "") or "").strip()
        self.no_store = not bool(getattr(settings, "DISABLE_NO_STORE_HEADERS", False))

    def __call__(self, request):
        response = self.get_response(request)
        if self.csp:
            header = "Content-Security-Policy-Report-Only" if self.csp_report_only else "Content-Security-Policy"
            response.setdefault(header, self.csp)
        if self.permissions_policy:
            response.setdefault("Permissions-Policy", self.permissions_policy)
        if self.no_store:
            # PHI hygiene: keep response bodies out of every cache — the browser
            # and any intermediary proxy. ``setdefault`` means a view that serves
            # non-PHI static assets (the content-hashed SPA bundles, the viewer
            # lib) can opt back into caching by setting its own Cache-Control.
            response.setdefault("Cache-Control", "no-store, no-cache, must-revalidate, private")
        return response


class ApiThrottleMiddleware:
    """Reject API requests that exceed their per-identity request-rate ceiling.

    Runs after ``AuthenticationMiddleware`` and ``SessionMiddleware`` so the
    identity resolution in :func:`epicurrents.throttle.check_request_throttle`
    can read ``request.user`` and the session key, and before
    ``ApiActivityLoggingMiddleware`` so a throttled flood does not generate
    ``Activity`` rows. The throttle logic, scope keying, and fail-open contract
    all live in :mod:`epicurrents.throttle`; this class only translates a
    positive verdict into a 429 with a ``Retry-After`` header.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        retry_after = check_request_throttle(request)
        if retry_after is not None:
            response = JsonResponse({"detail": "Rate limit exceeded. Please slow down."}, status=429)
            response["Retry-After"] = str(retry_after)
            return response
        return self.get_response(request)
