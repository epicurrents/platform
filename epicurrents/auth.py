"""Shared session-authentication helpers.

⚠️ LOAD-BEARING — CSRF enforcement on session-authenticated writes.
Every API helper that authenticates a caller via the Django **session
cookie** (the per-app ``_require_auth`` functions and the session branch
of ``_require_auth_or_federated``) MUST call :func:`enforce_session_csrf`
immediately after confirming the session user. For unsafe HTTP methods
that runs Django's CSRF check, so a session-cookie caller without a valid
CSRF token is rejected with 403.

The Ninja API operations are ``csrf_exempt`` (Django's
``CsrfViewMiddleware`` skips them), so this explicit call is the *only*
CSRF protection on the session-authenticated write surface. An endpoint
that reads ``request.user`` and acts on it without routing through a
helper that calls :func:`enforce_session_csrf` silently bypasses CSRF —
the same silent-failure shape the ``csrf-coverage`` review agent and the
contract test guard against.

**Deliberately not covered:** credentials that a browser does not send
automatically are not a CSRF vector and are exempt — the FederatedBearer
JWT (federation) and the ``?share_token=`` query param both authenticate
*outside* the session chokepoint, so they never reach this check. Pre-auth
POST endpoints (login, password-reset request/confirm) also do not call a
session helper and so are not covered here; SameSite=Lax and the existing
per-endpoint rate limits remain their defence.

Gated by ``settings.SESSION_CSRF_ENFORCED`` (default True; off in
development, where the Vite-served SPA and local tooling would otherwise
need the token). The Django test client is CSRF-exempt by default
(``enforce_csrf_checks=False``), and :func:`ninja.utils.check_csrf`
delegates to ``CsrfViewMiddleware``, which honours that — so the test
suite is unaffected.

See AGENTS.md → *Load-bearing files* and docs/operations.md → *Security
headers* before modifying.
"""

from django.conf import settings
from ninja.errors import HttpError
from ninja.utils import check_csrf

# Methods that mutate state and therefore require a CSRF token. Mirrors
# Django's CsrfViewMiddleware safe-method set (GET / HEAD / OPTIONS / TRACE
# are exempt).
_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def enforce_session_csrf(request) -> None:
    """Reject an unsafe session-cookie request that lacks a valid CSRF token.

    No-op for safe methods, when ``SESSION_CSRF_ENFORCED`` is off, or when the
    request is CSRF-exempt (the Django test client, or a request Django marked
    ``_dont_enforce_csrf_checks``). Raises ``HttpError(403)`` otherwise.

    Call this only after the caller has been confirmed session-authenticated;
    token/JWT callers must not reach it (see the module docstring).
    """
    if request.method not in _UNSAFE_METHODS:
        return
    if not getattr(settings, "SESSION_CSRF_ENFORCED", True):
        return
    # check_csrf returns an HttpResponseForbidden on failure, None on success.
    if check_csrf(request) is not None:
        raise HttpError(403, "CSRF verification failed")
