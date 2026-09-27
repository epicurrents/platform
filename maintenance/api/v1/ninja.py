"""Maintenance API v1 — status, the operation registry, and jobs.

Mounted at ``/api/v1/maintenance/``. Every operation but the lock probe calls
``_gate_enabled`` first, which answers 404 while ``REMOTE_MAINTENANCE_ENABLED``
is off so the surface does not exist on a deployment that has not opted in, and
then one of the tier guards. Host-tier writes additionally answer 403 while
``REMOTE_UPDATE_ENABLED`` is off. The lock probe is public and ungated because
``update.sh``'s manual path raises the flag on deployments without the feature,
and the SPA needs to see it come down.

Nothing here executes anything. A request becomes a ``MaintenanceJob`` row and,
on commit, either a Celery dispatch or a request file in the spool; the
executor is elsewhere, and the source scan in ``tests/test_no_host_execution.py``
keeps it that way.

Endpoints
---------
GET  /lock                   the maintenance flag, for the SPA to poll              (public, ungated)
GET  /status                 flags, versions, spool and agent state, step-up method   (staff)
GET  /operations             the registry with a JSON schema per operation           (staff)
GET  /jobs                   recent jobs                                              (staff)
POST /jobs                   request an operation                                     (superuser, step-up)
GET  /jobs/{job_id}          one job                                                  (staff)
GET  /jobs/{job_id}/log      the host agent's log tail, or the command output          (superuser)
POST /jobs/{job_id}/cancel   withdraw a request the executor has not picked up        (superuser)
POST /jobs/{job_id}/verify   confirm an update                                        (superuser, password)
POST /jobs/{job_id}/rollback ask for a rollback                                       (superuser, step-up)
POST /jobs/{job_id}/abandon  fail a job whose executor is gone                         (superuser, step-up)
GET  /packages               uploaded update packages                                 (staff)
POST /packages               upload a package: tarball, manifest, signature           (superuser)
DELETE /packages/{sha256}    remove an uploaded package                               (superuser)
"""

import logging
import re
import uuid
from datetime import UTC, datetime

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone
from ninja import NinjaAPI, Schema
from ninja.errors import HttpError
from pydantic import ValidationError

from activity.audit import log_activity
from epicurrents.auth import enforce_session_csrf
from epicurrents.security_log import get_client_ip, log_security_event
from epicurrents.version import VERSION_INFO, __version__
from maintenance import erasures, packaging, spool
from maintenance.lock import PHASE_VERIFYING, current_lock, read_lock
from maintenance.models import MaintenanceJob, MaintenancePackage
from maintenance.operations import HOST, get_operation, registered_operations
from user.stepup import confirm_step_up, step_up_method

api = NinjaAPI(
    title="Maintenance API",
    version="1",
    urls_namespace="maintenance-api-v1",
    docs_url=settings.API_DOCS_URL,
    openapi_url=settings.API_OPENAPI_URL,
)

logger = logging.getLogger(__name__)

JOB_LIST_LIMIT = 50
# The one operation with a verification window, and so the one the verify and
# rollback endpoints act on.
UPDATE_OPERATION = "platform.update"
RESTORE_OPERATION = "platform.rollback"
# The date and time update.sh stamps on a snapshot name, in UTC.
_SNAPSHOT_STAMP_RE = re.compile(r"-([0-9]{8})-([0-9]{6})$")


# ── Schemas ──────────────────────────────────────────────────────────────────


class SnapshotOut(Schema):
    """A snapshot on the host, as the agent's heartbeat lists it. Only the name is ever sent back."""

    name: str
    taken_at: str | None
    version: str | None
    code: bool
    migrations: str | None


class AgentOut(Schema):
    """The host agent as its heartbeat file describes it."""

    installed: bool
    enabled: bool | None
    version: str | None
    runtime: str | None
    last_run: str | None
    stale: bool | None
    capabilities: list[str]
    updater_script: int | None
    self_update: bool | None
    key_id: str | None
    next_key_id: str | None
    snapshots: list[SnapshotOut]


class LockOut(Schema):
    """The maintenance flag, when present."""

    phase: str
    since: str | None
    expected_until: str | None
    message: str
    job_id: str | None


class LockProbeOut(Schema):
    """The maintenance flag as the SPA polls it: ``locked`` and, while it is, what the flag says."""

    locked: bool
    phase: str | None
    since: str | None
    expected_until: str | None
    message: str | None


class ConflictOut(Schema):
    """A 409 with a reason the client acts on; ``erasures`` counts the accounts a restore would bring back."""

    detail: str
    reason: str | None = None
    erasures: int | None = None


class StepUpOut(Schema):
    """How the caller confirms a sensitive request, or why they cannot."""

    method: str | None
    available: bool
    reason: str | None


class StatusOut(Schema):
    """What the Maintenance tab needs to render its header."""

    remote_maintenance_enabled: bool
    remote_update_enabled: bool
    installed_version: str
    server_now: str
    spool_writable: bool
    release_key_present: bool
    release_key_ids: list[str]
    agent: AgentOut
    lock: LockOut | None
    in_flight_job: str | None
    step_up: StepUpOut


