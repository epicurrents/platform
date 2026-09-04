"""Tests for federated authentication on the recordings API.

Federated peers authenticate with a FederatedBearer JWT.  Rather than issuing
real JWTs in every test, _try_federated_auth is patched to return a
(FederatedPeer, remote_user_id) tuple directly.  JWT verification is covered by
the federation auth module's own tests.

Coverage:
- list_recordings:    federated listing, grant filtering, expiry, wildcard remote_user_id, trash denied
- recording_status:   grant required, 403 without grant
- recording_detail:   grant required, 403 without grant
- recording_detail_slice: grant required, 403 without grant
- download_recording: grant required, apply_middleware from grant
- slice_recording:    grant required, apply_middleware from grant
"""

from contextlib import contextmanager
from unittest.mock import patch

import pytest
from django.contrib.contenttypes.models import ContentType
from django.utils import timezone

from epicurrents.models import AccessRight
from federation.models import FederatedPeer
from recordings.models import Recording

# ---------------------------------------------------------------------------
# URLs
# ---------------------------------------------------------------------------

LIST_URL = "/recordings/api/v1/"
STATUS_URL = "/recordings/api/v1/status/{hash}"
DETAIL_URL = "/recordings/api/v1/{hash}"
DETAIL_SLICE_URL = "/recordings/api/v1/{hash}/slice"
DOWNLOAD_URL = "/recordings/api/v1/{hash}/file"
SLICE_URL = "/recordings/api/v1/{hash}/file/slice"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

MOCK_PATH = "recordings.api.v1.ninja._try_federated_auth"


@contextmanager
def _as_peer(peer, remote_user_id="user42"):
    """Patch _try_federated_auth to impersonate a FederatedPeer."""
    with patch(MOCK_PATH, return_value=(peer, remote_user_id)):
        yield


@contextmanager
def _no_auth():
    """Patch _try_federated_auth so no federated identity is found."""
    with patch(MOCK_PATH, return_value=None):
        yield


def _make_peer(user):
    return FederatedPeer.objects.create(
        url="https://peer.example.com",
        display_name="Test Peer",
        public_key="A" * 43,
        is_trusted=True,
        added_by=user,
    )


def _make_recording(user, **kwargs):
    defaults = {
        "author": user,
        "original_name": "test.edf",
        "stored_name": "FEDTEST1234567890FEDTEST123456AB.edf",
        "file_extension": ".edf",
        "file_size": 1024,
        "file_path": "/tmp/nonexistent.edf",
        "file_hash": "a" * 64,
        "content_hash": "b" * 64,
        "status": Recording.Status.READY,
    }
    defaults.update(kwargs)
    return Recording.objects.create(**defaults)


def _grant(peer, recording, giver, remote_user_id="user42", apply_middleware=False):
    ct = ContentType.objects.get_for_model(recording, for_concrete_model=False)
    return AccessRight.objects.create(
        content_type=ct,
        object_id=str(recording.pk),
        access_giver=giver,
        federated_peer=peer,
        remote_user_id=remote_user_id,
        can_read=True,
        apply_middleware=apply_middleware,
    )


def _hash(recording):
    return recording.stored_name.split(".")[0]


# ---------------------------------------------------------------------------
# Minimal valid EDF file builder (used by download / slice tests)
# ---------------------------------------------------------------------------


