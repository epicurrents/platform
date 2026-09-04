"""Tests for the notifications API — vapid-public-key, subscribe, unsubscribe."""

import pytest
from django.test import override_settings

from conftest import delete_json, post_json
from notifications.models import PushSubscription

VAPID_URL = "/api/v1/notifications/vapid-public-key"
# POST /subscribe → subscribe; DELETE /subscribe → unsubscribe (same path, different method).
SUBSCRIBE_URL = "/api/v1/notifications/subscribe"

SAMPLE_SUBSCRIPTION = {
    "endpoint": "https://push.example.com/sub/abc123",
    "p256dh": "BNsample_p256dh_key",
    "auth": "sample_auth_secret",
}


@pytest.mark.django_db
class TestVapidPublicKey:
    @override_settings(WEBPUSH_VAPID_PUBLIC_KEY="test-public-key")
    def test_returns_public_key(self, client):
        resp = client.get(VAPID_URL)
        assert resp.status_code == 200
        assert resp.json()["vapid_public_key"] == "test-public-key"

    @override_settings(WEBPUSH_VAPID_PUBLIC_KEY="")
    def test_returns_empty_string_when_not_configured(self, client):
        resp = client.get(VAPID_URL)
        assert resp.status_code == 200
        assert resp.json()["vapid_public_key"] == ""

    def test_unauthenticated_access_allowed(self, client):
        """VAPID public key is intentionally public."""
        resp = client.get(VAPID_URL)
        assert resp.status_code == 200


@pytest.mark.django_db
class TestSubscribeEndpoint:
    def test_unauthenticated_returns_401(self, client):
        resp = post_json(client, SUBSCRIBE_URL, SAMPLE_SUBSCRIPTION)
        assert resp.status_code == 401

    def test_creates_subscription(self, auth_client):
        c, user = auth_client
        resp = post_json(c, SUBSCRIBE_URL, SAMPLE_SUBSCRIPTION)
        assert resp.status_code == 200
        assert PushSubscription.objects.filter(user=user, endpoint=SAMPLE_SUBSCRIPTION["endpoint"]).exists()

    def test_upserts_existing_endpoint(self, auth_client):
        c, user = auth_client
        PushSubscription.objects.create(
            user=user,
            endpoint=SAMPLE_SUBSCRIPTION["endpoint"],
            p256dh="old_key",
            auth="old_auth",
        )
        updated = {**SAMPLE_SUBSCRIPTION, "p256dh": "new_key", "auth": "new_auth"}
        resp = post_json(c, SUBSCRIBE_URL, updated)
        assert resp.status_code == 200
        sub = PushSubscription.objects.get(endpoint=SAMPLE_SUBSCRIPTION["endpoint"])
        assert sub.p256dh == "new_key"
        assert sub.auth == "new_auth"
        assert PushSubscription.objects.filter(endpoint=SAMPLE_SUBSCRIPTION["endpoint"]).count() == 1

    def test_second_user_cannot_take_over_endpoint(self, client, make_user):
        """Registering another user's endpoint is acknowledged but changes nothing.

        The response must not reveal that the endpoint is already known, and
        the existing row (owner and keys) must stay intact — reassignment
        would let an attacker silence the victim's notifications and rebind
        the row's keys.
        """
        user1 = make_user(username="user1")
        user2 = make_user(username="user2")
        client.force_login(user1)
        post_json(client, SUBSCRIBE_URL, SAMPLE_SUBSCRIPTION)
        client.force_login(user2)
        takeover = {**SAMPLE_SUBSCRIPTION, "p256dh": "evil_key", "auth": "evil_auth"}
        resp = post_json(client, SUBSCRIBE_URL, takeover)
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}
        sub = PushSubscription.objects.get(endpoint=SAMPLE_SUBSCRIPTION["endpoint"])
        assert sub.user == user1
        assert sub.p256dh == SAMPLE_SUBSCRIPTION["p256dh"]
        assert sub.auth == SAMPLE_SUBSCRIPTION["auth"]
        assert PushSubscription.objects.filter(user=user2).count() == 0


@pytest.mark.django_db
class TestSubscribeEndpointValidation:
    """SSRF guard on the stored endpoint URL.

    ``send_push_to_user`` makes an outbound request to the endpoint, so the
    subscribe path must refuse URLs targeting internal services. The default
    test settings allow private URLs (see test_platform.py); these tests
    disable the override to exercise the real guard. IP-literal URLs keep
    the guard's hostname resolution offline.
    """

    def _subscribe(self, client, endpoint):
        return post_json(
            client,
            SUBSCRIBE_URL,
            {"endpoint": endpoint, "p256dh": "key", "auth": "secret"},
        )

    def test_rejects_http_endpoint(self, auth_client, settings):
        settings.FEDERATION_ALLOW_PRIVATE_PEER_URLS = False
        c, user = auth_client
        resp = self._subscribe(c, "http://push.example.com/sub/abc")
        assert resp.status_code == 400
        assert not PushSubscription.objects.filter(user=user).exists()

    def test_rejects_loopback_endpoint(self, auth_client, settings):
        settings.FEDERATION_ALLOW_PRIVATE_PEER_URLS = False
        c, user = auth_client
        resp = self._subscribe(c, "https://127.0.0.1/internal")
        assert resp.status_code == 400
        assert not PushSubscription.objects.filter(user=user).exists()

    def test_rejects_private_range_endpoint(self, auth_client, settings):
        settings.FEDERATION_ALLOW_PRIVATE_PEER_URLS = False
        c, user = auth_client
        for addr in ("10.0.0.1", "172.16.0.1", "192.168.1.1"):
            resp = self._subscribe(c, f"https://{addr}/x")
            assert resp.status_code == 400
        assert not PushSubscription.objects.filter(user=user).exists()

    def test_rejects_cloud_metadata_endpoint(self, auth_client, settings):
        settings.FEDERATION_ALLOW_PRIVATE_PEER_URLS = False
        c, user = auth_client
        resp = self._subscribe(c, "https://169.254.169.254/latest/meta-data/")
        assert resp.status_code == 400
        assert not PushSubscription.objects.filter(user=user).exists()

    def test_dev_override_allows_private_endpoint(self, auth_client, settings):
        settings.FEDERATION_ALLOW_PRIVATE_PEER_URLS = True
        c, user = auth_client
        resp = self._subscribe(c, "http://localhost:9000/dev-push")
        assert resp.status_code == 200
        assert PushSubscription.objects.filter(user=user).exists()


