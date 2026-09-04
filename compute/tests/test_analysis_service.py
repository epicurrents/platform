"""Tests for the analysis-run write service (compute/runs.py).

Real-DB integration tests for get_or_create_run and commit_segment against the
contract's EventSpec/LabelSpec: idempotency, the coverage-row guard, the skip
path, halo-overlap dedup, channel capture, and label persistence.
"""

import pytest
from django.contrib.auth import get_user_model

from annotations.models import Event
from compute.contract import EventSpec, LabelSpec
from compute.models import AnalysisRun, AnalysisSegment, RunAnnotation
from compute.runs import commit_segment, get_or_create_run
from recordings.models import Recording


@pytest.fixture()
def user(db):
    return get_user_model().objects.create_user(username="analyst", password="x")


def _recording(user, stem="ABC00000000000000000000000000001"):
    return Recording.objects.create(
        author=user,
        original_name="rec.edf",
        stored_name=f"{stem}.edf",
        file_size=1024,
        file_path="/tmp/rec.edf",
        status=Recording.Status.READY,
    )


def _run(recording, *, digest="d1", version="source"):
    run, _ = get_or_create_run(
        recording=recording,
        input_version_id=version,
        produces_kind="spike_events",
        input_digest=digest,
        image_digest="img1",
        params={"threshold": 0.43},
        grid_s=10.0,
        locality=AnalysisRun.Locality.WINDOW_INDEPENDENT,
    )
    return run


@pytest.mark.django_db
def test_get_or_create_run_is_idempotent(user):
    rec = _recording(user)
    assert _run(rec).pk == _run(rec).pk
    assert AnalysisRun.objects.count() == 1


@pytest.mark.django_db
def test_commit_segment_writes_events_links_and_flips_done(user):
    rec = _recording(user)
    run = _run(rec)

    seg = commit_segment(
        run=run,
        index=0,
        start_s=0.0,
        end_s=10.0,
        events=[
            EventSpec(
                kind="spike_events",
                onset_s=1.5,
                duration_s=0.1,
                channels=("F7", "T7"),
                confidence=0.9,
                extra={"peak_uv": 120.0},
            )
        ],
    )

    assert seg.state == AnalysisSegment.State.DONE
    assert Event.objects.count() == 1
    ev = Event.objects.get()
    assert ev.version_id == "source"
    assert ev.author.username == "__system__"
    assert ev.target_object_id == str(rec.pk)
    assert ev.event_class == "spike_events"
    assert ev.timestamp == 1.5
    # Channels ride in value JSON (interim), alongside confidence + extra.
    assert ev.value["channels"] == ["F7", "T7"]
    assert ev.value["confidence"] == 0.9
    assert ev.value["peak_uv"] == 120.0
    assert RunAnnotation.objects.filter(run=run, event=ev, segment_index=0).count() == 1


@pytest.mark.django_db
def test_commit_segment_persists_labels_as_typed_events(user):
    rec = _recording(user)
    run = _run(rec)

    commit_segment(
        run=run,
        index=0,
        start_s=0.0,
        end_s=30.0,
        labels=[LabelSpec(kind="sleep_stage", value="N2", onset_s=0.0, duration_s=30.0)],
    )

    ev = Event.objects.get()
    assert ev.event_class == "sleep_stage"
    assert ev.name == "N2"
    assert ev.value["value"] == "N2"  # the label's class preserved in payload
    assert RunAnnotation.objects.filter(run=run).count() == 1


@pytest.mark.django_db
def test_commit_segment_is_idempotent_on_retry(user):
    rec = _recording(user)
    run = _run(rec)
    kw = {
        "run": run,
        "index": 0,
        "start_s": 0.0,
        "end_s": 10.0,
        "events": [EventSpec(kind="spike_events", onset_s=1.5)],
    }
    commit_segment(**kw)
    commit_segment(**kw)  # coverage-row guard: no-op

    assert Event.objects.count() == 1
    assert RunAnnotation.objects.count() == 1


@pytest.mark.django_db
def test_commit_segment_skip_records_reason(user):
    rec = _recording(user)
    run = _run(rec)
    seg = commit_segment(run=run, index=2, start_s=20.0, end_s=30.0, events=[], skip_reason="data_gap")
    assert seg.state == AnalysisSegment.State.SKIPPED
    assert seg.skip_reason == "data_gap"
    assert Event.objects.count() == 0


@pytest.mark.django_db
def test_halo_overlap_event_dedups_across_segments(user):
    rec = _recording(user)
    run = _run(rec)
    spec = EventSpec(kind="spike_events", onset_s=9.9, duration_s=0.2)

    commit_segment(run=run, index=0, start_s=0.0, end_s=10.0, events=[spec])
    commit_segment(run=run, index=1, start_s=10.0, end_s=20.0, events=[spec])

    assert Event.objects.count() == 1
    assert RunAnnotation.objects.count() == 1
    assert RunAnnotation.objects.get().segment_index == 0
    assert AnalysisSegment.objects.filter(state=AnalysisSegment.State.DONE).count() == 2


@pytest.mark.django_db
def test_distinct_runs_do_not_share_events(user):
    rec = _recording(user)
    run_a = _run(rec, digest="d1")
    run_b = _run(rec, digest="d2")
    spec = EventSpec(kind="spike_events", onset_s=1.5)

    commit_segment(run=run_a, index=0, start_s=0.0, end_s=10.0, events=[spec])
    commit_segment(run=run_b, index=0, start_s=0.0, end_s=10.0, events=[spec])

    assert Event.objects.count() == 2
    assert RunAnnotation.objects.count() == 2


@pytest.mark.django_db
def test_same_event_on_different_channels_stays_distinct(user):
    rec = _recording(user)
    run = _run(rec)

    commit_segment(
        run=run,
        index=0,
        start_s=0.0,
        end_s=10.0,
        events=[
            EventSpec(kind="spike_events", onset_s=5.0, channels=("F7",)),
            EventSpec(kind="spike_events", onset_s=5.0, channels=("T7",)),
        ],
    )

    # Channels are in the identity hash, so these are two events, not one.
    assert Event.objects.count() == 2