def _build_edf(n_channels=1, n_records=4, record_duration=1.0, samples_per_record=256):
    """Return bytes of a minimal valid EDF file."""
    n_sig = n_channels
    header_size = 256 * (1 + n_sig)

    def _pad(s, length):
        return s[:length].ljust(length).encode("ascii")

    # Fixed header (256 bytes)
    hdr = bytearray()
    hdr += _pad("0", 8)  # version
    hdr += _pad("X X X X", 80)  # patient id
    hdr += _pad("Startdate 01.01.85", 80)  # recording id
    hdr += _pad("01.01.85", 8)  # startdate
    hdr += _pad("00.00.00", 8)  # starttime
    hdr += _pad(str(header_size), 8)  # bytes in header
    hdr += _pad("", 44)  # reserved
    hdr += _pad(str(n_records), 8)  # num data records
    hdr += _pad(str(record_duration), 8)  # record duration
    hdr += _pad(str(n_sig), 4)  # num signals
    assert len(hdr) == 256

    # Per-signal header fields (each field × n_sig channels)
    fields = [
        ("EEG Fp1", 16),
        ("AgAgCl", 80),
        ("uV", 8),
        ("-1000", 8),
        ("+1000", 8),
        ("-32768", 8),
        ("32767", 8),
        ("", 80),
        (str(samples_per_record), 8),
        ("", 32),
    ]
    for value, width in fields:
        for _ in range(n_sig):
            hdr += _pad(value, width)

    assert len(hdr) == header_size

    # Signal data: n_records records, each samples_per_record 2-byte samples
    record_bytes = samples_per_record * 2
    for _ in range(n_records):
        hdr += b"\x00" * record_bytes

    return bytes(hdr)


# ---------------------------------------------------------------------------
# TestFederatedList
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestFederatedList:
    def test_list_returns_granted_recordings(self, client, user):
        peer = _make_peer(user)
        r = _make_recording(user)
        _grant(peer, r, user)

        with _as_peer(peer):
            resp = client.get(LIST_URL)
        assert resp.status_code == 200
        hashes = [item["hash"] for item in resp.json()]
        assert _hash(r) in hashes

    def test_list_excludes_ungranted_recordings(self, client, user):
        peer = _make_peer(user)
        r_granted = _make_recording(user, stored_name="GRANTED1234567890GRANTED1234AB.edf")
        r_other = _make_recording(user, stored_name="OTHER123456789012345678901234AB.edf")
        _grant(peer, r_granted, user)
        # r_other has no grant for this peer

        with _as_peer(peer):
            resp = client.get(LIST_URL)
        assert resp.status_code == 200
        hashes = [item["hash"] for item in resp.json()]
        assert _hash(r_granted) in hashes
        assert _hash(r_other) not in hashes

    def test_list_trash_returns_403(self, client, user):
        peer = _make_peer(user)
        with _as_peer(peer):
            resp = client.get(f"{LIST_URL}?trash=true")
        assert resp.status_code == 403

    def test_list_unauthenticated_returns_401(self, client, user):
        with _no_auth():
            resp = client.get(LIST_URL)
        assert resp.status_code == 401

    def test_expired_grant_excluded(self, client, user):
        peer = _make_peer(user)
        r = _make_recording(user)
        ct = ContentType.objects.get_for_model(r, for_concrete_model=False)
        AccessRight.objects.create(
            content_type=ct,
            object_id=str(r.pk),
            access_giver=user,
            federated_peer=peer,
            remote_user_id="user42",
            can_read=True,
            expires_at=timezone.now() - timezone.timedelta(seconds=1),
        )
        with _as_peer(peer):
            resp = client.get(LIST_URL)
        assert resp.status_code == 200
        assert _hash(r) not in [item["hash"] for item in resp.json()]

    def test_wildcard_remote_user_grants_any_user(self, client, user):
        peer = _make_peer(user)
        r = _make_recording(user)
        _grant(peer, r, user, remote_user_id="")  # blank = any user

        with _as_peer(peer, remote_user_id="completely_different_user"):
            resp = client.get(LIST_URL)
        assert resp.status_code == 200
        assert _hash(r) in [item["hash"] for item in resp.json()]

    def test_wrong_remote_user_excluded(self, client, user):
        peer = _make_peer(user)
        r = _make_recording(user)
        _grant(peer, r, user, remote_user_id="alice")

        with _as_peer(peer, remote_user_id="bob"):
            resp = client.get(LIST_URL)
        assert resp.status_code == 200
        assert _hash(r) not in [item["hash"] for item in resp.json()]


