"""Tests for federation.fuse_fs.

All tests exercise pure-Python logic only — no real FUSE mount or libfuse2
installation is required.  FederationOperations instances are created via
``object.__new__()`` to bypass ``__init__`` (which loads the catalogue from the
DB and requires fusepy).  HTTP calls and the transform cache are mocked
throughout.

Coverage:
- _peer_slug — URL-to-slug conversion
- _edf_header_size — header byte calculation
- _TransformCache — lazy fetch, in-memory caching, pipeline application, error handling
- AnonymizeEDFHeader — PHI stripping, parse-error fallback
- load_catalogue — DB + HTTP interaction, error handling
- FederationOperations.getattr / readdir — virtual path resolution
- FederationOperations.read — EDF header substitution, data proxy, boundary spanning,
  full-file pipeline cache, network error mapping
"""

from __future__ import annotations

import errno
import json
import time
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from federation.fuse_fs import (
    FederationOperations,
    FuseOSError,
    _edf_header_size,
    _fetch_transformed_signal_range,
    _peer_slug,
    _PeerDir,
    _reconstruct_edf_header_from_catalogue,
    _RecordingFile,
    _TransformCache,
    load_catalogue,
)
from federation.middleware import AnonymizeEDFHeader, MiddlewarePipeline

pytestmark = pytest.mark.require_fuse

# ---------------------------------------------------------------------------
# Helpers shared across test classes
# ---------------------------------------------------------------------------


def _make_ops(dirs=None, files=None, pipeline=None, signal_contexts=None) -> FederationOperations:
    """Build a FederationOperations without __init__, load_catalogue, or fusepy."""
    ops = object.__new__(FederationOperations)
    ops.local_user_id = "1"
    ops._mount_time = int(time.time())
    ops._dirs = dirs or {}
    ops._files = files or {}
    ops._signal_contexts = signal_contexts or {}
    if pipeline is None:
        pipeline = MiddlewarePipeline([AnonymizeEDFHeader()])
    ops._pipeline = pipeline
    ops._transform_cache = _TransformCache(pipeline)
    # The consumer-side read audit state __init__ sets up; without it the first
    # read raises AttributeError before reaching the code under test.
    ops._acting_user = None
    ops._read_audited = set()
    ops._peer_by_url = {}
    return ops


def _sample_dirs_files():
    """Return a small, realistic catalogue used by multiple test classes."""
    header_size = _edf_header_size(32)  # 8448 bytes
    dirs = {
        "neuro.example.com": _PeerDir(
            slug="neuro.example.com",
            peer_url="https://neuro.example.com",
        ),
    }
    files = {
        "/neuro.example.com/patient001.edf": _RecordingFile(
            slug="neuro.example.com",
            peer_url="https://neuro.example.com",
            recording_hash="ABCDEF1234567890ABCDEF1234567890",
            filename="patient001.edf",
            file_size=102400,
            header_size=header_size,
            is_edf=True,
        ),
        "/neuro.example.com/labels.csv": _RecordingFile(
            slug="neuro.example.com",
            peer_url="https://neuro.example.com",
            recording_hash="00000000000000000000000000000001",
            filename="labels.csv",
            file_size=512,
            header_size=0,
            is_edf=False,
        ),
    }
    return dirs, files


# Fake anonymized header — exact size for a 32-channel EDF (8448 bytes).
_HEADER_SIZE_32CH = _edf_header_size(32)
_ANON_HEADER = b"ANON" * (_HEADER_SIZE_32CH // 4)

# ---------------------------------------------------------------------------
# _peer_slug
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://neuro.example.com/", "neuro.example.com"),
        ("https://neuro.example.com", "neuro.example.com"),
        ("http://10.0.0.1:8000", "10.0.0.1_8000"),
        ("https://a.b/c/d", "a.b_c_d"),
        ("https://example.com:443/", "example.com_443"),
    ],
)
def test_peer_slug(url, expected):
    assert _peer_slug(url) == expected


# ---------------------------------------------------------------------------
# _edf_header_size
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ns,expected",
    [
        (0, 256),
        (1, 512),
        (32, 8448),
        (64, 16640),
        (128, 33024),
    ],
)
def test_edf_header_size(ns, expected):
    assert _edf_header_size(ns) == expected


# ---------------------------------------------------------------------------
# _TransformCache
# ---------------------------------------------------------------------------


