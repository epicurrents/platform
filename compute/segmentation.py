"""Temporal decomposition — planning a run's segments and owning its events.

The pure core of the execution layer (analysis-execution-plan.md §1, §3). Given a
recording's duration and a stage's ``halo`` plus a dispatch-time ``segment_length``,
it partitions the recording into segments and answers *which segment owns an event*.

Two invariants make a segmented run byte-identical to a single-task run — the
correctness spine of §3:

* **Interiors partition ``[0, duration)`` exactly** — no gaps, no overlaps. Segment
  *k* owns ``[k·L, (k+1)·L)`` (the last clamped to ``duration``).
* **An event is owned by the one segment whose interior contains its onset.** Two
  neighbours sharing a halo both *see* an event in the overlap, but only the one
  whose interior holds the onset *emits* it — so no cross-task coordination is
  needed to avoid double-counting, and the assignment is independent of
  ``segment_length``. A 1-segment and a 50-segment run therefore emit the same set.

``segment_length`` is a throughput knob and is deliberately **not** part of run
identity (§3.1); it lives here as a dispatch argument, never on ``AnalysisRun``.

**Splices.** A discontinuous recording is not one signal but several, spliced end to
end (see ``recordings/continuity-and-timelines.md``). In the data-position timeline a
gap is not an interval — it is a zero-width seam at which the sample before and the
sample after were recorded minutes or hours apart. Handing a detector a window that
spans one is handing it a step discontinuity no filter should be run across, and it
has no way to detect that from the samples alone.

So splices are **hard boundaries**: no interior spans one, and no halo reaches across
one. The recording is first cut into maximal contiguous *runs* and each run is then
segmented as if it were its own recording, which is what it physically is. The
partition invariant survives unchanged — the union of the runs is still exactly
``[0, duration)`` — so ownership stays total and resegmentation-invariant.

Splices come from the recording, not from the dispatcher: they are the
``annotations.Interruption`` rows written at ingest, on the same timeline. That keeps
``segment_length`` the only throughput knob and keeps splice handling out of run
identity.

Pure Python — no Django, no I/O — so it can be exhaustively unit-tested and reused
by the Celery dispatcher and any resegmentation oracle alike.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

#: Locality values that can be sliced (mirror ``compute.models.AnalysisRun.Locality``;
#: duplicated here to keep this core Django-free). A ``global`` stage is holistic and
#: runs as a single monolithic segment.
SEGMENTABLE_LOCALITIES = frozenset({"window_independent", "local_context"})
GLOBAL_LOCALITY = "global"


@dataclass(frozen=True)
class SegmentPlan:
    """One planned segment.

    * ``interior`` — ``[interior_start_s, interior_end_s)`` — the region this segment
      *owns*; events with an onset here are emitted by this segment and no other.
    * ``context`` — ``[context_start_s, context_end_s)`` — the interior padded by the
      halo (clamped to the recording *and* to the enclosing contiguous run), the
      signal the detector actually reads so its receptive field has context at the
      interior edges.
    * ``run_index`` — which contiguous run of the recording this segment lies in. ``0``
      throughout a continuous recording; incremented at each splice.
    * ``context_starts_at_splice`` / ``context_ends_at_splice`` — the halo on that side
      was cut short by a **splice** rather than by the recording's own start or end.
      A processor that needs its full receptive field should check these: the context
      is short, and unlike at the recording edges there is signal beyond it that is
      deliberately withheld because it is not continuous with this segment.
    """

    index: int
    interior_start_s: float
    interior_end_s: float
    context_start_s: float
    context_end_s: float
    run_index: int = 0
    context_starts_at_splice: bool = False
    context_ends_at_splice: bool = False

    def owns(self, onset_s: float) -> bool:
        """True iff *onset_s* falls in this segment's half-open interior.

        Half-open ``[start, end)`` so an onset exactly on a segment boundary belongs
        to the later segment and never to both — the deterministic ownership rule of
        §3.3.
        """
        return self.interior_start_s <= onset_s < self.interior_end_s

    @property
    def context_duration_s(self) -> float:
        return self.context_end_s - self.context_start_s


def is_segmentable(locality: str) -> bool:
    """Whether a stage of this locality is fanned out (vs run monolithically)."""
    return locality in SEGMENTABLE_LOCALITIES


def normalise_splices(duration_s: float, splices: Iterable[float] = (), *, tol: float = 1e-9) -> list[float]:
    """Return the usable splice positions in ``(0, duration_s)``, sorted and unique.

    Positions at or outside the recording's own bounds are dropped rather than
    rejected: a splice at 0 or at ``duration`` cuts nothing off, and callers get their
    splice list from ingest, where a gap recorded at the very first record is a real
    and harmless case. Near-duplicates within *tol* collapse, so float noise in a
    stored timestamp cannot produce a zero-length run.
    """
    kept: list[float] = []
    for raw in sorted(float(s) for s in splices):
        if raw <= tol or raw >= duration_s - tol:
            continue
        if kept and raw - kept[-1] <= tol:
            continue
        kept.append(raw)
    return kept


def contiguous_runs(duration_s: float, splices: Iterable[float] = ()) -> list[tuple[float, float]]:
    """Partition ``[0, duration_s)`` into maximal contiguous runs, cut at each splice.

    Returns ``[(start, end), ...]`` in order, always covering ``[0, duration_s)``
    exactly — a continuous recording yields the single run ``[(0.0, duration_s)]``.
    Each run is a stretch of signal with no discontinuity inside it, which is the unit
    a detector can legitimately be handed.
    """
    if duration_s <= 0:
        return []
    bounds = [0.0, *normalise_splices(duration_s, splices), duration_s]
    return [(bounds[i], bounds[i + 1]) for i in range(len(bounds) - 1)]


def plan_segments(
    *,
    duration_s: float,
    segment_length_s: float,
    halo_s: float = 0.0,
    locality: str = "window_independent",
    splices: Sequence[float] = (),
) -> list[SegmentPlan]:
    """Partition ``[0, duration_s)`` into owned segments with halo-padded context.

    A ``global`` (holistic) stage yields a single segment spanning the whole recording
    with no halo: the whole signal is the context, and it owns everything. Splices are
    deliberately **not** cut for a global stage — holistic is what it declared itself
    to be, and a stage that asks for the entire recording has to cope with the entire
    recording. It can read the ``Interruption`` rows itself if it needs to.

    Otherwise the recording is cut at every splice into contiguous runs, and each run
    is independently partitioned into ``ceil(len / segment_length)`` interiors of
    length ``segment_length`` (the last shorter), each padded by ``halo_s`` on both
    sides and clamped to *that run's* bounds. A non-positive ``segment_length``, or one
    at least as long as a run, gives that run a single segment.

    The returned interiors always cover ``[0, duration_s)`` exactly — cutting at
    splices subdivides the partition, it does not perforate it — so for *any*
    ``segment_length`` every onset in range is owned by exactly one segment, which is
    the resegmentation invariance of §3. Segment indices are unique and consecutive
    across the whole plan, not per run, because ``AnalysisSegment`` is keyed on
    ``(run, index)``.
    """
    if duration_s <= 0:
        return []

    if not is_segmentable(locality):
        return [SegmentPlan(0, 0.0, duration_s, 0.0, duration_s)]

    segments: list[SegmentPlan] = []
    runs = contiguous_runs(duration_s, splices)
    for run_index, (run_start, run_end) in enumerate(runs):
        run_length = run_end - run_start
        if segment_length_s <= 0 or segment_length_s >= run_length:
            n, length = 1, run_length
        else:
            n, length = math.ceil(run_length / segment_length_s), segment_length_s
        for k in range(n):
            start = run_start + k * length
            end = min(run_start + (k + 1) * length, run_end)
            # Clamp the halo to the run, not the recording: reaching past either end
            # would cross a splice and splice the discontinuity into the context.
            context_start = max(run_start, start - halo_s)
            context_end = min(run_end, end + halo_s)
            segments.append(
                SegmentPlan(
                    index=len(segments),
                    interior_start_s=start,
                    interior_end_s=end,
                    context_start_s=context_start,
                    context_end_s=context_end,
                    run_index=run_index,
                    # A halo cut short *at the recording's own* start or end is not a
                    # splice; only a run boundary in the interior of the recording is.
                    context_starts_at_splice=(
                        run_start > 0.0 and context_start <= run_start and start - halo_s < run_start
                    ),
                    context_ends_at_splice=(run_end < duration_s and context_end >= run_end and end + halo_s > run_end),
                )
            )
    return segments


def owning_index(segments: list[SegmentPlan], onset_s: float) -> int | None:
    """Return the index of the segment that owns *onset_s*, or ``None`` if out of range.

    A convenience over :meth:`SegmentPlan.owns` for filtering a detector's output down
    to the events a given segment is allowed to emit.
    """
    for seg in segments:
        if seg.owns(onset_s):
            return seg.index
    return None
