"""The analysis-processor contract — the fixed boundary every detector conforms to.

This is a *spec*, not an implementation: dataclasses describing what a processor
consumes and produces, plus the :class:`Processor` protocol it must satisfy. There
are deliberately **no concrete processors here** — IED detectors, SzCORE, and anything
future implement this contract elsewhere; the pipeline depends only on the shapes
below.

Design intent
-------------
* **The boundary is fixed and inward-facing.** The pipeline defines this contract;
  processors accommodate it, never the reverse — exactly as detectors accommodate
  SzCORE, not SzCORE the detector.
* **Modality-agnostic, not epilepsy-specific.** We handle time-series signal data,
  so the only safe universals are: input is *signal + metadata*, output is *events
  and/or labels*. SzCORE's schema is designed around seizure events specifically, so
  it is one *encoding* a processor maps onto this contract — never copied wholesale.
* **A fixed spine plus an open payload.** The contract pins only the fields the
  platform must index, store, and display — time, channels, kind, confidence — and
  hands everything detector-specific to an open ``extra`` payload it does not
  interpret. That is what lets one contract absorb detectors we can't yet enumerate.
* **Channel-level granularity is first-class.** Every event and label carries the
  set of channels it localises to (by *canonical* label), with the empty set meaning
  "the whole montage / global". This is a hard requirement, not an ``extra`` field.
* **Decoupling.** A processor is a pure ``(SignalWindow, RunContext) -> AnalysisOutput``.
  It receives one padded window and the run's structured context and returns findings
  in recording-relative time. Loading the signal, ownership filtering across segment
  seams, and persistence are the pipeline's job — the processor sees none of it. The
  same signature is satisfied by an in-process call and by a container that
  serialises the window to BIDS and parses TSV back; the transport lives inside the
  processor.
* **Structured context, not a loose dict.** Signal metadata, algorithm config, and
  subject metadata each get a named home so they cannot silently intermingle:
  intrinsic signal metadata rides on :class:`SignalWindow`, the detector's own tuning
  and any subject context ride on :class:`RunContext`. The one field that could carry
  PHI — a demographic — is an explicit, egress-gated field, not a dict key.

Pure Python — no Django, no numpy at import — so the contract stays a light,
dependency-free description both the in-process and containerised worlds share.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

#: Default convention: signal amplitudes are microvolts, and onsets/durations are
#: seconds. This is the unit a row is in when :attr:`SignalWindow.channel_units` does
#: not say otherwise — it is a *default*, never an assertion. A channel whose physical
#: dimension is not a voltage (a photic-trigger line, a DC input, an oximetry trace)
#: cannot be expressed in microvolts at all, and pretending otherwise turns a real
#: measurement into a meaningless number; those rows name their own unit per channel.
#: See ``recordings.processors.units`` for the vocabulary, including the generic
#: ``a.u.`` for a dimension the header never established.
SIGNAL_UNIT = "uV"


@dataclass(frozen=True)
class SignalWindow:
    """The signal handed to a processor: one segment's halo-padded context window.

    ``data`` is an array-like of shape ``(n_channels, n_samples)`` (rows are
    channels, in the order of ``channels``), each row in the unit ``channel_units``
    gives for it — microvolts (:data:`SIGNAL_UNIT`) for every electrophysiological
    row. The window is padded by the stage's halo, so a processor always has
    receptive-field context at the interior edges; it need not know where the interior
    is — it emits everything it finds and the pipeline keeps only what this segment
    owns.

    Onsets in the output are **recording-relative seconds**. ``t0_s`` is this
    window's absolute start in the recording, so a processor converts a local sample
    index ``i`` via ``onset_s = t0_s + i / fs``.

    ``channel_types`` and ``channel_units`` are the two per-channel facts no scalar
    can carry. A recording is not homogeneous: alongside the montage it holds trigger
    lines, DC inputs and oximetry, in their own units. The window states what each row
    *is* and what it is *in*, so a processor selects rather than assumes — the same
    reason the version-fetch contract made ``units`` per channel there.
    """

    data: Any  # array-like (n_channels, n_samples); each row's unit in channel_units
    channels: tuple[str, ...]  # canonical channel labels; order matches data rows
    fs: float  # sampling rate, Hz
    t0_s: float  # absolute start time of this window in the recording, seconds
    n_samples: int
    #: Optional per-channel modality (``eeg``/``eog``/``emg``/``ekg``/``trig``/
    #: ``misc``), parallel to ``channels`` — empty when the caller does not
    #: distinguish types. Lets a multimodal processor select the channels it
    #: understands without guessing.
    channel_types: tuple[str, ...] = ()
    #: Optional per-channel physical unit, parallel to ``channels``: canonical tokens
    #: from ``recordings.processors.units`` (``uV``, ``mV``, ``%``, ``a.u.``, …).
    #: Empty means every row is in :data:`SIGNAL_UNIT`. A processor that needs
    #: microvolts must **check** this rather than assume it — a row reading ``a.u.``
    #: is a real signal whose physical dimension the recording never declared, and no
    #: scale factor rescues it; the correct response is to skip the row.
    channel_units: tuple[str, ...] = ()


@dataclass(frozen=True)
class EventSpec:
    """A time-localised finding — a spike, a seizure, an artefact span.

    Maps to an ``annotations.Event``. ``kind`` is the controlled
    ``annotations.AnnotationKind`` token the run advertises; ``label`` is an optional
    display name. ``channels`` is the set of *canonical* channel labels the event
    localises to — empty means the whole montage (e.g. a generalised seizure).
    ``confidence`` is a normalised score in ``[0, 1]`` when the detector provides
    one; ``extra`` carries anything detector-specific the platform stores but does
    not interpret (peak amplitude, sub-scores, model internals).
    """

    kind: str
    onset_s: float
    duration_s: float | None = None  # None = instantaneous
    channels: tuple[str, ...] = ()  # () = whole montage / global
    confidence: float | None = None
    label: str = ""
    extra: dict = field(default_factory=dict)


@dataclass(frozen=True)
class LabelSpec:
    """A classification over a span — a sleep stage, a signal-quality verdict, a
    bad-channel flag.

    Maps to an ``annotations.Label``. Distinct from :class:`EventSpec` because a
    label is a *classification of a region* rather than a discrete occurrence:
    ``value`` is the class (``"N2"``, ``"artifact"``, ``"bad"``). ``onset_s``/
    ``duration_s`` are the span it applies to, both ``None`` meaning "the whole
    window / recording". ``channels`` scopes it to specific channels (e.g. a
    bad-channel label on ``("T7",)``); empty = all channels.
    """

    kind: str
    value: str
    onset_s: float | None = None
    duration_s: float | None = None
    channels: tuple[str, ...] = ()  # () = all channels
    confidence: float | None = None
    extra: dict = field(default_factory=dict)


@dataclass(frozen=True)
class AnalysisOutput:
    """What a processor returns over one window: events and/or labels.

    Either collection may be empty — a spike detector returns only events, a sleep
    stager only labels, a quality checker perhaps both. The pipeline routes events to
    ``annotations.Event`` and labels to ``annotations.Label``, applying ownership by
    onset so a finding seen in two neighbours' halo is committed exactly once.
    """

    events: tuple[EventSpec, ...] = ()
    labels: tuple[LabelSpec, ...] = ()


@dataclass(frozen=True)
class RunContext:
    """The run's configuration and subject context — everything a detector needs
    that is *not* the signal itself. Structured, never a loose dict.

    * ``produces_kind`` — the ``annotations.AnnotationKind`` token this run emits, so
      a configurable detector knows which kind to label its findings with.
    * ``params`` — the detector's *own* algorithm configuration (thresholds, model
      variant, …). Legitimately open — this is the detector's private config space —
      but it carries **only** tuning, never signal or subject metadata; those have
      their own homes (:class:`SignalWindow` / the fields below).
    * ``subject_age_years`` — **PII-gated**, and the template for any future
      demographic a detector needs (age priors, sex-specific norms). Populated ONLY
      when egress policy permits: for a trusted in-process processor always; for an
      untrusted container only if the grant allows demographic egress — the boundary
      the BIDS/SzCORE privacy design governs. Defaults to ``None`` and is *not*
      populated today (demographics are not yet a ``Recording`` field); the named,
      egress-decided home exists so adding them later is data plumbing, not a
      contract change.

    Egress summary: every :class:`SignalWindow` field and ``produces_kind``/``params``
    here are non-PII and safe to serialise to any transport. Demographic fields are
    the only ones gated, and they are named precisely so the gate is per-field and
    explicit rather than a dict the pipeline has to scrub.
    """

    produces_kind: str
    params: dict = field(default_factory=dict)
    subject_age_years: float | None = None


@runtime_checkable
class Processor(Protocol):
    """Signal in, findings out — the whole contract a detector adapter satisfies.

    A processor is a pure function of a window and the run's context. It must not
    touch the database, the run row, or the segment plan: those are the pipeline's,
    and keeping them out is what lets the same processor run in-process or inside a
    container. It reports findings in recording-relative time over the *whole*
    window; the pipeline discards the ones this segment does not own.
    """

    def __call__(self, window: SignalWindow, context: RunContext) -> AnalysisOutput: ...
