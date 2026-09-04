"""Tests for activity.tasks — archive_old_activity."""

import pytest
from django.test import override_settings
from django.utils import timezone

from activity.models import Activity
from activity.tasks import archive_old_activity


def _make_activity(**kwargs):
    return Activity.including_archived.create(verb="test", method="GET", path="/api/test", **kwargs)


def _backdate(activity, days):
    """Override auto_now_add created_at via UPDATE (bypasses auto_now_add)."""
    Activity.including_archived.filter(pk=activity.pk).update(created_at=timezone.now() - timezone.timedelta(days=days))
    activity.refresh_from_db()
    return activity


@pytest.mark.django_db
class TestArchiveOldActivity:
    @override_settings(ACTIVITY_ARCHIVE_AFTER_DAYS=30)
    def test_archives_old_rows(self):
        old = _backdate(_make_activity(), days=31)
        result = archive_old_activity()
        assert result["archived"] >= 1
        old.refresh_from_db()
        assert old.archived_at is not None
        assert not Activity.objects.filter(pk=old.pk).exists()

    @override_settings(ACTIVITY_ARCHIVE_AFTER_DAYS=30)
    def test_leaves_recent_rows_untouched(self):
        recent = _make_activity()
        archive_old_activity()
        recent.refresh_from_db()
        assert recent.archived_at is None

    @override_settings(ACTIVITY_ARCHIVE_AFTER_DAYS=0)
    def test_disabled_when_days_is_zero(self):
        old = _backdate(_make_activity(), days=365)
        result = archive_old_activity()
        assert result["archived"] == 0
        old.refresh_from_db()
        assert old.archived_at is None

    @override_settings(ACTIVITY_ARCHIVE_AFTER_DAYS=30)
    def test_does_not_archive_already_archived_rows(self):
        already_archived = _backdate(_make_activity(), days=60)
        already_archived.archived_at = timezone.now() - timezone.timedelta(days=30)
        already_archived.save(update_fields=["archived_at"])
        before_archived_at = already_archived.archived_at
        archive_old_activity()
        already_archived.refresh_from_db()
        # archived_at should not have changed (already archived)
        assert already_archived.archived_at == before_archived_at
