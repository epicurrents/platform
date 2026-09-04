# Signal-modifying pipeline — implementation plan

Extends `recording-lifecycle-design.md`. That document draws the state boundary at file
mutation and sketches a job ledger; this one specifies how an **ordered, extensible set of
signal-modifying stages** is registered, sequenced, tracked, and eventually controlled from
the UI. It is written against the code as it stands (EOG correction shipped, WQN repair
WIP), and for the expectation that several more modifiers will follow.

The plan is deliberately staged so that each part is useful on its own and nothing
downstream is blocked on the UI. The hard problem is not the UI — it is §3, canonical
input, which the current single-stage design quietly avoids and a second stage breaks.

## 1. The organising idea: phases contain stages

Adopt the two-level model the question proposes.

- A **phase** is a coarse class of processing whose position relative to other phases is
  **fixed**. Phases are the pipeline's skeleton.
- A **stage** is one concrete modifier. Stages live in a phase, and their order **within**
  a phase is configurable where the science permits and constrained where it does not.

Proposed phases, in fixed order:

| phase | mutates the file? | examples | boundary |
|---|---|---|---|
| `INGEST` | writes it | format parse, vendor-format conversion | — |
| `RECONSTRUCT` | yes | EOG correction, WQN repair, future denoisers | **file becomes AVAILABLE at the end of this phase** |
| `DERIVE` | no | impedance extraction, qEEG indices, coherence | — |

This is exactly the `PROCESSING → AVAILABLE → READY` progression from the lifecycle doc,
now given a name for the middle band. `RECONSTRUCT` is the *only* phase that rewrites
signal, so it is the only one that gates openability — which is precisely the design doc's
core principle ("draw the boundary at file mutation, not at all-work-done").

Why two levels rather than one flat ordered list:

- **Phase order encodes hard invariants** that must never be user-editable. Derived indices
  must be computed on reconstructed signal, so `DERIVE` after `RECONSTRUCT` is not a
  preference. Making it a phase boundary means no UI, config, or future contributor can
  invert it.
- **Within-phase order is where the real choices live**, and they are not free either — see
  §4. Keeping them inside a phase scopes the reordering problem to a handful of stages that
  share an input contract, instead of a global list where a rater could drop the indices
  computation before the correction that feeds it.

## 2. The stage contract

Every reconstruction stage already has the same shape, discovered rather than imposed: EOG
correction and WQN repair are both a pure numeric core plus a thin EDF read/write wrapper.
Formalise that.

```python
class ReconstructionStage(Protocol):
    name: str  # stable id, e.g. "eog_regression"
    phase: Phase  # RECONSTRUCT
    order_hint: int  # default position within the phase
    requires: tuple[str, ...]  # stage names that must run before this one
    enabled_by_default: bool
    code_version: str  # algorithm identity; enters the manifest
    reproducible: bool  # can output be rebuilt bit-exact? (see below)

    def transform(self, header: bytes, signals: bytes) -> tuple[bytes, bytes]:
        """Idempotent given the same input. Must be a no-op on second application."""
```

**External-tool stages.** Not every modifier is Python — some marshal the signal to an
external tool (a Docker-hosted ML model, a remote service) and wait for the reply. From the
pipeline's view that is still `transform`: input bytes to output bytes, just I/O-bound, the
call living inside the stage's own `transform`. The **transport is deliberately deferred**
and unspecified here; a blocking round trip fits the contract as-is, and the dispatcher runs
stages in Celery, which can block on I/O. See
[`docs/engineering-notes/szcore-bids-integration.md`](../docs/engineering-notes/szcore-bids-integration.md)
for a worked instance of this deferred-transport slot — a SzCORE/BIDS detector fed by
materialise-to-tempdir vs a FUSE-backed view — and for why a detector is an *analysis* stage
(annotations out) rather than a `transform` stage (signal bytes out).

What is *not* deferrable is `reproducible`, because it decides the storage lifecycle of the
version a stage produces, and it is **orthogonal to external-ness**: a pinned-digest
deterministic container is reproducible; a pure-Python RNG stage is not.

