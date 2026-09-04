# SzCORE / BIDS integration — storage, stage model, and transport

**Status: design decision, pre-implementation.** Captures how a SzCORE-style
containerised seizure detector fits the platform without changing the storage
layout, and why. Companion to [`recordings/signal-pipeline-plan.md`](../../recordings/signal-pipeline-plan.md)
(external-stage model) and [`docs/engineering-notes/eeg-tooling-roadmap.md`](eeg-tooling-roadmap.md)
(Phase 4). The input-normalisation prerequisites and the container I/O contract
([compute/contract.py](../../compute/contract.py)) have since shipped; `to_bids`, the runner
sidecar and the DERIVE-phase stage registration are not built.

## TL;DR

1. **BIDS is an export/transport format, not a storage format.** Do **not**
   migrate the storage layout to BIDS. Materialise a BIDS view on demand at the
   container boundary.
2. A SzCORE detector is an **external *analysis* stage** — it emits
   annotations/events, not transformed signal bytes — which is a distinct shape
   from the `transform` stages in the pipeline plan.
3. **Sequencing:** braindecode stays next. Adopt the SzCORE container I/O
   *contract* now as a documented convention; build the BIDS adapter when the
   first containerised detector lands (seizure phase), not before.
4. **Transport:** the container only ever sees *a path*. Default backing is
   materialise-to-tempdir (signal hardlinked/reconstructed once, sidecars
   synthesised); a FUSE-backed provider is a pluggable alternative for
   compute-on-read and federated cases. The backing is swappable without
   touching the container or the detector.

## 1. Why not store BIDS

The canonical store is content-addressed (`stored_name` / `content_hash` /
`file_hash`), PHI-scrubbed (`original_name` is author-only; `display_name` falls
back to a hash prefix), and its truth is the manifest plus relational metadata
(`RecordingMeta` / `SignalInfo`) and the `annotations` app. BIDS-as-storage
fights every one of those:

- **PHI in paths.** BIDS encodes `sub-<label>/ses-<label>/…` into directory and
  file names. The store deliberately hashes identity out of paths; reintroducing
  subject labels on disk is the exact pattern the design avoids.
- **Lossy denormalisation.** `SignalInfo` (per-channel ranges, filters,
  transducer, sampling) and `RecordingMeta` are richer and queryable; BIDS
  sidecar JSON/TSV are a flat, lossy projection of them.
- **Collides with the invariants.** Content-addressing, dedup, the audit chain,
  preservation, and GDPR erasure all assume the store's own layout. BIDS' subject
  hierarchy and file-sidecar model are an impedance mismatch.

This is not a new position — the pipeline plan already states it for the file
container generally: *"keep it for export. Stop storing the truth in it."* BIDS
is one more export view of that kind, and SzCORE only ever wants the export view:
its contract is **BIDS directory in, TSV annotations out, offline, per run.**

## 2. Detectors are analysis stages, not transform stages

The pipeline plan's stage contract is
`transform(header, signals) -> (header, signals)` — input bytes to *output
signal bytes*, with a `reproducible` flag deciding cache-vs-archive lifecycle.

A detector (SpikeNet, SeizureTransformer, …) does **not** fit that shape: it
consumes signal bytes and emits **annotations/events** (→ `annotations.Event`),
producing no new signal version. So integrating one means adding an
**analysis/scoring stage** variant alongside the transform stages — a stage whose
output is annotation rows, not a version.

The `reproducible` machinery still applies, unchanged: a pinned-digest
deterministic detector is reproducible; a GPU/ML detector generally is **not**
(`reproducible = False`), so the annotations a clinician acted on are **archived,
not evicted**, and `code_version` pins the image *digest*, never a mutable tag.
The plan already handles exactly this.

## 3. Sequencing — why not start here

A real SzCORE integration is gated on two things, **neither of which is storage**:

- the external-stage dispatcher from the pipeline plan actually landing, and
- the analysis-stage (annotation-producing) variant above.

braindecode has neither dependency — it is an in-process PyTorch library for
loading/training models, no pipeline, storage, or container coupling — so it
remains the better next concrete step. What is cheap and worth doing *now*:

- **Adopt the SzCORE container I/O contract as a documented convention** in the
  pipeline plan (design note, zero code), so every containerised detector slots
  into the external-stage transport uniformly.