class OperationOut(Schema):
    """One registered operation, with the JSON schema of its arguments."""

    key: str
    executor: str
    label: str
    description: str
    requires_step_up: bool
    available: bool
    args_schema: dict


class JobOut(Schema):
    """One job. ``output`` is present only for superusers."""

    job_id: str
    operation: str
    executor: str
    state: str
    reason: str
    step: str
    in_flight: bool
    requested_by: str | None
    package_sha256: str | None
    args: dict
    created_at: str
    started_at: str | None
    finished_at: str | None
    verify_deadline: str | None
    verify_requested_at: str | None
    rollback_requested_at: str | None
    installed_version_before: str
    target_version: str
    running_version: str
    snapshot: str
    post_snapshot: str
    migrations_applied: bool | None
    output: str | None = None


class JobCreateIn(Schema):
    """A request for an operation. ``args`` is validated against the operation's own schema.

    ``operation`` is optional at the schema level so that a malformed body on a
    deployment with the feature off still answers 404 from the gate rather than
    a 422 that reveals the route exists; the view refuses a blank one.
    """

    operation: str = ""
    args: dict = {}
    password: str | None = None
    totp_code: str | None = None
    acknowledge_erasures: bool = False


class ConfirmIn(Schema):
    """Credentials for a step-up confirmation, and for a rollback the acknowledgement of erasures it undoes."""

    password: str | None = None
    totp_code: str | None = None
    acknowledge_erasures: bool = False


class LogOut(Schema):
    """The tail of a job's log."""

    job_id: str
    log: str
    truncated: bool
    bytes: int


class PackageOut(Schema):
    """One uploaded package. The hash is the identifier; no path is ever returned."""

    sha256: str
    version: str
    project: str
    plugins: list[str]
    platform_compatible: str
    built_at: str | None
    size: int
    key_id: str
    agent_version: int
    uploaded_by: str | None
    uploaded_at: str
    state: str
    applicable: bool


class RejectionOut(Schema):
    """Why an upload was refused: a message for the reader and a stable token for the client."""

    detail: str
    reason: str


# ── Guards ───────────────────────────────────────────────────────────────────


def _gate_enabled(request) -> None:
    """404 unless the deployment opted in. Named without ``_require_`` so the auth sweep does not mistake it for one."""
    if not getattr(settings, "REMOTE_MAINTENANCE_ENABLED", False):
        raise HttpError(404, "Not found")


def _deny(request, permission: str, user=None) -> None:
    """Record a refused caller in the security log; the guards raise the response."""
    log_security_event(
        "permission.denied",
        permission=permission,
        actor_id=getattr(user, "pk", None),
        ip=get_client_ip(request),
        path=request.path,
        method=request.method,
    )


def _authenticated(request):
    """The authenticated user, or a logged 401. No CSRF check: the callers make it once the role is known."""
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated:
        _deny(request, "maintenance.authenticated")
        raise HttpError(401, "Not authenticated")
    return user


def _require_auth(request):
    """Return the authenticated user or raise 401.

    Routes the request through the session-CSRF chokepoint; see AGENTS.md →
    *Session-authenticated write CSRF*.
    """
    user = _authenticated(request)
    enforce_session_csrf(request)
    return user


def _require_staff(request):
    """Return an authenticated staff (or superuser) user or raise 403, logging a refusal.

    The role is checked before the CSRF chokepoint, which on a multipart POST
    parses the body: a caller without the role must not get an upload spooled
    to disk by asking.
    """
    user = _authenticated(request)
    if not (user.is_staff or user.is_superuser):
        _deny(request, "maintenance.staff", user)
        raise HttpError(403, "Staff access required.")
    enforce_session_csrf(request)
    return user


def _require_superuser(request):
    """Return an authenticated superuser or raise 403, logging a refusal; CSRF after the role, as above."""
    user = _authenticated(request)
    if not user.is_superuser:
        _deny(request, "maintenance.superuser", user)
        raise HttpError(403, "Superuser access required.")
    enforce_session_csrf(request)
    return user


def _host_tier_enabled() -> bool:
    return bool(getattr(settings, "REMOTE_UPDATE_ENABLED", False))


def _require_host_tier(operation) -> None:
    """403 for a host-tier operation while the host tier is switched off."""
    if operation.executor == HOST and not _host_tier_enabled():
        raise HttpError(403, "Remote updates are disabled on this deployment (REMOTE_UPDATE_ENABLED).")


def _refuse_while_locked() -> None:
    """409 while the maintenance flag is up in any phase, read fresh rather than through the cache.

    Starting a job, uploading or removing a package in the verification window
    changes the deployment under an update nobody has confirmed yet, and a
    superuser is exempt from the lock's own refusal in that phase.
    """
    flag = read_lock()
    if flag is not None:
        raise HttpError(409, f"The platform is under maintenance ({flag.phase}); try again once it is over.")