class TestTransformCache:
    def test_get_header_first_call_fetches_and_transforms(self):
        pipeline = MiddlewarePipeline([AnonymizeEDFHeader()])
        cache = _TransformCache(pipeline)
        raw = b"R" * 512
        transformed = b"T" * 512

        with (
            patch.object(_TransformCache, "_fetch_range", return_value=raw) as mock_fetch,
            patch.object(pipeline, "apply_header", return_value=transformed) as mock_apply,
        ):
            result = cache.get_header("https://peer", "HASH", 512, "jwt")

        assert result == transformed
        mock_fetch.assert_called_once_with("https://peer", "HASH", 0, 511, "jwt")
        mock_apply.assert_called_once_with(raw)

    def test_get_header_second_call_returns_cached_without_refetch(self):
        pipeline = MiddlewarePipeline([AnonymizeEDFHeader()])
        cache = _TransformCache(pipeline)
        raw = b"R" * 512
        transformed = b"T" * 512

        with (
            patch.object(_TransformCache, "_fetch_range", return_value=raw) as mock_fetch,
            patch.object(pipeline, "apply_header", return_value=transformed),
        ):
            result1 = cache.get_header("https://peer", "HASH", 512, "jwt")
            result2 = cache.get_header("https://peer", "HASH", 512, "jwt")

        assert result1 == result2 == transformed
        mock_fetch.assert_called_once()  # not twice

    def test_get_header_different_hashes_cached_independently(self):
        pipeline = MiddlewarePipeline([])  # identity pipeline
        cache = _TransformCache(pipeline)

        def fake_fetch(peer_url, recording_hash, start, end, jwt):
            return recording_hash.encode().ljust(8, b"\x00")

        with patch.object(_TransformCache, "_fetch_range", side_effect=fake_fetch):
            r1 = cache.get_header("https://peer", "HASH_A", 8, "jwt")
            r2 = cache.get_header("https://peer", "HASH_B", 8, "jwt")

        assert r1[:6] == b"HASH_A"
        assert r2[:6] == b"HASH_B"

    def test_get_header_network_error_raises_eio(self):
        pipeline = MiddlewarePipeline([])
        cache = _TransformCache(pipeline)
        with (
            patch.object(
                _TransformCache,
                "_fetch_range",
                side_effect=OSError(errno.EIO, "connection reset"),
            ),
            pytest.raises(OSError) as exc_info,
        ):
            cache.get_header("https://peer", "HASH", 512, "jwt")
        assert exc_info.value.args[0] == errno.EIO

    def test_get_file_applies_full_pipeline(self):
        from federation.middleware import EDFFullFileMiddleware

        class _HalfSignals(EDFFullFileMiddleware):
            targets = frozenset({"fuse"})

            def transform(self, h, s):
                return h, s[: len(s) // 2]

            def compute_output_size(self, fs, hs):
                return hs + (fs - hs) // 2

        pipeline = MiddlewarePipeline([_HalfSignals()])
        cache = _TransformCache(pipeline)
        raw_header = b"H" * 256
        raw_signals = b"S" * 512

        def fake_fetch(peer_url, recording_hash, start, end, jwt):
            return raw_header if start == 0 else raw_signals

        with patch.object(_TransformCache, "_fetch_range", side_effect=fake_fetch):
            result = cache.get_file("https://peer", "HASH", 256, 768, "jwt")

        assert result == raw_header + raw_signals[:256]

    def test_anonymize_edf_header_returns_raw_on_parse_error(self):
        """Garbage bytes must be returned unchanged rather than raising."""
        anon = AnonymizeEDFHeader()
        raw = b"\x00" * 256  # not a valid EDF header
        result = anon.transform_header(raw)
        assert result == raw

    def test_anonymize_edf_header_strips_phi_from_valid_edf_header(self):
        """Patient name injected into the patient field must be erased."""
        from recordings.processors.edf import (
            EdfHeader,
            EdfSignalInfo,
            _build_clean_header,
        )

        ns = 1
        hdr = EdfHeader(
            data_format="edf+",
            patient_id="Firstname Lastname",
            local_recording_id="Study XYZ",
            recording_date=None,
            header_record_bytes=256 * (1 + ns),
            reserved="EDF+C",
            data_record_count=10,
            data_record_duration=1.0,
            signal_count=ns,
            is_plus=True,
            discontinuous=False,
        )
        sig = EdfSignalInfo(
            label="EEG Fp1",
            transducer_type="",
            physical_unit="uV",
            physical_min=-100.0,
            physical_max=100.0,
            digital_min=-32768,
            digital_max=32767,
            prefiltering="",
            sample_count=256,
            reserved="",
        )
        raw = bytearray(_build_clean_header(hdr, [sig]))

        # Inject a fake patient name into bytes 8–87 (the 80-byte patient ID field).
        phi = b"John Doe" + b" " * 72
        raw[8:88] = phi
        raw = bytes(raw)

        anon = AnonymizeEDFHeader()
        result = anon.transform_header(raw)

        patient_field = result[8:88].decode("latin-1").strip()
        assert "John" not in patient_field
        assert patient_field == "X X X X"


# ---------------------------------------------------------------------------
# load_catalogue
# ---------------------------------------------------------------------------


def _recording_list_payload():
    return [
        {
            "hash": "ABCDEF1234567890ABCDEF1234567890",
            "file_extension": ".edf",
            "original_name": "patient001.edf",
            "file_size": 102400,
            "status": "ready",
            "meta": {
                "format": "edf+",
                "signal_count": 32,
                "duration": 300.0,
                "data_record_count": 300,
                "data_record_duration": 1.0,
                "discontinuous": False,
            },
        },
        {
            "hash": "00000000000000000000000000000001",
            "file_extension": ".csv",
            "original_name": "labels.csv",
            "file_size": 512,
            "status": "ready",
            "meta": None,
        },
    ]


def _mock_urlopen(payload):
    resp = MagicMock()
    resp.read.return_value = json.dumps(payload).encode()
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


@pytest.fixture()
def trusted_peer(db, make_user):
    from federation.models import FederatedPeer

    return FederatedPeer.objects.create(
        url="https://neuro.example.com",
        display_name="Neuro",
        public_key="A" * 43,
        is_trusted=True,
        added_by=make_user(),
    )


def test_load_catalogue_creates_dir_and_files(trusted_peer, db):
    with (
        patch("federation.fuse_fs._make_jwt", return_value="test-jwt"),
        patch(
            "urllib.request.urlopen",
            return_value=_mock_urlopen(_recording_list_payload()),
        ),
    ):
        dirs, files, signal_contexts = load_catalogue("1")

    assert "neuro.example.com" in dirs
    assert dirs["neuro.example.com"].peer_url == "https://neuro.example.com"

    edf_path = "/neuro.example.com/patient001.edf"
    csv_path = "/neuro.example.com/labels.csv"
    assert edf_path in files
    assert csv_path in files

    edf = files[edf_path]
    assert edf.is_edf is True
    assert edf.header_size == _edf_header_size(32)
    assert edf.file_size == 102400
    assert edf.remote_file_size == 102400
    assert edf.recording_hash == "ABCDEF1234567890ABCDEF1234567890"

    csv = files[csv_path]
    assert csv.is_edf is False
    assert csv.header_size == 0


def test_load_catalogue_with_pipeline_pre_computes_file_size(trusted_peer, db):
    from federation.middleware import EDFFullFileMiddleware

    class _HalfSignals(EDFFullFileMiddleware):
        targets = frozenset({"fuse"})

        def transform(self, h, s):
            return h, s

        def compute_output_size(self, fs, hs):
            return hs + (fs - hs) // 2

    pipeline = MiddlewarePipeline([_HalfSignals()])
    with (
        patch("federation.fuse_fs._make_jwt", return_value="test-jwt"),
        patch(
            "urllib.request.urlopen",
            return_value=_mock_urlopen(_recording_list_payload()),
        ),
    ):
        _, files, _ = load_catalogue("1", pipeline=pipeline)

    edf = files["/neuro.example.com/patient001.edf"]
    header_size = _edf_header_size(32)
    expected_size = header_size + (102400 - header_size) // 2
    assert edf.file_size == expected_size
    assert edf.remote_file_size == 102400  # original preserved


def test_load_catalogue_uses_download_size_when_present(trusted_peer, db):
    """download_size from the server takes precedence over file_size as remote baseline."""
    payload = _recording_list_payload()
    # Server reports a download_size that differs from file_size (e.g. signal pipeline).
    payload[0]["download_size"] = 81920  # smaller than file_size=102400

    with (
        patch("federation.fuse_fs._make_jwt", return_value="test-jwt"),
        patch("urllib.request.urlopen", return_value=_mock_urlopen(payload)),
    ):
        _, files, _ = load_catalogue("1")  # no local pipeline

    edf = files["/neuro.example.com/patient001.edf"]
    # Without a local pipeline the remote size is the final st_size.
    assert edf.file_size == 81920
    assert edf.remote_file_size == 81920  # remote_file_size is set from the chosen baseline


def test_load_catalogue_falls_back_to_file_size_without_download_size(trusted_peer, db):
    """When download_size is absent the raw file_size is used as remote baseline."""
    payload = _recording_list_payload()
    assert "download_size" not in payload[0]

    with (
        patch("federation.fuse_fs._make_jwt", return_value="test-jwt"),
        patch("urllib.request.urlopen", return_value=_mock_urlopen(payload)),
    ):
        _, files, _ = load_catalogue("1")

    edf = files["/neuro.example.com/patient001.edf"]
    assert edf.file_size == 102400


def test_load_catalogue_skips_unreachable_peer(trusted_peer, db):
    with (
        patch("federation.fuse_fs._make_jwt", return_value="test-jwt"),
        patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.URLError("connection refused"),
        ),
    ):
        dirs, files, _ = load_catalogue("1")

    assert "neuro.example.com" in dirs  # dir still created
    assert not files


