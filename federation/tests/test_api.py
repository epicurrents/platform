"""Tests for the federation REST API."""

import json
from unittest.mock import patch

import pytest
from django.contrib.contenttypes.models import ContentType
from model_bakery import baker

from epicurrents.models import AccessRight
from federation.auth import create_jwt, generate_keypair, load_private_key
from federation.models import FederatedPeer

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

BASE = "/api/v1/federation"


def _post(client, path, data):
    return client.post(f"{BASE}{path}", json.dumps(data), content_type="application/json")


def _patch(client, path, data):
    return client.patch(f"{BASE}{path}", json.dumps(data), content_type="application/json")


def _make_peer(url="https://peer.example.com", trusted=True, **kwargs):
    pub_b64, _ = generate_keypair()
    return baker.make(
        FederatedPeer,
        url=url,
        public_key=pub_b64,
        is_trusted=trusted,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Well-known endpoint
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestWellKnown:
    URL = "/.well-known/epicurrents-federation.json"

    def test_returns_404_when_not_configured(self, client):
        with patch("federation.views.is_federation_configured", return_value=False):
            resp = client.get(self.URL)
        assert resp.status_code == 404

    def test_returns_public_key_when_configured(self, client, settings):
        pub_b64, _ = generate_keypair()
        settings.FEDERATION_INSTANCE_URL = "https://this.example.com"
        settings.FEDERATION_PUBLIC_KEY = pub_b64
        settings.FEDERATION_PRIVATE_KEY = generate_keypair()[1]
        resp = client.get(self.URL)
        assert resp.status_code == 200
        data = resp.json()
        assert data["federation_public_key"] == pub_b64

    def test_only_get_allowed(self, client, settings):
        settings.FEDERATION_INSTANCE_URL = "https://this.example.com"
        pub_b64, priv_b64 = generate_keypair()
        settings.FEDERATION_PUBLIC_KEY = pub_b64
        settings.FEDERATION_PRIVATE_KEY = priv_b64
        resp = client.post(self.URL, "{}", content_type="application/json")
        assert resp.status_code == 405

    def test_omits_next_key_field_when_not_rotating(self, client, settings):
        """`federation_public_key_next` is only present during an overlap window."""
        pub_b64, priv_b64 = generate_keypair()
        settings.FEDERATION_INSTANCE_URL = "https://this.example.com"
        settings.FEDERATION_PUBLIC_KEY = pub_b64
        settings.FEDERATION_PRIVATE_KEY = priv_b64
        settings.FEDERATION_PUBLIC_KEY_NEXT = ""
        resp = client.get(self.URL)
        assert resp.status_code == 200
        assert "federation_public_key_next" not in resp.json()

    def test_includes_next_key_when_rotation_announced(self, client, settings):
        pub_b64, priv_b64 = generate_keypair()
        next_pub, _ = generate_keypair()
        settings.FEDERATION_INSTANCE_URL = "https://this.example.com"
        settings.FEDERATION_PUBLIC_KEY = pub_b64
        settings.FEDERATION_PRIVATE_KEY = priv_b64
        settings.FEDERATION_PUBLIC_KEY_NEXT = next_pub
        resp = client.get(self.URL)
        assert resp.status_code == 200
        data = resp.json()
        assert data["federation_public_key"] == pub_b64
        assert data["federation_public_key_next"] == next_pub


# ---------------------------------------------------------------------------
# Peer management
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestPeerList:
    def test_requires_superuser(self, auth_client):
        client, _ = auth_client
        resp = client.get(f"{BASE}/peers/")
        assert resp.status_code == 403

    def test_requires_auth(self, client):
        resp = client.get(f"{BASE}/peers/")
        assert resp.status_code == 401

    def test_superuser_can_list(self, superuser_client):
        client, _ = superuser_client
        _make_peer()
        resp = client.get(f"{BASE}/peers/")
        assert resp.status_code == 200
        assert len(resp.json()) >= 1


@pytest.mark.django_db
class TestPeerCreate:
    def test_requires_superuser(self, auth_client):
        client, _ = auth_client
        resp = _post(client, "/peers/", {"url": "https://peer.example.com"})
        assert resp.status_code == 403

    def test_creates_peer_with_fetched_key(self, superuser_client):
        client, _ = superuser_client
        pub_b64, _ = generate_keypair()
        with patch("federation.services.fetch_peer_public_key", return_value=(pub_b64, "")):
            resp = _post(
                client,
                "/peers/",
                {"url": "https://newpeer.example.com", "display_name": "New Peer"},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["url"] == "https://newpeer.example.com"
        assert data["public_key"] == pub_b64
        assert data["is_trusted"] is False  # starts untrusted

    def test_duplicate_url_returns_409(self, superuser_client):
        client, _ = superuser_client
        _make_peer(url="https://dup.example.com")
        pub_b64, _ = generate_keypair()
        with patch("federation.services.fetch_peer_public_key", return_value=(pub_b64, "")):
            resp = _post(client, "/peers/", {"url": "https://dup.example.com"})
        assert resp.status_code == 409

    def test_unreachable_peer_returns_502(self, superuser_client):
        client, _ = superuser_client
        with patch(
            "federation.services.fetch_peer_public_key",
            side_effect=ValueError("refused"),
        ):
            resp = _post(client, "/peers/", {"url": "https://down.example.com"})
        assert resp.status_code == 502


@pytest.mark.django_db
class TestPeerUpdate:
    def test_superuser_can_trust_peer(self, superuser_client):
        client, _ = superuser_client
        peer = _make_peer(trusted=False)
        resp = _patch(client, f"/peers/{peer.pk}/", {"is_trusted": True})
        assert resp.status_code == 200
        assert resp.json()["is_trusted"] is True

    def test_superuser_can_set_display_name(self, superuser_client):
        client, _ = superuser_client
        peer = _make_peer()
        resp = _patch(client, f"/peers/{peer.pk}/", {"display_name": "EEG Lab"})
        assert resp.status_code == 200
        assert resp.json()["display_name"] == "EEG Lab"

    def test_unknown_peer_returns_404(self, superuser_client):
        client, _ = superuser_client
        resp = _patch(client, "/peers/99999/", {"is_trusted": True})
        assert resp.status_code == 404


@pytest.mark.django_db
class TestPeerDelete:
    def test_superuser_can_delete_peer(self, superuser_client):
        client, _ = superuser_client
        peer = _make_peer()
        resp = client.delete(f"{BASE}/peers/{peer.pk}/")
        assert resp.status_code == 200
        assert not FederatedPeer.objects.filter(pk=peer.pk).exists()

    def test_requires_superuser(self, auth_client):
        client, _ = auth_client
        peer = _make_peer()
        resp = client.delete(f"{BASE}/peers/{peer.pk}/")
        assert resp.status_code == 403

    def test_cascade_deletes_federation_grants(self, superuser_client, make_user):
        """Deleting a peer CASCADEs to every `AccessRight` row for that peer.

        Orphaning the grants is not an option — the `CheckConstraint` on
        `AccessRight` requires exactly one of the four target slots to be set,
        and SET_NULL on `federated_peer` would violate that.  PROTECT was
        considered (forcing operators to revoke grants explicitly) but rejected
        as user-hostile for the common case.  CASCADE with audit-log capture
        (see ``test_cascade_writes_changelog_entries_for_each_grant``) is the
        chosen policy.
        """
        client, owner = superuser_client
        peer = _make_peer()
        from recordings.models import Recording

        rec = baker.make(Recording, author=owner, file_size=1, status=Recording.Status.READY)
        ct = ContentType.objects.get_for_model(rec, for_concrete_model=False)
        grant = AccessRight.objects.create(
            content_type=ct,
            object_id=str(rec.pk),
            access_giver=owner,
            federated_peer=peer,
            remote_user_id="remote-user-1",
            can_read=True,
        )

        resp = client.delete(f"{BASE}/peers/{peer.pk}/")
        assert resp.status_code == 200
        assert not FederatedPeer.objects.filter(pk=peer.pk).exists()
        assert not AccessRight.objects.filter(pk=grant.pk).exists()

    def test_cascade_writes_changelog_entries_for_each_grant(self, superuser_client, make_user):
        """Each cascaded grant produces an `ObjectChangeLog` row.

        The activity app's `pre_delete` signal fires for cascaded models, so
        the audit trail is preserved without explicit work in `delete_peer`
        — what compliance needs is the row showing "this grant was deleted
        by user X at time T", and that's what we get.
        """
        from activity.models import ObjectChangeLog
        from recordings.models import Recording

        client, owner = superuser_client
        peer = _make_peer()

        # Three grants for this peer to make sure they all get logged.
        recs = [
            baker.make(
                Recording,
                author=owner,
                file_size=1,
                status=Recording.Status.READY,
                stored_name=f"{prefix * 10}0123456789ABCD.edf",
            )
            for prefix in ("A", "B", "C")
        ]
        rec_ct = ContentType.objects.get_for_model(recs[0], for_concrete_model=False)
        for rec in recs:
            AccessRight.objects.create(
                content_type=rec_ct,
                object_id=str(rec.pk),
                access_giver=owner,
                federated_peer=peer,
                remote_user_id="",
                can_read=True,
            )

        ar_ct = ContentType.objects.get_for_model(AccessRight)
        before = ObjectChangeLog.objects.filter(content_type=ar_ct, action=ObjectChangeLog.ACTION_DELETE).count()

        resp = client.delete(f"{BASE}/peers/{peer.pk}/")
        assert resp.status_code == 200

        after = ObjectChangeLog.objects.filter(content_type=ar_ct, action=ObjectChangeLog.ACTION_DELETE).count()
        # One delete entry per cascaded grant.
        assert after - before == 3


@pytest.mark.django_db
class TestPeerRefreshKey:
    def test_refreshes_public_key(self, superuser_client):
        client, _ = superuser_client
        peer = _make_peer()
        new_pub, _ = generate_keypair()
        with patch("federation.services.fetch_peer_public_key", return_value=(new_pub, "")):
            resp = _post(client, f"/peers/{peer.pk}/refresh-key/", {})
        assert resp.status_code == 200
        assert resp.json()["public_key"] == new_pub

    def test_logs_warning_when_key_changes(self, superuser_client, caplog):
        """An unexpected key change is also how a MITM would manifest."""
        client, _ = superuser_client
        peer = _make_peer()
        old_key = peer.public_key
        new_pub, _ = generate_keypair()
        assert new_pub != old_key
        with patch("federation.services.fetch_peer_public_key", return_value=(new_pub, "")):
            with caplog.at_level("WARNING", logger="federation.api.v1.ninja"):
                resp = _post(client, f"/peers/{peer.pk}/refresh-key/", {})
        assert resp.status_code == 200
        matching = [r for r in caplog.records if "key changed on refresh" in r.getMessage()]
        assert len(matching) == 1
        assert peer.url in matching[0].getMessage()

    def test_no_warning_when_key_unchanged(self, superuser_client, caplog):
        client, _ = superuser_client
        peer = _make_peer()
        with (
            patch(
                "federation.services.fetch_peer_public_key",
                return_value=(peer.public_key, ""),
            ),
            caplog.at_level("WARNING", logger="federation.api.v1.ninja"),
        ):
            resp = _post(client, f"/peers/{peer.pk}/refresh-key/", {})
        assert resp.status_code == 200
        assert not [r for r in caplog.records if "key changed on refresh" in r.getMessage()]


# ---------------------------------------------------------------------------
# Grant management
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestGrantCreate:
    def _make_recording(self, owner):
        from recordings.models import Recording

        return baker.make(Recording, author=owner, file_size=1, status=Recording.Status.READY)

    def test_author_can_create_grant(self, user, auth_client):
        client, author = auth_client
        rec = self._make_recording(author)
        peer = _make_peer()
        ct = ContentType.objects.get_for_model(rec, for_concrete_model=False)

        resp = _post(
            client,
            "/grants/",
            {
                "federated_peer_id": peer.pk,
                "remote_user_id": "remote-user-1",
                "content_type_id": ct.pk,
                "object_id": str(rec.pk),
                "can_read": True,
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["federated_peer_id"] == peer.pk
        assert data["remote_user_id"] == "remote-user-1"
        assert data["can_read"] is True

    def test_duplicate_grant_for_same_peer_and_remote_user_returns_409(self, user, auth_client):
        client, author = auth_client
        rec = self._make_recording(author)
        peer = _make_peer()
        ct = ContentType.objects.get_for_model(rec, for_concrete_model=False)
        payload = {
            "federated_peer_id": peer.pk,
            "remote_user_id": "remote-user-1",
            "content_type_id": ct.pk,
            "object_id": str(rec.pk),
            "can_read": True,
        }
        assert _post(client, "/grants/", payload).status_code == 200
        resp = _post(client, "/grants/", payload)
        assert resp.status_code == 409
        assert "already exists" in resp.json()["detail"]
        # A different remote user on the same peer and object is a new grant.
        resp = _post(client, "/grants/", {**payload, "remote_user_id": "remote-user-2"})
        assert resp.status_code == 200

    def test_non_author_without_share_right_is_denied(self, make_user, auth_client):
        client, other = auth_client
        owner = make_user(username="owner")
        rec = self._make_recording(owner)
        peer = _make_peer()
        ct = ContentType.objects.get_for_model(rec, for_concrete_model=False)

        resp = _post(
            client,
            "/grants/",
            {
                "federated_peer_id": peer.pk,
                "remote_user_id": "",
                "content_type_id": ct.pk,
                "object_id": str(rec.pk),
                "can_read": True,
            },
        )
        assert resp.status_code == 403

    def test_unknown_peer_returns_404(self, auth_client, make_user):
        client, author = auth_client
        rec = self._make_recording(author)
        ct = ContentType.objects.get_for_model(rec, for_concrete_model=False)

        resp = _post(
            client,
            "/grants/",
            {
                "federated_peer_id": 99999,
                "remote_user_id": "",
                "content_type_id": ct.pk,
                "object_id": str(rec.pk),
                "can_read": True,
            },
        )
        assert resp.status_code == 404

    def test_requires_auth(self, client):
        # Send a body that passes schema validation so the auth check is reached.
        resp = _post(
            client,
            "/grants/",
            {
                "federated_peer_id": 1,
                "content_type_id": 1,
                "object_id": "1",
                "can_read": True,
            },
        )
        assert resp.status_code == 401


@pytest.mark.django_db
class TestGrantList:
    def test_lists_own_grants(self, user, auth_client):
        client, u = auth_client
        peer = _make_peer()
        from recordings.models import Recording

        rec = baker.make(Recording, author=u, file_size=1, status=Recording.Status.READY)
        ct = ContentType.objects.get_for_model(rec, for_concrete_model=False)
        AccessRight.objects.create(
            content_type=ct,
            object_id=str(rec.pk),
            access_giver=u,
            federated_peer=peer,
            remote_user_id="",
            can_read=True,
        )
        resp = client.get(f"{BASE}/grants/")
        assert resp.status_code == 200
        assert len(resp.json()) == 1

    def test_requires_auth(self, client):
        resp = client.get(f"{BASE}/grants/")
        assert resp.status_code == 401


@pytest.mark.django_db
class TestGrantDelete:
    def test_giver_can_revoke(self, auth_client, user):
        client, u = auth_client
        peer = _make_peer()
        from recordings.models import Recording

        rec = baker.make(Recording, author=u, file_size=1, status=Recording.Status.READY)
        ct = ContentType.objects.get_for_model(rec, for_concrete_model=False)
        grant = AccessRight.objects.create(
            content_type=ct,
            object_id=str(rec.pk),
            access_giver=u,
            federated_peer=peer,
            remote_user_id="",
            can_read=True,
        )
        resp = client.delete(f"{BASE}/grants/{grant.pk}/")
        assert resp.status_code == 200
        assert not AccessRight.objects.filter(pk=grant.pk).exists()

    def test_non_giver_cannot_revoke(self, auth_client, make_user):
        client, other = auth_client
        owner = make_user(username="owner")
        peer = _make_peer()
        from recordings.models import Recording

        rec = baker.make(Recording, author=owner, file_size=1, status=Recording.Status.READY)
        ct = ContentType.objects.get_for_model(rec, for_concrete_model=False)
        grant = AccessRight.objects.create(
            content_type=ct,
            object_id=str(rec.pk),
            access_giver=owner,
            federated_peer=peer,
            remote_user_id="",
            can_read=True,
        )
        resp = client.delete(f"{BASE}/grants/{grant.pk}/")
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Inbound endpoint
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestInboundCheckObject:
    """Tests for GET /api/v1/federation/inbound/objects/{ct_id}/{object_id}/"""

    def _make_jwt(
        self,
        peer_url: str,
        priv_b64: str,
        audience: str,
        subject: str = "remote-user-1",
    ) -> str:
        priv = load_private_key(priv_b64)
        return create_jwt(priv, issuer=peer_url, audience=audience, subject=subject)

    def _auth_header(self, token: str) -> dict:
        return {"HTTP_AUTHORIZATION": f"FederatedBearer {token}"}

    def test_returns_200_when_access_granted(self, client, make_user, settings):
        local_pub, local_priv = generate_keypair()
        settings.FEDERATION_INSTANCE_URL = "https://local.example.com"
        settings.FEDERATION_PUBLIC_KEY = local_pub
        settings.FEDERATION_PRIVATE_KEY = local_priv

        peer_pub, peer_priv = generate_keypair()
        peer = _make_peer(url="https://peer.example.com", trusted=True)
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

        token = self._make_jwt(
            peer_url="https://peer.example.com",
            priv_b64=peer_priv,
            audience="https://local.example.com",
            subject="remote-user-1",
        )
        resp = client.get(
            f"{BASE}/inbound/objects/{ct.pk}/{rec.pk}/",
            **self._auth_header(token),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["object_id"] == str(rec.pk)

    def test_returns_404_when_no_grant(self, client, make_user, settings):
        """No grant returns 404, not 403 — see ``test_response_identical_for_missing_and_unauthorized``."""
        local_pub, local_priv = generate_keypair()
        settings.FEDERATION_INSTANCE_URL = "https://local.example.com"
        settings.FEDERATION_PUBLIC_KEY = local_pub
        settings.FEDERATION_PRIVATE_KEY = local_priv

        peer_pub, peer_priv = generate_keypair()
        peer = _make_peer(url="https://peer.example.com", trusted=True)
        peer.public_key = peer_pub
        peer.save()

        owner = make_user(username="owner")
        from recordings.models import Recording

        rec = baker.make(Recording, author=owner, file_size=1, status=Recording.Status.READY)
        ct = ContentType.objects.get_for_model(rec, for_concrete_model=False)
        # No AccessRight created.

        token = self._make_jwt(
            peer_url="https://peer.example.com",
            priv_b64=peer_priv,
            audience="https://local.example.com",
        )
        resp = client.get(
            f"{BASE}/inbound/objects/{ct.pk}/{rec.pk}/",
            **self._auth_header(token),
        )
        assert resp.status_code == 404

    def test_response_identical_for_missing_and_unauthorized(self, client, make_user, settings):
        """A peer must not be able to tell "doesn't exist" from "no access".

        Returning distinct responses for the two cases leaks the existence of
        PHI-adjacent records to remote peers (they can probe object IDs and
        learn which ones exist).  The endpoint deliberately collapses both to
        the same 404 + same body; this test locks the invariant in so a
        future refactor that adds a different message in one branch will
        fail loudly.
        """
        local_pub, local_priv = generate_keypair()
        settings.FEDERATION_INSTANCE_URL = "https://local.example.com"
        settings.FEDERATION_PUBLIC_KEY = local_pub
        settings.FEDERATION_PRIVATE_KEY = local_priv

        peer_pub, peer_priv = generate_keypair()
        peer = _make_peer(url="https://peer.example.com", trusted=True)
        peer.public_key = peer_pub
        peer.save()

        owner = make_user(username="owner")
        from recordings.models import Recording

        # Case A: object exists, no grant → "unauthorized" branch.
        rec = baker.make(Recording, author=owner, file_size=1, status=Recording.Status.READY)
        ct = ContentType.objects.get_for_model(rec, for_concrete_model=False)

        # Two distinct tokens so each request carries a fresh ``jti`` and the
        # replay-detection nonce cache does not reject the second call.
        token_a = self._make_jwt(
            peer_url="https://peer.example.com",
            priv_b64=peer_priv,
            audience="https://local.example.com",
        )
        token_b = self._make_jwt(
            peer_url="https://peer.example.com",
            priv_b64=peer_priv,
            audience="https://local.example.com",
        )
        resp_unauth = client.get(
            f"{BASE}/inbound/objects/{ct.pk}/{rec.pk}/",
            **self._auth_header(token_a),
        )
        # Case B: same CT, object id that does not exist → "missing" branch.
        resp_missing = client.get(
            f"{BASE}/inbound/objects/{ct.pk}/{rec.pk + 999_999}/",
            **self._auth_header(token_b),
        )

        assert resp_unauth.status_code == 404
        assert resp_missing.status_code == 404
        assert resp_unauth.content == resp_missing.content

    def test_returns_401_without_auth(self, client, settings):
        settings.FEDERATION_INSTANCE_URL = "https://local.example.com"
        pub, priv = generate_keypair()
        settings.FEDERATION_PUBLIC_KEY = pub
        settings.FEDERATION_PRIVATE_KEY = priv
        resp = client.get(f"{BASE}/inbound/objects/1/1/")
        assert resp.status_code == 401

    def test_returns_401_for_untrusted_peer(self, client, make_user, settings):
        local_pub, local_priv = generate_keypair()
        settings.FEDERATION_INSTANCE_URL = "https://local.example.com"
        settings.FEDERATION_PUBLIC_KEY = local_pub
        settings.FEDERATION_PRIVATE_KEY = local_priv

        peer_pub, peer_priv = generate_keypair()
        peer = _make_peer(url="https://untrusted.example.com", trusted=False)
        peer.public_key = peer_pub
        peer.save()

        token = self._make_jwt(
            peer_url="https://untrusted.example.com",
            priv_b64=peer_priv,
            audience="https://local.example.com",
        )
        resp = client.get(
            f"{BASE}/inbound/objects/1/1/",
            **self._auth_header(token),
        )
        assert resp.status_code == 401

    def test_wildcard_remote_user_grants_any_user(self, client, make_user, settings):
        """A grant with remote_user_id='' grants access to any authenticated peer user."""
        local_pub, local_priv = generate_keypair()
        settings.FEDERATION_INSTANCE_URL = "https://local.example.com"
        settings.FEDERATION_PUBLIC_KEY = local_pub
        settings.FEDERATION_PRIVATE_KEY = local_priv

        peer_pub, peer_priv = generate_keypair()
        peer = _make_peer(url="https://peer.example.com", trusted=True)
        peer.public_key = peer_pub
        peer.save()

        owner = make_user(username="owner")
        from recordings.models import Recording

        rec = baker.make(Recording, author=owner, file_size=1, status=Recording.Status.READY)
        ct = ContentType.objects.get_for_model(rec, for_concrete_model=False)

        # Wildcard grant (any user from this peer).
        AccessRight.objects.create(
            content_type=ct,
            object_id=str(rec.pk),
            access_giver=owner,
            federated_peer=peer,
            remote_user_id="",  # wildcard
            can_read=True,
        )

        token = self._make_jwt(
            peer_url="https://peer.example.com",
            priv_b64=peer_priv,
            audience="https://local.example.com",
            subject="any-random-user",
        )
        resp = client.get(
            f"{BASE}/inbound/objects/{ct.pk}/{rec.pk}/",
            **self._auth_header(token),
        )
        assert resp.status_code == 200

    def test_failed_recording_indistinguishable_from_missing(self, client, make_user, settings):
        """FAILED recordings collapse into the same 404 + body as missing / no-grant.

        Same indistinguishability requirement as
        ``test_response_identical_for_missing_and_unauthorized`` — a peer
        must not be able to learn that a recording exists-but-failed by
        comparing responses.
        """
        local_pub, local_priv = generate_keypair()
        settings.FEDERATION_INSTANCE_URL = "https://local.example.com"
        settings.FEDERATION_PUBLIC_KEY = local_pub
        settings.FEDERATION_PRIVATE_KEY = local_priv

        peer_pub, peer_priv = generate_keypair()
        peer = _make_peer(url="https://peer.example.com", trusted=True)
        peer.public_key = peer_pub
        peer.save()

        owner = make_user(username="owner")
        from recordings.models import Recording

        # FAILED recording with a valid grant — should be treated as missing.
        rec = baker.make(Recording, author=owner, file_size=1, status=Recording.Status.FAILED)
        ct = ContentType.objects.get_for_model(rec, for_concrete_model=False)
        AccessRight.objects.create(
            content_type=ct,
            object_id=str(rec.pk),
            access_giver=owner,
            federated_peer=peer,
            remote_user_id="remote-user-1",
            can_read=True,
        )

        token_failed = self._make_jwt(
            peer_url="https://peer.example.com",
            priv_b64=peer_priv,
            audience="https://local.example.com",
            subject="remote-user-1",
        )
        token_missing = self._make_jwt(
            peer_url="https://peer.example.com",
            priv_b64=peer_priv,
            audience="https://local.example.com",
            subject="remote-user-1",
        )

        resp_failed = client.get(
            f"{BASE}/inbound/objects/{ct.pk}/{rec.pk}/",
            **self._auth_header(token_failed),
        )
        resp_missing = client.get(
            f"{BASE}/inbound/objects/{ct.pk}/{rec.pk + 999_999}/",
            **self._auth_header(token_missing),
        )

        assert resp_failed.status_code == 404
        assert resp_missing.status_code == 404
        assert resp_failed.content == resp_missing.content


@pytest.mark.django_db
class TestFederationAuditTrail:
    """Activity-row annotation contract for the federation API.

    One representative test per endpoint, locking the verb + target +
    metadata shape so a future regression that drops the annotation
    surfaces here rather than in a SIEM rule months later.
    """

    def test_peer_list_records_federation_peer_list(self, superuser_client):
        from activity.models import Activity

        client, _ = superuser_client
        _make_peer(url="https://p1.example.com")
        _make_peer(url="https://p2.example.com")
        resp = client.get(f"{BASE}/peers/")
        assert resp.status_code == 200

        activity = Activity.objects.filter(verb="federation.peer.list").latest("created_at")
        assert activity.metadata["returned_count"] >= 2

    def test_peer_create_records_federation_peer_create(self, superuser_client):
        from activity.models import Activity

        client, _ = superuser_client
        pub_b64, _ = generate_keypair()
        with patch(
            "federation.services.fetch_peer_public_key",
            return_value=(pub_b64, ""),
        ):
            resp = _post(client, "/peers/", {"url": "https://newaudit.example.com"})
        assert resp.status_code == 200
        peer_pk = resp.json()["id"]

        activity = Activity.objects.filter(verb="federation.peer.create").latest("created_at")
        peer_ct = ContentType.objects.get_for_model(FederatedPeer)
        assert activity.target_content_type_id == peer_ct.pk
        assert activity.target_object_id == str(peer_pk)

    def test_peer_read_records_federation_peer_read(self, superuser_client):
        from activity.models import Activity

        client, _ = superuser_client
        peer = _make_peer()
        resp = client.get(f"{BASE}/peers/{peer.pk}/")
        assert resp.status_code == 200

        activity = Activity.objects.filter(verb="federation.peer.read").latest("created_at")
        peer_ct = ContentType.objects.get_for_model(FederatedPeer)
        assert activity.target_content_type_id == peer_ct.pk
        assert activity.target_object_id == str(peer.pk)

    def test_peer_update_records_federation_peer_update(self, superuser_client):
        from activity.models import Activity

        client, _ = superuser_client
        peer = _make_peer(trusted=False)
        resp = _patch(client, f"/peers/{peer.pk}/", {"is_trusted": True})
        assert resp.status_code == 200

        activity = Activity.objects.filter(verb="federation.peer.update").latest("created_at")
        peer_ct = ContentType.objects.get_for_model(FederatedPeer)
        assert activity.target_content_type_id == peer_ct.pk
        assert activity.target_object_id == str(peer.pk)
        assert activity.metadata["fields_updated"] == ["is_trusted"]

    def test_peer_delete_records_federation_peer_delete(self, superuser_client):
        from activity.models import Activity

        client, _ = superuser_client
        peer = _make_peer(url="https://delaudit.example.com")
        peer_pk = peer.pk
        resp = client.delete(f"{BASE}/peers/{peer_pk}/")
        assert resp.status_code == 200

        activity = Activity.objects.filter(verb="federation.peer.delete").latest("created_at")
        peer_ct = ContentType.objects.get_for_model(FederatedPeer)
        # log_activity runs BEFORE delete() so the pk is preserved on the
        # Activity row; the full deleted state is reconstructable via the
        # linked ObjectChangeLog row.
        assert activity.target_content_type_id == peer_ct.pk
        assert activity.target_object_id == str(peer_pk)

    def test_peer_refresh_key_records_federation_peer_refresh_key(self, superuser_client):
        from activity.models import Activity

        client, _ = superuser_client
        peer = _make_peer()
        new_pub, _ = generate_keypair()
        with patch(
            "federation.services.fetch_peer_public_key",
            return_value=(new_pub, ""),
        ):
            resp = _post(client, f"/peers/{peer.pk}/refresh-key/", {})
        assert resp.status_code == 200

        activity = Activity.objects.filter(verb="federation.peer.refresh_key").latest("created_at")
        peer_ct = ContentType.objects.get_for_model(FederatedPeer)
        assert activity.target_content_type_id == peer_ct.pk
        assert activity.target_object_id == str(peer.pk)
        assert activity.metadata["key_changed"] is True

    def test_grant_list_records_federation_grant_list(self, auth_client):
        from activity.models import Activity

        client, _ = auth_client
        resp = client.get(f"{BASE}/grants/")
        assert resp.status_code == 200

        activity = Activity.objects.filter(verb="federation.grant.list").latest("created_at")
        assert "returned_count" in activity.metadata

    def test_grant_create_records_federation_grant_create(self, auth_client):
        from activity.models import Activity
        from recordings.models import Recording

        client, author = auth_client
        rec = baker.make(Recording, author=author, file_size=1, status=Recording.Status.READY)
        peer = _make_peer()
        ct = ContentType.objects.get_for_model(rec, for_concrete_model=False)
        resp = _post(
            client,
            "/grants/",
            {
                "federated_peer_id": peer.pk,
                "remote_user_id": "remote-user-1",
                "content_type_id": ct.pk,
                "object_id": str(rec.pk),
                "can_read": True,
            },
        )
        assert resp.status_code == 200
        grant_pk = resp.json()["id"]

        activity = Activity.objects.filter(verb="federation.grant.create").latest("created_at")
        access_right_ct = ContentType.objects.get_for_model(AccessRight, for_concrete_model=False)
        assert activity.target_content_type_id == access_right_ct.pk
        assert activity.target_object_id == str(grant_pk)

    def test_grant_revoke_records_federation_grant_revoke(self, auth_client):
        from activity.models import Activity
        from recordings.models import Recording

        client, author = auth_client
        rec = baker.make(Recording, author=author, file_size=1, status=Recording.Status.READY)
        peer = _make_peer()
        ct = ContentType.objects.get_for_model(rec, for_concrete_model=False)
        grant = AccessRight.objects.create(
            content_type=ct,
            object_id=str(rec.pk),
            access_giver=author,
            federated_peer=peer,
            remote_user_id="",
            can_read=True,
        )
        grant_pk = grant.pk
        resp = client.delete(f"{BASE}/grants/{grant_pk}/")
        assert resp.status_code == 200

        activity = Activity.objects.filter(verb="federation.grant.revoke").latest("created_at")
        access_right_ct = ContentType.objects.get_for_model(AccessRight, for_concrete_model=False)
        assert activity.target_content_type_id == access_right_ct.pk
        assert activity.target_object_id == str(grant_pk)

    def test_inbound_probe_records_federation_inbound_probe(self, client, make_user, settings):
        """Successful probe sets target=obj and the per-peer metadata."""
        from activity.models import Activity

        local_pub, local_priv = generate_keypair()
        settings.FEDERATION_INSTANCE_URL = "https://local.example.com"
        settings.FEDERATION_PUBLIC_KEY = local_pub
        settings.FEDERATION_PRIVATE_KEY = local_priv

        peer_pub, peer_priv = generate_keypair()
        peer = _make_peer(url="https://peer.example.com", trusted=True)
        peer.public_key = peer_pub
        peer.save()

        owner = make_user(username="audit_owner")
        from recordings.models import Recording

        rec = baker.make(Recording, author=owner, file_size=1, status=Recording.Status.READY)
        rec_ct = ContentType.objects.get_for_model(rec, for_concrete_model=False)
        AccessRight.objects.create(
            content_type=rec_ct,
            object_id=str(rec.pk),
            access_giver=owner,
            federated_peer=peer,
            remote_user_id="remote-user-1",
            can_read=True,
        )

        priv = load_private_key(peer_priv)
        token = create_jwt(
            priv,
            issuer="https://peer.example.com",
            audience="https://local.example.com",
            subject="remote-user-1",
        )
        resp = client.get(
            f"{BASE}/inbound/objects/{rec_ct.pk}/{rec.pk}/",
            HTTP_AUTHORIZATION=f"FederatedBearer {token}",
        )
        assert resp.status_code == 200

        activity = Activity.objects.filter(verb="federation.inbound.probe").latest("created_at")
        # On the success path target_* (set via target=obj) carries the
        # probed identifiers; metadata holds only peer-side context.
        assert activity.target_content_type_id == rec_ct.pk
        assert activity.target_object_id == str(rec.pk)
        assert activity.metadata["peer_id"] == peer.pk
        assert activity.metadata["peer_url"] == "https://peer.example.com"
        assert activity.metadata["remote_user_id"] == "remote-user-1"
        assert "probed_content_type_id" not in activity.metadata
        assert "probed_object_id" not in activity.metadata

    def test_inbound_probe_denied_still_records_verb(self, client, make_user, settings):
        """A denied probe (no grant) still carries the verb on its Activity row.

        The verb is set up front so the audit trail can distinguish
        federation-inbound probes from other failed reads.
        """
        from activity.models import Activity

        local_pub, local_priv = generate_keypair()
        settings.FEDERATION_INSTANCE_URL = "https://local.example.com"
        settings.FEDERATION_PUBLIC_KEY = local_pub
        settings.FEDERATION_PRIVATE_KEY = local_priv

        peer_pub, peer_priv = generate_keypair()
        peer = _make_peer(url="https://peer.example.com", trusted=True)
        peer.public_key = peer_pub
        peer.save()

        owner = make_user(username="deny_owner")
        from recordings.models import Recording

        rec = baker.make(Recording, author=owner, file_size=1, status=Recording.Status.READY)
        rec_ct = ContentType.objects.get_for_model(rec, for_concrete_model=False)
        # No AccessRight — probe will deny.

        priv = load_private_key(peer_priv)
        token = create_jwt(
            priv,
            issuer="https://peer.example.com",
            audience="https://local.example.com",
            subject="remote-user-1",
        )
        resp = client.get(
            f"{BASE}/inbound/objects/{rec_ct.pk}/{rec.pk}/",
            HTTP_AUTHORIZATION=f"FederatedBearer {token}",
        )
        assert resp.status_code == 404

        activity = Activity.objects.filter(verb="federation.inbound.probe").latest("created_at")
        assert activity.metadata["peer_id"] == peer.pk
        assert activity.metadata["probed_content_type_id"] == rec_ct.pk
        assert activity.metadata["probed_object_id"] == str(rec.pk)
