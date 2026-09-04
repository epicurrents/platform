"""Tests for the Celery analysis-execution orchestration (compute/tasks.py).

Runs under CELERY_TASK_ALWAYS_EAGER, so launch → group → segment tasks →
finalisation execute synchronously. A fake signal loader stands in for the
version-signal materialisation slice, and fake processors stand in for detectors;
both conform to the contract (compute/contract.py).
"""

import pytest
from django.contrib.auth import get_user_model

from annotations.models import Event
from compute.contract import AnalysisOutput, EventSpec, LabelSpec, SignalWindow
from compute.models import AnalysisRun, AnalysisSegment, RunAnnotation
from compute.runs import get_or_create_run
from compute.tasks import (
    _PROCESSORS,
    launch_analysis_run,
    register_processor,
    run_analysis_segment,
    set_signal_loader,
)
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


@pytest.fixture(autouse=True)
def signal_loader():
    """Fake loader: a 1 Hz single-channel window spanning the segment context, so a
    processor emitting at the context midpoint lands in the interior."""

    def _loader(run, seg):
        n = max(int(seg.context_end_s - seg.context_start_s), 1)
        return SignalWindow(
            data=[[0.0] * n],
            channels=("Fp1",),
            fs=1.0,
            t0_s=seg.context_start_s,
            n_samples=n,
        )

    set_signal_loader(_loader)
    yield
    set_signal_loader(None)


@pytest.fixture()
def processor():
    registered = []

    def _register(name, fn):
        register_processor(name, fn)
        registered.append(name)
        return name

    yield _register
    for name in registered:
        _PROCESSORS.pop(name, None)


def _launch_kwargs(recording, detector, **over):
    kw = {
        "recording_id": recording.pk,
        "detector": detector,
        "produces_kind": "spike_events",
        "input_version_id": "source",
        "input_digest": "d1",
        "image_digest": "img1",
        "params": {},
        "grid_s": 1.0,
        "halo_s": 5.0,
        "max_event_span_s": 1.0,
        "locality": "window_independent",
        "duration_s": 100.0,
        "segment_length_s": 30.0,
    }
    kw.update(over)
    return kw


def _midpoint(window, context):
    onset = window.t0_s + window.n_samples / window.fs / 2.0
    return AnalysisOutput(events=(EventSpec(kind="spike_events", onset_s=onset),))


@pytest.mark.django_db
def test_launch_fans_out_all_segments_and_finalises(user, processor):
    processor("mid", _midpoint)
    rec = _recording(user)

    launch_analysis_run.delay(**_launch_kwargs(rec, "mid"))

    run = AnalysisRun.objects.get(input_digest="d1", image_digest="img1")
    assert run.state == AnalysisRun.State.DONE
    assert run.finished_at is not None
    assert run.segments.count() == 4  # ceil(100/30)
    assert run.segments.filter(state=AnalysisSegment.State.DONE).count() == 4
    # One owned event per segment (each context midpoint sits in its interior).
    assert Event.objects.filter(version_id="source").count() == 4
    assert RunAnnotation.objects.filter(run=run).count() == 4


@pytest.mark.django_db
def test_ownership_assigns_boundary_event_to_the_owning_segment(user, processor):
    # Every segment "detects" an event at t=30.0 (the seg0/seg1 interior seam);
    # it is in seg0's halo and seg1's interior, so ownership gives it to seg1.
    processor(
        "boundary",
        lambda w, c: AnalysisOutput(events=(EventSpec(kind="spike_events", onset_s=30.0),)),
    )
    rec = _recording(user)

    launch_analysis_run.delay(**_launch_kwargs(rec, "boundary"))

    run = AnalysisRun.objects.get(input_digest="d1", image_digest="img1")
    assert Event.objects.count() == 1
    assert RunAnnotation.objects.get(run=run).segment_index == 1


@pytest.mark.django_db
def test_whole_window_label_emitted_once_by_segment_zero(user, processor):
    processor(
        "stager",
        lambda w, c: AnalysisOutput(
            labels=(LabelSpec(kind="recording_quality", value="good"),)  # onset None
        ),
    )
    rec = _recording(user)

    launch_analysis_run.delay(**_launch_kwargs(rec, "stager"))

    AnalysisRun.objects.get(input_digest="d1", image_digest="img1")
    # Null-onset label assigned to segment 0 only, so exactly one across 4 segments.
    assert Event.objects.filter(event_class="recording_quality").count() == 1


@pytest.mark.django_db
def test_global_locality_runs_as_one_segment(user, processor):
    processor(
        "one",
        lambda w, c: AnalysisOutput(events=(EventSpec(kind="spike_events", onset_s=10.0),)),
    )
    rec = _recording(user)

    launch_analysis_run.delay(**_launch_kwargs(rec, "one", locality="global"))

    run = AnalysisRun.objects.get(input_digest="d1", image_digest="img1")
    assert run.state == AnalysisRun.State.DONE
    assert run.segments.count() == 1
    assert Event.objects.count() == 1


@pytest.mark.django_db
def test_relaunch_is_idempotent_resume(user, processor):
    processor("mid", _midpoint)
    rec = _recording(user)
    kw = _launch_kwargs(rec, "mid")

    launch_analysis_run.delay(**kw)
    launch_analysis_run.delay(**kw)

    run = AnalysisRun.objects.get(input_digest="d1", image_digest="img1")
    assert run.state == AnalysisRun.State.DONE
    assert run.segments.count() == 4
    assert Event.objects.filter(version_id="source").count() == 4


@pytest.mark.django_db
def test_segment_failure_marks_segment_and_run_failed(user, processor):
    def boom(window, context):
        raise RuntimeError("detector crashed")

    processor("boom", boom)
    rec = _recording(user)
    run, _ = get_or_create_run(
        recording=rec,
        input_version_id="source",
        produces_kind="spike_events",
        input_digest="d1",
        image_digest="img1",
        params={},
        grid_s=1.0,
        halo_s=0.0,
        max_event_span_s=1.0,
        locality=AnalysisRun.Locality.WINDOW_INDEPENDENT,
    )
    AnalysisRun.objects.filter(pk=run.pk).update(state=AnalysisRun.State.RUNNING)
    AnalysisSegment.objects.create(run=run, index=0, start_s=0.0, end_s=10.0)

    with pytest.raises(RuntimeError):
        run_analysis_segment.delay(
            run_id=run.pk,
            detector="boom",
            index=0,
            interior_start_s=0.0,
            interior_end_s=10.0,
            context_start_s=0.0,
            context_end_s=10.0,
        )

    run.refresh_from_db()
    assert run.state == AnalysisRun.State.FAILED
    assert AnalysisSegment.objects.get(run=run, index=0).state == AnalysisSegment.State.FAILED