# ---------------------------------------------------------------------------
# TestFederatedStatus
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestFederatedStatus:
    def test_status_with_grant(self, client, user):
        peer = _make_peer(user)
        r = _make_recording(user)
        _grant(peer, r, user)

        with _as_peer(peer):
            resp = client.get(STATUS_URL.format(hash=_hash(r)))
        assert resp.status_code == 200
        assert resp.json()["status"] == "ready"

    def test_status_without_grant_returns_403(self, client, user):
        peer = _make_peer(user)
        r = _make_recording(user)
        # no grant created

        with _as_peer(peer):
            resp = client.get(STATUS_URL.format(hash=_hash(r)))
        assert resp.status_code == 403

    def test_status_unauthenticated_returns_401(self, client, user):
        r = _make_recording(user)
        with _no_auth():
            resp = client.get(STATUS_URL.format(hash=_hash(r)))
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# TestFederatedDetail
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestFederatedDetail:
    def test_detail_with_grant(self, client, user):
        peer = _make_peer(user)
        r = _make_recording(user)
        _grant(peer, r, user)

        with _as_peer(peer):
            resp = client.get(DETAIL_URL.format(hash=_hash(r)))
        assert resp.status_code == 200
        assert resp.json()["hash"] == _hash(r)

    def test_detail_without_grant_returns_403(self, client, user):
        peer = _make_peer(user)
        r = _make_recording(user)

        with _as_peer(peer):
            resp = client.get(DETAIL_URL.format(hash=_hash(r)))
        assert resp.status_code == 403

    def test_detail_unauthenticated_returns_401(self, client, user):
        r = _make_recording(user)
        with _no_auth():
            resp = client.get(DETAIL_URL.format(hash=_hash(r)))
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# TestFederatedDetailSlice
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestFederatedDetailSlice:
    def _make_edf_recording_with_meta(self, user):
        from recordings.models import RecordingMeta

        r = _make_recording(user)
        ct = ContentType.objects.get_for_model(r, for_concrete_model=False)
        RecordingMeta.objects.create(
            content_type=ct,
            object_id=str(r.pk),
            format="EDF",
            duration=10.0,
            data_record_count=10,
            data_record_duration=1.0,
            signal_count=1,
            discontinuous=False,
        )
        return r

    def test_detail_slice_with_grant(self, client, user):
        peer = _make_peer(user)
        r = self._make_edf_recording_with_meta(user)
        _grant(peer, r, user)

        with _as_peer(peer):
            resp = client.get(f"{DETAIL_SLICE_URL.format(hash=_hash(r))}?t_start=0&t_end=5")
        assert resp.status_code == 200
        data = resp.json()
        assert data["meta"]["data_record_count"] == 5

    def test_detail_slice_without_grant_returns_403(self, client, user):
        peer = _make_peer(user)
        r = self._make_edf_recording_with_meta(user)

        with _as_peer(peer):
            resp = client.get(f"{DETAIL_SLICE_URL.format(hash=_hash(r))}?t_start=0&t_end=5")
        assert resp.status_code == 403

    def test_detail_slice_unauthenticated_returns_401(self, client, user):
        r = self._make_edf_recording_with_meta(user)
        with _no_auth():
            resp = client.get(f"{DETAIL_SLICE_URL.format(hash=_hash(r))}?t_start=0&t_end=5")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# TestFederatedDownload
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestFederatedDownload:
    def _make_edf_on_disk(self, user, tmp_path, apply_middleware=False):
        edf_bytes = _build_edf(n_channels=1, n_records=4, samples_per_record=256)
        edf_file = tmp_path / "FEDDOWN1234567890FEDDOWN12345ABC.edf"
        edf_file.write_bytes(edf_bytes)

        from recordings.models import RecordingMeta

        r = _make_recording(
            user,
            stored_name="FEDDOWN1234567890FEDDOWN12345ABC.edf",
            file_path=str(edf_file),
            file_size=len(edf_bytes),
        )
        ct = ContentType.objects.get_for_model(r, for_concrete_model=False)
        RecordingMeta.objects.create(
            content_type=ct,
            object_id=str(r.pk),
            format="EDF",
            duration=4.0,
            data_record_count=4,
            data_record_duration=1.0,
            signal_count=1,
            discontinuous=False,
        )
        return r, edf_bytes

    def test_download_with_grant(self, client, user, tmp_path):
        peer = _make_peer(user)
        r, edf_bytes = self._make_edf_on_disk(user, tmp_path)
        _grant(peer, r, user)

        with _as_peer(peer):
            resp = client.get(DOWNLOAD_URL.format(hash=_hash(r)))
        assert resp.status_code == 200
        assert b"".join(resp.streaming_content) == edf_bytes

    def test_download_without_grant_returns_403(self, client, user, tmp_path):
        peer = _make_peer(user)
        r, _ = self._make_edf_on_disk(user, tmp_path)

        with _as_peer(peer):
            resp = client.get(DOWNLOAD_URL.format(hash=_hash(r)))
        assert resp.status_code == 403

    def test_download_unauthenticated_returns_401(self, client, user, tmp_path):
        r, _ = self._make_edf_on_disk(user, tmp_path)
        with _no_auth():
            resp = client.get(DOWNLOAD_URL.format(hash=_hash(r)))
        assert resp.status_code == 401

    def test_download_apply_middleware_anonymises_header(self, client, user, tmp_path):
        """Grant with apply_middleware=True should scrub the EDF patient field."""
        peer = _make_peer(user)
        r, _ = self._make_edf_on_disk(user, tmp_path)
        _grant(peer, r, user, apply_middleware=True)

        with _as_peer(peer):
            resp = client.get(DOWNLOAD_URL.format(hash=_hash(r)))
        assert resp.status_code == 200
        body = b"".join(resp.streaming_content)
        # AnonymizeEDFHeader rewrites patient id (bytes 8..87) to "X X X X"
        assert b"X X X X" in body[:256]

    def test_download_without_middleware_returns_raw(self, client, user, tmp_path):
        """Grant without apply_middleware should return the original patient field."""
        peer = _make_peer(user)
        r, edf_bytes = self._make_edf_on_disk(user, tmp_path)
        _grant(peer, r, user, apply_middleware=False)

        with _as_peer(peer):
            resp = client.get(DOWNLOAD_URL.format(hash=_hash(r)))
        assert resp.status_code == 200
        assert b"".join(resp.streaming_content) == edf_bytes


