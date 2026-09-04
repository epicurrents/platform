"""Tests for federation.limits — per-peer download rate / byte quotas.

Covers the helper directly (sufficient for the byte-bucket math) plus one
end-to-end check on the recordings `download_recording` endpoint, where the
limits actually bite.  The wiring at `slice_recording` is identical to
`download_recording` so testing one is enough — the bug-prone part is the
helper, not the call site.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from model_bakery import baker

from federation.auth import generate_keypair
from federation.limits import (
    QuotaExceeded,
    check_peer_download_limits,
    check_peer_inbound_rate,
)
from federation.models import FederatedPeer


def _make_peer():
    pub, _ = generate_keypair()
    return baker.make(FederatedPeer, url="https://peer.example.com", public_key=pub, is_trusted=True)


# ---------------------------------------------------------------------------
# check_peer_download_limits — direct unit tests
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestByteQuota:
    def test_under_budget_passes(self, settings):
        settings.FEDERATION_PEER_DAILY_BYTE_LIMIT = 1024
        settings.FEDERATION_PEER_DOWNLOAD_RATE_LIMIT = 0
        peer = _make_peer()
        check_peer_download_limits(peer, expected_bytes=512)
        check_peer_download_limits(peer, expected_bytes=512)  # cumulative 1024 — at limit

    def test_over_budget_raises(self, settings):
        settings.FEDERATION_PEER_DAILY_BYTE_LIMIT = 1024
        settings.FEDERATION_PEER_DOWNLOAD_RATE_LIMIT = 0
        peer = _make_peer()
        check_peer_download_limits(peer, expected_bytes=1024)  # at limit
        with pytest.raises(QuotaExceeded, match="byte budget"):
            check_peer_download_limits(peer, expected_bytes=1)  # 1 byte over

    def test_zero_limit_disables_check(self, settings):
        settings.FEDERATION_PEER_DAILY_BYTE_LIMIT = 0
        settings.FEDERATION_PEER_DOWNLOAD_RATE_LIMIT = 0
        peer = _make_peer()
        # Multi-GB charge passes when limit is disabled.
        check_peer_download_limits(peer, expected_bytes=10 * 1024**3)

    def test_per_peer_isolation(self, settings):
        """A's usage doesn't bleed into B's quota."""
        settings.FEDERATION_PEER_DAILY_BYTE_LIMIT = 1024
        settings.FEDERATION_PEER_DOWNLOAD_RATE_LIMIT = 0
        peer_a = _make_peer()
        peer_b = baker.make(
            FederatedPeer,
            url="https://other.example.com",
            public_key=generate_keypair()[0],
            is_trusted=True,
        )
        check_peer_download_limits(peer_a, expected_bytes=1024)
        # B is still fresh and should not be blocked.
        check_peer_download_limits(peer_b, expected_bytes=1024)

    def test_no_charge_when_expected_bytes_zero(self, settings):
        """A 0-byte charge shouldn't consume budget (or initialize a bucket)."""
        settings.FEDERATION_PEER_DAILY_BYTE_LIMIT = 100
        settings.FEDERATION_PEER_DOWNLOAD_RATE_LIMIT = 0
        peer = _make_peer()
        check_peer_download_limits(peer, expected_bytes=0)
        # Now spend the full budget — should still work.
        check_peer_download_limits(peer, expected_bytes=100)


@pytest.mark.django_db
class TestRateLimit:
    def test_under_rate_limit_passes(self, settings):
        settings.FEDERATION_PEER_DAILY_BYTE_LIMIT = 0
        settings.FEDERATION_PEER_DOWNLOAD_RATE_LIMIT = 3
        peer = _make_peer()
        for _ in range(3):
            check_peer_download_limits(peer, expected_bytes=0)

    def test_over_rate_limit_raises(self, settings):
        settings.FEDERATION_PEER_DAILY_BYTE_LIMIT = 0
        settings.FEDERATION_PEER_DOWNLOAD_RATE_LIMIT = 2
        peer = _make_peer()
        check_peer_download_limits(peer, expected_bytes=0)
        check_peer_download_limits(peer, expected_bytes=0)
        with pytest.raises(QuotaExceeded, match="rate limit"):
            check_peer_download_limits(peer, expected_bytes=0)

    def test_zero_rate_limit_disables_check(self, settings):
        settings.FEDERATION_PEER_DAILY_BYTE_LIMIT = 0
        settings.FEDERATION_PEER_DOWNLOAD_RATE_LIMIT = 0
        peer = _make_peer()
        for _ in range(1000):
            check_peer_download_limits(peer, expected_bytes=0)


