"""Tests for federation.middleware."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from federation.middleware import (
    AnonymizeEDFHeader,
    DownsampleMiddleware,
    DropChannelsMiddleware,
    EDFFullFileMiddleware,
    EDFHeaderMiddleware,
    EDFSignalMiddleware,
    MiddlewarePipeline,
    StripAnnotationTextMiddleware,
    _SignalInfoLike,
)

# ---------------------------------------------------------------------------
# Test helpers (concrete middleware implementations)
# ---------------------------------------------------------------------------


class _DoubleHeaderMiddleware(EDFHeaderMiddleware):
    """Doubles each byte value (clamped to 255). Same length — isometric."""

    targets = frozenset({"fuse", "api"})

    def transform_header(self, raw_header: bytes) -> bytes:
        return bytes(min(b * 2, 255) for b in raw_header)


class _UppercaseHeaderMiddleware(EDFHeaderMiddleware):
    """Uppercases ASCII bytes. FUSE-only target."""

    targets = frozenset({"fuse"})

    def transform_header(self, raw_header: bytes) -> bytes:
        return raw_header.upper()


class _HalfSignalMiddleware(EDFFullFileMiddleware):
    """Halves the signal data (non-isometric)."""

    targets = frozenset({"fuse", "api"})

    def transform(self, raw_header, raw_signals):
        return raw_header, raw_signals[: len(raw_signals) // 2]

    def compute_output_size(self, file_size, header_size):
        return header_size + (file_size - header_size) // 2


class _FuseOnlyFullMiddleware(EDFFullFileMiddleware):
    """Identity full-file middleware, FUSE-only target."""

    targets = frozenset({"fuse"})

    def transform(self, raw_header, raw_signals):
        return raw_header, raw_signals

    def compute_output_size(self, file_size, header_size):
        return file_size


# ---------------------------------------------------------------------------
# MiddlewarePipeline.is_empty / is_isometric / is_size_preserving
# ---------------------------------------------------------------------------


def test_empty_pipeline_is_empty():
    assert MiddlewarePipeline([]).is_empty


def test_non_empty_pipeline_is_not_empty():
    assert not MiddlewarePipeline([AnonymizeEDFHeader()]).is_empty


def test_header_only_pipeline_is_isometric():
    assert MiddlewarePipeline([AnonymizeEDFHeader()]).is_isometric


def test_full_file_pipeline_is_not_isometric():
    assert not MiddlewarePipeline([_HalfSignalMiddleware()]).is_isometric


def test_mixed_pipeline_is_not_isometric():
    assert not MiddlewarePipeline([AnonymizeEDFHeader(), _HalfSignalMiddleware()]).is_isometric


def test_strip_annotation_pipeline_is_not_isometric():
    """StripAnnotationTextMiddleware is EDFSignalMiddleware → not isometric."""
    p = MiddlewarePipeline([AnonymizeEDFHeader(), StripAnnotationTextMiddleware()])
    assert not p.is_isometric


def test_strip_annotation_pipeline_is_size_preserving():
    """StripAnnotationTextMiddleware is size_invariant → pipeline is size-preserving."""
    p = MiddlewarePipeline([AnonymizeEDFHeader(), StripAnnotationTextMiddleware()])
    assert p.is_size_preserving


def test_header_only_pipeline_is_size_preserving():
    assert MiddlewarePipeline([AnonymizeEDFHeader()]).is_size_preserving


def test_full_file_pipeline_is_not_size_preserving():
    assert not MiddlewarePipeline([_HalfSignalMiddleware()]).is_size_preserving


def test_drop_channels_pipeline_is_not_size_preserving():
    """DropChannelsMiddleware changes record size → not size-preserving."""
    assert not MiddlewarePipeline([DropChannelsMiddleware(["EEG"])]).is_size_preserving


def test_size_invariant_flag():
    assert StripAnnotationTextMiddleware.size_invariant is True
    assert DropChannelsMiddleware(["X"]).size_invariant is False
    assert DownsampleMiddleware(factor=2).size_invariant is False


# ---------------------------------------------------------------------------
# MiddlewarePipeline.for_scope
# ---------------------------------------------------------------------------


def test_for_scope_fuse_includes_fuse_only_and_both():
    pipeline = MiddlewarePipeline([_UppercaseHeaderMiddleware(), _HalfSignalMiddleware()])
    fuse = pipeline.for_scope("fuse")
    # Both have "fuse" in targets — neither dropped
    assert not fuse.is_isometric  # _HalfSignalMiddleware survives


def test_for_scope_api_excludes_fuse_only_header_middleware():
    pipeline = MiddlewarePipeline([_UppercaseHeaderMiddleware(), _HalfSignalMiddleware()])
    api = pipeline.for_scope("api")
    # _UppercaseHeaderMiddleware has targets={"fuse"} → excluded
    # _HalfSignalMiddleware has targets={"fuse","api"} → included
    assert not api.is_isometric


def test_for_scope_api_excludes_fuse_only_full_middleware():
    pipeline = MiddlewarePipeline([_FuseOnlyFullMiddleware()])
    api = pipeline.for_scope("api")
    assert api.is_empty


def test_for_scope_returns_new_pipeline_instance():
    pipeline = MiddlewarePipeline([AnonymizeEDFHeader()])
    assert pipeline.for_scope("fuse") is not pipeline


# ---------------------------------------------------------------------------
# MiddlewarePipeline.compute_output_size
# ---------------------------------------------------------------------------


def test_isometric_pipeline_size_unchanged():
    pipeline = MiddlewarePipeline([AnonymizeEDFHeader()])
    assert pipeline.compute_output_size(file_size=100_000, header_size=8448) == 100_000


def test_full_file_pipeline_halves_signal_size():
    pipeline = MiddlewarePipeline([_HalfSignalMiddleware()])
    expected = 8448 + (100_000 - 8448) // 2
    assert pipeline.compute_output_size(file_size=100_000, header_size=8448) == expected


def test_chained_full_file_middlewares_compute_cumulatively():
    pipeline = MiddlewarePipeline([_HalfSignalMiddleware(), _HalfSignalMiddleware()])
    first_size = 8448 + (100_000 - 8448) // 2
    expected = 8448 + (first_size - 8448) // 2
    assert pipeline.compute_output_size(file_size=100_000, header_size=8448) == expected


def test_empty_pipeline_size_unchanged():
    assert MiddlewarePipeline([]).compute_output_size(50_000, 256) == 50_000


# ---------------------------------------------------------------------------
# MiddlewarePipeline.apply_header
# ---------------------------------------------------------------------------


def test_apply_header_single_middleware():
    pipeline = MiddlewarePipeline([_DoubleHeaderMiddleware()])
    result = pipeline.apply_header(bytes([1, 2, 3, 4]))
    assert result == bytes([2, 4, 6, 8])


def test_apply_header_chained_middlewares_run_in_order():
    # _UppercaseHeaderMiddleware runs first, then _DoubleHeaderMiddleware
    pipeline = MiddlewarePipeline([_UppercaseHeaderMiddleware(), _DoubleHeaderMiddleware()])
    raw = b"abc"
    result = pipeline.apply_header(raw)
    # uppercase: b"ABC" = [65, 66, 67], double: [130, 132, 134]
    assert result == bytes([65 * 2, 66 * 2, 67 * 2])


def test_apply_header_skips_full_file_middleware():
    full_mw = MagicMock(spec=_HalfSignalMiddleware)
    full_mw.targets = frozenset({"fuse"})
    pipeline = MiddlewarePipeline([full_mw])
    pipeline.apply_header(b"header")
    full_mw.transform.assert_not_called()


def test_apply_header_empty_pipeline_returns_raw():
    raw = b"unchanged"
    assert MiddlewarePipeline([]).apply_header(raw) == raw


# ---------------------------------------------------------------------------
# MiddlewarePipeline.apply_full
# ---------------------------------------------------------------------------


def test_apply_full_header_middlewares_run_before_full_file():
    calls = []

    class _TrackHeader(EDFHeaderMiddleware):
        targets = frozenset({"fuse"})

        def transform_header(self, h):
            calls.append("header")
            return h

    class _TrackFull(EDFFullFileMiddleware):
        targets = frozenset({"fuse"})

        def transform(self, h, s):
            calls.append("full")
            return h, s

        def compute_output_size(self, fs, hs):
            return fs

    MiddlewarePipeline([_TrackHeader(), _TrackFull()]).apply_full(b"h" * 256, b"s" * 1024)
    assert calls == ["header", "full"]


def test_apply_full_transforms_signals():
    pipeline = MiddlewarePipeline([_HalfSignalMiddleware()])
    raw_header = b"H" * 256
    raw_signals = b"S" * 512
    new_header, new_signals = pipeline.apply_full(raw_header, raw_signals)
    assert new_header == raw_header
    assert new_signals == raw_signals[:256]


# ---------------------------------------------------------------------------
# AnonymizeEDFHeader
# ---------------------------------------------------------------------------


def test_anonymize_targets_fuse_and_api():
    assert "fuse" in AnonymizeEDFHeader.targets
    assert "api" in AnonymizeEDFHeader.targets


def test_anonymize_returns_same_length_on_garbage_input():
    anon = AnonymizeEDFHeader()
    raw = b"\x00" * 256
    assert len(anon.transform_header(raw)) == len(raw)


def _make_fingerprinted_header():
    """A raw header carrying every channel-block fingerprint class the middleware must clean."""
    from recordings.tests.test_edf_processor import _make_edf_header

    return _make_edf_header(
        signals=[
            {
                "label": "EEG Fp1-Cz",
                "transducer": "AgAgCl sintered VendorCorp",
                "prefiltering": "HP:0.1Hz LP:75Hz",
                "sample_count": 16,
            },
            {
                "label": "XYZ99",
                "transducer": "VendorCorp DC box",
                "prefiltering": "0.53-70 Hz",
                "sample_count": 16,
            },
        ]
    )


def test_anonymize_cleans_channel_labels():
    from recordings.processors.edf import parse_edf_header, parse_signal_infos

    out = AnonymizeEDFHeader().transform_header(_make_fingerprinted_header())
    infos = parse_signal_infos(out, parse_edf_header(out))
    assert [si.label for si in infos] == ["Fp1", "MISC_1"]


def test_anonymize_blanks_transducer_and_reconstructs_prefiltering():
    from recordings.processors.edf import parse_edf_header, parse_signal_infos

    out = AnonymizeEDFHeader().transform_header(_make_fingerprinted_header())
    infos = parse_signal_infos(out, parse_edf_header(out))
    assert all(si.transducer_type == "" for si in infos)
    assert infos[0].prefiltering == "HP:0.1Hz LP:75Hz"
    assert infos[1].prefiltering == ""


def test_anonymize_removes_raw_channel_fingerprints_from_bytes():
    out = AnonymizeEDFHeader().transform_header(_make_fingerprinted_header())
    assert b"VendorCorp" not in out
    assert b"XYZ99" not in out
    assert b"0.53-70" not in out


def test_anonymize_channel_cleaning_is_isometric():
    raw = _make_fingerprinted_header()
    assert len(AnonymizeEDFHeader().transform_header(raw)) == len(raw)


def test_anonymize_preserves_annotation_channel_label():
    from recordings.processors.edf import parse_edf_header, parse_signal_infos
    from recordings.tests.test_edf_processor import _make_edf_header

    raw = _make_edf_header(
        reserved="EDF+C",
        signals=[
            {"label": "Fp1", "sample_count": 16},
            {"label": "EDF Annotations", "sample_count": 16},
        ],
    )
    out = AnonymizeEDFHeader().transform_header(raw)
    infos = parse_signal_infos(out, parse_edf_header(out))
    assert infos[1].label == "EDF Annotations"
    assert infos[1].is_annotation_channel


# ---------------------------------------------------------------------------
# EDFSignalMiddleware — pipeline properties
# ---------------------------------------------------------------------------


class _DropFirstChannel(EDFSignalMiddleware):
    """Drops the first signal channel. Used as a test fixture."""

    targets = frozenset({"fuse", "api"})

    def transform_header(self, raw_header: bytes) -> bytes:
        from recordings.processors.edf import (
            build_header,
            parse_edf_header,
            parse_signal_infos,
        )

        hdr = parse_edf_header(raw_header)
        infos = parse_signal_infos(raw_header, hdr)
        kept = infos[1:] if len(infos) > 1 else infos
        hdr.signal_count = len(kept)
        return build_header(hdr, kept)

    def output_record_size(self, signal_infos, bytes_per_sample):
        return sum(si.sample_count * bytes_per_sample for si in signal_infos[1:])

    def transform_record(self, record_bytes, signal_infos, bytes_per_sample):
        offset = signal_infos[0].sample_count * bytes_per_sample
        return record_bytes[offset:]


def test_signal_middleware_pipeline_is_not_isometric():
    assert not MiddlewarePipeline([_DropFirstChannel()]).is_isometric


def test_signal_middleware_pipeline_has_signal_middleware():
    assert MiddlewarePipeline([_DropFirstChannel()]).has_signal_middleware


def test_header_only_pipeline_has_no_signal_middleware():
    assert not MiddlewarePipeline([AnonymizeEDFHeader()]).has_signal_middleware


def test_mixed_header_and_signal_has_signal_middleware():
    assert MiddlewarePipeline([AnonymizeEDFHeader(), _DropFirstChannel()]).has_signal_middleware


def test_full_file_middleware_not_counted_as_signal_middleware():
    assert not MiddlewarePipeline([_HalfSignalMiddleware()]).has_signal_middleware


# ---------------------------------------------------------------------------
# build_signal_context
# ---------------------------------------------------------------------------


def _make_two_channel_edf():
    """Return a minimal 2-channel EDF header + one data record (bytes)."""
    from recordings.tests.test_edf_processor import _make_edf_data, _make_edf_header

    signals = [
        {"label": "EEG Fp1", "sample_count": 4},
        {"label": "EEG Fp2", "sample_count": 8},
    ]
    header = _make_edf_header(signals=signals, n_records=2)
    data = _make_edf_data(signals, n_records=2)
    return header, data, signals


def test_build_signal_context_no_signal_mw_preserves_sizes():
    header, data, signals = _make_two_channel_edf()
    pipeline = MiddlewarePipeline([AnonymizeEDFHeader()])
    ctx = pipeline.build_signal_context(header, n_records=2)
    # No signal middleware — input == output sizes.
    assert ctx.input_record_size == ctx.output_record_size
    assert ctx.n_records == 2
    # Full file size = new header + 2 * record_size.
    assert ctx.output_file_size == ctx.new_header_size + 2 * ctx.output_record_size


def test_build_signal_context_drop_first_channel():
    header, data, signals = _make_two_channel_edf()
    pipeline = MiddlewarePipeline([_DropFirstChannel()])
    ctx = pipeline.build_signal_context(header, n_records=2)
    # Input record: ch0(4*2) + ch1(8*2) = 8 + 16 = 24 bytes.
    assert ctx.input_record_size == (4 + 8) * 2
    # Output record: ch1 only = 8*2 = 16 bytes.
    assert ctx.output_record_size == 8 * 2
    assert ctx.new_header_size < len(header)  # fewer channels → smaller header


def test_signal_context_transform_record_drops_first_channel():
    header, data, signals = _make_two_channel_edf()
    pipeline = MiddlewarePipeline([_DropFirstChannel()])
    ctx = pipeline.build_signal_context(header, n_records=2)
    # Build one record: 4 samples ch0 + 8 samples ch1, each 2 bytes.
    ch0 = b"\x01\x00" * 4
    ch1 = b"\x02\x00" * 8
    record = ch0 + ch1
    result = ctx.transform_record(record)
    assert result == ch1
    assert len(result) == ctx.output_record_size


# ---------------------------------------------------------------------------
# DropChannelsMiddleware
# ---------------------------------------------------------------------------


def test_drop_channels_transform_header_reduces_ns():
    from recordings.processors.edf import parse_edf_header, parse_signal_infos
    from recordings.tests.test_edf_processor import _make_edf_header

    signals = [
        {"label": "EEG Fp1", "sample_count": 4},
        {"label": "EEG Fp2", "sample_count": 4},
        {"label": "EMG chin", "sample_count": 8},
    ]
    header = _make_edf_header(signals=signals)
    mw = DropChannelsMiddleware(["EMG chin"])
    new_header = mw.transform_header(header)

    parsed = parse_edf_header(new_header)
    assert parsed.signal_count == 2
    infos = parse_signal_infos(new_header, parsed)
    labels = [si.label for si in infos]
    assert "EEG Fp1" in labels
    assert "EEG Fp2" in labels
    assert not any("EMG" in lbl for lbl in labels)


def test_drop_channels_transform_record_extracts_kept_channels():
    from recordings.processors.edf import parse_edf_header, parse_signal_infos
    from recordings.tests.test_edf_processor import _make_edf_header

    signals = [
        {"label": "EEG Fp1", "sample_count": 4},
        {"label": "EMG chin", "sample_count": 2},
        {"label": "EEG Fp2", "sample_count": 4},
    ]
    header_bytes = _make_edf_header(signals=signals)
    hdr = parse_edf_header(header_bytes)
    sig_infos = parse_signal_infos(header_bytes, hdr)

    ch0 = b"\x01\x00" * 4  # EEG Fp1, 4 samples
    ch1 = b"\x02\x00" * 2  # EMG chin, 2 samples (to be dropped)
    ch2 = b"\x03\x00" * 4  # EEG Fp2, 4 samples
    record = ch0 + ch1 + ch2

    mw = DropChannelsMiddleware(["EMG chin"])
    result = mw.transform_record(record, sig_infos, bytes_per_sample=2)
    assert result == ch0 + ch2
    assert mw.output_record_size(sig_infos, 2) == len(result)


def test_drop_channels_never_drops_annotation_channel():
    from recordings.processors.edf import parse_edf_header, parse_signal_infos
    from recordings.tests.test_edf_processor import _make_edf_header

    signals = [
        {"label": "EEG Fp1", "sample_count": 4},
        {"label": "edf annotations", "sample_count": 40},
    ]
    header_bytes = _make_edf_header(signals=signals)
    hdr = parse_edf_header(header_bytes)
    parse_signal_infos(header_bytes, hdr)

    mw = DropChannelsMiddleware(["edf annotations"])  # try to drop annotation channel
    new_header = mw.transform_header(header_bytes)
    parsed = parse_edf_header(new_header)
    # Annotation channel must not be removed.
    assert parsed.signal_count == 2


def test_drop_channels_case_insensitive():
    from recordings.processors.edf import parse_edf_header
    from recordings.tests.test_edf_processor import _make_edf_header

    signals = [
        {"label": "EEG Fp1", "sample_count": 4},
        {"label": "EMG Chin", "sample_count": 2},
    ]
    header_bytes = _make_edf_header(signals=signals)
    mw = DropChannelsMiddleware(["emg chin"])  # lowercase
    new_header = mw.transform_header(header_bytes)
    assert parse_edf_header(new_header).signal_count == 1


# ---------------------------------------------------------------------------
# DownsampleMiddleware
# ---------------------------------------------------------------------------


def test_downsample_rejects_factor_less_than_2():
    with pytest.raises(ValueError):
        DownsampleMiddleware(factor=1)


def test_downsample_transform_header_reduces_sample_counts():
    from recordings.processors.edf import parse_edf_header, parse_signal_infos
    from recordings.tests.test_edf_processor import _make_edf_header

    signals = [
        {"label": "EEG Fp1", "sample_count": 256},
        {"label": "EEG Fp2", "sample_count": 256},
    ]
    header_bytes = _make_edf_header(signals=signals)
    mw = DownsampleMiddleware(factor=4)
    new_header = mw.transform_header(header_bytes)

    parsed = parse_edf_header(new_header)
    infos = parse_signal_infos(new_header, parsed)
    for si in infos:
        assert si.sample_count == 64


def test_downsample_selective_channels():
    from recordings.processors.edf import parse_edf_header, parse_signal_infos
    from recordings.tests.test_edf_processor import _make_edf_header

    signals = [
        {"label": "EEG Fp1", "sample_count": 256},
        {"label": "EMG chin", "sample_count": 512},
    ]
    header_bytes = _make_edf_header(signals=signals)
    mw = DownsampleMiddleware(factor=4, channels=["EEG Fp1"])
    new_header = mw.transform_header(header_bytes)

    parsed = parse_edf_header(new_header)
    infos = parse_signal_infos(new_header, parsed)
    by_label = {si.label.strip(): si.sample_count for si in infos}
    assert by_label["EEG Fp1"] == 64  # downsampled
    assert by_label["EMG chin"] == 512  # unchanged


def test_downsample_transform_record_decimates():
    from recordings.processors.edf import parse_edf_header, parse_signal_infos
    from recordings.tests.test_edf_processor import _make_edf_header

    signals = [{"label": "EEG Fp1", "sample_count": 8}]
    header_bytes = _make_edf_header(signals=signals)
    hdr = parse_edf_header(header_bytes)
    sig_infos = parse_signal_infos(header_bytes, hdr)

    # 8 samples, 2 bytes each → record is 16 bytes.
    # Samples (little-endian int16): 0,1,2,3,4,5,6,7.
    record = b"".join(i.to_bytes(2, "little") for i in range(8))

    mw = DownsampleMiddleware(factor=2)
    result = mw.transform_record(record, sig_infos, bytes_per_sample=2)

    # Should keep samples 0,2,4,6 (every 2nd).
    expected = b"".join(i.to_bytes(2, "little") for i in [0, 2, 4, 6])
    assert result == expected
    assert mw.output_record_size(sig_infos, 2) == len(result)


def test_downsample_skips_annotation_channel():
    from recordings.processors.edf import parse_edf_header, parse_signal_infos
    from recordings.tests.test_edf_processor import _make_edf_header

    signals = [
        {"label": "EEG Fp1", "sample_count": 8},
        {"label": "edf annotations", "sample_count": 40},
    ]
    header_bytes = _make_edf_header(signals=signals)
    hdr = parse_edf_header(header_bytes)
    parse_signal_infos(header_bytes, hdr)

    mw = DownsampleMiddleware(factor=2)  # all channels
    new_header = mw.transform_header(header_bytes)
    new_hdr = parse_edf_header(new_header)
    new_infos = parse_signal_infos(new_header, new_hdr)

    by_label = {si.label.strip(): si.sample_count for si in new_infos}
    assert by_label["EEG Fp1"] == 4  # downsampled
    assert by_label["edf annotations"] == 40  # unchanged


def test_downsample_output_record_size_consistent():
    from recordings.processors.edf import parse_edf_header, parse_signal_infos
    from recordings.tests.test_edf_processor import _make_edf_header

    signals = [
        {"label": "EEG Fp1", "sample_count": 8},
        {"label": "EEG Fp2", "sample_count": 16},
    ]
    header_bytes = _make_edf_header(signals=signals)
    hdr = parse_edf_header(header_bytes)
    sig_infos = parse_signal_infos(header_bytes, hdr)

    mw = DownsampleMiddleware(factor=4)
    record = b"\x00\x00" * (8 + 16)
    result = mw.transform_record(record, sig_infos, bytes_per_sample=2)
    assert len(result) == mw.output_record_size(sig_infos, 2)


# ---------------------------------------------------------------------------
# SignalPipelineContext: chained middlewares
# ---------------------------------------------------------------------------


def test_chained_drop_and_downsample():
    """Drop one channel then downsample — context correctly chains both steps."""
    from recordings.processors.edf import parse_edf_header, parse_signal_infos
    from recordings.tests.test_edf_processor import _make_edf_header

    signals = [
        {"label": "EEG Fp1", "sample_count": 8},
        {"label": "EMG chin", "sample_count": 4},
        {"label": "EEG Fp2", "sample_count": 8},
    ]
    header_bytes = _make_edf_header(signals=signals)

    pipeline = MiddlewarePipeline(
        [
            DropChannelsMiddleware(["EMG chin"]),
            DownsampleMiddleware(factor=2),
        ]
    )
    ctx = pipeline.build_signal_context(header_bytes, n_records=1)

    # Input record: 8+4+8 channels * 2 bytes = 40 bytes.
    assert ctx.input_record_size == (8 + 4 + 8) * 2

    # After drop: EEG Fp1(8) + EEG Fp2(8) = 32 bytes per input record to step 2.
    # After downsample factor=2: EEG Fp1(4) + EEG Fp2(4) = 16 bytes output.
    assert ctx.output_record_size == (4 + 4) * 2

    # The new header should have 2 channels with sample_count=4 each.
    new_hdr = parse_edf_header(ctx.new_header)
    new_infos = parse_signal_infos(ctx.new_header, new_hdr)
    assert new_hdr.signal_count == 2
    for si in new_infos:
        assert si.sample_count == 4

    # Verify record transform end-to-end.
    eeg_fp1 = b"".join(i.to_bytes(2, "little") for i in range(8))  # 8 samples
    emg = b"\xff\xff" * 4  # 4 samples (to be dropped)
    eeg_fp2 = b"".join((i + 8).to_bytes(2, "little") for i in range(8))  # 8 samples
    record = eeg_fp1 + emg + eeg_fp2

    result = ctx.transform_record(record)
    # Expected: Fp1[0,2,4,6] + Fp2[0,2,4,6]
    expected_fp1 = b"".join(i.to_bytes(2, "little") for i in [0, 2, 4, 6])
    expected_fp2 = b"".join((i + 8).to_bytes(2, "little") for i in [0, 2, 4, 6])
    assert result == expected_fp1 + expected_fp2


# ---------------------------------------------------------------------------
# StripAnnotationTextMiddleware
# ---------------------------------------------------------------------------


def _make_edf_plus_header(n_eeg_samples=16, anno_sample_count=60):
    """Return header bytes for a 2-channel EDF+C file (1 EEG + 1 annotation)."""
    from recordings.tests.test_edf_processor import _make_edf_header

    signals = [
        {"label": "EEG Fp1", "sample_count": n_eeg_samples},
        {"label": "EDF Annotations", "sample_count": anno_sample_count},
    ]
    return _make_edf_header(reserved="EDF+C", signals=signals)


def _make_anno_bytes(onset: float, text: str, total_bytes: int) -> bytes:
    """Build annotation channel bytes: timekeeping TAL + one text TAL, null-padded."""
    from recordings.processors.edf import _encode_tal_entry, _encode_timekeeping_tal

    content = _encode_timekeeping_tal(onset) + _encode_tal_entry(onset, 0.0, text)
    assert len(content) <= total_bytes, "text TAL too large for channel"
    return content + bytes(total_bytes - len(content))


class TestStripAnnotationTextMiddleware:
    def test_transform_header_is_identity(self):
        """transform_header returns the header unchanged."""
        hdr = _make_edf_plus_header()
        m = StripAnnotationTextMiddleware()
        assert m.transform_header(hdr) == hdr

    def test_output_record_size_equals_input(self):
        """output_record_size matches sum of sample_count * bps for all channels."""
        from recordings.processors.edf import parse_edf_header, parse_signal_infos

        hdr = _make_edf_plus_header(n_eeg_samples=16, anno_sample_count=60)
        header = parse_edf_header(hdr)
        sig_infos = parse_signal_infos(hdr, header)
        m = StripAnnotationTextMiddleware()
        # 2 bytes per sample (EDF): (16 + 60) * 2 = 152
        assert m.output_record_size(sig_infos, 2) == (16 + 60) * 2

    def test_strip_removes_text_from_annotation_channel(self):
        """transform_record removes text TALs; timekeeping TAL survives."""
        from recordings.processors.edf import (
            _parse_tal_record,
            parse_edf_header,
            parse_signal_infos,
        )

        anno_bytes_count = 60 * 2  # sample_count=60, bps=2
        hdr = _make_edf_plus_header(n_eeg_samples=8, anno_sample_count=60)
        header = parse_edf_header(hdr)
        sig_infos = parse_signal_infos(hdr, header)

        eeg_data = b"\x01\x02" * 8  # 8 EEG samples
        anno_data = _make_anno_bytes(5.0, "seizure onset", anno_bytes_count)
        record = eeg_data + anno_data

        m = StripAnnotationTextMiddleware()
        result = m.transform_record(record, sig_infos, 2)

        # Record size must be unchanged.
        assert len(result) == len(record)

        # EEG channel bytes must be untouched.
        assert result[: len(eeg_data)] == eeg_data

        # Annotation channel: timekeeping TAL preserved, text stripped.
        anno_out = result[len(eeg_data) :]
        record_onset, annotations = _parse_tal_record(anno_out)
        assert record_onset == pytest.approx(5.0)
        assert annotations == []  # no text TALs

    def test_signal_channels_untouched(self):
        """Signal channel bytes are passed through without modification."""
        from recordings.processors.edf import parse_edf_header, parse_signal_infos

        anno_bytes_count = 40 * 2
        hdr = _make_edf_plus_header(n_eeg_samples=16, anno_sample_count=40)
        header = parse_edf_header(hdr)
        sig_infos = parse_signal_infos(hdr, header)

        eeg_data = bytes(range(32))  # 16 samples * 2 bytes, distinct bytes
        anno_data = _make_anno_bytes(0.0, "test label", anno_bytes_count)
        record = eeg_data + anno_data

        result = StripAnnotationTextMiddleware().transform_record(record, sig_infos, 2)
        assert result[: len(eeg_data)] == eeg_data

    def test_plain_edf_no_annotation_channel_passes_through(self):
        """Plain EDF (no annotation channel) — record returned unchanged."""
        from recordings.processors.edf import parse_edf_header, parse_signal_infos
        from recordings.tests.test_edf_processor import _make_edf_header

        signals = [{"label": "EEG Fp1", "sample_count": 8}]
        hdr = _make_edf_header(signals=signals)
        header = parse_edf_header(hdr)
        sig_infos = parse_signal_infos(hdr, header)

        record = b"\xaa\xbb" * 8
        result = StripAnnotationTextMiddleware().transform_record(record, sig_infos, 2)
        assert result == record

    def test_no_timekeeping_tal_leaves_channel_unchanged(self):
        """If no timekeeping TAL is found, annotation channel bytes are unchanged."""
        from recordings.processors.edf import parse_edf_header, parse_signal_infos

        anno_bytes_count = 40 * 2
        hdr = _make_edf_plus_header(n_eeg_samples=4, anno_sample_count=40)
        header = parse_edf_header(hdr)
        sig_infos = parse_signal_infos(hdr, header)

        # Anno channel contains only null bytes (no valid TAL at all).
        eeg_data = b"\x00\x00" * 4
        anno_data = bytes(anno_bytes_count)
        record = eeg_data + anno_data

        result = StripAnnotationTextMiddleware().transform_record(record, sig_infos, 2)
        # No timekeeping TAL → channel bytes left unchanged.
        assert result == record

    def test_build_signal_context_with_strip_and_anonymize(self):
        """Pipeline with AnonymizeEDFHeader + StripAnnotationTextMiddleware builds correctly."""
        from recordings.processors.edf import parse_edf_header

        hdr = _make_edf_plus_header(n_eeg_samples=16, anno_sample_count=40)
        pipeline = MiddlewarePipeline(
            [
                AnonymizeEDFHeader(),
                StripAnnotationTextMiddleware(),
            ]
        )
        ctx = pipeline.build_signal_context(hdr, n_records=3)

        # File size must be unchanged.
        assert ctx.input_record_size == ctx.output_record_size
        # new_header is the anonymized version.
        anon_hdr = parse_edf_header(ctx.new_header)
        assert "X X X X" in anon_hdr.patient_id

    def test_build_signal_context_robust_to_mocked_header_transform(self):
        """build_signal_context works even when a header middleware returns non-EDF bytes."""
        from unittest.mock import patch

        hdr = _make_edf_plus_header(n_eeg_samples=8, anno_sample_count=30)
        sentinel = b"Z" * len(hdr)

        pipeline = MiddlewarePipeline(
            [
                AnonymizeEDFHeader(),
                StripAnnotationTextMiddleware(),
            ]
        )

        with patch.object(AnonymizeEDFHeader, "transform_header", return_value=sentinel):
            ctx = pipeline.build_signal_context(hdr, n_records=1)

        # new_header is the sentinel (mock output threaded through).
        assert ctx.new_header == sentinel
        # Record sizes are correct (derived from raw_header, not the sentinel).
        assert ctx.input_record_size == ctx.output_record_size
        assert ctx.input_record_size == (8 + 30) * 2


# ---------------------------------------------------------------------------
# transform_signal_infos
# ---------------------------------------------------------------------------


def _make_signal_infos(specs):
    """Build a list of _SignalInfoLike from a list of dicts."""
    return [
        _SignalInfoLike(
            label=s["label"],
            sample_count=s["sample_count"],
            is_annotation_channel=s.get("is_annotation_channel", False),
        )
        for s in specs
    ]


class TestTransformSignalInfos:
    """EDFSignalMiddleware.transform_signal_infos() for built-in middlewares."""

    def test_default_is_identity(self):
        """StripAnnotationTextMiddleware (no override) returns signal_infos unchanged."""
        infos = _make_signal_infos(
            [
                {"label": "EEG Fp1", "sample_count": 256},
                {
                    "label": "EDF Annotations",
                    "sample_count": 60,
                    "is_annotation_channel": True,
                },
            ]
        )
        result = StripAnnotationTextMiddleware().transform_signal_infos(infos)
        assert result is infos  # exact same list object

    def test_drop_channels_removes_label(self):
        infos = _make_signal_infos(
            [
                {"label": "EEG Fp1", "sample_count": 256},
                {"label": "EMG chin", "sample_count": 64},
                {"label": "EEG Fp2", "sample_count": 256},
            ]
        )
        result = DropChannelsMiddleware(["EMG chin"]).transform_signal_infos(infos)
        assert len(result) == 2
        labels = [si.label for si in result]
        assert "EEG Fp1" in labels
        assert "EEG Fp2" in labels
        assert "EMG chin" not in labels

    def test_drop_channels_never_drops_annotation_channel(self):
        infos = _make_signal_infos(
            [
                {
                    "label": "EDF Annotations",
                    "sample_count": 60,
                    "is_annotation_channel": True,
                },
                {"label": "EEG Fp1", "sample_count": 256},
            ]
        )
        result = DropChannelsMiddleware(["EDF Annotations"]).transform_signal_infos(infos)
        assert len(result) == 2  # annotation channel preserved

    def test_downsample_updates_sample_counts(self):
        infos = _make_signal_infos(
            [
                {"label": "EEG Fp1", "sample_count": 256},
                {"label": "EEG Fp2", "sample_count": 128},
            ]
        )
        result = DownsampleMiddleware(factor=4).transform_signal_infos(infos)
        assert len(result) == 2
        assert result[0].sample_count == 64  # 256 // 4
        assert result[1].sample_count == 32  # 128 // 4

    def test_downsample_skips_annotation_channel(self):
        infos = _make_signal_infos(
            [
                {"label": "EEG Fp1", "sample_count": 256},
                {
                    "label": "EDF Annotations",
                    "sample_count": 60,
                    "is_annotation_channel": True,
                },
            ]
        )
        result = DownsampleMiddleware(factor=2).transform_signal_infos(infos)
        assert result[0].sample_count == 128
        assert result[1].sample_count == 60  # annotation channel unchanged


# ---------------------------------------------------------------------------
# build_signal_context_from_infos
# ---------------------------------------------------------------------------


class TestBuildSignalContextFromInfos:
    """MiddlewarePipeline.build_signal_context_from_infos()."""

    def _two_channel_infos(self):
        return _make_signal_infos(
            [
                {"label": "EEG Fp1", "sample_count": 4},
                {"label": "EEG Fp2", "sample_count": 8},
            ]
        )

    def test_no_signal_middleware_sizes_match_header_only_pipeline(self):
        """Without signal middleware the context matches the header-only case."""
        infos = self._two_channel_infos()
        bps = 2
        n_records = 3
        header_size = 256 * (1 + len(infos))

        pipeline = MiddlewarePipeline([AnonymizeEDFHeader()])
        ctx = pipeline.build_signal_context_from_infos(infos, bps, n_records, header_size)

        expected_rec_size = (4 + 8) * bps
        assert ctx.input_record_size == expected_rec_size
        assert ctx.output_record_size == expected_rec_size
        assert ctx.n_records == n_records
        assert ctx.output_file_size == header_size + n_records * expected_rec_size
        assert ctx.new_header is None  # no raw_header provided

    def test_drop_channels_output_record_size_and_header_size(self):
        infos = self._two_channel_infos()
        bps = 2
        n_records = 2
        header_size = 256 * (1 + len(infos))  # 3 * 256 = 768

        pipeline = MiddlewarePipeline([DropChannelsMiddleware(["EEG Fp1"])])
        ctx = pipeline.build_signal_context_from_infos(infos, bps, n_records, header_size)

        # Input: ch0(4) + ch1(8) = 24 bytes.
        assert ctx.input_record_size == (4 + 8) * bps
        # Output: ch1 only = 8 * 2 = 16 bytes.
        assert ctx.output_record_size == 8 * bps
        # Output header: (1 remaining channel + 1) * 256 = 512 bytes.
        assert ctx.new_header_size == 2 * 256
        assert ctx.output_file_size == 2 * 256 + n_records * 8 * bps

    def test_downsample_output_record_size(self):
        infos = _make_signal_infos(
            [
                {"label": "EEG Fp1", "sample_count": 256},
                {"label": "EEG Fp2", "sample_count": 128},
            ]
        )
        bps = 2
        n_records = 10
        header_size = 256 * 3

        pipeline = MiddlewarePipeline([DownsampleMiddleware(factor=2)])
        ctx = pipeline.build_signal_context_from_infos(infos, bps, n_records, header_size)

        assert ctx.input_record_size == (256 + 128) * bps
        assert ctx.output_record_size == (128 + 64) * bps
        assert ctx.new_header_size == header_size  # channel count unchanged
        assert ctx.output_file_size == header_size + n_records * (128 + 64) * bps

    def test_chained_drop_then_downsample(self):
        infos = _make_signal_infos(
            [
                {"label": "EEG Fp1", "sample_count": 8},
                {"label": "EMG chin", "sample_count": 4},
                {"label": "EEG Fp2", "sample_count": 8},
            ]
        )
        bps = 2
        pipeline = MiddlewarePipeline(
            [
                DropChannelsMiddleware(["EMG chin"]),
                DownsampleMiddleware(factor=2),
            ]
        )
        ctx = pipeline.build_signal_context_from_infos(infos, bps, n_records=1, header_size=1024)

        assert ctx.input_record_size == (8 + 4 + 8) * bps  # 40 bytes
        assert ctx.output_record_size == (4 + 4) * bps  # 16 bytes (after drop+downsample)
        assert ctx.new_header_size == 256 * 3  # 2 output channels + 1

    def test_with_raw_header_populates_new_header(self):
        """When raw_header is provided, new_header is populated by apply_header."""
        from recordings.processors.edf import parse_edf_header, parse_signal_infos
        from recordings.tests.test_edf_processor import _make_edf_header

        signals_spec = [
            {"label": "EEG Fp1", "sample_count": 4},
            {"label": "EEG Fp2", "sample_count": 8},
        ]
        raw_header = _make_edf_header(signals=signals_spec)
        hdr = parse_edf_header(raw_header)
        infos = parse_signal_infos(raw_header, hdr)
        bps = 2
        n_records = 2
        header_size = len(raw_header)

        pipeline = MiddlewarePipeline([AnonymizeEDFHeader()])
        ctx_from_bytes = pipeline.build_signal_context(raw_header, n_records)
        ctx_from_infos = pipeline.build_signal_context_from_infos(
            infos, bps, n_records, header_size, raw_header=raw_header
        )

        assert ctx_from_infos.new_header == ctx_from_bytes.new_header
        assert ctx_from_infos.output_file_size == ctx_from_bytes.output_file_size

    def test_sizes_match_build_signal_context_for_drop_pipeline(self):
        """build_signal_context_from_infos and build_signal_context agree on sizes."""
        from recordings.processors.edf import parse_edf_header, parse_signal_infos
        from recordings.tests.test_edf_processor import _make_edf_header

        signals_spec = [
            {"label": "EEG Fp1", "sample_count": 64},
            {"label": "EEG Fp2", "sample_count": 64},
            {"label": "EMG chin", "sample_count": 32},
        ]
        raw_header = _make_edf_header(signals=signals_spec)
        hdr = parse_edf_header(raw_header)
        infos = parse_signal_infos(raw_header, hdr)
        bps = 2
        n_records = 5
        header_size = len(raw_header)

        pipeline = MiddlewarePipeline([DropChannelsMiddleware(["EMG chin"])])
        ctx_bytes = pipeline.build_signal_context(raw_header, n_records)
        ctx_infos = pipeline.build_signal_context_from_infos(infos, bps, n_records, header_size)

        assert ctx_infos.input_record_size == ctx_bytes.input_record_size
        assert ctx_infos.output_record_size == ctx_bytes.output_record_size
        assert ctx_infos.new_header_size == ctx_bytes.new_header_size
        assert ctx_infos.output_file_size == ctx_bytes.output_file_size