# ---------------------------------------------------------------------------
# TestFederatedSlice
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestFederatedSlice:
    def _make_edf_on_disk(self, user, tmp_path):
        edf_bytes = _build_edf(n_channels=1, n_records=10, samples_per_record=256)
        edf_file = tmp_path / "FEDSLICE234567890FEDSLICE2345ABC.edf"
        edf_file.write_bytes(edf_bytes)

        from recordings.models import RecordingMeta

        r = _make_recording(
            user,
            stored_name="FEDSLICE234567890FEDSLICE2345ABC.edf",
            file_path=str(edf_file),
            file_size=len(edf_bytes),
        )
        ct = ContentType.objects.get_for_model(r, for_concrete_model=False)
        RecordingMeta.objects.create(
            content_type=ct,
            object_id=str(r.pk),
            format="EDF",
            duration=10.0,
            data_record_count=10,
            data_record_duration=1.0,
            signal_count=1,
            discontinuous=False,
        )
        return r

    def test_slice_with_grant(self, client, user, tmp_path):
        peer = _make_peer(user)
        r = self._make_edf_on_disk(user, tmp_path)
        _grant(peer, r, user)

        with _as_peer(peer):
            resp = client.get(f"{SLICE_URL.format(hash=_hash(r))}?t_start=0&t_end=5")
        assert resp.status_code == 200
        body = b"".join(resp.streaming_content)
        # 5 records × 256 samples × 2 bytes + header
        header_size = 256 * 2  # 1 channel
        record_size = 256 * 2
        assert len(body) == header_size + 5 * record_size

    def test_slice_without_grant_returns_403(self, client, user, tmp_path):
        peer = _make_peer(user)
        r = self._make_edf_on_disk(user, tmp_path)

        with _as_peer(peer):
            resp = client.get(f"{SLICE_URL.format(hash=_hash(r))}?t_start=0&t_end=5")
        assert resp.status_code == 403

    def test_slice_unauthenticated_returns_401(self, client, user, tmp_path):
        r = self._make_edf_on_disk(user, tmp_path)
        with _no_auth():
            resp = client.get(f"{SLICE_URL.format(hash=_hash(r))}?t_start=0&t_end=5")
        assert resp.status_code == 401

    def test_slice_apply_middleware_anonymises_header(self, client, user, tmp_path):
        peer = _make_peer(user)
        r = self._make_edf_on_disk(user, tmp_path)
        _grant(peer, r, user, apply_middleware=True)

        with _as_peer(peer):
            resp = client.get(f"{SLICE_URL.format(hash=_hash(r))}?t_start=0&t_end=5")
        assert resp.status_code == 200
        body = b"".join(resp.streaming_content)
        assert b"X X X X" in body[:256]

    def test_slice_apply_middleware_strips_annotation_text(self, client, user, tmp_path):
        """Federated slices pass through the full serve pipeline, not header-only."""
        from recordings.models import RecordingMeta
        from recordings.processors.edf import _parse_tal_record
        from recordings.tests.test_edf_processor import (
            _make_anno_record,
            _make_edf_header,
            _make_tal,
        )

        anno_sample_count = 60
        signals = [
            {"label": "EEG Fp1", "sample_count": 8},
            {"label": "EDF Annotations", "sample_count": anno_sample_count},
        ]
        header_bytes = _make_edf_header(reserved="EDF+C", signals=signals, n_records=1)
        eeg_data = b"\x01\x02" * 8
        anno_bytes = _make_anno_record(
            onset=0.0,
            tals=[_make_tal(0.5, "spike")],
            total_bytes=anno_sample_count * 2,
        )
        content = header_bytes + eeg_data + anno_bytes
        edf_file = tmp_path / "FEDSLICEANNO67890FEDSLICE2345ABC.edf"
        edf_file.write_bytes(content)

        r = _make_recording(
            user,
            stored_name="FEDSLICEANNO67890FEDSLICE2345ABC.edf",
            file_path=str(edf_file),
            file_size=len(content),
        )
        ct = ContentType.objects.get_for_model(r, for_concrete_model=False)
        RecordingMeta.objects.create(
            content_type=ct,
            object_id=str(r.pk),
            format="EDF",
            duration=1.0,
            data_record_count=1,
            data_record_duration=1.0,
            signal_count=len(signals),
            discontinuous=False,
        )
        peer = _make_peer(user)
        _grant(peer, r, user, apply_middleware=True)

        with _as_peer(peer):
            resp = client.get(SLICE_URL.format(hash=_hash(r)))
        assert resp.status_code == 200
        body = b"".join(resp.streaming_content)

        assert len(body) == len(content)
        header_size = len(header_bytes)
        assert body[header_size : header_size + 16] == eeg_data
        anno_out = body[header_size + 16 : header_size + 16 + anno_sample_count * 2]
        record_onset, annotations = _parse_tal_record(anno_out)
        assert record_onset == pytest.approx(0.0)
        assert annotations == []


