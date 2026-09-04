"""Consumer-side federated-access audit trail.

Regression test for the audit *asymmetry* fix: the serving peer records the
inbound check in ``FederationAuditLog``, but the consumer (mounting) end left no
trail at all. ``load_catalogue`` must now emit one activity record per trusted
peer it enumerates, so "which of our users reached which peer, and how much did
they see" is answerable on the pulling side too.

Place at ``federation/tests/test_consumer_audit.py`` in the real tree.

These are deliberately schema-independent: the peers and the JWT/HTTP layer are
mocked, so the test exercises the audit wiring in ``fuse_fs`` without depending
on the ``FederatedPeer`` field layout or a live catalogue. A follow-up
integration test (real ``FederatedPeer`` rows + a real Activity write, asserting
``Activity.objects`` count) is worth adding once the DB fixtures are in reach; it
would also lock in the ``interface=COMMAND`` / ``actor`` / ``target`` shape end
to end.
"""

import json
from types import SimpleNamespace
from unittest import mock

from federation import fuse_fs


def _fake_urlopen(payload):
    """Return a context-manager stand-in whose .read() yields *payload* as JSON."""
    resp = mock.MagicMock()
    resp.read.return_value = json.dumps(payload).encode("utf-8")
    cm = mock.MagicMock()
    cm.__enter__.return_value = resp
    cm.__exit__.return_value = False
    return cm


def test_load_catalogue_records_one_access_per_peer():
    """Each trusted peer enumerated yields exactly one access record, carrying
    that peer's accessible-recording count."""
    peer_a = SimpleNamespace(url="https://peer-a.example")
    peer_b = SimpleNamespace(url="https://peer-b.example")

    catalogue = {
        "https://peer-a.example": [
            {"hash": "aaa", "file_extension": ".edf", "meta": {}},
            {"hash": "bbb", "file_extension": ".edf", "meta": {}},
        ],
        "https://peer-b.example": [
            {"hash": "ccc", "file_extension": ".edf", "meta": {}},
        ],
    }

    def fake_urlopen(req, timeout=None, context=None):
        for url, recs in catalogue.items():
            if req.full_url.startswith(url):
                return _fake_urlopen(recs)
        raise AssertionError(f"unexpected url {req.full_url}")

    with (
        mock.patch("federation.models.FederatedPeer") as FP,
        mock.patch("federation.fuse_fs._make_jwt", return_value="jwt-token"),
        mock.patch("federation.fuse_fs.urllib.request.urlopen", side_effect=fake_urlopen),
        mock.patch("federation.fuse_fs._record_federation_access") as rec,
        mock.patch("django.contrib.auth.get_user_model") as gum,
    ):
        FP.objects.filter.return_value = [peer_a, peer_b]
        acting = SimpleNamespace(pk=7)
        gum.return_value.objects.filter.return_value.first.return_value = acting

        fuse_fs.load_catalogue(local_user_id="7")

    # One record per peer, with the correct per-peer count and resolved actor.
    assert rec.call_count == 2
    by_peer = {c.args[1].url: c.args for c in rec.call_args_list}
    assert by_peer["https://peer-a.example"][0] is acting  # actor
    assert by_peer["https://peer-a.example"][2] == 2  # recording_count
    assert by_peer["https://peer-b.example"][2] == 1


def test_record_federation_access_uses_system_activity_command_interface():
    """The record is written through with_system_activity at COMMAND interface,
    with the peer as target and the count in metadata — matching the federation
    management commands' pattern rather than log_activity (no request here)."""
    peer = SimpleNamespace(url="https://peer-a.example")
    actor = SimpleNamespace(pk=7)

    fake_cm = mock.MagicMock()
    fake_cm.__enter__.return_value = None
    fake_cm.__exit__.return_value = False
    # Hold a direct reference to the mock and assert on it — do NOT re-import
    # activity.system_activity to reach it: `import a.b as c` binds c to the real
    # submodule via the parent package attribute, bypassing the sys.modules stub.
    fake_wsa = mock.MagicMock(return_value=fake_cm)
    fake_activity = SimpleNamespace(Interface=SimpleNamespace(COMMAND="command"))

    with mock.patch.dict(
        "sys.modules",
        {
            "activity.models": SimpleNamespace(Activity=fake_activity),
            "activity.system_activity": SimpleNamespace(with_system_activity=fake_wsa),
        },
    ):
        fuse_fs._record_federation_access(actor, peer, 2)

    fake_wsa.assert_called_once()
    args, kwargs = fake_wsa.call_args
    assert args[0] == "federation.remote.access"
    assert kwargs["interface"] == "command"
    assert kwargs["actor"] is actor
    assert kwargs["target"] is peer
    assert kwargs["metadata"] == {
        "peer_url": "https://peer-a.example",
        "recording_count": 2,
    }


def test_audit_failure_never_breaks_the_mount():
    """A failure in the audit path is swallowed — enumeration must not depend on
    the timeline write succeeding."""
    peer = SimpleNamespace(url="https://peer-a.example")

    with mock.patch.dict(
        "sys.modules",
        {
            "activity.models": SimpleNamespace(Activity=SimpleNamespace(Interface=SimpleNamespace(COMMAND="command"))),
            "activity.system_activity": SimpleNamespace(
                with_system_activity=mock.MagicMock(side_effect=RuntimeError("boom"))
            ),
        },
    ):
        # Must not raise.
        fuse_fs._record_federation_access(peer, peer, 1)


