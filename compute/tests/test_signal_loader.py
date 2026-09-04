"""Tests for the concrete signal loader (``compute/signal_loader.py``).

No database, no MNE, and no real EDF: ``_window_bounds``, ``_mne_edf_gain`` and
``channel_scale`` are pure, and ``load_signal_window`` is exercised with a fake
``AnalysisRun`` (a ``SimpleNamespace``), a monkeypatched ``read_header`` supplying
header signal metadata, and a monkeypatched ``_read_raw`` returning a small
in-memory fake raw.

The header and the reader are patched *separately* on purpose: the loader treats the
header as the authority and MNE as a reader that has already applied its own partial
scaling, so the interesting failures are exactly the ones where the two disagree —
a channel count mismatch, an fs mismatch, a dimension MNE declined to rescale. A
single fixture that derived both from one source could not express any of them.
``compute/signal_smoke.py`` covers the other half, where a real header meets real MNE.

The same split applies to the splice guard. ``read_splice_positions`` is patched rather
than fed a real discontinuous EDF, because what belongs to *this* module is the
arithmetic around the read — the marker gate, the two refusals, the record range, and
the strictly-inside filter — and none of that is visible when the answer comes out of a
file. What a given record timeline *means* is the reader's contract and is pinned where
the reader lives (``recordings/tests/test_edf_processor.py::TestReadRecordGaps``); that
the two are call-compatible is asserted here directly, so patching cannot hide a
signature drift.
"""

import inspect
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from compute import signal_loader
from compute.contract import SignalWindow
from compute.segmentation import SegmentPlan
from recordings.processors.channel_labels import classify_channel
from recordings.processors.edf import (
    EdfSignalInfo,
    extract_signal_type,
    read_splice_positions,
)
from recordings.processors.units import GENERIC_UNIT


def _seg(context_start_s, context_end_s, index=0):
    """A SegmentPlan whose interior equals its context (interior bounds are unused
    by the loader — only the context window is read)."""
    return SegmentPlan(
        index=index,
        interior_start_s=context_start_s,
        interior_end_s=context_end_s,
        context_start_s=context_start_s,
        context_end_s=context_end_s,
    )


def _info(label, unit="uV", *, rate=100.0, annotation=False):
    """One header signal: only the fields the loader reads are meaningful."""
    return EdfSignalInfo(
        label=label,
        transducer_type="",
        physical_unit=unit,
        physical_min=-1.0,
        physical_max=1.0,
        digital_min=-32768,
        digital_max=32767,
        prefiltering="",
        sample_count=int(rate),
        reserved="",
        sampling_rate=rate,
        is_annotation_channel=annotation,
    )


class _FakeRaw:
    """Minimal stand-in for an MNE Raw: sfreq, n_times, ch_names, and a sample-range
    ``get_data`` returning whatever MNE would return — i.e. already multiplied by the
    gain MNE assigns the channel's dimension, which for an unrecognised dimension is 1.
    """

    def __init__(self, data, ch_names, sfreq):
        self._data = np.asarray(data, dtype=float)
        self.ch_names = list(ch_names)
        self.info = {"sfreq": float(sfreq)}
        self.n_times = self._data.shape[1]

    def get_data(self, start=0, stop=None):
        stop = self.n_times if stop is None else stop
        # A copy, as MNE's does: the loader scales rows in place.
        return self._data[:, start:stop].copy()


def _run(path):
    return SimpleNamespace(
        pk=1,
        input_version_id=signal_loader.SOURCE_VERSION_ID,
        recording_id=7,
        recording=SimpleNamespace(file_path=str(path)),
    )


def _header(infos, *, discontinuous=False, record_duration=1.0):
    """A fake general header carrying only the fields the loader reads.

    ``discontinuous`` and ``data_record_duration`` are here because the splice guard
    consults the EDF+D marker before anything else: a stand-in header without them makes
    every window read fail on an ``AttributeError`` that has nothing to do with the test.
    They default to a continuous, one-second-record file — the overwhelmingly common case,
    and the one where the guard costs no I/O.
    """
    return SimpleNamespace(
        signal_infos=infos,
        discontinuous=discontinuous,
        data_record_duration=record_duration,
    )