- Defer the BIDS adapter to the seizure phase; build it then as a standalone
  `to_bids(recording, dest)` exporter (independently useful for data sharing,
  `mne-bids-pipeline`, and any BIDS App).

## 4. Transport — real filetree vs virtual filesystem

**The container contract is a path.** A SzCORE/BIDS-App container reads a BIDS
tree from a fixed input path and writes annotations to a fixed output path. It
never knows — and must never depend on — how that input path is backed. Model the
adapter as a **BIDS input provider** that yields a path; the backing is an
implementation detail behind it.

### Default backing: materialise-to-tempdir (cheap)

Write a real tempdir, but avoid copying signal bytes: **hardlink** the stored EDF
(or reconstruct the scored version once) and **synthesise only the tiny
sidecars** — `dataset_description.json`, `_eeg.json` (from `RecordingMeta` +
`SignalInfo`), `_channels.tsv` (from `SignalInfo`), `_events.tsv` (from
`annotations.Event`). This is robust, portable, least-privilege, and matches
SzCORE's offline-container expectation. It is the right default.

**Hardlink, not symlink — this is load-bearing.** A *symlink* in the tempdir
pointing at the stored EDF **will not resolve inside the container**: the
container only bind-mounts the tempdir, so the link's target sits outside its
mount namespace and dangles. Only a **hardlink** presents the bytes inside the
tree without copying — and a hardlink requires the tempdir to be on the *same
filesystem* as the stored recording. So the rule is: hardlink when same-FS,
**copy** when cross-FS, never a plain symlink; provision the tempdir root on the
recordings volume's filesystem to keep the fast path. (A compute-on-read scored
version has no stored blob to link and is materialised once regardless.)

### Alternative backing: FUSE-backed view (reuse `federation/fuse_fs.py`)

The federation FUSE layer already does the hard part: a **read-only** presentation
of recordings as files, with **on-read transforms** (Layer 1 server-side
anonymisation; Layer 2 local channel-drop / downsample) and accurate `stat()`
sizes precomputed from per-channel info. A detector's BIDS input is a natural fit
for the same machinery — present a recording as a BIDS tree and compute the
*analysis version* (montage subset, downsample, reconstruction) on read, exactly
like the Layer-2 pipeline. This genuinely wins in two cases:

- **Compute-on-read versions** — the scored version is manifest-derived, so there
  is no stored blob to hardlink; the bytes are produced on read.
- **Federated recordings** — the recording lives on a peer; `fuse_fs.py` already
  streams it over HTTP with `Range` requests. A BIDS-shaped wrapper over that is
  the reuse.

Note the current `fuse_fs.py` is **remote-oriented** (fetches from a peer, lays
out `<peer-slug>/<filename>`). Using it for a *local* recording in a *BIDS*
layout means generalising it (a local/manifest backend + a BIDS view), which is
real work — not free reuse.

### Why the default is still a real tempdir (the FUSE caveats)

- **Docker + FUSE propagation is fragile.** A container sees FUSE-backed files
  only if the bind mount is `rshared`/`rslave` and the FUSE mount predates the
  container; rootless/hardened Docker often blocks it. A plain tempdir bind mount
  has none of this.
- **Read pattern.** A detector does one sequential full-file read. FUSE's
  lazy/partial advantage is muted; it adds per-read syscall overhead for no gain
  on a local bounded file.
- **Isolation.** SzCORE detector images are third-party/arbitrary. A read-only
  bind of a plain dir is least-privilege; putting a host FUSE daemon in the read
  path of an untrusted container enlarges the trust surface.
- **Writable output.** The container also needs a *writable* output dir for the
  annotations — a small real tempdir regardless of how the input is backed.

**Conclusion:** start with materialise-to-tempdir; reach for a FUSE-backed
provider only when compute-on-read or federated access makes it worth the
complexity. Because both sit behind the same path abstraction, the choice can be
deferred and mixed per run without touching the container or the detector. This
is the concrete instantiation of the pipeline plan's *"transport deliberately
deferred"* slot.

## 5. PHI and correctness caveats

