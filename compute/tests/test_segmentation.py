"""Tests for the temporal-decomposition core (compute/segmentation.py).

The focus is the correctness spine of analysis-execution-plan.md §3: interiors
partition the recording exactly, and ownership-by-onset makes the segmentation
resegmentation-invariant — a 1-segment and an N-segment run own the same events.

The second half covers **splices** (recordings/continuity-and-timelines.md): a
discontinuous recording is cut into contiguous runs first, so no interior and no halo
spans a gap, and the partition invariant above survives the subdivision.

Pure functions, no DB.
"""

import itertools

import pytest

from compute.segmentation import (
    SegmentPlan,
    contiguous_runs,
    is_segmentable,
    normalise_splices,
    owning_index,
    plan_segments,
)


def test_interiors_partition_the_recording_exactly():
    segs = plan_segments(duration_s=100.0, segment_length_s=30.0, halo_s=5.0)
    # Contiguous, gap-free, overlap-free interiors covering [0, 100).
    assert segs[0].interior_start_s == 0.0
    assert segs[-1].interior_end_s == 100.0
    for a, b in itertools.pairwise(segs):
        assert a.interior_end_s == b.interior_start_s
    # ceil(100/30) = 4 segments, last one short (90..100).
    assert len(segs) == 4
    assert (segs[-1].interior_start_s, segs[-1].interior_end_s) == (90.0, 100.0)


def test_context_pads_by_halo_and_clamps_to_recording():
    segs = plan_segments(duration_s=100.0, segment_length_s=30.0, halo_s=5.0)
    # First segment: no left pad (clamped at 0), right pad of halo.
    assert segs[0].context_start_s == 0.0
    assert segs[0].context_end_s == 35.0
    # Interior segment: padded both sides.
    assert (segs[1].context_start_s, segs[1].context_end_s) == (25.0, 65.0)
    # Last segment: right pad clamped at duration.
    assert segs[-1].context_end_s == 100.0


def test_every_onset_owned_by_exactly_one_segment():
    segs = plan_segments(duration_s=100.0, segment_length_s=30.0, halo_s=5.0)
    for onset in [0.0, 29.999, 30.0, 59.9, 90.0, 99.999]:
        owners = [s.index for s in segs if s.owns(onset)]
        assert len(owners) == 1, f"onset {onset} owned by {owners}"


def test_boundary_onset_belongs_to_later_segment_only():
    segs = plan_segments(duration_s=60.0, segment_length_s=30.0, halo_s=10.0)
    # Onset exactly on the 30.0 seam → owned by segment 1, not 0 (half-open).
    assert segs[0].owns(30.0) is False
    assert segs[1].owns(30.0) is True


@pytest.mark.parametrize("segment_length", [7.0, 10.0, 30.0, 33.3, 250.0])
def test_resegmentation_invariance(segment_length):
    """For any segment_length, a fixed set of onsets is owned by exactly one
    segment each — so the emitted set is identical regardless of fan-out width."""
    duration = 200.0
    onsets = [0.0, 5.5, 42.1, 99.9, 100.0, 150.0, 199.9]
    segs = plan_segments(duration_s=duration, segment_length_s=segment_length, halo_s=4.0)
    for onset in onsets:
        owners = [s.index for s in segs if s.owns(onset)]
        assert len(owners) == 1, f"L={segment_length} onset={onset} owners={owners}"
    # And the owner set covers every onset (none dropped).
    owned = {owning_index(segs, o) for o in onsets}
    assert None not in owned


def test_global_locality_runs_monolithically():
    segs = plan_segments(duration_s=100.0, segment_length_s=30.0, halo_s=5.0, locality="global")
    assert len(segs) == 1
    s = segs[0]
    # One segment owning the whole recording, no halo (context == interior).
    assert (s.interior_start_s, s.interior_end_s) == (0.0, 100.0)
    assert (s.context_start_s, s.context_end_s) == (0.0, 100.0)


def test_segment_length_at_or_above_duration_is_monolithic():
    segs = plan_segments(duration_s=50.0, segment_length_s=50.0, halo_s=5.0)
    assert len(segs) == 1
    assert segs[0].context_start_s == 0.0 and segs[0].context_end_s == 50.0


def test_zero_or_negative_duration_yields_no_segments():
    assert plan_segments(duration_s=0.0, segment_length_s=10.0) == []
    assert plan_segments(duration_s=-5.0, segment_length_s=10.0) == []


def test_is_segmentable():
    assert is_segmentable("window_independent")
    assert is_segmentable("local_context")
    assert not is_segmentable("global")


def test_owning_index_out_of_range_is_none():
    segs = plan_segments(duration_s=60.0, segment_length_s=30.0)
    assert owning_index(segs, -1.0) is None
    assert owning_index(segs, 60.0) is None  # == duration, past the last interior
    assert owning_index(segs, 45.0) == 1


