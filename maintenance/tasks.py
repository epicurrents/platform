"""Celery side of the maintenance app: the celery-tier executor and the spool housekeeping.

``run_job`` is the only thing that executes a celery-tier operation, and what
it executes is the management command the registration names — never anything
read from the request. ``sync_spool`` projects the host agent's files onto the
job rows every minute, reaps celery-tier jobs no worker is running any more and
hashes packages the reconciliation found; ``prune_spool`` removes the files of
jobs long finished. ``send_job_notice`` and ``reapply_erasures`` are dispatched
on commit by the sync, so neither a mail relay nor an account erasure runs
inside it.
"""

import io
import logging
from datetime import timedelta

from celery import shared_task
from celery.exceptions import SoftTimeLimitExceeded
from django.conf import settings
from django.core.management import call_command
from django.utils import timezone

from epicurrents.security_log import log_security_event

logger = logging.getLogger(__name__)

# Days a finished job's spool files are kept before prune_spool removes them.
SPOOL_RETENTION_DAYS = 30
# Past its operation's soft time limit plus this, a celery-tier job that still
# reads requested or running has no worker behind it: the worker died, the
# broker lost the message, or the process was killed past the soft limit.
STALE_MARGIN = timedelta(minutes=15)


def _tail(text: str) -> str:
    """The last ``MAINTENANCE_JOB_OUTPUT_LIMIT`` characters of ``text``."""
    limit = int(getattr(settings, "MAINTENANCE_JOB_OUTPUT_LIMIT", 64 * 1024))
    if len(text) <= limit:
        return text
    return "…[truncated]…\n" + text[-limit:]


def _finish(job, *, state, reason, output, expect):
    """Record the outcome, then announce it: the notice is queued only once the state is saved."""
    from maintenance.notify import dispatch_notice, needs_notice

    announce = needs_notice(job, state)
    finished = job.mark_finished(
        state=state,
        reason=reason,
        expect=expect,
        output=_tail(output),
        last_notified_state=state if announce else job.last_notified_state,
    )
    if finished and announce:
        dispatch_notice(job, state)
    return finished


@shared_task(bind=True)
def run_job(self, job_pk: int):
    """Run a requested celery-tier job: the registered command, output captured, state recorded.

    Exits quietly when the row is no longer ``requested`` (cancelled before the
    worker picked it up) or is not a celery-tier job, and fails it as
    ``maintenance_lock`` while the maintenance flag is up: the host agent is
    between a dump and a restore, and a command writing now writes into a
    database about to be replaced. The command runs inside an audited scope
    targeting the job, so the ORM writes it makes are attributed to it; the
    row's own transitions go through ``transition``, since the compare-and-set
    keeps a concurrent cancel from being overwritten.

    Whatever the command raises ends in a recorded outcome, ``SystemExit``
    included — a command that calls ``sys.exit(1)`` is a failure, ``exit(0)`` a
    success — because a row left ``running`` holds the one-in-flight slot and
    blocks every later job. Interpreter-level exceptions are recorded and then
    re-raised, so a worker shutdown still shuts the worker down.
    """
    from activity.models import Activity
    from activity.system_activity import with_system_activity
    from maintenance.lock import read_lock
    from maintenance.models import MaintenanceJob
    from maintenance.operations import command_argv, get_operation

    job = MaintenanceJob.objects.filter(pk=job_pk).first()
    if job is None or job.executor != MaintenanceJob.Executor.CELERY:
        return {"ran": False}
    operation = get_operation(job.operation)
    with with_system_activity("maintenance.job.run", interface=Activity.Interface.CELERY, target=job):
        if operation is None or operation.executor != "celery":
            _finish(job, state=MaintenanceJob.State.FAILED, reason="refused_operation", output="", expect=["requested"])
            return {"ran": False}
        if read_lock() is not None:
            _finish(
                job,
                state=MaintenanceJob.State.FAILED,
                reason="maintenance_lock",
                output="The platform was under maintenance when the job would have started; nothing ran.\n",
                expect=["requested"],
            )
            return {"ran": False}
        if not job.transition(expect=["requested"], state=MaintenanceJob.State.RUNNING, started_at=timezone.now()):
            return {"ran": False}

        buffer = io.StringIO()
        state, reason = MaintenanceJob.State.SUCCEEDED, ""
        interrupted = None
        try:
            args = operation.args_schema(**(job.args or {}))
            argv = command_argv(operation, args, requested_by_id=job.requested_by_id)
            call_command(operation.command, *argv, stdout=buffer, stderr=buffer)
        except SoftTimeLimitExceeded:
            state, reason = MaintenanceJob.State.FAILED, "timeout"
            buffer.write("\nThe command exceeded its time limit and was stopped.\n")
        except SystemExit as exc:
            from celery.exceptions import WorkerShutdown, WorkerTerminate

            if isinstance(exc, WorkerShutdown | WorkerTerminate):
                state, reason, interrupted = MaintenanceJob.State.FAILED, "interrupted", exc
                buffer.write("\nThe worker shut down while the command ran.\n")
            elif exc.code not in (None, 0):
                state, reason = MaintenanceJob.State.FAILED, "command_failed"
                buffer.write(f"\nThe command exited with status {exc.code}.\n")
        except Exception as exc:
            state, reason = MaintenanceJob.State.FAILED, "command_failed"
            buffer.write(f"\n{type(exc).__name__}: {exc}\n")
            logger.exception("Maintenance job %s (%s) failed", job.job_id, job.operation)
        except BaseException as exc:
            state, reason, interrupted = MaintenanceJob.State.FAILED, "interrupted", exc
            buffer.write(f"\nThe command was interrupted ({type(exc).__name__}).\n")
        _finish(job, state=state, reason=reason, output=buffer.getvalue(), expect=["running"])
        log_security_event(
            "maintenance.job_state",
            job_id=str(job.job_id),
            operation=job.operation,
            state=str(state),
            reason=reason or None,
        )
    if interrupted is not None:
        raise interrupted
    return {"ran": True, "state": str(state)}


