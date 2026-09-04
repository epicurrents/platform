# BIDS export (`to_bids`) — privacy, audit & GDPR design

**Status: design / threat model, pre-implementation.** `to_bids` materialises a
transient BIDS view of a recording to feed a detector container (see
[`szcore-bids-integration.md`](szcore-bids-integration.md) §4, §6). Because it
produces a copy of **recording-subject personal data** and hands it to
**untrusted third-party container code**, it sits on the same risk plane as
federated serving and must be designed against the platform's load-bearing
guarantees — audit trail, PHI de-identification, GDPR retention/erasure — *before*
any code. This note is the spec the `gdpr-compliance` and `phi-exposure` review
agents audit against; it is expected to iterate.

## Anchor: this is the federation-serving flow, pointed at a local container

The platform already de-identifies a recording when it leaves the trust boundary
to a federated peer: anonymised EDF header + stripped annotation text,
"de-identified by default" (`docs/gdpr-compliance.md` → processor flows;
`federation/README.md` → middleware). A detector-container export is the **same
operation with a different consumer** — a local container instead of a remote
peer. So the governing rule is: **reuse the federation/ingest de-identification
boundary; do not invent a second PHI path.** Everything below follows from that.

Two facts from the codebase make it tractable:

1. **The stored file is already header-anonymised.** Ingest rewrites the EDF
   header in place during processing (`recordings/preservation.py` preserves the
   raw upload precisely because "processing rewrites the file in place for header
   anonymisation"). `to_bids` therefore sources the **processed** recording
   (`Recording.file_path`), which is already de-identified — never the raw upload.
2. **The originals volume is write-only.** No code reads `RECORDINGS_ORIGINALS_PATH`.
   `to_bids` must not be the first — it never touches raw originals.

## 1. Personal-data surfaces of the conversion (what can leak)

Every place PHI could enter the BIDS tree, and the rule for each:

| Surface | Risk | Rule |
|---|---|---|
| **EDF header** (patient name / id / birthdate / recording date) | The classic leak — a raw EDF header is full PHI | Source the already-anonymised stored file; **assert** the header is scrubbed before writing; never re-inject from `original_name`/metadata. |
| **`sub-<label>`** in paths/filenames | BIDS bakes a subject id into every path | Pseudonym = `stored_name[:8].upper()`, the platform's existing public handle (the `display_name` fallback); uppercased-alphanumeric, a valid BIDS label as-is (assert `^[A-Za-z0-9]+$`). **Never** `original_name` or any clinical id. |
| **`participants.tsv`** | Canonical BIDS PHI sink (age, sex, id) | Omit it, or emit a single pseudonymous row with no direct identifiers and no birth date. No demographics today; a future detector needing age gets a minimisation-preserving hook (a coarse band, not a birthdate) — a new personal-data flow that re-enters this design and the GDPR inventory, never a silent raw field. |
| **`_scans.tsv` / `_eeg.json` acquisition time** | Reintroduces the recording date | Omit `acq_time`; `recording_date` is already nulled after de-id — do not read it back in. |
| **`_events.tsv` annotation text** | Clinical free-text annotations are PHI | Do **not** export existing annotation *content*. For a detector the input events are empty anyway (the container *produces* events). Mirror federation's "stripped annotation text". |
| **`_channels.tsv`** | Low — channel labels/types/units | Safe; carries no subject PHI. Sourced from `SignalInfo.canonical_label` (the non-destructive normalised name), not the raw header label — see SzCORE-note *Input-normalisation prerequisites*. |
| **The pseudonym→recording map** | Re-identifies the subject | Stays backend-side and audited; **never** written into the tempdir or visible to the container. |

## 2. De-identification boundary (reuse, single point)

- **One boundary.** Route through the existing ingest/federation de-id, not a new
  implementation. The stored file is the de-identified artefact; `to_bids` is an
  *assembler* of already-clean bytes, not a de-identifier.
- **De-identified by default, no opt-out.** Federation allows a raw
  `--no-apply-middleware` egress to a *trusted peer under a DPA*. A container is
  untrusted third-party code with no DPA — there is **no** raw path. Ever.
- **Pseudonymised, not anonymised.** The output is re-identifiable via the backend
  map, so it **remains personal data under GDPR** — an Art. 32 safeguard, not an
  exemption. Document it as such; do not claim anonymisation.
- **Assert, don't assume.** A `phi-exposure`-style check verifies the emitted
  header/paths carry no identifiers — a failed assertion aborts the run rather
  than shipping a leak.

## 3. Ephemerality & retention

- **Tempdir only, guaranteed cleanup.** The BIDS tree lives solely in a tempdir,
  removed in a `finally` / context manager that fires on success, exception,
  timeout, and cancellation. A crashed run leaves nothing on disk.
- **Never durable, never backed up.** No BIDS artefact is written to the store,
  media volumes, or anything borgmatic snapshots. The tempdir root is outside
  backup scope.
- **Orphan sweeper.** Belt-and-braces max-age sweep of the tempdir root (mirroring
  the recordings orphan reaper), so a killed process can't strand PHI. This is the
  same **daily reaper** that reconciles abandoned runs (§4) — one reaper handling
  both the stray tempdir and the stray operational row, not two.
- **Derived-output retention** (these *do* persist and need inventory entries):
  detector `Event` rows follow `annotations.*` retention (life of target; author
  + target-object cascade on purge); the per-sample probability-trace cache is a
  **new personal-data store** → must be added to the GDPR Data inventory, keyed to
  the recording, and **cascade-deleted on recording purge**.

## 4. Audit trail

- **Model writes auto-audit.** Creating `Event`/cache rows writes tamper-evident
  `ObjectChangeLog` entries via the existing signals (masked fields excluded).
- **The processing act needs an explicit record.** Materialising PHI to a tempdir
  and handing it to a container is *not* a model change, so it won't auto-log. It
  is a data-egress-to-external-code event — the direct analogue of
  `FederationAuditLog`. Record, per run: actor (system detector identity),
  recording + **`input_digest`** (exact bytes scored), detector **image digest**,
  params, pseudonym used, lifecycle timestamps, and the run disposition (below).
  **Decided: a dedicated `DetectorRunAuditLog`** mirroring `FederationAuditLog`,
  not `activity.Activity` — this is an egress-to-untrusted-code event and deserves
  the same first-class, durable trail federation gets.
- **Two append-only entries, correlated by `run_id` — never a mutated one.** The
  exposure spans time, so it takes two records: a `DetectorRunStarted` written the
  moment PHI is *materialised* for the container (the exposure has begun), and a
  `DetectorRunFinished` written at completion, carrying the outcome. They share a
  `run_id`; the second does **not** edit the first. This matters because the log is
  a per-shard hash chain — resolving a run by rewriting its opening entry would
  break the seal, and a second run's entry may legitimately land *between* the two
  (chain integrity depends on append-order, not on the pair being adjacent, so the
  interleave is harmless). A crash / kill / timeout after the first entry leaves a
  `DetectorRunStarted` with no `DetectorRunFinished` — which is exactly the signal
  we want: the exposure happened and its disposition is unknown, never erased.
- **Record the disposition, not the answer.** The `DetectorRunFinished` entry
  carries an `outcome` flag — `completed` / `failed` / `cancelled` / `timed_out` —
  plus, on failure, the `failure_kind` (`availability` | `computational`) from
  SzCORE-note §6. Do **not** put the detector's findings (events, counts,
  probabilities) in the audit log: it is **permanent** (unlike the results, which
  cascade out on recording purge), so clinical output there would create a permanent
  health-data store *outside* the erasure cascade — exactly what the audit-field
  masking exists to prevent. The findings live with the `Event` rows and die with
  them; the audit log records only that a run happened, against which bytes/image,
  and how it ended.
