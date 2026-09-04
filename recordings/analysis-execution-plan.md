# Analysis-stage execution: parallel throughput over segments

Sibling to [`retention-and-lifecycle-plan.md`](retention-and-lifecycle-plan.md), which settles
*what* an analysis stage is (a DERIVE-phase job that emits annotations, ordered in a DAG,
requiring upstream dependencies by **annotation kind**) and how its outputs are retained. This
note settles *how an analysis run executes* when the analysis is naturally windowed — which
most detectors are (a spike detector slides 1 s windows, sleep staging is per-epoch, braindecode is
per-window).

The old application had an ONNX epoch-runner that sliced a recording and ran inference
window-by-window. This generalises that into a contract, and picks a primary optimisation
target: **parallel throughput**. Resumability and progressive (live) scoring are explicitly
*secondary* — most recordings are fully analysed before anyone opens them, so first-result
latency matters little, while total wall-clock across a backlog matters a lot. The pleasant
result is that resumability and progressive display fall out of the throughput design for
free; we do not build them separately.

## 1. The temporal-decomposition contract

An analysis stage declares four things, from which the dispatcher derives batching,
parallelism, and seam handling mechanically — no per-model special-casing:

- **grid** — the window stride the stage runs on, expressed on the recording's record grid
  (same whole-second discipline as `version-fetch-contract.md`).
