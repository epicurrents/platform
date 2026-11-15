# Composite module — specification plan

**Status:** started 2026-11-14; every point decided 2026-11-15 and amended the same day after an adversarial review, every item of which is settled. The next step is the composite module's specification, into which these decisions move.

The [composite study document](composite-study-document.md) fixes the architecture: the modality, the parts, the default child, derived children bound to the parts' measurements. It is not yet enough to build a module from. This plan takes the missing pieces one at a time. Each point records the decisions as they are made; once a point is decided, its result is written into the module's own specification, which lives in the composite module's repository and is linked from the design note.

Facts already established that every point can rely on:

- Core's `DataResource` carries `childResources` and `activeChildResource`, so a composite resource needs nothing new from core, and `defaultChild` is its initial `activeChildResource`.
- The viewer's packages split file parsing (`*-reader`) from resources (`*-module`); a study module depends on core alone.
- The tab module is unpublished, and its subcontexts are to be retired in favour of cell bindings (its ROADMAP records the surface that goes).

## Points

| # | Point | Blocks | Status |
|---|---|---|---|
| 1 | Manifest schema and fixtures | 2, 3, 5 | decided |
| 2 | Reader and module split, sweep loading | 3, 4 | decided |
| 3 | Part to NCS and EMG resource mapping | 4 | decided |
| 4 | Interfaces: composite resource, child builders, measures, cell bindings | first slice | decided |
| 5 | The vocabulary files | first slice | decided; marker and measure terms defined in point 6 |
| 6 | Measurement definitions | measures and bindings | decided |
| 7 | Marker and distance persistence | saving a reader's work | decided |
| 8 | Platform side: modality, validating pass, serving, assessment | storing a composite study | decided |
| 9 | Reference value generation | reference sets from annotated datasets | decided |

Point 9 follows 7 and 8, since it reads saved work across a dataset on the platform. Points 1 to 5 are what the first slice needs: a reader, the composite resource, the parts as plain resources without measurements, the child-builder registry and the default child. Points 6 and 7 add measurements and bindings; point 8 can run in parallel with any of them.

### 1. Manifest schema and fixtures

Decide, in order:

- **1a. Where the schema lives.** The viewer and the platform both validate a manifest. The proposal mirrors the core vocabularies: the composite module's repository holds the canonical JSON Schema, and the platform pins a copy by version and digest.
- **1b. The envelope.** `format`, `version`, `modality`, `kind`, `defaultChild`, `vocabulary`, `parts`, and the converter-only `sourceNames`. Which are required, and how a reader treats a `version` it does not know.
- **1c. Ids.** Whether part and sweep ids are unique within the document (a binding names a sweep, so sweep ids probably are), and whether they carry any meaning.
- **1d. Part fields.** `id`, `modality`, `test`, `side`, `nerve` or `muscle`, `sweeps`, part-level `seed`.
- **1e. Sweep fields per test.** Motor; sensory odd and even halves; F-wave and H-reflex series; EMG saves; what is common and what is per test.
- **1f. `seed`.** The shape of marker latencies on a sweep and of segment distances on a part. Marker terms themselves are decided in point 5.
- **1g. `sourceNames`.** How a converter's unresolved names are referenced from parts and sweeps, and how ingest removes them.
- **1h. Fixtures.** One synthetic document per test type, generated rather than taken from a real examination.

### 2. Reader and module split, sweep loading

- Whether a composite reader package parses the file and the module builds resources from it, or the module parses it itself.
- Whether sweeps load with the document or per part on demand, by byte range; and whether a worker is involved.

### 3. Part to NCS and EMG resource mapping

- The template an NCS part is turned into: channels per sweep, sites from the sweeps' stimulation points, segments from consecutive sites, the split pair.
- What the EMG module expects of an EMG save, and whether a part with several saves is one resource or several.
- How a series (F-wave, H-reflex) maps onto the NCS resource's channels.

### 4. Interfaces

- The composite resource, as a `DataResource` with its parts and derived children.
- The child-builder registry: how a `kind` registers a builder, what the builder receives and returns, and how its default child is named.
- The measure interface a part implements for the tab module to resolve bindings through, including the change event.
- The cell binding type, and how it replaces `subcontext` in core's table types.

### 5. The vocabulary file

- `epicurrents.edx` as JSON in the viewer's vocabulary format, with sides, nerves, sites, muscles, test types, derived-child names and marker terms.
- Its pinned copy in the platform's vocabulary directory.

### 6. Measurement definitions

Decide, in order:

- **6a. Common conventions.** Units, the baseline, how a marker position becomes a sample, signs and polarity, what makes a measure unavailable.
- **6b. Placement.** Whether the module places markers itself where no seed or reader has, and if so by what rule; what a reader's edit does to measures depending on it.
- **6c. Motor conduction.** Markers, the response measures (latencies, amplitudes, durations, areas) and the segment measures (conduction velocity, amplitude and area change, dispersion).
- **6d. Sensory conduction.** Markers, including whether an initial positive peak is one; onset and peak velocities; the pair mean.
- **6e. F-wave.** Markers per sweep and for the M response; persistence and what counts as a present F wave; minimum, mean and maximum latency, chronodispersion, amplitudes, the F/M ratio.
- **6f. H-reflex.** M and H markers; the recruitment series; maximum amplitudes and the H/M ratio.
- **6g. Distances and links.** A response's stimulation-to-recording distance and a segment's; distances derived from offsets on one landmark; which refs the composite links automatically and which only the reader does (4d).
- **6h. EMG saves and MUP templates.** Which measures a save has, if any (interference pattern analysis), and a template's (amplitude, duration, phases, turns, area, size index); the firing-rate question left open in 1e.
- **6i. Reference limits.** Whether deviation from reference values belongs to this point or later, given that a regression limit needs the subject's age.

Repetitive stimulation, single-fibre EMG and the evoked potentials are specified when those tests are built.

### 7. Persistence

How markers, distances and findings persist as labels carrying codes of confined vocabularies (design note section 8), and how a seed relates to a saved marker.

### 8. Platform side

- **8a. Modality.** `composite` in `RECORDING_MODALITIES`, declared by the converter's output and never inferred by the channel vote.
- **8b. The validating pass.** Parsing, strict validation against the pinned schema and vocabularies, source-name resolution (1g), the canonical rewrite, and the pass's own version.
- **8c. The mapping state.** The author-private waiting state, the upload page's mapping step, and name maps configured like channel maps.
- **8d. Serving.** A pass-through entry in the serve pipeline, range requests, and the offload interlock kept on `apply_middleware`.
- **8e. Vocabularies on the platform.** `epicurrents.edx.work` and `epicurrents.edx.finding` registered as confined, the pinned copies, and the composite write gate (point 7).
- **8f. The assessment.** Given-data rows, the construction claim with its basis, and the residual covert channel.
- **8g. Linked imports and the lifecycle.** `--link` against the canonical rewrite, no lifecycle stages at first, purge and erasure.
- **8h. Subject covariates.** Age, sex, height and later weight, stored beside the recording for reference values.

### 9. Reference value generation

Generating reference sets from the markers readers place in the viewer and the exact covariates of 8h:

- **9a. Computation.** Which code computes the measures and the statistics, where, and when.
- **9b. The reference population.** Where the studies a set is computed from come from.
- **9c. Annotation protocol and blinding.** What a reader sees while marking work a reference set will be computed from.
- **9d. Annotator layers.** One annotator, each separately, or a combination.
- **9e. Statistics.** Per cell (test, nerve, sites, technique, measure): declared transforms and covariates, regression with prediction limits, percentiles, outliers reported rather than excluded, no extrapolation beyond the covariates' range.
- **9f. Release and distribution.** Aggregates only, a minimum number of examinations per cell, the release gate of a pooled dataset, provenance (6i), and reference sets as keyed, versioned platform objects with sharing rules that the viewer loads.

## Build obligations

Found by the privacy reviews of the finished plan (2026-11-15) and binding on the build; none changes a decision, each is a place the implementation must not miss.