def _wire(
    monkeypatch,
    tmp_path,
    infos,
    data,
    ch_names=None,
    sfreq=100.0,
    *,
    discontinuous=False,
    record_duration=1.0,
):
    """Point the loader at a fake header and a fake reader; return the run."""
    f = tmp_path / "r.edf"
    f.write_bytes(b"x")
    if ch_names is None:
        ch_names = [i.label for i in infos if not i.is_annotation_channel]
    header = _header(infos, discontinuous=discontinuous, record_duration=record_duration)
    monkeypatch.setattr(signal_loader, "read_header", lambda p: (header, infos))
    monkeypatch.setattr(signal_loader, "_read_raw", lambda p: _FakeRaw(data, ch_names, sfreq))
    return _run(f)


# ── _window_bounds (pure) ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "start_s, end_s, fs, n_total, exp_start, exp_stop, exp_t0",
    [
        (5.0, 10.0, 256.0, 100_000, 1280, 2560, 5.0),  # exact sample boundaries
        (-0.5, 2.0, 100.0, 100_000, 0, 200, 0.0),  # halo before start clamps to 0
        (98.0, 101.0, 100.0, 10_000, 9800, 10_000, 98.0),  # halo past end clamps to n_total
        (0.5, 1.5, 3.0, 100, 1, 5, 1.0 / 3.0),  # floor(start), ceil(stop)
    ],
)
def test_window_bounds(start_s, end_s, fs, n_total, exp_start, exp_stop, exp_t0):
    start, stop, t0 = signal_loader._window_bounds(start_s, end_s, fs, n_total)
    assert (start, stop) == (exp_start, exp_stop)
    assert t0 == pytest.approx(exp_t0)


def test_window_bounds_rejects_nonpositive_fs():
    with pytest.raises(ValueError):
        signal_loader._window_bounds(0.0, 1.0, 0.0, 100)


def test_window_bounds_rejects_empty_window():
    # Window entirely past the recording end -> stop clamps below start.
    with pytest.raises(ValueError):
        signal_loader._window_bounds(50.0, 51.0, 100.0, 100)


# ── _mne_edf_gain: the mirror of MNE's own table ──────────────────────────


@pytest.mark.parametrize(
    "dimension, gain",
    [
        ("uV", 1e-6),
        (" uV ", 1e-6),  # MNE strips, so we strip
        ("μV", 1e-6),  # Greek small letter mu
        ("µV", 1e-6),  # micro sign
        ("\x83\xcaV", 1e-6),  # sjis mu byte pair, latin-1 decoded
        ("mV", 1e-3),
        # Everything below is a dimension MNE does NOT recognise. Gain 1 means it
        # hands the number through untouched and then reports it as volts, which is
        # exactly the trap the loader exists to undo.
        ("uv", 1.0),  # case-sensitive comparison upstream
        ("UV", 1.0),
        ("nV", 1.0),
        ("V", 1.0),
        ("mv", 1.0),
        ("%", 1.0),
        ("", 1.0),
        ("none", 1.0),
    ],
)
def test_mne_edf_gain(dimension, gain):
    assert signal_loader._mne_edf_gain(dimension) == gain


# ── channel_scale: unit token + factor applied to MNE's output ────────────


@pytest.mark.parametrize(
    "dimension, unit, factor",
    [
        # Voltages MNE handles: it has already converted to volts, so the factor is
        # the plain volts->microvolts 1e6 regardless of the header's own prefix.
        ("uV", "uV", 1e6),
        ("µV", "uV", 1e6),
        ("mV", "uV", 1e6),
        # Voltages MNE does NOT handle: the number is still in the header's own unit,
        # so the factor is that unit's conversion to microvolts and nothing else.
        ("uv", "uV", 1.0),
        ("nV", "uV", 1e-3),
        ("V", "uV", 1e6),
        ("kV", "uV", 1e9),
        ("microvolt", "uV", 1.0),
        # Not voltages: no conversion exists, so the samples pass through and the
        # window says what they actually are.
        ("%", "%", 1.0),
        ("mmHg", "mmHg", 1.0),
        ("°C", "degC", 1.0),
        ("", GENERIC_UNIT, 1.0),
        ("none", GENERIC_UNIT, 1.0),
        ("banana", GENERIC_UNIT, 1.0),
    ],
)
def test_channel_scale(dimension, unit, factor):
    got_unit, got_factor = signal_loader.channel_scale(dimension)
    assert got_unit == unit
    assert got_factor == pytest.approx(factor)