# ── Splices ───────────────────────────────────────────────────────────────────────
#
# A gap is a zero-width seam in the data-position timeline, not an interval, so a
# splice list is a list of instants and the recording is cut *at* them.


class TestNormaliseSplices:
    def test_sorts_and_keeps_interior_positions(self):
        assert normalise_splices(100.0, [70.0, 10.0, 40.0]) == [10.0, 40.0, 70.0]

    def test_drops_positions_at_or_outside_the_recording_bounds(self):
        # A splice at 0 or at duration cuts nothing off; one outside is not addressable.
        assert normalise_splices(100.0, [0.0, 100.0, -5.0, 150.0]) == []
        assert normalise_splices(100.0, [0.0, 50.0, 100.0]) == [50.0]

    def test_collapses_near_duplicates(self):
        # Float noise in a stored timestamp must not produce a zero-length run.
        assert normalise_splices(100.0, [50.0, 50.0, 50.0 + 1e-12]) == [50.0]

    def test_accepts_any_iterable_of_numbers(self):
        assert normalise_splices(10.0, (3, 6)) == [3.0, 6.0]


class TestContiguousRuns:
    def test_continuous_recording_is_one_run(self):
        assert contiguous_runs(100.0) == [(0.0, 100.0)]
        assert contiguous_runs(100.0, []) == [(0.0, 100.0)]

    def test_one_splice_yields_two_runs(self):
        assert contiguous_runs(307.0, [29.0]) == [(0.0, 29.0), (29.0, 307.0)]

    def test_runs_cover_the_recording_exactly(self):
        runs = contiguous_runs(100.0, [10.0, 55.5, 90.0])
        assert runs[0][0] == 0.0
        assert runs[-1][1] == 100.0
        for a, b in itertools.pairwise(runs):
            assert a[1] == b[0]  # abutting, because a splice has no width
        assert sum(end - start for start, end in runs) == pytest.approx(100.0)

    def test_empty_recording_has_no_runs(self):
        assert contiguous_runs(0.0, [5.0]) == []


