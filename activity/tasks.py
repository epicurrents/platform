"""Activity Celery tasks — archive old Activity rows + periodic integrity check."""

import logging

from celery import shared_task
from django.utils import timezone

logger = logging.getLogger(__name__)


@shared_task
def verify_audit_integrity():
    """Periodic integrity check over the audit trail.

    Delegates to ``activity.integrity_check.run_integrity_check`` so the
    same machinery is invokable from a shell for ad-hoc forensics.
    Anomalies emit structured security events via
    ``epicurrents.security_log``; clean runs log a single INFO line.
    """
    from activity.integrity_check import run_integrity_check

    return run_integrity_check()


# Rows are archived in batches to avoid one enormous UPDATE holding a lock.
_BATCH_SIZE = 1000


@shared_task
def archive_old_activity():
    """Mark Activity rows older than ACTIVITY_ARCHIVE_AFTER_DAYS as archived.

    Archived rows are hidden from the default Activity.objects queryset but are
    never deleted — use Activity.including_archived to query the full history.
    Set ACTIVITY_ARCHIVE_AFTER_DAYS=0 to disable archiving entirely.
    """
    from django.conf import settings

    from activity.models import Activity

    days = getattr(settings, "ACTIVITY_ARCHIVE_AFTER_DAYS", 90)
    if days <= 0:
        logger.info("archive_old_activity: disabled (ACTIVITY_ARCHIVE_AFTER_DAYS=%d)", days)
        return {"archived": 0}

    cutoff = timezone.now() - timezone.timedelta(days=days)
    total = 0

    while True:
        # Fetch a batch of IDs, then update only those rows to keep the lock
        # window small and avoid loading full model instances into memory.
        ids = list(
            Activity.including_archived.filter(created_at__lt=cutoff, archived_at__isnull=True).values_list(
                "id", flat=True
            )[:_BATCH_SIZE]
        )
        if not ids:
            break
        updated = Activity.including_archived.filter(id__in=ids).update(archived_at=timezone.now())
        total += updated

    logger.info(
        "archive_old_activity: archived=%d cutoff=%s days=%d",
        total,
        cutoff.isoformat(),
        days,
    )
    return {"archived": total}