def test_channel_scale_never_defaults_to_microvolts():
    """The failure this module was rewritten to prevent: an unrecognised dimension
    must not be reported as a voltage, however plausible the channel looks."""
    for dimension in ("", "none", "n/a", "?", "arb", "banana"):
        unit, factor = signal_loader.channel_scale(dimension)
        assert unit == GENERIC_UNIT
        assert factor == 1.0


# ── load_signal_window: preconditions ─────────────────────────────────────


def test_derived_version_not_implemented(tmp_path):
    f = tmp_path / "r.edf"
    f.write_bytes(b"x")
    run = SimpleNamespace(
        pk=1,
        input_version_id="deadbeef",  # any non-'source' id
        recording_id=7,
        recording=SimpleNamespace(file_path=str(f)),
    )
    with pytest.raises(NotImplementedError):
        signal_loader.load_signal_window(run, _seg(0.0, 1.0))


def test_missing_source_file(tmp_path):
    run = SimpleNamespace(
        pk=1,
        input_version_id=signal_loader.SOURCE_VERSION_ID,
        recording_id=7,
        recording=SimpleNamespace(file_path=str(tmp_path / "nope.edf")),
    )
    with pytest.raises(FileNotFoundError):
        signal_loader.load_signal_window(run, _seg(0.0, 1.0))


def test_mixed_sampling_rates_refused(tmp_path, monkeypatch):
    """MNE would upsample the slow channel and report one sfreq; the contract has no
    way to say which rows are measured, so the read is refused rather than guessed."""
    infos = [_info("EEG Fp1-Ref", rate=500.0), _info("EEG C3-Ref", rate=100.0)]
    run = _wire(monkeypatch, tmp_path, infos, np.zeros((2, 10)), sfreq=500.0)
    with pytest.raises(NotImplementedError, match="mixes sampling rates"):
        signal_loader.load_signal_window(run, _seg(0.0, 0.02))


def test_channel_count_mismatch_refused(tmp_path, monkeypatch):
    """Rows that cannot be attributed to a header signal have no known unit."""
    infos = [_info("EEG Fp1-Ref"), _info("EEG C3-Ref")]
    run = _wire(monkeypatch, tmp_path, infos, np.zeros((1, 10)), ch_names=["EEG Fp1-Ref"])
    with pytest.raises(ValueError, match="channels but the header declares"):
        signal_loader.load_signal_window(run, _seg(0.0, 0.02))


def test_sampling_rate_disagreement_refused(tmp_path, monkeypatch):
    """A reader fs that differs from the header's turns every sample index into a
    different timestamp than the one the segment plan meant."""
    infos = [_info("EEG Fp1-Ref", rate=100.0)]
    run = _wire(monkeypatch, tmp_path, infos, np.zeros((1, 10)), sfreq=256.0)
    with pytest.raises(ValueError, match="Hz but the header declares"):
        signal_loader.load_signal_window(run, _seg(0.0, 0.02))


# ── load_signal_window: the window itself ─────────────────────────────────


def test_happy_path_units_labels_and_shape(tmp_path, monkeypatch):
    fs = 100.0
    ch_names = ["EEG Fp1-Ref", "EEG C3-Ref", "Status"]
    infos = [
        _info("EEG Fp1-Ref", "uV"),
        _info("EEG C3-Ref", "uV"),
        _info("Status", ""),  # a trigger line with no declared dimension
    ]
    # What MNE hands back: volts for the two it rescaled, raw digital-derived numbers
    # for the one it did not.
    data = np.array(
        [
            [1e-6, 2e-6, 3e-6, 4e-6],
            [5e-6, 6e-6, 7e-6, 8e-6],
            [0.0, 1.0, 0.0, 1.0],
        ]
    )
    run = _wire(monkeypatch, tmp_path, infos, data, ch_names, fs)
    # Window covering all 4 samples: [0, 0.04)s at 100 Hz -> [0, 4).
    win = signal_loader.load_signal_window(run, _seg(0.0, 0.04))

    assert isinstance(win, SignalWindow)
    assert win.fs == fs
    assert win.t0_s == 0.0
    assert win.n_samples == 4
    assert win.data.shape == (3, 4)
    # The two microvolt channels are converted; the undeclared one is untouched.
    np.testing.assert_allclose(win.data[:2], data[:2] * 1e6)
    np.testing.assert_allclose(win.data[2], data[2])
    assert win.channel_units == ("uV", "uV", GENERIC_UNIT)
    # Labels/types reproduce the ingest primitives exactly, in channel order.
    expected = [classify_channel(n, extract_signal_type(n)) for n in ch_names]
    assert win.channel_types == tuple(t for t, _ in expected)
    assert win.channels == tuple(c for _, c in expected)


