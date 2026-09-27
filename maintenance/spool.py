"""The spool: the files through which the web tier and the host agent talk.

Layout under ``MAINTENANCE_SPOOL_PATH`` (``./update`` in the deployment root,
``/data/update`` inside the production containers), every JSON file carrying
``protocol: 1`` and every write a temporary file renamed into place::

    agent.json                 agent heartbeat: version, enabled, runtime, last_run
    maintenance.json           the lock flag (maintenance.lock)
    erasures.jsonl             account erasures, appended by erase_user (maintenance.erasures)
    packages/<sha256>/         an uploaded package with its manifest and signature
    jobs/<id>.json             a request written by Django
    jobs/<id>.claimed.json     the same request, renamed by the agent when it takes it
    jobs/<id>.verify           a marker: the superuser confirmed the update
    jobs/<id>.rollback         a marker: the superuser asked for a rollback
    jobs/<id>.status.json      the agent's published view of the job
    jobs/<id>.log              the agent's published copy of the job log

The agent publishes; everything the web tier writes is presence to it, never
content it trusts. It claims a request by renaming ``<id>.json`` to
``<id>.claimed.json`` before it reads anything, which is what makes a cancel
decisive: :func:`remove_request` unlinks ``<id>.json`` and either succeeds, and
the agent never sees the request, or finds it gone, and the agent has it.

The spool, not the database, is the authoritative record of a host-tier job:
the rollback restores the pre-update dump and erases every row written after
it. :func:`sync` projects the spool onto ``MaintenanceJob`` rows, re-creating
the ones a restore removed, and is the only reader of ``status.json``.
"""

import logging
import os
import re
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from django.core.cache import cache
from django.db import DatabaseError, IntegrityError, models, transaction
from django.utils import timezone

from epicurrents.security_log import log_security_event
from maintenance.lock import parse_timestamp, spool_path

logger = logging.getLogger(__name__)

PROTOCOL = 1
MARKER_VERIFY = "verify"
MARKER_ROLLBACK = "rollback"
MARKERS = (MARKER_VERIFY, MARKER_ROLLBACK)

SYNC_LOCK_KEY = "maintenance:spool-sync"
SYNC_LOCK_SECONDS = 5
# A row younger than this with no file yet is a request whose on-commit write
# has not landed, not an orphan.
ORPHAN_GRACE = timedelta(seconds=60)
# The agent runs on a one-minute timer; twice that without a heartbeat means
# it is not running.
AGENT_STALE_SECONDS = 120
# Byte cap on the log tail an API caller receives.
LOG_TAIL_BYTES = 64 * 1024
# A snapshot name as the agent reports them; anything else in a heartbeat's
# snapshot list is dropped rather than shown.
SNAPSHOT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*-[0-9]{8}-[0-9]{6}$")

# The status.json fields copied onto the row, by name.
_STATUS_TEXT_FIELDS = (
    "reason",
    "step",
    "agent_version",
    "installed_version_before",
    "target_version",
    "running_version",
    "snapshot",
    "post_snapshot",
)
_STATUS_TIME_FIELDS = ("started_at", "finished_at", "verify_deadline")
# What a status file may change on a row, and all the sync writes back: the
# API's own stamps (verify_requested_at, rollback_requested_at) stay untouched
# even when they were set after the row was loaded.
_STATUS_ROW_FIELDS = (
    "state",
    "in_flight",
    *_STATUS_TEXT_FIELDS,
    *_STATUS_TIME_FIELDS,
    "migrations_applied",
    "spool_updated_at",
    "last_notified_state",
)


def jobs_dir() -> Path:
    """The directory of request, marker, status and log files."""
    return spool_path() / "jobs"


def packages_dir() -> Path:
    """The directory of uploaded packages."""
    return spool_path() / "packages"


def request_path(job_id) -> Path:
    """The request file of ``job_id``."""
    return jobs_dir() / f"{job_id}.json"


def claimed_path(job_id) -> Path:
    """The request file of ``job_id`` once the agent has claimed it."""
    return jobs_dir() / f"{job_id}.claimed.json"


def status_path(job_id) -> Path:
    """The agent's status file of ``job_id``."""
    return jobs_dir() / f"{job_id}.status.json"


def marker_path(job_id, kind: str) -> Path:
    """The ``.verify`` or ``.rollback`` marker of ``job_id``."""
    if kind not in MARKERS:
        raise ValueError(f"Unknown marker kind {kind!r}")
    return jobs_dir() / f"{job_id}.{kind}"


def log_path(job_id) -> Path:
    """The agent's log of ``job_id``."""
    return jobs_dir() / f"{job_id}.log"


