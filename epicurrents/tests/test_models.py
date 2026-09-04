"""Tests for AccessRight model and AccessRightQuerySet."""

import pytest
from django.contrib.contenttypes.models import ContentType
from django.utils import timezone

from epicurrents.models import AccessRight


def _recording_ct_and_id(recording):
    ct = ContentType.objects.get_for_model(recording, for_concrete_model=False)
    return ct, str(recording.pk)


@pytest.mark.django_db
class TestAccessRightQuerySetActive:
    def test_active_returns_non_expiring(self, user, make_user):
        from model_bakery import baker

        recording = baker.make("recordings.Recording", author=user)
        ct, oid = _recording_ct_and_id(recording)
        target = make_user(username="target")
        ar = AccessRight.objects.create(
            content_type=ct,
            object_id=oid,
            access_giver=user,
            access_target=target,
            can_read=True,
        )
        assert AccessRight.objects.active().filter(pk=ar.pk).exists()

    def test_active_excludes_expired(self, user, make_user):
        from model_bakery import baker

        recording = baker.make("recordings.Recording", author=user)
        ct, oid = _recording_ct_and_id(recording)
        target = make_user(username="target")
        ar = AccessRight.objects.create(
            content_type=ct,
            object_id=oid,
            access_giver=user,
            access_target=target,
            can_read=True,
            expires_at=timezone.now() - timezone.timedelta(seconds=1),
        )
        assert not AccessRight.objects.active().filter(pk=ar.pk).exists()

    def test_active_includes_future_expiry(self, user, make_user):
        from model_bakery import baker

        recording = baker.make("recordings.Recording", author=user)
        ct, oid = _recording_ct_and_id(recording)
        target = make_user(username="target")
        ar = AccessRight.objects.create(
            content_type=ct,
            object_id=oid,
            access_giver=user,
            access_target=target,
            can_read=True,
            expires_at=timezone.now() + timezone.timedelta(days=1),
        )
        assert AccessRight.objects.active().filter(pk=ar.pk).exists()


@pytest.mark.django_db
class TestAccessRightForTarget:
    def test_for_target_matches_user(self, user, make_user):
        from model_bakery import baker

        recording = baker.make("recordings.Recording", author=user)
        ct, oid = _recording_ct_and_id(recording)
        target = make_user(username="target")
        ar = AccessRight.objects.create(
            content_type=ct,
            object_id=oid,
            access_giver=user,
            access_target=target,
            can_read=True,
        )
        assert AccessRight.objects.for_target(target).filter(pk=ar.pk).exists()

    def test_for_target_excludes_other_user(self, user, make_user):
        from model_bakery import baker

        recording = baker.make("recordings.Recording", author=user)
        ct, oid = _recording_ct_and_id(recording)
        target = make_user(username="target")
        other = make_user(username="other")
        ar = AccessRight.objects.create(
            content_type=ct,
            object_id=oid,
            access_giver=user,
            access_target=target,
            can_read=True,
        )
        assert not AccessRight.objects.for_target(other).filter(pk=ar.pk).exists()

    def test_for_target_matches_group(self, user, make_user):
        from django.contrib.auth.models import Group
        from model_bakery import baker

        recording = baker.make("recordings.Recording", author=user)
        ct, oid = _recording_ct_and_id(recording)
        member = make_user(username="member")
        group = Group.objects.create(name="testgroup")
        member.groups.add(group)
        ar = AccessRight.objects.create(
            content_type=ct,
            object_id=oid,
            access_giver=user,
            access_target_group=group,
            can_read=True,
        )
        assert AccessRight.objects.for_target(member).filter(pk=ar.pk).exists()

    def test_for_target_unauthenticated_returns_none(self, user):
        from unittest.mock import MagicMock

        anon = MagicMock()
        anon.is_authenticated = False
        assert not AccessRight.objects.for_target(anon).exists()
