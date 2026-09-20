"""Celery side of the maintenance app: the celery-tier executor and the spool housekeeping.

``run_job`` is the only thing that executes a celery-tier operation, and what
it executes is the management command the registration names — never anything
read from the request. ``sync_spool`` projects the host agent's files onto the
job rows every minute; ``prune_spool`` removes the files of jobs long finished.
"""

import io
import logging

from celery import shared_task
from celery.exceptions import SoftTimeLimitExceeded
from django.conf import settings
from django.core.management import call_command
from django.utils import timezone

from epicurrents.security_log import log_security_event

logger = logging.getLogger(__name__)

# Days a finished job's spool files are kept before prune_spool removes them.
SPOOL_RETENTION_DAYS = 30


def _tail(text: str) -> str:
    """The last ``MAINTENANCE_JOB_OUTPUT_LIMIT`` characters of ``text``."""
    limit = int(getattr(settings, "MAINTENANCE_JOB_OUTPUT_LIMIT", 64 * 1024))
    if len(text) <= limit:
        return text
    return "…[truncated]…\n" + text[-limit:]


@shared_task(bind=True)
def run_job(self, job_pk: int):
    """Run a requested celery-tier job: the registered command, output captured, state recorded.

    Exits quietly when the row is no longer ``requested`` (cancelled before the
    worker picked it up) or is not a celery-tier job. The command runs inside an
    audited scope targeting the job, so the ORM writes it makes are attributed
    to it; the row's own transitions go through ``transition``, since the
    compare-and-set keeps a concurrent cancel from being overwritten.
    """
    from activity.models import Activity
    from activity.system_activity import with_system_activity
    from maintenance.models import MaintenanceJob
    from maintenance.notify import notify_job_state
    from maintenance.operations import get_operation

    job = MaintenanceJob.objects.filter(pk=job_pk).first()
    if job is None or job.executor != MaintenanceJob.Executor.CELERY:
        return {"ran": False}
    operation = get_operation(job.operation)
    with with_system_activity("maintenance.job.run", interface=Activity.Interface.CELERY, target=job):
        if operation is None or operation.executor != "celery":
            job.mark_finished(state=MaintenanceJob.State.FAILED, reason="refused_operation", expect=["requested"])
            return {"ran": False}
        if not job.transition(expect=["requested"], state=MaintenanceJob.State.RUNNING, started_at=timezone.now()):
            return {"ran": False}

        buffer = io.StringIO()
        state, reason = MaintenanceJob.State.SUCCEEDED, ""
        try:
            args = operation.args_schema(**(job.args or {}))
            call_command(operation.command, *operation.command_args(args), stdout=buffer, stderr=buffer)
        except SoftTimeLimitExceeded:
            state, reason = MaintenanceJob.State.FAILED, "timeout"
            buffer.write("\nThe command exceeded its time limit and was stopped.\n")
        except Exception as exc:
            state, reason = MaintenanceJob.State.FAILED, "command_failed"
            buffer.write(f"\n{type(exc).__name__}: {exc}\n")
            logger.exception("Maintenance job %s (%s) failed", job.job_id, job.operation)
        announced = notify_job_state(job, state=state)
        job.mark_finished(
            state=state,
            reason=reason,
            expect=["running"],
            output=_tail(buffer.getvalue()),
            last_notified_state=announced or job.last_notified_state,
        )
        log_security_event(
            "maintenance.job_state",
            job_id=str(job.job_id),
            operation=job.operation,
            state=str(state),
            reason=reason or None,
        )
    return {"ran": True, "state": str(state)}


@shared_task
def sync_spool():
    """Project the spool onto the job rows; the beat half of ``maintenance.spool.sync``."""
    from maintenance import spool

    return spool.sync()


@shared_task
def prune_spool():
    """Remove request, marker, status and log files of jobs finished more than ``SPOOL_RETENTION_DAYS`` ago."""
    from datetime import timedelta

    from maintenance import spool
    from maintenance.models import MaintenanceJob

    cutoff = timezone.now() - timedelta(days=SPOOL_RETENTION_DAYS)
    removed = 0
    old = MaintenanceJob.objects.filter(
        executor=MaintenanceJob.Executor.HOST, in_flight=False, finished_at__isnull=False, finished_at__lt=cutoff
    )
    for job in old:
        candidates = [spool.request_path(job.job_id), spool.status_path(job.job_id), spool.log_path(job.job_id)]
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
