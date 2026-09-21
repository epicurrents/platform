"""Maintenance API v1 — status, the operation registry, and jobs.

Mounted at ``/api/v1/maintenance/``. Every operation calls ``_gate_enabled``
first, which answers 404 while ``REMOTE_MAINTENANCE_ENABLED`` is off so the
surface does not exist on a deployment that has not opted in, and then one of
the tier guards. Host-tier writes additionally answer 403 while
``REMOTE_UPDATE_ENABLED`` is off.

Nothing here executes anything. A request becomes a ``MaintenanceJob`` row and,
on commit, either a Celery dispatch or a request file in the spool; the
executor is elsewhere, and the source scan in ``tests/test_no_host_execution.py``
keeps it that way.

Endpoints
---------
GET  /status                 flags, versions, spool and agent state, step-up method   (staff)
GET  /operations             the registry with a JSON schema per operation           (staff)
GET  /jobs                   recent jobs                                              (staff)
POST /jobs                   request an operation                                     (superuser, step-up)
GET  /jobs/{job_id}          one job                                                  (staff)
GET  /jobs/{job_id}/log      the host agent's log tail, or the command output          (superuser)
POST /jobs/{job_id}/cancel   withdraw a request the executor has not picked up        (superuser)
POST /jobs/{job_id}/verify   confirm an update                                        (superuser, password)
POST /jobs/{job_id}/rollback ask for a rollback                                       (superuser, step-up)
GET  /packages               uploaded update packages                                 (staff)
POST /packages               upload a package: tarball, manifest, signature           (superuser)
DELETE /packages/{sha256}    remove an uploaded package                               (superuser)
"""

import uuid

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone
from ninja import File, NinjaAPI, Schema, UploadedFile
from ninja.errors import HttpError
from pydantic import ValidationError

from activity.audit import log_activity
from epicurrents.auth import enforce_session_csrf
from epicurrents.security_log import get_client_ip, log_security_event
from epicurrents.version import VERSION_INFO, __version__
from maintenance import packaging, spool
from maintenance.lock import current_lock
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

JOB_LIST_LIMIT = 50
# The one operation with a verification window, and so the one the verify and
# rollback endpoints act on.
UPDATE_OPERATION = "platform.update"


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


class ConfirmIn(Schema):
    """Credentials for a step-up confirmation."""

    password: str | None = None
    totp_code: str | None = None


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


def _require_auth(request):
    """Return the authenticated user or raise 401.

    Routes the request through the session-CSRF chokepoint; see AGENTS.md →
    *Session-authenticated write CSRF*.
    """
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated:
        raise HttpError(401, "Not authenticated")
    enforce_session_csrf(request)
    return user


def _require_staff(request):
    """Return an authenticated staff (or superuser) user or raise 403."""
    user = _require_auth(request)
    if not (user.is_staff or user.is_superuser):
        raise HttpError(403, "Staff access required.")
    return user


def _require_superuser(request):
    """Return an authenticated superuser or raise 403."""
    user = _require_auth(request)
    if not user.is_superuser:
        raise HttpError(403, "Superuser access required.")
    return user


def _host_tier_enabled() -> bool:
    return bool(getattr(settings, "REMOTE_UPDATE_ENABLED", False))


def _require_host_tier(operation) -> None:
    """403 for a host-tier operation while the host tier is switched off."""
    if operation.executor == HOST and not _host_tier_enabled():
        raise HttpError(403, "Remote updates are disabled on this deployment (REMOTE_UPDATE_ENABLED).")


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


def _package_applicable(package) -> bool:
    """Whether a request may name this package now: available, and newer than what runs."""
    if package.state != MaintenancePackage.State.AVAILABLE:
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


@api.get("/status", response=StatusOut)
def get_status(request):
    """The deployment's maintenance state, for the tab header."""
    _gate_enabled(request)
    user = _require_staff(request)
    spool.sync()
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
    spool.sync()
    jobs = MaintenanceJob.objects.select_related("requested_by", "package")[:JOB_LIST_LIMIT]
    log_activity(verb="maintenance.job.list")
    return [_serialize_job(job, for_superuser=user.is_superuser) for job in jobs]


