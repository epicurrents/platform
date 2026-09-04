"""Media Celery tasks.

``purge_deleted_media``
    Hard-delete media files that have been soft-deleted longer than the
    retention window — the erasure half of the soft-delete + purge contract.
"""

import logging
from datetime import timedelta
from pathlib import Path

from celery import shared_task
from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)


@shared_task
def purge_deleted_media():
    """Hard-delete media files trashed beyond the retention window.

    ⚠️ LOAD-BEARING — GDPR Art. 17 erasure pipeline for non-signal media.
    A user (or operator) trashes a media file → ``deleted_at`` is set → after
    ``MEDIA_TRASH_RETENTION_DAYS`` the row + file are permanently removed. The
    single filter (``deleted_at__isnull=False, deleted_at__lt=cutoff``) is the
    contract: silent narrowing keeps PHI-bearing media (a face in a video, a
    subject identifier in a document) past the window with no visible signal;
    widening it reaps still-live files. The file is unlinked before the row is
    removed, and an unlink failure preserves the row so the next run retries
    rather than leaving a dangling DB pointer or an orphaned file. See
    AGENTS.md → *Load-bearing files* and ``TestPurgeDeletedMediaContract`` in
    ``media/tests/test_tasks.py`` before modifying.

    Unlike :func:`recordings.tasks.purge_deleted_recordings` there is no
    orphan-reaper branch: media has no processing step, so there is no
    PENDING / PROCESSING state to strand a row in.

    Per-row ``media.delete()`` fires the ``pre_delete`` audit signal inside the
    ``with_system_activity`` scope, so each deletion lands in
    ``ObjectChangeLog`` under one parent ``Activity`` row
    (``verb="media.purge"``, ``interface=celery``).
    """
    from activity.models import Activity
    from activity.system_activity import with_system_activity

    retention_days = getattr(settings, "MEDIA_TRASH_RETENTION_DAYS", 30)
    cutoff = timezone.now() - timedelta(days=retention_days)

    with with_system_activity(
        "media.purge",
        interface=Activity.Interface.CELERY,
        metadata={"retention_days": retention_days},
    ):
        return _purge_deleted_media_body(cutoff=cutoff, retention_days=retention_days)


def _purge_deleted_media_body(*, cutoff, retention_days):
    """Walk the soft-deleted queryset inside an open audited scope.

    Extracted so the outer task owns the ``with_system_activity`` scope while
    the per-row ``media.delete()`` here fires ``pre_delete`` under it.
    """
    from media.models import MediaFile

    queryset = MediaFile.objects.filter(
        deleted_at__isnull=False,
        deleted_at__lt=cutoff,
    )

    purged = 0
    errors = 0

    for media in queryset.iterator():
        file_path = Path(media.file_path)
        try:
            if file_path.exists():
                file_path.unlink()
        except OSError as exc:
            logger.error(
                "Could not delete file %s for media %d: %s — skipping row.",
                media.file_path,
                media.pk,
                exc,
            )
            errors += 1
            continue

        media.delete()
        purged += 1

    logger.info(
        "purge_deleted_media: purged=%d errors=%d cutoff=%s retention_days=%d",
        purged,
        errors,
        cutoff.isoformat(),
        retention_days,
    )
    return {"purged": purged, "errors": errors}
