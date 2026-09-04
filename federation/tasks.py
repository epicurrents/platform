"""Celery tasks for the federation app — audit-log retention pruning."""

import logging
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)


@shared_task
def prune_federation_audit_log():
    """Delete ``FederationAuditLog`` rows older than the retention window.

    The retention window comes from ``FEDERATION_AUDIT_RETENTION_DAYS``
    (default 2200 days ≈ 6 years, matching the HIPAA-style regulatory
    minimum documented on the model). Operators with a lower regulatory
    floor tune the setting down; setting it to ``0`` disables pruning
    entirely for deployments that must keep the log indefinitely.

    Rows carry ``remote_user_id`` — a remote data subject's pseudonymous
    identifier — so unbounded retention conflicts with storage limitation
    (GDPR Art. 5(1)(e)); this task is the storage limit. Bulk deletion is
    deliberate: audit rows about *federation access* are not user-data
    mutations, and the rows must not generate ``ObjectChangeLog`` entries
    of their own (this task runs outside an audited scope).
    """
    from federation.models import FederationAuditLog

    retention_days = getattr(settings, "FEDERATION_AUDIT_RETENTION_DAYS", 2200)
    if not retention_days:
        return {"pruned": 0, "disabled": True}

    cutoff = timezone.now() - timedelta(days=retention_days)
    pruned, _ = FederationAuditLog.objects.filter(created_at__lt=cutoff).delete()
    logger.info(
        "prune_federation_audit_log: pruned=%d cutoff=%s retention_days=%d",
        pruned,
        cutoff.isoformat(),
        retention_days,
    )
    return {"pruned": pruned, "disabled": False}