- `reproducible = True` (default) — output is **evictable cache**, rebuildable from source +
  manifest (§3.5), because a rebuild yields the same bytes.
- `reproducible = False` — output is **retained, not evicted**. A rebuild is not guaranteed
  to reproduce it (GPU nondeterminism, sampling, a container that changed under a fixed tag),
  so the bytes a rater scored must be archived rather than regenerated. Such a version breaks
  the "cache is a pure function of source + manifest" property and is handled as archive, not
  cache. For these, `code_version` must pin the tool's identity as tightly as possible — the
  image *digest*, never a mutable `latest`.

This is the one part of external-tool support that must exist in the contract before any
stage relies on it; treating a non-reproducible external output as evictable cache would let
a rebuild silently return different bytes than were scored — the "manifest is a lie" failure
the model exists to prevent.

The registry mirrors `activity/derived_state.register_derived_state_digester` exactly —
that pattern is already in the tree, tested, idempotent, and hooked at `AppConfig.ready`.
Do not invent a new mechanism.

```python
# recordings/pipeline/registry.py
def register_reconstruction_stage(stage: ReconstructionStage) -> None: ...
def reconstruction_stages() -> list[ReconstructionStage]: ...  # resolved order
```

An EOG-regression middleware satisfies `transform` as written. A WQN stage gets a
`transform` wrapper around the repair routine. Neither ships in core — both are
project-supplied, and this plan describes the shape they plug into. Adding a future modifier becomes: implement the
protocol, call `register_reconstruction_stage` in the app's `ready()`. No signal edit, no
dispatch edit. That is the "add a stage" requirement, met.

**This also fixes the config trap.** Today `Config.artifacts.eog_correction` is read only
inside the derived pipeline and does not gate the stage that actually rewrites the file
(`tasks.py:84` instantiates the middleware directly). With a registry, enablement is a
property of the stage record and is read at dispatch — there is one place enablement lives,
and it governs the thing it names.

## 3. Canonical input, versioned: source + manifest + cache

This is the part the current design does not handle, and it is load-bearing for everything
below. It must be settled before WQN ships as a second modifier.

### 3.1 Why `_orig` cannot be the storage model

`_orig` makes **one file carry its own history**, and history inside the artifact cannot
branch, be addressed, or survive a second stage. Concretely, EOG correction writes `{ch}`
(corrected) and `{ch}_orig` (untouched), recomputing from `_orig` for idempotency. That is
a one-stage trick. With two stages rewriting the same channels:

- WQN's `_orig` and EOG's `_orig` refer to different baselines and collide on the label.
- Re-running EOG alone must preserve WQN's later work, or redo it — but WQN ran *on EOG's
  output*, so "preserve" is incoherent and "redo" means the toggle secretly re-runs the
  whole phase.
- Disabling EOG must yield a file as if only WQN had run, which requires WQN to be
  re-derivable from the true original, not from EOG's output.

None of this is expressible with a single suffix. EDF makes it worse: 16-character labels
and a bounded channel count mean you cannot encode N versions as channels even if you tried.

**The reframe: `_orig` is an interchange format, not the storage model.** It is genuinely
good at one thing — handing a recipient a single self-describing EDF with corrected and
original travelling together. Keep it for *export*. Stop storing the truth in it.

### 3.2 The model

Split the artifact from its history.

- **Source** — the ingested EDF. Immutable, content-hashed, never rewritten by any stage.
- **Manifest** — an ordered list of stage applications, one per recording state:
  `(stage_name, code_version, params_hash, input_hash → output_hash)`. Small, text-like,
  itself content-addressable.
- **Served signal** — source replayed through the manifest, materialised as a *derived
  artifact* and cached, keyed by the manifest hash.

Versioning is not a feature added on top; it falls out. Change EOG's `_QUIET_KEEP` → new
`params_hash` → new manifest → a new addressable version, with the old manifest still valid
and rebuildable. The EOG-only and EOG+WQN variants we juggle by hand today (`dirty_iv5.edf`,
`dirty_iv5_wqn.edf`) become two manifests over one source. Dozens of versions cost dozens of
tiny recipes; the expensive bytes are cache, reference-counted against live manifests.

