"""Tests for federation.tasks — audit-log retention pruning."""

import pytest
from django.test import override_settings
from django.utils import timezone
from model_bakery import baker

from federation.models import FederationAuditLog
from federation.tasks import prune_federation_audit_log


def _log_row(days_old, **kwargs):
    row = baker.make(
        FederationAuditLog,
        peer_url="https://peer.example",
        remote_user_id="remote-sub-1",
        action="download_recording",
        status_code=200,
        **kwargs,
    )
    FederationAuditLog.objects.filter(pk=row.pk).update(created_at=timezone.now() - timezone.timedelta(days=days_old))
    return row


@pytest.mark.django_db
class TestPruneFederationAuditLog:
    def test_prunes_rows_past_retention(self):
        old = _log_row(days_old=400)
        recent = _log_row(days_old=10)
        with override_settings(FEDERATION_AUDIT_RETENTION_DAYS=365):
            result = prune_federation_audit_log()
        assert result == {"pruned": 1, "disabled": False}
        assert not FederationAuditLog.objects.filter(pk=old.pk).exists()
        assert FederationAuditLog.objects.filter(pk=recent.pk).exists()

    def test_zero_retention_disables_pruning(self):
        _log_row(days_old=4000)
        with override_settings(FEDERATION_AUDIT_RETENTION_DAYS=0):
            result = prune_federation_audit_log()
        assert result == {"pruned": 0, "disabled": True}
        assert FederationAuditLog.objects.count() == 1