# ---------------------------------------------------------------------------
# Inbound rate limit on inbound_check_object
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestInboundRateLimit:
    def test_under_limit_passes(self, settings):
        settings.FEDERATION_PEER_INBOUND_RATE_LIMIT = 3
        peer = _make_peer()
        for _ in range(3):
            check_peer_inbound_rate(peer)

    def test_over_limit_raises(self, settings):
        settings.FEDERATION_PEER_INBOUND_RATE_LIMIT = 2
        peer = _make_peer()
        check_peer_inbound_rate(peer)
        check_peer_inbound_rate(peer)
        with pytest.raises(QuotaExceeded, match="inbound rate limit"):
            check_peer_inbound_rate(peer)

    def test_zero_disables_check(self, settings):
        settings.FEDERATION_PEER_INBOUND_RATE_LIMIT = 0
        peer = _make_peer()
        for _ in range(1000):
            check_peer_inbound_rate(peer)

    def test_distinct_counter_from_download_rate(self, settings):
        """Download rate and inbound rate use separate buckets."""
        settings.FEDERATION_PEER_DAILY_BYTE_LIMIT = 0
        settings.FEDERATION_PEER_DOWNLOAD_RATE_LIMIT = 1
        settings.FEDERATION_PEER_INBOUND_RATE_LIMIT = 1
        peer = _make_peer()
        # Hit download rate to its cap.
        check_peer_download_limits(peer, expected_bytes=0)
        with pytest.raises(QuotaExceeded, match="download rate"):
            check_peer_download_limits(peer, expected_bytes=0)
        # Inbound counter is independent — still has a slot.
        check_peer_inbound_rate(peer)
        with pytest.raises(QuotaExceeded, match="inbound rate"):
            check_peer_inbound_rate(peer)


# ---------------------------------------------------------------------------
# End-to-end on download_recording
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestDownloadEndpointLimits:
    """Confirms wiring at the recordings endpoint surfaces 429 on quota exhaustion.

    Uses the existing test pattern from `recordings/tests/test_federation.py`
    of mocking `_try_federated_auth` so we don't have to forge real JWTs;
    the limit check itself runs against the real cache.
    """

    def _make_recording(self, user, **kwargs):
        from recordings.models import Recording

        defaults = {
            "author": user,
            "original_name": "test.edf",
            "stored_name": "LIMITS123456789012345678901234AB.edf",
            "file_extension": ".edf",
            "file_size": 2 * 1024,  # 2 KiB so we can blow through small budgets
            "file_path": "/tmp/nonexistent.edf",
            "file_hash": "a" * 64,
            "content_hash": "b" * 64,
            "status": Recording.Status.READY,
        }
        defaults.update(kwargs)
        return Recording.objects.create(**defaults)

    def _grant(self, peer, recording, giver):
        from django.contrib.contenttypes.models import ContentType

        from epicurrents.models import AccessRight

        ct = ContentType.objects.get_for_model(recording, for_concrete_model=False)
        return AccessRight.objects.create(
            content_type=ct,
            object_id=str(recording.pk),
            access_giver=giver,
            federated_peer=peer,
            remote_user_id="remote-user-1",
            can_read=True,
        )

    def test_inbound_check_returns_429_when_rate_exhausted(self, client, user, settings):
        """End-to-end: inbound endpoint surfaces 429 from the rate limit."""
        from django.contrib.contenttypes.models import ContentType

        from federation.auth import create_jwt, load_private_key
        from federation.models import FederatedPeer
        from recordings.models import Recording

        settings.FEDERATION_PEER_INBOUND_RATE_LIMIT = 1
        settings.FEDERATION_INSTANCE_URL = "https://local.example.com"
        local_pub, local_priv = generate_keypair()
        settings.FEDERATION_PUBLIC_KEY = local_pub
        settings.FEDERATION_PRIVATE_KEY = local_priv

        peer_pub, peer_priv = generate_keypair()
        FederatedPeer.objects.create(
            url="https://peer.example.com",
            public_key=peer_pub,
            is_trusted=True,
        )
        rec = baker.make(Recording, author=user, file_size=1, status=Recording.Status.READY)
        ct = ContentType.objects.get_for_model(rec, for_concrete_model=False)

        def _make_token():
            return create_jwt(
                load_private_key(peer_priv),
                issuer="https://peer.example.com",
                audience="https://local.example.com",
                subject="user-1",
            )

        # First call — under limit, should reach 404 (no grant) cleanly.
        resp1 = client.get(
            f"/api/v1/federation/inbound/objects/{ct.pk}/{rec.pk}/",
            HTTP_AUTHORIZATION=f"FederatedBearer {_make_token()}",
        )
        assert resp1.status_code == 404, resp1.content
        # Second call — over rate limit, expect 429 before access check.
        resp2 = client.get(
            f"/api/v1/federation/inbound/objects/{ct.pk}/{rec.pk}/",
            HTTP_AUTHORIZATION=f"FederatedBearer {_make_token()}",
        )
        assert resp2.status_code == 429
        assert b"inbound rate" in resp2.content.lower()

    def test_download_returns_429_when_byte_budget_exhausted(self, client, user, settings):
        settings.FEDERATION_PEER_DAILY_BYTE_LIMIT = 1024  # 1 KiB — file is 2 KiB
        settings.FEDERATION_PEER_DOWNLOAD_RATE_LIMIT = 0
        peer = _make_peer()
        rec = self._make_recording(user)
        self._grant(peer, rec, user)

        with patch(
            "recordings.api.v1.ninja._try_federated_auth",
            return_value=(peer, "remote-user-1"),
        ):
            resp = client.get(f"/recordings/api/v1/{rec.stored_name.split('.')[0]}/file")
        assert resp.status_code == 429
        assert b"byte budget" in resp.content.lower() or "byte budget" in resp.json().get("detail", "")
