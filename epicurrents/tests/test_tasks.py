"""Tests for epicurrents background tasks."""

import pytest
from django.contrib.contenttypes.models import ContentType
from django.utils import timezone

from epicurrents.models import AccessRight
from epicurrents.tasks import purge_expired_access_rights


@pytest.mark.django_db
class TestPurgeExpiredAccessRights:
    def _make_ar(self, user, recording, *, expires_at=None):
        ct = ContentType.objects.get_for_model(recording, for_concrete_model=False)
        return AccessRight.objects.create(
            content_type=ct,
            object_id=str(recording.pk),
            access_giver=user,
            access_target=user,
            can_read=True,
            expires_at=expires_at,
        )

    def test_deletes_expired_rows(self, user):
        from model_bakery import baker

        recording = baker.make("recordings.Recording", author=user)
        ar = self._make_ar(
            user,
            recording,
            expires_at=timezone.now() - timezone.timedelta(seconds=1),
        )
        result = purge_expired_access_rights()
        assert result["deleted"] >= 1
        assert not AccessRight.objects.filter(pk=ar.pk).exists()

    def test_keeps_non_expiring_rows(self, user):
        from model_bakery import baker

        recording = baker.make("recordings.Recording", author=user)
        ar = self._make_ar(user, recording, expires_at=None)
        purge_expired_access_rights()
        assert AccessRight.objects.filter(pk=ar.pk).exists()

    def test_keeps_future_expiry(self, user):
        from model_bakery import baker

        recording = baker.make("recordings.Recording", author=user)
        ar = self._make_ar(
            user,
            recording,
            expires_at=timezone.now() + timezone.timedelta(days=1),
        )
        purge_expired_access_rights()
        assert AccessRight.objects.filter(pk=ar.pk).exists()

    def test_returns_deleted_count(self, user):
        from model_bakery import baker

        recording = baker.make("recordings.Recording", author=user)
        self._make_ar(
            user,
            recording,
            expires_at=timezone.now() - timezone.timedelta(seconds=1),
        )
        result = purge_expired_access_rights()
        assert isinstance(result["deleted"], int)
        assert result["deleted"] >= 1

    def test_creates_celery_activity_row(self, user):
        from model_bakery import baker

        from activity.models import Activity

        recording = baker.make("recordings.Recording", author=user)
        self._make_ar(
            user,
            recording,
            expires_at=timezone.now() - timezone.timedelta(seconds=1),
        )
        purge_expired_access_rights()
        activity = (
            Activity.objects.filter(
                verb="epicurrents.access_rights.purge",
                interface=Activity.Interface.CELERY,
            )
            .order_by("-created_at")
            .first()
        )
        assert activity is not None
        assert activity.actor is None
        assert "cutoff" in activity.metadata

    def test_purged_row_produces_delete_audit(self, user):
        from model_bakery import baker

        from activity.models import Activity, ObjectChangeLog

        recording = baker.make("recordings.Recording", author=user)
        ar = self._make_ar(
            user,
            recording,
            expires_at=timezone.now() - timezone.timedelta(seconds=1),
        )
        ar_pk = ar.pk
        purge_expired_access_rights()
        delete_rows = list(
            ObjectChangeLog.objects.filter(
                content_type=ContentType.objects.get_for_model(AccessRight),
                object_id=str(ar_pk),
                action=ObjectChangeLog.ACTION_DELETE,
            )
        )
        assert len(delete_rows) == 1
        parent = Activity.objects.filter(verb="epicurrents.access_rights.purge").latest("created_at")
        assert delete_rows[0].activity_id == parent.pk


@pytest.mark.django_db
class TestClearExpiredSessions:
    def test_runs_without_error(self):
        from epicurrents.tasks import clear_expired_sessions

        # Just verify the task completes without raising
        clear_expired_sessions()
