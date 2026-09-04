"""Real-DB integration tests for the consumer-side federation audit trail.

Companion to ``test_consumer_audit.py``. That file mocks the activity layer to
stay schema-independent; these exercise the **real** ``with_system_activity``
primitive and the **real** ``activity.Activity`` model end to end, so they lock
in the actual call signature and prove a row lands with the right
actor / verb / interface / target / metadata. Only the network boundary
(``urlopen`` + ``_make_jwt``) is mocked — the audit write is real.

Place at ``federation/tests/test_consumer_audit_integration.py``.

Mirrors the patch targets and payload/peer shape of the existing
``test_fuse_fs.py`` load_catalogue tests, but asserts the audit side effect the
older tests predate. Deliberately no ``require_fuse`` mark: the audit paths are
driven directly (``load_catalogue`` and ``_audit_first_read``), so fusepy is not
needed and these run anywhere the DB does.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from django.contrib.contenttypes.models import ContentType

from activity.models import Activity
from federation.fuse_fs import FederationOperations, _RecordingFile, load_catalogue
from federation.models import FederatedPeer

# --------------------------------------------------------------------------- #
# Helpers / fixtures                                                           #
# --------------------------------------------------------------------------- #


def _mock_urlopen(payload):
    resp = MagicMock()
    resp.read.return_value = json.dumps(payload).encode()
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def _recording_list_payload(n):
    """n non-EDF (meta=None) ready recordings — enough for the access count."""
    return [
        {
            "hash": f"{i:032X}",
            "file_extension": ".edf",
            "original_name": f"rec{i}.edf",
            "file_size": 1024,
            "status": "ready",
            "meta": None,
        }
        for i in range(1, n + 1)
    ]


@pytest.fixture()
def acting_user(make_user):
    # make_user is the project-level fixture the existing federation tests use
    # (see trusted_peer in test_fuse_fs.py); it honours the configured user model.
    return make_user()


@pytest.fixture()
def peer(db):
    return FederatedPeer.objects.create(
        url="https://neuro.example.com",
        display_name="Neuro",
        public_key="A" * 43,
        is_trusted=True,
    )


def _peer_ct():
    return ContentType.objects.get_for_model(FederatedPeer)


# --------------------------------------------------------------------------- #
# Enumeration record — load_catalogue                                          #
# --------------------------------------------------------------------------- #


@pytest.mark.django_db
def test_load_catalogue_writes_real_access_activity(acting_user, peer):
    with (
        patch("federation.fuse_fs._make_jwt", return_value="jwt"),
        patch("urllib.request.urlopen", return_value=_mock_urlopen(_recording_list_payload(3))),
    ):
        load_catalogue(str(acting_user.pk))

    rows = Activity.objects.filter(verb="federation.remote.access")
    assert rows.count() == 1
    row = rows.get()
    assert row.actor_id == acting_user.pk
    assert row.interface == Activity.Interface.COMMAND
    assert row.metadata.get("recording_count") == 3
    # target is the peer (identity via content_type + object_id).
    assert row.target_content_type_id == _peer_ct().id
    assert row.target_object_id == str(peer.pk)


@pytest.mark.django_db
def test_load_catalogue_unresolvable_user_records_unattributed(peer):
    """A local_user_id that resolves to no user still records the access, with a
    null actor — the audit is never silently dropped for a missing user."""
    with (
        patch("federation.fuse_fs._make_jwt", return_value="jwt"),
        patch("urllib.request.urlopen", return_value=_mock_urlopen(_recording_list_payload(1))),
    ):
        load_catalogue("2147483000")  # no such user

    row = Activity.objects.get(verb="federation.remote.access")
    assert row.actor_id is None
    assert row.target_object_id == str(peer.pk)


# --------------------------------------------------------------------------- #
# Per-recording read record — _audit_first_read                                #
# --------------------------------------------------------------------------- #


def _ops(acting_user):
    """A FederationOperations carrying only the audit state (no __init__/fuse)."""
    ops = object.__new__(FederationOperations)
    ops._acting_user = acting_user
    ops._read_audited = set()
    ops._peer_by_url = {}
    return ops


def _entry(peer_url, recording_hash):
    return _RecordingFile(
        slug="neuro.example.com",
        peer_url=peer_url,
        recording_hash=recording_hash,
        filename="patient001.edf",  # PII-ish original_name — must NOT reach metadata
        file_size=1024,
        header_size=0,
        is_edf=True,
    )


@pytest.mark.django_db
def test_first_read_writes_real_read_activity_and_dedups(acting_user, peer):
    ops = _ops(acting_user)
    entry = _entry(peer.url, "ABCDEF0123456789ABCDEF0123456789")

    ops._audit_first_read(entry)
    ops._audit_first_read(entry)  # continuation — deduped, no second row

    rows = Activity.objects.filter(verb="federation.remote.read")
    assert rows.count() == 1
    row = rows.get()
    assert row.actor_id == acting_user.pk
    assert row.interface == Activity.Interface.COMMAND
    # Code only: the hash, nothing else. No filename (PII), no peer_url (target).
    assert row.metadata == {"recording_hash": "ABCDEF0123456789ABCDEF0123456789"}
    # Peer resolved via _peer_for_url → target is the peer.
    assert row.target_content_type_id == _peer_ct().id
    assert row.target_object_id == str(peer.pk)


@pytest.mark.django_db
def test_first_read_with_unresolvable_peer_still_records(acting_user):
    """A peer_url matching no FederatedPeer still records the read (target null),
    so an audit gap never hides a genuine read."""
    ops = _ops(acting_user)
    entry = _entry("https://ghost.example", "DEADBEEFDEADBEEFDEADBEEFDEADBEEF")

    ops._audit_first_read(entry)

    row = Activity.objects.get(verb="federation.remote.read")
    assert row.actor_id == acting_user.pk
    assert row.target_content_type_id is None
    assert not row.target_object_id  # None or ""
    assert row.metadata == {"recording_hash": "DEADBEEFDEADBEEFDEADBEEFDEADBEEF"}