- **Part index.** Parts are addressed by recording hash and part id, never a sequential key; the listing applies the release gate's rules to every key it serves (no sequential id to a non-manager, the release month for a released pooled member), and its keys join the grantee-visible shape test.
- **Extraction.** The part file endpoint calls `_hidden_for_caller` with the share token, serves through `_build_serve_pipeline`, and joins the parity test's request shapes. An extract marks itself a subset and names no source recording, its hash included, since a file travels beyond the access it was served under.
- **Every byte path dispatches on modality**, the FUSE mount included, so no composite file passes through an EDF middleware by its failing open.
- **Work labels** derive `object_hash` from the recording's `stored_name`, never a primary key.
- **The annotation-text rule** in AGENTS.md and the assessment row say that `edx.work` codes carry readings — marker positions and distances — to de-identifying recipients, a confined vocabulary admitting readings by design, and that `edx.finding` codes are withheld as text is. The readings flag on a vocabulary registration changes [annotations/redaction.py](../../annotations/redaction.py), a load-bearing file: its contract test gains the finding cases, own-row exemption included, on every surface the rule covers.
- **Audit verbs** for every new write: covariates, the per-dataset reference consent and the manager's part selection (field names and ids only, never values), reference-set share, delete and export, the mapping endpoints (counts only). A refused work code puts no value in the security log or an activity row.
- **Registrations.** `SubjectCovariates` and `CompositeMapping` take exemption entries as `original_name` does: patient data, a cascade from the recording, no user key. A `ReferenceSet` key to a user has a decided `on_delete`, `register_subject_pii` and `register_export_relation`, never `SET_NULL` by default; a run's provenance names annotators by opaque id only. Personal name maps, if ever saved on the platform, take erasure and export registration and cascade with their user.
- **Rollback** never restores a withheld field of `SubjectCovariates` or `CompositeMapping`; a guard is registered if the generic path would.
- **Hidden with their recording.** Covariate, mapping and part rows follow the FAILED-hiding and trash rules of their recording on every surface.
- **The part index's digest-only audit** is documented inline on the writing site, with how the audit signals skip those rows.
- **Reference sets.** A set's key and labels are grantee-visible free text and return `name_warnings`; an exported set names its dataset by `object_hash`, never by name. Calling an aggregate set fit for release is a disclosure-control threshold, not an anonymity finding, which only a deployment's contextual assessment may make. A set outlives the recordings and accounts behind it because, past the cell minimum and the rounded ranges, it holds aggregates rather than any one person's data; the exemption entry states that rationale, and a set computed from an erased or purged member is marked stale (9f).
- **Inventories.** [docs/gdpr-compliance.md](../gdpr-compliance.md) gains rows for `SubjectCovariates` (health data), `CompositeMapping`, `CompositePart`, `ReferenceSet` and the work labels, and states that exact covariates reach no federated peer and that an exported set is a manual transfer.
- **E-norms** (9b's later direction) takes its own assessment before it is built.
- **Settings.** `COMPOSITE_WORK_SHARE_TOKEN_WRITES` is read with `env_bool`, default off.
- **The submission gate** accepts covariate bands in the widths the pool's profile declares and refuses an exact value (8h); the bands join the class a release selector sizes.
- **The blind protocol's withholding** (9c) is applied where annotation rows are serialised, the time-slice and bundle surfaces included, and scoped by `controls_membership`, so a third party's dataset hides nobody's work.
- **Member sets are scrubbed on delete.** Purge, erasure, withdrawal and veto replace the member in every run's provenance with a count (9f).
- **Reference generation reads samples** through the checks every byte reader makes, `linked_file_change` among them.
- **The compute README's admission rule** gains its third reason: computation that would run in the browser but must run where data the browser may not hold is.
- **Work is saved in bulk**, one request and one transaction per save (point 7), never one request per marker moved.

## Decisions

Decisions are recorded here as they are made, with their date, until they move into the module's specification.

**1a. Where the schema lives** (2026-11-14). The composite module's repository holds the canonical JSON Schema; the platform pins a copy by version and digest, as it does the core vocabularies. A change is made in the module first and copied across with the pin moved, never edited in the copy.

**1b. The envelope** (2026-11-14).

| Field | Required | Rule |
|---|---|---|
| `format` | yes | The constant `epicurrents.composite`. |
| `version` | yes | Version of the file format, independent of the vocabulary version. |
| `modality` | yes | The constant `composite`, so the platform's ingest check stays a comparison. |
| `kind` | no | Selects the child builder. |
| `defaultChild` | no | A part id or a derived-child term of the `kind`. |
| `vocabulary` | yes | A list of `{ standard, version }`, one per governing vocabulary: `epicurrents.anatomy` and `epicurrents.edx` (point 5). |
| `parts` | yes | At least one part. |
| `sourceNames` | converter output only | Refused in a stored file. |
| `requires` | no | Feature names a reader must understand to read the file correctly (`int16`, later `recording_side`). A reader refuses a file naming one it does not know. |
| `subset` | extracts only | `true` on a part extract (8d); refused at ingest. |

Validation is strict at ingest and lenient in a reader. Ingest refuses an unknown key and rewrites the manifest canonically. A reader refuses a major `version` it does not know and a `requires` entry it does not know, and ignores unknown optional fields otherwise, so an older viewer opens a file that gained an optional field and never misreads one in which a new field changes what an old one means.

**1c. Ids** (2026-11-14). A part holds data elements, and format version 1 defines one element kind, the sweep; nearly everything an electrodiagnostic examination records is one, but a later test type may carry data of another shape, so the id scheme and the part's layout are not closed to it.

- Every id is a kind prefix followed by a sequence number: `p` for parts, `s` for sweeps, `^p[1-9][0-9]*$` and `^s[1-9][0-9]*$`. A new element kind is a new prefix, registered in the schema with its own array on the part, and leaves existing ids and files valid.
- Each kind is numbered in document order across the whole document, not per part, and ingest refuses a file whose ids are not consecutive. A converter assigns them in the order it writes the elements.
- Ids are unique across the document, so a saved marker or `defaultChild` names an element without a path. A binding names a part and a protocol step instead, so that it follows the selected trial (carried into 1e).
- An id carries no meaning beyond the order the document already has, and the pattern keeps it from carrying text the vocabulary check would not see. It also keeps a part id from colliding with a derived-child term in `defaultChild`.
- A stored file is never rewritten, so its ids are stable and a saved marker can refer to its element for good.
- Segments carry no id; a segment distance names its two steps (1f): a step and not a site, since one site can be stimulated more than once in a part, and not a sweep, so that the distance holds for whichever trial is selected.

**1d. Part fields** (2026-11-14).

| Field | Required | Rule |
|---|---|---|
| `id` | yes | `p<n>` (1c). |
| `modality` | yes | `ncs` or `emg`, later `ep`; agrees with `test` per the design note's test-type table. |
| `test` | yes | A vocabulary term. |
| `side` | yes | `left`, `right` or `midline`. `midline` is for a structure with no side, such as the tongue, never a default for an unrecorded one. |
| `nerve` | per test | Required for the conduction tests (`motor`, `sensory`, `fwave`, `hreflex`, `rns`); absent for `emg`. |
| `muscle` | per test | Required for `emg`; absent otherwise. A motor part's recording muscle is the sweeps' `recording`. |
| `sweeps` | yes | At least one, in acquisition order. A converter drops an empty protocol folder rather than writing an empty part, and ingest refuses an empty part. |
| `seed` | no | Segment distances (1f). |

Nothing else sits on a part: no label, which comes from the terms, and no free text. Limb temperature is a seed where a source records it (1f) and otherwise a reader's input (6g), neither export of the first vendor carrying it.

**1e. Sweep fields** (2026-11-14).

A sweep's role in its part is three fields, and ingest refuses two sweeps of one part with the same three:

| Field | Required | Rule |
|---|---|---|
| `step` | stimulated sweeps | Protocol step within the part, counting from 1 in acquisition order. Required on a stimulated sweep, since distances and segments name steps (1f); an EMG save or a MUP template without one is its own step. |
| `trial` | no | Trial within the step, counting from 1; default 1. |
| `split` | sensory | `odd` or `even` where the device stores split averages; absent otherwise. |

Selection is per step: the device's chosen trial is a seed naming a trial number (1f), and a sensory pair is selected whole. A series (F-wave, H-reflex) has one sweep per step and its measurements use every step.

Every sweep:

| Field | Required | Rule |
|---|---|---|
| `id` | yes | `s<n>` (1c). |
| `samplingRate` | yes | Hz. |
| `data` | yes | `{ offset, samples }`, and optionally `encoding` and `scale`. Samples are float32 microvolts by default; `encoding: "int16"` with `scale` in microvolts per count halves a block's size, which matters for long EMG saves (about 0.18 µV a count at a ±6 mV range). A file using it names `int16` in `requires` (1b), so a reader knowing only float32 refuses it and never reads the block as floats. |
| `filters` | no | `{ highpass, lowpass }` in Hz, `notch` a boolean. |
| `seed` | no | Marker latencies (1f). |

Stimulated sweeps (every test but `emg` and `mup`):

| Field | Required | Rule |
|---|---|---|
| `stimulation` | yes | Site term. |
| `recording` | yes | Site or muscle term. |
| `intensity` | no | Amperes. Absent when the export does not carry it or the device reports none. |
| `pulseWidth` | no | Seconds. |
| `averages` | yes | Integer, at least 1. |
| `triggerOffset` | yes | Sample index of the stimulus. |
| `technique` | no | `orthodromic` or `antidromic`, on a sensory sweep, where the source says so; absent is unknown. Reference values differ by technique, and inferring it from the positions would need the anatomical ordering point 5 leaves out. |

A position (`stimulation`, `recording`) is a term, or an object placing a point relative to one (2026-11-15):

```json
"stimulation": { "site": "elbow", "offset": { "direction": "distal", "mm": 50 } }
```

A plain term is shorthand for `{ "site": … }`. `site` may hold a source-name reference (1g); `direction` is an anatomical relation term (point 5) and `mm` a positive number, so an offset adds no text. One offset per position in version 1; two axes are a later minor version as a list. The conventional relative terms (`below_elbow`, `above_knee`) stay terms, being what labs record and reference values are tied to; the offset is for positions measured more precisely, an inching study's 1 cm steps across the carpal tunnel being the case that needs it. Whether the offset difference of two positions on one landmark fills a segment distance is settled with the measurements (point 6).

EMG saves (`emg`) carry no stimulation fields and no `triggerOffset`; a save is free-running and its length is `data.samples`.

Motor unit potentials (`mup`, part modality `emg`, on a `muscle` and `side`) are the templates an automatic or multi-MUP analysis averages from collected discharges. Each MUP is a sweep and its own step:

| Field | Required | Rule |
|---|---|---|
| `averages` | yes | Number of discharges averaged into the template. |
| `triggerOffset` | yes | Sample index of the alignment point the device averaged on. |
| `source` | no | Id of the EMG save the MUP was extracted from, when the document carries that save. |

Two rules hold for every sweep. One channel per sweep: a protocol recording two channels at once writes two sweeps sharing `step` and `trial` with different `recording`. Samples are stored as recorded, negative meaning the active electrode negative to the reference; negative-up is the viewer's display convention, and a converter checks each vendor's sign against a printout.

Single-fibre EMG (`sfemg`, part modality `emg`, on a `muscle` and `side`, concentric-needle recordings included) is a test of its own rather than a kind of `mup`: jitter is measured from the individual consecutive discharges of a fibre pair, which averaging would destroy, and its measures (inter-potential interval, mean consecutive difference, blocking, fibre density) are not the template's. Each step is a fibre pair, or a single fibre when stimulated; each trial is one consecutive discharge in recorded order; each sweep is one discharge window with `triggerOffset` at the trigger potential, or at the stimulus when stimulated.

| Field | Where | Required | Rule |
|---|---|---|---|
| `activation` | part | yes | `voluntary` or `stimulated`; a stimulated part's sweeps carry the stimulation fields. |
| `interval` | sweep | no | Seconds since the previous discharge of the same step, which some analyses sort by. Relative timing within the recording, never a clock time. |

`trial` therefore reads one of two ways, which the design note's test-type table records per test: alternatives, where the measurements use the one selected trial of each step (`motor`, `sensory`), or a sequence, where they use every trial in order and nothing is selected (`sfemg`). A seed's `selected` is valid only for a test whose trials are alternatives. F-wave and H-reflex keep one sweep per step, their sequence being the steps, which may differ in intensity.

A later test adds its own optional fields within this structure (a pulse train on an `rns` sweep, `train: { rate, pulses }`) as a minor format version.

Open within 1e, to settle with the MUP measurement definitions (point 6): whether a device's per-MUP firing rate, which needs the discharge times and cannot be recomputed from the template alone, travels as a seed, and whether the discharge times themselves do.

**1f. Seeds** (2026-11-14). A seed is the examiner's work as the device recorded it — marker positions, distances, trial selection — in numbers and references only.

On a sweep, marker positions as sample indices from the start of the sweep's data, integers, which is what a measure reads (6a) and what a saved marker holds (point 7). A converter turns a device's latency into the nearest sample, adding `triggerOffset` to a stimulus-relative one, so a seeded marker and a hand-placed one become latencies the same way. A marker the device left unset is absent. Marker names are vocabulary terms per test (point 5); a conduction sweep's set is `onset`, `negative_peak`, `positive_peak` and `end`.

```json
"seed": { "markers": { "onset": 533, "negative_peak": 696, "positive_peak": 778, "end": 811 } }
```

On a part, distances in millimetres, the selected trial of each step that has more than one, and the limb's surface temperature in degrees Celsius where the source recorded one:

```json
"seed": {
    "distances": [
        { "from": 1, "mm": 72 },
        { "from": 1, "to": 3, "mm": 230 }
    ],
    "selected": [ { "step": 2, "trial": 1 } ],
    "temperature": 32.5
}
```

A distance names steps, never sweeps, being a property of the sites and so of every trial recorded there. With `from` alone it runs from that step's stimulation site to its recording site, a `recording` term naming which where the part records at several; with `to`, between the stimulation sites of two steps. Both steps are in the part, and `mm` is positive. A field with nothing to carry is absent.

A seed never holds a value computed from these numbers. Ingest checks every step it names against the part, every marker name against the test's terms and every marker index against its sweep's sample count. A seed is not edited: a reader's moved marker or changed distance is saved as their own (point 7), and the seed stays in the stored file as the source's record.

**1g. Source names and name maps** (2026-11-14). The platform vocabulary holds canonical names only, and every name a stored file carries is one of them; the uploader is where a source's labels meet it.

A converter's output carries the names it could not resolve in `sourceNames`, an array of distinct strings after NFC normalisation, and a name field refers to one by index:

```json
"sourceNames": ["Medianus", "Ranne", "APB", "Oikea"],
"parts": [ { "id": "p1", "side": { "source": 3 }, "nerve": { "source": 0 },
             "sweeps": [ { "id": "s1", "stimulation": { "source": 1 }, "recording": { "source": 2 } } ] } ]
```

- Five fields may hold a reference in place of a term: `side`, `nerve`, `muscle`, `stimulation`, `recording`. In a reader's hand-over, never in a stored file, so may a device's stored EMG grading (6h), a source value like a name. `test` may not; the converter reads it from the export's type field and the submitter can correct it.
- A converter writes a term itself only where it resolves one deterministically, from a device's factory identifier; it never guesses from a typed label.
- A name is mapped per kind of field, so one string used as a nerve and as a site is two questions.
- Resolution runs in order: the name map the upload names, if any; exact matching on the vocabulary (design note section 4); the submitter, for whatever is left, mapping each name or marking a part or sweep to be dropped.
- Ingest then replaces every reference with its term, removes `sourceNames`, removes dropped sweeps with their data blocks, renumbers ids and offsets to stay consecutive (1c), and drops any seed entry naming a step left without a sweep. All of it precedes storing; a stored file is never rewritten, and ingest refuses one that still carries a reference or `sourceNames`.
- Source names live only in the author-private staging state while the upload waits, under the rule for `Recording.original_name`.

A **name map** is the composite counterpart of a channel map ([recordings/README.md → Channel maps](../../recordings/README.md#channel-maps)): an exact table from one source's labels, per kind of field, to vocabulary terms, chosen when that source's files are uploaded, so a batch of recordings following one naming needs no per-file mapping. The name avoids the submission pool's ingest profile, which is a different thing. Maps are not shipped: the composite module's repository holds a template for writing one, and a deployment's maps live in its own configuration, keyed like `RECORDING_CHANNEL_MAPS`. A submitter's saved mapping is a map of their own. A map chooses among terms and never adds one.

**1h. Fixtures** (2026-11-14). A deterministic generator in the composite module's repository writes every fixture from synthetic waveforms with a fixed random seed, so the files are byte-identical on every run; no real examination enters a fixture. Three sets: a valid document per first-build test and one complete examination with trials, a selection, seeds and `defaultChild`; the converter output of that examination, with `sourceNames` and references, and a name map resolving part of it; and one refusal fixture per ingest rule, paired with the refusal it must produce. They live beside the schema, and the platform pins the valid and refusal sets with its copy of the schema (1a). The generator is written with the module.

**2. Readers and the module** (2026-11-14). A composite resource takes data from many formats — several vendors' exports, more than one per vendor, a vendor database API, later the composite file itself and a DICOM representation — so each format has a reader of its own.

- **The contract.** Every reader hands the module the composite document in memory: the manifest as an object in the shape of the schema (point 1) and an asynchronous `getSamples(sweepId)`. The module knows the contract and no format.
- **The converter-output form is accepted.** A reader cannot resolve a lab's labels, so the module opens a document still carrying `sourceNames` and references and shows the source name where no term is set. Viewing a local file never requires mapping; strictness stays at platform ingest (1b).
- **A reader is held to the converter's rules.** A database export opened in the viewer carries the patient tables, the reports and a snapshot of identification, and its reader reads its allowlist only and surfaces nothing else. The rules in design note section 6 are reader rules, and a converter follows them because it is one.
- **One parser, two outputs.** A vendor reader's package may ship a command-line tool that writes the composite file from the same code, and that tool is what a deployment configures as its external converter; the platform still never imports or names it.
- **Loading is the reader's.** How a reader fetches its source and whether it uses a worker depends on the format (a folder of text files, an XML dump with serialised blocks, byte ranges of the composite file). The module builds every part and derived child from the manifest alone, loads conduction, MUP and single-fibre samples in the background once the parts exist, and loads an EMG save when its part is first activated. The accessor is asynchronous, so a reader can move its work behind a worker without the module changing.
- **Order.** First a reader for the first vendor's two export layouts, its curve export and its database export; then the vendor API reader converted to produce the contract; the composite file, DICOM and other vendors' readers wait. Whether a vendor reader's package is public is decided per package by the builder's public and non-public split.

**3. Part to resource mapping** (2026-11-14).

- **3a. One NCS resource per part, any number of recording sites.** A part may record from several sites at once — sweeps sharing step and trial with different `recording` are simultaneous responses to one stimulus — which nerve anastomosis studies, blink reflexes and somatosensory evoked potentials need. A part's `nerve` is the stimulated nerve, the recorded muscle may be supplied by another, so no per-sweep nerve override is needed (the design note's anastomosis question). A later minor version adds an optional sweep `recordingSide`, defaulting to the part's `side`, which then reads as the stimulated side; a blink reflex needs it, and a file using it names `recording_side` in `requires` (1b), an older reader otherwise showing the response on the wrong side. In the NCS module, measurements move from the stimulation site to the response, which is the channel (one stimulation site and one recording site); a site is a stimulation point without measurements; segments are grouped by recording site, two stimulation sites seen from one recording site, as `NcsNerveSegment.recordingSite` already expresses. Grouping by recording site rather than splitting the part is also the fix for the vendor API reader's motor sections collapsing. The type changes belong to points 4 and 6.

- **3b. One channel per sweep.** The channel's name is the sweep id, so markers and bindings map back to the document; its label is the stimulation site's display name, or the source name where no term is set. Every trial is a channel; trials not selected are hidden.
- **3c. Sensory pairs.** Both halves are channels, hidden by default, and the composite module adds a computed mean channel (`averaged: true`), which is what is shown and measured. The halves weigh equally until the design note's split-average question is settled. The mean has no id of its own; work on it names its members (point 7). The halves are the device's reproducibility check: they are overlaid on the mean, never measured, and neither can be left out of it, so a pair is used whole or its trial is not selected. A seed for the pair sits on its odd half and is read as the mean's. Ingest refuses markers on an even half, and a pair whose halves differ in sample count, sampling rate or trigger offset.
- **3d. Sites, segments, seeds.** Stimulation sites come from the steps in order, segments from consecutive sites, and a `from`-only distance becomes a segment ending at the recording site (`NcsNerveSegment.endSite: null`). Seeded markers become channel markers named by their term. Every measurement field is `null` until point 6.
- **3e. Units and sign.** Samples are converted from microvolts to volts, which both modules hold, and their sign is never changed; negative-up is the interface's `displayPolarity`. The vendor API reader currently also negates its samples, which has to be justified against a printout when that reader is converted, and belongs in that reader if the vendor stores inverted curves.
- **3f. Series.** An F-wave or H-reflex part has one channel per sweep. A sweep's stimulus intensity has no place on the NCS channel yet; that is an interface addition for point 4.
- **EMG saves.** One EMG resource per save, not one per muscle with the saves as channels, since a resource has a single duration and saves differ in length. The part's grouping of its saves lives in the composite resource.
- **Lazy saves.** A save's EMG resource is built when it is first activated, with its samples, so the EMG module needs no way of receiving samples after construction.
- **MUP templates.** A separate resource type in the EMG module, holding the templates of one part.

**Evoked potentials** (2026-11-15). Evoked potentials are a part modality of their own, `ep`, with a module of their own: `ssep`, `vep`, `baep`, `erg`, `pep` (contact-heat and laser) and `mep`, all later. The reasons and the format additions they bring (a sweep `stimulus` object, a recording `reference`, a `replications` reading of `trial`, component marker terms) are in the design note's test-type section. The trace display and markers they share with nerve conduction come from core's biosignal components and, in the interface, possibly a shared sweep-trace view. The name stays `ep`, the term current clinical standards use; event-related potential is the broader term and in clinical use mostly means the cognitive potentials, which would join as further `ep` tests (P300, mismatch negativity) if ever needed.

**4a. The composite resource** (2026-11-15). Preliminary shapes, to be adjusted where implementation finds a reason:

```ts
interface CompositeResource extends DataResource {
    /** The document as the reader handed it over (point 2), converter-output form allowed. */
    document: CompositeDocument
    /** Parts by part id, built from the manifest alone. */
    parts: Map<string, CompositePart>
    /** Derived children by term, from the kind's child builder. */
    derived: Map<string, DataResource>
    /** Resolve and activate the default child: `defaultChild`, the builder's default, the first part. */
    activateDefaultChild (): void
    /** The child resource for a part id or derived-child term, building a lazy one (an EMG save) on demand. */
    getChild (ref: string): Promise<DataResource | null>
}

interface CompositePart {
    id: string
    test: string
    /** One resource for an NCS or MUP part, one per save for an EMG part. */
    resources: DataResource[]
    /** Selected trial per step, seeded from the document and changed by the reader. */
    selection: Map<number, number>
}
```

- `childResources` holds the parts' built resources and the derived children, and `activeChildResource` is the one record of the active child.
- `activateDefaultChild` runs when the composite resource is activated with no active child; reactivating it keeps the child last active.
- Trial selection lives on the part, where a binding naming a part and a step resolves; the NCS resource is told which channels to show.
- An EMG save is a placeholder entry until `getChild` builds it; `childResources` lists built resources only.

**4b. The child-builder registry** (2026-11-15). A builder returns serialised templates, and the composite resource turns them into resources through core's runtime module registry (`getResourceFromSerialized`), as it does parts, so the composite module depends on core alone and the `edx` builder can ship inside it.

```ts
interface CompositeChildBuilder {
    /** The document kind it serves; one builder per kind. */
    kind: string
    /** Derived-child terms it builds, equal to the vocabulary's list for the kind (point 5). */
    terms: string[]
    /** Term to activate when the manifest names no `defaultChild`; null for the first part. */
    defaultChild: string | null
    /** One serialised template per term, built from the parts. */
    build (composite: CompositeResource): Array<{ term: string, template: Partial<DataResource> }>
}

registerCompositeChildBuilder (builder: CompositeChildBuilder): void
```

- The `edx` builder returns core's `DataTableTemplate` with `modality: 'tab'`; a template whose modality no registered module serves is skipped with a warning, and a skipped default falls through to the first part.
- A builder runs once, after the parts exist; tables stay current through their bindings (4d), never by rebuilding.
- Registration precedes loading, a second builder for a kind is refused, and the builder's `terms` are checked against the vocabulary at registration.
- A kind without a builder has parts and no derived children.
- The cell-binding type is therefore a core type, since it travels inside a template core declares.

**4c. Values, at two levels** (2026-11-15). The binding layer is a core facility any resource can use; the composite resource is one user of it. Core declares it without knowing what a part or a sweep is:

```ts
/** Core. A value inside a resource, addressed by a name and a provider-defined scope. */
type ValueRef<S extends object = object> = { name: string, scope: S }

/** Core. What can be bound: plain values and a time span, enough for a measure or a selected segment. */
type BoundValue = number | string | boolean | { start: number, end: number } | null

/** Core. Implemented by a resource whose values others bind to. */
interface ValueProvider<R extends ValueRef = ValueRef> {
    /** Current value, numbers in fixed units (distances in millimetres, as in seeds); null when not available. */
    getValue (ref: R): BoundValue
    /** Whether the ref is an input a reader may write rather than a derived value. */
    isInput (ref: R): boolean
    /** Write an input; false for a derived value. */
    setValue (ref: R, value: BoundValue): boolean
    /** Every ref whose value changed, batched per change; returns the unsubscribe. */
    onValuesChanged (handler: (refs: R[]) => void): () => void
    /** Make the resource the ref comes from the provider's active child. */
    focus (ref: R): void
}

/** Core. Implemented by a resource whose content binds to values. */
interface ValueConsumer {
    bindValues (provider: ValueProvider): void
}
```

The composite's refs specialise it, with a measure term (point 6) as the name:

```ts
type CompositeValueRef = ValueRef<{
    part: string
    /** Resolved to the selected trial by the composite resource. */
    step?: number
    /** What a part-level provider is addressed by. */
    sweep?: string
    /** A recording-site term, for a part recording at several sites. */
    recording?: string
    /** Steps, as in a seed's distances (1f). */
    segment?: { from: number, to?: number }
}>
```

- A part resource is a provider answering refs by `sweep`, its channel names (3b), and knows nothing of steps or selection.
- The composite resource is the tables' provider: it resolves a `step` to the selected trial's sweep and delegates to the part, and a changed selection emits every ref on that step.
- The composite resource calls `bindValues(this)` on each derived child implementing `ValueConsumer` once it is built; the tab module implements it without knowing what a composite is, and a table from a plain tabular source is never bound.
- Decimals and display units belong to the table's column configuration.
- The layer sits above core's property-change events, which report a resource's own properties; it addresses values inside a resource that are not top-level properties, one sweep's latency or one event's span. An EEG resource providing the span selected as the posterior dominant rhythm, bound by a report field, is the same mechanism with no composite involved.

**4d. Bindings, inputs and links** (2026-11-15).

```ts
/** Core. What a section, a table or a cell points back to. */
type SourceRef = ValueRef['scope']

/** Core. A table cell; `subcontext` is removed. */
type DataTableRowValue = {
    value: boolean | number | string | null
    descriptors?: Record<string, boolean | number | string>
    tooltip?: string
    /** Bound: the table fills `value` from its provider and keeps it current. */
    bind?: ValueRef
    /** Points back to where the content comes from, without being bound. */
    source?: SourceRef
} | null
```

Section and table templates replace `subcontext` with `source?: SourceRef`.

- **`value` is always current.** A bound cell's `value` is written from the provider on binding and on every change, so anything reading the table — the interface, an export, the tab module's plain-text rendering — reads current numbers without knowing about bindings. A ref the provider cannot answer reads `null`; a span is rendered by the column configuration.
- **Editability is the provider's.** A cell is editable when `isInput` says so; an edit goes through `setValue`, and the change event updates it like every cell depending on it.
- **Every value has one owner.** A distance lives on its part resource and nowhere else. The NCS view's sidebar writes it on the part directly and the table through the composite, which delegates to the same part; the composite relays the part's changes, mapped to the steps they are selected for, so both update. Inputs are as reactive as derived values, and nothing holds a copy.
- **Links.** The composite resource may declare two refs one value — a physical distance used by a motor part and an F-wave part — so a write to either reaches the canonical owner and a change emits both. Links are the composite's, not the document's; whether one is made automatically or only by the reader is settled with the measurements (point 6).
- **Navigation.** A cell's `source`, or its binding's scope, is passed to `focus`, and the composite sets its `activeChildResource`. Focus is activation in core's terms; the interface shows what is active. A `source` without a `bind` is a row label pointing at a part.

**5. Vocabularies** (2026-11-15). Two files, because anatomy is shared and the electrodiagnostic rules are not.

- **`epicurrents.anatomy`** names anatomical structures and regions for any module that refers to them: nerves and their branches, with an optional `parent`; muscles; the regions and landmarks used as stimulation and recording points (`wrist`, `erb_point`, `popliteal_fossa`, `digit_2`, relative positions such as `below_elbow`); and laterality (`left`, `right`, `midline`) as a separate qualifier, never folded into a term. It also names the anatomical relations a position is placed by: `proximal`, `distal`, `medial`, `lateral`, `anterior`, `posterior`, `superior`, `inferior`, `dorsal`, `palmar`, `plantar` (1e). It ships in viewer core, as the biosignal event vocabulary does, since modules depend on core alone, and the platform pins a copy by version and digest: a term is added in core first and copied across with the pin moved. A term may carry `aliases`, synonyms in international use such as `peroneal` for `fibular`, and optional crosswalk identifiers in Terminologia Anatomica 2 and the Foundational Model of Anatomy, both freely available; SNOMED CT is licensed and would at most be a crosswalk a licensed deployment adds. EEG electrode positions are out of scope, having names of their own that an evoked-potential derivation refers to, and so is innervation, a domain table on which sources disagree.
- **`epicurrents.edx`** holds the domain: tests with their part modality and reading of `trial`, the kinds with their derived-child terms (`edx`: `summary` and one per test), markers and measures per test, and which anatomy categories each manifest field accepts (`nerve` nerves; `muscle` muscles; `stimulation` regions or nerves; `recording` regions or muscles). It lives beside the schema in the composite module's repository and the platform pins it with the schema (1a). The design note's test-type table describes this file rather than being a second source.
- **Layout.** Both keep the envelope of the existing vocabulary files (`standard`, `version`, `description`). A term's key is its code, the value a manifest carries, with a display `name` and an optional `description`.
- **Matching** at ingest is on the code, the display name or a listed alias, after case folding and treating space, underscore, hyphen and full stop alike. An abbreviation is never an alias; it belongs in a name map (1g).
- **Validation is membership only.** A field's value is a term of a category the field accepts, and a marker is in its test's list. Nothing checks anatomical plausibility, which is the reader's judgement and would refuse legitimate unusual protocols.
- **Versioning.** A term is never removed or renamed once shipped; additions are a minor version. A reader meeting a term newer than its copy shows the code rather than refusing the document; ingest validates against its pinned version.
- **Contents.** Point 5 fills the anatomy from the design note's lists and the `edx` tests and derived-child terms; marker and measure terms arrive with point 6, the conduction markers `onset`, `negative_peak`, `positive_peak` and `end` already decided (1f).

**6a. Common conventions** (2026-11-15).

- **Units** are SI: seconds for latencies and durations, volts for amplitudes, volt-seconds for areas, metres per second for velocities, millimetres for distances as in seeds, and fractions, not percentages, for changes. Display units and decimals belong to the table's columns.
- **A marker is a sample index**, in a seed (1f) and in saved work (point 7); the interface snaps a placed marker to the nearest sample, as `NcsChannelMarker` reads one. At 24 to 48 kHz a sample is 0.02 to 0.04 ms, below any reading's resolution.
- **A latency is measured from the stimulus**: the marker's index less `triggerOffset`, over `samplingRate`.
- **Arithmetic.** Measures are computed in float64 from the samples as stored, an integral being the sum of the samples times the sample interval, in sample order. A latency is therefore exact, and two implementations of any other measure agree within a relative 1e-6 (9a).
- **Two baselines.** From the onset marker, the signal's value there, as devices measure baseline-to-peak amplitude; or tilted, the straight line between the onset marker and the positive peak, which corrects baseline drift and is common in sensory measurement. A measure taken against the tilted line is a measure of its own (`amplitude_negative_tilt` beside `amplitude_negative`, the area likewise), so both are always available, a table binds the one it shows, and no setting changes a number under a binding.
- **Signs.** Measures are computed on the samples as stored (1e), never the displayed trace, so display polarity changes no number. The negative peak is the phase in which the active electrode is negative; amplitudes are positive magnitudes, the phase in the measure's name.
- **No response is not no value.** `null` is a value that cannot be computed, a marker or a distance missing. An absent response is a finding the reader records as an input on the response: its amplitudes and areas are then `0`, its latencies `null`, and every velocity depending on it `null`.
- **Saturation.** A measure using a sample on a run of identical values at the sweep's extreme carries a `<measure>.clipped` flag on the same ref, since the input range is not exported and the flat run is the only evidence; the measure is still given.

**6b. Placement** (2026-11-15).

- A marker's origin is the reader, a seed or automatic placement, in that precedence, and each marker records its own. A reader's marker hides the one beneath it; removing it brings that one back.
- Automatic placement fills only sweeps whose seed carries no markers, so a marker a device left unset stays unset. Under a blind protocol seeded markers are ignored and it fills every sweep (9c).
- **Placement is anchored on the reader's markers.** A reader typically places only the onset of a motor response, and the rest follow from the waveform after it; the detector therefore also runs anchored, placing every marker the reader has not placed relative to those they have — the peaks and the end after a reader's onset, an F wave's end after its onset — and re-placing them when the reader moves an anchor. A marker the reader placed is never moved by the detector.
- The detector is deterministic and versioned with the module, so a value computed from automatic markers can name the rules that placed them; the rules per test are in 6c to 6f.
- The detector never records an absent response; finding nothing leaves the markers unset and the measures `null`.
- Automatic markers are recomputed on every load until the reader marks the part reviewed, which saves each automatic marker then standing, with its origin (point 7). From then on every marker of the part is a saved one and a later detector version moves none of them; an edit withdraws the review. The reader's markers and inputs are saved as they are made, and seeds are already in the stored file.
- Placing, moving or removing a marker, or changing an input, emits a change for every measure depending on it (4c).

**6c. Motor conduction** (2026-11-15). Markers `onset`, `negative_peak`, `positive_peak`, `end`.

| Response measure | Definition |
|---|---|
| `latency_onset` | Onset latency; at the most distal site, the distal motor latency. |
| `latency_negative_peak` | Negative-peak latency. |
| `amplitude_negative` | Onset baseline to negative peak. |
| `amplitude_negative_tilt` | Tilted baseline to negative peak. |
| `amplitude_peak_to_peak` | Negative peak to positive peak. |
| `duration_negative` | Onset to the negative phase's end, where the signal recrosses the baseline after the negative peak, found in the samples rather than marked. |
| `duration_total` | Onset to end. |
| `area_negative` | Integral below the baseline over the negative phase. |
| `area_negative_tilt` | The same against the tilted baseline. |
| `area_total` | Integral of the absolute deviation from the baseline, onset to end. |

A segment is two stimulation sites seen from one recording site. The default segments join consecutive steps; a reader may add one between any two stimulation sites of a recording site, skipping those between, as a quick test for conduction block does, and a seed's distance between two non-consecutive steps defines one too. A reader's segment is an input, saved with their work (point 7).

| Segment measure | Definition |
|---|---|
| `latency_difference` | Proximal less distal onset latency. |
| `conduction_velocity` | Segment distance over `latency_difference`, in metres per second; `null` when either is missing or the difference is not positive. |
| `amplitude_change` | (proximal − distal) / distal on `amplitude_negative`; negative is a drop. |
| `amplitude_change_peak_to_peak` | The same on `amplitude_peak_to_peak`, an option beside the default. |
| `area_change` | The same on `area_negative`. |
| `duration_change` | The same on `duration_negative`, for temporal dispersion. |

The most distal response has a distance but no velocity, its latency including neuromuscular transmission; a residual latency can join later as a term of its own.

Detector: blank the first millisecond after the stimulus; onset is the first sample after it leaving the pre-response noise band by more than the larger of three standard deviations and a fixed floor, and staying out; the negative peak is the most negative sample before the next return to baseline; the positive peak the most positive after it, before the signal settles; the end the first sample after that back inside the band. The constants are the implementation's, versioned with the detector and pinned by tests against the synthetic fixtures' known latencies and amplitudes (1h).

**6d. Sensory conduction** (2026-11-15). Markers `onset`, `negative_peak`, `positive_peak`, `end`, and an optional `first_deflection`.

- `onset` is where the negative deflection starts, in sensory as in motor, so an onset latency means one thing in every test. With an initial positive phase, that is its peak, where the signal turns negative; without one, where the signal leaves the baseline going negative.
- The default sensory amplitude is `amplitude_negative_tilt`, the tilted line from `onset` to `positive_peak` (6a). With an initial positive phase it is the positive-to-positive tilt, without one the onset-to-positive tilt, one definition covering both as clinical practice measures them.
- `first_deflection`, where an initial positive deflection leaves the baseline, gives a `latency_first_deflection` for research; no clinical measure depends on it.
- Response measures are motor's (6c) — latencies, the amplitudes, durations and areas — and `conduction_velocity_onset` and `conduction_velocity_peak`, the stimulation-to-recording distance over `latency_onset` and `latency_negative_peak`. Segments are motor's, an inching step or a short segment, the velocity over the onset-latency difference.
- Measures are taken on the pair's mean channel alone (3c); the halves carry no markers, and a reproducibility measure for the pair can join later as its own term.
- The `edx` builder's sensory columns are `amplitude_negative_tilt`, `latency_onset`, `latency_negative_peak` and `conduction_velocity_onset`, the builder's choice rather than a rule of the measures.
- Detector: motor's with a 0.5 ms blanking. A positive deflection above the noise band before the negative phase puts `onset` at its maximum and `first_deflection` where it left the band; without one, `onset` is where the signal leaves the band going negative and `first_deflection` stays unset.
- A converter maps a vendor marker placed at the negative deflection's start in the absence of a positive phase to `onset`; which vendors do so the export samples will show.

**6e. F-wave** (2026-11-15). A series of sweeps, one per step, each holding the M response and then the F wave.

- Markers per sweep: `m_onset` and `m_end`, the M response's bounds as the motor detector places them; `f_onset`, set only where an F wave is present; `f_end`, bounding the window the F amplitude is measured in.
- Sweep measures: `latency_m_onset`; `amplitude_m_peak_to_peak` over the M window; `latency_f_onset`; `amplitude_f_peak_to_peak`, the largest less the smallest sample between `f_onset` and `f_end`, F waves being polyphasic; `f_m_interval`, `latency_f_onset` less the M onset latency, the sweep's own where marked and the series' otherwise.
- Presence: a sweep has an F wave when `amplitude_f_peak_to_peak` reaches the presence threshold, an input on the part defaulting to a constant of the implementation, conventions varying around 20 µV. A reader may mark a sweep's F wave absent (6a) or present whatever the threshold says, a small wave clearly seen counting.

| Series measure | Definition |
|---|---|
| `f_m_interval_min`, `f_m_interval_mean`, `f_m_interval_max` | Over the sweeps with an F wave. The minimum is the measure clinical practice relies on most, since subtracting the M latency removes conduction disturbances distal to the stimulation site. |
| `f_latency_min`, `f_latency_mean`, `f_latency_max` | Over the sweeps with an F wave. |
| `f_persistence` | Sweeps with an F wave over all sweeps, as a fraction. |
| `f_chronodispersion` | `f_latency_max` less `f_latency_min`. |
| `f_amplitude_mean`, `f_amplitude_max` | Over the sweeps with an F wave. |
| `f_m_ratio_max`, `f_m_ratio_mean` | `f_amplitude_max` and `f_amplitude_mean` over the series' M amplitude; for research, neither being a clinical routine. |
| `latency_m_onset` | The series' M onset, from the first sweep carrying `m_onset`. |

The `edx` builder's F-wave columns are `f_m_interval_min`, `f_latency_min` and `f_persistence`. F-wave conduction velocity waits, since it needs the distance from the stimulation site to the cord, a distance of another kind than a segment's (6g).

Detector: the M response by the motor detector; the F window from a fixed margin after `m_end` to the sweep's end, `f_onset` its first sample leaving the noise band and `f_end` its last outside it. A window with no excursion reaching the threshold leaves `f_onset` unset, which persistence counts as no F wave — the one case where the detector's silence counts, persistence being defined over the sweeps without one.

**6f. H-reflex** (2026-11-15). A recruitment series, one sweep per step, usually at rising intensity, each with an M and an H response.

- Markers per sweep: `m_onset`, `m_end`, `h_onset`, `h_end`, bounding the two windows as the F-wave markers do.
- Sweep measures: `latency_m_onset`, `amplitude_m_peak_to_peak`, `latency_h_onset`, `amplitude_h_peak_to_peak` over the H window. Each response's presence is a threshold input on the part with the reader's present and absent override, as for the F wave.

| Series measure | Definition |
|---|---|
| `h_amplitude_max` | The largest H peak-to-peak over the series. |
| `m_amplitude_max` | The largest M peak-to-peak, Mmax where the series reaches supramaximal intensity. |
| `h_m_ratio` | `h_amplitude_max` over `m_amplitude_max`. |
| `latency_h_onset_min` | The shortest H onset latency over the sweeps with an H response. |
| `latency_h_onset_at_max` | The H onset latency of the sweep with the largest H. |
| `h_threshold_intensity` | The lowest intensity with an H response present; `null` where the source carries no intensities. |

A recruitment-curve fit is research for later.

Detector: at low intensity the H response appears with no M before it, so M and H are told apart by latency. The boundary is an input on the part, defaulting to the end of the M response of the highest-intensity sweep, where M is largest and certain; a response starting before it is M, after it H, and each window is measured as the F wave's is.

Side-to-side differences, of an H latency here and of any measure elsewhere, are a value of the composite resource rather than a part's, since no part holds both sides; they are defined once in 6g.

**6g. Distances, links and side differences** (2026-11-15).

- **Distances are inputs** of two kinds: a response's, from its stimulation site to its recording site (scope `{ part, step }`), and a segment's, between two stimulation sites (scope `{ part, segment }`). Each records its origin, in the precedence reader, seed, derived. A distance belongs to its steps and holds for whichever trial is selected.
- **Derived distances** the composite resource fills until a reader or a seed gives one: two stimulation positions offset from one landmark along one axis (distal and proximal its two senses) give the difference of their offsets, so an inching study needs no typing; a reader's segment skipping sites (6c) gives the sum of the consecutive segments along its path when all are known. Nothing else is guessed; a stimulation-to-recording distance is never estimated from anatomy.
- **Temperature** is an input on a part, the limb's surface temperature in degrees Celsius, from a seed or the reader. No measure is corrected for it; reference generation takes it as a covariate (9e).
- **Links** (4d) are made automatically only between parts sharing nerve, side, stimulation position and recording where both tests use the distance — a repeated part, a motor and an F-wave part from one stimulation and recording. A reader may link any two distance refs and remove an automatic link; the composite resource holds the links, and a reader's links and unlinks are saved with their work (point 7).
- **Side differences.** Mirror parts — one test on one nerve or muscle with the same stimulation and recording on opposite sides — are paired automatically, and a reader may pair or unpair any two. For a measure of a pair the composite resource gives `<measure>.side_difference`, the absolute difference, and for an amplitude `<measure>.side_ratio`, the smaller over the larger, as composite-level values of scope `{ pair: [partA, partB] }`.

**6h. EMG saves and MUP templates** (2026-11-15).

An EMG part's findings are the examiner's gradings, categorical inputs on the part whose values are ordered terms of the `edx` vocabulary, never text, so a table or a report binds them like any value (4c). They are saved as codes of `epicurrents.edx.finding`, which a de-identifying recipient does not receive (point 7). The grid is a compromise between the grading styles in use:

| Category | Finding | Scale |
|---|---|---|
| Spontaneous activity | Insertional activity | `normal`, `increased`, `reduced` |
| | Fibrillation potentials | `0` to `5+` |
| | Positive sharp waves | `0` to `5+` |
| | Fasciculations | `0` to `3+` |
| | Complex repetitive discharges | `0` to `3+` |
| | Myotonic discharges | `0` to `3+` |
| MUP morphology | Amplitude | `3-` to `3+` |
| | Duration | `3-` to `3+` |
| | Polyphasia | `0` to `3+` |
| | Instability (jitter) | `normal`, `increased`; optional |
| Voluntary activity | Recruitment | `normal`, `reduced`, `early` |
| | Interference pattern | `normal`, `1-`, `2-`, `3-`, `none` |

- **Grading styles translate through the name-map mechanism (1g).** A reader writes a device's stored grade into `sourceNames` and refers to it from the finding in the document it hands over; resolution runs through the upload's name map, which is where a lab's grading style becomes one map, then exact matching on the scale's terms, then the submitter. A style that does not fit, a four-step scale against a six-step one, is placed by the map or the submitter and never interpolated, and a grade that cannot be placed is dropped. Showing a device's grading as stored, as the vendor API reader does, remains possible in a local file through the converter-output form (point 2), never in a stored one.
- **A device's gradings become the uploader's findings, never part of the stored file.** The file is served unchanged to every reader, so a grading in it would reach a de-identifying recipient past the withholding of `edx.finding`. The viewer's upload step resolves the gradings a reader handed over, leaves them out of the canonical file, and once the recording exists saves them as the uploader's own finding labels, which the uploader may then edit like any other. Ingest refuses a file carrying a grading, and on the fallback route (8c) the canonical rewrite drops them with a count, ingest writing no confined vocabulary (point 7).
- **A save has no computed measures in version 1**; interference pattern analysis (turns and amplitude, the envelope) is later.

A MUP template has markers `onset` and `end` bounding the potential and an optional `satellite` for a late component.

| Template measure | Definition |
|---|---|
| `amplitude_peak_to_peak` | The largest less the smallest sample between onset and end. |
| `duration` | `end` less `onset`. |
| `phases` | Baseline crossings between onset and end, plus one. |
| `turns` | Direction changes exceeding the turn threshold, an input on the part defaulting near the usual 20 to 25 µV. |
| `area` | Integral of the absolute value between onset and end. |
| `thickness` | `area` over `amplitude_peak_to_peak`. |
| `size_index` | Twice the base-ten logarithm of the amplitude in millivolts, plus the thickness in milliseconds. |

The part's measures over its templates are `mup_count`, the means of amplitude, duration, phases and turns, and `polyphasic_fraction`, the share with more than four phases. A template's firing rate (1e) is a seed when the device reports one and `null` otherwise; discharge times are not carried in version 1, and could be re-detected from the source save when a template names one. The detector places onset and end where the signal leaves and regains the baseline band by a slope criterion, versioned with the others. Multi-MUP statistics beyond these and the turns-amplitude cloud are later.

**6i. Reference sets** (2026-11-15). Building waits; these constraints hold now. Generating reference values from the markers readers place is one of the module's purposes, and benchmarking against several sets is part of it.

- **A reference set is configuration, never part of a document**: limits per measure for a test, nerve and site, as mean and standard deviations from a regression or as fixed bounds. It carries a key, a version and its provenance — the dataset it was generated from, n, the method, the measure-definition and detector versions it was computed under.
- **Any number of sets may be active**, an ordered list the reader changes. Per set, `<measure>.deviation` (in standard deviations; `null` for a fixed-bound limit) and `<measure>.abnormal` (true, false or `null`) carry a `reference: '<key>@<version>'` in their scope, so a table shows sets side by side; a ref naming no set uses the first active one.
- **Consensus** over the active sets is a value of the composite resource: `<measure>.consensus` is `normal` when every set covering the measure calls it normal, `abnormal` when every one calls it abnormal, `discordant` when they disagree, and `null` when no active set covers it or none can be evaluated; `<measure>.consensus_count` gives the abnormal and covering counts. It is computed in the module, so an export or a report carries it, and changing the active sets emits like any input.
- **A measure has a `layer` dimension**: the same measure computed from reader A's markers, reader B's, the seed or the detector, for inter-rater agreement and detector benchmarking. A ref naming no layer follows the precedence of 6b.
- **Measures must be reproducible from saved work** (a constraint on point 7): marker positions and inputs are saved, never computed measures; a reviewed part records the measure-definition and detector versions it was reviewed under; two readers' markers on one sweep are kept apart, as the annotations layer keeps each author's rows.
- **Covariates.** A regression limit needs the subject's age, sex and height, later weight. They are stored beside the recording, never in the document, and served exactly only where 8h allows; the composite resource reads them as inputs when present, and without them only fixed-bound limits apply and a regression limit reads `null`.
- **Generation** is point 9.
- The first build has no reference sets; its tables show measures only.

**7. Persistence** (2026-11-15).

- **One label per item.** Each item of a reader's work is a `Label` on the recording carrying one code. A label suits work positioned within a sweep, a composite file having no common timeline; its `name` is the term's display name, as ingest names a label from a footer code, and its `value` is empty.
- **Two confined vocabularies.** `epicurrents.edx.work` holds markers, geometry, selections and reviews. Its codes cross the annotation-text boundary, so that work reaches de-identifying grantees, federated peers and reference generation over a pool, where a label value or a code's `meta` would be withheld from all of them. `epicurrents.edx.finding` holds an EMG part's gradings (6h), an examiner's conclusions about the subject and not positions on a trace. It registers as a vocabulary of readings, a flag on the registration that [annotations/redaction.py](../../annotations/redaction.py) consults: a caller under a de-identifying grant receives no finding code they did not write, as with text. The split follows the unit redaction already distinguishes, the standard, so no rule is per verb.
- **The grammar** is `<verb>:<subject>=<value>`, each separator with one role: `:` ends the verb, `>` joins the two ends of a segment (from, to), `/` qualifies a subject with a term or a number, `=` introduces the value, `~` joins the two sides of a link or a pair, `+` joins the sweeps a mean is taken over, `@` names a value's origin. None occurs within an id, a term, a grade (`2+`, `3-`) or a number, every number is an integer, and none has a sign.

| Item | Value |
|---|---|
| Marker | `marker:s12/negative_peak=696`, a sample index (6a); `=696@detector` for an automatic marker saved at review |
| Marker on a mean | `marker:s5+s6/negative_peak=701`, the members in id order (3c) |
| Response distance | `distance:p2/3=72` (part, step); `distance:p2/3/abductor_digiti_minimi=72` where the part records at several sites |
| Segment distance | `distance:p2/1>3=230` (part, from step, to step) |
| Presence | `presence:s4=absent` or `=present`; `presence:s4/h_response=absent` on a sweep holding several responses, the response a term of the test (`m_response`, `f_wave`, `h_response`) |
| Trial selection | `selected:p2/2=1` (part, step, trial) |
| Reader segment | `segment:p2/1>4=on` |
| Link | `link:p2/1>3~p4/1>3=on`, distance links being the only kind in version 1; `=off` cancels an automatic one |
| Pairing | `pair:p1~p2=on`; `=off` cancels an automatic one |
| Temperature | `temperature:p3=325`, tenths of a degree Celsius |
| Part input | `threshold:p4/f_presence=20` in microvolts, `boundary:p6=18000` in microseconds |
| Review | `reviewed:p3=1.0/1.0`, the measure-definition and detector versions |
| Finding, in `epicurrents.edx.finding` | `finding:p5/fibrillation=2+` |

| Domain | Range | Form |
|---|---|---|
| Ids | `p` or `s` and 1 to 5 digits | as 1c |
| Step, trial | 1 to 999 | integer |
| Distance | 1 to 999 mm | integer |
| Marker | sample index 0 to 199999, and below its sweep's sample count by the write gate (8e) | integer |
| Boundary | 0 to 2000000 µs | integer |
| Threshold | 1 to 10000 µV | integer |
| Temperature | 150 to 450 | integer |
| Version | `major.minor`, each 0 to 99 | |
| Whole value | at most 64 characters | |

- **Validation.** The validator matches each verb's anchored pattern and checks every part against its domain: id patterns, `edx` terms, the ranges above. `canonicalise` writes numbers without leading zeros and the two sides of a `~` and the members of a `+` in ascending order, so equal items are spelled alike. The limits leave no room for a name, a date or an identity code in a value; they cannot remove every covert channel, marker positions being legitimately free numbers that a determined writer could spread digits across, which is why a confined vocabulary is for an authenticated writer and not for files, and what the assessment row (point 8) states. Integer sample indices bound that channel by the sweep's length.
- **Identity.** An item's key is its code up to the `=`, and a label's `object_hash` derives from its author, the recording's `stored_name` and that key. Saving an item again therefore updates its row, and a state and its opposite cannot both exist: presence, a link and a pairing each carry their state as the value. Moving a marker rewrites the value; removing it deletes the label, restoring the seed or automatic marker beneath (6b). Deleting an `=off` label returns a link or pairing to the automatic rule.
- **Steps for geometry, sweeps for waveforms.** A distance or a segment names steps and holds for whichever trial is selected. A marker or a presence names the sweep it was placed on, or the members of the mean it was placed on, since it describes one waveform and means nothing on another. The write gate refuses a marker or a presence naming one half of a sensory pair (3c).
- **Review.** `reviewed` is the reader's statement that a part is finished. Saving it also saves every automatic marker then standing as `@detector`, so the part's measures are computed from saved markers alone, whatever a later detector would place. The viewer deletes the label on any later edit to the part. The platform does not rely on that: a part counts as reviewed in a layer only where none of that layer's labels on the part is newer than its `reviewed` label. The stamp is per part because one per recording would be restamped by a single edit over work done under another detector.
- **Authors are layers.** Each author's labels are their own, the `layer` dimension (6i) selects one, and nothing is merged. Which layers a reader sees follows the existing permissions and a dataset's blind protocol (9c); their own is the default.
- **Share-token writers are a deployment's decision.** A confined vocabulary rests on a writer the platform can name, which a share-token holder is not. `COMPOSITE_WORK_SHARE_TOKEN_WRITES`, off by default, admits work and finding codes written under a share token, for crowd-sourced annotation; each `annotator` name under a token is then a layer. Reference generation reads such a layer only where the dataset's protocol admits it (9d).
- **Saving.** The viewer saves on the reader's save, every changed item in one bulk request the platform applies in one transaction, never one request per marker moved: each label written is a chained audit row.
- **Not saved as work:** automatic markers on a part not yet reviewed, recomputed; seeds, already in the stored file; computed measures, recomputed (6i). Covariates are not reader work and have their own record (8h).
- **Writer.** The viewer writes through the annotations API. Ingest never writes either vocabulary, accepting closed ones only; device work arrives as seeds in the file, a device's gradings excepted, which the viewer saves as the uploader's findings (6h).

Fully marked work is a few hundred labels per examination, the corpus averaging about 85 sweeps.

**8a. Modality** (2026-11-15). `composite` joins `RECORDING_MODALITIES`. A file declares it through its magic and the manifest's `modality`; the channel vote never infers it.

**8b. The validating pass** (2026-11-15). Ingest parses the file, validates the manifest strictly against the pinned schema and vocabularies (1a, 5) and refuses anything else, resolves source names (1g), and rewrites the manifest canonically. The pass carries a version of its own beside `DEIDENTIFICATION_VERSION`, stamped on every composite recording it writes and pinned the same way: a digest over the validator, the canonical rewrite and the pinned schema and vocabulary files fails the suite when any of them changes without a bump. A vocabulary's minor version therefore bumps the pass too, so a stored file names exactly the term lists that admitted it.

The pass also refuses what a schema cannot express, each a place bytes could ride unseen: a data section the sweeps' blocks do not cover exactly, in order, without a gap, an overlap or a trailing byte; padding that is not zero; a sample that is not finite; a duplicate key in the manifest; a `requires` entry or a `subset` flag it does not expect; and a number outside the range and precision its field declares, the rewrite spelling each in one canonical form as the work grammar does (point 7).

**8c. Mapping before upload, and the fallback** (2026-11-15).

- **The viewer maps before anything is sent**, as the submission upload prepares its file against what the platform accepts. The upload step lists every unresolved source name and does not continue until each has a term or is dropped; dropping removes parts and sweeps and renumbers ids and offsets by ingest's rules (1g). No source name or reference leaves the browser, and for a file the viewer prepared ingest's canonical rewrite changes nothing.
- **The upload is the resource's serialisation.** A composite resource's serialised form is the composite document of point 2, so a reader's document, the resource built from it and the resource serialised close a loop. Each part's resource serialises its part, which makes the NCS, EMG and MUP resources' serialisation real work and their `fromSerialized` its round-trip check; the composite resource assembles the manifest and samples, volts back to microvolts, canonical fields only, seeds unchanged. A file format is an encoding of that form: the composite file is one, a thin writer of magic, length, manifest and blocks pinned by the fixtures (1h) so the viewer's writer and the platform's validator agree, and a DICOM exporter would be another, the module unchanged. The reader's work is not in it, being annotations (point 7), and serialising a resource that still carries an unresolved reference fails, so the mapping rule is enforced by the serialiser and not only by the interface.
- **`serialize()` becomes `toSerialized()`** in core, pairing with `fromSerialized()`. The base declaration in core's application and biosignal types and its implementations (`GenericAsset`, `GenericAnnotation`, `ResourceLabel`, `GenericBiosignalEvent`) gain `toSerialized()`, and `serialize()` stays one release as a deprecated alias forwarding to it, so the sibling packages and the interface move at their own pace; the alias goes at core's next major. It is a core prerequisite of the composite module, beside the value-binding types (4c).
- **Name maps in the viewer.** The deployment's name maps are served in full to signed-in users who may upload, the audience that already sees the channel-map keys, since a map holds a lab's labels, a fingerprint of a site and not of a patient. A submitter's own saved choices stay in the browser for now; saved on the platform they would link to the user and need registering for erasure and subject export.
- **The fallback** is platform-side mapping for files that did not come through the viewer: converter output from a batch upload in the platform interface, or `import_recordings`. Such an upload waits as `PROCESSING` with `ingested_at` unset, which leaves the purge filter as it is and lets the orphan reaper retire one left unmapped past the retention window. A `CompositeMapping` row tied to the recording holds the source names, their unresolved occurrences and the chosen name map; it is author-private through `_can_see_original_name`, its source-name fields are registered with `register_masked_fields(..., withhold=True)` so no name reaches the permanent trail, and it goes with its recording or when ingest completes. The author's `GET` and `POST /recordings/{hash}/mapping` read and submit the mapping, auditing as `recordings.composite.mapping.read` and `.submit` with counts only. The mapping response carries the date the reaper would retire the upload, the page shows it, and the author is notified before it passes, the notice naming the recording by its hash-prefix handle only. A name map chosen at upload (`name_map` beside `channel_map`, configured in `RECORDING_NAME_MAPS`) has its key and digest sealed in the READY audit row, as a channel map's are.
- **The fallback exists only where direct upload does.** `RECORDINGS_DIRECT_UPLOAD`, a deployment-level setting in core and not a composite one, closes the direct route for every format: off, the upload endpoint accepts only files the viewer prepared, which leaves no platform-side mapping. It is a platform item of its own in the ROADMAP.

**8d. Serving, the part index and extraction** (2026-11-15).

- **Dispatch on modality in `_build_serve_pipeline`.** A `composite` recording gets an empty pipeline, the file holding no identification by construction once the validating pass (8b) has admitted it. The dispatch is explicit: no EDF pass ever runs on composite bytes, the EDF header middleware's fail-open on bytes it cannot parse never being the reason a file passes through, and a recording whose modality has no entry is refused rather than passed through, so a new format is not served until its pipeline is decided.
- **Range requests** work on the stored bytes as they do now, and the composite reader relies on them; a peer's download size is the file's, an empty pipeline changing no length. The offload stays keyed on `apply_middleware`: a de-identifying grant streams the file, any other caller may be offloaded.
- **EDF-only endpoints refuse** a composite recording with a code saying the format has no time slices, the time-range slice among them, rather than failing inside the EDF parser.
- **Contract tests.** `test_serve_pipeline_parity` gains the composite request shapes — full and ranged, with and without middleware, the same bytes — and the refusal of an unknown modality; the load-bearing reviewer runs on the change.
- **One file per recording.** The stored file stays the single unit, EMG saves included: purge, erasure, linking, preservation, lifecycle versions and the de-identification record all rest on one file per recording, and separate files would reopen each of them and bring back the wrapper's consistency problem. On-demand loading needs no split: the canonical rewrite orders the data section with every other sweep block before any EMG save, so opening a study is two range requests — header and manifest, then the sweeps in one contiguous read — and a save is one more when its part is first activated.
- **The part index.** The validating pass writes a `CompositePart` row per part from the manifest it has validated: the recording, the part id, modality, test, nerve or muscle, side, technique where declared, sweep count, sampling rates, the stimulation and recording site sets, and whether it carries seeds. A part listing filters them across what the caller can read, with the recording listing's visibility (FAILED and trashed hidden, the release gate, federated terms). The rows add nothing a reader of the file does not already have, so they need no erasure registration and go with their recording by cascade; as high-fanout derived rows they are audited by a digest in the recording's READY audit row rather than one row each.
- **Extraction.** `GET /recordings/{hash}/parts/{part_id}/file` serves a composite file holding that part alone, with the full file's access check, grant terms and serving rules. An extract keeps the original part and sweep ids, so a reader's work codes stay valid against it, and its envelope carries `subset: true` and names no source, a file travelling beyond the access it was served under; the viewer fetches work by the recording the extract was served from. The consecutive-id rule (1c) holds for complete documents only, and ingest refuses a subset. Bulk extraction is a job over a part query, writing one subset file per part. A reader's work follows its part through the part and sweep ids it names, which is how the annotation export and reference generation (point 9) work per part.
- **Derived exports.** A later lifecycle derive job writes EDF per part or per EMG save for tools that read only EDF, the stored file staying the one source. WAV is neither a stored nor an export form, carrying no units, calibration or per-channel metadata.

**8e. Vocabularies on the platform** (2026-11-15).

- **Pinned copies**, each by version and digest with a test failing on drift, as [annotations/core_vocabularies.py](../../annotations/core_vocabularies.py) pins the event vocabularies: the anatomy file, shipped by viewer core and serving more than composite studies, in a vocabulary directory of the `epicurrents` app; the `edx` file and the schema, from the composite module's repository, with the composite code in `recordings/composite/`, where the validating pass reads them.
- **`epicurrents.edx.work` and `epicurrents.edx.finding` register as confined** from the recordings app's `ready()`, the app owning the format, the second also as a vocabulary of readings (point 7). Their validator is point 7's grammar, their `canonicalise` its canonical spelling. Ingest never writes either, accepting closed vocabularies only, so the API is their one writer, and a write under a share token is refused unless `COMPOSITE_WORK_SHARE_TOKEN_WRITES` is on.
- **The write gate is built with it.** A code is accepted only when every part, step and sweep it names exists in the recording, a step in the part named with it and the members of a mean in one step; a marker's index lies below its sweep's sample count; and a marker, response, measure or finding term is valid for the part's test. The part index stores what the gate reads and no more: each part's sweep id range, the ids being consecutive (1c), its steps, and its sweeps' sample counts. The gate runs on the annotation write path for these two standards only, in the write's transaction, and a refusal is a 422 naming the rule, the value never logged.
- **Strict mode is unchanged.** `ANNOTATION_CODE_STRICT_VOCABULARY` stays a project's decision; `edx.work` is validated whatever it says, being registered.

**8f. The assessment** (2026-11-15). Written into [docs/anonymisation-compliance.md](../anonymisation-compliance.md) with a log row when point 8 is built.

| Surface | Removed or replaced | Survives |
|---|---|---|
| Composite file, admitted by the validating pass and served unchanged | Everything outside the schema and vocabularies is refused at ingest: patient and study identification, dates and times, operator and workstation, free text, source names; ids are pattern-bound | Acquisition order; per-sweep parameters (site terms, intensity, pulse width, averages, filters, sampling rate, technique); seeds (device markers, distances, trial selection); samples at full resolution; EMG save lengths |
| Part index and listing | Nothing beyond the file | The file's terms and counts |
| Reader work, `edx.work` codes | Typed text: terms and bounded integers only, by the grammar and the write gate | Every reader's markers, distances, temperatures, selections, links and reviews, to every grantee |
| Reader findings, `edx.finding` codes | Withheld from a caller under a de-identifying grant, their own rows excepted, as text is | Every reader's gradings to the author, raw-grant holders and superusers |
| Reference sets (9f) | Individual observations and covariates: cells are aggregates over a minimum count, with rounded ranges and coefficients | Per-cell models and counts, to whoever the set is shared with |
| Subject covariates (8h) | Withheld from every grantee, share-token holder and federated peer, under any grant; never in the audit trail | Exact values to the recording's author and superusers |

Design gaps to record:

- **Acquisition order fingerprints a lab**, the order of parts being a protocol template, the kind of site signature the EDF pass removes by its canonical channel order. Version 1 keeps it for the reader; a pooled dataset's ingest profile could require a canonical part order, as the submission gate refuses a non-canonical record split.
- **Device parameters fingerprint a vendor** — sampling rates, filter settings, pulse-width defaults — as sampling rate does for EDF; recorded, and constrained by a pool's profile.
- **Distances are anthropometric.** Segment lengths track height, and `distance:` work codes reach every grantee (point 7). Alone a segment length says little about a person; the strong quasi-identifier is its combination with the exact covariates, which reach no grantee (8h) and leave a reference generation run only as aggregates (point 9). Recorded as a gap rather than withheld, since withholding one verb's codes would make the redaction rule per-verb.
- **The covert channel in work codes** is bounded by the numeric ranges and the write gate and rests on an authenticated writer (point 7), or on the deployment's decision to admit share-token writers.
- **The covert channel in the file** is the samples themselves, which no validator can bound, and the low digits of its numeric parameters, which the declared precisions bound (8b). The file comes from a submitter who could as well send the identification by other means; the channel is recorded and not closed.
- **A dataset's manager can reconstruct covariates from aggregates.** A manager reads every member's file and work, so knows each outcome a regression was fitted to; only the covariates are hidden. Coefficients over chosen overlapping subsets solve for them. Four things bound it and none removes it: the author's consent names the dataset, so a manager cannot compose subsets at will (8h); a dataset may declare bands, which are then all a reconstruction recovers (8h); a cell is republished only when its observations have changed by the privacy minimum (9f); and published coefficients are rounded (9f). Recorded as a residual risk of reference generation, against an adversary the author chose to share with.
- **Blinding is partial.** The platform withholds other readers' work under a blind protocol; seeds travel in the file, and their markers are ignored by the viewer alone (9c).
- **The construction claim** is stated with its basis: the file holds no identification because the validator refuses everything else, not as a property of the samples or the parameters.

**8g. Linked imports, the lifecycle, the rest** (2026-11-15).

- `import_recordings --link` validates a composite file without rewriting it, a linked file never being written, and refuses one the canonical rewrite would change — source names, references, drops, a non-canonical spelling. Only a canonical file is linked.
- No reconstruction stages or derive jobs at first: a composite recording is `ready` once ingested. Derived EDF exports join later as a derive job (8d).
- Purge, erasure and preservation are unchanged, a composite recording being one file. The originals volume keeps what was uploaded, as for every format: the canonical file on the viewer route, the vendor input on the fallback route.

**8h. Subject covariates** (2026-11-15). Many reference values are unusable without the subject's age, sex and height, and later weight, so they are stored, exactly.

- **Not in the file**, which is served unchanged to every reader (8d). A `SubjectCovariates` row tied to the recording holds `age` in whole years at the examination, floored, a finer age beside an upload date the author knows approximating a birth date, `sex` from a closed list, `height` in centimetres, later `weight` in kilograms.
- **Exact to the recording's author and superusers only**, through `_can_see_original_name`, the single author-private gate; every grantee, share-token holder and federated peer receives nothing, under any grant. No collaborator needs an individual's covariates, reference generation running on the platform; a raw grant's widening would be a deliberate later change with a contract test of its own.
- **Reference use is the author's decision, per dataset.** The recording's author allows its covariates into reference generation for a named dataset, never for every dataset at once, and a run reads covariates only where its own dataset is named (9b). A global flag would let anyone managing a dataset compose subsets of consenting recordings freely, which is what the reconstruction in 8f needs.
- **Pooled submissions carry bands, never exact values**: age and height in the widths the pool's profile declares, ten years and ten centimetres by default, and sex. The bands are part of the equivalence class a release selector sizes, so the pool's k sees them. Fitting a regression needs no more than a band's midpoint; an exact value is needed only to evaluate a limit for one subject, which the viewer does on values its reader already holds.
- **Any other dataset may declare bands.** A dataset's managers may declare that its runs read age and height in bands of stated widths and never the stored values, which bounds what the reconstruction recorded in 8f recovers to a band. It is an option, a small single-site set fitting better on the values themselves. An author allowing reference use is shown which form the dataset declares, and narrowing the bands or dropping them afterwards ends every allowance given under the earlier form.
- **Never in the audit trail**: the fields are registered with `register_masked_fields(..., withhold=True)`. They are a patient's data rather than a user's, so `erase_subject` would never reach a value written there; the row goes with its recording.
- **Entry.** The viewer's upload step pre-fills them from the vendor export where it holds them — the database export has height, birth date and sex — computes the age in whole years from the birth date and the examination date, neither of which leaves the browser, and sends the confirmed values as structured fields beside the file. This is the one exception to the reader's allowlist (point 2; design note section 6), and it is that narrow: sex, height and the two dates, read only for the upload step, the dates discarded once the age is computed. A converter the platform runs reads none of them. On the fallback route the submitter enters them in the mapping step. Viewing a local file reads them, so regression limits work there too.
- **Use.** Reference generation runs where the exact values are and releases only aggregates over a minimum number of examinations per cell (9f).

**9a. Computation** (2026-11-15).

- **The statistics run in the compute app**, in Python, as a scheduled Celery task, which a dataset's manager may also start, after a large annotation batch for instance. It opens its audited scope with `with_system_activity`. The interface consumes the reference sets it produces and never computes one.
- **The measures are implemented twice, from one definition**: in the viewer, where a reader sees them, and in the compute app, where reference values are made from them. The definition is the written one of point 6, not either implementation; the viewer cannot be made to depend on a Python runtime for a number it shows while a marker moves. The 1h fixtures carry a file of expected measures per sweep that both test suites reproduce, latencies exactly and every other measure within a relative 1e-6 (6a), so a definition changed on one side fails the other.
- **The detector is implemented once**, in the viewer. A run reads reviewed parts only, whose markers are all saved (point 7), so the compute app never places a marker and no second detector has to agree with the first.
- **Versions.** The Python implementation declares the measure-definition version it implements, recorded in the set's provenance with the detector versions the parts were reviewed under. Saved markers are positions, which a new measure definition reads as well as the old, so a version change is recorded and refuses nothing.

**9b. The reference population** (2026-11-15). A dataset's members are the population a set is computed from, and its manager decides what belongs, part by part: normality is a property of a nerve or a muscle and not of an examination, a patient examined for one entrapment having many normal nerves and one that is not. Every part of a member is included unless the manager excludes it or names the ones to include, a selection kept per member on the dataset by part id and audited; whether a part counts as normal is decided by that selection, not by a rule of the job. A later method may lift that requirement: extrapolated reference values (e-norms) estimate the limits from the distribution of all of a lab's data, abnormal studies included, so a run could take a whole clinical dataset rather than one curated as normal. It is recorded here as the direction, not designed. Datasets bring membership, access, a pooled dataset's release gate and the dataset reports with them. The job runs as the system and reads exact covariates only of members whose author has allowed reference use for this dataset (8h), so a manager running over other authors' recordings uses their covariates only with their consent to this use; nothing individual leaves the job (9f).

**9c. Annotation protocol and blinding** (2026-11-15). A reader who sees a number while placing a marker anchors on it, and reference limits made from such markers partly reproduce the expectations that shaped them.

- **Blinding is a mode of the composite resource**, not a display setting: in blind mode every derived measure, deviation and consensus ref answers `null`, a seed's markers and other annotators' layers are not exposed, and covariates read as absent, while inputs — markers, distances, absent flags, findings — work as ever. Every consumer is blinded without cooperating, and the module stays ignorant of the interface. A distance is still entered; the velocity it produces stays hidden.
- **The dataset declares the protocol**: blind or open, and whether marking starts from the detector's estimate or from no markers. The viewer applies it to every recording opened from that dataset, so every annotator works under the same rules.
- **The platform withholds other readers' work.** While a recording is a member a blind-protocol dataset controls (`controls_membership`), a reader who does not manage that dataset receives only their own `edx.work` and `edx.finding` rows on it, on every annotation surface and however they opened the recording. Seeds are in the file, which is served unchanged, so ignoring them is the viewer's alone.
- **A seed's judgements are ignored, its measurements kept.** Most exports carry values from the device, so a blind-protocol dataset admits members with seeds. Under it a seed's markers count for nothing: the detector places on every sweep (6b). A device's gradings are the uploader's findings (6h), withheld from every other reader with the rest of that layer. A seed's distances, temperature and trial selection stand, having been measured or chosen at the examination, where a reader working from the trace afterwards cannot measure them again. A reviewed part's markers are all saved ones, the reader's or the detector's (point 7), so a run over such a dataset reads no seeded marker; its provenance still records how many members carried seeds.
- **The usual protocol is blind, starting from the detector's estimate**, a reader placing the anchors (a motor response's onset) and the detector following (6b). Starting from no markers is the option for work that must not lean on the detector's rules.
- **Provenance records the protocol** (6i), so a set from blinded annotation is told apart from one generated from routine clinical marking.
- **Routine clinical work stays open**: the protocol applies only to a dataset declaring it.

**9d. Annotator layers** (2026-11-15). A reference limit is applied by one reader measuring once, so it must carry one reader's variability; averaging annotators before computing narrows the limits below what any single reader achieves.

- **One observation per examination by default**, from one annotator's layer: the dataset's designated primary annotator where it has one, otherwise one drawn at random with the seed stored in the run's provenance so the draw repeats.
- **Per-rater sets** are an option, one set per annotator from their layer alone, to see whether annotators' sets differ systematically.
- **Only reviewed parts count.** A part contributes from a layer only where that layer has reviewed it (point 7). A part reviewed with no marker moved counts like any other, which keeps the population from favouring the traces the detector got wrong; a part with work and no review contributes nothing.
- **Share-token layers** are read only where the dataset's protocol admits them, and never as the primary annotator.
- **An agreement report is a by-product**, not a set: where examinations carry several layers the run reports per measure the intraclass correlation and the Bland–Altman bias and limits of agreement, reader against reader. Against the detector it reports, per marker term, the share of automatic markers readers kept at review, which the saved origins give without a detector in the compute app; a detector-only set waits for one.
- **Absent responses are never excluded by default.** A response a reader marked absent is an observation of its cell, and whether a run's amplitude and area cells include them is declared for the run, with no default, and recorded in its provenance (9e). A part whose response is missing for a technical reason is left out by the manager's part selection (9b), one part at a time. A latency or a velocity has no value on an absent response, which a cell counts either way.

**9e. Statistics** (2026-11-15). One default method, versioned as a whole and named in a set's provenance (`default@1`). Nothing in it is chosen from the data a run reads.

| Stage | Default |
|---|---|
| Cells | Test, nerve or muscle, stimulation, recording, technique and measure, a segment measure adding its segment. An undeclared technique is a value of its own, never merged with a declared one. Sides are pooled, side difference having cells of its own |
| Selection | One observation per examination and cell: one annotator's layer (9d), and where an examination holds the cell's measure more than once, both sides or a repeated part, one of them drawn under the run's stored seed. A side-difference cell takes its one observation from the pair |
| Transform | Declared per measure term in `edx`: log for amplitudes and areas, none for latencies, durations and velocities |
| Covariates | Declared per measure term in `edx` and always fitted, never kept or dropped by significance: age; height for measures depending on limb length (distal and F latencies, F-M intervals); sex; the response distance for a response latency (6g); limb temperature (6g). Where fewer observations carry the distance or the temperature than the model's minimum, it is left out and the cell says so (`distance_unadjusted`, `temperature_unadjusted`); otherwise the observations lacking it are excluded and counted |
| Outliers | None excluded. Observations beyond Tukey fences on the transformed residuals are counted, and the cell also records the limits computed without them |
| Absent responses | Declared for the run, `included` or `excluded`, with no default. Excluded, they are counted and the cell's limits describe present responses only, which the cell says (`absent_excluded`). Included, an amplitude or area cell is a mixture: with a share p of its observations absent, the lower limit is the prediction limit over the present responses at the level (0.025 − p) / (1 − p), and where p reaches 0.025 the cell has no lower limit, an absent response lying within reference (`absence_within_reference`). p is the cell's share, not modelled on the covariates in this method. The statistical minimum counts present responses |
| Model | Linear regression on the declared covariates. A cell of at least 120 observations also records non-parametric percentiles beside it |
| Limits | One-sided, by the abnormal direction each measure term declares in `edx` (amplitudes and velocities low, latencies and durations high, side differences large), two-sided only where a term says so: the regression's prediction limit at the 2.5th or 97.5th percentile, the prediction plus or minus the t-quantile times the standard error of a new observation, leverage included. Side-difference cells give an upper limit on `side_difference` and a lower limit on an amplitude's `side_ratio` |
| Spread | The residuals are tested for a spread that changes along the prediction; a cell where it does is marked `heteroscedastic`, its limits being too wide at one end of the covariate range and too narrow at the other |
| Acceptance | The minimum count (9f) and the covariate ranges, outside which a regression limit reads `null` |

- **No registry of methods in the first build.** The method is code in the compute app. A second method is what would justify registering steps under keys, and a set already names the method that made it, so sets by a later method compare side by side as active sets (6i).
- **The release rule is not the model's.** What leaves the job is checked after the model by code apart from it: the cell-record schema with no per-subject field, the minimum count per cell, and the rounding of 9f.
- **A cell records** n, the excluded, outlying and absent-response counts, the absent-response rule, the method, the transform, the coefficients with their covariance matrix, which the prediction limit needs, the residual standard deviation, the covariate ranges, the direction, its flags, and what the model does not adjust for: the distinct filter settings of its observations and the shares carrying a distance and a temperature.
- **Limits travel as a model family the viewer evaluates exactly**: linear, log-linear, fixed bounds, percentiles. A tabulated form for methods outside those families waits with the registry.

**9f. Release and distribution** (2026-11-15).

- **Two minimums per cell.** A statistical one, below which a limit means nothing (40 observations plus 10 for each fitted covariate by default, 120 for percentiles), and a privacy one, below which aggregates describe individuals: `REFERENCE_CELL_MIN_EXAMINATIONS`, a deployment setting defaulting to 10, distinct from a pool's k, which is a class size. A cell is published only when it meets both; otherwise it appears uncomputed with its count. With one observation per examination (9e) the two count the same thing. The minimum counts examinations, not subjects: the platform holds no subject identity across examinations, by design, so a patient examined twice counts twice, and within a pool a cell's spread over contributors is unknowable, ledgers and recordings being unjoinable. Both are recorded limitations.
- **What a cell must not reveal.** A cell's exact covariate minimum and maximum are two individuals' values, so the recorded range is rounded outward to 5 years and 5 centimetres; the 120-observation threshold keeps an extreme percentile an interpolation between several observations; coefficients, their covariance and the residual standard deviation are aggregates, published to four significant digits, which costs a limit nothing and blunts the reconstruction recorded in 8f. The release check (9e) applies these after the last step, whatever method made the cell.
- **Reference sets are platform objects.** A `ReferenceSet` row carries its key, version, provenance — the dataset, each stage's `key@version`, the annotation protocol, the layer rule and its seed, the absent-response rule, the measure-definition version — and its cells. A new run is a new version, and earlier versions stay, since deviations name `key@version`. Sets are shared as datasets are, through `AccessRight` rows, so the model declares the reverse generic relations the GenericFK rule asks for; a set exports as a JSON file for another deployment or a local viewer, no more sensitive than the set, being aggregates. A reader loads the sets they can read by key and activates them (6i).
- **Pooled datasets.** A run reads released members only, under the release gate. Pooled members carry covariate bands (8h), and a regression over them is fitted on the bands' midpoints, the cell recording the band widths. A run over any other dataset declaring bands does the same.
- **Who runs it.** A dataset's managers and superusers, over the covariates of members whose authors allowed reference use for that dataset (8h); the dataset reports list the runs made over a dataset for its other managers to see.
- **Versions and staleness.** A set's versions are immutable. A new version republishes a cell in two cases only: the observations behind it differ from the last published by at least the privacy minimum, added and removed counted together; or its members are unchanged and their work or the measure definitions are not, re-annotation isolating no member. Otherwise the cell is carried forward as it was, so no two versions of a cell difference down to fewer members than the minimum. The rule is per cell: a membership changed by ten examinations may change one cell by a single observation.
- **Members.** A run's provenance keeps its member set, visible to the set's managers only. A member withdrawn, vetoed, erased or purged marks every cell computed from it stale, shown in the viewer and the set listing, and the next run excludes it. When its recording is deleted the member is scrubbed from every member set naming it, which then holds a count in its place.
- **Audit.** A run audits as `compute.reference.generate`, its target the set, with ids and counts only.