What this buys that layered-`_orig` cannot:

- **Composition.** N stages, no label collisions, no "whose `_orig` is this".
- **Precise invalidation.** A stage's `output_hash` is a function of `input_hash`,
  `params_hash`, and `code_version`. Change any one and everything downstream is *known*
  stale by hash comparison — you do not recompute to discover staleness. This is the
  `input_digest` field the lifecycle doc's ledger already reaches for, generalised.
- **Citeable provenance.** Which version did a rater score? The manifest they viewed. A
  published index result cites a manifest hash and anyone can rebuild that exact signal.

The viewer's overlay concern is solved better than by baked-in `_orig`: it overlays "signal
at manifest-state N" against "state M" by rebuilding both from source. This also aligns with
the Pyodide/Float32Array direction — the browser already caches decoded source, so "rebuild
version N" can happen client-side from source plus a small manifest, no whole-EDF transfer.

### 3.3 The determinism contract

Replay only works if stages are deterministic, and one field is what keeps it honest.
`params_hash` is not enough: if a stage's *algorithm* changes, identical params produce a
different output and reproducibility breaks **silently** — the exact class of bug this
project has spent its length killing. So:

- Every stage carries a `code_version` the manifest records. A stage code change is a new
  version, and downstream invalidates by hash.
- Stages must be deterministic — no RNG, no wall-clock, no dependence on ambient state. Our
  two satisfy this (verified: `_orig` round-trips bit-exact). It must be a **stated contract**
  a stage promises, not an accident, because the first stage that seeds a random pool breaks
  addressability for the whole pipeline.

### 3.4 Where the manifest lives, and why not the bytes

The manifests want a permanent, signed, append-only home; the derived bytes emphatically do
not belong there.