def test_load_catalogue_ignores_untrusted_peer(db, make_user):
    from federation.models import FederatedPeer

    FederatedPeer.objects.create(
        url="https://untrusted.example.com",
        public_key="B" * 43,
        is_trusted=False,
        added_by=make_user(),
    )
    with patch("federation.fuse_fs._make_jwt", return_value="jwt"):
        dirs, files, _ = load_catalogue("1")

    assert not dirs
    assert not files


def test_load_catalogue_bdf_detected_as_edf(trusted_peer, db):
    payload = [
        {
            "hash": "C" * 32,
            "file_extension": ".bdf",
            "original_name": "brain.bdf",
            "file_size": 204800,
            "status": "ready",
            "meta": {
                "format": "bdf+",
                "signal_count": 64,
                "duration": 600.0,
                "data_record_count": 600,
                "data_record_duration": 1.0,
                "discontinuous": False,
            },
        }
    ]
    with (
        patch("federation.fuse_fs._make_jwt", return_value="jwt"),
        patch("urllib.request.urlopen", return_value=_mock_urlopen(payload)),
    ):
        _, files, _ = load_catalogue("1")

    entry = files["/neuro.example.com/brain.bdf"]
    assert entry.is_edf is True
    assert entry.header_size == _edf_header_size(64)


# ---------------------------------------------------------------------------
# FederationOperations.getattr
# ---------------------------------------------------------------------------