def now_iso() -> str:
    """The current UTC time as the spool spells it."""
    return timezone.now().replace(microsecond=0).isoformat().replace("+00:00", "Z")


def write_json_atomic(path: Path, data: dict) -> None:
    """Write ``data`` to ``path`` through a temporary file in the same directory."""
    import json

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def read_json(path: Path) -> dict | None:
    """Read a JSON object from ``path``; ``None`` when absent, unreadable, not JSON or not an object."""
    import json

    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.warning("Spool file %s cannot be read: %s", path.name, exc)
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        logger.warning("Spool file %s is not JSON", path.name)
        return None
    if not isinstance(data, dict):
        logger.warning("Spool file %s is not a JSON object", path.name)
        return None
    protocol = data.get("protocol")
    if not isinstance(protocol, int) or protocol > PROTOCOL:
        logger.warning("Spool file %s carries protocol %r, newer than %d; ignored", path.name, protocol, PROTOCOL)
        return None
    return data


def is_writable() -> bool:
    """Whether this process can write requests into the spool.

    Checks the jobs directory when it exists, since that is where requests go
    and an agent that created it as root would leave it unwritable, and the
    spool root otherwise, where the directory would be created.
    """
    root = spool_path()
    if not root.is_dir():
        return False
    jobs = jobs_dir()
    target = jobs if jobs.is_dir() else root
    return os.access(target, os.W_OK)


def write_request(job) -> Path:
    """Write the request file the agent picks up. Called once the job row has committed."""
    path = request_path(job.job_id)
    write_json_atomic(
        path,
        {
            "protocol": PROTOCOL,
            "job_id": str(job.job_id),
            "operation": job.operation,
            "requested_by_id": job.requested_by_id,
            "requested_at": now_iso(),
            "args": job.args,
        },
    )
    return path


def remove_request(job) -> bool:
    """Withdraw the request before the agent claims it; ``False`` when it had already been claimed or was gone.

    The unlink is the whole decision. The agent claims by renaming the same
    file, so exactly one of the two succeeds; a caller that gets ``False`` must
    leave the row to the agent.
    """
    try:
        request_path(job.job_id).unlink()
    except FileNotFoundError:
        return False
    return True


def write_marker(job, kind: str, *, by_user_id) -> Path:
    """Write a ``.verify`` or ``.rollback`` marker for the agent."""
    path = marker_path(job.job_id, kind)
    write_json_atomic(path, {"protocol": PROTOCOL, "at": now_iso(), "by_user_id": by_user_id})
    return path


def read_request(job_id) -> dict | None:
    """The request file, parsed, whether or not the agent has claimed it."""
    return read_json(request_path(job_id)) or read_json(claimed_path(job_id))


def read_status(job_id) -> dict | None:
    """The agent's status file, parsed."""
    return read_json(status_path(job_id))


def read_agent() -> dict | None:
    """The agent heartbeat, parsed."""
    return read_json(spool_path() / "agent.json")


def _snapshot_rows(value) -> list[dict]:
    """The heartbeat's snapshot list, kept to rows of the expected shape; anything else is dropped."""
    rows: list[dict] = []
    if not isinstance(value, list):
        return rows
    for item in value:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if not isinstance(name, str) or not SNAPSHOT_NAME_RE.match(name):
            continue
        migrations = item.get("migrations")
        version = item.get("version")
        rows.append(
            {
                "name": name,
                "taken_at": item["taken_at"] if isinstance(item.get("taken_at"), str) else None,
                "version": str(version) if version not in (None, "") else None,
                "code": bool(item.get("code")),
                "migrations": migrations if migrations in ("none", "applied") else None,
            }
        )
    return rows


def _optional_str(value) -> str | None:
    return str(value) if value not in (None, "") else None