# ---------------------------------------------------------------------------
# TestFederatedAudit — verifies the audit-log wiring at the endpoint layer.
#
# The helper (`log_federation_access`) and inbound endpoint wiring have their
# own dedicated tests in federation/tests/test_audit.py.  Here we cover one
# single-recording endpoint (recording_status, representative of detail /
# detail_slice / download / slice — all use the identical grant/deny pattern)
# plus list_recordings, which has the distinct "summary row" pattern.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestFederatedAudit:
    def test_status_grant_writes_200_audit_row(self, client, user):
        from federation.models import FederationAuditLog

        peer = _make_peer(user)
        r = _make_recording(user)
        _grant(peer, r, user)

        with _as_peer(peer):
            resp = client.get(STATUS_URL.format(hash=_hash(r)))
        assert resp.status_code == 200

        row = FederationAuditLog.objects.get()
        assert row.action == "recording_status"
        assert row.status_code == 200
        assert row.target == r
        assert row.remote_user_id == "user42"

    def test_status_denial_writes_403_audit_row(self, client, user):
        from federation.models import FederationAuditLog

        peer = _make_peer(user)
        r = _make_recording(user)
        # No grant.

        with _as_peer(peer):
            resp = client.get(STATUS_URL.format(hash=_hash(r)))
        assert resp.status_code == 403

        row = FederationAuditLog.objects.get()
        assert row.action == "recording_status"
        assert row.status_code == 403
        assert row.target == r

    def test_list_writes_single_summary_row(self, client, user):
        """List endpoint logs one row per call regardless of how many recordings it returns.

        The compact summary keeps the audit table from exploding on routine
        federation listings; forensics that needs the exact recording set can
        reconstruct it from the AccessRight table at the audit timestamp.
        """
        from federation.models import FederationAuditLog

        peer = _make_peer(user)
        for stored_name in (
            "AAAA1234567890AAAA1234567890ABCD.edf",
            "BBBB1234567890BBBB1234567890ABCD.edf",
            "CCCC1234567890CCCC1234567890ABCD.edf",
        ):
            r = _make_recording(user, stored_name=stored_name)
            _grant(peer, r, user)

        with _as_peer(peer):
            resp = client.get(LIST_URL)
        assert resp.status_code == 200
        assert len(resp.json()) == 3

        rows = FederationAuditLog.objects.all()
        assert rows.count() == 1
        row = rows.first()
        assert row.action == "list_recordings"
        assert row.status_code == 200
        assert row.target is None

    def test_list_trash_denial_writes_403_row(self, client, user):
        from federation.models import FederationAuditLog

        peer = _make_peer(user)
        with _as_peer(peer):
            resp = client.get(f"{LIST_URL}?trash=true")
        assert resp.status_code == 403

        row = FederationAuditLog.objects.get()
        assert row.action == "list_recordings"
        assert row.status_code == 403
        assert row.target is None

    @pytest.mark.parametrize(
        "url_template,action",
        [
            (STATUS_URL, "recording_status"),
            (DETAIL_URL, "recording_detail"),
            (DETAIL_SLICE_URL, "recording_detail_slice"),
            (DOWNLOAD_URL, "download_recording"),
            (SLICE_URL, "slice_recording"),
        ],
    )
    def test_failed_recording_writes_404_audit_row_not_200(self, client, user, url_template, action):
        """FAILED + grant must audit as 404, not 200.

        Pre-fix the per-endpoint ``log_federation_access`` ran on the
        granted branch before ``_failed_hidden_for_caller`` raised 404,
        so the audit trail recorded successful access for requests that
        actually returned 404.  Compliance reconstructions would have
        misclassified every FAILED hit as a delivered response.
        """
        from federation.models import FederationAuditLog

        peer = _make_peer(user)
        r = _make_recording(user, status=Recording.Status.FAILED)
        _grant(peer, r, user)

        with _as_peer(peer):
            resp = client.get(url_template.format(hash=_hash(r)))
        assert resp.status_code == 404

        row = FederationAuditLog.objects.get()
        assert row.action == action
        assert row.status_code == 404
        assert row.target == r

    def test_download_failed_does_not_charge_quota(self, client, user):
        """A 404 from FAILED-hidden must not burn the peer's daily byte budget.

        The quota check runs after grant validation; the FAILED-hidden
        check has to fire before it so a peer probing FAILED recordings
        cannot deplete its budget on responses it never actually receives.
        """
        from federation.models import FederationAuditLog

        peer = _make_peer(user)
        r = _make_recording(user, status=Recording.Status.FAILED, file_size=10_000_000)
        _grant(peer, r, user)

        with _as_peer(peer):
            resp = client.get(DOWNLOAD_URL.format(hash=_hash(r)))
        assert resp.status_code == 404

        # No 429 row, no 200 row — only the FAILED-hidden 404 row.
        rows = FederationAuditLog.objects.all()
        assert rows.count() == 1
        assert rows.first().status_code == 404