- **Pseudonymous labels only.** The BIDS `sub-<label>` must not leak PHI — reuse
  the recording's existing public handle `stored_name[:8].upper()` (already the
  `display_name` fallback), which is uppercased-alphanumeric and so a valid BIDS
  label as-is; assert `^[A-Za-z0-9]+$` at write. Case-insensitive-FS collisions
  are moot: each ephemeral tree holds exactly one subject. Omit acquisition
  datetime (already nulled after de-id). Omit or pseudonymise `participants.tsv`
  — no demographics today; a future detector that needs age gets a documented,
  minimisation-preserving hook (a coarse band, not a birthdate), not a raw field.
  Keeping BIDS ephemeral at the boundary is itself a control: you decide exactly
  what leaves the store.
- **Target SzCORE's specific profile.** SzCORE expects a particular EEG-BIDS +
  HED-SCORE annotation flavour, not generic BIDS — build the adapter against a
  current SzCORE template and validate, don't assume.
- **Seizure-oriented pattern.** SzCORE's contract is reusable for spikes/sleep as
  a *pattern*, but its annotation vocabulary is seizure-specific; extend it for
  other event types rather than taking the schema verbatim.

## 6. Detector dispatch and communication chain

A SzCORE detector is a **DERIVE-phase analysis stage** in the `RecordingJob`
ledger (`recordings/signal-pipeline-plan.md` §5–6): read-only, dispatched in the
parallel fan-out *after* `AVAILABLE`, and its output is **annotations, not a
signal version**. So it reuses the ledger for status but lands in the
`annotations` app, never the version/manifest chain.

### The loop

1. **Trigger (Viewer → backend).** Either automatic — a ledger row created at
   ingest for the enabled detector stage — or on-demand via
   `PATCH /recordings/{hash}/jobs {stage_name, enabled:true}`, which re-dispatches
   the DERIVE phase. The endpoint does auth / read-permission, then a **cache
   check** on `(input_digest, image_digest, params)`; a hit returns the existing
   result and never touches a container.
2. **Enqueue.** On a miss the dispatcher writes the `RecordingJob` row `QUEUED`
   and fires the Celery task into the DERIVE fan-out. Idempotent on the cache key
   (two clicks collapse to one run).
3. **Process (task → runner → container).** Resolve the **input version** — the
   reconstructed/cleaned signal RECONSTRUCT already produced — and record its
   `input_digest` on the row. Materialise a **pseudonymous BIDS tempdir** (§4
   default transport), create the output dir, mark `RUNNING`, and hand the run to
   the sidecar runner.
4. **Container.** Reads BIDS from the read-only input path, writes HED-SCORE
   annotations to the output path, offline. Nothing else is reachable.
5. **Ingest.** Parse the output → `annotations.Event` rows (`timestamp`,
   `duration`), each with a `Code(standard="hed", value=…, meta={schema, detector,
   image_digest, confidence})`; dense per-sample probability → a
   `SpikeNetResultCache`-style blob. Mark `DONE`, or `FAILED` with the sanitised
   container error.
6. **Response (backend → Viewer).** The Viewer's existing 3-second badge poll on
   `GET /recordings/{hash}/jobs` watches the row go `RUNNING → DONE`, then fetches
   the `Event` list (filtered by detector provenance) and the probability trace
   (the lead-field/SpikeNet binary-endpoint pattern) and overlays seizure spans +
   a heat-strip. Long jobs may additionally push-notify via the `notifications`
   app.

```
Viewer          Django/Ninja        Celery (DERIVE)      Runner sidecar        Container
  │ PATCH /jobs  │ cache check        │                    │                     │
  ├─────────────>│──enqueue──────────>│ resolve input ver. │                     │
  │  202 QUEUED  │                    │ materialise BIDS    │                     │
  │ GET /jobs 3s │                    │ POST /runs ────────>│ docker run ────────>│ BIDS in
  │<──RUNNING────│                    │                     │                     │→ HED-SCORE out
  │              │                    │ GET /runs/{id} ────>│<────────────────────│
  │              │                    │ ingest → Event+Code(hed) + prob cache
  │ GET /jobs 3s │                    │ mark DONE           │                     │
  │<──DONE───────│                    │                     │                     │
  │ GET /annotations ; GET …/prob/ → overlay                │                     │
```

### Decisions