class TestGetattr:
    def test_root_is_directory(self):
        import stat as stat_mod

        ops = _make_ops(*_sample_dirs_files())
        result = ops.getattr("/")
        assert result["st_mode"] & stat_mod.S_IFDIR
        assert result["st_mode"] & 0o555

    def test_peer_slug_is_directory(self):
        import stat as stat_mod

        ops = _make_ops(*_sample_dirs_files())
        result = ops.getattr("/neuro.example.com")
        assert result["st_mode"] & stat_mod.S_IFDIR

    def test_recording_is_regular_file_with_correct_size(self):
        import stat as stat_mod

        ops = _make_ops(*_sample_dirs_files())
        result = ops.getattr("/neuro.example.com/patient001.edf")
        assert result["st_mode"] & stat_mod.S_IFREG
        assert result["st_size"] == 102400

    def test_unknown_path_raises_enoent(self):
        ops = _make_ops(*_sample_dirs_files())
        with pytest.raises(FuseOSError) as exc_info:
            ops.getattr("/neuro.example.com/nonexistent.edf")
        assert exc_info.value.args[0] == errno.ENOENT

    def test_unknown_slug_raises_enoent(self):
        ops = _make_ops(*_sample_dirs_files())
        with pytest.raises(FuseOSError) as exc_info:
            ops.getattr("/unknown-peer")
        assert exc_info.value.args[0] == errno.ENOENT


# ---------------------------------------------------------------------------
# FederationOperations.readdir
# ---------------------------------------------------------------------------


class TestReaddir:
    def test_root_lists_peer_slugs(self):
        ops = _make_ops(*_sample_dirs_files())
        entries = ops.readdir("/", 0)
        assert "neuro.example.com" in entries
        assert "." in entries and ".." in entries

    def test_peer_dir_lists_filenames(self):
        ops = _make_ops(*_sample_dirs_files())
        entries = ops.readdir("/neuro.example.com", 0)
        assert "patient001.edf" in entries
        assert "labels.csv" in entries

    def test_peer_dir_does_not_leak_other_peer_files(self):
        dirs, files = _sample_dirs_files()
        dirs["other.example.com"] = _PeerDir("other.example.com", "https://other.example.com")
        files["/other.example.com/secret.edf"] = _RecordingFile(
            slug="other.example.com",
            peer_url="https://other.example.com",
            recording_hash="F" * 32,
            filename="secret.edf",
            file_size=1024,
            header_size=512,
            is_edf=True,
        )
        ops = _make_ops(dirs, files)
        entries = ops.readdir("/neuro.example.com", 0)
        assert "secret.edf" not in entries


# ---------------------------------------------------------------------------
# FederationOperations.read
# ---------------------------------------------------------------------------