def _sync_for_read() -> bool:
    """Run the spool sync unless the lock phase is ``updating`` or ``rolling_back``; returns whether it ran.

    In those phases the database is between the pre-update dump and the
    recreate, so a read writes nothing and answers from the spool through
    ``spool.overlay`` instead.
    """
    flag = current_lock()
    if flag is not None and flag.phase != PHASE_VERIFYING:
        return False
    spool.sync()
    return True


def _snapshot_taken_at(name: str | None) -> datetime | None:
    """When a snapshot was taken: from the heartbeat when it lists the snapshot, else from the name's stamp."""
    if not name:
        return None
    for row in spool.agent_summary()["snapshots"]:
        if row["name"] == name and row["taken_at"]:
            taken = spool.parse_timestamp(row["taken_at"])
            if taken is not None:
                return taken
    match = _SNAPSHOT_STAMP_RE.search(name)
    if match is None:
        return None
    try:
        return datetime.strptime("".join(match.groups()), "%Y%m%d%H%M%S").replace(tzinfo=UTC)
    except ValueError:
        return None


def _erasure_conflict(taken_at: datetime | None, *, acknowledged: bool) -> dict | None:
    """The 409 body when restoring a database taken at ``taken_at`` would undo erasures made since, or ``None``.

    An unknown time counts every recorded erasure, which errs towards asking.
    """
    if acknowledged:
        return None
    count = erasures.erasures_since(taken_at)
    if not count:
        return None
    return {
        "detail": (
            f"{count} account(s) were erased after the snapshot was taken. Restoring the database brings them back "
            "until they are erased again once the rollback has settled. Repeat the request with "
            "acknowledge_erasures to go ahead."
        ),
        "reason": "erasures_since_snapshot",
        "erasures": count,
    }


# ── Serialisation ────────────────────────────────────────────────────────────


def _iso(value) -> str | None:
    return value.isoformat() if value is not None else None


def _serialize_job(job, *, for_superuser: bool) -> dict:
    return {
        "job_id": str(job.job_id),
        "operation": job.operation,
        "executor": job.executor,
        "state": job.state,
        "reason": job.reason,
        "step": job.step,
        "in_flight": job.in_flight,
        "requested_by": job.requested_by.username if job.requested_by is not None else None,
        "package_sha256": job.package.sha256 if job.package is not None else None,
        "args": job.args or {},
        "created_at": job.created_at.isoformat(),
        "started_at": _iso(job.started_at),
        "finished_at": _iso(job.finished_at),
        "verify_deadline": _iso(job.verify_deadline),
        "verify_requested_at": _iso(job.verify_requested_at),
        "rollback_requested_at": _iso(job.rollback_requested_at),
        "installed_version_before": job.installed_version_before,
        "target_version": job.target_version,
        "running_version": job.running_version,
        "snapshot": job.snapshot,
        "post_snapshot": job.post_snapshot,
        "migrations_applied": job.migrations_applied,
        "output": job.output if for_superuser else None,
    }


def _get_job(job_id: str):
    try:
        parsed = uuid.UUID(job_id)
    except ValueError:
        raise HttpError(404, "No such job") from None
    job = MaintenanceJob.objects.select_related("requested_by", "package").filter(job_id=parsed).first()
    if job is None:
        raise HttpError(404, "No such job")
    return job


def _serialize_package(package) -> dict:
    return {
        "sha256": package.sha256,
        "version": package.version,
        "project": package.project,
        "plugins": list(package.plugins or []),
        "platform_compatible": package.platform_compatible,
        "built_at": _iso(package.built_at),
        "size": package.size,
        "key_id": package.key_id,
        "agent_version": _manifest_agent_version(package.manifest),
        "uploaded_by": package.uploaded_by.username if package.uploaded_by is not None else None,
        "uploaded_at": package.uploaded_at.isoformat(),
        "state": package.state,
        "applicable": _package_applicable(package),
    }


def _manifest_agent_version(manifest) -> int:
    value = manifest.get("agent_version") if isinstance(manifest, dict) else None
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


#: The package states a request may name. An applied package stays nameable
#: because a rollback makes it newer than what runs again; while it is the
#: release that runs, the version check refuses it.
_NAMEABLE_STATES = (MaintenancePackage.State.AVAILABLE, MaintenancePackage.State.APPLIED)


def _package_applicable(package) -> bool:
    """Whether a request may name this package now: available or applied, and newer than what runs."""
    if package.state not in _NAMEABLE_STATES:
        return False
    try:
        return packaging.parse_version(package.version) > VERSION_INFO
    except packaging.InvalidVersion:
        return False


def _get_package(sha256: str):
    if len(sha256) != 64 or any(c not in "0123456789abcdef" for c in sha256):
        raise HttpError(404, "No such package")
    package = MaintenancePackage.objects.select_related("uploaded_by").filter(sha256=sha256).first()
    if package is None:
        raise HttpError(404, "No such package")
    return package


def _step_up_status(user) -> dict:
    method = step_up_method(user)
    if method is None:
        return {
            "method": None,
            "available": False,
            "reason": "This account signs in through an external provider and has no second factor enrolled.",
        }
    return {"method": method, "available": True, "reason": None}


# ── Endpoints ────────────────────────────────────────────────────────────────


