"""Global per-identity request-rate throttle for the REST API surface.

The auth endpoints already carry NAT-safe throttles (login per-username,
password-reset per-email, federation per-peer). This module adds a ceiling on
everything else — uploads, annotations, library, and the anonymous
``?share_token=`` read paths — so a single identity cannot flood the API or
enumerate share tokens unbounded.

The deployment population is the binding constraint. Instances serve NAT'd
shared-egress groups (a classroom annotating from one access point, a hospital
behind a corporate proxy) that all present as a single client IP, so a naive
per-IP throttle would lock out a whole room for one user's behaviour. The
scope key is therefore resolved per *identity* in priority order — authenticated
user, then share token, then session — and only falls back to the client IP for
callers that present none of those. The IP tier carries its own much higher
ceiling (``API_THROTTLE_IP_RATE``) so a blind anonymous flood is still bounded
without realistically reaching a shared-egress group's legitimate traffic.

Counting reuses the cache ``add`` + ``incr`` idiom from
``federation.limits`` — atomic across workers when backed by Redis, fine on
``LocMemCache`` single-process. Everything is gated by ``API_THROTTLE_ENABLED``
(off in development) and tuned through ``API_THROTTLE_RATES`` /
``API_THROTTLE_SCOPE_MAP`` / ``API_THROTTLE_IP_RATE``, all of which a project
plugin may override or zero out. A limit of ``0`` disables that tier.

The throttle fails **open**: any cache error or unexpected condition lets the
request through rather than 500-ing the API. For this feature degraded-but-
available is the correct failure direction — the cost of an outage on the happy
path outweighs the cost of a brief unthrottled window.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time

from django.conf import settings
from django.core.cache import cache

from epicurrents.security_log import get_client_ip, log_security_event

logger = logging.getLogger(__name__)

# Recognised API mount shape. Intentionally mirrors the audit middleware's
# matcher but is defined independently: the two filters answer different
# questions (audit coverage vs. throttle scope) and must be free to diverge
# without one silently dragging the other along.
_THROTTLE_PATH_RE = re.compile(r"^/(?:[^/]+/)?api/v\d+(?:/|$)")

# Cache-key prefix — namespaced so an operator inspecting Redis can grep it.
_PREFIX = "throttle:"
_WINDOW = 60


def _minute_bucket() -> str:
    """Return the current UTC minute as a string for cache-key bucketing."""
    return time.strftime("%Y%m%d%H%M", time.gmtime())


def _seconds_until_next_minute() -> int:
    """Return whole seconds remaining in the current minute bucket."""
    return _WINDOW - int(time.time()) % _WINDOW


def _scope_for_path(path: str) -> str:
    """Return the throttle scope for *path* from ``API_THROTTLE_SCOPE_MAP``.

    The map is an ordered sequence of ``(prefix, scope)`` pairs; the first
    matching prefix wins. Paths matching none fall to the ``"default"`` scope.
    """
    for prefix, scope in getattr(settings, "API_THROTTLE_SCOPE_MAP", ()):
        if path.startswith(prefix):
            return scope
    return "default"


def _identity(request) -> tuple[str, str]:
    """Resolve the NAT-safe ``(kind, key)`` the request is throttled against.

    Priority: authenticated user → ``share_token`` query param → session key →
    client IP. The share token is hashed so the raw token never lands in the
    cache keyspace.
    """
    user = getattr(request, "user", None)
    if user is not None and getattr(user, "is_authenticated", False):
        return "user", str(user.pk)
    token = request.GET.get("share_token")
    if token:
        return "token", hashlib.sha256(token.encode("utf-8")).hexdigest()[:32]
    session_key = getattr(getattr(request, "session", None), "session_key", None)
    if session_key:
        # Hashed for the same reason as the share token: the session key is
        # a bearer credential and must not land in the cache keyspace raw.
        return "session", hashlib.sha256(session_key.encode("utf-8")).hexdigest()[:32]
    return "ip", get_client_ip(request) or "unknown"


def _limit_for(kind: str, scope: str) -> int:
    """Return the per-minute request ceiling for an identity kind + scope.

    IP-keyed (unidentified) callers use the single high ``API_THROTTLE_IP_RATE``
    ceiling; identified callers use the per-scope ``API_THROTTLE_RATES`` value,
    falling back to the ``"default"`` scope rate. ``0`` means that tier is off.
    """
    if kind == "ip":
        return int(getattr(settings, "API_THROTTLE_IP_RATE", 0))
    rates = getattr(settings, "API_THROTTLE_RATES", {})
    return int(rates.get(scope, rates.get("default", 0)))


def check_request_throttle(request) -> int | None:
    """Return a ``Retry-After`` second count if the request is over its limit.

    Returns ``None`` when the throttle is disabled, the path is not an API
    mount, the relevant tier's limit is ``0``, or the caller is within budget.
    Never raises — any failure resolving the cache returns ``None`` (fail open).
    """
    if not getattr(settings, "API_THROTTLE_ENABLED", False):
        return None
    path = request.path
    if not _THROTTLE_PATH_RE.match(path):
        return None
    try:
        scope = _scope_for_path(path)
        kind, ident = _identity(request)
        limit = _limit_for(kind, scope)
        if limit <= 0:
            return None
        key = f"{_PREFIX}{scope}:{kind}:{ident}:{_minute_bucket()}"
        # Seed atomically then increment, mirroring federation.limits — the
        # minute-bucket TTL auto-expires stale counters if a worker crashes.
        cache.add(key, 0, timeout=_WINDOW + 5)
        try:
            count = cache.incr(key)
        except ValueError:
            # Race: the key expired between add and incr. Re-seed at 1.
            cache.add(key, 1, timeout=_WINDOW + 5)
            count = 1
        if count > limit:
            log_security_event(
                "throttle.rate_limited",
                scope=scope,
                identity_kind=kind,
                limit=limit,
                count=count,
                path=path,
            )
            return _seconds_until_next_minute()
    except Exception:
        logger.exception("API throttle check failed; allowing the request.")
        return None
    return None