def test_annotation_channel_excluded_from_row_mapping(tmp_path, monkeypatch):
    """MNE drops the TAL channel, so header index and window row index diverge; the
    loader must filter the same channel or every row's unit shifts by one."""
    infos = [
        _info("EEG Fp1-Ref", "uV"),
        _info("EDF Annotations", "", annotation=True),
        _info("Pulse", "bpm"),
    ]
    data = np.array([[1e-6, 2e-6], [72.0, 73.0]])
    run = _wire(monkeypatch, tmp_path, infos, data)
    win = signal_loader.load_signal_window(run, _seg(0.0, 0.02))

    assert win.channel_units == ("uV", "bpm")
    assert win.channels == ("Fp1", "Pulse")
    np.testing.assert_allclose(win.data[0], [1.0, 2.0])
    np.testing.assert_allclose(win.data[1], [72.0, 73.0])


@pytest.mark.parametrize(
    "dimension, mne_value, expected",
    [
        ("uV", 3e-6, 3.0),  # MNE converted to volts; 1e6 back to uV
        ("mV", 3e-6, 3.0),  # ditto - the header's prefix is already spent
        ("V", 3.0, 3e6),  # not rescaled: 3 V is 3e6 uV
        ("nV", 3000.0, 3.0),  # not rescaled: 3000 nV is 3 uV
        ("uv", 3.0, 3.0),  # not rescaled, already microvolts
        ("%", 97.0, 97.0),  # not a voltage: untouched
        ("none", 3.0, 3.0),  # no dimension: untouched
    ],
)
def test_per_channel_scaling_is_exact(tmp_path, monkeypatch, dimension, mne_value, expected):
    """One channel at a time, so the arithmetic is readable: whatever MNE hands back
    for a channel declaring *dimension*, the window holds the right number."""
    infos = [_info("EEG Fp1-Ref", dimension)]
    run = _wire(monkeypatch, tmp_path, infos, np.array([[mne_value, mne_value]]))
    win = signal_loader.load_signal_window(run, _seg(0.0, 0.02))
    assert float(win.data[0, 0]) == pytest.approx(expected)


# ── _splices_in_window: the loader's own discontinuity guard ──────────────
#
# The loader is the last place that can notice a window reaching across a splice. The
# segment planner normally cuts at every splice, but a hand-built plan, a resegmentation
# oracle or a plan replayed from before the cutting existed carries no such guarantee,
# and the samples themselves cannot show it: a splice leaves no trace in the array, only
# a step where two instants recorded hours apart sit side by side.

#: One EEG channel plus the annotation channel that carries the record timeline — the
#: minimum shape in which the question "where are the splices?" is answerable at all.
_ANNOTATED = [_info("EEG Fp1-Ref"), _info("EDF Annotations", "", annotation=True)]


def _capture_reader(monkeypatch, positions):
    """Patch the timeline reader with a stub that records exactly how it was called."""
    calls: list[tuple[tuple, dict]] = []

    def fake(*args, **kwargs):
        calls.append((args, kwargs))
        return list(positions)

    monkeypatch.setattr(signal_loader, "read_splice_positions", fake)
    return calls


def test_continuous_recording_is_never_opened(monkeypatch):
    """The EDF+D marker gates the read, and that gate is the reason the guard is
    affordable: virtually every recording the platform stores is continuous, and for those
    the check must not cost a single seek. A guard that read the timeline unconditionally
    would be a tax on the common case to catch a defect in the rare one."""

    def boom(*args, **kwargs):
        raise AssertionError("an EDF+C file's record timeline must not be read")

    monkeypatch.setattr(signal_loader, "read_splice_positions", boom)
    assert signal_loader._splices_in_window(Path("rec.edf"), _header(_ANNOTATED), _ANNOTATED, 0, 1000, 100.0) == []


def test_discontinuous_without_annotation_channel_is_refused():
    """Marked discontinuous, no timeline to place the gaps with: the question cannot be
    answered, and the guard exists precisely so that "I could not tell" does not read as
    "safe". A denoising command must refuse the same condition — the two
    disagreeing about one file would be worse than either being wrong alone."""
    infos = [_info("EEG Fp1-Ref")]
    with pytest.raises(ValueError, match="no annotation channel"):
        signal_loader._splices_in_window(Path("rec.edf"), _header(infos, discontinuous=True), infos, 0, 1000, 100.0)


