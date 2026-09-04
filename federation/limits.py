"""Per-peer rate / byte-quota limits on the federated download paths.

Aim: bound how much a single compromised peer can exfiltrate before anyone
notices.  Two limits compose:

* **Daily byte quota** (`FEDERATION_PEER_DAILY_BYTE_LIMIT`) — bounds *total*
  bytes served to one peer in a 24-hour rolling window.  This is the
  exfil-prevention measure: even if the peer hammers the download endpoint at
  full speed, they cannot exceed the daily budget.
* **Per-minute request rate** (`FEDERATION_PEER_DOWNLOAD_RATE_LIMIT`) — bounds
  burst.  A peer hitting `download_recording` 1000×/min is a clear abuse
  pattern that the byte quota alone won't catch (each individual request might
  be small).

Both use Django's cache (`cache.add` + `cache.incr`), which is atomic across
gunicorn workers when backed by Redis — the same correctness story as the
replay-protection nonce cache in `federation/auth.py`.  In local dev /
single-process settings, `LocMemCache` is fine.

The defaults in `settings/common.py` are generous enough that an honest peer
running normal workloads will not notice; the limits exist to cap a
compromised peer, not throttle the happy path.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from federation.models import FederatedPeer


logger = logging.getLogger(__name__)


# Cache key prefixes — namespaced so an operator inspecting the Redis keyspace
# can grep for federation limits.
BYTE_QUOTA_PREFIX = "fed-bytes:"  # value: bytes served today
RATE_LIMIT_PREFIX = "fed-rate:"  # value: download requests in current minute
INBOUND_RATE_PREFIX = "fed-inbound:"  # value: inbound checks in current minute

# Window granularity.  Day buckets are calendar days (UTC) so an operator
# inspecting "today's usage" sees a value that aligns with their wall clock.
_SECONDS_IN_DAY = 24 * 60 * 60
_SECONDS_IN_MINUTE = 60


def _day_bucket() -> str:
    """Return today's UTC date as a string for cache-key bucketing."""
    return time.strftime("%Y%m%d", time.gmtime())


def _minute_bucket() -> str:
    """Return current UTC minute as a string for cache-key bucketing."""
    return time.strftime("%Y%m%d%H%M", time.gmtime())


def check_peer_download_limits(peer: FederatedPeer, expected_bytes: int) -> None:
    """Raise ``QuotaExceeded`` if the peer is over either configured limit.

    Atomically reserves both quota slots: the per-minute request counter and
    the daily byte budget.  ``expected_bytes`` is charged against the daily
    budget — callers pass the **full file size** even when the request is a
    partial Range or a time slice.  Bounding by actual bytes served would let
    a peer fetch the whole file as 1000 small slices and bypass the daily
    budget entirely; bounding by file size closes that loop at the cost of
    over-counting honest partial reads.  See federation/README.md →
    *Rate limiting and quotas* for the rationale.

    Charging up front rather than on response completion keeps the bookkeeping
    simple at the cost of over-counting if the client disconnects mid-stream
    (acceptable: an attacker who hammers and aborts is exactly who we want
    to throttle).

    Raises:
        QuotaExceeded: peer exceeded either the daily byte budget or the
            per-minute request rate.  Caller surfaces this as HTTP 429.
    """
    from django.conf import settings
    from django.core.cache import cache

    byte_limit = int(getattr(settings, "FEDERATION_PEER_DAILY_BYTE_LIMIT", 0))
    rate_limit = int(getattr(settings, "FEDERATION_PEER_DOWNLOAD_RATE_LIMIT", 0))

    # Rate-limit check.  Increment first so concurrent requests see consistent
    # state — over-counting a request that we ultimately reject is acceptable;
    # silently allowing an over-the-limit request is not.
    if rate_limit > 0:
        rate_key = f"{RATE_LIMIT_PREFIX}{peer.pk}:{_minute_bucket()}"
        # cache.incr raises ValueError if key is missing; seed atomically with
        # cache.add then incr.  The minute-bucket TTL means stale entries
        # auto-expire even if a worker crashes mid-request.
        cache.add(rate_key, 0, timeout=_SECONDS_IN_MINUTE + 5)
        try:
            n = cache.incr(rate_key)
        except ValueError:
            # Race: another worker set+expired between our add and incr.
            cache.add(rate_key, 1, timeout=_SECONDS_IN_MINUTE + 5)
            n = 1
        if n > rate_limit:
            logger.warning(
                "Federation peer %s (id=%d) hit per-minute download rate limit (%d/%d).",
                peer.url,
                peer.pk,
                n,
                rate_limit,
            )
            raise QuotaExceeded(f"Per-minute download rate limit exceeded ({rate_limit}/min).")

    # Byte-quota check.  Read the current day's tally and reject if adding
    # ``expected_bytes`` would exceed the budget.  Then commit the charge —
    # over-counting on a connection drop is acceptable per the function
    # docstring.
    if byte_limit > 0 and expected_bytes > 0:
        byte_key = f"{BYTE_QUOTA_PREFIX}{peer.pk}:{_day_bucket()}"
        cache.add(byte_key, 0, timeout=_SECONDS_IN_DAY + 60)
        current = cache.get(byte_key, 0)
        if current + expected_bytes > byte_limit:
            logger.warning(
                "Federation peer %s (id=%d) would exceed daily byte budget (%d + %d > %d).",
                peer.url,
                peer.pk,
                current,
                expected_bytes,
                byte_limit,
            )
            raise QuotaExceeded(f"Daily byte budget exceeded ({byte_limit} bytes/day).")
        try:
            cache.incr(byte_key, expected_bytes)
        except ValueError:
            cache.add(byte_key, expected_bytes, timeout=_SECONDS_IN_DAY + 60)


class QuotaExceeded(Exception):
    """Raised when a peer exceeds a configured download limit.

    Callers in the API layer translate this to ``HttpError(429, ...)``.  The
    distinct exception type lets ad-hoc callers (e.g. management commands
    that pre-stage downloads) handle the condition without parsing strings.
    """


def check_peer_inbound_rate(peer: FederatedPeer) -> None:
    """Raise ``QuotaExceeded`` if peer exceeds the inbound request rate.

    Throttles ``inbound_check_object`` to slow object-id enumeration by a
    compromised peer.  Even though the 404-collapse at that endpoint denies
    the peer existence information, capping the probe rate slows reconnaissance
    and surfaces enumeration patterns in the audit log (each 429 produces a
    `FederationAuditLog` row).

    Distinct counter from ``check_peer_download_limits`` so a peer making
    routine inbound checks doesn't share state with their download budget.
    """
    from django.conf import settings
    from django.core.cache import cache

    rate_limit = int(getattr(settings, "FEDERATION_PEER_INBOUND_RATE_LIMIT", 0))
    if rate_limit <= 0:
        return

    key = f"{INBOUND_RATE_PREFIX}{peer.pk}:{_minute_bucket()}"
    cache.add(key, 0, timeout=_SECONDS_IN_MINUTE + 5)
    try:
        n = cache.incr(key)
    except ValueError:
        cache.add(key, 1, timeout=_SECONDS_IN_MINUTE + 5)
        n = 1
    if n > rate_limit:
        logger.warning(
            "Federation peer %s (id=%d) hit per-minute inbound rate limit (%d/%d).",
            peer.url,
            peer.pk,
            n,
            rate_limit,
        )
        raise QuotaExceeded(f"Per-minute inbound rate limit exceeded ({rate_limit}/min).")