- **Timing lives in a free-form metadata JSON column, not fixed timestamp columns.**
  The entries carry lifecycle timestamps — `queued_at`, `materialised_at`,
  `container_started_at`, `container_finished_at`, `finalised_at` — in an extensible
  `metadata` JSON field rather than a rigid schema, because future stages will add
  timestamps we can't enumerate now (a fixed column set would force a migration per
  addition). From these we derive two figures that answer different questions:
  **`exposure_ms`** (`finalised_at − materialised_at`) — the compliance number, how
  long untrusted code held the PHI — and **`container_ms`**
  (`container_finished_at − container_started_at`) — the compute cost, distinct
  because a run can sit queued before the container starts. The runner is the
  **authoritative source** for the two container timestamps (it owns the container
  lifecycle; see SzCORE-note §6 runner API); the backend stamps the rest. Wall-clock
  values are for the audit record; if a duration is ever used for billing or SLA it
  should come from a monotonic clock, not the difference of two wall-clock stamps.
- **Not the same object as the ledger.** The `RecordingJob` row is *live
  operational status* for the UI (rebuildable, ephemeral); `DetectorRunAuditLog` is
  the *durable compliance record* of the exposure + disposition. The `outcome`
  appears in both, for different reasons — don't collapse them.
- **Abandoned runs are reconciled, never deleted.** A `DetectorRunStarted` with no
  `DetectorRunFinished` past a generous cutoff (24 h — far beyond any real detector
  runtime, so a gap that old is reliably abandoned, not in-flight) is closed by a
  **daily reaper** that *appends* a `DetectorRunFinished` with `outcome=timed_out`.
  It must **not** delete the `Started` — that entry is the permanent, append-only
  exposure record, and deleting it is exactly what the two-entry model forbids. The
  reaper deletes only the *operational* residue: the live-status ledger row and any
  stranded tempdir (the §3 sweep — same reaper). So the compliance chain only ever
  grows; the ephemeral state is what gets cleaned.