@api.get("/lock", response=LockProbeOut)
def get_lock(request):
    """The maintenance flag, for the SPA to detect a phase change and the release.

    Public and not gated by ``REMOTE_MAINTENANCE_ENABLED``: ``update.sh``'s
    manual path raises the flag without the feature. Exempt from the lock
    itself and from the Activity trail (``ACTIVITY_PATH_SKIP_LIST``), since a
    browser polls it. A flag that cannot be read reports ``updating``, which is
    how the middleware treats it.
    """
    flag = current_lock()
    if flag is None:
        return {"locked": False, "phase": None, "since": None, "expected_until": None, "message": None}
    return {
        "locked": True,
        "phase": flag.phase,
        "since": flag.since,
        "expected_until": flag.expected_until,
        "message": flag.message,
    }


@api.get("/status", response=StatusOut)
def get_status(request):
    """The deployment's maintenance state, for the tab header."""
    _gate_enabled(request)
    user = _require_staff(request)
    _sync_for_read()
    lock = current_lock()
    in_flight = MaintenanceJob.objects.filter(in_flight=True).only("job_id").first()
    keys = packaging.load_release_keys()
    log_activity(verb="maintenance.status.read")
    return {
        "remote_maintenance_enabled": True,
        "remote_update_enabled": _host_tier_enabled(),
        "installed_version": __version__,
        "server_now": timezone.now().isoformat(),
        "spool_writable": spool.is_writable(),
        "release_key_present": bool(keys),
        "release_key_ids": [packaging.key_id(key) for key in keys],
        "agent": spool.agent_summary(),
        "lock": {
            "phase": lock.phase,
            "since": lock.since,
            "expected_until": lock.expected_until,
            "message": lock.message,
            "job_id": lock.job_id,
        }
        if lock is not None
        else None,
        "in_flight_job": str(in_flight.job_id) if in_flight is not None else None,
        "step_up": _step_up_status(user),
    }


@api.get("/operations", response=list[OperationOut])
def list_operations(request):
    """The registry, with each operation's argument schema and whether its tier is switched on."""
    _gate_enabled(request)
    _require_staff(request)
    host_enabled = _host_tier_enabled()
    agent = spool.agent_summary()
    log_activity(verb="maintenance.operation.list")

    def available(operation) -> bool:
        # A host operation needs the tier on and, once an agent reports, an
        # agent that carries it out; before any heartbeat the tier alone
        # decides, since the banner already says the agent is not installed.
        if operation.executor != HOST:
            return True
        if not host_enabled:
            return False
        return not agent["installed"] or operation.key in agent["capabilities"]

    return [
        {
            "key": operation.key,
            "executor": operation.executor,
            "label": operation.label,
            "description": operation.description,
            "requires_step_up": operation.requires_step_up,
            "available": available(operation),
            "args_schema": operation.args_schema.model_json_schema(),
        }
        for operation in registered_operations()
    ]


@api.get("/jobs", response=list[JobOut])
def list_jobs(request):
    """The most recent jobs, newest first."""
    _gate_enabled(request)
    user = _require_staff(request)
    synced = _sync_for_read()
    jobs = list(MaintenanceJob.objects.select_related("requested_by", "package")[:JOB_LIST_LIMIT])
    if not synced:
        spool.overlay(jobs)
    log_activity(verb="maintenance.job.list")
    return [_serialize_job(job, for_superuser=user.is_superuser) for job in jobs]


@api.get("/jobs/{job_id}", response=JobOut)
def get_job(request, job_id: str):
    """One job by its id."""
    _gate_enabled(request)
    user = _require_staff(request)
    synced = _sync_for_read()
    job = _get_job(job_id)
    if not synced:
        spool.overlay([job])
    log_activity(verb="maintenance.job.read", target=job)
    return _serialize_job(job, for_superuser=user.is_superuser)


@api.get("/jobs/{job_id}/log", response=LogOut)
def get_job_log(request, job_id: str):
    """The tail of the host agent's log for the job, or the captured command output for a celery-tier job."""
    _gate_enabled(request)
    _require_superuser(request)
    job = _get_job(job_id)
    if job.executor == MaintenanceJob.Executor.HOST:
        text, truncated, total = spool.read_log_tail(job.job_id, spool.LOG_TAIL_BYTES)
    else:
        text, truncated, total = job.output, job.output.startswith("…[truncated]…"), len(job.output.encode("utf-8"))
    log_activity(verb="maintenance.job.log", target=job, metadata={"bytes": total})
    return {"job_id": str(job.job_id), "log": text, "truncated": truncated, "bytes": total}


def _log_state(job) -> None:
    log_security_event(
        "maintenance.job_state",
        job_id=str(job.job_id),
        operation=job.operation,
        state=job.state,
        reason=job.reason or None,
    )