class TestSpliceAwareSegmentation:
    def test_no_interior_spans_a_splice(self):
        splices = [29.0, 140.0]
        segs = plan_segments(duration_s=307.0, segment_length_s=60.0, halo_s=0.7, splices=splices)
        for s in segs:
            for splice in splices:
                assert not (s.interior_start_s < splice < s.interior_end_s), (
                    f"segment {s.index} interior [{s.interior_start_s}, {s.interior_end_s}) spans {splice}"
                )

    def test_no_context_crosses_a_splice(self):
        # The halo is the whole point: a detector's receptive field must not straddle
        # the seam, or it filters across a step discontinuity it cannot detect.
        splices = [29.0, 140.0]
        segs = plan_segments(duration_s=307.0, segment_length_s=60.0, halo_s=15.0, splices=splices)
        for s in segs:
            for splice in splices:
                assert not (s.context_start_s < splice < s.context_end_s), (
                    f"segment {s.index} context [{s.context_start_s}, {s.context_end_s}) crosses {splice}"
                )

    def test_interiors_still_partition_the_recording_exactly(self):
        # Cutting at a splice *subdivides* the partition, it does not perforate it.
        segs = plan_segments(duration_s=307.0, segment_length_s=60.0, halo_s=5.0, splices=[29.0, 140.0])
        assert segs[0].interior_start_s == 0.0
        assert segs[-1].interior_end_s == 307.0
        for a, b in itertools.pairwise(segs):
            assert a.interior_end_s == b.interior_start_s

    @pytest.mark.parametrize("segment_length", [7.0, 13.0, 60.0, 500.0])
    def test_resegmentation_invariance_holds_with_splices(self, segment_length):
        onsets = [0.0, 28.999, 29.0, 100.0, 139.9, 140.0, 306.9]
        segs = plan_segments(
            duration_s=307.0,
            segment_length_s=segment_length,
            halo_s=4.0,
            splices=[29.0, 140.0],
        )
        for onset in onsets:
            owners = [s.index for s in segs if s.owns(onset)]
            assert len(owners) == 1, f"L={segment_length} onset={onset} → {owners}"

    def test_onset_on_a_splice_belongs_to_the_run_that_follows(self):
        # Same half-open rule as any other seam: the sample at 29.0 is the first
        # sample of the second run, so the second run owns it.
        segs = plan_segments(duration_s=307.0, segment_length_s=60.0, splices=[29.0])
        assert segs[0].owns(29.0) is False
        assert segs[1].owns(29.0) is True
        assert segs[1].run_index == 1

    def test_run_index_increments_at_each_splice(self):
        segs = plan_segments(duration_s=100.0, segment_length_s=15.0, splices=[30.0, 60.0])
        by_run: dict[int, list[SegmentPlan]] = {}
        for s in segs:
            by_run.setdefault(s.run_index, []).append(s)
        assert sorted(by_run) == [0, 1, 2]
        assert by_run[0][0].interior_start_s == 0.0
        assert by_run[0][-1].interior_end_s == 30.0
        assert by_run[1][0].interior_start_s == 30.0
        assert by_run[2][-1].interior_end_s == 100.0
        # Indices are unique and consecutive across the whole plan, not per run —
        # AnalysisSegment is keyed on (run, index).
        assert [s.index for s in segs] == list(range(len(segs)))

    def test_splice_flags_distinguish_a_seam_from_the_recording_edges(self):
        segs = plan_segments(duration_s=307.0, segment_length_s=60.0, halo_s=0.7, splices=[29.0])
        first, second = segs[0], segs[1]
        # Segment 0 starts at the recording's own start — short halo, but not a splice.
        assert first.context_starts_at_splice is False
        assert first.context_ends_at_splice is True
        assert (first.context_start_s, first.context_end_s) == (0.0, 29.0)
        # Segment 1 begins the second run: its left halo is withheld signal.
        assert second.context_starts_at_splice is True
        assert second.context_ends_at_splice is False
        assert (second.context_start_s, second.context_end_s) == (29.0, 89.7)
        # The final segment ends at the recording's own end — again not a splice.
        assert segs[-1].context_ends_at_splice is False
        assert segs[-1].context_end_s == 307.0

    def test_no_flag_when_the_halo_fits_inside_the_run(self):
        # A run long enough that the halo never reaches its bounds sets no flag.
        segs = plan_segments(duration_s=100.0, segment_length_s=10.0, halo_s=1.0, splices=[50.0])
        middle = [s for s in segs if 20.0 <= s.interior_start_s <= 30.0]
        assert middle
        for s in middle:
            assert s.context_starts_at_splice is False
            assert s.context_ends_at_splice is False

    def test_zero_halo_never_flags_a_splice(self):
        # With no halo nothing is withheld, so there is nothing to warn about.
        segs = plan_segments(duration_s=100.0, segment_length_s=10.0, halo_s=0.0, splices=[50.0])
        assert not any(s.context_starts_at_splice for s in segs)
        assert not any(s.context_ends_at_splice for s in segs)

    def test_a_run_shorter_than_the_segment_length_is_one_segment(self):
        # The splice at 5.0 leaves a 5 s run; it is not padded out or merged away.
        segs = plan_segments(duration_s=100.0, segment_length_s=30.0, halo_s=2.0, splices=[5.0])
        assert (segs[0].interior_start_s, segs[0].interior_end_s) == (0.0, 5.0)
        assert (segs[0].context_start_s, segs[0].context_end_s) == (0.0, 5.0)
        assert segs[0].run_index == 0
        assert segs[1].run_index == 1

    def test_continuous_behaviour_is_unchanged_by_the_splice_parameter(self):
        # The regression guard: passing no splices must reproduce the old plan
        # exactly, field for field.
        without = plan_segments(duration_s=100.0, segment_length_s=30.0, halo_s=5.0)
        with_empty = plan_segments(duration_s=100.0, segment_length_s=30.0, halo_s=5.0, splices=[])
        assert without == with_empty
        assert [(s.interior_start_s, s.interior_end_s, s.context_start_s, s.context_end_s) for s in without] == [
            (0.0, 30.0, 0.0, 35.0),
            (30.0, 60.0, 25.0, 65.0),
            (60.0, 90.0, 55.0, 95.0),
            (90.0, 100.0, 85.0, 100.0),
        ]
        assert all(s.run_index == 0 for s in without)

    def test_global_locality_ignores_splices(self):
        # Holistic is what it declared itself to be: a stage that asks for the whole
        # recording gets the whole recording and reads the Interruption rows itself.
        segs = plan_segments(
            duration_s=307.0,
            segment_length_s=60.0,
            halo_s=5.0,
            locality="global",
            splices=[29.0],
        )
        assert len(segs) == 1
        assert (segs[0].interior_start_s, segs[0].interior_end_s) == (0.0, 307.0)

    def test_out_of_range_splices_do_not_change_the_plan(self):
        clean = plan_segments(duration_s=100.0, segment_length_s=30.0, halo_s=5.0)
        noisy = plan_segments(
            duration_s=100.0,
            segment_length_s=30.0,
            halo_s=5.0,
            splices=[0.0, 100.0, 250.0, -3.0],
        )
        assert clean == noisy

    def test_matches_the_reference_recording(self):
        # testdata/test.edf: 307 s, EDF+D, one 1 s gap keyed at data position 29.0.
        segs = plan_segments(duration_s=307.0, segment_length_s=60.0, halo_s=0.7, splices=[29.0])
        assert [(s.run_index, s.interior_start_s, s.interior_end_s) for s in segs] == [
            (0, 0.0, 29.0),
            (1, 29.0, 89.0),
            (1, 89.0, 149.0),
            (1, 149.0, 209.0),
            (1, 209.0, 269.0),
            (1, 269.0, 307.0),
        ]
