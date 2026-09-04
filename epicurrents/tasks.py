"""Epicurrents core Celery tasks — periodic AccessRight purge and session cleanup."""

import logging

from celery import shared_task
from django.core.management import call_command
from django.utils import timezone

logger = logging.getLogger(__name__)

# Fallback for the heartbeat interval when the beat entry has been removed or
# renamed. The schedule in settings is the real value; this keeps the emitted
# field honest rather than absent if someone reschedules the task by hand.
SECURITY_HEARTBEAT_INTERVAL_SECONDS = 300


@shared_task
def clear_expired_sessions():
    """Delete expired Django sessions from the database.

    Delegates to the built-in `clearsessions` management command, which respects
    whichever session backend is configured. Safe to run frequently — it only
    touches rows whose expiry date has already passed.
    """
    call_command("clearsessions")
    logger.info("clear_expired_sessions: completed")


@shared_task
def purge_expired_access_rights():
    """Hard-delete AccessRight rows whose expiry timestamp has passed.

    Expired rights are already inactive (the active() queryset filters them out),
    but the rows accumulate over time. This task removes them permanently so the
    table stays compact. Runs on a schedule defined in CELERY_BEAT_SCHEDULE.

    Audit attribution: the bulk delete runs inside
    ``with_system_activity("epicurrents.access_rights.purge",
    interface=CELERY)`` so each removed row fires ``pre_delete`` and
    produces a DELETE ``ObjectChangeLog`` entry attributed to the same
    parent ``Activity``.
    """
    from activity.models import Activity
    from activity.system_activity import with_system_activity
    from epicurrents.models import AccessRight

    cutoff = timezone.now()
    with with_system_activity(
        "epicurrents.access_rights.purge",
        interface=Activity.Interface.CELERY,
        metadata={"cutoff": cutoff.isoformat()},
    ):
        deleted, _ = AccessRight.objects.filter(expires_at__isnull=False, expires_at__lte=cutoff).delete()
    logger.info("purge_expired_access_rights: deleted=%d cutoff=%s", deleted, cutoff.isoformat())
    return {"deleted": deleted}


@shared_task
def emit_security_heartbeat():
    """Emit a periodic liveness signal on the security log stream.

    An off-host log sink cannot alert on a host being compromised; it can only
    alert on the stream stopping. A stream carrying security events alone is
    silent on a healthy system, so silence has to be made meaningful by something
    that speaks when all is well — this task is that something, and its absence
    at the sink is the alarm.

    It reports on the whole path rather than on liveness of any one component:
    beat has to schedule it, a worker has to run it, the logging configuration
    has to route it, and the shipper has to deliver it. Any break in that chain
    stops the heartbeat, which is the intended sensitivity. It says nothing about
    whether ``web`` is serving requests — that is an uptime check's job, and
    conflating the two would make this fire for the wrong reason.

    ``interval_seconds`` is published in the event so a receiver derives its
    alerting window from the sender rather than repeating the schedule in a
    second place, where the two would drift apart silently.
    """
    from django.conf import settings

    from epicurrents.security_log import log_security_event

    schedule = settings.CELERY_BEAT_SCHEDULE.get("emit-security-heartbeat", {})
    interval = schedule.get("schedule", SECURITY_HEARTBEAT_INTERVAL_SECONDS)
    if not isinstance(interval, (int, float)):
        # A crontab or solar schedule — legitimate, and not a number of
        # seconds. Publishing the fallback keeps the field present and
        # numeric for the receiver; raising here would stop the heartbeat
        # over a formatting detail, which the far end cannot distinguish
        # from the host being gone.
        interval = SECURITY_HEARTBEAT_INTERVAL_SECONDS
    log_security_event("system.heartbeat", interval_seconds=int(interval))
    return {"interval_seconds": int(interval)}