def _engage(job, operation) -> None:
    """Hand a committed job to its executor; a hand-off that fails marks the job failed and answers 503.

    Runs after the commit rather than in ``on_commit``, so a failure reaches the
    caller instead of a 500 with the row committed in flight: the request file
    for the host tier (``spool_write_failed``), the Celery dispatch for the
    celery tier (``dispatch_failed``).
    """
    if operation.executor == HOST:
        try:
            spool.write_request(job)
        except OSError as exc:
            logger.warning("Request file of job %s could not be written: %s", job.job_id, exc.strerror or exc)
            reason, detail = "spool_write_failed", "The request could not be written into the maintenance spool."
        else:
            return
    else:
        from maintenance.tasks import run_job

        try:
            run_job.apply_async(args=[job.pk], soft_time_limit=operation.soft_time_limit)
        except Exception:
            logger.exception("Job %s could not be dispatched to a worker", job.job_id)
            reason, detail = (
                "dispatch_failed",
                "The job could not be handed to a worker; the task queue is unreachable.",
            )
        else:
            return
    if job.mark_finished(state=MaintenanceJob.State.FAILED, reason=reason, expect=["requested"]):
        _log_state(job)
    raise HttpError(503, f"{detail} The job was marked failed.")


@api.post("/jobs", response={202: JobOut, 409: ConflictOut})
def create_job(request, payload: JobCreateIn):
    """Request an operation.

    The registry decides what the key means; the request supplies only its
    typed arguments and, when the operation asks for it, a step-up
    confirmation. One job may be in flight at a time, across both tiers, so a
    request while another runs answers 409, and so does any request while the
    maintenance flag is up. A ``platform.rollback`` that restores the database
    answers 409 with ``reason: erasures_since_snapshot`` while accounts were
    erased after its snapshot, until the body carries ``acknowledge_erasures``.
    Step-up comes last, after every check that could refuse, so a refused
    request spends no one-time code.

    The row is committed first and the executor engaged afterwards — a Celery
    dispatch for the celery tier, a request file in the spool for the host
    tier — so an executor never sees a job the database does not have, and a
    hand-off that fails marks the row failed rather than leaving it in flight.
    """
    _gate_enabled(request)
    user = _require_superuser(request)
    _refuse_while_locked()
    spool.sync()
    operation = get_operation(payload.operation)
    if operation is None:
        raise HttpError(400, "Unknown operation.")
    _require_host_tier(operation)
    try:
        args = operation.args_schema(**(payload.args or {}))
    except ValidationError as exc:
        errors = "; ".join(f"{'.'.join(str(p) for p in e['loc']) or 'args'}: {e['msg']}" for e in exc.errors())
        raise HttpError(400, f"Invalid arguments: {errors}") from None

    package = None
    target_version = ""
    request_args = args.model_dump(exclude_none=True)
    conflict = None
    if operation.executor == HOST:
        sha256 = getattr(args, "package_sha256", None)
        if sha256:
            package = MaintenancePackage.objects.filter(sha256=sha256, state__in=_NAMEABLE_STATES).first()
            if package is None:
                raise HttpError(400, "No uploaded package has that hash, or it has not been verified.")
            if not _package_applicable(package):
                raise HttpError(400, f"Package {package.version} is not newer than the installed {__version__}.")
            target_version = package.version
        snapshot = getattr(args, "snapshot", None)
        if snapshot:
            # The agent is the authority on what the host holds; its heartbeat
            # is checked here so a mistyped name answers now, not a tick later.
            agent = spool.agent_summary()
            known = {row["name"]: row for row in agent["snapshots"]}
            if agent["installed"] and snapshot not in known:
                raise HttpError(400, "The host agent reports no snapshot by that name.")
            target_version = (known.get(snapshot) or {}).get("version") or ""
            if operation.key == RESTORE_OPERATION and request_args.get("restore_database", True):
                conflict = _erasure_conflict(_snapshot_taken_at(snapshot), acknowledged=payload.acknowledge_erasures)
        # The agent reads the window from the request, and a request that
        # leaves it out means the deployment's default, not the agent's.
        if "verify_window_minutes" in type(args).model_fields and request_args.get("verify_window_minutes") is None:
            request_args["verify_window_minutes"] = int(getattr(settings, "REMOTE_UPDATE_VERIFY_WINDOW_MINUTES", 30))
    if MaintenanceJob.objects.filter(in_flight=True).exists():
        raise HttpError(409, "Another maintenance job is in flight.")
    if operation.executor == HOST and not spool.is_writable():
        raise HttpError(409, "The maintenance spool is not writable by the platform; an operator needs to fix it.")
    if conflict is not None:
        return 409, conflict
    if operation.requires_step_up:
        confirm_step_up(request, user, password=payload.password, totp_code=payload.totp_code)

    job = MaintenanceJob(
        operation=operation.key,
        executor=operation.executor,
        requested_by=user,
        package=package,
        args=request_args,
        target_version=target_version,
    )
    try:
        with transaction.atomic():
            if package is not None:
                # Held until the commit, so a removal of the same package
                # waits and then sees this job in flight, or wins first and
                # this request sees the package gone.
                locked = MaintenancePackage.objects.select_for_update().filter(pk=package.pk).first()
                if locked is None or locked.state not in _NAMEABLE_STATES:
                    raise HttpError(409, "The package was removed while the request was being made.")
            job.save()
            # The row carries the operation, executor and target version; only
            # the cross-model reference is worth repeating.
            log_activity(
                verb="maintenance.job.create",
                target=job,
                metadata={"package_sha256": package.sha256} if package is not None else None,
            )
    except IntegrityError:
        raise HttpError(409, "Another maintenance job is in flight.") from None
    log_security_event(
        "maintenance.job_requested",
        ip=get_client_ip(request),
        actor_id=user.pk,
        job_id=str(job.job_id),
        operation=job.operation,
        executor=job.executor,
    )
    _engage(job, operation)
    job.refresh_from_db()
    return 202, _serialize_job(job, for_superuser=True)