def agent_summary(now: datetime | None = None) -> dict:
    """What the status endpoint says about the agent, from its heartbeat file."""
    data = read_agent()
    if data is None:
        return {
            "installed": False,
            "enabled": None,
            "version": None,
            "runtime": None,
            "last_run": None,
            "stale": None,
            "capabilities": [],
            "updater_script": None,
            "self_update": None,
            "key_id": None,
            "next_key_id": None,
            "snapshots": [],
        }
    now = now or timezone.now()
    last_run = data.get("last_run") if isinstance(data.get("last_run"), str) else None
    seen = parse_timestamp(last_run)
    stale = seen is None or (now - seen).total_seconds() > AGENT_STALE_SECONDS
    capabilities = data.get("capabilities")
    # The agent writes numbers as strings; either spelling is accepted.
    updater_script = data.get("updater_script")
    if isinstance(updater_script, str) and updater_script.isdigit():
        updater_script = int(updater_script)
    elif isinstance(updater_script, bool) or not isinstance(updater_script, int):
        updater_script = None
    return {
        "installed": True,
        "enabled": bool(data.get("enabled")),
        "version": str(data.get("version")) if data.get("version") is not None else None,
        "runtime": str(data.get("runtime")) if data.get("runtime") is not None else None,
        "last_run": last_run,
        "stale": stale,
        "capabilities": [str(item) for item in capabilities] if isinstance(capabilities, list) else [],
        "updater_script": updater_script,
        "self_update": data["self_update"] if isinstance(data.get("self_update"), bool) else None,
        "key_id": _optional_str(data.get("key_id")),
        "next_key_id": _optional_str(data.get("next_key_id")),
        "snapshots": _snapshot_rows(data.get("snapshots")),
    }


def read_log_tail(job_id, limit: int = LOG_TAIL_BYTES) -> tuple[str, bool, int]:
    """The last ``limit`` bytes of the agent's log: ``(text, truncated, total_bytes)``."""
    path = log_path(job_id)
    try:
        total = path.stat().st_size
        with path.open("rb") as handle:
            if total > limit:
                handle.seek(total - limit)
            data = handle.read()
    except FileNotFoundError:
        return "", False, 0
    return data.decode("utf-8", errors="replace"), total > limit, total


# ── Reconciliation ───────────────────────────────────────────────────────────


def _log_state(job) -> None:
    """Record a state the agent reported in the security log, which a database restore cannot erase."""
    log_security_event(
        "maintenance.job_state",
        job_id=str(job.job_id),
        operation=job.operation,
        state=job.state,
        reason=job.reason or None,
    )


def _job_id_of(path: Path, suffix: str):
    """The job id a file in ``jobs/`` is named for, when it ends in ``suffix`` and the rest is a UUID."""
    name = path.name
    if not name.endswith(suffix):
        return None
    try:
        return uuid.UUID(name[: -len(suffix)])
    except ValueError:
        return None


def _scan_jobs() -> tuple[dict, dict]:
    """Request and status files keyed by job id. Files with an unusable name are skipped."""
    requests: dict = {}
    statuses: dict = {}
    directory = jobs_dir()
    if not directory.is_dir():
        return requests, statuses
    for path in directory.iterdir():
        if not path.is_file():
            continue
        # The longer suffixes first: "<id>.status.json" and "<id>.claimed.json"
        # both end in ".json", and a claimed request is still a request.
        job_id = _job_id_of(path, ".status.json")
        if job_id is not None:
            data = read_json(path)
            if data is not None:
                statuses[job_id] = data
            continue
        job_id = _job_id_of(path, ".claimed.json")
        if job_id is None:
            job_id = _job_id_of(path, ".json")
        if job_id is not None:
            data = read_json(path)
            if data is not None:
                requests.setdefault(job_id, data)
    return requests, statuses


def _status_allowed(current: str, new: str) -> bool:
    """Whether a status file may move a row from ``current`` to ``new``.

    A cancelled row is final: the cancel won the race for the request file, so
    any status naming the job is a stray. A settled row never goes back in
    flight, with the one move the protocol has for it, a late rollback of a
    succeeded update; a row settled by the web tier (abandoned, stale) may
    still learn the outcome the agent reports.
    """
    from maintenance.models import MaintenanceJob

    if current == MaintenanceJob.State.CANCELLED:
        return False
    if current in MaintenanceJob.IN_FLIGHT_STATES or new not in MaintenanceJob.IN_FLIGHT_STATES:
        return True
    return current == MaintenanceJob.State.SUCCEEDED and new == MaintenanceJob.State.ROLLING_BACK


def _clip(name: str, value: str) -> str:
    """``value`` cut to the column's length: an overlong field in a status file must not fail every sync."""
    from maintenance.models import MaintenanceJob

    limit = MaintenanceJob._meta.get_field(name).max_length
    return value[:limit] if limit else value