**Manifests → the existing GPG audit chain (`ObjectChangeLog`). Decided.** The property
wanted — permanent, signed, per-recording, tamper-evident — is already implemented in the
tree, and a manifest hash is a natural thing for that chain to sign. It is the home rather
than literal git for one specific reason: git gives permanence but *fights* GDPR erasure
(history is immutable across all clones), whereas the audit chain already has erasure
carve-outs (`register_subject_pii` in the project's `apps.py`) so a manifest can be permanent *and*
have its patient-identifying surroundings scrubbed on an Art. 17 request. Git would
reintroduce the permanence-vs-erasure conflict the audit chain was built to resolve.

The audit chain is **authoritative**. A manifest may *also* appear in project git history as
a convenience (a committed pipeline definition, a reproducibility artifact), but that copy is
a mirror, not the source of truth: the guarantee is that any manifest is retrievable from the
audit chain, and erasure operates against the audit chain. Nothing keys on the git copy.

**Derived bytes → never in versioned history.** Git snapshots binaries whole; high-entropy
signal has no useful delta, so every version is a full permanent copy that bloats the repo
without bound and cannot be pruned without history rewriting. And it would put patient signal
into unerasable history, directly undermining the erasure machinery. The bytes live in the
storage tier of §3.5, which supports real deletion.

Because the recipe is durable, the derived cache stops being "don't lose a version" and
becomes "cap disk". Any cached artifact can be evicted freely — the manifest to rebuild it is
safe. That dissolves the hard part of the GC problem: what remains is pure size-bounded
eviction (LRU, rebuild on miss) with no correctness stakes.

### 3.5 Three storage classes, distinguished by invariant

The deployment binds `recordings-data`, `staging-data`, `media-data` under one backup regime
today, which merges two data classes that should be separate. There are three, and they
differ on backup-inclusion, mutability, and retention:

| class | irreplaceable | backed up | mutable | erasable |
|---|---|---|---|---|
| **Original** (optional bind) | yes | yes | never | yes |
| **Source-of-record** (deidentified EDF + manifests) | yes | yes | never | yes |
| **Artifact cache** (NEW volume) | **no** | **no** | freely | yes |

- **borgmatic must EXCLUDE the artifact volume.** Backing up rebuildable bytes pays twice —
  storage and restore time — for data that is a pure function of source + manifest. The
  backup surface collapses to the irreducible data.
- **This is a disaster-recovery feature, not just a saving.** After a restore the cache is
  empty and rewarms lazily on first view, so RTO is bounded by restoring the *small* things
  (deidentified EDFs, tiny manifests), not the derived corpus. The cache can even sit on
  local SSD scratch rather than a network volume — losing it is a non-event.
- **The trap: "disposable" is true for backup and cost, false for access control and
  erasure.** Deidentified EEG is pseudonymised, not anonymised — still personal data under
  GDPR. The artifact cache is a third *storage* location, **not** a third *sensitivity*
  domain. It needs source-level access restriction, and a subject-erasure must *purge* its
  entries, not leave them to evict naturally. The manifest is the bridge: cache entries are
  keyed by `(recording, manifest_hash)`, so erasure is "drop source, scrub the manifest's PII
  surroundings, purge the cache entries." Missing that last clause is a compliance hole hidden
  inside a volume everyone thinks of as throwaway.

### 3.6 Rejected alternative: layered `_orig`

Recording what each stage consumed in channel metadata, with `{ch}_orig` always meaning the
ingested original and intermediate states replayed on demand. Rejected: re-deriving an
intermediate state replays anyway, so it carries the manifest model's compute cost without
its conceptual simplicity, keeps the EDF-label collision problem, and needs fiddly per-channel
provenance bookkeeping — precisely the kind of subtle-bug surface this project has spent its
length eliminating.

## 4. Within-phase ordering is constrained, not free

The UI must not present reconstruction stages as freely reorderable, because some orderings
are wrong rather than merely different — established empirically, not assumed:

- **WQN must run after EOG correction.** Its measured benefit (+2853 net cells, near-zero
  index bias) was on EOG-corrected input. On raw input it would be repairing signal
  dominated by eye artefact — the job EOG does better.
- **WQN needs the EOG gate off**, which is a property of *how EOG configures the shared
  rejection*, not of WQN itself — a cross-stage coupling, recorded with the
  project that implements those stages rather than here.

So the `requires` field is not decoration. The registry resolves a topological order from
`requires`; `order_hint` breaks ties only among stages with no dependency between them. The
reorder UI (§7) may only permute stages that are mutually unordered — it surfaces the
partial order and refuses edits that violate it, rather than offering a free list that lets
a rater produce a subtly worse recording with no error.

Cross-stage configuration coupling (the gate) is the sharper edge and is called out as an
open question in §11 — it is not captured by `requires` alone.

## 5. The job ledger (design §4, made concrete)

One table in `recordings`, rows created at ingest by enumerating the registered stages for
the active project.

```
RecordingJob
  recording   FK
  stage_name  str          # matches the registry
  phase       str          # RECONSTRUCT | DERIVE
  order       int          # resolved position, for display and replay
  enabled     bool         # per-recording override of the stage default
  state       str          # QUEUED | RUNNING | DONE | FAILED | SKIPPED
  started_at, finished_at, error
  input_digest  str        # hash of what this stage consumed — see below
```

`input_digest` here is the `input_hash` of §3.2, surfaced on the run row: it lets the
rebuilder decide whether a cached stage output is still valid or must be recomputed, and it
proves which bytes each stage saw.

**Ledger and manifest are not the same object, and the distinction matters.** The
`RecordingJob` row is *live run state* — QUEUED/RUNNING/DONE, timestamps, the error from
this attempt — and belongs in the operational DB. The manifest (§3.2) is the *durable,
signed recipe* and lives in the audit chain (§3.4). A run writes a ledger row as it
executes and, on success, appends/updates the manifest. The ledger can be rebuilt from
scratch on redeploy; the manifest cannot, and must not be, lost.

State derives from ledger rows exactly as the design doc specifies (any `RECONSTRUCT` row
unsettled → `PROCESSING`; all reconstruct settled, derive outstanding → `AVAILABLE`; all
settled → `READY`). `SKIPPED` is new and necessary: a disabled stage must appear in the
ledger so the badge shows `3/4 done, 1 skipped` rather than silently vanishing.

## 6. Dispatch

Replace the bare `.delay()` calls in the project's `apps.py` with a phase-aware dispatcher in
`recordings`:

1. On ingest completion, create ledger rows from the registry.
2. Dispatch the `RECONSTRUCT` phase as a **Celery chain** (design §5 — serialise the
   modifying phase; two stages rewriting the same file in parallel is last-write-wins). The
   chain reads `enabled`, skips disabled stages (marking them `SKIPPED`), and rebuilds the
   derived artifact from source per §3.2, writing the manifest as it goes.
3. On the chain's success, transition to `AVAILABLE` and fan out the `DERIVE` phase, which
   *can* be parallel since those jobs only read.

The infinite-loop guard that EOG currently needs (disconnecting its own `post_save`) is
deleted: dispatch is no longer triggered by the file save, it is triggered once at ingest
and re-triggered explicitly on a toggle/reorder/rerun request.

## 7. API and frontend

**API** — the ledger makes this a straight read:

- `GET /recordings/{hash}/jobs` → ordered rows with `stage_name, phase, order, enabled,
  state, error`. Generalises the existing single-job status endpoint.
- `PATCH /recordings/{hash}/jobs` → `{stage_name, enabled}` and/or a reordering, validated
  against the registry's partial order; a valid change re-dispatches the phase.

**Frontend** — the badge and 3-second polling patterns in `ComputationView.vue` already
exist for the single indices job; this generalises them to a list. Net-new is only the
ordered list with per-row enable toggles and drag-reorder, the latter constrained by the
partial order from §4. The `AVAILABLE` state gets the "N/X done" progress badge the design
doc §7.3 describes.

## 8. Viewer: version compare (core, not project)

The viewer must fetch cached processed signal instead of processing ad hoc, and overlay it
against another version. This is **core application** functionality, not the project's — the viewer
serves recordings generally, and the version model is about recordings and signal states,
not about EEG correction. The seam:

- **Core knows:** a recording has an ordered set of *versions*, each identified by a
  manifest hash, each carrying a human label and per-channel provenance. It renders whatever
  versions the API returns. It does **not** know what EOG or WQN are.
- **The project supplies:** the stages that produce versions and their labels (from the
  registry, §2). Nothing project-specific is imported by the viewer.

### 8.1 Version-pair, not file-pair

The user framing — "primary from the processed file, original from the original file" — is
the right default, expressed as a *choice of which two versions to show*: primary defaults
to the latest enabled manifest, overlay defaults to source. Making it version-pair rather
than file-pair costs nothing now and unlocks the comparison the findings docs leave open
(EOG-only vs EOG+WQN) for free: same renderer, different second version. The existing
overlay renderer keyed on `correctedChannelSuffix = '_orig'` already draws pairs; the
migration moves the *data source* from "suffix within one file" to "second version fetched
on demand", not the rendering.

### 8.2 Load order and granularity

Processed-first, original-lazy — matching what raters do (score the processed signal, pull
the original only to check a spot). But laziness must be **per-channel-per-window**, not
per-file: the original is inspected in spots, and the viewer already caches decoded
`Float32Array` per channel, so fetch the original channel(s) for the visible window when the
overlay is toggled. Whole-file lazy load still moves 100+ MB the first time someone glances.

The viewer fetches processed **referential** channels and derives montages client-side —
the same invariant that made the correction montage-independent (correct sources, derive on
demand), reused rather than reinvented.

### 8.3 Three correctness edges, all served by per-channel provenance

- **Not every channel is modified by every stage.** A `SKIPPED` channel's processed version
  *is* the source. The manifest's per-channel "modified by [stages]" tells the viewer which
  channels have an overlay worth drawing, so it neither implies a difference where there is
  none nor fetches the original for channels that match.
- **Alignment is guaranteed by the model, not assumed by the viewer.** Source-replay makes
  both versions share rate, length, and origin — but a future resampling stage would break
  index-wise overlay. The version fetch carries the source content-hash; the viewer refuses
  to overlay two versions whose source hashes differ.
- **Which version was scored must be recorded.** An annotation cites the manifest hash the
  rater viewed, and the audit chain captures it. The viewer displays version identity (label
  + hash) and the rating references it — it never silently serves "latest".

### 8.4 The one genuinely new interaction: cache miss

A requested version may not be materialised (evicted, or never built). The fetch is not
"GET a file" but "GET version N, which may return building-in-progress". The viewer needs a
graceful "reconstructing…" state that polls — the §3.5 rebuild-on-miss surfaced at the UI,
and the one place a user waits on the pipeline. Worth designing deliberately.

## 9. Client-side stages via Pyodide

The correction/repair algorithms are candidates to run in the browser as Pyodide service
modules — the ambulance real-time goal, and an offline-capable viewer. Feasibility was
measured (not assumed): with `mne`, `django`, and `federation` hidden, `repair.py`,
`coherence.py`, `psd.py`, `artifacts.py`, `preprocess.py`, `indices.py`, the EDF byte-decoder
in `recordings/processors/edf.py`, and the numeric core of `middleware.py` all import and run
on packages Pyodide ships (numpy, scipy, pandas, pywavelets). `mne` itself is pure-Python and
installs from PyPI via micropip, so even the mne-dependent loaders port; and the
Float32Array handoff (§8) sidesteps EDF decoding for the correction path entirely.

The decoupling is already prototyped: `smoke_commands.py` stubs Django and drives the stages
headless; the only structural change is splitting `middleware.py`'s numeric core from its
`federation` EDF-transport base class (worth doing regardless — those methods are currently
untestable without the base class).

### 9.1 The sharp edge: cross-runtime determinism

This is where client-side execution meets §3.3 and must not be waved through. A stage that
writes a **canonical version into the manifest** must produce the *same* `output_hash` as any
other runtime that recomputes it — that is what makes the manifest a truth rather than a
claim. WASM builds of numpy/scipy can differ from native in floating-point results (different
BLAS, SIMD, fast-math flags). So bit-exact agreement across the browser and the server is
**not free** and must be verified, not assumed.

The clean split follows directly:

- **Tier A — read-only, no manifest written:** diagnostics, real-time preview, "what would
  this look like" exploration. Runs freely client-side; a floating-point discrepancy is
  cosmetic because nothing durable depends on the exact bytes. Most of the value — instant
  feedback, offline inspection — lives here.
- **Tier B — canonical version-producing:** either stays server-side (the canonical runtime),
  or the determinism contract is *verified bit-exact across runtimes first*, which is a real
  task, not a checkbox. A client-produced canonical version that silently disagrees with the
  server is exactly the "manifest is a lie" failure §3.3 exists to prevent.

The ambulance real-time ambition mostly lives in Tier A (show the medic a corrected trace
now), with Tier B as the durable record produced when the recording reaches the server.
Performance (WASM numpy ~2–5× native) and peak memory on a full recording remain unmeasured
and are the other two things to settle before committing to Tier B in the browser.

## 10. Build order

Three phases — backend pipeline, core viewer version-compare, client-side Pyodide — bridged
by one shared artifact: **the version-fetch API contract**. Pin the contract first (how a
version is identified, per-channel provenance, the source-hash guard, the building-in-progress
state, §8) and the phases proceed in parallel against it; leave it implicit and they drift.

*Phase 1 — backend pipeline.* Steps ordered so the riskiest decision is validated earliest.

1. **Settle §3 (source + manifest + cache).** The storage-class split (§3.5) and manifest
   home (§3.4) are infrastructure decisions, not code. Everything else assumes them.
2. **Stage registry + protocol**, EOG and WQN registered, each declaring `code_version` and
   promising determinism (§3.3). No behaviour change yet — dispatch still hardcoded, but
   stages are now enumerable and the config trap is closed. Small, and immediately makes
   "add a stage" real.
3. **Artifact cache volume + manifest storage.** New volume, borgmatic exclusion, manifest
   append to the audit chain. This is the infrastructure §3 rests on; land it before the
   dispatcher writes anything.
4. **Ledger table + migration**, rows created at ingest, state derived from them. Still no
   UI; the modifying stage becomes DB-tracked for the first time.
5. **Phase-aware chained dispatch**, replacing the signal receiver — reads source, rebuilds
   the derived artifact, writes the manifest. This is the one step that touches the path
   that rewrites files — do it after the harness can exercise it, and behind a flag that
   falls back to current behaviour.
6. **`GET /jobs`** + progress badge; **`GET /versions/{n}`** implementing the contract.
7. **`PATCH /jobs`** + enable toggles and constrained reorder. The net-new control UI.

*Phase 2 — core viewer version-compare (§8).* Can start once the contract (step 0) is fixed,
in parallel with phase 1's later steps against a mocked endpoint. Migrate the existing
`_orig` overlay to version-pair fetch; add per-channel provenance rendering, the source-hash
guard, version identity in annotations, and the cache-miss "reconstructing" state. Lands in
**core**, imports nothing project-specific.

*Phase 3 — client-side Pyodide (§9).* Tier A (read-only diagnostics + real-time preview)
first: it is unblocked by the determinism question and carries most of the value. Tier B
(canonical version production in-browser) only after cross-runtime bit-exactness, WASM
throughput, and peak memory are measured — three open questions, not a build step.

## 11. What is decided vs open

**Decided:** two-level phase/stage model; registry mirroring the derived-state one; ledger
shape; chained modifying dispatch; the source + manifest + cache model of §3, with the audit
chain as the **authoritative** manifest home (git only ever a convenience mirror, §3.4) and
the artifact cache as an unbacked-up third volume.

**Open, needs a call:**

- **Cross-stage config coupling.** WQN needs EOG's gate off. `requires` orders stages but
  does not express "stage X changes shared rejection config that stage Y depends on." This
  may need a shared-config object the phase threads through, or it may be acceptable to
  encode as a documented constraint the registry validates. Not solved here.
- **Determinism enforcement.** §3.3 makes determinism a contract, but nothing yet *checks*
  it. A stage could quietly introduce RNG and break addressability with no error. Worth a
  test that runs a stage twice on the same input and asserts identical `output_hash` — cheap,
  and it catches the failure at contribution time rather than in a reproducibility dispute.
- **Testing the rewrite path.** the project's smoke-command tool already drives the
  stages headless against a synthetic EDF; the chained dispatcher should be exercised the
  same way before it touches a real file. The safety net for step 5, built alongside it.
- **Migration of existing corrected files.** Recordings already rewritten in place under the
  old `_orig` convention have a *mutated* source, so their true original must be reconstructed
  before they can enter the model. Bounded, but must not be forgotten.
- **Erasure reaches the cache.** A subject-erasure must purge artifact-cache entries for the
  recording, not leave them to natural eviction (§3.5). Easy to miss precisely because the
  cache is framed as disposable.
- **Cross-runtime determinism for Tier-B Pyodide (§9.1).** Whether a browser-produced
  canonical version can be trusted to match the server bit-for-bit is unmeasured. Blocks
  Tier B only; Tier A is unaffected.
- **The version-fetch contract (step 0).** Both the backend endpoint and the core viewer
  depend on its exact shape. It is the bridge between the three phases and should be pinned
  before either side is built, not negotiated across them. *(Resolved — see
  `version-fetch-contract.md`.)*
- **External-tool transport.** How signal bytes reach an external stage (a Docker model, a
  remote service) and the reply returns — message format, framing, streaming vs request/reply.
  Deferred by decision; the `reproducible` flag (§2) is the only part that could not wait.
- **Availability failure vs computational failure.** An external stage can be *unreachable*
  (container down, network) in a way a pure-Python stage cannot. That is a retryable failure,
  distinct from a deterministic "bad input" failure, and the dispatcher's retry policy (step
  5) must tell them apart — a transient outage should not mark a version permanently `FAILED`.
  Not solved here; flagged so the ledger's failure semantics account for it when built.

The through-line: ordering and tracking are the easy parts and mostly follow an existing
in-tree pattern. The genuinely hard question was what a stage's canonical input *is* once
there is more than one stage — now answered as *source replayed through a versioned,
signed manifest, materialised into an evictable cache*. The three phases hang off that one
answer: the backend produces the versions, the core viewer compares them, and Pyodide may
one day produce them client-side — but only the read-only tier until the manifest can trust
a browser's arithmetic.