@api.post("/jobs/{job_id}/cancel", response=JobOut)
def cancel_job(request, job_id: str):
    """Withdraw a request the executor has not yet picked up.

    A celery-tier job past ``requested`` is already running and cannot be
    stopped from here. A host-tier request is withdrawn by unlinking its file,
    which the agent claims by renaming: the unlink succeeding is what makes the
    cancel win, and the row turns ``cancelled`` only then. A request the agent
    has claimed answers 409.
    """
    _gate_enabled(request)
    user = _require_superuser(request)
    spool.sync()
    job = _get_job(job_id)
    if job.state != MaintenanceJob.State.REQUESTED:
        raise HttpError(409, f"A job in state {job.state!r} cannot be cancelled.")
    if job.executor == MaintenanceJob.Executor.HOST and not spool.remove_request(job):
        raise HttpError(409, "The host agent has already picked this request up.")
    if not job.mark_finished(state=MaintenanceJob.State.CANCELLED, actor=user, expect=["requested"]):
        raise HttpError(409, "The job changed state while cancelling.")
    log_activity(verb="maintenance.job.cancel", target=job)
    return _serialize_job(job, for_superuser=True)


def _withdraw_stamp(job, user, field: str) -> None:
    """Undo a verify or rollback stamp whose marker could not be written, so the request can be repeated."""
    job.transition(expect=[job.state], actor=user, **{field: None})


@api.post("/jobs/{job_id}/verify", response=JobOut)
def verify_job(request, job_id: str, payload: ConfirmIn):
    """Confirm an update: the agent marks it succeeded and lifts the lock.

    Once per job, before the window closes, and not after a rollback was asked
    for: the stamp is set only while both it and the rollback stamp are null.
    """
    _gate_enabled(request)
    user = _require_superuser(request)
    spool.sync()
    job = _get_job(job_id)
    if job.operation != UPDATE_OPERATION:
        raise HttpError(409, "Only an update has a verification window.")
    if job.state != MaintenanceJob.State.AWAITING_VERIFICATION:
        raise HttpError(409, f"A job in state {job.state!r} is not awaiting verification.")
    if job.verify_deadline is not None and timezone.now() >= job.verify_deadline:
        raise HttpError(409, "The verification window has closed; the update is being rolled back.")
    if job.verify_requested_at is not None:
        raise HttpError(409, "This update has already been confirmed.")
    if job.rollback_requested_at is not None:
        raise HttpError(409, "A rollback of this update has already been asked for.")
    confirm_step_up(request, user, password=payload.password, totp_code=payload.totp_code, second_factor=False)
    now = timezone.now()
    if not job.transition(
        expect=["awaiting_verification"],
        actor=user,
        unset=("verify_requested_at", "rollback_requested_at"),
        verify_requested_at=now,
    ):
        raise HttpError(409, "The job changed while confirming.")
    try:
        spool.write_marker(job, spool.MARKER_VERIFY, by_user_id=user.pk)
    except OSError:
        _withdraw_stamp(job, user, "verify_requested_at")
        raise HttpError(503, "The confirmation could not be written into the maintenance spool.") from None
    log_activity(verb="maintenance.job.verify", target=job)
    return _serialize_job(job, for_superuser=True)


def _rollback_restores_database(job) -> bool:
    """Whether rolling ``job`` back restores the database: unless it applied no migration, which lets the agent keep it."""
    return job.migrations_applied is not False