def _apply_status(job, status: dict) -> bool:
    """Copy a newer status file onto ``job``; returns whether anything changed."""
    from maintenance.models import MaintenanceJob

    updated_at = parse_timestamp(status.get("updated_at"))
    if updated_at is None:
        logger.warning("Status file of job %s has no usable updated_at; ignored", job.job_id)
        return False
    if job.spool_updated_at is not None and updated_at <= job.spool_updated_at:
        return False
    state = status.get("state")
    if state not in MaintenanceJob.State.values:
        logger.warning("Status file of job %s names unknown state %r; ignored", job.job_id, state)
        return False
    if job.pk is not None and not _status_allowed(job.state, state):
        logger.warning("Status file of job %s would move it from %s to %s; ignored", job.job_id, job.state, state)
        return False
    job.state = state
    job.in_flight = state in MaintenanceJob.IN_FLIGHT_STATES
    for name in _STATUS_TEXT_FIELDS:
        value = status.get(name)
        if name == "target_version" and value in (None, ""):
            # A rollback's status file names no target; the row's, taken from
            # the snapshot when the request was made, stands.
            continue
        setattr(job, name, _clip(name, str(value)) if value is not None else "")
    for name in _STATUS_TIME_FIELDS:
        setattr(job, name, parse_timestamp(status.get(name)))
    applied = status.get("migrations_applied")
    job.migrations_applied = applied if isinstance(applied, bool) else None
    job.spool_updated_at = updated_at
    return True


def _row_from_request(job_id, request: dict, status: dict | None):
    """Build an unsaved row for a request file that has no row — after a restore, typically."""
    from django.contrib.auth import get_user_model

    from maintenance.models import MaintenanceJob, MaintenancePackage
    from maintenance.operations import get_operation

    operation = str(request.get("operation") or "")
    registered = get_operation(operation)
    args = request.get("args") if isinstance(request.get("args"), dict) else {}
    requested_by = None
    if isinstance(request.get("requested_by_id"), int):
        requested_by = get_user_model().objects.filter(pk=request["requested_by_id"]).first()
    package = None
    sha256 = args.get("package_sha256")
    if isinstance(sha256, str):
        package = MaintenancePackage.objects.filter(sha256=sha256).first()
    job = MaintenanceJob(
        job_id=job_id,
        operation=operation,
        executor=registered.executor if registered else MaintenanceJob.Executor.HOST,
        requested_by=requested_by,
        package=package,
        args=args,
    )
    requested_at = parse_timestamp(request.get("requested_at"))
    if requested_at is not None:
        job.created_at = requested_at
    # What the request endpoint had worked out at the time: a package's
    # version, or the version the snapshot to restore was taken at. A status
    # file that names a target overrides it below; a rollback's names none.
    if package is not None:
        job.target_version = package.version
    elif isinstance(args.get("snapshot"), str):
        known = {row["name"]: row for row in agent_summary()["snapshots"]}
        job.target_version = (known.get(args["snapshot"]) or {}).get("version") or ""
    if status is not None:
        _apply_status(job, status)
    return job


def overlay(jobs):
    """Show each in-flight host-tier row with its newer status file applied, in memory only.

    The read path while the lock phase is ``updating`` or ``rolling_back``: the
    database is between the pre-update dump and the recreate, and a row written
    now is lost to the restore or lands in a schema about to change, so the job
    page answers from the spool without :func:`sync`. Returns ``jobs``.
    """
    from maintenance.models import MaintenanceJob

    for job in jobs:
        if job.executor == MaintenanceJob.Executor.HOST and job.in_flight:
            status = read_status(job.job_id)
            if status is not None:
                _apply_status(job, status)
    return jobs


def restored_database(job) -> bool:
    """Whether ``job`` settled in a state whose outcome put an older database in place.

    A rolled-back update (conservatively: the agent keeps the database only when
    no migration ran, and the re-erasure that follows is harmless when it did),
    or a ``platform.rollback`` that did not ask to keep the database.
    """
    from maintenance.models import MaintenanceJob

    if job.operation == "platform.update":
        return job.state == MaintenanceJob.State.ROLLED_BACK
    if job.operation == "platform.rollback":
        return job.state == MaintenanceJob.State.SUCCEEDED and (job.args or {}).get("restore_database") is not False
    return False


def _dispatch_erasure_reapply() -> None:
    """Queue ``reapply_erasures`` for after the commit; a broker that is down is logged, and the next restore retries."""

    def _dispatch():
        try:
            from maintenance.tasks import reapply_erasures

            reapply_erasures.delay()
        except Exception:
            logger.exception("Re-applying erasures after a database restore could not be dispatched")

    transaction.on_commit(_dispatch)


