"""Federation integration tests — real HTTP between Django and a mock peer.

These tests differ from the rest of the federation test suite by exercising
the *outbound* HTTP path through Python's real ``urllib.request`` stack
against a running HTTP server (``pytest-httpserver``), rather than mocking
``urllib.request.urlopen`` to a stub response. The fixture and the broader
strategy are documented in ``federation/tests/conftest.py`` and in the
ROADMAP item "Testing — federation integration test harness".

Phase 1 scope (what the mock peer can prove):
  - Real ``.well-known/`` fetch under the SSRF guard, size limit, and
    timeout enforcement.
  - URL parsing across the outbound stack.
  - Inbound JWT auth pipeline at the request layer: trust gating, signature
    verification, audience binding, JTI replay protection, key-rotation
    overlap acceptance.

What Phase 1 cannot prove (deferred to Phase 2 / Docker Compose):
  - TLS chain validation, SNI, certificate pinning.
  - Bidirectional flows where the mock peer needs to call back into the
    local Django (only one Django process exists in Phase 1).
  - The FUSE filesystem path (requires a real remote, not a stub).
  - Inter-instance clock-skew behaviour (single process = single clock).
"""

from __future__ import annotations

import uuid

import pytest
from django.contrib.contenttypes.models import ContentType
from model_bakery import baker

from epicurrents.models import AccessRight
from federation.auth import create_jwt, fetch_peer_public_key, generate_keypair, load_private_key

BASE = "/api/v1/federation"
LOCAL_URL = "https://local.example.com"


def _configure_local(settings):
    """Set the local instance's federation identity for the test scope."""
    pub, priv = generate_keypair()
    settings.FEDERATION_INSTANCE_URL = LOCAL_URL
    settings.FEDERATION_PUBLIC_KEY = pub
    settings.FEDERATION_PRIVATE_KEY = priv


def _make_recording_with_grant(mock_peer, make_user, *, remote_user_id="remote-user-1"):
    """Create a recording on the local instance with a federation grant to the peer.

    Returns ``(recording, content_type)`` so callers can build the URL.
    """
    from recordings.models import Recording

    owner = make_user(username=f"owner-{uuid.uuid4().hex[:8]}")
    rec = baker.make(Recording, author=owner, file_size=1, status=Recording.Status.READY)
    ct = ContentType.objects.get_for_model(rec, for_concrete_model=False)
    AccessRight.objects.create(
        content_type=ct,
        object_id=str(rec.pk),
        access_giver=owner,
        federated_peer=mock_peer.peer,
        remote_user_id=remote_user_id,
        can_read=True,
    )
    return rec, ct


@pytest.mark.django_db
class TestFetchPeerPublicKeyIntegration:
    """``fetch_peer_public_key`` against a real HTTP server.

    Complements the existing ``urlopen``-mocked tests in ``test_auth.py``
    by validating the real outbound network path — URL parsing, SSRF
    guard with ``FEDERATION_ALLOW_PRIVATE_PEER_URLS`` opt-in, response
    body decoding — none of which the mock-based tests exercise.
    """

    def test_returns_public_key_from_real_well_known_endpoint(self, mock_federated_peer):
        current, next_key = fetch_peer_public_key(mock_federated_peer.url)
        assert current == mock_federated_peer.public_key_b64
        assert next_key == ""  # mock peer publishes no rotation overlap