- **halo** — how much signal on each side of a window the stage needs for context (a model's
  receptive field, a filter's edge). Segments overlap by the halo so a segmented run is
  byte-identical to a single run.
- **max_event_span** — the longest annotation the stage can emit. Drives seam handling.
- **locality class** — the discriminator that decides whether the run can be segmented at all:

| locality | needs | segmentable? | example |
|---|---|---|---|
| **window-independent** | each window alone (+ receptive-field halo) | yes — embarrassingly parallel | per-window spike CNN, amplitude threshold |
| **local-context** | ±N s around each window | yes — overlap by halo, reconcile seams | context-window detector |
| **global / holistic** | the whole recording at once | **no** — runs as one task | whole-night Viterbi staging, recording-wide normalisation, ICA |

Only the first two are segmented. A global stage declares itself so and runs monolithically,
all-or-nothing like a reconstruction stage; the dispatcher does not try to slice it. This same
locality field also governs DAG streaming (retention note §1): a downstream stage with a
*local* dependency may consume upstream segments as they commit; a *global* dependency waits
for full upstream coverage.

## 2. Throughput-first execution model

A segmentable run **fans out into independent segment tasks** (a Celery group), optionally
followed by a finalisation callback (a chord) only when the stage needs seam reconciliation.
Four consequences follow from optimising fan-out for throughput specifically:

- **Segment size is a throughput tuning knob, not a semantic one.** Too fine and per-segment
  fixed cost dominates (model reload, halo recompute — the halo is recomputed in both
  neighbours, so overlap waste ≈ `2·halo / segment_length`); too coarse and stragglers leave
  workers idle. Pick `segment_length ≫ halo` and large enough to amortise fixed cost, small
  enough to fill the worker pool. Because it is a tuning knob, it is **excluded from run
  identity** — see §3.
- **Persistent worker/sidecar pool, model loaded once, pulling segments.** Throughput wants
  warm-up amortised across many segments, so the external-tool sidecar (the transport the
  `compute/` session chose) is a long-lived pool that pulls work, not a per-batch cold
  invocation. A segment is one unit of work handed to a warm worker.
- **Contention-free commit path.** With many segments committing concurrently, the write path
  must not serialise. Annotations are per-segment append-only rows keyed `(run_id, kind,
  segment)`; **coverage is stored as per-segment rows too**, unioned on read — never a single
  mutable coverage row every segment locks to update. This is the schema decision throughput
  forces on the deferred ledger.
- **Seam reconciliation, when needed, is the chord callback.** A window-independent stage with
  halo dedupe needs none. A local-context stage commits each segment's interior as final and
  holds a boundary margin (width `max_event_span`) provisional until the neighbour lands, then
  the finalisation pass resolves it.

## 3. Determinism under parallelism (the correctness spine)

Parallel fan-out must yield the *same annotations* as a single run, regardless of how many
segments there were or what order they finished. Three rules guarantee it:

1. **Segment size and worker count are excluded from run identity.** The run key stays
   `(input_digest, image_digest, params)`; `grid`, `halo`, and `params` are in (they change
   output), segmentation is out. A 1-segment and a 50-segment run of a reproducible analysis
   are byte-identical — the same reproducible/`version_id` principle as the manifest.
2. **The merge of segment outputs is commutative and idempotent.** Completion order must not
   affect the result, and a retried segment (Celery autoretry) must not double-insert — hence
   the `(run_id, kind, segment)` upsert key.
3. **Every emitted event has exactly one owning segment, by a deterministic rule** (e.g. the
   segment whose interior contains the event's onset). Overlapping halos then cannot
   double-count an event that two neighbours both saw, without any cross-task coordination.

## 4. Why the secondary benefits come for free

- **Resumability** — a run that dies partway just re-fans-out its incomplete segments;
  idempotent per-segment commits (§3.2) make re-running finished ones harmless. No frontier
  checkpointing engineered; it is a byproduct of independent idempotent segments.
- **Progressive display** — the per-segment coverage rows already answer "what is analysed so
  far," so a viewer *could* poll a coverage cursor and stream partials. We keep the coverage
  record (it is load-bearing for correctness anyway — it distinguishes "analysed, nothing
  found" from "not yet reached"), but we do not build viewer streaming now; the viewer reads
  completed annotations + coverage at open time.

## 5. Conformance and benchmarking: the resegmentation oracle

The determinism spine of §3 is not only a correctness requirement — it is a **metamorphic
oracle**, and one of the few label-free tests available for an ML analysis stage. You usually
cannot assert "these detections are correct" without ground truth, but you *can* assert "these
detections are invariant to how the recording was sliced," and that needs no labels. The
degenerate case (monolithic vs segmented) plus randomized resegmentation is a strong automatic
correctness check the platform gets for free from a property it already relies on.

What makes it a benchmarking *endpoint* rather than a unit test is *what* it validates: the
declared contract of §1, which the dispatcher otherwise trusts blindly. A single equality
assertion catches three failure classes:

- **Undeclared statefulness** — the sharpest. A stage that claims `window-independent` but
  secretly carries state across windows (running normalisation, adaptive threshold, RNN hidden
  state) produces *different* output when segment boundaries move, because the state resets in
  different places. Resegmentation is the perturbation that surfaces exactly this.
- **Mis-declared halo** — if the true receptive field exceeds the declared `halo`, seams leak
  edge artefacts and the two segmentations diverge near boundaries, naming the bug's time
  range.
- **Nondeterminism behind `reproducible=True`** — GPU non-associativity, unseeded dropout;
  resegmentation plus a plain rerun surfaces it.

So the oracle turns `halo`, `locality`, and `reproducible` from *self-declarations we trust*
into *checked properties*. This closes the retention loop of the retention note: a stage does
not merely *claim* `reproducible=True` to earn evictable-cache storage — it **earns** it by
passing resegmentation-invariance across a corpus. Fail, and it must declare
`reproducible=False`, and its outputs are retained rather than cached. The conformance harness
is the gate that decides storage lifecycle.

### 5.1 Two benchmarking layers

- **Conformance** (this — label-free, metamorphic): does the *implementation* honour its
  declared contract? Segmentation invariance, idempotence, determinism.
- **Accuracy** (label-based): how *good* are the detections against a reference? This is what
  the `compute/` session's SzCORE/BIDS integration is — detector benchmarking against reference
  annotations. It is enabled by the annotation-kind vocabulary: because detectors are swappable
  by kind, two spike detectors can run head-to-head, or against a human-scored `spike_events`
  reference.

Conformance sits *beneath* accuracy — there is no point scoring a detector's F1 if its
implementation is not self-consistent under segmentation.

### 5.2 Two oracle modes

Strict byte-equality only holds for a reproducible stage emitting *discrete* detections:

- **Strict** (reproducible + discrete): exact equality on the canonical annotation form.
  Continuous probability traces are **quantised before storage** so that "byte-identical" is a
  real, testable claim rather than one defeated by a last-ULP reduction-order difference.
- **Tolerant** (non-reproducible, or continuous outputs held to a bound): statistical
  agreement — event-set IoU ≥ threshold, trace within ε. Weaker, but still catches gross
  halo/seam bugs. A genuinely stochastic stage can only ever be held to this mode.

Global/holistic stages are exempt from *segmentation* invariance (they do not segment) but are
still subject to the determinism and idempotence checks.

### 5.3 Harness shape

Run monolithic plus a few **randomized-offset** segmentations — the offset matters, so seams
land at different absolute times — plus one adversarial run whose boundaries **bisect known
events** to stress `max_event_span` and the provisional-margin reconciliation. Assert equality
on the canonical annotation form using the same ownership/merge rules as §3, and run one
identical repeat for pure determinism. The first divergence range points straight at the
offending halo/seam/state bug. The same resegmentation machinery doubles as the *performance*
sweep: because output is invariant to segment size, segment size can be tuned purely for
throughput (§2) with the equality oracle guaranteeing correctness is unaffected.

## 6. Consequences for the deferred ledger

When the `RecordingJob` / run ledger is built (still deferred), it must:

- model an analysis run's progress as **per-segment coverage rows** per `(run, kind)`, unioned
  on read — not a single mutable coverage field;
- store annotations as append-only rows keyed `(run_id, kind, segment)`, upsertable for retry
  idempotency;
- carry the stage's declared `grid` / `halo` / `max_event_span` / locality so a resumed or
  re-fanned-out run reconstructs the identical segmentation;
- keep segmentation parameters **out** of the run identity hash;
- record a stage's **conformance verdict** (strict/tolerant, pass/fail, corpus) as the
  evidence backing its `reproducible` flag — so the storage-lifecycle decision (retention note)
  traces to a checked property, not a bare declaration.

No change is required to the built cores (`stages.py`, `registry.py`, `manifest.py`,
`params.py`); this is analysis-side execution, above the reconstruction pipeline.