- **Execution: a dedicated detector-runner sidecar (decided).** The Celery worker
  does **not** hold `docker.sock`; it calls a small, locked-down runner service
  over an internal API. SzCORE images are arbitrary third-party code, and
  docker-out-of-docker would hand the worker — and thus any compromised detector —
  control of the host daemon. The runner is the single chokepoint where container
  lifecycle, sandboxing, resource limits, and image-digest pinning live; workers
  stay unprivileged. Rootless Podman inside the runner is a reasonable engine.
- **Trust boundary (non-negotiable).** `--network=none`; read-only, **pseudonymous**
  BIDS input (no PHI crosses the boundary, §5); non-root; dropped capabilities;
  CPU / memory / wall-clock cgroup limits; killable.
- **Input-version binding.** Score the reconstructed version RECONSTRUCT produced
  (DERIVE runs after `AVAILABLE`); persist its `input_digest` so the result traces
  to exact bytes.
- **Result home + provenance.** Seizure events → `annotations.Event` +
  `Code(standard="hed", …)`. HED-SCORE is the machine-readable form of the
  ILAE-endorsed **SCORE** standard (Beniczky et al.; Beniczky co-authored SzCORE),
  so events land SCORE-coded, not bespoke. The `standard` is `hed` rather than
  `hed-score` because a HED string may mix SCORE-library and standard-HED tags in
  one namespace; which library and version produced it is pinned in `meta`
  ([hed-score-integration.md](hed-score-integration.md)). Every row records image digest, params,
  `input_digest`, and timestamp.
- **Reproducibility → cache vs archive.** Pin the image by **digest, never a tag**.
  Deterministic detector → evictable cache keyed `(input_digest, image_digest,
  params)`. Nondeterministic (GPU) → `reproducible=False` → the annotations a
  clinician acted on are **archived, not rebuilt** (plan §3.3). The image digest
  is the `code_version`.
- **Async contract.** Reuse the `RecordingJob` ledger + 3-second badge poll
  (`ComputationView.vue`); no new status machinery. Results fetched via the
  annotations API + the probability endpoint; optional push-notify for long jobs.
- **Failure semantics (plan §10, resolved here).** The runner returns a *typed*
  verdict: **availability failure** (image pull, OOM, timeout, runner down) → retry
  with backoff, not a permanent `FAILED`; **computational failure** (non-zero exit,
  invalid output) → terminal `FAILED` with sanitised container stderr in the ledger
  `error`.
- **Cancellation & backpressure.** Long runs on a dedicated Celery queue; cancel =
  revoke task + runner kills the container + row → cancelled; per-user limits via
  `throttle.py`.
- **Scope.** Batch only (whole recording in, events out). Real-time / streaming
  detection is a different contract — explicitly out of scope.

### Runner API (sketch)

Keep the worker dumb; the runner owns all container privilege:

```
POST   /runs      {image_digest, input_dir, output_dir, params, limits} -> {run_id}
GET    /runs/{id} -> {state: pending|running|done|failed,
                      failure_kind: availability|computational|null,
                      exit_code, stderr_tail,
                      started_at, finished_at}   # container lifecycle, runner-authoritative
DELETE /runs/{id} -> kill
```

The runner does the image pull (by digest), applies the sandbox flags and cgroup
limits, enforces the timeout, and makes the availability-vs-computational call.

The runner is the **authoritative source for the container lifecycle timestamps**
(`started_at` / `finished_at`): it owns the container, so only it knows when the
image actually began and stopped executing — as distinct from when the backend
queued the run. The backend reads these back on `GET /runs/{id}` and records them,
alongside its own `queued_at` / `materialised_at` / `finalised_at`, into the
`DetectorRunAuditLog` entry's free-form `metadata` JSON (see
[`bids-export-privacy-design.md`](bids-export-privacy-design.md) §4). That split is
what lets the audit distinguish `exposure_ms` (how long PHI was materialised) from
`container_ms` (actual compute), since a run can sit queued before it starts.

## Input-normalisation prerequisites (settled)

`to_bids` must not become a dumping ground for input-cleanup logic that the rest
of the platform also needs. Three recording-level concerns were resolved as
platform changes that `to_bids` (and SpikeNet, YASA, the forward model) all
consume, rather than exporter-local hacks:

- **Canonical channel names.** *Resolved by phases 1–3 of the
  [channel-de-identification plan](channel-deidentification-plan.md):* ingest now
  rewrites `SignalInfo.label` to the canonical name and keeps the raw header string
  (`'EEG T3-Ref'`) author-private as `source_label`, and a vendor converter that
  preserves the old nomenclature (T3/T4/T5/T6, A1/A2) is normalised by the same
  pass. The original proposal, kept for the reasoning: add a `canonical_label`
  column populated at ingest by a normaliser (strip the `EEG `/reference affixes → old→new 10-10 map, e.g.
  T3→T7, T4→T8, T5→P7, T6→P8, A1→M1, A2→M2 → validate against a 10-10 vocabulary;
  blank for non-EEG). It is **non-destructive** — the raw `label` stays for
  provenance, the stored EDF is not mutated. `to_bids`'s channel step then reads
  `canonical_label`, selects the detector's expected set, and drops extras
  (EKG/EMG/unmatched) — the simple relabel-then-drop behaviour, with the mapping
  owned once by the platform instead of buried in the exporter.
- **Mains frequency — deployment default + per-recording override.** Today
  `notch_hz` is a per-call detector parameter with no recording-level source (the
  core keeps *no* regional default, by design). Resolve it at the platform layer:
  a deployment setting `EEG_MAINS_HZ` (unset ⇒ unknown ⇒ BIDS `PowerLineFrequency`
  = `n/a`, no notch) as the backstop, overridden by a nullable
  **`Recording.power_line_frequency`** (on `Recording`, *not* `RecordingMeta` —
  meta/`SignalInfo` are rebuilt on every reprocess and would wipe an override).
  Resolution: `recording.power_line_frequency ?? settings.EEG_MAINS_HZ ??
  unknown`. Not per-dataset: a recording is a many-to-many member of datasets
  across users, so a dataset-level value is ambiguous. The parsed `SignalInfo.notch`
  stays *advisory evidence* (surface it in the UI; warn on disagreement with the
  resolved value; optionally pre-suggest an override when unambiguous). The
  override needs a **batch-apply endpoint** (mirror the collection bulk-rename
  that writes `display_name`) for importing a foreign-region dataset. The resolved
  value feeds both `_eeg.json`'s `PowerLineFrequency` and the detector `notch_hz`,
  and still flows into the existing detection-cache identity key — reproducibility
  unchanged, only the *source* of the number moves.
- **Format gate — fail fast on non-EDF.** `RecordingMeta.format` is
  `edf | edf+ | bdf | bdf+`; the platform stores EDF, and SzCORE's contract is
  EDF-shaped. `to_bids` asserts EDF/EDF+ and returns **SKIPPED** (not FAILED) with
  a clear reason on a BDF recording — no format sniffing, no lossy transcode
  inside the hardening boundary.

## What to build, and when

| When | What |
|---|---|
| Now | Adopt the SzCORE container I/O contract as the documented external-stage transport convention in the pipeline plan. No code. |
| Seizure phase | Register the detector as a **DERIVE-phase analysis stage** in the `RecordingJob` ledger (§6); dispatch in the read-only fan-out after `AVAILABLE`. |
| Seizure phase | Build the **detector-runner sidecar** (rootless-Podman engine): digest-pinned pull, sandbox + cgroup limits, typed availability/computational verdict; `POST/GET/DELETE /runs`. |
| Seizure phase | Add the analysis-stage (annotation-producing) variant to the stage model; ingest HED-SCORE → `Event` + `Code(standard="hed")`, cache the probability trace. |
| Seizure phase | Add `SignalInfo.canonical_label` + an ingest normaliser (10-10 vocabulary; non-destructive); reuse across `to_bids`, SpikeNet, YASA, forward model. |
| Seizure phase | Add `EEG_MAINS_HZ` setting + nullable `Recording.power_line_frequency` override + batch-apply endpoint; resolver feeds `PowerLineFrequency` and detector `notch_hz`. |
| Seizure phase | Build `to_bids(recording, dest)` behind a BIDS-input-provider interface; default backing = materialise-to-tempdir (**hardlink** signal, copy if cross-FS; synth sidecars); EDF-only, SKIPPED on BDF; `sub-` = `stored_name[:8].upper()`. |
| Later / if needed | FUSE-backed provider (generalise `federation/fuse_fs.py`) for compute-on-read and federated recordings. |
| Never | Migrate the storage layout to BIDS. Give the worker `docker.sock` (use the runner sidecar). |