@api.get("/jobs/{job_id}", response=JobOut)
def get_job(request, job_id: str):
    """One job by its id."""
    _gate_enabled(request)
    user = _require_staff(request)
    spool.sync()
    job = _get_job(job_id)
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


@api.post("/jobs", response={202: JobOut})
def create_job(request, payload: JobCreateIn):
    """Request an operation.

    The registry decides what the key means; the request supplies only its
    typed arguments and, when the operation asks for it, a step-up
    confirmation. One job may be in flight at a time, across both tiers, so a
    request while another runs answers 409. The row is created first and the
    executor engaged on commit — a Celery dispatch for the celery tier, a
    request file in the spool for the host tier — so an executor never sees a
    job the database does not have.
    """
    _gate_enabled(request)
    user = _require_superuser(request)
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
    if operation.requires_step_up:
        confirm_step_up(request, user, password=payload.password, totp_code=payload.totp_code)

    package = None
    target_version = ""
    request_args = args.model_dump(exclude_none=True)
    if operation.executor == HOST:
        sha256 = getattr(args, "package_sha256", None)
        if sha256:
            package = MaintenancePackage.objects.filter(sha256=sha256, state=MaintenancePackage.State.AVAILABLE).first()
            if package is None:
                raise HttpError(400, "No uploaded package has that hash.")
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
        # The agent reads the window from the request, and a request that
        # leaves it out means the deployment's default, not the agent's.
        if "verify_window_minutes" in type(args).model_fields and request_args.get("verify_window_minutes") is None:
            request_args["verify_window_minutes"] = int(getattr(settings, "REMOTE_UPDATE_VERIFY_WINDOW_MINUTES", 30))
    if MaintenanceJob.objects.filter(in_flight=True).exists():
        raise HttpError(409, "Another maintenance job is in flight.")

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
            job.save()
            # The row carries the operation, executor and target version; only
            # the cross-model reference is worth repeating.
            log_activity(
                verb="maintenance.job.create",
                target=job,
                metadata={"package_sha256": package.sha256} if package is not None else None,
            )
            if operation.executor == HOST:
                transaction.on_commit(lambda: spool.write_request(job))
            else:
                from maintenance.tasks import run_job

                transaction.on_commit(
                    lambda: run_job.apply_async(args=[job.pk], soft_time_limit=operation.soft_time_limit)
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
    return 202, _serialize_job(job, for_superuser=True)


@api.post("/jobs/{job_id}/cancel", response=JobOut)
def cancel_job(request, job_id: str):
    """Withdraw a request the executor has not yet picked up.

    A celery-tier job past ``requested`` is already running and cannot be
    stopped from here; a host-tier job past it belongs to the agent.
    """
    _gate_enabled(request)
    user = _require_superuser(request)
    spool.sync()
    job = _get_job(job_id)
    if job.state != MaintenanceJob.State.REQUESTED:
        raise HttpError(409, f"A job in state {job.state!r} cannot be cancelled.")
    if job.executor == MaintenanceJob.Executor.HOST and spool.read_status(job.job_id) is not None:
        raise HttpError(409, "The agent has already picked this job up.")
    if not job.mark_finished(state=MaintenanceJob.State.CANCELLED, actor=user, expect=["requested"]):
        raise HttpError(409, "The job changed state while cancelling.")
    if job.executor == MaintenanceJob.Executor.HOST:
        spool.remove_request(job)
    log_activity(verb="maintenance.job.cancel", target=job)
    return _serialize_job(job, for_superuser=True)


@api.post("/jobs/{job_id}/verify", response=JobOut)
def verify_job(request, job_id: str, payload: ConfirmIn):
    """Confirm an update: the agent marks it succeeded and lifts the lock."""
    _gate_enabled(request)
    user = _require_superuser(request)
    spool.sync()
    job = _get_job(job_id)
    if job.operation != UPDATE_OPERATION:
        raise HttpError(409, "Only an update has a verification window.")
    if job.state != MaintenanceJob.State.AWAITING_VERIFICATION:
        raise HttpError(409, f"A job in state {job.state!r} is not awaiting verification.")
    confirm_step_up(request, user, password=payload.password, totp_code=payload.totp_code, second_factor=False)
    now = timezone.now()
    if not job.transition(expect=["awaiting_verification"], actor=user, verify_requested_at=now):
        raise HttpError(409, "The job changed state while confirming.")
    spool.write_marker(job, spool.MARKER_VERIFY, by_user_id=user.pk)
    log_activity(verb="maintenance.job.verify", target=job)
    return _serialize_job(job, for_superuser=True)


@api.post("/jobs/{job_id}/rollback", response=JobOut)
def rollback_job(request, job_id: str, payload: ConfirmIn):
    """Ask the agent to roll an update back.

    Allowed while the job awaits verification, and after it succeeded for as
    long as its pre-update snapshot exists; the agent makes the second check.
    Everything written since the update's snapshot is lost from the database,
    which the UI says before the click and the notification repeats.
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
    if job.state == MaintenanceJob.State.SUCCEEDED and not job.snapshot:
        raise HttpError(409, "This job left no snapshot to roll back to.")
    confirm_step_up(request, user, password=payload.password, totp_code=payload.totp_code)
    now = timezone.now()
    if not job.transition(expect=list(allowed), actor=user, rollback_requested_at=now):
        raise HttpError(409, "The job changed state while requesting the rollback.")
    spool.write_marker(job, spool.MARKER_ROLLBACK, by_user_id=user.pk)
    log_activity(verb="maintenance.job.rollback", target=job)
    log_security_event(
        "maintenance.rollback_requested",
        ip=get_client_ip(request),
        actor_id=user.pk,
        job_id=str(job.job_id),
        reason=job.state,
    )
    return _serialize_job(job, for_superuser=True)


# ── Packages ─────────────────────────────────────────────────────────────────


@api.get("/packages", response=list[PackageOut])
def list_packages(request):
    """The uploaded packages, newest first, pruned ones included so the history reads whole."""
    _gate_enabled(request)
    _require_staff(request)
    spool.sync()
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
def upload_package(
    request,
    package: UploadedFile = File(...),
    manifest: UploadedFile = File(...),
    signature: UploadedFile = File(...),
):
    """Upload a release: the tarball, its manifest and the signature over the manifest, as three parts.

    The signature is checked against ``REMOTE_UPDATE_RELEASE_KEY_PATH``, or
    the successor key a release announced beside it, and the manifest against
    what this deployment is — newer than the installed
    version, the same project and plugins, within every installed pin — before
    the tarball is copied into the spool and hashed against the manifest. A
    refusal names its reason, leaves nothing in the packages directory and is
    written to the security log. The host agent repeats the checks on its own
    copy before anything runs; this is the immediate answer, not the boundary.
    """
    _gate_enabled(request)
    user = _require_superuser(request)
    if not _host_tier_enabled():
        raise HttpError(403, "Remote updates are disabled on this deployment (REMOTE_UPDATE_ENABLED).")
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
        packaging.verify_signature(manifest_bytes, signature_bytes, keys)
        parsed = packaging.read_manifest(manifest_bytes)
        declared = parsed.version
        packaging.check_manifest(parsed)
        with transaction.atomic():
            row = packaging.store(package, manifest_bytes, signature_bytes, parsed, uploaded_by=user)
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
    """Remove an uploaded package's files. The row stays as ``pruned``, since jobs refer to it."""
    _gate_enabled(request)
    user = _require_superuser(request)
    spool.sync()
    package = _get_package(sha256)
    if MaintenanceJob.objects.filter(in_flight=True, package=package).exists():
        raise HttpError(409, "A job in flight refers to this package.")
    if package.state == MaintenancePackage.State.PRUNED:
        raise HttpError(409, "This package has already been removed.")
    try:
        packaging.remove_files(package.sha256)
    except OSError as exc:
        raise HttpError(409, f"The package files could not be removed: {exc.strerror or exc}") from None
    with transaction.atomic():
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
