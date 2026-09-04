"""Analysis-run persistence service — the sanctioned write paths over the
pipeline models.

Two entry points:

* :func:`get_or_create_run` — idempotent creation of an :class:`~compute.models.AnalysisRun`
  by its content-addressed run key ``(input_digest, image_digest)``.
* :func:`commit_segment` — the contention-free, retry-safe commit of one segment's
  output (analysis-execution §2/§3): in a single transaction it persists the produced
  findings, links them to the run, and flips the segment's coverage row to DONE.

Findings arrive as the contract's :class:`~compute.contract.EventSpec` /
:class:`~compute.contract.LabelSpec` (the same shapes a processor returns). Both are
persisted as ``annotations.Event`` rows — events keep their event nature, labels are
stored as *typed* events (``event_class`` = the kind) because ``annotations.Label``
carries no time fields. **Interim** persistence choices, flagged for follow-up:

* **Channels ride in the Event's ``value`` JSON** (``value["channels"]``), not a
  first-class column — pending the storage decision (JSON array vs a normalised
  channel join). Channel granularity is captured end-to-end today; only its
  indexing is deferred.
* **Labels are typed events**, pending a decision on whether ``annotations.Label``
  gains time fields or per-region classifications stay as ``event_class``-typed
  events.
"""

from __future__ import annotations

import hashlib

from django.contrib.contenttypes.models import ContentType
from django.db import transaction

from annotations.models import Event
from epicurrents.system_user import get_system_user
from recordings.models import Recording

from .contract import EventSpec, LabelSpec  # noqa: F401
from .models import AnalysisRun, AnalysisSegment, RunAnnotation


def get_or_create_run(
    *,
    recording,
    input_version_id,
    produces_kind,
    input_digest,
    image_digest,
    params,
    grid_s,
    halo_s=0.0,
    max_event_span_s=0.0,
    locality,
) -> tuple[AnalysisRun, bool]:
    """Return the run for this run key, creating it if absent.

    Idempotent on the content-addressed key ``(input_digest, image_digest)`` — the
    model's unique constraint, with ``params`` already folded into ``input_digest``.
    A second call with the same key returns the existing run untouched.
    """
    return AnalysisRun.objects.get_or_create(
        input_digest=input_digest,
        image_digest=image_digest,
        defaults={
            "recording": recording,
            "input_version_id": input_version_id,
            "produces_kind": produces_kind,
            "params": params,
            "grid_s": grid_s,
            "halo_s": halo_s,
            "max_event_span_s": max_event_span_s,
            "locality": locality,
        },
    )


def _finding_object_hash(run, kind, name, onset_s, duration_s, channels) -> str:
    """Deterministic 32-char ``object_hash`` for a machine-produced finding.

    Scoped to the run (via ``input_digest``) and the finding's identity — kind,
    name, onset, duration, and the (sorted) channel set — but **not** the segment,
    so a finding landing in two segments' halo overlap hashes identically and dedups
    to one row across the overlap. Channels are in the identity so the same finding
    on different channels stays distinct.
    """
    payload = "|".join(
        [
            run.input_digest,
            kind,
            name,
            repr(onset_s),
            repr(duration_s),
            ",".join(sorted(channels or ())),
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32].upper()


def _persist_finding(
    run,
    index,
    content_type,
    author,
    *,
    kind,
    name,
    onset_s,
    duration_s,
    channels,
    confidence,
    extra,
    label_value=None,
):
    """Upsert one finding as a version-bound ``annotations.Event`` + its run link.

    ``get_or_create`` on the ``(target, version_id, object_hash)`` unique key makes
    concurrent segment tasks racing on the same halo finding safe (IntegrityError →
    re-query), and idempotent across a retried segment.
    """
    object_hash = _finding_object_hash(run, kind, name, onset_s, duration_s, channels)

    value = dict(extra or {})
    if channels:
        value["channels"] = list(channels)  # interim: channels in value JSON
    if confidence is not None:
        value["confidence"] = confidence
    if label_value is not None:
        value["value"] = label_value  # the label's class, when this is a LabelSpec

    event, _ = Event.objects.get_or_create(
        target_content_type=content_type,
        target_object_id=str(run.recording_id),
        version_id=run.input_version_id,
        object_hash=object_hash,
        defaults={
            "author": author,
            "event_class": kind,
            "name": name,
            "timestamp": onset_s,
            "duration": duration_s,
            "value": (value or None),
        },
    )
    RunAnnotation.objects.get_or_create(run=run, event=event, defaults={"segment_index": index})


@transaction.atomic
def commit_segment(
    *,
    run: AnalysisRun,
    index: int,
    start_s: float,
    end_s: float,
    events=(),
    labels=(),
    author=None,
    skip_reason: str = "",
) -> AnalysisSegment:
    """Atomically commit one segment's output. Idempotent per ``(run, index)``.

    The coverage row is the idempotency guard: the segment is locked FOR UPDATE and,
    if already DONE or SKIPPED, the call returns without rewriting. Otherwise, in one
    transaction it persists every :class:`EventSpec` and :class:`LabelSpec`
    (version-bound, authored by the system user unless *author* is given), links each
    to ``(run, index)``, and flips the segment to DONE — or to SKIPPED with
    *skip_reason* when there are no findings and a reason is supplied.

    A whole-window label (``onset_s is None``) is anchored at the segment's
    ``start_s``; that is meaningful for a monolithic (global-locality) run and is the
    caller's responsibility to keep sensible for a segmented one.
    """
    AnalysisSegment.objects.get_or_create(run=run, index=index, defaults={"start_s": start_s, "end_s": end_s})
    seg = AnalysisSegment.objects.select_for_update().get(run=run, index=index)
    if seg.state in (AnalysisSegment.State.DONE, AnalysisSegment.State.SKIPPED):
        return seg

    events = list(events)
    labels = list(labels)
    if not events and not labels and skip_reason:
        seg.state = AnalysisSegment.State.SKIPPED
        seg.skip_reason = skip_reason
        seg.save(update_fields=["state", "skip_reason", "updated_at"])
        return seg

    author = author or get_system_user()
    content_type = ContentType.objects.get_for_model(Recording)

    for e in events:
        _persist_finding(
            run,
            index,
            content_type,
            author,
            kind=e.kind,
            name=(e.label or e.kind),
            onset_s=e.onset_s,
            duration_s=e.duration_s,
            channels=e.channels,
            confidence=e.confidence,
            extra=e.extra,
        )
    for lab in labels:
        _persist_finding(
            run,
            index,
            content_type,
            author,
            kind=lab.kind,
            name=lab.value,
            onset_s=(lab.onset_s if lab.onset_s is not None else start_s),
            duration_s=lab.duration_s,
            channels=lab.channels,
            confidence=lab.confidence,
            extra=lab.extra,
            label_value=lab.value,
        )

    seg.state = AnalysisSegment.State.DONE
    seg.save(update_fields=["state", "updated_at"])
    return seg
