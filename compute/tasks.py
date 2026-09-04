"""Analysis execution — the Celery fan-out over a run's segments.

Turns the write primitives (``compute.runs``) and the temporal-decomposition core
(``compute.segmentation``) into an actual, non-blocking run:

* :func:`launch_analysis_run` — plans the segments, creates their coverage rows,
  marks the run RUNNING, and **fans out a Celery group** of one task per segment.
  It returns immediately; nothing runs on the request thread.
* :func:`run_analysis_segment` — on a worker: runs the run's registered processor
  over the segment's halo-padded context, keeps only the events this segment
  *owns* (onset in its interior, §3.3), and commits them via
  :func:`~compute.runs.commit_segment`.

**Finalisation without a chord.** Rather than a chord callback (which is fragile
under eager mode and can be lost if the result backend hiccups), the *last segment
to finish* flips the run to its terminal state: after each commit the task checks
whether every planned segment is terminal and, if so, atomically transitions the
run with ``filter(state=RUNNING).update(...)`` — idempotent and race-safe, so
concurrent finishers can't double-finalise. This also composes with resumability
(§4): re-launching re-fans-out only the incomplete segments, and whichever lands
last finalises.

The **processor** — the thing that reads signal and runs a detector — is looked up
from a registry by name, so this orchestration is detector-agnostic. Concrete
processors (a detector plus its signal loading) register themselves and are wired
in a later slice; the orchestration here is complete and testable with any processor.

**Discontinuity.** :func:`launch_analysis_run` reads the recording's splices itself
(:func:`splices_for`) and hands them to :func:`~compute.segmentation.plan_segments`,
so no segment interior or halo ever spans a gap. It is deliberately not a caller
argument: a discontinuous recording quietly segmented as if it were continuous is
the failure mode worth designing against, and most of the archive is discontinuous,
so the safe behaviour has to be the one every launch path gets for free.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from celery import group, shared_task
from django.utils import timezone

from recordings.models import Recording
from recordings.pipeline.manifest import SOURCE_VERSION_ID

from .contract import AnalysisOutput, RunContext, SignalWindow
from .models import AnalysisRun, AnalysisSegment
from .runs import commit_segment, get_or_create_run
from .segmentation import SegmentPlan, plan_segments

logger = logging.getLogger(__name__)


def splices_for(recording: Recording) -> list[float]:
    """Return the recording's splice positions, in the data-position timeline.

    One per ``annotations.Interruption`` row — the gaps ingest found and stored (see
    ``recordings/continuity-and-timelines.md``). ``Interruption.timestamp`` is already a
    data position, so no translation happens here; that is the whole point of having
    picked one canonical timeline.

    Read at plan time rather than passed in by the caller, so every launch path is
    splice-aware by default. A discontinuous recording silently segmented as if it were
    continuous is the failure mode worth designing against — most of the archive is
    discontinuous, so the safe behaviour has to be the one you get for free.

    Only ``source``-bound rows are read. Ingest writes the gaps it found in the source
    file and binds them to the base version; nothing yet writes per-version continuity,
    and no derived version we currently produce changes where the seams are — resampling
    moves samples, not splices. When a version *does* change the gap structure (a future
    RECONSTRUCT that splits or concatenates), it will have to write its own rows and this
    lookup will take the run's ``input_version_id``. Reading every version's rows
    indiscriminately would be the wrong shortcut: it would silently union the seams of
    versions this run is not looking at.
    """
    from django.contrib.contenttypes.models import ContentType

    from annotations.models import Interruption

    recording_ct = ContentType.objects.get_for_model(recording, for_concrete_model=False)
    return sorted(
        Interruption.objects.filter(
            target_content_type=recording_ct,
            target_object_id=str(recording.pk),
            version_id=SOURCE_VERSION_ID,
        ).values_list("timestamp", flat=True)
    )


# A processor: signal + run context in, findings out (compute/contract.py). It does
# not load signal or know about segmentation; it returns findings across the whole
# window and the pipeline keeps only the ones this segment owns.
ProcessorFn = Callable[[SignalWindow, RunContext], AnalysisOutput]

# A signal loader materialises the padded context window for one segment at the run's
# input_version_id. The concrete loader (source-EDF reader now; derived-version
# materialisation later) is a pending slice; register one via set_signal_loader. Kept
# an injectable seam so the orchestration is testable with a fake loader.
SignalLoaderFn = Callable[["AnalysisRun", SegmentPlan], SignalWindow]

_PROCESSORS: dict[str, ProcessorFn] = {}
_signal_loader: SignalLoaderFn | None = None


def register_processor(name: str, fn: ProcessorFn | None = None):
    """Register a processor under *name*. Usable as ``register_processor('x', fn)``
    or as a decorator ``@register_processor('x')``."""
    if fn is not None:
        _PROCESSORS[name] = fn
        return fn

    def _deco(f: ProcessorFn) -> ProcessorFn:
        _PROCESSORS[name] = f
        return f

    return _deco


def get_processor(name: str) -> ProcessorFn:
    try:
        return _PROCESSORS[name]
    except KeyError:
        raise LookupError(f"No analysis processor registered under {name!r}; known: {sorted(_PROCESSORS)}")


def set_signal_loader(fn: SignalLoaderFn | None) -> None:
    """Register the loader that builds a segment's context window. One process-wide
    loader; passing ``None`` clears it (tests set a fake and reset)."""
    global _signal_loader
    _signal_loader = fn


def _load_window(run: AnalysisRun, segment: SegmentPlan) -> SignalWindow:
    if _signal_loader is None:
        raise RuntimeError(
            "No signal loader registered (compute.tasks.set_signal_loader). The "
            "concrete version-signal loader is a pending slice (materialisation)."
        )
    return _signal_loader(run, segment)


_TERMINAL_SEGMENT_STATES = frozenset(
    {
        AnalysisSegment.State.DONE,
        AnalysisSegment.State.SKIPPED,
        AnalysisSegment.State.FAILED,
    }
)


@shared_task
def launch_analysis_run(
    *,
    recording_id: int,
    detector: str,
    produces_kind: str,
    input_version_id: str,
    input_digest: str,
    image_digest: str,
    params: dict,
    grid_s: float,
    halo_s: float,
    max_event_span_s: float,
    locality: str,
    duration_s: float,
    segment_length_s: float,
) -> int:
    """Plan, persist, and fan out an analysis run. Returns the run pk.

    Idempotent: re-launching the same run key re-plans the same segments,
    re-creates any missing coverage rows, and re-dispatches — segments already
    committed are no-ops in :func:`~compute.runs.commit_segment`, so this is the
    resume path too.
    """
    recording = Recording.objects.get(pk=recording_id)

    run, _created = get_or_create_run(
        recording=recording,
        input_version_id=input_version_id,
        produces_kind=produces_kind,
        input_digest=input_digest,
        image_digest=image_digest,
        params=params,
        grid_s=grid_s,
        halo_s=halo_s,
        max_event_span_s=max_event_span_s,
        locality=locality,
    )

    # Splices are read from the recording, not taken from the caller, so no launch
    # path can forget them (compute/segmentation.py, "Splices").
    segments = plan_segments(
        duration_s=duration_s,
        segment_length_s=segment_length_s,
        halo_s=halo_s,
        locality=locality,
        splices=splices_for(recording),
    )

    # Pre-create every planned coverage row so the "all segments terminal" check
    # in _maybe_finalize_run knows the full expected set. ignore_conflicts makes
    # re-launch (resume) a no-op for existing rows.
    AnalysisSegment.objects.bulk_create(
        [
            AnalysisSegment(
                run=run,
                index=s.index,
                start_s=s.interior_start_s,
                end_s=s.interior_end_s,
            )
            for s in segments
        ],
        ignore_conflicts=True,
    )

    # Audit breadcrumb for the timeline (the run's own state is the operational
    # record; the durable PipelineRunAudit chain is a later slice).
    _record_launch(recording, run, produces_kind, len(segments))

    AnalysisRun.objects.filter(pk=run.pk, state=AnalysisRun.State.QUEUED).update(
        state=AnalysisRun.State.RUNNING, started_at=timezone.now()
    )

    # Fan out — one task per segment. Dispatched AFTER the audit scope so segment
    # work is not nested under the launch's Activity row.
    signatures = [
        run_analysis_segment.s(
            run_id=run.pk,
            detector=detector,
            index=s.index,
            interior_start_s=s.interior_start_s,
            interior_end_s=s.interior_end_s,
            context_start_s=s.context_start_s,
            context_end_s=s.context_end_s,
            run_index=s.run_index,
            context_starts_at_splice=s.context_starts_at_splice,
            context_ends_at_splice=s.context_ends_at_splice,
        )
        for s in segments
    ]
    if signatures:
        group(signatures).delay()

    return run.pk


@shared_task
def run_analysis_segment(
    *,
    run_id: int,
    detector: str,
    index: int,
    interior_start_s: float,
    interior_end_s: float,
    context_start_s: float,
    context_end_s: float,
    run_index: int = 0,
    context_starts_at_splice: bool = False,
    context_ends_at_splice: bool = False,
) -> int:
    """Load one segment's window, run the processor, and commit the findings it owns.

    The splice fields carry defaults so a message enqueued by an older deploy still
    deserialises; they describe the plan rather than drive it — the context bounds are
    already clamped at the splice by the time they are sent, so a worker that ignores
    them still reads only contiguous signal.
    """
    run = AnalysisRun.objects.get(pk=run_id)
    segment = SegmentPlan(
        index=index,
        interior_start_s=interior_start_s,
        interior_end_s=interior_end_s,
        context_start_s=context_start_s,
        context_end_s=context_end_s,
        run_index=run_index,
        context_starts_at_splice=context_starts_at_splice,
        context_ends_at_splice=context_ends_at_splice,
    )

    try:
        window = _load_window(run, segment)
        context = RunContext(produces_kind=run.produces_kind, params=run.params)
        output = get_processor(detector)(window, context)
        # Ownership-by-onset (§3.3): of everything the detector saw across the
        # halo-padded context, keep only the findings whose onset falls in THIS
        # segment's interior. Neighbours sharing the halo drop them, so a finding in
        # the overlap is emitted exactly once with no cross-task coordination.
        owned_events = [e for e in output.events if segment.owns(e.onset_s)]
        # A whole-window label (onset None) has no onset to own by; assign it to
        # segment 0 only, so a segmented run emits it once rather than per segment.
        owned_labels = [
            lab
            for lab in output.labels
            if (lab.onset_s is not None and segment.owns(lab.onset_s)) or (lab.onset_s is None and index == 0)
        ]
        commit_segment(
            run=run,
            index=index,
            start_s=interior_start_s,
            end_s=interior_end_s,
            events=owned_events,
            labels=owned_labels,
        )
    except Exception:
        logger.exception(
            "Analysis segment failed: run=%s index=%s detector=%s",
            run_id,
            index,
            detector,
        )
        AnalysisSegment.objects.filter(run_id=run_id, index=index).update(state=AnalysisSegment.State.FAILED)
        _maybe_finalize_run(run_id)
        raise

    _maybe_finalize_run(run_id)
    return index


def _maybe_finalize_run(run_id: int) -> None:
    """Flip the run to a terminal state once every planned segment is terminal.

    Idempotent and race-safe: the transition is a single conditional UPDATE gated
    on ``state=RUNNING``, so of many segments finishing concurrently exactly one
    wins and the rest match zero rows. The run is FAILED if any segment failed,
    else DONE. Conformance (earning ``reproducible``) is a separate, later pass.
    """
    seg_states = list(AnalysisSegment.objects.filter(run_id=run_id).values_list("state", flat=True))
    if not seg_states or not all(s in _TERMINAL_SEGMENT_STATES for s in seg_states):
        return

    new_state = AnalysisRun.State.FAILED if AnalysisSegment.State.FAILED in seg_states else AnalysisRun.State.DONE
    AnalysisRun.objects.filter(pk=run_id, state=AnalysisRun.State.RUNNING).update(
        state=new_state, finished_at=timezone.now()
    )


def _record_launch(recording, run, produces_kind: str, n_segments: int) -> None:
    """Best-effort timeline breadcrumb for a launched run. Never fatal."""
    try:
        from activity.models import Activity
        from activity.system_activity import with_system_activity

        with with_system_activity(
            "compute.analysis.run",
            interface=Activity.Interface.CELERY,
            target=recording,
            metadata={
                "run_id": run.pk,
                "produces_kind": produces_kind,
                "input_version_id": run.input_version_id,
                "n_segments": n_segments,
            },
        ):
            pass
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Could not record analysis-run launch: %s", exc)
