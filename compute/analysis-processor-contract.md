# Analysis-processor contract

**Status: spec drafted (`compute/contract.py`), no concrete processor yet.**
Suggested home alongside the other compute design notes.

The execution layer (`compute/tasks.py`) fans a run out into per-segment tasks and
each task calls a **processor**. This note pins what a processor *is* — the fixed
boundary every detector conforms to — and why it is shaped the way it is. The spec
itself is code: `compute/contract.py`. There is deliberately no `Detector`
implementation here; IED detectors, SzCORE, and anything future implement the contract
elsewhere, and the pipeline depends only on its shapes.

## Two boundaries, one of which is fixed

There are two contracts in play, and keeping them apart is the whole design:

1. **pipeline ↔ processor** — internal, fixed, inward-facing. The pipeline defines
   it; every processor conforms to it, never the reverse. This is
   `(SignalWindow, params) -> AnalysisOutput`.
2. **processor ↔ detector** — where in-process and containerised detectors differ.
   In-process, the processor calls a Python core directly. For a container, the
   processor serialises the window to the detector's wire format (BIDS for SzCORE),
   runs it, and parses the result (TSV) back. This lives *inside* the processor.

The pipeline only ever sees boundary 1. That is the analog of "detectors conform to
SzCORE": here, processors conform to `AnalysisOutput`, and a SzCORE processor is the
adapter where SzCORE's schema meets ours.

## Why not adopt SzCORE wholesale

SzCORE is a maintained, valuable standard — but it is designed around **seizure
events specifically** (its event types, its whole-recording BIDS framing). We run
arbitrary time-series analyses: spikes, sleep staging, artefact detection, qEEG,
things we can't yet enumerate. Copying SzCORE and calling it done would bind the
platform's internal contract to one clinical question.

So the internal contract keeps only what is safe to assume for *any* time-series
detector, and treats SzCORE as one encoding a processor maps onto it:

- **Input** is *signal + metadata* — samples, canonical channel labels, sampling
  rate, a window offset, optional per-channel modality.
- **Output** is *events and/or labels* — either may be empty.
- **A fixed spine plus an open payload.** The contract pins only the fields the
  platform must index, store, and display: time (`onset_s`/`duration_s`), `channels`,
  `kind`, `confidence`. Everything detector-specific goes in an open `extra` dict the
  platform stores but never interprets. That fixed-spine/open-payload split is what
  lets one contract absorb detectors we haven't met — the spine is small enough to be
  universal, the payload flexible enough to carry the rest.

## Events vs labels

Both are findings, but they are semantically different and map to different
annotation models:

- **`EventSpec`** — a *discrete, time-localised occurrence* (a spike, a seizure, an
  artefact span) → `annotations.Event`.
- **`LabelSpec`** — a *classification of a region* (a sleep stage over an epoch, a
  signal-quality verdict, a bad-channel flag) → `annotations.Label`.

A processor returns an `AnalysisOutput` carrying both collections; a spike detector
fills only `events`, a sleep stager only `labels`, a quality checker perhaps both.

## Channel-level granularity is first-class

Every `EventSpec` and `LabelSpec` carries `channels: tuple[str, ...]` — the set of
**canonical** channel labels the finding localises to, with the empty tuple meaning
"the whole montage / global". This is a hard part of the spine, not an `extra`
field: a spike localises to `("F7", "T7")`, a generalised seizure to `()`, a
bad-channel label to `("T7",)`. Referencing channels by their *canonical* label (the
normalisation built earlier this session) keeps the reference stable across montages
and portable across instances.

## Decoupling: the processor is a pure function

A processor is `(SignalWindow, RunContext) -> AnalysisOutput`. It must not touch the
database, the `AnalysisRun` row, or the `SegmentPlan`:

- It receives one **halo-padded context window** and the run's **structured
  context**, and returns findings in **recording-relative** time over the whole
  window.
- It does **not** know where its interior is or that segmentation exists — it emits
  everything it detects; the pipeline discards what this segment does not own
  (ownership-by-onset, analysis-execution §3.3).
- Loading the signal, ownership filtering across seams, and persistence are the
  pipeline's job.

Keeping runs/segments/DB out of the processor is exactly what lets the same callable
run in-process *or* inside a container — the transport is a private matter of the
processor, invisible above.

## Context is structured, and metadata egress is per-field

The second argument is a `RunContext`, not a loose dict, so the three kinds of
information a detector might need each have a named home and cannot silently
intermingle:

- **Intrinsic signal metadata** — channels, `fs`, `t0_s`, `n_samples`,
  `channel_types`, `channel_units` — rides on `SignalWindow`. All of it is non-PII and
  safe to serialise to any transport (`t0_s` is a **data position** in
  recording-relative seconds — never a wall-clock date, and in a discontinuous
  recording not a wall-clock offset either; see `continuity-and-timelines.md`).
  `channel_units` is per-channel and must be **checked**, not assumed: a row reading
  `a.u.` is a real signal whose physical dimension the recording never declared, and a
  processor needing microvolts has to skip it rather than scale it.
- **Algorithm configuration** — thresholds, model variant — is `RunContext.params`,
  a dict that is legitimately open because it is the *detector's own* config space,
  but carries only tuning.
- **Subject metadata** — `RunContext.subject_age_years` and any future demographic —
  is a **named, PII-gated field**. It is populated only when egress policy permits:
  always for a trusted in-process processor, and for an untrusted container only if
  the grant allows demographic egress (the BIDS/SzCORE privacy boundary). It defaults
  to `None` and is not populated today (demographics are not yet a `Recording`
  field), but the gated home exists so adding them later is data plumbing, not a
  contract change.

This is what resolves the "`params` grab-bag" risk: signal metadata is named on the
window, subject metadata is named and gated on the context, and `params` is left as
the one genuinely open space — the detector's own.

## Status of the wiring

The contract is now wired through the write and execution layers (this slice):

- **Registry rewired.** `compute/tasks.py` registers
  `(SignalWindow, RunContext) -> AnalysisOutput`, and `run_analysis_segment` builds
  the context, loads the window via an injectable **signal-loader seam**
  (`set_signal_loader`), ownership-filters events and labels, and commits.
- **`commit_segment` extended** to persist both events and labels, with channels.
- **`EventSpec` converged** onto the contract's (`onset_s`/`kind`/`channels`); the
  placeholder in `runs.py` is gone.
- **Null-onset label ownership** resolved: a whole-window label is assigned to
  segment 0 only, so a segmented run emits it once.
- **Concrete signal loader landed** (`compute/signal_loader.py`): it builds the
  `SignalWindow` for `[context_start, context_end]` at the run's `input_version_id`,
  reading the source EDF lazily. It refuses rather than guesses — a derived
  `input_version_id` (awaits `ArtifactCacheEntry`), mixed sampling rates (awaits the
  resampling RECONSTRUCT output), and any reader/header disagreement on channel count
  or rate all raise. Per-channel unit and type resolution happen here, from the
  **header's** own fields rather than from anything MNE reports back. Tests still
  inject a fake loader through `set_signal_loader`.

- **Segmentation is splice-aware.** `launch_analysis_run` reads the recording's
  `Interruption` rows and cuts the plan at every gap, so the context window a processor
  receives is always contiguous signal — it never has to detect a discontinuity it
  cannot see in the samples (`recordings/continuity-and-timelines.md`). This is
  invisible above the contract by design; the one thing a processor might want and
  cannot yet have is the knowledge that its context was *cut short by a splice* rather
  than by the recording's end. `SegmentPlan` carries that
  (`context_starts_at_splice` / `context_ends_at_splice`); `SignalWindow` does not, and
  giving it a field is a contract change deliberately deferred until a real detector
  asks for it.

Two things are deliberately **interim / still open**:

1. **Channels + labels persistence shape.** Channels currently ride in the Event's
   `value` JSON, and labels are stored as `event_class`-typed `annotations.Event`
   rows (because `annotations.Label` has no time fields). Both are functionally
   complete but await a decision: a first-class `channels` column (JSON array vs a
   normalised join — a join only if channel-scoped queries need indexing), and
   whether per-region labels warrant time fields on `annotations.Label` or stay as
   typed events.

2. **Whether the window advertises a splice-shortened context.** Correctness does not
   depend on it — the bounds are clamped before the loader sees them — but a detector
   with a long receptive field may legitimately want to know its left context is 2 s
   rather than the 30 s the stage asked for, and to distinguish that from the same
   shortfall at the recording's start. The fact exists on `SegmentPlan`; whether it
   belongs on `SignalWindow` is a question the first real detector answers.

None of these are needed to *state* the contract — they are what it costs to run a
real detector against it, and they're the natural content of the next slice.