@pytest.mark.parametrize("record_duration", [0.0, -1.0])
def test_discontinuous_with_unusable_record_duration_is_refused(record_duration):
    """A splice sits at ``record_index * record_duration``, so a non-positive duration
    collapses every seam onto zero. The positions would parse and be meaningless."""
    with pytest.raises(ValueError, match="data record duration"):
        signal_loader._splices_in_window(
            Path("rec.edf"),
            _header(_ANNOTATED, discontinuous=True, record_duration=record_duration),
            _ANNOTATED,
            0,
            1000,
            100.0,
        )


def test_only_splices_strictly_inside_the_window_count(monkeypatch):
    """Strictly inside, and the strictness is the whole design. A splice is a zero-width
    seam in data position, so a window *ending* at one — which is exactly what a planner
    cutting at splices produces — contains no discontinuity: every sample in it was
    recorded contiguously. Counting the boundary would refuse every correctly planned
    segment at a gap.
    """
    _capture_reader(monkeypatch, [0.0, 5.0, 7.5, 10.0, 22.0])
    got = signal_loader._splices_in_window(
        Path("rec.edf"),
        _header(_ANNOTATED, discontinuous=True),
        _ANNOTATED,
        500,  # 5.0 s at 100 Hz
        1000,  # 10.0 s
        100.0,
    )
    assert got == [7.5]


def test_record_range_is_generous_by_one_record(monkeypatch):
    """Only the records the window overlaps can carry a splice inside it, so the read is
    restricted to those — the second half of what makes the guard cheap. The bounds round
    outwards deliberately: a window starting mid-record must still see that record's
    onset, and one record either side costs a couple of seeks while a range that rounded
    inwards would miss a seam at the very edge of the window.
    """
    calls = _capture_reader(monkeypatch, [])
    signal_loader._splices_in_window(
        Path("rec.edf"),
        _header(_ANNOTATED, discontinuous=True, record_duration=2.0),
        _ANNOTATED,
        550,  # 5.5 s at 100 Hz -> inside record 2, which spans [4, 6)s
        950,  # 9.5 s          -> inside record 4, which spans [8, 10)s
        100.0,
    )
    ((_args, kwargs),) = calls
    assert kwargs["first_record"] == 2
    assert kwargs["last_record"] == 5


def test_reader_is_called_the_way_the_real_reader_expects(monkeypatch):
    """The one thing patching the reader could hide: a call the real function would reject.
    Binding the captured call against the genuine signature closes that hole, so the
    cheaper unit tests above stay trustworthy as ``read_splice_positions`` evolves."""
    calls = _capture_reader(monkeypatch, [])
    signal_loader._splices_in_window(
        Path("rec.edf"), _header(_ANNOTATED, discontinuous=True), _ANNOTATED, 0, 1000, 100.0
    )
    ((args, kwargs),) = calls
    inspect.signature(read_splice_positions).bind(*args, **kwargs)


def test_window_spanning_a_splice_is_refused(tmp_path, monkeypatch):
    """The refusal in full, through the public entry point: it happens *before*
    ``get_data``, and it names the position so the caller can see which seam it planned
    across rather than being told only that something is wrong."""
    infos = list(_ANNOTATED)
    run = _wire(monkeypatch, tmp_path, infos, np.zeros((1, 400)), discontinuous=True)
    _capture_reader(monkeypatch, [2.0])
    with pytest.raises(ValueError) as excinfo:
        signal_loader.load_signal_window(run, _seg(0.0, 4.0))
    message = str(excinfo.value)
    assert "1 discontinuity" in message
    assert "splice at 2s" in message
    assert "plan_segments" in message


def test_window_ending_at_a_splice_is_allowed(tmp_path, monkeypatch):
    """The complement of the refusal, and the case that must keep working: a segment cut
    *at* the splice is the correct plan, so it has to load. Without this the guard could
    pass its own tests while making every discontinuous recording unreadable."""
    infos = list(_ANNOTATED)
    run = _wire(monkeypatch, tmp_path, infos, np.zeros((1, 400)), discontinuous=True)
    _capture_reader(monkeypatch, [0.0, 4.0])
    win = signal_loader.load_signal_window(run, _seg(0.0, 4.0))
    assert win.n_samples == 400
