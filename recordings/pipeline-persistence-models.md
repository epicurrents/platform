# Pipeline persistence — model sketches

**Status: models implemented — `compute` migration `0003`, `annotations` migration `0006`.
Dispatch, GC/retention, and the service/API layers are still unbuilt.** The durable/operational
models that the three pipeline plans left deferred:
[`signal-pipeline-plan.md`](signal-pipeline-plan.md) (reconstruction, §5–6 ledger),
[`retention-and-lifecycle-plan.md`](retention-and-lifecycle-plan.md) (analysis DAG, retention),
[`analysis-execution-plan.md`](analysis-execution-plan.md) (segment fan-out, conformance).
Field names may still move; the two structural questions that were open in the first draft —
**app placement** and the **inter-table reference shape** — are now settled and are baked into the
shapes below. What is *settled* and drives them:

- **Two run models, not one.** A reconstruction run (linear byte-mutating chain, identity =
  manifest `version_id`) and an analysis run (annotation-producing DAG-by-kind, identity =
  `(input_digest, image_digest, params)`, plus segments/coverage/conformance the reconstruction
  side has no concept of). One table serving both would be a nulls-ridden compromise.
- **`compute` owns the pipeline models.** All of `RecordingJob`, `ArtifactCacheEntry`,
  `AnalysisRun`, `AnalysisSegment`, `RunAnnotation`, and `PipelineRunAudit` live in `compute` —
  the pipeline is one app. `recordings` stays about the *file* (the `Recording` and its identity);
  `compute` owns everything about *processing* it, and depends on `recordings`/`annotations`
  (never the reverse). `AnnotationKind` is the one exception — it is a vocabulary the
  `annotations` app owns (retention §1.1).
- **Manifests already have a home: the `ObjectChangeLog` audit chain** (signal-plan §3.4,
  decided). The version *identity* is not a new table — it is the manifest hash signed into the
  chain. What the DB needs is the *operational* ledger and the *materialised bytes*, not a
  second copy of the recipe.
- **Recording a run into the audit chain is a CREATE, never a MODIFY.** The source bytes are
  immutable; every derivative (a new version, a new annotation set) is a *new* content-addressed
  asset. So the `ObjectChangeLog`/audit event for a completed run is the CREATE of that asset —
  never a MODIFY of the source recording. Nothing these pipelines do mutates an existing asset.
- **One audit log, dedicated and chained, polymorphic via a JSON `meta` payload** — each pipeline
  serialises its own structure into `meta` rather than one table carrying every pipeline's columns
  as nulls; and selected events *also* get an explicit `log_activity` breadcrumb so they surface on
  the recent-events timeline (§D), mirroring how `federation` splits its audit.
- **The clinician sees one versioned recording.** Signal derivatives *are* the versions;
  analysis results are annotation sets bound to a `(recording, version_id)`.
- **The run tables reference each other by `(recording, version_id)` strings — no FKs between
  them.** This is forced by content-addressing and by eviction; see §E.

## Model map

| Model | App | Role | Rebuildable? |
|---|---|---|---|
| `RecordingJob` | compute | Reconstruction operational ledger (§5) — one row per RECONSTRUCT stage | yes (registry + manifest); live-state, **not chained** |
| `ArtifactCacheEntry` | compute | Materialised version bytes in the cache tier, keyed `(recording, version_id)` | yes (bytes) / entry is the GC unit |
| `AnalysisRun` | compute | One annotation-producing run against a version | no — the run *record* is durable |
| `AnalysisSegment` | compute | Per-segment coverage of a run, unioned on read | yes (re-fan-out) |
| `RunAnnotation` | compute | Provenance edge — which `(run, segment)` produced an `annotations.Event` | no — provenance, durable with the run |
| `AnnotationKind` | annotations | Controlled vocabulary of kind tokens producers advertise / consumers require | n/a (config) |
| `PipelineRunAudit` | compute | Durable, append-only run-event audit (dedicated, chained via `activity.audit`); per-pipeline payload in `meta` | **no — never** |

