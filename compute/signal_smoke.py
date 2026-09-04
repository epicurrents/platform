"""Smoke checks for the concrete signal loader against a real EDF/BDF file.

``compute/tests/test_signal_loader.py`` locks down the loader's own arithmetic with
a fake raw. What it cannot see is what MNE does to a real header before
:func:`~compute.signal_loader.load_signal_window` ever reaches ``raw.ch_names`` or
``get_data`` — it drops the ``EDF Annotations`` channel, de-duplicates channel
names, upsamples every channel to the highest sampling rate, and scales only the
physical units it recognises. Each of those silently changes what a window means.

:func:`run_checks` reads one real recording and answers those questions, comparing
the loader's output against the header parsed by ``recordings.processors.edf`` —
the same parser ingest uses, so a mismatch means the loader and the stored
``SignalInfo`` rows disagree. Deliberately **Django-free**: it takes a path, not a
``Recording``, so it can run from a management command, from a pytest module
gated on the ``require_edf`` marker, or as a plain script during development.
Only the last exists today: neither the command (which would add the comparison
against the stored ``SignalInfo`` rows) nor the gated module has been written,
and the ``testdata/README.md`` convention documents the by-hand invocation.

Checks are diagnostics, not just assertions: a ``FAIL`` is a loader defect, a
``WARN`` is a property of the file the loader does not currently account for (a
discontinuous timeline, mixed sampling rates), and ``INFO`` lines carry the numbers
worth eyeballing. The distinction matters because a real clinical recording will
frequently trip a ``WARN`` that is nobody's bug.
"""

from __future__ import annotations

import itertools
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from recordings.processors.channel_labels import is_auxiliary_type
from recordings.processors.edf import (
    EdfHeader,
    EdfParseError,
    EdfSignalInfo,
    read_record_gaps,
)
from recordings.processors.units import (
    GENERIC_UNIT,
    MICROVOLT,
    canonical_unit,
    to_microvolts,
)

from .segmentation import SegmentPlan, plan_segments
from .signal_loader import (
    _window_bounds,
    channel_scale,
    load_signal_window,
    read_header,
)

PASS = "PASS"
FAIL = "FAIL"
WARN = "WARN"
INFO = "INFO"
SKIP = "SKIP"


@dataclass
class Check:
    """One named observation about the loader's behaviour on a real file."""

    name: str
    status: str
    detail: str
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class _Stub:
    """Minimal stand-in for an ``AnalysisRun``: the loader reads four attributes."""

    file_path: str
    input_version_id: str
    pk: int = 0
    recording_id: int = 0

    @property
    def recording(self):
        return self


def _first_record_physical(path: Path, header: EdfHeader, infos: list[EdfSignalInfo]) -> dict[int, float]:
    """Decode sample 0 of every signal in data record 0 to physical units.

    An independent path to the same numbers the loader returns: digital integers
    straight out of the file, converted with the header's own scaling
    (``(digital + digital_offset) * units_per_bit``). Nothing here goes through MNE,
    which is the point — it is what catches a channel MNE declined to rescale.
    """
    bytes_per_sample = 3 if header.data_format.rstrip("+") == "bdf" else 2
    out: dict[int, float] = {}
    with path.open("rb") as handle:
        handle.seek(header.header_record_bytes)
        record = handle.read(header.record_byte_size or sum(i.sample_count * bytes_per_sample for i in infos))
    offset = 0
    for index, info in enumerate(infos):
        if not info.is_annotation_channel and info.sample_count:
            chunk = record[offset : offset + bytes_per_sample]
            if len(chunk) == bytes_per_sample:
                digital = int.from_bytes(chunk, "little", signed=True)
                out[index] = (digital + info.digital_offset) * info.units_per_bit
        offset += info.sample_count * bytes_per_sample
    return out


