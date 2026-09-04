"""Tests for the federation management commands (peer + grant lifecycle, check_peer).

The commands wrap ``federation.services``; these tests cover the CLI surface —
argument handling, the fingerprint-verified trust gate, the grant-renewal path,
and that every write opens an audited COMMAND-interface scope. The underlying
service behaviour is additionally exercised through the API tests.
"""

import io
from datetime import timedelta

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone
from model_bakery import baker

from activity.models import Activity
from epicurrents.models import AccessRight
from federation.auth import generate_keypair
from federation.models import FederatedPeer
from federation.services import key_fingerprint

pytestmark = pytest.mark.django_db

PEER_URL = "https://peer.example.com"


def _run(cmd, **kwargs):
    out = io.StringIO()
    call_command(cmd, stdout=out, stderr=io.StringIO(), **kwargs)
    return out.getvalue()


@pytest.fixture
def peer_key():
    pub_b64, _ = generate_keypair()
    return pub_b64


@pytest.fixture
def fetch_ok(monkeypatch, peer_key):
    """Stub the peer-key fetch in both the services and check_peer namespaces."""

    def _fetch(url, **kwargs):
        return peer_key, ""

    monkeypatch.setattr("federation.services.fetch_peer_public_key", _fetch)
    monkeypatch.setattr("federation.management.commands.federation_check_peer.fetch_peer_public_key", _fetch)
    return peer_key


@pytest.fixture
def trusted_peer(peer_key):
    return FederatedPeer.objects.create(url=PEER_URL, public_key=peer_key, is_trusted=True)


# --- peer lifecycle ---------------------------------------------------------


def test_add_peer_creates_untrusted_prints_fingerprint_and_audits(fetch_ok):
    out = _run("federation_add_peer", url=PEER_URL, display_name="Peer")
    peer = FederatedPeer.objects.get(url=PEER_URL)
    assert peer.is_trusted is False
    assert peer.public_key == fetch_ok
    assert key_fingerprint(fetch_ok) in out
    assert Activity.objects.filter(interface=Activity.Interface.COMMAND, verb="federation.peer.create").exists()


def test_trust_peer_matching_fingerprint(fetch_ok):
    _run("federation_add_peer", url=PEER_URL)
    peer = FederatedPeer.objects.get(url=PEER_URL)
    _run("federation_trust_peer", peer=str(peer.pk), fingerprint=key_fingerprint(fetch_ok))
    peer.refresh_from_db()
    assert peer.is_trusted is True


def test_trust_peer_wrong_fingerprint_refuses(fetch_ok):
    _run("federation_add_peer", url=PEER_URL)
    peer = FederatedPeer.objects.get(url=PEER_URL)
    with pytest.raises(CommandError):
        _run("federation_trust_peer", peer=str(peer.pk), fingerprint="deadbeef")
    peer.refresh_from_db()
    assert peer.is_trusted is False


def test_untrust_peer(trusted_peer):
    _run("federation_trust_peer", peer=trusted_peer.url, untrust=True)
    trusted_peer.refresh_from_db()
    assert trusted_peer.is_trusted is False


def test_refresh_peer_key_updates_key(trusted_peer, monkeypatch):
    new_pub, _ = generate_keypair()
    monkeypatch.setattr("federation.services.fetch_peer_public_key", lambda url, **kw: (new_pub, ""))
    _run("federation_refresh_peer_key", peer=str(trusted_peer.pk))
    trusted_peer.refresh_from_db()
    assert trusted_peer.public_key == new_pub


# --- grant lifecycle --------------------------------------------------------


def _recording(author, content_hash):
    return baker.make("recordings.Recording", content_hash=content_hash, author=author)


def test_grant_recording_and_revoke(trusted_peer, make_superuser):
    giver = make_superuser(username="fedadmin")
    _recording(giver, "hash-abc")
    _run("federation_grant", peer=str(trusted_peer.pk), giver="fedadmin", recording="hash-abc")

    grant = AccessRight.objects.get(federated_peer=trusted_peer)
    assert grant.can_read is True
    assert grant.access_giver == giver
    assert Activity.objects.filter(interface=Activity.Interface.COMMAND, verb="federation.grant.create").exists()

    _run("federation_revoke_grant", grant_id=grant.pk)
    assert not AccessRight.objects.filter(pk=grant.pk).exists()


def test_grant_wildcard_and_flags(trusted_peer, make_superuser):
    giver = make_superuser(username="fedadmin3")
    _recording(giver, "hash-flags")
    _run(
        "federation_grant",
        peer=str(trusted_peer.pk),
        giver="fedadmin3",
        recording="hash-flags",
        share=True,
        apply_middleware=True,
    )
    grant = AccessRight.objects.get(federated_peer=trusted_peer)
    assert grant.remote_user_id == ""  # wildcard
    assert grant.can_share is True
    assert grant.apply_middleware is True


def test_grant_defaults_to_deidentified_serving(trusted_peer, make_superuser):
    # Fail-safe contract: a federated grant created without an explicit
    # apply_middleware choice serves de-identified bytes. Raw serving to
    # another controller must be a deliberate opt-out.
    giver = make_superuser(username="fedadmin4")
    _recording(giver, "hash-default-mw")
    _run("federation_grant", peer=str(trusted_peer.pk), giver="fedadmin4", recording="hash-default-mw")
    grant = AccessRight.objects.get(federated_peer=trusted_peer)
    assert grant.apply_middleware is True


def test_grant_explicit_raw_optout(trusted_peer, make_superuser):
    giver = make_superuser(username="fedadmin5")
    _recording(giver, "hash-raw-mw")
    _run(
        "federation_grant",
        peer=str(trusted_peer.pk),
        giver="fedadmin5",
        recording="hash-raw-mw",
        apply_middleware=False,
    )
    grant = AccessRight.objects.get(federated_peer=trusted_peer)
    assert grant.apply_middleware is False


def test_renew_grant_sets_and_clears_expiry(trusted_peer, make_superuser):
    giver = make_superuser(username="fedadmin2")
    _recording(giver, "hash-xyz")
    _run("federation_grant", peer=str(trusted_peer.pk), giver="fedadmin2", recording="hash-xyz")
    grant = AccessRight.objects.get(federated_peer=trusted_peer)

    when = (timezone.now() + timedelta(days=30)).isoformat()
    _run("federation_renew_grant", grant_id=grant.pk, expires=when)
    grant.refresh_from_db()
    assert grant.expires_at is not None

    _run("federation_renew_grant", grant_id=grant.pk, no_expiry=True)
    grant.refresh_from_db()
    assert grant.expires_at is None


def test_grant_unknown_recording_errors(trusted_peer, make_superuser):
    make_superuser(username="fedadmin4")
    with pytest.raises(CommandError):
        _run("federation_grant", peer=str(trusted_peer.pk), giver="fedadmin4", recording="nope")


# --- listings + check_peer --------------------------------------------------


def test_list_peers(trusted_peer):
    out = _run("federation_list_peers")
    assert trusted_peer.url in out
    assert "trusted" in out


def test_list_grants(trusted_peer, make_superuser):
    giver = make_superuser(username="fedadmin5")
    _recording(giver, "hash-list")
    _run("federation_grant", peer=str(trusted_peer.pk), giver="fedadmin5", recording="hash-list")
    out = _run("federation_list_grants")
    assert trusted_peer.url in out


def test_check_peer_reachability_no_probe(trusted_peer, fetch_ok):
    out = _run("federation_check_peer", peer=str(trusted_peer.pk), no_probe=True)
    assert "well-known reachable" in out
    assert key_fingerprint(fetch_ok) in out
    assert "key matches the registered key" in out
