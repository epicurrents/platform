"""Concrete signal loader — reads a segment's context window from a recording.

Bridges the execution layer's ``_load_window`` seam (:mod:`compute.tasks`) to real
signal data: given an :class:`~compute.models.AnalysisRun` and a
:class:`~compute.segmentation.SegmentPlan`, it opens the run's input recording, reads
the samples covering the segment's halo-padded context window
``[context_start_s, context_end_s]``, and returns a
:class:`~compute.contract.SignalWindow` with canonical channel labels, per-channel
types and per-channel units.

Scope of this slice
-------------------
Only the **source** version (``input_version_id == SOURCE_VERSION_ID``) is handled:
samples are read straight from ``recording.file_path`` (the preserved EDF/BDF). A
*derived* version (a ``RecordingJob`` output) must have its processed signal
materialised first — a later slice — so a derived ``input_version_id`` raises
``NotImplementedError`` rather than silently scoring the source.

Discontinuity is refused, not accommodated
------------------------------------------
A window that spans a splice is a step discontinuity dressed as signal, and no
processor can detect it from the samples (see ``recordings/continuity-and-timelines.md``
for the two timelines this is about). Correct plans never produce one — segmentation
cuts at every splice — so the loader's own guard exists for the plan that was not built
that way: it reads the splices near the requested window out of the *file*, not the
database, and refuses. See :func:`_splices_in_window` for why both of those are
constraints rather than preferences.

The header is the authority, MNE is only the reader
---------------------------------------------------
Reading uses MNE (``mne.io.read_raw_edf`` / ``read_raw_bdf``) — the same tool every
in-repo detector uses — but nothing about *meaning* is taken from it, because MNE's
EDF reader silently rewrites the parts that matter:

* **Units.** It rescales a physical dimension to volts only on an exact,
  case-sensitive string match against ``uV``/``µV``/``μV``/sjis-µV/``mV``; every
  other dimension — including ``nV``, lowercase ``uv``, ``%`` and blank — is passed
  through with gain 1 and then *reported* as volts. Its public ``info`` cannot be
  interrogated afterwards: every channel comes back ``FIFF_UNIT_V, cal=1, range=1``.
  So the loader reads the dimension out of the header itself, converts with
  :func:`~recordings.processors.units.to_microvolts`, and divides out whatever MNE
  already applied (:func:`_mne_edf_gain`). A dimension that is not a voltage keeps
  its own unit and its own numbers.
* **Sampling rate.** It upsamples every channel to the highest rate in the file and
  reports one ``sfreq``. The contract carries a single ``fs``, so it *cannot*
  distinguish measured samples from step-interpolated ones — a mixed-rate recording
  is therefore refused by name rather than quietly returned (see
  :func:`load_signal_window`).
* **Channel identity.** It drops the annotation (TAL) channel and de-duplicates
  repeated names. Rows are matched to header signals positionally, which is safe
  because the reader preserves order; the count is asserted rather than assumed.

Channel labels and types come from the exact ingest primitives
(``classify_channel(label, extract_signal_type(label))`` — see
``recordings.processors.edf`` where ingest calls the same pair) applied to the
header's own labels, so the loader's canonical labels and signal types cannot drift
from the stored ``SignalInfo`` rows.

MNE is imported lazily so importing this module (it is imported at app ``ready()``
to register the loader) stays cheap, and the window is read with ``preload=False`` +
a sample-range ``get_data(start, stop)`` so a long recording is not fully
materialised to score one segment.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import TYPE_CHECKING

from recordings.pipeline.manifest import SOURCE_VERSION_ID
from recordings.processors.channel_labels import classify_channel
from recordings.processors.edf import (
    EdfHeader,
    EdfSignalInfo,
    extract_signal_type,
    parse_edf_header,
    parse_signal_infos,
    read_splice_positions,
)
from recordings.processors.units import MICROVOLT, canonical_unit, to_microvolts

from .contract import SignalWindow
from .segmentation import SegmentPlan

if TYPE_CHECKING:  # pragma: no cover - annotation only
    # Imported for typing only: this module is reachable from a plain script (the
    # smoke checks) and must not drag in the model layer, which needs a configured
    # app registry.
    from .models import AnalysisRun

#: Suffixes read as 24-bit BDF rather than 16-bit EDF.
_BDF_SUFFIXES = {".bdf"}

#: Exact physical-dimension strings MNE's EDF reader recognises, and the gain it
#: applies to reach volts. A **mirror** of ``mne/io/edf/edf.py`` (the ``edf_info
#: ["units"]`` loop in ``_read_edf_header``), reproduced here because the value is not
#: recoverable from the ``Raw`` object afterwards and the loader must divide it out.
#:
#: Two properties are load-bearing and easy to miss: the comparison is on the
#: *stripped but otherwise untouched* header field, so it is **case-sensitive**
#: (lowercase ``uv`` does not match), and the fallback is gain **1**, i.e. "treat the
#: number as if it were already volts" — which is how a ``nV`` channel ends up
#: reported nine orders of magnitude too large.
#:
#: Keys are the latin-1 decoding of the header bytes, matching both MNE's own decode
#: and ``recordings.processors.edf._read_field``.
_MNE_EDF_GAINS: dict[str, float] = {
    "uV": 1e-6,
    "μV": 1e-6,  # Greek small letter mu
    "µV": 1e-6,  # micro sign
    "\x83\xcaV": 1e-6,  # sjis mu
    "mV": 1e-3,
}


def _mne_edf_gain(dimension: str) -> float:
    """The gain MNE's EDF/BDF reader applies to a channel declaring *dimension*.

    Returns 1.0 for every dimension it does not recognise — see
    :data:`_MNE_EDF_GAINS` for why that is a trap rather than a no-op.
    """
    return _MNE_EDF_GAINS.get(dimension.strip(), 1.0)


def _window_bounds(
    context_start_s: float,
    context_end_s: float,
    fs: float,
    n_total_samples: int,
) -> tuple[int, int, float]:
    """Resolve a time window to a half-open sample range ``[start, stop)``, clamped
    to the recording.

    Returns ``(start_sample, stop_sample, t0_s)`` where ``t0_s`` is the **absolute**
    start time of the returned data (``start_sample / fs``). ``t0_s`` differs from
    ``context_start_s`` only when the halo runs before the recording start and the
    window clamps to sample 0; onsets stay recording-relative because the processor
    derives them from ``t0_s`` (see :class:`~compute.contract.SignalWindow`). ``start``
    rounds down and ``stop`` rounds up to the nearest sample so the interior is always
    fully covered by the returned window.
    """
    if fs <= 0:
        raise ValueError(f"Non-positive sampling rate: {fs!r}.")
    start_sample = max(0, math.floor(context_start_s * fs))
    stop_sample = min(n_total_samples, math.ceil(context_end_s * fs))
    if stop_sample <= start_sample:
        raise ValueError(
            f"Empty signal window: [{context_start_s}, {context_end_s}]s at {fs} Hz "
            f"resolves to samples [{start_sample}, {stop_sample}) but the recording "
            f"has {n_total_samples} samples."
        )
    return start_sample, stop_sample, start_sample / fs


def _splices_in_window(
    path: Path,
    header: EdfHeader,
    all_infos: list[EdfSignalInfo],
    start_sample: int,
    stop_sample: int,
    fs: float,
) -> list[float]:
    """Data positions of the splices lying strictly inside samples ``[start, stop)``.

    The loader's **independent** discontinuity guard, and independent in two senses that
    both had to hold for it to exist at all.

    It does not consult the database. A ``SegmentPlan`` reaching this module was
    normally built by :func:`compute.tasks.launch_analysis_run`, which already cut at
    every splice — but a hand-built plan, a resegmentation oracle or a replayed plan
    from before the cutting existed carries no such guarantee, and the loader is the last
    place that can tell. It cannot ask ``compute.tasks.splices_for``: this module is
    reachable from a plain script (the smoke checks drive it with a stand-in run object
    and no app registry), so a model query here would break the one caller that has no
    database at all. Instead the splices come from the file, which is where they were
    recorded in the first place — the same instants ingest wrote its ``Interruption``
    rows from.

    It also does not read more of the file than the window covers. A splice can only sit
    on a data-record boundary, so only the records the window overlaps can carry one
    inside it, and :func:`~recordings.processors.edf.read_record_gaps` seeks to exactly
    those. A **continuous** recording costs nothing: the EDF+D marker is checked first
    and the file is never reopened.

    A file that declares itself discontinuous but carries no annotation channel has no
    readable timeline, so the question cannot be answered and ``ValueError`` is raised
    rather than answered with "no splices" — the whole point of the guard is that
    "I could not tell" must not read as "safe".
    """
    if not header.discontinuous:
        return []
    if not any(info.is_annotation_channel for info in all_infos):
        raise ValueError(
            f"{path.name} is marked EDF+D (discontinuous) but carries no annotation "
            "channel, so where acquisition paused cannot be read from the file. A "
            "window therefore cannot be shown not to span a splice."
        )
    record_duration = header.data_record_duration
    if record_duration <= 0:
        raise ValueError(
            f"{path.name} is marked EDF+D (discontinuous) but declares a data record "
            f"duration of {record_duration!r}, so its record timeline cannot be placed."
        )
    start_s, stop_s = start_sample / fs, stop_sample / fs
    # A splice before record r sits at r * record_duration, so only records the window
    # overlaps need looking at. Both bounds are deliberately generous by one record.
    positions = read_splice_positions(
        path,
        header,
        all_infos,
        first_record=math.floor(start_s / record_duration),
        last_record=math.ceil(stop_s / record_duration),
    )
    return [p for p in positions if start_s < p < stop_s]


def _read_raw(path: Path):
    """Open an EDF/BDF file lazily via MNE (volts, not preloaded)."""
    import mne

    if path.suffix.lower() in _BDF_SUFFIXES:
        return mne.io.read_raw_bdf(str(path), preload=False, verbose="ERROR")
    return mne.io.read_raw_edf(str(path), preload=False, verbose="ERROR")


def read_header(path: Path) -> tuple[EdfHeader, list[EdfSignalInfo]]:
    """Parse the general + per-signal header with the parser ingest uses.

    Returns ``(header, infos)`` with ``header.signal_infos`` populated and *all*
    signals present, annotation channels included — filtering them is the caller's
    decision, and the smoke checks need to see them.
    """
    with path.open("rb") as handle:
        head = handle.read(256)
        header = parse_edf_header(head)
        rest = handle.read(max(0, header.header_record_bytes - 256))
    infos = parse_signal_infos(head + rest, header)
    header.signal_infos = infos
    return header, infos


def channel_scale(dimension: str) -> tuple[str, float]:
    """Return ``(unit, factor)`` for a channel declaring physical *dimension*.

    ``factor`` is what MNE's returned value must be multiplied by, and ``unit`` is the
    unit of the result:

    * a **voltage** dimension yields ``("uV", to_microvolts(unit) / mne_gain)`` — the
      conversion the header asks for, with MNE's own scaling divided back out. Correct
      by construction for the dimensions MNE handles *and* the ones it mishandles: a
      ``nV`` channel gets ``1e-3 / 1`` and a lowercase ``uv`` channel gets ``1.0 / 1``,
      where a blanket ``* 1e6`` was wrong by nine and six orders of magnitude.
    * anything **else** — ``%``, ``mmHg``, or the generic ``a.u.`` standing in for a
      dimension the header never established — yields ``(unit, 1.0)``: the samples are
      handed over untouched and labelled with what they actually are. There is no
      honest conversion to microvolts here, and inventing one is what made a photic
      trigger line look like a 1e6-fold amplitude excursion.
    """
    unit = canonical_unit(dimension)
    gain = _mne_edf_gain(dimension)
    factor = to_microvolts(unit)
    if factor is None:
        return unit, 1.0 / gain
    return MICROVOLT, factor / gain


def load_signal_window(run: AnalysisRun, segment: SegmentPlan) -> SignalWindow:
    """Load one segment's padded context window as a :class:`SignalWindow`.

    The concrete implementation of the ``compute.tasks`` signal-loader seam.

    Preconditions are checked and refused explicitly rather than papered over,
    because every one of them would otherwise produce a plausible-looking window that
    means something other than it claims:

    * a derived ``input_version_id`` → ``NotImplementedError`` (materialisation is a
      later slice);
    * **mixed per-channel sampling rates** → ``NotImplementedError``, naming the
      resampled derived version that would satisfy the read. MNE would upsample the
      slow channels to the fastest rate and the contract's single ``fs`` cannot record
      that some rows are step-interpolated rather than measured;
    * a header whose signal count disagrees with the reader's → ``ValueError``, since
      rows could then not be attributed to a unit or a label at all;
    * a **context window spanning a splice** → ``ValueError`` (see
      :func:`_splices_in_window`). Correctly planned segments never do, because
      :func:`compute.segmentation.plan_segments` cuts at every splice; this catches the
      plan that was not built that way, and it is the last place anything can. A window
      that spans one hands the processor a step discontinuity it cannot see;
    * a missing source file → ``FileNotFoundError``; a degenerate window →
      ``ValueError`` (see :func:`_window_bounds`).
    """
    if run.input_version_id != SOURCE_VERSION_ID:
        raise NotImplementedError(
            f"Signal loader handles only the source version for now; run {run.pk} "
            f"scores derived version {run.input_version_id!r}. Materialising a "
            "derived version's signal is a pending slice."
        )

    path = Path(run.recording.file_path)
    if not path.is_file():
        raise FileNotFoundError(f"Source signal for recording {run.recording_id} not found at {path}.")

    header, all_infos = read_header(path)
    infos = [info for info in all_infos if not info.is_annotation_channel]

    rates = sorted({info.sampling_rate for info in infos})
    if len(rates) > 1:
        raise NotImplementedError(
            f"Recording {run.recording_id} mixes sampling rates "
            f"({', '.join(f'{r:g}' for r in rates)} Hz) and the analysis contract "
            "carries a single fs per window. Score a resampled derived version "
            "(a uniform-rate RECONSTRUCT output) instead of the source, so the "
            "interpolation is a recorded, auditable step rather than a side effect "
            "of the reader."
        )

    raw = _read_raw(path)
    if len(raw.ch_names) != len(infos):
        raise ValueError(
            f"Reader returned {len(raw.ch_names)} channels but the header declares "
            f"{len(infos)} non-annotation signals for recording {run.recording_id}; "
            "window rows cannot be attributed to header signals, so their units and "
            "labels are unknown. "
            f"reader: {list(raw.ch_names)!r} header: {[i.label for i in infos]!r}"
        )

    fs = float(raw.info["sfreq"])
    if rates and not math.isclose(fs, rates[0], rel_tol=1e-9, abs_tol=1e-9):
        raise ValueError(
            f"Reader reports {fs:g} Hz but the header declares {rates[0]:g} Hz for "
            f"recording {run.recording_id}; sample indices and times would disagree."
        )

    start_sample, stop_sample, t0_s = _window_bounds(segment.context_start_s, segment.context_end_s, fs, raw.n_times)

    # Checked before the samples are read, so a refused window costs no I/O beyond the
    # handful of record onsets the check itself seeks to.
    splices = _splices_in_window(path, header, all_infos, start_sample, stop_sample, fs)
    if splices:
        raise ValueError(
            f"Segment {segment.index} of run {run.pk} spans "
            f"{len(splices)} discontinuit{'y' if len(splices) == 1 else 'ies'} in "
            f"recording {run.recording_id}: its context window "
            f"[{start_sample / fs:g}, {stop_sample / fs:g})s crosses a splice at "
            f"{', '.join(f'{p:g}s' for p in splices)}. Acquisition paused there, so the "
            "sample before and the sample after sit side by side in the array while "
            "having been recorded minutes or hours apart — a step discontinuity no "
            "filter should be run across and one the processor has no way to detect "
            "from the samples. Plan segments with "
            "compute.segmentation.plan_segments(..., splices=...), which cuts the "
            "recording at every splice so no interior and no halo reaches across one."
        )

    data = raw.get_data(start=start_sample, stop=stop_sample)

    # Per-channel: canonical label + type from the ingest primitives, unit and scale
    # from the header's own physical dimension. Rows are scaled in place — the array
    # is freshly allocated by get_data — which keeps numpy out of this module's imports.
    canonical: list[str] = []
    types: list[str] = []
    units: list[str] = []
    for row, info in enumerate(infos):
        label = info.label.strip()
        sig_type, canon = classify_channel(label, extract_signal_type(label))
        canonical.append(canon)
        types.append(sig_type)
        unit, factor = channel_scale(info.physical_unit)
        units.append(unit)
        if factor != 1.0:
            data[row] *= factor

    return SignalWindow(
        data=data,
        channels=tuple(canonical),
        fs=fs,
        t0_s=t0_s,
        n_samples=int(data.shape[1]),
        channel_types=tuple(types),
        channel_units=tuple(units),
    )