class TestRead:
    def _ops(self) -> FederationOperations:
        return _make_ops(*_sample_dirs_files())

    # ── Edge cases ──────────────────────────────────────────────────────────

    def test_offset_beyond_eof_returns_empty(self):
        ops = self._ops()
        with patch("federation.fuse_fs._make_jwt", return_value="jwt"):
            result = ops.read("/neuro.example.com/patient001.edf", 1024, 999_999, 0)
        assert result == b""

    def test_unknown_path_raises_enoent(self):
        ops = self._ops()
        with patch("federation.fuse_fs._make_jwt", return_value="jwt"), pytest.raises(FuseOSError) as exc_info:
            ops.read("/neuro.example.com/ghost.edf", 512, 0, 0)
        assert exc_info.value.args[0] == errno.ENOENT

    # ── Non-EDF: transparent proxy ───────────────────────────────────────────

    def test_non_edf_proxies_raw_bytes(self):
        ops = self._ops()
        raw = b"col1,col2\n1,2\n"
        with (
            patch("federation.fuse_fs._make_jwt", return_value="jwt"),
            patch("federation.fuse_fs._http_range", return_value=raw) as mock_range,
        ):
            result = ops.read("/neuro.example.com/labels.csv", 512, 0, 0)

        assert result == raw
        mock_range.assert_called_once_with(
            "https://neuro.example.com/recordings/api/v1/00000000000000000000000000000001/file",
            "jwt",
            0,
            511,
        )

    # ── EDF isometric pipeline: header-only read ─────────────────────────────

    def test_edf_header_only_returns_anon_bytes_without_network_io(self):
        ops = self._ops()
        ops._transform_cache = MagicMock()
        ops._transform_cache.get_header.return_value = _ANON_HEADER
        with (
            patch("federation.fuse_fs._make_jwt", return_value="jwt"),
            patch("federation.fuse_fs._http_range") as mock_range,
        ):
            result = ops.read("/neuro.example.com/patient001.edf", 256, 0, 0)

        assert result == _ANON_HEADER[:256]
        mock_range.assert_not_called()

    def test_edf_header_read_at_non_zero_offset(self):
        ops = self._ops()
        ops._transform_cache = MagicMock()
        ops._transform_cache.get_header.return_value = _ANON_HEADER
        with (
            patch("federation.fuse_fs._make_jwt", return_value="jwt"),
            patch("federation.fuse_fs._http_range") as mock_range,
        ):
            result = ops.read("/neuro.example.com/patient001.edf", 256, 8, 0)

        assert result == _ANON_HEADER[8:264]
        mock_range.assert_not_called()

    # ── EDF isometric pipeline: data section read ────────────────────────────

    def test_edf_data_section_proxied_directly(self):
        ops = self._ops()
        ops._transform_cache = MagicMock()
        data_bytes = b"\x01\x02" * 256
        with (
            patch("federation.fuse_fs._make_jwt", return_value="jwt"),
            patch("federation.fuse_fs._http_range", return_value=data_bytes) as mock_range,
        ):
            result = ops.read(
                "/neuro.example.com/patient001.edf",
                512,
                _HEADER_SIZE_32CH,
                0,
            )

        assert result == data_bytes
        ops._transform_cache.get_header.assert_not_called()
        mock_range.assert_called_once_with(
            "https://neuro.example.com/recordings/api/v1/ABCDEF1234567890ABCDEF1234567890/file",
            "jwt",
            _HEADER_SIZE_32CH,
            _HEADER_SIZE_32CH + 511,
        )

    # ── EDF isometric pipeline: spanning the header/data boundary ────────────

    def test_edf_spanning_boundary_concatenates_anon_header_and_raw_data(self):
        ops = self._ops()
        ops._transform_cache = MagicMock()
        ops._transform_cache.get_header.return_value = _ANON_HEADER
        data_bytes = b"\xff" * 128
        with (
            patch("federation.fuse_fs._make_jwt", return_value="jwt"),
            patch("federation.fuse_fs._http_range", return_value=data_bytes) as mock_range,
        ):
            # 64 bytes before end of header → request spans the boundary.
            offset = _HEADER_SIZE_32CH - 64
            result = ops.read("/neuro.example.com/patient001.edf", 192, offset, 0)

        assert result == _ANON_HEADER[offset:] + data_bytes
        mock_range.assert_called_once_with(
            "https://neuro.example.com/recordings/api/v1/ABCDEF1234567890ABCDEF1234567890/file",
            "jwt",
            _HEADER_SIZE_32CH,
            _HEADER_SIZE_32CH + 127,
        )

    # ── EDF full-file pipeline ───────────────────────────────────────────────

    def test_edf_full_file_pipeline_serves_from_cache(self):
        from federation.middleware import EDFFullFileMiddleware

        class _IdentityFull(EDFFullFileMiddleware):
            targets = frozenset({"fuse"})

            def transform(self, h, s):
                return h, s

            def compute_output_size(self, fs, hs):
                return fs

        pipeline = MiddlewarePipeline([_IdentityFull()])
        dirs, files = _sample_dirs_files()
        # Set remote_file_size so get_file knows how much to fetch
        edf_entry = files["/neuro.example.com/patient001.edf"]
        files["/neuro.example.com/patient001.edf"] = edf_entry._replace(remote_file_size=102400)
        ops = _make_ops(dirs, files, pipeline=pipeline)
        full_content = _ANON_HEADER + b"\xab" * (102400 - _HEADER_SIZE_32CH)
        ops._transform_cache = MagicMock()
        ops._transform_cache.get_file.return_value = full_content

        with patch("federation.fuse_fs._make_jwt", return_value="jwt"):
            result = ops.read("/neuro.example.com/patient001.edf", 256, 0, 0)

        assert result == full_content[:256]
        ops._transform_cache.get_file.assert_called_once()

    # ── Error mapping ────────────────────────────────────────────────────────

    def test_network_timeout_raises_eio(self):
        ops = self._ops()
        with (
            patch("federation.fuse_fs._make_jwt", return_value="jwt"),
            patch(
                "federation.fuse_fs._http_range",
                side_effect=urllib.error.URLError("timed out"),
            ),
            pytest.raises(FuseOSError) as exc_info,
        ):
            ops.read("/neuro.example.com/labels.csv", 512, 0, 0)
        assert exc_info.value.args[0] == errno.EIO

    def test_http_403_raises_eacces(self):
        ops = self._ops()
        err = urllib.error.HTTPError(url="", code=403, msg="Forbidden", hdrs={}, fp=None)
        with (
            patch("federation.fuse_fs._make_jwt", return_value="jwt"),
            patch("federation.fuse_fs._http_range", side_effect=err),
            pytest.raises(FuseOSError) as exc_info,
        ):
            ops.read("/neuro.example.com/labels.csv", 512, 0, 0)
        assert exc_info.value.args[0] == errno.EACCES

    def test_http_404_raises_enoent(self):
        ops = self._ops()
        err = urllib.error.HTTPError(url="", code=404, msg="Not Found", hdrs={}, fp=None)
        with (
            patch("federation.fuse_fs._make_jwt", return_value="jwt"),
            patch("federation.fuse_fs._http_range", side_effect=err),
            pytest.raises(FuseOSError) as exc_info,
        ):
            ops.read("/neuro.example.com/labels.csv", 512, 0, 0)
        assert exc_info.value.args[0] == errno.ENOENT

    def test_http_500_raises_eio(self):
        ops = self._ops()
        err = urllib.error.HTTPError(url="", code=500, msg="Server Error", hdrs={}, fp=None)
        with (
            patch("federation.fuse_fs._make_jwt", return_value="jwt"),
            patch("federation.fuse_fs._http_range", side_effect=err),
            pytest.raises(FuseOSError) as exc_info,
        ):
            ops.read("/neuro.example.com/labels.csv", 512, 0, 0)
        assert exc_info.value.args[0] == errno.EIO