def _save(job, *, notify: bool, **kwargs) -> bool:
    """Save ``job`` in its own savepoint, queueing its notice only if the save lands; ``False`` when it did not."""
    from maintenance.notify import dispatch_notice

    try:
        with transaction.atomic():
            job.save(**kwargs)
            if notify:
                dispatch_notice(job, job.state)
    except IntegrityError:
        # The spool reports this job in flight while another row is. Cannot
        # both be true; the row keeps its state and the next tick tries again
        # once the other job has settled.
        logger.warning("Job %s from the spool would put a second job in flight; not saved this tick", job.job_id)
        return False
    except DatabaseError:
        logger.exception("Job %s could not be saved from the spool; not saved this tick", job.job_id)
        return False
    return True


def sync(*, force: bool = False) -> dict:
    """Project the spool onto the job rows.

    Applies each newer ``status.json`` to its row, re-creates rows whose request
    file (claimed or not) exists without one, and fails an in-flight host-tier
    row that has neither file as ``orphaned``. Runs under a short cache lock so
    the callers — every maintenance read, the top of every write, the beat task
    — do not stampede; ``force`` skips the lock. Opens an audited scope only
    when there is something to write, so the minute tick does not inflate the
    audit trail.

    Each row is saved before anything is announced: the notice is queued in the
    save's own transaction and sent from a worker after the commit, so a status
    that cannot be saved announces nothing and a slow relay never runs under the
    lock. A job that settles having restored the database queues the re-erasure
    of the accounts the restore brought back (``maintenance.erasures``).
    """
    from activity.models import Activity
    from activity.system_activity import with_system_activity
    from maintenance.models import MaintenanceJob
    from maintenance.notify import needs_notice

    counts = {"applied": 0, "created": 0, "orphaned": 0, "notified": 0, "skipped": False}
    if not force and not cache.add(SYNC_LOCK_KEY, 1, timeout=SYNC_LOCK_SECONDS):
        counts["skipped"] = True
        return counts

    # Package rows first: a request re-created below links to its package by
    # hash, and the same restore that erased the job rows erased those.
    from maintenance import packaging

    counts["packages"] = packaging.reconcile()

    requests, statuses = _scan_jobs()
    # The in-flight rows (orphan candidates) and the rows the spool names; not
    # every host-tier row ever, which the audit record keeps indefinitely.
    named = list(requests.keys() | statuses.keys())
    rows = {
        job.job_id: job
        for job in MaintenanceJob.objects.filter(executor=MaintenanceJob.Executor.HOST).filter(
            models.Q(in_flight=True) | models.Q(job_id__in=named)
        )
    }

    to_apply = []
    for job_id, status in statuses.items():
        job = rows.get(job_id)
        if job is None:
            continue
        previous = job.state
        if _apply_status(job, status):
            to_apply.append((job, previous))
    to_create = []
    for job_id, request in requests.items():
        if job_id not in rows:
            to_create.append(_row_from_request(job_id, request, statuses.get(job_id)))
    cutoff = timezone.now() - ORPHAN_GRACE
    to_orphan = [
        job
        for job_id, job in rows.items()
        if job.in_flight and job_id not in requests and job_id not in statuses and job.created_at < cutoff
    ]
    if not (to_apply or to_create or to_orphan):
        return counts

    reapply = False
    with with_system_activity(
        "maintenance.job.sync",
        interface=Activity.Interface.CELERY,
        metadata={"applied": len(to_apply), "created": len(to_create), "orphaned": len(to_orphan)},
    ):
        for job, previous in to_apply:
            announce = needs_notice(job)
            if announce:
                job.last_notified_state = job.state
            if not _save(job, notify=announce, update_fields=_STATUS_ROW_FIELDS):
                continue
            counts["notified"] += int(announce)
            _log_state(job)
            if job.state == MaintenanceJob.State.SUCCEEDED:
                packaging.mark_applied(job.package)
            if previous != job.state and restored_database(job):
                reapply = True
            counts["applied"] += 1
        for job in to_orphan:
            job.state = MaintenanceJob.State.FAILED
            job.reason = "orphaned"
            job.in_flight = False
            job.finished_at = timezone.now()
            announce = needs_notice(job)
            if announce:
                job.last_notified_state = job.state
            fields = ["state", "reason", "in_flight", "finished_at", "last_notified_state"]
            if not _save(job, notify=announce, update_fields=fields):
                continue
            counts["notified"] += int(announce)
            _log_state(job)
            counts["orphaned"] += 1
        for job in to_create:
            announce = needs_notice(job)
            if announce:
                job.last_notified_state = job.state
            if not _save(job, notify=announce):
                continue
            counts["notified"] += int(announce)
            if job.state == MaintenanceJob.State.SUCCEEDED:
                packaging.mark_applied(job.package)
            if restored_database(job):
                reapply = True
            counts["created"] += 1
    if reapply:
        _dispatch_erasure_reapply()
    return counts