A detector's result cache and the eventual per-detector caches are *not* in this list — they are
throwaway result caches (like `LeadFieldCache`), rebuildable from an `AnalysisRun` + the model,
and evicted under the plain cache default. The `AnalysisRun` is the durable record; the cache is
its materialisation.

---

## A. `RecordingJob` — reconstruction ledger

Straight from signal-plan §5, RECONSTRUCT-only (the `phase` field is dropped — DERIVE lives in
`AnalysisRun`). Live run state; rebuildable from the registry + the manifest, so it carries no
irreplaceable data. It lives in `compute` and FKs *out* to `recordings.Recording`.

```python
class RecordingJob(models.Model):
    recording = models.ForeignKey("recordings.Recording", on_delete=models.CASCADE, related_name="reconstruction_jobs")
    stage_name = models.CharField(max_length=64)  # matches registry.py
    order = models.PositiveSmallIntegerField()  # resolved topological position
    enabled = models.BooleanField(default=True)  # per-recording stage override

    class State(models.TextChoices):
        QUEUED = "queued"
        RUNNING = "running"
        DONE = "done"
        FAILED = "failed"
        SKIPPED = "skipped"

    state = models.CharField(max_length=8, choices=State.choices, default=State.QUEUED, db_index=True)

    # §5: hash of what this stage consumed — proves which bytes it saw, and lets the
    # rebuilder decide cache validity. This is §3.2's input_hash surfaced on the run row.
    input_digest = models.CharField(max_length=64, blank=True, default="")
    # The version this stage PRODUCED (its output manifest hash), or '' until DONE. The join
    # to ArtifactCacheEntry and to annotations bound to this version.
    output_version_id = models.CharField(max_length=64, blank=True, default="")

    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    error = models.TextField(blank=True, default="")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["recording", "stage_name"], name="recordingjob_unique_stage_per_recording")
        ]
        indexes = [models.Index(fields=["recording", "order"])]
```

**These rows are the operational ledger, and are deliberately *not* part of any integrity chain.**
They are live state (`QUEUED → RUNNING → DONE`) that is fully rebuildable from the registry + the
manifest, so hash-chaining them would protect nothing that isn't already re-derivable. What *is*
chained is the durable fact that a stage produced a version — recorded once, on completion, as a
**CREATE** of a new version asset in the audit trail (§D), never a MODIFY of the source. Keep the
two apart: rebuildable operational state here, permanent CREATE event there.

Recording-level derived status (`PROCESSING`/`AVAILABLE`/`READY`) is computed from these rows
plus the `AnalysisRun` rows, per §5 — see §F.

## B. `ArtifactCacheEntry` — materialised version bytes