def _record_onset_gaps(path: Path, header: EdfHeader, infos: list[EdfSignalInfo]) -> list[tuple[int, float]]:
    """Return ``(record_index, extra_seconds)`` for every gap in the EDF+D timeline.

    A **tolerant** wrapper over :func:`recordings.processors.edf.read_record_gaps` —
    see there for the record-index convention (the splice sits at
    ``record_index * data_record_duration``, the same instant ingest keys its
    ``Interruption`` rows on) and for why the scan seeks instead of reading the file.

    The tolerance is the only thing this adds, and it is deliberate: a file whose
    timekeeping TALs cannot be read reports *no* gaps here rather than raising, because
    a smoke check exists to report on a file and aborting the whole run tells the
    operator less than a continuity check that comes back empty. The signal loader
    makes the opposite call on the same primitive and refuses the window — it is about
    to hand samples to a detector, so "I could not tell" has to mean "no".
    """
    try:
        return read_record_gaps(path, header, infos)
    except (EdfParseError, OSError):
        return []


def run_checks(
    path: str | Path,
    *,
    segment_length_s: float = 60.0,
    halo_s: float = 0.7,
    check_equivalence: bool = True,
) -> list[Check]:
    """Run every smoke check against *path* and return the results in order.

    *halo_s* deliberately defaults to a value that does not land on a sample
    boundary at any common sampling rate, so the loader's floor/ceil rounding is
    actually exercised rather than accidentally exact.
    """
    import numpy as np

    path = Path(path)
    checks: list[Check] = []

    def add(name, status, detail, **data):
        checks.append(Check(name, status, detail, data))

    if not path.is_file():
        add("file", FAIL, f"No such file: {path}")
        return checks

    header, infos = read_header(path)
    signal = [i for i in infos if not i.is_annotation_channel]
    n_anno = len(infos) - len(signal)

    from .signal_loader import SOURCE_VERSION_ID, _read_raw

    run = _Stub(file_path=str(path), input_version_id=SOURCE_VERSION_ID)
    raw = _read_raw(path)
    fs = float(raw.info["sfreq"])
    duration_s = raw.n_times / fs

    add(
        "file",
        INFO,
        f"{path.name}: {header.data_format.upper()}"
        f"{' (discontinuous)' if header.discontinuous else ''}, "
        f"{duration_s:.1f}s at {fs:g} Hz, {len(infos)} header signals "
        f"({len(signal)} signal + {n_anno} annotation), {raw.n_times} samples",
        format=header.data_format,
        discontinuous=header.discontinuous,
        duration_s=duration_s,
        fs=fs,
        n_times=int(raw.n_times),
        n_header_signals=len(infos),
        n_annotation_channels=n_anno,
    )

    # ── what MNE handed back vs what the header declares ──────────────────
    if list(raw.ch_names) == [i.label for i in signal]:
        add(
            "header_alignment",
            PASS,
            f"MNE returned the {len(signal)} non-annotation channels in header order"
            + (
                f"; the {n_anno} annotation channel(s) are excluded, so a window row index is NOT a header signal index"
                if n_anno
                else ""
            ),
        )
    else:
        add(
            "header_alignment",
            FAIL,
            "MNE channel names differ from the header's non-annotation labels — a "
            "window row cannot be matched to a stored SignalInfo row by position. "
            f"MNE: {list(raw.ch_names)!r} header: {[i.label for i in signal]!r}",
            mne=list(raw.ch_names),
            header=[i.label for i in signal],
        )

    rates = sorted({i.sampling_rate for i in signal})
    if len(rates) > 1:
        add(
            "sampling_rates",
            WARN,
            f"Channels have different sampling rates ({', '.join(f'{r:g}' for r in rates)} "
            f"Hz); MNE upsamples all of them to {fs:g} Hz, so the lower-rate rows in a "
            "window are step-interpolated rather than measured. The contract carries a "
            "single fs and cannot express this.",
            rates=rates,
        )
    else:
        add("sampling_rates", PASS, f"All signal channels sample at {rates[0]:g} Hz")

    # ── the window itself ─────────────────────────────────────────────────
    segments = plan_segments(duration_s=duration_s, segment_length_s=segment_length_s, halo_s=halo_s)
    windows = []
    problems = []
    for seg in segments:
        win = load_signal_window(run, seg)
        windows.append(win)
        end_s = win.t0_s + win.n_samples / win.fs
        if win.t0_s > seg.interior_start_s + 1e-9:
            problems.append(f"seg{seg.index}: t0 {win.t0_s} misses interior start {seg.interior_start_s}")
        if end_s < seg.interior_end_s - 1e-9:
            problems.append(f"seg{seg.index}: window ends {end_s} before interior end {seg.interior_end_s}")
        if win.n_samples != win.data.shape[1]:
            problems.append(f"seg{seg.index}: n_samples {win.n_samples} != data columns {win.data.shape[1]}")
        if len(win.channels) != win.data.shape[0] or len(win.channel_types) != win.data.shape[0]:
            problems.append(f"seg{seg.index}: {win.data.shape[0]} rows but {len(win.channels)} labels")
    if problems:
        add("window_coverage", FAIL, "; ".join(problems), problems=problems)
    else:
        add(
            "window_coverage",
            PASS,
            f"{len(segments)} segment(s) of {segment_length_s:g}s + {halo_s:g}s halo: every "
            "interior fully covered, shapes and label counts self-consistent",
            n_segments=len(segments),
        )

    first, last = windows[0], windows[-1]
    clamp = []
    if first.t0_s != 0.0:
        clamp.append(f"first window starts at {first.t0_s}, expected 0.0")
    last_end_sample = round((last.t0_s + last.n_samples / last.fs) * fs)
    if last_end_sample != raw.n_times:
        clamp.append(f"last window ends at sample {last_end_sample}, expected {raw.n_times}")
    if clamp:
        add("clamping", FAIL, "; ".join(clamp))
    else:
        add(
            "clamping",
            PASS,
            "Leading halo clamps to sample 0 (t0_s 0.0, not the negative context start) "
            "and the trailing halo clamps to the final sample",
        )

    try:
        _window_bounds(duration_s + 10.0, duration_s + 11.0, fs, raw.n_times)
        add("degenerate_window", FAIL, "A window past the recording end did not raise")
    except ValueError:
        add("degenerate_window", PASS, "A window past the recording end raises ValueError")

    # ── labels: loader vs the parser ingest uses ───────────────────────────
    expected_labels = tuple(i.canonical_label for i in signal)
    expected_types = tuple(i.signal_type for i in signal)
    if first.channels == expected_labels and first.channel_types == expected_types:
        add(
            "canonical_labels",
            PASS,
            "Window labels and signal types match the ingest header parser exactly, "
            "so they cannot drift from the stored SignalInfo rows",
        )
    else:
        add(
            "canonical_labels",
            FAIL,
            f"Window labelling differs from the header parser. window: {first.channels!r} / "
            f"{first.channel_types!r} header: {expected_labels!r} / {expected_types!r}",
            window_labels=list(first.channels),
            header_labels=list(expected_labels),
        )

    blank = [i for i, c in enumerate(first.channels) if c == ""]
    seen: dict[str, int] = {}
    dupes = []
    for c in first.channels:
        if c:
            seen[c] = seen.get(c, 0) + 1
    dupes = [c for c, n in seen.items() if n > 1]
    if blank or dupes:
        parts = []
        if blank:
            parts.append(f"{len(blank)} channel(s) canonicalise to '' ({', '.join(signal[i].label for i in blank)})")
        if dupes:
            parts.append(f"duplicate canonical label(s): {', '.join(dupes)}")
        add(
            "label_uniqueness",
            WARN,
            "; ".join(parts) + ". A processor selecting channels by canonical name cannot address these "
            "rows unambiguously; selection has to be positional.",
            blank_indices=blank,
            duplicates=dupes,
        )
    else:
        add("label_uniqueness", PASS, "Every canonical label is non-empty and unique")

    # ── units: every row's number and its declared unit agree ─────────────
    # The expectation is stated from the *header's* dimension, independently of how
    # the loader gets there: a voltage dimension must arrive converted to microvolts
    # and labelled uV; anything else — a percentage, a trigger line, a dimension the
    # header never established — must arrive untouched and labelled with what it is.
    # MNE's own partial rescaling is not mentioned here on purpose: it is the loader's
    # job to cancel it, and a check that repeated the gain table would only prove the
    # loader agrees with itself.
    physical = _first_record_physical(path, header, infos)
    at_zero = load_signal_window(run, SegmentPlan(0, 0.0, 1.0, 0.0, 1.0))
    wrong_value: list[tuple[str, str, float, float]] = []
    wrong_unit: list[tuple[str, str, str, str]] = []
    unconverted: list[tuple[str, str]] = []
    for row, info in enumerate(signal):
        dimension = info.physical_unit
        unit = canonical_unit(dimension)
        factor = to_microvolts(unit)
        want_unit = MICROVOLT if factor is not None else unit
        got_unit = at_zero.channel_units[row] if at_zero.channel_units else ""
        if got_unit != want_unit:
            wrong_unit.append((info.label, dimension, want_unit, got_unit))
        if factor is None:
            unconverted.append((info.label, unit))
        header_index = infos.index(info)
        measured = physical.get(header_index)
        if measured is None or abs(measured) < 1e-12:
            continue
        want = measured * (factor if factor is not None else 1.0)
        got = float(at_zero.data[row, 0])
        if abs(got - want) > abs(want) * 1e-9:
            wrong_value.append((info.label, dimension, want, got))

    if wrong_unit or wrong_value:
        parts = []
        if wrong_value:
            parts.append(
                "value(s) not in the unit the window claims: "
                + "; ".join(
                    f"{label!r} (dimension {dim or 'blank'!r}, expected {want:.6g}, "
                    f"got {got:.6g}, off by {got / want if want else float('inf'):.3g}x)"
                    for label, dim, want, got in wrong_value
                )
            )
        if wrong_unit:
            parts.append(
                "unit token(s) mislabelled: "
                + "; ".join(
                    f"{label!r} (dimension {dim or 'blank'!r}, expected {want!r}, got {got!r})"
                    for label, dim, want, got in wrong_unit
                )
            )
        add(
            "units",
            FAIL,
            "; ".join(parts) + ". A row's number and the unit beside it must describe the same "
            "quantity — a scale factor applied to a dimension the header never "
            "established produces a meaningless value, not an approximate one.",
            wrong_value=[m[0] for m in wrong_value],
            wrong_unit=[m[0] for m in wrong_unit],
        )
    else:
        detail = (
            "Every channel's window value equals the header's own physical scaling, "
            "and channel_units names the unit it is in"
        )
        if unconverted:
            detail += ". Not convertible to microvolts, carried in native units: " + ", ".join(
                f"{label} ({unit})" for label, unit in unconverted
            )
        add(
            "units",
            PASS,
            detail,
            non_voltage=[m[0] for m in unconverted],
            generic=[m[0] for m in unconverted if m[1] == GENERIC_UNIT],
        )

    # ── signal types: what the 10-10 gate promoted and what it demoted ─────
    aux = [
        (info.label, first.channel_types[row], first.channels[row])
        for row, info in enumerate(signal)
        if is_auxiliary_type(first.channel_types[row])
    ]
    demoted = [(label, canon) for label, _t, canon in aux if not canon]
    if demoted:
        add(
            "signal_types",
            WARN,
            f"{len(demoted)} of {len(signal)} channel(s) carry a modality marker but "
            "resolve to no known electrode and no auxiliary role, so they are typed "
            "misc with no canonical label: "
            + ", ".join(repr(label) for label, _c in demoted)
            + ". Expected for an intracranial or high-density montage, whose names are "
            "not 10-10 — those need a per-recording montage declaration or a label "
            "alias, not a looser match.",
            demoted=[label for label, _c in demoted],
            n_auxiliary=len(aux),
        )
    elif aux:
        add(
            "signal_types",
            PASS,
            f"{len(signal) - len(aux)} channel(s) resolved to 10-10 electrodes or a "
            f"named modality; {len(aux)} auxiliary channel(s) resolved to a role: "
            + ", ".join(f"{label} → {t}/{canon}" for label, t, canon in aux),
            n_auxiliary=len(aux),
        )
    else:
        add(
            "signal_types",
            PASS,
            f"All {len(signal)} channel(s) resolved to electrodes or named modalities; no auxiliary channels",
            n_auxiliary=0,
        )

    # ── the lazy windowed read vs the idiom the detectors already use ──────
    if check_equivalence:
        seg = segments[len(segments) // 2]
        t0 = time.perf_counter()
        win = load_signal_window(run, seg)
        t_window = time.perf_counter() - t0
        t0 = time.perf_counter()
        full = _read_raw_preloaded(path)
        reference = full.get_data()
        t_full = time.perf_counter() - t0
        # The detectors' idiom is a blanket ``* 1e6``; the loader's per-channel factor
        # is the thing under test, so the reference applies the same per-channel factor
        # rather than the blanket one. What is being compared is the *read* — lazy
        # windowed vs preload-and-slice — not the scaling, which the units check owns.
        for row, info in enumerate(signal):
            row_factor = channel_scale(info.physical_unit)[1]
            if row_factor != 1.0:
                reference[row] *= row_factor
        start, stop, _ = _window_bounds(seg.context_start_s, seg.context_end_s, fs, raw.n_times)
        diff = float(np.abs(win.data - reference[:, start:stop]).max())
        if diff == 0.0:
            add(
                "equivalence",
                PASS,
                "The lazy windowed read is bit-identical to the detectors' preload-and-slice idiom",
            )
        else:
            add(
                "equivalence",
                FAIL,
                f"Windowed read differs from a full preload by up to {diff:g} (in each row's own unit)",
                max_abs_diff=diff,
            )
        add(
            "cost",
            INFO,
            f"One {seg.context_duration_s:g}s window: {t_window * 1000:.0f} ms, "
            f"{win.data.nbytes / 1e6:.1f} MB. Whole file preloaded: {t_full * 1000:.0f} ms, "
            f"{reference.nbytes / 1e6:.1f} MB. Windowing trades wall-clock for memory — "
            "the win only appears once a recording no longer fits comfortably in RAM.",
            window_ms=t_window * 1000,
            full_ms=t_full * 1000,
            window_mb=win.data.nbytes / 1e6,
            full_mb=reference.nbytes / 1e6,
        )
    else:
        add("equivalence", SKIP, "Equivalence check skipped (would preload the whole file)")

    # ── the timeline the loader assumes is linear ──────────────────────────
    gaps = _record_onset_gaps(path, header, infos)
    marker = "EDF+D / BDF+D" if header.discontinuous else header.data_format.upper()
    if gaps:
        skew = sum(extra for _, extra in gaps)
        where = ", ".join(
            f"{extra:+g}s before record {r} (data position {r * header.data_record_duration:g}s)"
            for r, extra in gaps[:5]
        )
        add(
            "continuity",
            WARN,
            f"Marked {marker} and the record onsets contain {len(gaps)} gap(s) totalling "
            f"{skew:g}s ({where}). MNE reads the records as contiguous, so a window's t0_s "
            "is a data position, not a wall-clock offset from the recording start — which "
            "is the platform's canonical timeline, and the one ingest translates TAL onsets "
            "onto, so processor output and stored events already agree. Each gap is a splice "
            "where the signal either side is physically unrelated; segmentation now cuts the "
            "recording at them (see splice_segmentation below), so no interior or halo spans "
            "one. This stays a WARN because it is a property of the file, not a defect: the "
            "recording really is several recordings, and a detector needing more context than "
            "the shortest run provides still cannot be given it.",
            discontinuous=header.discontinuous,
            n_gaps=len(gaps),
            total_skew_s=skew,
        )
    elif header.discontinuous:
        add(
            "continuity",
            WARN,
            f"Marked {marker} but no record-onset gap was found, so the sample timeline is "
            "linear in this file. The marker alone is enough for a denoising command to refuse "
            "the recording; the loader has no such guard.",
            discontinuous=True,
            n_gaps=0,
            total_skew_s=0.0,
        )
    else:
        add(
            "continuity",
            PASS,
            "Continuous recording: sample position and recording time advance together, "
            "so t0_s is a true recording-relative timestamp",
            discontinuous=False,
            n_gaps=0,
            total_skew_s=0.0,
        )

    # ── and that segmentation actually respects those splices ──────────────
    # The splices are derived from the file's own record onsets rather than from the
    # DB, so this check stays Django-free. They are the same instants ingest keys its
    # Interruption rows on (``_record_onset_gaps``' docstring pins that convention),
    # which is what makes this a faithful stand-in for the production lookup in
    # ``compute.tasks.splices_for``.
    splices = [r * header.data_record_duration for r, _extra in gaps]
    if not splices:
        add(
            "splice_segmentation",
            PASS if not header.discontinuous else SKIP,
            "No splices to segment around",
            n_splices=0,
        )
    else:
        spliced = plan_segments(
            duration_s=duration_s,
            segment_length_s=segment_length_s,
            halo_s=halo_s,
            splices=splices,
        )
        straddling = [
            (seg.index, splice, seg.context_start_s, seg.context_end_s)
            for seg in spliced
            for splice in splices
            if seg.context_start_s < splice < seg.context_end_s
        ]
        covered = spliced[0].interior_start_s == 0.0 and all(
            a.interior_end_s == b.interior_start_s for a, b in itertools.pairwise(spliced)
        )
        # The last interior ends at the recording's end, to within a sample.
        covered = covered and abs(spliced[-1].interior_end_s - duration_s) < 1.0 / fs
        if straddling:
            add(
                "splice_segmentation",
                FAIL,
                f"{len(straddling)} segment context(s) still cross a splice: "
                + "; ".join(f"seg{i} [{cs:g}, {ce:g}) crosses {sp:g}s" for i, sp, cs, ce in straddling[:5])
                + ". A halo spanning a splice hands the detector a step discontinuity it "
                "has no way to detect from the samples.",
                n_splices=len(splices),
                n_straddling=len(straddling),
            )
        elif not covered:
            add(
                "splice_segmentation",
                FAIL,
                "Cutting at the splices left the interiors no longer partitioning "
                f"[0, {duration_s:g}) exactly — ownership is no longer total, so some "
                "onsets would be emitted by no segment.",
                n_splices=len(splices),
            )
        else:
            n_runs = 1 + len({s.run_index for s in spliced} - {0})
            without = len(
                plan_segments(
                    duration_s=duration_s,
                    segment_length_s=segment_length_s,
                    halo_s=halo_s,
                )
            )
            add(
                "splice_segmentation",
                PASS,
                f"{len(splices)} splice(s) cut the recording into {n_runs} contiguous "
                f"run(s), segmented into {len(spliced)} segment(s) (vs {without} ignoring "
                "the splices). No interior and no halo spans a seam, and the interiors "
                f"still partition [0, {duration_s:g}) exactly.",
                n_splices=len(splices),
                n_runs=n_runs,
                n_segments=len(spliced),
                n_segments_ignoring_splices=without,
                splices_s=splices,
            )

    return checks


def _read_raw_preloaded(path: Path):
    """The detectors' idiom: read the whole file into memory (reference path)."""
    import mne

    if path.suffix.lower() == ".bdf":
        return mne.io.read_raw_bdf(str(path), preload=True, verbose="ERROR")
    return mne.io.read_raw_edf(str(path), preload=True, verbose="ERROR")


def worst_status(checks: list[Check]) -> str:
    """Return the most severe status present (FAIL > WARN > PASS)."""
    statuses = {c.status for c in checks}
    for level in (FAIL, WARN, PASS):
        if level in statuses:
            return level
    return INFO