@api.post("/jobs/{job_id}/rollback", response={200: JobOut, 409: ConflictOut})
def rollback_job(request, job_id: str, payload: ConfirmIn):
    """Ask the agent to roll an update back.

    Allowed while the job awaits verification, and after it succeeded for as
    long as its pre-update snapshot exists, nothing is in flight and no later
    update or restore has succeeded: rolling back an older update would put its
    snapshot over everything since. The agent makes the same checks. Once per
    job, and not after the update was confirmed while it awaited verification.

    Everything written since the update's snapshot is lost from the database,
    which the UI says before the click and the notification repeats; accounts
    erased since then come back until the re-erasure that follows the rollback,
    so while there are any the request answers 409 with ``reason:
    erasures_since_snapshot`` until the body carries ``acknowledge_erasures``.
    """
    _gate_enabled(request)
    user = _require_superuser(request)
    spool.sync()
    job = _get_job(job_id)
    if job.operation != UPDATE_OPERATION:
        raise HttpError(
            409, "Only an update can be rolled back here; a snapshot is restored with the roll-back operation."
        )
    allowed = (MaintenanceJob.State.AWAITING_VERIFICATION, MaintenanceJob.State.SUCCEEDED)
    if job.state not in allowed:
        raise HttpError(409, f"A job in state {job.state!r} cannot be rolled back.")
    if job.rollback_requested_at is not None:
        raise HttpError(409, "A rollback of this update has already been asked for.")
    unset = ["rollback_requested_at"]
    if job.state == MaintenanceJob.State.AWAITING_VERIFICATION:
        if job.verify_requested_at is not None:
            raise HttpError(409, "This update has already been confirmed.")
        unset.append("verify_requested_at")
    else:
        if not job.snapshot:
            raise HttpError(409, "This job left no snapshot to roll back to.")
        if MaintenanceJob.objects.filter(in_flight=True).exists():
            raise HttpError(409, "Another maintenance job is in flight.")
        later = MaintenanceJob.objects.filter(
            operation__in=[UPDATE_OPERATION, RESTORE_OPERATION],
            state=MaintenanceJob.State.SUCCEEDED,
            created_at__gt=job.created_at,
        )
        if later.exists():
            raise HttpError(409, "A later update or restore has succeeded; only the newest update can be rolled back.")
    restores_database = _rollback_restores_database(job)
    erasures_undone = 0
    if restores_database:
        taken_at = _snapshot_taken_at(job.snapshot) or job.started_at or job.created_at
        conflict = _erasure_conflict(taken_at, acknowledged=payload.acknowledge_erasures)
        if conflict is not None:
            return 409, conflict
        erasures_undone = erasures.erasures_since(taken_at)
    confirm_step_up(request, user, password=payload.password, totp_code=payload.totp_code)
    now = timezone.now()
    if not job.transition(expect=[job.state], actor=user, unset=unset, rollback_requested_at=now):
        raise HttpError(409, "The job changed while requesting the rollback.")
    try:
        spool.write_marker(job, spool.MARKER_ROLLBACK, by_user_id=user.pk)
    except OSError:
        _withdraw_stamp(job, user, "rollback_requested_at")
        raise HttpError(503, "The rollback request could not be written into the maintenance spool.") from None
    # Whether the database goes back, and how many erasures the superuser
    # acknowledged it would undo until the re-erasure: neither is recoverable
    # from the row once the rollback has settled.
    log_activity(
        verb="maintenance.job.rollback",
        target=job,
        metadata={"restores_database": restores_database, "erasures_acknowledged": erasures_undone},
    )
    log_security_event(
        "maintenance.rollback_requested",
        ip=get_client_ip(request),
        actor_id=user.pk,
        job_id=str(job.job_id),
        reason=job.state,
    )
    return 200, _serialize_job(job, for_superuser=True)


@api.post("/jobs/{job_id}/abandon", response=JobOut)
def abandon_job(request, job_id: str, payload: ConfirmIn):
    """Fail a job in flight whose executor is gone, as ``abandoned``, to free the one-in-flight slot.

    A celery-tier job may be abandoned at any time: the worker that ran it may
    have died without a trace, and the reaper otherwise waits out the time
    limit. A host-tier job only while the agent's heartbeat is stale or absent —
    a running agent reports its own outcome, and abandoning under it would let
    a second job start beside one that is still changing the host. An
    unclaimed request file is withdrawn; a status the agent publishes later
    may still record the outcome, but never puts the job back in flight.
    """
    _gate_enabled(request)
    user = _require_superuser(request)
    spool.sync()
    job = _get_job(job_id)
    if not job.in_flight:
        raise HttpError(409, f"A job in state {job.state!r} is not in flight.")
    if job.executor == MaintenanceJob.Executor.HOST:
        agent = spool.agent_summary()
        if agent["installed"] and not agent["stale"]:
            raise HttpError(409, "The host agent is running and will report this job's outcome itself.")
    confirm_step_up(request, user, password=payload.password, totp_code=payload.totp_code)
    before = job.state
    from maintenance.notify import dispatch_notice, needs_notice

    announce = needs_notice(job, MaintenanceJob.State.FAILED)
    if not job.mark_finished(
        state=MaintenanceJob.State.FAILED,
        reason="abandoned",
        actor=user,
        expect=[before],
        last_notified_state=MaintenanceJob.State.FAILED if announce else job.last_notified_state,
    ):
        raise HttpError(409, "The job changed state while abandoning it.")
    if job.executor == MaintenanceJob.Executor.HOST:
        spool.remove_request(job)
    if announce:
        dispatch_notice(job, MaintenanceJob.State.FAILED)
    log_activity(verb="maintenance.job.abandon", target=job)
    log_security_event(
        "maintenance.job_abandoned",
        ip=get_client_ip(request),
        actor_id=user.pk,
        job_id=str(job.job_id),
        operation=job.operation,
        executor=job.executor,
        state_before=before,
    )
    return _serialize_job(job, for_superuser=True)


# ── Packages ─────────────────────────────────────────────────────────────────