The cache tier of signal-plan §3.5 made a row. The **GC unit**: reference-counted, evictable,
and — critically — the thing a subject-erasure must *purge* (§3.5's "missing that last clause is
a compliance hole"). Not backed up; may sit on scratch. Also in `compute`, FK out to `recordings`.

```python
class ArtifactCacheEntry(models.Model):
    recording = models.ForeignKey(
        "recordings.Recording", on_delete=models.CASCADE, related_name="artifact_cache_entries"
    )
    version_id = models.CharField(max_length=64)  # manifest hash, or 'source'
    # reproducible governs eviction cost (rebuildable vs irreplaceable) — NOT identity
    # (never in the manifest hash). See retention §2.1.
    reproducible = models.BooleanField(default=True)
    storage_path = models.CharField(max_length=1024, blank=True, default="")  # '' when evicted
    size_bytes = models.BigIntegerField(default=0)

    class Disposition(models.TextChoices):
        MATERIALISED = "materialised"
        EVICTED = "evicted"  # reproducible: gone, rebuild on miss
        TOMBSTONED = "tombstoned"  # non-reproducible referenced eviction (retention §2.4)
        PURGED = "purged"  # subject erasure

    disposition = models.CharField(max_length=12, choices=Disposition.choices, default=Disposition.MATERIALISED)

    created_at = models.DateTimeField(auto_now_add=True)
    last_accessed_at = models.DateTimeField(null=True, blank=True)  # LRU
    pinned = models.BooleanField(default=False)  # explicit retention (retention §2.2)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["recording", "version_id"], name="artifactcache_unique_version_per_recording"
            )
        ]
```

Reference count is **computed, not stored** (§E). `reproducible=True` entries evict under a
plain size/TTL default; `reproducible=False` entries follow the retain-by-default policy and
tombstone when a *referenced* one is evicted.

**On the `version_id` sentinel (kept as `'source'`, not NULL).** A non-empty manifest resolves to
its sha256; the base version resolves to the literal `SOURCE_VERSION_ID = "source"`
(`manifest.py`). We keep that sentinel rather than modelling "the source" as `version_id IS NULL`,
because NULL would fork "the original" into two representations — the string `'source'` that the
manifest core, the API, and annotations already use, and a NULL the DB layer would invent — and
every join and uniqueness check would have to handle both. One canonical name is worth a reserved
string. Note the direct consequence for the key: `'source'` is the *same* string for every
recording, so it is **not** globally unique — which is exactly why the composite `(recording,
version_id)` is the key everywhere and `version_id` alone never is (a real manifest hash already
encodes its source and would be unique, but the sentinel is not).

## C. `AnalysisRun` + `AnalysisSegment` + `RunAnnotation`

The durable analysis record. Identity is the `compute/` run key, and it **references the exact
version it scored** (the cross-pipeline link §E depends on). Carries the temporal-decomposition
contract (analysis-execution §1) so a resumed run reconstructs identical segmentation, and the
conformance verdict backing `reproducible` (analysis-execution §5). A completed run is, like a
reconstruction, the **CREATE** of a new asset — an annotation set — never a MODIFY of the signal.

```python
class AnalysisRun(models.Model):
    recording = models.ForeignKey("recordings.Recording", on_delete=models.CASCADE, related_name="analysis_runs")
    # The signal version scored — the load-bearing cross-model reference (§E). A plain string,
    # NOT an FK to ArtifactCacheEntry: see §E for why eviction forbids the FK.
    input_version_id = models.CharField(max_length=64)  # a produced RecordingJob.output_version_id
    produces_kind = models.CharField(max_length=64)  # annotation kind emitted (→ AnnotationKind)

    # Identity: (input_digest, image_digest, params). input_digest folds in the version digest
    # AND the digests of any annotation sets consumed (retention §1.1) — honest across the
    # signal→annotation boundary.
    input_digest = models.CharField(max_length=64)
    image_digest = models.CharField(max_length=128)  # detector container/model identity
    params = models.JSONField(default=dict)

    # Temporal-decomposition contract (analysis-execution §1) — segmentation is NOT in identity.
    grid_s = models.FloatField()
    halo_s = models.FloatField(default=0.0)
    max_event_span_s = models.FloatField(default=0.0)

    class Locality(models.TextChoices):
        WINDOW_INDEPENDENT = "window_independent"
        LOCAL_CONTEXT = "local_context"
        GLOBAL = "global"

    locality = models.CharField(max_length=20, choices=Locality.choices)

    reproducible = models.BooleanField(default=False)  # EARNED via conformance, not declared
    conformance = models.JSONField(default=dict)  # {mode: strict|tolerant, pass, corpus} — §5

    class State(models.TextChoices):
        QUEUED = "queued"
        RUNNING = "running"
        DONE = "done"
        FAILED = "failed"
        CANCELLED = "cancelled"

    state = models.CharField(max_length=10, choices=State.choices, default=State.QUEUED, db_index=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["input_digest", "image_digest"],  # params folded into input_digest
                name="analysisrun_unique_run_key",
            )
        ]
        indexes = [models.Index(fields=["recording", "input_version_id", "produces_kind"])]


class AnalysisSegment(models.Model):
    """Per-segment coverage — unioned on read, never a single mutable coverage row
    (analysis-execution §2, the contention-free commit path)."""

    run = models.ForeignKey(AnalysisRun, on_delete=models.CASCADE, related_name="segments")
    index = models.PositiveIntegerField()
    start_s = models.FloatField()
    end_s = models.FloatField()

    class State(models.TextChoices):
        PENDING = "pending"
        DONE = "done"
        FAILED = "failed"
        SKIPPED = "skipped"

    state = models.CharField(max_length=8, choices=State.choices, default=State.PENDING)
    skip_reason = models.CharField(max_length=64, blank=True, default="")  # why SKIPPED (empty otherwise)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["run", "index"], name="analysissegment_unique_index_per_run")]
```

**`SKIPPED` + `skip_reason`.** A segment is `SKIPPED` (not `DONE`, not `FAILED`) when the run
legitimately produced nothing for it and that is not an error — e.g. a segment that is entirely a
data gap / interruption, all-flat, or outside the recording's coverage. `skip_reason` records
which, so the union-on-read view can show "covered, nothing to report" distinctly from "not yet
run" (`PENDING`) and "errored" (`FAILED`). **Conformance caveat (analysis-execution §5):** a skip
is only legitimate when it is *invariant under resegmentation* — the same region must skip
regardless of grid, or the resegmentation oracle would see coverage change and must refuse to grant
`reproducible`. So a segment may be skipped only for reasons intrinsic to the signal region
(gap/flat/out-of-coverage), never for reasons tied to a particular segmentation. A skip that a
different grid would not reproduce is a bug, not a skip.

Produced annotations are `annotations.Event` rows (already hash-chained), linked to their
producing `(run, segment)` through a dedicated **`RunAnnotation`** table — resolving §Open #2 in
favour of the through-model over denormalised fields on `Event`:

```python
class RunAnnotation(models.Model):
    """Provenance edge: this AnalysisRun (at this segment) produced this annotation Event.
    Lives in compute so `annotations` stays producer-agnostic (retention §1.1) — annotations
    knows nothing about the pipeline; only machine-produced events get a link row."""

    run = models.ForeignKey(AnalysisRun, on_delete=models.CASCADE, related_name="annotation_links")
    event = models.ForeignKey("annotations.Event", on_delete=models.CASCADE, related_name="+")
    segment_index = models.PositiveIntegerField(null=True, blank=True)  # null = run-global (GLOBAL locality)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["run", "event"], name="runannotation_unique_event_per_run")]
        indexes = [models.Index(fields=["run", "segment_index"]), models.Index(fields=["event"])]
```

Four points make the through-model the right shape rather than just the simpler-looking one:

- **Dependency direction is the decisive reason.** `annotations` is the producer-agnostic
  foundation (a hypnogram may come from YASA, braindecode, or a human scorer — retention §1.1).
  Putting a `run_id`/`segment` FK on `annotations.Event` would invert that (annotations → compute)
  and give every human-authored event null pipeline columns. The through-model points *outward*
  from `compute` to both `AnalysisRun` and `annotations.Event`, so `annotations` stays clean and
  only machine-produced events carry a link.
- **The link is provenance, not the idempotency guard.** Because `annotations.Event` is
  hash-chained and append-only, the §3.2 fan-out cannot dedup by delete-and-replace on retry.
  Idempotency is the **coverage row's** job: a segment task, in one transaction, writes its Events,
  writes their `RunAnnotation` links, and flips its `AnalysisSegment` to `DONE`; a retry sees `DONE`
  and skips — no duplicate Events, chain intact. `RunAnnotation` just records the edges that
  committed segment produced.
- **Reference the run + a plain `segment_index` int, not the `AnalysisSegment` PK.**
  `AnalysisSegment` is rebuildable (re-fan-out), so its PKs are lifecycle-dependent; the *index* is
  contract-stable by construction. Linking by run + index decouples the edge from the segment row's
  lifecycle, so regenerating coverage rows never orphans links. `segment_index` is nullable for
  `GLOBAL`-locality runs that emit whole-signal annotations not tied to one segment.
- **`kind` is derived from `run.produces_kind`, not stored on the edge.** One run emits one kind,
  so duplicating it here is drift waiting to happen; the effective upsert key is `(run,
  segment_index)`. If runs ever go multi-kind, add `kind` back to the run and the edge together.

Both FKs `CASCADE`. In practice Events tombstone rather than hard-delete (the chain), so the event
cascade rarely fires — it is just the correct semantics for the rare real deletion; the run cascade
matters for subject-erasure and for a superseding rerun. Note this edge is *also* the forward index
for the re-fan-out invalidation of retention §1.1 ("rerunning sleep staging invalidates the
hypnogram's consumers"): `run.annotation_links` gives a run's annotations directly, and the consumer
direction stays via `input_digest` folding in consumed-set digests. It is distinct from the
version reference of §E — that is the annotation's `version_id` (§F) keeping *bytes* alive; this
keeps *provenance*. Two different edges; do not conflate them.

## D. `PipelineRunAudit` — one durable log, per-pipeline JSON payload

The durable, append-only run-event trail. **One dedicated table** in `compute`; each pipeline
inserts its own payload structure into `meta` (the point of review point 2) rather than a table
full of the other pipeline's null columns. Generalises the `DetectorRunAuditLog` of the `to_bids`
note; the external-egress case (handing PHI to an untrusted container) is just one `action`.

```python
class PipelineRunAudit(models.Model):
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    # target via GenericFK — Recording, ArtifactCacheEntry, or AnalysisRun
    target_content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE)
    target_object_id = models.CharField(max_length=255)

    class Action(models.TextChoices):
        RECONSTRUCT_RAN = "reconstruct.ran"  # a version CREATED (not the source MODIFIED)
        ANALYSIS_STARTED = "analysis.started"  # PHI materialised / run began
        ANALYSIS_FINISHED = "analysis.finished"  # annotation set CREATED; disposition known
        DETECTOR_EGRESS = "detector.egress"  # external-container exposure (to_bids note)
        ARTIFACT_EVICTED = "artifact.evicted"  # retention §2.4 tombstone trail

    action = models.CharField(max_length=32, choices=Action.choices)

    # Per-pipeline payload — NEVER clinical findings; NEVER unmasked PII. Reconstruction inserts
    # {manifest, version_id, code_versions}; analysis inserts {input_digest, image_digest,
    # run_id, disposition, failure_kind, queued_at, materialised_at, container_started_at,
    # container_finished_at, finalised_at → exposure_ms/container_ms}; eviction inserts
    # {version_id, policy, evicted_at}. Extensible without a migration.
    meta = models.JSONField(default=dict)

    created_at = models.DateTimeField(auto_now_add=True)
    # Hash-chained with the SAME primitives as activity.ObjectChangeLog (activity.audit) — one
    # verifiable integrity mechanism, a purpose-built log. Not a parallel root.
    prev_hash = models.CharField(max_length=64, blank=True, default="")
    content_hash = models.CharField(max_length=64, blank=True, default="")
```

**Integration decision (now settled, was §D-open / §Open #3): a dedicated chained table, not
folded into `ObjectChangeLog`.** This mirrors `federation.FederationAuditLog` exactly — a
purpose-built, long-retention, structured log that reuses `activity.audit`'s chaining primitives
so there is *one* integrity mechanism but a log shaped for its feature. `ObjectChangeLog` keeps its
role (the manifest/version CREATE events of signal-plan §3.4); `PipelineRunAudit` carries the
richer run lifecycle (egress exposure windows, disposition, conformance) that would bloat the
generic change log.

**Timeline visibility — the federation-consistent split (review point 2).** The question was
whether pipeline events should *also* appear on the "recent events" timeline, and if so how. The
answer follows `federation`'s existing pattern rather than inventing a new one: the dedicated log
stands alone, and for the *selected* events worth surfacing (a run finished, an artifact evicted)
the **service layer emits an explicit `log_activity` breadcrumb** — deliberately, per event — into
`activity.Activity`. No signal auto-mirroring both ways, and no multitable union query at read
time. This is the same shape `federation` uses: `FederationAuditLog` is the durable structured
record, and `federation/services.py` calls `log_activity` explicitly for the management ops worth
timelining. Two stores, one deliberate breadcrumb between them.

Keeping this from drifting from the read-side audit is a live concern (it is the same discipline
the local-recording-read vs federated-access logs already follow — a clean action↔verb mapping,
recording identity carried in every payload, and a consciously-chosen granularity per scope). The
detailed local-read ↔ federation-access mapping lives with the federation/activity audit notes, not
here; the rule this doc inherits from it is only: **when a pipeline event goes to both stores, use a
consistent verb vocabulary and carry the same identity in both**, so the dedicated log and the
timeline never tell divergent stories.

## E. Signal-derivative reference set — LOAD-BEARING

This is the invariant review point 3 asks to enumerate and pin. **A `(recording, version_id)`'s
reference count is computed by enumerating every model that can point at it. The GC and
subject-erasure both depend on this set being complete. Adding any new model that references a
version and NOT updating the eviction/purge rules is a correctness/compliance bug — a version
evicted while still referenced (orphaning an annotation) or PII left in the cache after erasure.**

Current reference providers (retention §2.2 made concrete against these models):

| Reference | From | Points at | Enforces |
|---|---|---|---|
| Annotation anchored to a version | `annotations.*` (via §F binding) | `(recording, version_id)` | not evicting bytes an annotation cites |
| Analysis run scored a version | `AnalysisRun.input_version_id` | version | provenance of a served detection |
| Derivative built from a version | `RecordingJob.input_digest` of a downstream stage | parent version | reconstruction-chain integrity |
| Explicit pin | `ArtifactCacheEntry.pinned` | version | operator "keep this" |
| Exported / cited `version_id` | export / report record (future) | version | a published id stays resolvable |
| Federation grant | `federation` grant on a version | version | a peer may still pull it |

The mechanism should be a **single registry of reference-providers** (a function per provider
returning the versions it references), so `references_to(recording, version_id)` and the GC
mark-phase iterate one list — and the load-bearing rule is: *adding a provider means adding it
here AND to the erasure purge sweep.* A doc-enforced checklist (like the `gdpr-compliance`
agent's inventory gate) is the right home for the rule.

**Why the run tables link by string, not ForeignKey.** `AnalysisRun.input_version_id`,
`RecordingJob.output_version_id`, and `ArtifactCacheEntry.version_id` are all plain `CharField`s
that meet on `(recording, version_id)`; there are deliberately **no FKs between the run tables**.
Two forces require this. First, there is no "versions" table to point an FK *at* — a version's
identity is the manifest hash in the `ObjectChangeLog` chain (§3.4), not a row — so the
content-addressed string *is* the join key by necessity. Second, `ArtifactCacheEntry` is the
evictable GC unit: if `AnalysisRun` (or an annotation) held an FK to it, eviction would force
either a cascade-delete of the durable run record — data loss — or a nulled link that loses
provenance. Linking by the content-addressed string means the durable record keeps naming the
exact version it scored *after* the bytes are evicted and the cache row is gone. Provenance
surviving eviction is the whole point of content-addressing, so string linkage here is the design,
not a shortcut. (Note the corollary from §B: the join must be on `(recording, version_id)`, never
`version_id` alone, because the `'source'` sentinel collides across recordings.)

This also means the reference *set* and the forward *join* are two different questions. The
`(recording, version_id)` key fully answers the join ("which cache entry holds the version this run
scored"). It does **not** by itself make the reference set a single `WHERE version_id = V` sweep,
because not every provider points by `version_id` — the reconstruction-chain parent points via
`input_digest` (a hash of consumed bytes; for `AnalysisRun`, a compound digest also folding in
consumed annotation sets). That is exactly why §E needs the provider registry with a resolver per
provider, rather than one indexed column.

## F. Version binding + derived status

- **Annotations bind to `(recording, version_id)` — implemented (`annotations` migration `0006`).**
  `AnnotationBase` gained a `version_id` field (default `'source'`), binding an annotation to the
  exact derivative it was made/scored on. Three decisions settled in the build: (a) `version_id`
  joins the per-target uniqueness key — `(target_content_type, target_object_id, version_id,
  object_hash)` — so the same `object_hash` can exist on `'source'` and on a derived version as
  distinct rows, and the `(target, version_id)` prefix indexes per-version queries; (b) `version_id`
  enters `content_hash` **only when non-source**, so `'source'` is the absence of a binding
  (mirroring the empty-manifest → `SOURCE_VERSION_ID` rule) and every pre-existing annotation hashes
  byte-identically — no rehash, no data migration; (c) the kind vocabulary is a standalone
  `annotations.AnnotationKind` token table, referenced by `AnalysisRun.produces_kind` as a string
  (not FK), shipped empty — token seeding is deferred and possibly project-specific (tracked in
  ROADMAP).
- **One versioned-recording view.** The clinician-facing status/timeline is a *union read* over
  `RecordingJob` (reconstruction progress → versions) and `AnalysisRun` (annotation sets per
  version), ordered in time — not two lists. `PROCESSING`/`AVAILABLE`/`READY` derives from the
  reconstruction rows exactly as §5 specifies; analysis rows add per-version annotation-set
  availability under it.

## Open questions / deferred

1. ~~**App placement for `AnalysisRun`/`AnalysisSegment`.**~~ **Resolved: `compute` owns all
   pipeline models** (`RecordingJob`, `ArtifactCacheEntry`, `AnalysisRun`, `AnalysisSegment`,
   `RunAnnotation`, `PipelineRunAudit`); `AnnotationKind` stays in `annotations`. `compute` depends
   on `recordings`/`annotations`, never the reverse.
2. ~~**Annotation ↔ run/segment link.**~~ **Resolved: the `RunAnnotation` through-model** (§C) —
   in `compute`, FK to `AnalysisRun` + `annotations.Event` + a nullable `segment_index`, keeping
   `annotations` producer-agnostic; idempotency is the coverage row's job, not the link's.
3. ~~**`PipelineRunAudit` integration.**~~ **Resolved: a dedicated chained table** (§D) reusing
   `activity.audit`'s primitives, plus explicit selective `log_activity` breadcrumbs for timeline
   visibility — the federation-consistent split.
4. **Subject-erasure into the audit trail** — review point 5: whether erasure should cascade into
   the audit log itself (preserve only actor/target/action, drop `meta`; and if the target then
   dangles, whether preserving anything is meaningful). This is **wider than these two models** —
   it changes the platform-wide audit/erasure contract (`activity` tombstoning, the manifest's
   permanence-vs-erasure carve-out of signal-plan §3.4). Flagged here, decided separately, before
   `PipelineRunAudit`'s retention is finalised.
5. **How a *manually* produced annotation set advertises its kind.** retention §1.1 says a kind
   dependency is satisfied by "any completed run — automated or manual". An automated producer
   advertises via `AnalysisRun.produces_kind`, but a human scorer is not an `AnalysisRun`, so a
   manually-scored hypnogram has no `produces_kind` to match against. Two candidate resolutions:
   tag the annotation set itself with an `AnnotationKind` (both automated and manual production then
   carry the kind), or have manual production create a lightweight run record. Deferred to the
   analysis-dispatch layer that first needs to resolve kind dependencies — noted here because it
   decides whether `AnnotationKind` must also attach to annotations, not only to runs.