- **Traceability.** The recorded `(input_digest, image_digest, params)` lets any
  served annotation be traced to the exact model and the exact bytes — defensible
  under scrutiny, and the cache key from §6 of the SzCORE note.

## 5. GDPR inventory obligations (enforced — ships in the same commit)

The `gdpr-compliance` review agent **blocks** commits that add a personal-data
model or an outbound flow without updating `docs/gdpr-compliance.md`. So building
`to_bids` + the detector requires, in the same change:

- **Processor / cross-controller flows table:** a row for the detector-container
  flow. Destination: local detector container (untrusted third-party code).
  Role: **internal processing on operator infra** — see *The DPA question* below.
  Data: de-identified (pseudonymised) EEG, no annotation text. Safeguard:
  pseudonymisation + `--network=none` + ephemeral tempdir + digest-pin + audit log.

### The DPA question

**A local, offline container needs no DPA — and the isolation is *why*.** A DPA
(GDPR Art. 28) is required when a controller (the operator) hands personal data to
a **processor**: a separate party processing it on the controller's behalf. The
pivot is whether anyone *receives* the data. Under `--network=none` on the
operator's own infrastructure, nobody does — the image author supplied **code, not
a service**, and the data never leaves the operator's control. That is the operator
processing their own data with a tool (like any library or binary), so **no Art. 28
relationship exists and no DPA is required**; the image provider is not a processor
because they receive nothing.

The classification is **contingent on the isolation holding.** The moment a
detector phones home — calls the author's cloud, ships features to an API — the
author becomes a genuine processor (DPA required) and, if outside the EEA, this
becomes an international transfer (Art. 44+, SCCs). So `--network=none` is not only
security hardening; it is the **compliance control that keeps this in the "local
tool" category.** Never give a detector network access without redoing this
analysis.

Operator-side nuances (governance, not strict law, and therefore the operator's
paperwork per `docs/gdpr-compliance.md`): if the operator is themselves a processor
for a health authority, their *own* DPA may require notifying that controller of a
new processing tool — even though the image author is still not a sub-processor.
And keeping an internal vendor/provenance record for third-party code that touches
PHI in-process (image digest, licence, a short risk review) is prudent supply-chain
hygiene — the digest-pinned inventory row is the hook for it. *Not legal advice; the
operator's DPO makes the determination.*
- **Data inventory table:** a row for the probability-trace cache (derived PHI;
  retention = life of recording; erasure = cascade on purge).
- **`recordings/README.md`** de-id section if `to_bids` is treated as a new de-id
  surface (it should be treated as a *consumer* of the existing one, not a new one
  — note that explicitly to keep the boundary single).

## 6. Erasure reachability (Art. 17)

- Detector `Event` rows and the probability cache must be reachable by the
  recording purge cascade — `annotations.*` already cascade via the `Recording`
  GenericRelations; the cache must be keyed to the recording with the same cascade.
- No BIDS artefact survives a run, so there is nothing extra to erase on the export
  side. The known gap "patient-side audit snapshots persist after purge" applies
  to the new `Event` `ObjectChangeLog` snapshots exactly as it does to existing
  annotations — **not a new gap**, folded into the planned purge-time tombstoning.

## 7. Load-bearing tests (must exist before shipping)