def reap_stale_jobs(now=None) -> int:
    """Fail as ``stale`` every celery-tier job still requested or running past its time limit plus a margin.

    The celery tier has no heartbeat; a row a dead worker left behind would
    otherwise hold the one-in-flight slot for good. The clock starts at
    ``started_at`` for a running job and at ``created_at`` for one no worker
    ever picked up. Host-tier rows are the spool sync's: the orphan rule and
    the agent's own statuses cover them.
    """
    from activity.models import Activity
    from activity.system_activity import with_system_activity
    from maintenance.models import MaintenanceJob
    from maintenance.operations import get_operation

    now = now or timezone.now()
    candidates = MaintenanceJob.objects.filter(
        executor=MaintenanceJob.Executor.CELERY,
        state__in=[MaintenanceJob.State.REQUESTED, MaintenanceJob.State.RUNNING],
    )
    stale = []
    for job in candidates:
        operation = get_operation(job.operation)
        limit = timedelta(seconds=operation.soft_time_limit if operation is not None else 3600) + STALE_MARGIN
        since = job.started_at or job.created_at
        if since is not None and now - since > limit:
            stale.append(job)
    if not stale:
        return 0
    reaped = 0
    with with_system_activity(
        "maintenance.job.reap", interface=Activity.Interface.CELERY, metadata={"stale": len(stale)}
    ):
        for job in stale:
            output = (
                job.output + "\nNo worker reported an outcome within the time limit; the job was failed as stale.\n"
            )
            if _finish(job, state=MaintenanceJob.State.FAILED, reason="stale", output=output, expect=[job.state]):
                log_security_event(
                    "maintenance.job_state",
                    job_id=str(job.job_id),
                    operation=job.operation,
                    state=MaintenanceJob.State.FAILED,
                    reason="stale",
                )
                reaped += 1
    return reaped


@shared_task
def sync_spool():
    """The beat half of ``maintenance.spool.sync``, plus the housekeeping only a worker should do.

    Reaps stale celery-tier jobs and hashes the packages the reconciliation
    left ``unverified``. Does nothing while the lock phase is ``updating`` or
    ``rolling_back``, when the database must not be written.
    """
    from maintenance import packaging, spool
    from maintenance.lock import PHASE_VERIFYING, read_lock

    flag = read_lock()
    if flag is not None and flag.phase != PHASE_VERIFYING:
        return {"skipped": "locked"}
    counts = spool.sync()
    counts["stale"] = reap_stale_jobs()
    counts["verified"] = packaging.verify_pending()
    return counts


@shared_task
def send_job_notice(job_pk: int, state: str):
    """Notify superusers that a job entered ``state``; dispatched on commit of the write that recorded it."""
    from maintenance.models import MaintenanceJob
    from maintenance.notify import send_notice

    job = MaintenanceJob.objects.filter(pk=job_pk).first()
    if job is None:
        return {"sent": 0}
    return {"sent": send_notice(job, state)}


@shared_task
def verify_packages():
    """Hash the packages the reconciliation left ``unverified``; see ``packaging.verify_pending``."""
    from maintenance import packaging

    return packaging.verify_pending()


@shared_task
def reapply_erasures():
    """Erase again every recorded account a database restore brought back; see ``maintenance.erasures``."""
    from maintenance import erasures

    return erasures.reapply()


@shared_task
def prune_spool():
    """Remove request, marker, status and log files of jobs finished more than ``SPOOL_RETENTION_DAYS`` ago."""
    from maintenance import spool
    from maintenance.models import MaintenanceJob

    cutoff = timezone.now() - timedelta(days=SPOOL_RETENTION_DAYS)
    removed = 0
    old = MaintenanceJob.objects.filter(
        executor=MaintenanceJob.Executor.HOST, in_flight=False, finished_at__isnull=False, finished_at__lt=cutoff
    )
    for job in old:
        candidates = [
            spool.request_path(job.job_id),
            spool.claimed_path(job.job_id),
            spool.status_path(job.job_id),
            spool.log_path(job.job_id),
        ]
        candidates += [spool.marker_path(job.job_id, kind) for kind in spool.MARKERS]
        for path in candidates:
            try:
                path.unlink()
                removed += 1
            except FileNotFoundError:
                continue
            except OSError as exc:
                logger.warning("Could not remove spool file %s: %s", path.name, exc)
    return {"removed": removed}