# --------------------------------------------------------------------------- #
# Per-recording read trail (Option B): one record per recording per mount,     #
# the consumer-side counterpart of the server's download/slice audit rows.     #
# --------------------------------------------------------------------------- #


def _mount_stub():
    """A FederationOperations instance with just the audit state, no FUSE/DB."""
    ops = fuse_fs.FederationOperations.__new__(fuse_fs.FederationOperations)
    ops._acting_user = SimpleNamespace(pk=7)
    ops._read_audited = set()
    ops._peer_by_url = {}
    return ops


def test_first_read_records_once_then_dedups():
    """The first read of a recording emits one record; subsequent reads of the
    same recording (its many range requests) emit nothing."""
    ops = _mount_stub()
    entry = SimpleNamespace(
        peer_url="https://peer-a.example",
        recording_hash="aaa",
        filename="rec-a.edf",
    )

    with (
        mock.patch.object(fuse_fs, "_record_federation_read") as rec,
        mock.patch.object(ops, "_peer_for_url", return_value="PEER"),
    ):
        ops._audit_first_read(entry)  # first range read
        ops._audit_first_read(entry)  # continuation
        ops._audit_first_read(entry)  # continuation

    rec.assert_called_once_with(ops._acting_user, "PEER", "https://peer-a.example", "aaa")


def test_distinct_recordings_each_record_once():
    """Dedup is per (peer, recording), so two different recordings each log."""
    ops = _mount_stub()
    a = SimpleNamespace(peer_url="https://peer-a.example", recording_hash="aaa", filename="a.edf")
    b = SimpleNamespace(peer_url="https://peer-a.example", recording_hash="bbb", filename="b.edf")

    with (
        mock.patch.object(fuse_fs, "_record_federation_read") as rec,
        mock.patch.object(ops, "_peer_for_url", return_value="PEER"),
    ):
        ops._audit_first_read(a)
        ops._audit_first_read(a)
        ops._audit_first_read(b)

    assert rec.call_count == 2
    assert {c.args[3] for c in rec.call_args_list} == {"aaa", "bbb"}


def test_read_record_uses_read_verb_and_names_the_recording():
    """The read record carries the read verb, COMMAND interface, peer as target,
    and identifies the specific recording in metadata."""
    peer = SimpleNamespace(url="https://peer-a.example")
    actor = SimpleNamespace(pk=7)
    fake_cm = mock.MagicMock()
    fake_cm.__enter__.return_value = None
    fake_cm.__exit__.return_value = False
    fake_wsa = mock.MagicMock(return_value=fake_cm)
    fake_activity = SimpleNamespace(Interface=SimpleNamespace(COMMAND="command"))

    with mock.patch.dict(
        "sys.modules",
        {
            "activity.models": SimpleNamespace(Activity=fake_activity),
            "activity.system_activity": SimpleNamespace(with_system_activity=fake_wsa),
        },
    ):
        fuse_fs._record_federation_read(actor, peer, "https://peer-a.example", "aaa")

    fake_wsa.assert_called_once()
    args, kwargs = fake_wsa.call_args
    assert args[0] == "federation.remote.read"
    assert kwargs["interface"] == "command"
    assert kwargs["actor"] is actor
    assert kwargs["target"] is peer
    # Code only: the recording hash, nothing else. No filename (PII), no
    # peer_url (already the target).
    assert kwargs["metadata"] == {"recording_hash": "aaa"}


def test_read_record_swallows_write_failure():
    """A failing timeline write inside the read record is swallowed — a read must
    never depend on the audit succeeding."""
    peer = SimpleNamespace(url="https://peer-a.example")
    with mock.patch.dict(
        "sys.modules",
        {
            "activity.models": SimpleNamespace(Activity=SimpleNamespace(Interface=SimpleNamespace(COMMAND="command"))),
            "activity.system_activity": SimpleNamespace(
                with_system_activity=mock.MagicMock(side_effect=RuntimeError("boom"))
            ),
        },
    ):
        # Must not raise.
        fuse_fs._record_federation_read(None, peer, "https://peer-a.example", "aaa")


def test_first_read_marks_before_write_so_no_retry():
    """Because _audit_first_read marks the recording before writing, even a write
    that raised would not be retried on the next read (dedup holds regardless of
    outcome)."""
    ops = _mount_stub()
    entry = SimpleNamespace(peer_url="https://peer-a.example", recording_hash="aaa", filename="a.edf")
    calls = {"n": 0}

    def raising_once(*a, **k):
        calls["n"] += 1
        raise RuntimeError("boom")

    with (
        mock.patch.object(fuse_fs, "_record_federation_read", side_effect=raising_once),
        mock.patch.object(ops, "_peer_for_url", return_value="PEER"),
    ):
        for _ in range(3):
            try:
                ops._audit_first_read(entry)
            except RuntimeError:
                pass

    assert calls["n"] == 1  # marked before write ⇒ attempted exactly once
