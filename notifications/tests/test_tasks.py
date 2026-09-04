"""Tests for notifications.tasks — send_push_to_user."""

from unittest.mock import MagicMock, patch

import pytest
from django.test import override_settings

from notifications.models import PushSubscription
from notifications.tasks import send_push_to_user

# webpush is imported inside send_push_to_user, so we patch at its source module.
_WEBPUSH_PATH = "pywebpush.webpush"


@pytest.mark.django_db
class TestSendPushToUser:
    @override_settings(WEBPUSH_VAPID_PRIVATE_KEY="")
    def test_skips_when_no_vapid_key(self, user):
        PushSubscription.objects.create(
            user=user,
            endpoint="https://push.example.com/sub/1",
            p256dh="key",
            auth="auth",
        )
        result = send_push_to_user(user_id=user.pk, title="Test", body="body")
        assert result == {"sent": 0, "stale": 0}

    @override_settings(WEBPUSH_VAPID_PRIVATE_KEY="fake-private-key")
    def test_sends_to_all_user_subscriptions(self, user):
        PushSubscription.objects.create(
            user=user,
            endpoint="https://push.example.com/sub/1",
            p256dh="key1",
            auth="auth1",
        )
        PushSubscription.objects.create(
            user=user,
            endpoint="https://push.example.com/sub/2",
            p256dh="key2",
            auth="auth2",
        )
        with patch(_WEBPUSH_PATH) as mock_webpush:
            result = send_push_to_user(user_id=user.pk, title="T", body="B")
        assert mock_webpush.call_count == 2
        assert result["sent"] == 2
        assert result["stale"] == 0

    @override_settings(WEBPUSH_VAPID_PRIVATE_KEY="fake-private-key")
    def test_removes_stale_410_subscriptions(self, user):
        sub = PushSubscription.objects.create(
            user=user,
            endpoint="https://push.example.com/gone",
            p256dh="key",
            auth="auth",
        )
        from pywebpush import WebPushException

        mock_response = MagicMock()
        mock_response.status_code = 410
        exc = WebPushException("Gone", response=mock_response)

        with patch(_WEBPUSH_PATH, side_effect=exc):
            result = send_push_to_user(user_id=user.pk, title="T", body="B")

        assert result["stale"] == 1
        assert not PushSubscription.objects.filter(pk=sub.pk).exists()

    @override_settings(WEBPUSH_VAPID_PRIVATE_KEY="fake-private-key")
    def test_removes_stale_404_subscriptions(self, user):
        sub = PushSubscription.objects.create(
            user=user,
            endpoint="https://push.example.com/notfound",
            p256dh="key",
            auth="auth",
        )
        from pywebpush import WebPushException

        mock_response = MagicMock()
        mock_response.status_code = 404
        exc = WebPushException("Not Found", response=mock_response)

        with patch(_WEBPUSH_PATH, side_effect=exc):
            result = send_push_to_user(user_id=user.pk, title="T", body="B")

        assert result["stale"] == 1
        assert not PushSubscription.objects.filter(pk=sub.pk).exists()

    @override_settings(WEBPUSH_VAPID_PRIVATE_KEY="fake-private-key")
    def test_non_410_error_does_not_remove_subscription(self, user):
        sub = PushSubscription.objects.create(
            user=user,
            endpoint="https://push.example.com/error",
            p256dh="key",
            auth="auth",
        )
        from pywebpush import WebPushException

        mock_response = MagicMock()
        mock_response.status_code = 500
        exc = WebPushException("Server Error", response=mock_response)

        with patch(_WEBPUSH_PATH, side_effect=exc):
            result = send_push_to_user(user_id=user.pk, title="T", body="B")

        assert result["stale"] == 0
        assert PushSubscription.objects.filter(pk=sub.pk).exists()

    @override_settings(WEBPUSH_VAPID_PRIVATE_KEY="fake-private-key")
    def test_no_subscriptions_returns_zero_counts(self, user):
        with patch(_WEBPUSH_PATH) as mock_webpush:
            result = send_push_to_user(user_id=user.pk, title="T", body="B")
        mock_webpush.assert_not_called()
        assert result == {"sent": 0, "stale": 0}

    @override_settings(WEBPUSH_VAPID_PRIVATE_KEY="fake-private-key")
    def test_payload_includes_title_body_and_data(self, user):
        PushSubscription.objects.create(
            user=user,
            endpoint="https://push.example.com/sub",
            p256dh="key",
            auth="auth",
        )
        import json

        with patch(_WEBPUSH_PATH) as mock_webpush:
            send_push_to_user(
                user_id=user.pk,
                title="Recording ready",
                body="Your file is done.",
                data={"type": "recording_ready", "recording_id": 42},
            )
        call_kwargs = mock_webpush.call_args[1]
        payload = json.loads(call_kwargs["data"])
        assert payload["title"] == "Recording ready"
        assert payload["body"] == "Your file is done."
        assert payload["type"] == "recording_ready"
        assert payload["recording_id"] == 42


