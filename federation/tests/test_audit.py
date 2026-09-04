"""Tests for the federation audit log.

Covers the helper (``federation.audit.log_federation_access``), the wiring at
``inbound_check_object``, and the wiring at the federated branches of the six
recordings endpoints.  Peer-deletion behaviour is also verified here so the
SET_NULL contract on ``FederationAuditLog.peer`` is load-bearing.
"""

from __future__ import annotations

import pytest
from django.contrib.contenttypes.models import ContentType
from model_bakery import baker

from epicurrents.models import AccessRight
from federation.audit import log_federation_access
from federation.auth import create_jwt, generate_keypair, load_private_key
from federation.models import FederatedPeer, FederationAuditLog

BASE = "/api/v1/federation"


def _make_peer(url="https://peer.example.com", trusted=True):
    pub_b64, _ = generate_keypair()
    return baker.make(FederatedPeer, url=url, public_key=pub_b64, is_trusted=trusted)


def _configure_local(settings):
    pub, priv = generate_keypair()
    settings.FEDERATION_INSTANCE_URL = "https://local.example.com"
    settings.FEDERATION_PUBLIC_KEY = pub
    settings.FEDERATION_PRIVATE_KEY = priv


def _make_jwt(peer_url, peer_priv_b64, audience, subject="remote-user-1"):
    return create_jwt(
        load_private_key(peer_priv_b64),
        issuer=peer_url,
        audience=audience,
        subject=subject,
    )


# ---------------------------------------------------------------------------
# log_federation_access helper
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestLogFederationAccess:
    def test_writes_row_with_target(self):
        peer = _make_peer()
        from recordings.models import Recording

        rec = baker.make(Recording, file_size=1, status=Recording.Status.READY)
        log_federation_access(
            peer=peer,
            remote_user_id="user-7",
            action="download_recording",
            target=rec,
            status_code=200,
        )
        row = FederationAuditLog.objects.get()
        assert row.peer_id == peer.pk
        assert row.peer_url == peer.url
        assert row.remote_user_id == "user-7"
        assert row.action == "download_recording"
        assert row.status_code == 200
        assert row.target_object_id == str(rec.pk)
        assert row.target == rec

    def test_writes_row_with_path_params_when_no_target(self):
        """The probe-detection fallback: log raw path params when no model was resolved."""
        peer = _make_peer()
        from recordings.models import Recording

        rec_ct = ContentType.objects.get_for_model(Recording, for_concrete_model=False)
        log_federation_access(
            peer=peer,
            remote_user_id="user-7",
            action="inbound_check_object",
            target_content_type_id=rec_ct.pk,
            target_object_id="999999",
            status_code=404,
        )
        row = FederationAuditLog.objects.get()
        assert row.target_content_type_id == rec_ct.pk
        assert row.target_object_id == "999999"
        assert row.status_code == 404

    def test_writes_row_with_no_target_for_list_actions(self):
        peer = _make_peer()
        log_federation_access(
            peer=peer,
            remote_user_id="user-7",
            action="list_recordings",
            target=None,
            status_code=200,
        )
        row = FederationAuditLog.objects.get()
        assert row.target_content_type is None
        assert row.target_object_id == ""

    def test_peer_delete_preserves_row_via_set_null(self):
        peer = _make_peer()
        log_federation_access(
            peer=peer,
            remote_user_id="user-7",
            action="list_recordings",
            status_code=200,
        )
        peer_url = peer.url
        peer.delete()
        row = FederationAuditLog.objects.get()
        assert row.peer is None
        assert row.peer_url == peer_url  # denormalised field survives


# ---------------------------------------------------------------------------
# inbound_check_object wiring
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestInboundCheckObjectAuditing:
    def _request(self, client, token, ct_id, object_id):
        return client.get(
            f"{BASE}/inbound/objects/{ct_id}/{object_id}/",
            HTTP_AUTHORIZATION=f"FederatedBearer {token}",
        )

    def test_grant_writes_200_row_with_target(self, client, make_user, settings):
        _configure_local(settings)
        peer_pub, peer_priv = generate_keypair()
        peer = _make_peer(url="https://peer.example.com")
        peer.public_key = peer_pub
        peer.save()
        owner = make_user(username="owner")
        from recordings.models import Recording

        rec = baker.make(Recording, author=owner, file_size=1, status=Recording.Status.READY)
        ct = ContentType.objects.get_for_model(rec, for_concrete_model=False)
        AccessRight.objects.create(
            content_type=ct,
            object_id=str(rec.pk),
            access_giver=owner,
            federated_peer=peer,
            remote_user_id="remote-user-1",
            can_read=True,
        )
        token = _make_jwt("https://peer.example.com", peer_priv, "https://local.example.com")
        resp = self._request(client, token, ct.pk, rec.pk)
        assert resp.status_code == 200
        row = FederationAuditLog.objects.get()
        assert row.action == "inbound_check_object"
        assert row.status_code == 200
        assert row.target == rec
        assert row.remote_user_id == "remote-user-1"

    def test_denial_of_existing_object_writes_404_row_with_target(self, client, make_user, settings):
        _configure_local(settings)
        peer_pub, peer_priv = generate_keypair()
        peer = _make_peer(url="https://peer.example.com")
        peer.public_key = peer_pub
        peer.save()
        owner = make_user(username="owner")
        from recordings.models import Recording

        rec = baker.make(Recording, author=owner, file_size=1, status=Recording.Status.READY)
        ct = ContentType.objects.get_for_model(rec, for_concrete_model=False)
        # No grant.
        token = _make_jwt("https://peer.example.com", peer_priv, "https://local.example.com")
        resp = self._request(client, token, ct.pk, rec.pk)
        assert resp.status_code == 404
        row = FederationAuditLog.objects.get()
        assert row.action == "inbound_check_object"
        assert row.status_code == 404
        # Critical for forensics: even though the peer-facing response is
        # indistinguishable from a missing-object response, the audit row
        # records the actual object PK so we can tell denial from probe.
        assert row.target == rec

    def test_probe_of_missing_object_writes_404_row_with_path_params(self, client, settings):
        _configure_local(settings)
        peer_pub, peer_priv = generate_keypair()
        peer = _make_peer(url="https://peer.example.com")
        peer.public_key = peer_pub
        peer.save()
        from recordings.models import Recording

        rec_ct = ContentType.objects.get_for_model(Recording, for_concrete_model=False)
        token = _make_jwt("https://peer.example.com", peer_priv, "https://local.example.com")
        # PK that does not exist.
        resp = self._request(client, token, rec_ct.pk, 999_999)
        assert resp.status_code == 404
        row = FederationAuditLog.objects.get()
        assert row.action == "inbound_check_object"
        assert row.status_code == 404
        # No model instance was resolved; the probed identifier is preserved
        # via the path-param fallback so probe sweeps don't all collapse into
        # identical empty-target rows.
        assert row.target_content_type_id == rec_ct.pk
        assert row.target_object_id == "999999"
        assert row.target is None  # generic FK resolves to None for missing pk

    def test_no_audit_row_on_auth_failure(self, client, settings):
        """Auth failures (bad token, untrusted peer) are intentionally not audited.

        Compliance question is "what did successfully-authenticated peers
        access".  Bad-token noise is in the Django logs, not here.
        """
        _configure_local(settings)
        resp = client.get(
            f"{BASE}/inbound/objects/1/1/",
            HTTP_AUTHORIZATION="FederatedBearer not.a.jwt",
        )
        assert resp.status_code == 401
        assert FederationAuditLog.objects.count() == 0
