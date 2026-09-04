"""Tests for library.tasks — trash retention purge."""

import pytest
from django.test import override_settings
from django.utils import timezone
from model_bakery import baker

from library.models import Collection, Dataset
from library.tasks import purge_deleted_library


def _backdate_delete(obj, days):
    type(obj).objects.filter(pk=obj.pk).update(deleted_at=timezone.now() - timezone.timedelta(days=days))


@pytest.mark.django_db
class TestPurgeDeletedLibrary:
    def test_purges_trashed_rows_past_retention(self, user):
        collection = baker.make(Collection, author=user, name="old trash")
        dataset = baker.make(Dataset, author=user, name="old trash ds")
        _backdate_delete(collection, 45)
        _backdate_delete(dataset, 45)
        with override_settings(LIBRARY_TRASH_RETENTION_DAYS=30):
            result = purge_deleted_library()
        assert not Collection.objects.filter(pk=collection.pk).exists()
        assert not Dataset.objects.filter(pk=dataset.pk).exists()
        assert result["Collection"] == 1
        assert result["Dataset"] == 1

    def test_keeps_recently_trashed_and_live_rows(self, user):
        live = baker.make(Collection, author=user)
        recent = baker.make(Collection, author=user)
        _backdate_delete(recent, 5)
        with override_settings(LIBRARY_TRASH_RETENTION_DAYS=30):
            purge_deleted_library()
        assert Collection.objects.filter(pk=live.pk).exists()
        assert Collection.objects.filter(pk=recent.pk).exists()

    def test_live_child_reparents_to_root_on_parent_purge(self, user):
        parent = baker.make(Collection, author=user)
        child = baker.make(Collection, author=user, parent=parent)
        _backdate_delete(parent, 45)
        with override_settings(LIBRARY_TRASH_RETENTION_DAYS=30):
            purge_deleted_library()
        child.refresh_from_db()
        assert child.parent is None
        assert child.deleted_at is None

    def test_purge_produces_audit_rows(self, user):
        from activity.models import Activity, ObjectChangeLog

        collection = baker.make(Collection, author=user)
        _backdate_delete(collection, 45)
        with override_settings(LIBRARY_TRASH_RETENTION_DAYS=30):
            purge_deleted_library()
        activity = Activity.objects.filter(verb="library.purge").latest("created_at")
        assert ObjectChangeLog.objects.filter(
            activity=activity,
            action=ObjectChangeLog.ACTION_DELETE,
            object_id=str(collection.pk),
        ).exists()