@pytest.mark.django_db
class TestStaleSubscriptionAudit:
    """Stale-subscription cleanup must produce an audit trail.

    Successful pushes are deliberately not wrapped in `with_system_activity`
    (operational telemetry); only when at least one subscription was found
    stale does the cleanup open an audited scope.
    """

    @override_settings(WEBPUSH_VAPID_PRIVATE_KEY="fake-private-key")
    def test_stale_cleanup_creates_celery_activity_row(self, user):
        from pywebpush import WebPushException

        from activity.models import Activity

        PushSubscription.objects.create(
            user=user,
            endpoint="https://push.example.com/gone",
            p256dh="key",
            auth="auth",
        )
        mock_response = MagicMock()
        mock_response.status_code = 410
        exc = WebPushException("Gone", response=mock_response)

        with patch(_WEBPUSH_PATH, side_effect=exc):
            send_push_to_user(user_id=user.pk, title="T", body="B")

        activity = (
            Activity.objects.filter(
                verb="notifications.subscription.purge_stale",
                interface=Activity.Interface.CELERY,
            )
            .order_by("-created_at")
            .first()
        )
        assert activity is not None
        # user_id rides on metadata, not as a target — the cleanup task
        # does not load the User instance.
        assert activity.target_object_id == ""
        assert activity.target_content_type is None
        assert activity.metadata == {"user_id": user.pk, "count": 1}

    @override_settings(WEBPUSH_VAPID_PRIVATE_KEY="fake-private-key")
    def test_stale_subscription_delete_audit_row(self, user):
        from django.contrib.contenttypes.models import ContentType
        from pywebpush import WebPushException

        from activity.models import Activity, ObjectChangeLog

        sub = PushSubscription.objects.create(
            user=user,
            endpoint="https://push.example.com/gone",
            p256dh="key",
            auth="auth",
        )
        sub_pk = sub.pk
        mock_response = MagicMock()
        mock_response.status_code = 410
        exc = WebPushException("Gone", response=mock_response)

        with patch(_WEBPUSH_PATH, side_effect=exc):
            send_push_to_user(user_id=user.pk, title="T", body="B")

        delete_rows = list(
            ObjectChangeLog.objects.filter(
                content_type=ContentType.objects.get_for_model(PushSubscription),
                object_id=str(sub_pk),
                action=ObjectChangeLog.ACTION_DELETE,
            )
        )
        assert len(delete_rows) == 1
        parent = Activity.objects.filter(verb="notifications.subscription.purge_stale").latest("created_at")
        assert delete_rows[0].activity_id == parent.pk

    @override_settings(WEBPUSH_VAPID_PRIVATE_KEY="fake-private-key")
    def test_successful_push_does_not_open_activity_scope(self, user):
        from activity.models import Activity

        PushSubscription.objects.create(
            user=user,
            endpoint="https://push.example.com/ok",
            p256dh="key",
            auth="auth",
        )
        with patch(_WEBPUSH_PATH):
            send_push_to_user(user_id=user.pk, title="T", body="B")

        assert not Activity.objects.filter(verb="notifications.subscription.purge_stale").exists()