# ---------------------------------------------------------------------------
# Signal pipeline helpers
# ---------------------------------------------------------------------------


def _build_1ch_edf_header(n_records: int, samples_per_record: int = 10) -> bytes:
    """Build a minimal 1-channel EDF header for signal pipeline tests.

    Header size: 256 * 2 = 512 bytes.
    Input record size: samples_per_record * 2 bytes (EDF uses 2 bytes/sample).
    """
    from recordings.processors.edf import EdfHeader, EdfSignalInfo, _build_clean_header

    hdr = EdfHeader(
        data_format="edf",
        patient_id="X X X X",
        local_recording_id="Startdate X X X X",
        recording_date=None,
        header_record_bytes=256 * 2,
        reserved="",
        data_record_count=n_records,
        data_record_duration=1.0,
        signal_count=1,
        is_plus=False,
        discontinuous=False,
    )
    sig = EdfSignalInfo(
        label="EEG Fp1",
        transducer_type="",
        physical_unit="uV",
        physical_min=-100.0,
        physical_max=100.0,
        digital_min=-32768,
        digital_max=32767,
        prefiltering="",
        sample_count=samples_per_record,
        reserved="",
    )
    return _build_clean_header(hdr, [sig])


def _build_signal_ctx(n_records: int = 5, samples_per_record: int = 10):
    """Build a DownsampleMiddleware(factor=2) SignalPipelineContext for tests.

    Input record size:  samples_per_record * 2 bytes  (e.g. 10 * 2 = 20)
    Output record size: (samples_per_record // 2) * 2 bytes  (e.g. 5 * 2 = 10)
    Header size: 512 bytes (1-channel EDF, unchanged by downsampling).
    """
    from federation.middleware import DownsampleMiddleware, MiddlewarePipeline

    pipeline = MiddlewarePipeline([DownsampleMiddleware(factor=2)]).for_scope("fuse")
    raw_header = _build_1ch_edf_header(n_records, samples_per_record)
    return pipeline.build_signal_context(raw_header, n_records), raw_header


class TestFetchTransformedSignalRange:
    """Unit tests for _fetch_transformed_signal_range."""

    # Layout: 1 ch, 10 samples/rec, factor-2 downsample.
    # input_record_size=20, output_record_size=10, header_size=512.

    def _entry(self):
        return _RecordingFile(
            slug="neuro.example.com",
            peer_url="https://neuro.example.com",
            recording_hash="ABCDEF1234567890ABCDEF1234567890",
            filename="test.edf",
            file_size=512 + 5 * 10,  # new_header + 5 transformed records
            header_size=512,  # original header size (used for remote fetch offset)
            is_edf=True,
            remote_file_size=512 + 5 * 20,
        )

    def test_fetches_single_record_and_transforms(self):
        ctx, _ = _build_signal_ctx(n_records=5)
        entry = self._entry()
        # Request first 4 bytes of output signal (first 4 of transformed record 0).
        # That maps to input record 0: bytes 512–531 on the remote.
        raw_record = bytes(range(20))  # 10 samples × 2 bytes
        with patch("federation.fuse_fs._http_range", return_value=raw_record) as mock_range:
            result = _fetch_transformed_signal_range(entry, ctx, 512, 515, "jwt")

        # Should have fetched exactly one input record (20 bytes).
        mock_range.assert_called_once_with(
            "https://neuro.example.com/recordings/api/v1/ABCDEF1234567890ABCDEF1234567890/file",
            "jwt",
            512,
            531,
        )
        # Result is 4 bytes, sliced from the 10-byte transformed record.
        assert len(result) == 4

    def test_fetches_multiple_records_in_one_request(self):
        ctx, _ = _build_signal_ctx(n_records=5)
        entry = self._entry()
        # Request all 50 output signal bytes (5 records × 10 bytes).
        # Remote: input bytes 512–611 (5 records × 20 bytes).
        raw_all = bytes(range(100))  # 5 × 20 bytes
        with patch("federation.fuse_fs._http_range", return_value=raw_all) as mock_range:
            result = _fetch_transformed_signal_range(entry, ctx, 512, 561, "jwt")

        mock_range.assert_called_once_with(
            "https://neuro.example.com/recordings/api/v1/ABCDEF1234567890ABCDEF1234567890/file",
            "jwt",
            512,
            611,
        )
        assert len(result) == 50

    def test_network_error_raises_eio(self):
        ctx, _ = _build_signal_ctx(n_records=5)
        entry = self._entry()
        with (
            patch(
                "federation.fuse_fs._http_range",
                side_effect=urllib.error.URLError("timeout"),
            ),
            pytest.raises(OSError) as exc_info,
        ):
            _fetch_transformed_signal_range(entry, ctx, 512, 519, "jwt")
        assert exc_info.value.args[0] == errno.EIO


def _signal_entry(index=0, label="EEG Fp1", sample_count=256, is_annotation_channel=False):
    """Build a minimal SignalInfoOut-compatible dict for test payloads."""
    return {
        "index": index,
        "label": label,
        "sample_count": sample_count,
        "sampling_rate": float(sample_count),
        "is_annotation_channel": is_annotation_channel,
        "signal_type": "",
        "physical_unit": "uV",
        "physical_min": -100.0,
        "physical_max": 100.0,
        "digital_min": -32768,
        "digital_max": 32767,
        "transducer_type": "",
        "prefiltering": "",
    }