@api.get("/packages", response=list[PackageOut])
def list_packages(request):
    """The uploaded packages, newest first, pruned ones included so the history reads whole."""
    _gate_enabled(request)
    _require_staff(request)
    _sync_for_read()
    packages = MaintenancePackage.objects.select_related("uploaded_by")[:JOB_LIST_LIMIT]
    log_activity(verb="maintenance.package.list")
    return [_serialize_package(package) for package in packages]


def _reject_package(request, user, exc: packaging.PackageRejected, *, declared_version: str = ""):
    log_security_event(
        "maintenance.package_rejected",
        ip=get_client_ip(request),
        actor_id=user.pk,
        reason=exc.reason,
        version=declared_version or None,
    )
    return exc.status, {"detail": exc.message, "reason": exc.reason}


@api.post("/packages", response={201: PackageOut, 400: RejectionOut, 409: RejectionOut, 413: RejectionOut})
def upload_package(request):
    """Upload a release: the tarball, its manifest and the signature over the manifest, as three parts.

    The parts are read from ``request.FILES`` only after the gate, the role,
    the host-tier flag and the lock have been checked: a ``File`` parameter
    would have the multipart body parsed, and spooled to disk, before any of
    them ran. The signature is checked against
    ``REMOTE_UPDATE_RELEASE_KEY_PATH``, or the successor key a release announced
    beside it, and the manifest against what this deployment is — newer than
    the installed version, the same project and plugins, within every
    installed pin — before the tarball is copied into the spool and hashed
    against the manifest. A refusal names its reason, leaves nothing in the
    packages directory and is written to the security log. The host agent
    repeats the checks on its own copy before anything runs; this is the
    immediate answer, not the boundary.
    """
    _gate_enabled(request)
    user = _require_superuser(request)
    if not _host_tier_enabled():
        raise HttpError(403, "Remote updates are disabled on this deployment (REMOTE_UPDATE_ENABLED).")
    _refuse_while_locked()
    parts = {name: request.FILES.get(name) for name in ("package", "manifest", "signature")}
    missing = sorted(name for name, part in parts.items() if part is None)
    if missing:
        raise HttpError(422, f"Missing multipart part(s): {', '.join(missing)}.")
    package, manifest, signature = parts["package"], parts["manifest"], parts["signature"]
    spool.sync()
    declared = ""
    try:
        if manifest.size > packaging.MANIFEST_LIMIT or signature.size > packaging.SIGNATURE_LIMIT:
            raise packaging.PackageRejected(
                "manifest", "The manifest or the signature is far larger than either can be."
            )
        manifest_bytes = manifest.read()
        signature_bytes = signature.read()
        keys = packaging.load_release_keys()
        if not keys:
            raise packaging.PackageRejected(
                "key_missing",
                "This deployment has no release key to verify packages against (REMOTE_UPDATE_RELEASE_KEY_PATH).",
                status=409,
            )
        key = packaging.verify_signature(manifest_bytes, signature_bytes, keys)
        parsed = packaging.read_manifest(manifest_bytes)
        declared = parsed.version
        packaging.check_manifest(parsed)
        with transaction.atomic():
            row = packaging.store(
                package, manifest_bytes, signature_bytes, parsed, uploaded_by=user, key_id=packaging.key_id(key)
            )
            # The target row carries the hash and the version; nothing to repeat.
            log_activity(verb="maintenance.package.create", target=row)
            pruned = packaging.prune()
    except packaging.PackageRejected as exc:
        return _reject_package(request, user, exc, declared_version=declared)
    log_security_event(
        "maintenance.package_uploaded",
        ip=get_client_ip(request),
        actor_id=user.pk,
        sha256=row.sha256,
        version=row.version,
        pruned=len(pruned),
    )
    return 201, _serialize_package(row)


@api.delete("/packages/{sha256}", response=PackageOut)
def delete_package(request, sha256: str):
    """Remove an uploaded package's files. The row stays as ``pruned``, since jobs refer to it.

    The row is locked for the check and the removal, the same lock a job
    request naming the package takes, so a removal cannot slip between that
    request's check and its commit.
    """
    _gate_enabled(request)
    user = _require_superuser(request)
    _refuse_while_locked()
    spool.sync()
    package = _get_package(sha256)
    with transaction.atomic():
        package = MaintenancePackage.objects.select_for_update().get(pk=package.pk)
        if MaintenanceJob.objects.filter(in_flight=True, package=package).exists():
            raise HttpError(409, "A job in flight refers to this package.")
        if package.state == MaintenancePackage.State.PRUNED:
            raise HttpError(409, "This package has already been removed.")
        try:
            packaging.remove_files(package.sha256)
        except OSError as exc:
            raise HttpError(409, f"The package files could not be removed: {exc.strerror or exc}") from None
        package.state = MaintenancePackage.State.PRUNED
        package.save(update_fields=["state"])
        log_activity(verb="maintenance.package.delete", target=package)
    log_security_event(
        "maintenance.package_removed",
        ip=get_client_ip(request),
        actor_id=user.pk,
        sha256=package.sha256,
        version=package.version,
    )
    return _serialize_package(package)