@pytest.mark.django_db
class TestInboundAuthIntegration:
    """End-to-end inbound auth via the ``inbound_check_object`` endpoint.

    Each test signs a token with the mock peer's key (or a forged key) and
    submits it through Django's real request pipeline. The endpoint exercises
    the full ``parse_federation_auth`` path — header parsing, signature
    verification, audience binding, replay detection, peer trust gating —
    plus the downstream access-right lookup.
    """

    def _url(self, ct, obj_id) -> str:
        return f"{BASE}/inbound/objects/{ct.pk}/{obj_id}/"

    def _request(self, client, url, token):
        return client.get(url, HTTP_AUTHORIZATION=f"FederatedBearer {token}")

    def test_valid_jwt_with_grant_returns_200(self, client, mock_federated_peer, make_user, settings):
        _configure_local(settings)
        rec, ct = _make_recording_with_grant(mock_federated_peer, make_user)

        token = mock_federated_peer.sign_jwt(audience=LOCAL_URL)
        resp = self._request(client, self._url(ct, rec.pk), token)

        assert resp.status_code == 200

    def test_tampered_signature_returns_401(self, client, mock_federated_peer, make_user, settings):
        _configure_local(settings)
        rec, ct = _make_recording_with_grant(mock_federated_peer, make_user)

        # Sign with a *different* private key — the registered peer.public_key
        # is the mock_federated_peer's; this signature will not verify.
        _, forged_priv_b64 = generate_keypair()
        forged_token = create_jwt(
            load_private_key(forged_priv_b64),
            issuer=mock_federated_peer.url,
            audience=LOCAL_URL,
            subject="remote-user-1",
        )
        resp = self._request(client, self._url(ct, rec.pk), forged_token)

        assert resp.status_code == 401

    def test_jti_replay_returns_401_on_second_use(
        self,
        client,
        mock_federated_peer,
        make_user,
        settings,
    ):
        _configure_local(settings)
        rec, ct = _make_recording_with_grant(mock_federated_peer, make_user)

        # Reuse the same JTI on both requests; the replay cache should reject
        # the second one even though the signature is still valid.
        replay_jti = uuid.uuid4().hex
        token = mock_federated_peer.sign_jwt(audience=LOCAL_URL, jti=replay_jti)

        first = self._request(client, self._url(ct, rec.pk), token)
        second = self._request(client, self._url(ct, rec.pk), token)

        assert first.status_code == 200
        assert second.status_code == 401

    def test_audience_mismatch_returns_401(
        self,
        client,
        mock_federated_peer,
        make_user,
        settings,
    ):
        _configure_local(settings)
        rec, ct = _make_recording_with_grant(mock_federated_peer, make_user)

        # Token's ``aud`` claim points at a different instance than the local
        # one — federation auth rejects this to prevent token reuse across
        # peers that happen to share a signing key.
        token = mock_federated_peer.sign_jwt(audience="https://elsewhere.example.com")
        resp = self._request(client, self._url(ct, rec.pk), token)

        assert resp.status_code == 401

    def test_rotation_overlap_accepts_next_key(
        self,
        client,
        mock_federated_peer,
        make_user,
        settings,
    ):
        _configure_local(settings)
        rec, ct = _make_recording_with_grant(mock_federated_peer, make_user)

        # Simulate a peer in the middle of a key rotation: the peer advertises
        # both ``public_key`` (current) and ``public_key_next`` (the new key
        # about to take over). Tokens signed by either key must verify during
        # the overlap window.
        next_pub_b64, next_priv_b64 = generate_keypair()
        mock_federated_peer.peer.public_key_next = next_pub_b64
        mock_federated_peer.peer.save()

        token_signed_with_next_key = create_jwt(
            load_private_key(next_priv_b64),
            issuer=mock_federated_peer.url,
            audience=LOCAL_URL,
            subject="remote-user-1",
        )
        resp = self._request(client, self._url(ct, rec.pk), token_signed_with_next_key)

        assert resp.status_code == 200

    def test_untrusted_peer_returns_401_even_with_valid_signature(
        self,
        client,
        mock_federated_peer,
        make_user,
        settings,
    ):
        _configure_local(settings)
        rec, ct = _make_recording_with_grant(mock_federated_peer, make_user)

        # Flip the trust bit *after* the grant is in place. The signature is
        # still valid against the registered public key, but the peer no longer
        # passes the trust gate, so the request must be rejected at the auth
        # layer — never reaches the access-right lookup.
        mock_federated_peer.peer.is_trusted = False
        mock_federated_peer.peer.save()

        token = mock_federated_peer.sign_jwt(audience=LOCAL_URL)
        resp = self._request(client, self._url(ct, rec.pk), token)

        assert resp.status_code == 401