class TestReconstructEdfHeaderFromCatalogue:
    """Unit tests for _reconstruct_edf_header_from_catalogue."""

    def test_produces_parseable_header(self):
        from recordings.processors.edf import parse_edf_header, parse_signal_infos

        meta = {
            "format": "edf",
            "data_record_count": 10,
            "data_record_duration": 1.0,
            "signal_count": 1,
            "discontinuous": False,
        }
        signals = [_signal_entry(index=0, label="EEG Fp1", sample_count=256)]
        raw = _reconstruct_edf_header_from_catalogue(meta, signals)

        hdr = parse_edf_header(raw)
        assert hdr.data_format == "edf"
        assert hdr.data_record_count == 10
        assert hdr.signal_count == 1

        sig_infos = parse_signal_infos(raw, hdr)
        assert len(sig_infos) == 1
        assert sig_infos[0].label.strip() == "EEG Fp1"
        assert sig_infos[0].sample_count == 256

    def test_plus_format_sets_reserved_field(self):
        from recordings.processors.edf import parse_edf_header

        meta = {
            "format": "edf+",
            "data_record_count": 1,
            "data_record_duration": 1.0,
            "signal_count": 1,
            "discontinuous": False,
        }
        raw = _reconstruct_edf_header_from_catalogue(meta, [_signal_entry()])
        hdr = parse_edf_header(raw)
        assert hdr.reserved.strip() == "EDF+C"
        assert hdr.is_plus is True

    def test_bdf_format(self):
        from recordings.processors.edf import parse_edf_header

        meta = {
            "format": "bdf",
            "data_record_count": 5,
            "data_record_duration": 1.0,
            "signal_count": 1,
            "discontinuous": False,
        }
        raw = _reconstruct_edf_header_from_catalogue(meta, [_signal_entry()])
        hdr = parse_edf_header(raw)
        assert hdr.data_format == "bdf"

    def test_signals_sorted_by_index(self):
        from recordings.processors.edf import parse_edf_header, parse_signal_infos

        meta = {
            "format": "edf",
            "data_record_count": 1,
            "data_record_duration": 1.0,
            "signal_count": 2,
            "discontinuous": False,
        }
        # Pass in reverse order.
        signals = [
            _signal_entry(index=1, label="EMG"),
            _signal_entry(index=0, label="EEG"),
        ]
        raw = _reconstruct_edf_header_from_catalogue(meta, signals)
        hdr = parse_edf_header(raw)
        sig_infos = parse_signal_infos(raw, hdr)
        assert sig_infos[0].label.strip() == "EEG"
        assert sig_infos[1].label.strip() == "EMG"


class TestLoadCatalogueSignalPipeline:
    """load_catalogue builds SignalPipelineContexts when signal middleware is active."""

    def test_builds_context_from_catalogue_signals_without_http_fetch(self, trusted_peer, db):
        """When catalogue includes signals, no HTTP range request is issued."""
        from unittest.mock import patch

        from federation.middleware import DownsampleMiddleware, MiddlewarePipeline

        pipeline = MiddlewarePipeline([DownsampleMiddleware(factor=2)])
        # 1-channel EDF: 300 records, 256 sps.  input_rec=512, output_rec=256.
        header_size = _edf_header_size(1)  # 512

        payload = [
            {
                "hash": "ABCDEF1234567890ABCDEF1234567890",
                "file_extension": ".edf",
                "original_name": "test.edf",
                "file_size": header_size + 300 * 512,
                "status": "ready",
                "meta": {
                    "format": "edf",
                    "signal_count": 1,
                    "duration": 300.0,
                    "data_record_count": 300,
                    "data_record_duration": 1.0,
                    "discontinuous": False,
                    "signals": [_signal_entry(index=0, label="EEG Fp1", sample_count=256)],
                },
            }
        ]

        with (
            patch("federation.fuse_fs._make_jwt", return_value="jwt"),
            patch("urllib.request.urlopen", return_value=_mock_urlopen(payload)),
            patch("federation.fuse_fs._http_range") as mock_range,
        ):
            _, files, signal_contexts = load_catalogue("1", pipeline=pipeline)

        # No header fetches — context built from catalogue data.
        mock_range.assert_not_called()

        key = ("https://neuro.example.com", "ABCDEF1234567890ABCDEF1234567890")
        assert key in signal_contexts

        ctx = signal_contexts[key]
        assert ctx.output_record_size == 256  # 128 samples × 2 bytes
        assert ctx.input_record_size == 512  # 256 samples × 2 bytes
        assert ctx.n_records == 300
        assert ctx.new_header is not None  # header reconstructed from catalogue

        entry = files["/neuro.example.com/test.edf"]
        assert entry.file_size == ctx.output_file_size
        assert entry.file_size == header_size + 300 * 256

    def test_catalogue_signals_build_exception_falls_back_to_remote_size(self, trusted_peer, db):
        """When context building from catalogue signals raises, fall back gracefully."""
        from unittest.mock import patch

        from federation.middleware import DownsampleMiddleware, MiddlewarePipeline

        pipeline = MiddlewarePipeline([DownsampleMiddleware(factor=2)])
        payload = [
            {
                "hash": "ABCDEF1234567890ABCDEF1234567890",
                "file_extension": ".edf",
                "original_name": "test.edf",
                "file_size": 102400,
                "status": "ready",
                "meta": {
                    "format": "edf",
                    "signal_count": 1,
                    "duration": 10.0,
                    "data_record_count": 10,
                    "data_record_duration": 1.0,
                    "discontinuous": False,
                    "signals": [_signal_entry(index=0, label="EEG", sample_count=256)],
                },
            }
        ]

        with (
            patch("federation.fuse_fs._make_jwt", return_value="jwt"),
            patch("urllib.request.urlopen", return_value=_mock_urlopen(payload)),
            patch(
                "federation.fuse_fs._reconstruct_edf_header_from_catalogue",
                side_effect=RuntimeError("simulated failure"),
            ),
        ):
            _, files, signal_contexts = load_catalogue("1", pipeline=pipeline)

        assert not signal_contexts
        entry = files["/neuro.example.com/test.edf"]
        assert entry.file_size == 102400


