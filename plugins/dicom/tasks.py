"""Celery tasks for the *dicom* plugin.

``purge_deleted_dicom_studies``
    Hard-delete studies that have been soft-deleted longer than the retention
    window, plus instances stranded mid-upload — the erasure half of the
    soft-delete + purge contract.

DICOM ingest itself is synchronous (see ``plugins/dicom/ingest.py``); there
are no indexing tasks.
"""

import logging
from datetime import timedelta
from pathlib import Path

from celery import shared_task
from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

# A PENDING instance normally lives for milliseconds — the gap between the
# upload transaction committing and the staging→final file move. One stranded
# for this long marks a request that died in that gap.
ORPHAN_PENDING_MAX_AGE = timedelta(hours=24)


@shared_task(name="dicom.purge_deleted_dicom_studies")
def purge_deleted_dicom_studies():
    """Hard-delete DICOM studies trashed beyond the retention window.

    ⚠️ LOAD-BEARING — GDPR Art. 17 erasure pipeline for DICOM imaging. A user
    trashes a study → ``deleted_at`` is set → after
    ``DICOM_TRASH_RETENTION_DAYS`` the study, its series/instance rows, and
    the stored files are permanently removed. The purge filter
    (``deleted_at__isnull=False, deleted_at__lt=cutoff``) is the contract:
    silent narrowing keeps patient-identifying imaging past the window with
    no visible signal; widening it reaps live studies. File unlinking happens
    in the ``pre_delete`` receiver (``plugins/dicom/signals.py``) inside the
    per-study delete, so an unlink failure aborts and rolls back that study's
    deletion and the next run retries — rows and files never diverge.

    The orphan branch reaps ``PENDING`` instances older than
    ``ORPHAN_PENDING_MAX_AGE``: rows stranded between the upload transaction
    commit and the staging→final move by a crashed request. Their staged
    and/or final files are removed best-effort before the row deletion.

    Per-row deletes fire the audit signals inside the ``with_system_activity``
    scope, so each erasure lands in ``ObjectChangeLog`` under one parent
    ``Activity`` row (``verb="dicom.purge"``, ``interface=celery``). Contract
    test: ``TestPurgeDeletedDicomStudiesContract`` in
    ``plugins/dicom/tests/test_tasks.py``.
    """
    from activity.models import Activity
    from activity.system_activity import with_system_activity

    retention_days = getattr(settings, "DICOM_TRASH_RETENTION_DAYS", 30)
    cutoff = timezone.now() - timedelta(days=retention_days)

    with with_system_activity(
        "dicom.purge",
        interface=Activity.Interface.CELERY,
        metadata={"retention_days": retention_days},
    ):
        return _purge_body(cutoff=cutoff, retention_days=retention_days)


def _purge_body(*, cutoff, retention_days):
    """Walk the purge querysets inside an open audited scope.

    Extracted so the outer task owns the ``with_system_activity`` scope while
    the per-row deletes here fire the audit signals under it.
    """
    from django.db import transaction

    from plugins.dicom.ingest import refresh_study_aggregates
    from plugins.dicom.models import DicomInstance, DicomStudy

    purged = 0
    errors = 0

    queryset = DicomStudy.objects.filter(
        deleted_at__isnull=False,
        deleted_at__lt=cutoff,
    )
    for study in queryset.iterator():
        try:
            with transaction.atomic():
                study.delete()
        except OSError as exc:
            logger.error(
                "Could not purge DicomStudy %d (file unlink failed): %s — keeping row for the next run.",
                study.pk,
                exc,
            )
            errors += 1
            continue
        purged += 1

    # Orphan reaper: PENDING instances stranded by a crashed upload request.
    reaped = 0
    orphan_cutoff = timezone.now() - ORPHAN_PENDING_MAX_AGE
    staging_path = Path(getattr(settings, "DICOM_STAGING_PATH", "/data/dicom-staging"))
    upload_path = Path(getattr(settings, "DICOM_UPLOAD_PATH", "/data/dicom"))
    orphan_qs = DicomInstance.objects.filter(
        status=DicomInstance.Status.PENDING,
        created_at__lt=orphan_cutoff,
    ).select_related("series__study")
    touched_studies = {}
    for inst in orphan_qs.iterator():
        for candidate in (staging_path / inst.stored_name, upload_path / inst.stored_name):
            try:
                candidate.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                logger.warning("Could not remove orphan file %s: %s", candidate, exc)
        study = inst.series.study
        touched_studies[study.pk] = study
        inst.delete()
        reaped += 1
    for study in touched_studies.values():
        refresh_study_aggregates(study)

    logger.info(
        "purge_deleted_dicom_studies: purged=%d reaped=%d errors=%d cutoff=%s retention_days=%d",
        purged,
        reaped,
        errors,
        cutoff.isoformat(),
        retention_days,
    )
    return {"purged": purged, "reaped": reaped, "errors": errors}