- **PHI-leak golden test:** emitted EDF header has no name/id/birthdate/date;
  `sub-` label is `stored_name[:8].upper()` (matches `^[A-Za-z0-9]+$`), not
  `original_name`; no `participants.tsv` identifiers; `_events.tsv` carries no
  free-text annotation content.
- **Cleanup test:** tempdir removed on success **and** on raised exception /
  simulated timeout; the daily reaper removes an aged stray tempdir.
- **Audit test:** a run emits both audit entries (`DetectorRunStarted` /
  `DetectorRunFinished`, correlated by `run_id`) + ledger row carrying
  `input_digest`; no PHI in the audit metadata (hashed/masked only). An abandoned
  run (`Started`, no `Finished`, aged past cutoff) is reconciled by the reaper
  *appending* a `timed_out` `Finished`, and the `Started` is **still present**.
- **De-id-source test:** `to_bids` reads the processed file, and a raw-header
  fixture is rejected by the assertion rather than exported.
- **Format-gate test:** a BDF-`RecordingMeta.format` fixture returns **SKIPPED**
  with a reason, never a partial/transcoded export.
- **Channel-normalisation test:** an old-nomenclature fixture (`EEG T3-Ref`, `A1`,
  plus an `EKG` extra) yields `_channels.tsv` with canonical 10-10 names
  (`T7`, `M1`) and the non-EEG extra dropped — driven by `SignalInfo.canonical_label`,
  with the raw `label` untouched.
- **Native BIDS-structure check:** a small in-repo validator asserting the SzCORE
  profile (required sidecars, entity shape, `PowerLineFrequency` present or `n/a`)
  — **no Node `bids-validator` dependency**.

These join the `phi-exposure` per-commit gate and the six-monthly four-lens GDPR
sweep.

## What to build — only after this design is reviewed

0. **Recording-model prerequisites** (shared platform changes, not `to_bids`-local;
   see SzCORE-note *Input-normalisation prerequisites*): `SignalInfo.canonical_label`
   + ingest normaliser; `EEG_MAINS_HZ` setting + nullable `Recording.power_line_frequency`
   override + batch-apply endpoint. `to_bids` consumes these; so do SpikeNet/YASA/forward.
1. **Dependency-light `to_bids` core** — assembles a deterministic SzCORE-profile
   BIDS tree from already-de-identified bytes + structured metadata; `sub-` =
   `stored_name[:8].upper()`; channels from `canonical_label` (select + drop extras);
   `PowerLineFrequency` from the resolver; no annotation text; the header-scrub
   assertion; EDF-only (SKIPPED on BDF). No Django, unit-tested against the in-repo
   structure check and the PHI-leak golden test.
2. **Django wrapper** — sources `Recording.file_path` (processed, de-identified),
   resolves the pseudonym map (backend-only), **hardlinks** the signal into a
   guaranteed-cleanup tempdir (copy if cross-FS), emits the `DetectorRunStarted` /
   `DetectorRunFinished` audit entries + `input_digest`.
3. **Inventory + README updates** in the same commit (§5) — or the `gdpr-compliance`
   agent blocks it, by design.
4. **The §7 tests** as ship-blockers.
5. **Daily reaper** — reconciles abandoned runs (append `timed_out` `Finished`) and
   sweeps stray tempdirs (§3/§4), one reaper for both.

Build step 0 — the input-normalisation prerequisites — has shipped; steps 1-5
are not built. The decisions are now settled: §4 — a dedicated
`DetectorRunAuditLog` written as **two append-only entries** (`DetectorRunStarted` /
`DetectorRunFinished`) correlated by `run_id`, recording the run **disposition**
(outcome + failure-kind) and **lifecycle timestamps in a free-form `metadata` JSON
column** (deriving `exposure_ms` / `container_ms`), never the clinical findings, and
reconciled-not-deleted when abandoned; §5 — a local offline container needs no DPA,
the `--network=none` isolation being the control that keeps it a "local tool", with
the operator's own sub-processor-notification / vendor-record hygiene left to their
governance. Input handling is settled too: non-destructive `canonical_label`
normalisation, two-level mains resolution (`EEG_MAINS_HZ` ← `Recording.power_line_frequency`),
`sub-` reuse of the public handle, EDF-only with SKIPPED-on-BDF, and a native
BIDS-structure check. Build order above; prerequisites (step 0) first.