class TestReadSignalPipeline:
    """FederationOperations.read() with an EDFSignalMiddleware pipeline."""

    # 1-channel EDF, factor-2 downsample, 5 records.
    # new_header_size=512, input_rec=20, output_rec=10, output_file_size=562.
    _N_RECORDS = 5
    _SAMPLES = 10
    _HEADER_SIZE = 512  # 256 * (1 + 1) for 1-channel EDF
    _IN_REC = 20  # 10 samples × 2 bytes
    _OUT_REC = 10  # 5 samples × 2 bytes
    _PATH = "/neuro.example.com/patient001.edf"

    def _setup(self):
        from federation.middleware import DownsampleMiddleware, MiddlewarePipeline

        pipeline = MiddlewarePipeline([DownsampleMiddleware(factor=2)])
        ctx, _ = _build_signal_ctx(self._N_RECORDS, self._SAMPLES)
        output_file_size = ctx.output_file_size  # 512 + 5*10 = 562

        entry = _RecordingFile(
            slug="neuro.example.com",
            peer_url="https://neuro.example.com",
            recording_hash="ABCDEF1234567890ABCDEF1234567890",
            filename="patient001.edf",
            file_size=output_file_size,
            header_size=self._HEADER_SIZE,
            is_edf=True,
            remote_file_size=self._HEADER_SIZE + self._N_RECORDS * self._IN_REC,
        )
        dirs = {"neuro.example.com": _PeerDir("neuro.example.com", "https://neuro.example.com")}
        files = {self._PATH: entry}
        signal_contexts = {("https://neuro.example.com", "ABCDEF1234567890ABCDEF1234567890"): ctx}
        ops = _make_ops(dirs, files, pipeline=pipeline, signal_contexts=signal_contexts)
        return ops, ctx

    def test_header_only_read_returns_transformed_header(self):
        ops, ctx = self._setup()
        with (
            patch("federation.fuse_fs._make_jwt", return_value="jwt"),
            patch("federation.fuse_fs._http_range") as mock_range,
        ):
            result = ops.read(self._PATH, 32, 0, 0)

        assert result == ctx.new_header[:32]
        mock_range.assert_not_called()

    def test_signal_only_read_issues_one_range_request(self):
        ops, ctx = self._setup()
        # Read 10 output bytes starting at the signal region (offset=512).
        # Covers output record 0 → input bytes 512–531.
        raw_record = bytes(range(self._IN_REC))
        with (
            patch("federation.fuse_fs._make_jwt", return_value="jwt"),
            patch("federation.fuse_fs._http_range", return_value=raw_record) as mock_range,
        ):
            result = ops.read(self._PATH, self._OUT_REC, self._HEADER_SIZE, 0)

        assert len(result) == self._OUT_REC
        mock_range.assert_called_once()
        _, call_jwt, start, end = mock_range.call_args[0]
        assert start == self._HEADER_SIZE
        assert end == self._HEADER_SIZE + self._IN_REC - 1

    def test_boundary_spanning_read_concatenates_header_and_signal(self):
        ops, ctx = self._setup()
        # 8 bytes from end of header + 8 bytes from start of signal region.
        offset = self._HEADER_SIZE - 8
        raw_record = bytes(range(self._IN_REC))
        with (
            patch("federation.fuse_fs._make_jwt", return_value="jwt"),
            patch("federation.fuse_fs._http_range", return_value=raw_record),
        ):
            result = ops.read(self._PATH, 16, offset, 0)

        assert result[:8] == ctx.new_header[offset:]
        assert len(result) == 16

    def test_no_signal_context_falls_back_to_raw_proxy(self):
        ops, ctx = self._setup()
        ops._signal_contexts = {}  # clear contexts
        raw = b"\xab" * 10
        with (
            patch("federation.fuse_fs._make_jwt", return_value="jwt"),
            patch("federation.fuse_fs._http_range", return_value=raw) as mock_range,
        ):
            result = ops.read(self._PATH, 10, self._HEADER_SIZE, 0)

        assert result == raw
        mock_range.assert_called_once()

    def test_getattr_reports_output_file_size(self):
        ops, ctx = self._setup()
        attr = ops.getattr(self._PATH)
        assert attr["st_size"] == ctx.output_file_size
