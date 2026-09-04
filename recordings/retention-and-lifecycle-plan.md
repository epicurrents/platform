# Artefact retention, analysis ordering, and lifecycle

Sibling to [`signal-pipeline-plan.md`](signal-pipeline-plan.md). That plan settles the
**reconstruction** side: an ordered, content-addressed chain of signal-*mutating* stages
whose recipe is a `Manifest` and whose identity is a `version_id`. This note settles two
questions that plan deliberately left open, both surfaced while a parallel session built the
`compute/` analysis tools (spike detectors, braindecode, cleaning, sleep, SzCORE):

1. **Ordering of *analysis* stages** that produce annotations rather than signal bytes, and
   that sometimes depend on each other's outputs.
2. **Retention** — which artefact bytes are kept, which are evictable, and what the default
   is for outputs that cannot be rebuilt.

Neither changes the three built cores (`stages.py`, `registry.py`, `manifest.py`) or the
manifest's identity rule. Both are decisions of policy and dispatch that wrap them.

---

## 1. Two registries, not one

The reconstruction registry is **not** widened to hold analysis stages. The two kinds of
dependency are different in nature, and merging them would corrupt what the manifest means.

**Reconstruction `requires` encodes mutation order on shared bytes.** EOG-then-WQN is a
different signal from WQN-then-EOG; the order is non-commutative and the manifest *hashes*
it because it determines the output bytes. The result is a **linear chain**, one lineage of
signal bytes, and `registry.py` correctly rejects any stage whose `phase is not
Phase.RECONSTRUCT`.

**Analysis `requires` encodes a producer/consumer data dependency on an *artefact*.** Sleep
staging must precede spindle detection because the spindle detector *reads the hypnogram*,
not because it mutates a signal the stager also touched. This graph is a **DAG with
fan-out** — one hypnogram feeds spindles, REM-behaviour analysis, and arousal indices
independently — and its nodes:

- produce **annotations, not signal bytes** (a DERIVE-phase *analysis* stage);
- have identity `(input_digest, image_digest, params)` — the run key the `compute/` design
  already uses — **not** a manifest `version_id`;
- invalidate differently: rerunning sleep staging invalidates only the hypnogram's
  consumers, not the signal and not independent analyses.

So analysis stages live in a **separate analysis job-DAG**. It may reuse the *same mechanism
shape* as the reconstruction registry (declare `requires`, topological resolution, cycle
detection — plausibly the same code, a second instance), but it resolves to a DAG dispatched
job-by-job. A node becomes runnable when **its signal version exists** *and* **every
required annotation is present for that `(recording, version)`**.

### 1.1 Analysis stages require by *annotation kind*, not producer stage

This is the deliberate asymmetry with the reconstruction side. There, the producer *is* the
identity — one EOG stage yields the corrected bytes — so requiring by stage name is correct.
On the analysis side a hypnogram may come from YASA, from braindecode, or from a human
scorer, and the spindle detector must not care which. Analysis stages therefore declare
dependencies on **annotation kinds** (`hypnogram`, `spike_events`, `artifact_spans`, …), and
any completed run — automated or manual — that produced that kind satisfies the dependency.

This implies one new primitive: a small **registry of annotation kinds**, a controlled
vocabulary of semantic tokens that producers advertise and consumers require. It should live
with the `annotations` app so the `compute/` tools and our analysis dispatch resolve against
the same tokens rather than each inventing strings. A consumer's `input_digest` folds in the
digests of the annotation sets it actually consumed, keeping the cache/audit identity honest
across the signal→annotation boundary.

---

## 2. Retention

### 2.1 The reframing: reproducibility ≠ retention

The built design uses `reproducible` as a proxy for "must keep." Separate the two axes:

- **Reproducible?** — can these bytes be rebuilt? Governs the *cost* of deletion:
  rebuildable = free (recompute from source + manifest), non-reproducible = irreversible.
- **Referenced?** — does anything point at these *exact* bytes? Governs the *consequence* of
  deletion: referenced = deleting orphans something, unreferenced = no harm.

From this, the whole policy collapses onto **one governed class of bytes**. The only
artefacts that can ever be non-reconstructable are:

- the **immutable source** — always kept; and
- the **outputs of non-reproducible stages**.

Everything reproducible is a pure function of source + manifest and is therefore *always*
evictable cache — evicting it costs compute, never data. So retention policy needs to decide
about exactly one thing: **non-reproducible stage outputs.** (Note this includes
non-reproducible *intermediaries*: a stochastic stage feeding a further stage is the root of
its whole sub-chain's reproducibility, so its output is destructive to evict even when
nothing references it directly.)

### 2.2 Reference, not annotation-presence, is the retention root

"Contains annotations" is the main case but too narrow. Model retention as **reference
counting**, where a live reference to a version's exact bytes is any of:

- an **annotation** anchored to that version;
- an explicit **pin**;
- an exported or citeable **`version_id`** (published, in a report);
- a **federation grant** — a peer holds or may pull it;
- a **retained derivative** built from it.

An artefact is a GC root iff its reference count is > 0.

### 2.3 The default: retain; eviction is opt-in

**Reproducible artefacts** need no policy and no ceremony: they are loss-free cache, evicted
under a plain cache default (size/TTL). This covers the entire current pipeline (EOG, WQN are
both reproducible), so a fresh install processes them immediately.

**Non-reproducible outputs default to *retain* — no automatic eviction of any kind.**
Eviction of non-reproducible bytes is a strictly explicit administrator opt-in. The
reasoning is asymmetric failure modes: if the default is retain and an admin forgets to
configure eviction, the failure is a **full disk** — visible, non-destructive, recoverable.
If the default permitted eviction and they forget to lock it down, the failure is **silently
destroyed, irreproducible provenance** — invisible until someone opens an orphaned
annotation, and irreversible. For clinical data the recoverable failure wins every time.

The admin opt-in, when set, chooses the eviction scope for non-reproducible outputs:

| profile | non-reproducible outputs | intended deployment |
|---|---|---|
| **retain-all** (default) | never evicted | clinical / record-of-record |
| **retain-if-referenced** | evicted when reference count hits 0 (after grace) | space-conscious, provenance-preserving |
| **transient** | evictable regardless of references; orphaning accepted | benchmarking, throwaway |

### 2.4 Safeguards on eviction

- **Audit records are retained independently of bytes.** The space cost is the bytes; the
  audit row (manifest, `version_id`, stage, `code_version`, "ran at T, evicted at T′") is
  tiny. Keep it always, in every profile. Traceability *of process* survives even when the
  bytes are gone.
- **Tombstone on eviction of a referenced non-reproducible output.** Chain verification
  (`activity.audit.verify_chain`) must be able to distinguish "evicted under policy P" from
  "tampered / missing", and a reader of an orphaned annotation must see that its bytes were
  *intentionally discarded* rather than corrupted.
- **Pin primitive + grace window.** A `pin` protects a version explicitly; GC never sweeps
  anything younger than a TTL or while its containing job/session is open. This closes the
  footgun where an ML output is swept before the operator has anchored an annotation to it.
- **Governance coupling.** The `transient` profile is incompatible with a clinical /
  PII-retention deployment; if those flags are set, refuse to select it rather than trust the
  setting was deliberate.

### 2.5 What this does *not* touch

Retention is orthogonal to identity: nothing here enters the manifest hash, consistent with
the existing rule that `reproducible` and storage lifecycle are not part of `version_id`.
Two byte-identical versions must stay identical regardless of where they are stored or
whether one has been evicted.

---

## 3. Consequences for the roadmap

- The **`RecordingJob` / audit-persistence** step (plan §5–6, still unbuilt) should be
  designed to carry **both** signal-version manifests **and** analysis-run records, since the
  `compute/` session is already building analysis consumers against that ledger.
- Stand up the **annotation-kind vocabulary** as its own small piece alongside the
  `annotations` app.
- GC is a **mark-sweep** over the artifact-cache tier: roots = source + referenced
  non-reproducible outputs + pins (+ everything, if `retain-all`); sweep evicts the rest,
  tombstoning any referenced eviction. Reproducible artefacts are swept under the ordinary
  cache default.
- No change is required to `stages.py`, `registry.py`, or `manifest.py`.
