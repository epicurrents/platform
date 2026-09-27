"""The maintenance flag: a file in the spool that suspends the platform.

``update.sh`` writes ``update/maintenance.json`` before anything else changes
and removes it at exit; the host agent owns the same file across a remote
update's whole lifecycle. A file rather than a database row because the rollback
restores the database dump and would erase a row half-way through the very
operation it announces.

This module only reads the flag. The policy that turns a phase into a 503 lives
in ``epicurrents.middleware.MaintenanceLockMiddleware``, which needs the API
path matcher that module owns.

⚠️ LOAD-BEARING — the maintenance lock. :func:`read_lock` fails closed on a
flag it cannot parse, and that is the contract: a flag somebody wrote that
reads as "not locked" lets the platform accept writes the next rollback
silently discards. The one deliberate fail-open is a spool directory this
process cannot enter, reported as not locked because nothing can be known
about it and the status endpoint shows the defect. Contract test
``maintenance/tests/test_maintenance_lock.py``; see AGENTS.md → *Load-bearing
files*.
"""

import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from django.conf import settings

logger = logging.getLogger(__name__)

PROTOCOL = 1
PHASE_UPDATING = "updating"
PHASE_VERIFYING = "verifying"
PHASE_ROLLING_BACK = "rolling_back"
PHASES = (PHASE_UPDATING, PHASE_VERIFYING, PHASE_ROLLING_BACK)

# How long a read is trusted before the file is stat'ed again. One second keeps
# the per-request cost to nothing while a flag going up or down is noticed
# within the time a browser takes to repaint.
CACHE_SECONDS = 1.0
DEFAULT_RETRY_AFTER = 30
DEFAULT_MESSAGE = "The platform is under maintenance."


@dataclass(frozen=True)
class MaintenanceLock:
    """The parsed flag. ``malformed`` marks a file that exists but could not be read as one."""

    phase: str
    since: str | None = None
    expected_until: str | None = None
    message: str = DEFAULT_MESSAGE
    job_id: str | None = None
    protocol: int = PROTOCOL
    malformed: bool = False

    def retry_after(self, now: datetime | None = None) -> int:
        """Seconds a caller should wait: until ``expected_until`` when known, else a default."""
        deadline = parse_timestamp(self.expected_until)
        if deadline is None:
            return DEFAULT_RETRY_AFTER
        now = now or datetime.now(timezone.utc)
        return max(1, int((deadline - now).total_seconds()))

    def as_response_body(self) -> dict:
        """The JSON body of the 503; the same shape whatever the phase."""
        return {
            "detail": "maintenance",
            "phase": self.phase,
            "since": self.since,
            "expected_until": self.expected_until,
            "message": self.message,
        }


def parse_timestamp(value) -> datetime | None:
    """Parse an ISO-8601 timestamp with an optional ``Z``; ``None`` for anything else."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def spool_path() -> Path:
    """The spool root, from ``MAINTENANCE_SPOOL_PATH``."""
    return Path(settings.MAINTENANCE_SPOOL_PATH)


def lock_path() -> Path:
    """Where the flag lives."""
    return spool_path() / "maintenance.json"


def read_lock() -> MaintenanceLock | None:
    """Read the flag from disk without caching.

    Absent means not locked. Present but unreadable, unparsable, of a newer
    protocol or naming an unknown phase all fail closed: the lock is reported as
    ``updating`` with a generic message, because a flag that cannot be read was
    still written by something that meant to lock the platform. The exception
    is a spool directory this process cannot enter at all, which is reported as
    not locked: nothing can be known about a flag there, and the defect is
    visible on the status endpoint rather than as a platform-wide 503.
    """
    path = lock_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        if not os.access(path.parent, os.X_OK):
            # The whole spool is out of reach, which is a deployment defect the
            # status endpoint reports, not a flag somebody raised. Locking the
            # platform over it would turn a permissions slip into an outage.
            logger.warning("Maintenance spool at %s cannot be entered (%s); treating as not locked", path.parent, exc)
            return None
        logger.warning("Maintenance flag at %s exists but cannot be read (%s); treating as locked", path, exc)
        return MaintenanceLock(phase=PHASE_UPDATING, malformed=True)
    try:
        data = json.loads(raw)
    except ValueError:
        logger.warning("Maintenance flag at %s is not JSON; treating as locked", path)
        return MaintenanceLock(phase=PHASE_UPDATING, malformed=True)
    if not isinstance(data, dict):
        return MaintenanceLock(phase=PHASE_UPDATING, malformed=True)
    protocol = data.get("protocol")
    phase = data.get("phase")
    if not isinstance(protocol, int) or protocol > PROTOCOL or phase not in PHASES:
        logger.warning("Maintenance flag at %s has protocol %r and phase %r; treating as locked", path, protocol, phase)
        return MaintenanceLock(phase=PHASE_UPDATING, malformed=True)
    message = data.get("message")
    job_id = data.get("job_id")
    return MaintenanceLock(
        phase=phase,
        since=data.get("since") if isinstance(data.get("since"), str) else None,
        expected_until=data.get("expected_until") if isinstance(data.get("expected_until"), str) else None,
        message=message if isinstance(message, str) and message.strip() else DEFAULT_MESSAGE,
        job_id=str(job_id) if job_id is not None else None,
        protocol=protocol,
    )


_cache: dict = {"checked_at": None, "value": None}


def current_lock() -> MaintenanceLock | None:
    """The flag as of the last second, re-read from disk when the cache has aged out."""
    now = time.monotonic()
    checked_at = _cache["checked_at"]
    if checked_at is not None and now - checked_at < CACHE_SECONDS:
        return _cache["value"]
    value = read_lock()
    _cache["checked_at"] = now
    _cache["value"] = value
    return value


def invalidate_cache() -> None:
    """Forget the cached read, so the next :func:`current_lock` goes to disk."""
    _cache["checked_at"] = None
    _cache["value"] = None