@pytest.mark.django_db
class TestUnsubscribeEndpoint:
    def test_unauthenticated_returns_401(self, client):
        resp = delete_json(client, SUBSCRIBE_URL, {"endpoint": "https://push.example.com/x"})
        assert resp.status_code == 401

    def test_removes_own_subscription(self, auth_client):
        c, user = auth_client
        PushSubscription.objects.create(
            user=user,
            endpoint=SAMPLE_SUBSCRIPTION["endpoint"],
            p256dh="key",
            auth="auth",
        )
        resp = delete_json(c, SUBSCRIBE_URL, {"endpoint": SAMPLE_SUBSCRIPTION["endpoint"]})
        assert resp.status_code == 200
        assert not PushSubscription.objects.filter(user=user, endpoint=SAMPLE_SUBSCRIPTION["endpoint"]).exists()

    def test_cannot_remove_other_users_subscription(self, client, user, make_user):
        other = make_user(username="other")
        PushSubscription.objects.create(
            user=user,
            endpoint=SAMPLE_SUBSCRIPTION["endpoint"],
            p256dh="key",
            auth="auth",
        )
        client.force_login(other)
        delete_json(client, SUBSCRIBE_URL, {"endpoint": SAMPLE_SUBSCRIPTION["endpoint"]})
        # Subscription for original user must still exist
        assert PushSubscription.objects.filter(user=user, endpoint=SAMPLE_SUBSCRIPTION["endpoint"]).exists()

    def test_removing_nonexistent_endpoint_is_idempotent(self, auth_client):
        c, _ = auth_client
        resp = delete_json(c, SUBSCRIBE_URL, {"endpoint": "https://nonexistent.example.com/sub"})
        assert resp.status_code == 200


@pytest.mark.django_db
class TestNotificationsAuditTrail:
    """Activity-row annotation contract for the notifications API.

    Companion to the platform-wide audit-trail backfill (ROADMAP:
    *Activity — restore audit-trail coverage on reads + non-recordings
    /library writes*). One representative test per write endpoint —
    these lock the verb and target shape so a future regression that
    drops the annotation gets caught here instead of being noticed by
    a SIEM rule months later.

    The third endpoint in this app, ``GET /vapid-public-key``, is on
    ``ACTIVITY_PATH_SKIP_LIST`` and intentionally produces no row;
    that policy is enforced by the middleware contract tests in
    ``epicurrents/tests/``.
    """

    def test_subscribe_records_create_activity(self, auth_client):
        from activity.models import Activity

        c, user = auth_client
        resp = post_json(c, SUBSCRIBE_URL, SAMPLE_SUBSCRIPTION)
        assert resp.status_code == 200

        activity = Activity.objects.latest("created_at")
        subscription = PushSubscription.objects.get(user=user, endpoint=SAMPLE_SUBSCRIPTION["endpoint"])
        assert activity.verb == "notifications.subscription.create"
        assert activity.target_object_id == str(subscription.pk)
        assert activity.metadata["upserted"] is False

    def test_subscribe_marks_upsert_when_endpoint_already_exists(self, auth_client):
        from activity.models import Activity

        c, user = auth_client
        # First subscribe → creates the row.
        post_json(c, SUBSCRIBE_URL, SAMPLE_SUBSCRIPTION)
        # Second with the same endpoint → upsert path.
        post_json(c, SUBSCRIBE_URL, SAMPLE_SUBSCRIPTION)

        activity = Activity.objects.filter(verb="notifications.subscription.create").latest("created_at")
        assert activity.metadata["upserted"] is True

    def test_unsubscribe_records_delete_activity(self, auth_client):
        from activity.models import Activity

        c, user = auth_client
        subscription = PushSubscription.objects.create(
            user=user,
            endpoint=SAMPLE_SUBSCRIPTION["endpoint"],
            p256dh=SAMPLE_SUBSCRIPTION["p256dh"],
            auth=SAMPLE_SUBSCRIPTION["auth"],
        )
        subscription_pk = subscription.pk

        resp = delete_json(c, SUBSCRIBE_URL, {"endpoint": SAMPLE_SUBSCRIPTION["endpoint"]})
        assert resp.status_code == 200

        activity = Activity.objects.latest("created_at")
        assert activity.verb == "notifications.subscription.delete"
        assert activity.target_object_id == str(subscription_pk)
        assert activity.metadata["row_existed"] is True

    def test_unsubscribe_records_row_existed_false_when_idempotent(self, auth_client):
        """Unsubscribe is idempotent — confirm the audit row reflects that
        the call had no row to delete."""
        from activity.models import Activity

        c, _ = auth_client
        resp = delete_json(c, SUBSCRIBE_URL, {"endpoint": "https://nonexistent.example.com/sub"})
        assert resp.status_code == 200

        activity = Activity.objects.latest("created_at")
        assert activity.verb == "notifications.subscription.delete"
        assert activity.target_object_id == ""
        assert activity.metadata["row_existed"] is False
