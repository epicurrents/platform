"""Tests for activity models — ActiveActivityManager and archived_at behavior."""

import pytest
from django.utils import timezone

from activity.models import Activity, ObjectChangeLog


@pytest.mark.django_db
class TestActiveActivityManager:
    def _make_activity(self, **kwargs):
        return Activity.including_archived.create(verb="test", method="GET", path="/api/test", **kwargs)

    def test_default_manager_excludes_archived(self):
        archived = self._make_activity(archived_at=timezone.now())
        assert not Activity.objects.filter(pk=archived.pk).exists()

    def test_default_manager_includes_non_archived(self):
        active = self._make_activity()
        assert Activity.objects.filter(pk=active.pk).exists()

    def test_including_archived_returns_all(self):
        archived = self._make_activity(archived_at=timezone.now())
        active = self._make_activity()
        all_pks = set(Activity.including_archived.values_list("pk", flat=True))
        assert archived.pk in all_pks
        assert active.pk in all_pks

    def test_archive_marks_archived_at(self):
        activity = self._make_activity()
        Activity.including_archived.filter(pk=activity.pk).update(archived_at=timezone.now())
        activity.refresh_from_db()
        assert activity.archived_at is not None
        assert not Activity.objects.filter(pk=activity.pk).exists()


@pytest.mark.django_db
class TestObjectChangeLogNeverArchived:
    """ObjectChangeLog is perpetual — it has no archived_at field."""

    def test_object_change_log_has_no_archived_at(self):
        field_names = [f.name for f in ObjectChangeLog._meta.get_fields()]
        assert "archived_at" not in field_names
