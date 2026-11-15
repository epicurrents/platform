# Composite module — specification plan

**Status:** started 2026-11-14. Resume at the first point whose status is not *decided*.

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
| 5 | The vocabulary files | first slice | decided; marker and measure terms come with point 6 |
| 6 | Measurement definitions in the NCS module | measures and bindings | open |
| 7 | Marker and distance persistence | saving a reader's work | open |
| 8 | Platform side: modality, validating pass, serving, assessment | storing a composite study | open |

Points 1 to 5 are what the first slice needs: a reader, the composite resource, the parts as plain resources without measurements, the child-builder registry and the default child. Points 6 and 7 add measurements and bindings; point 8 can run in parallel with any of them.

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

The marker set per test type; amplitude (baseline-to-peak or peak-to-peak), area and duration bounds; conduction velocity; F-wave persistence and its threshold; H-reflex measures.

### 7. Persistence

Whether markers and distances persist as codes of a confined `epicurrents.edx.measure` vocabulary or as labels (design note section 8), and how a seed relates to a saved marker.

### 8. Platform side

The `composite` modality, the validating de-identification pass and its version, the serve-pipeline entry, the given-data rows in the anonymisation assessment, and the mapping step during upload (design note sections 5 and 7).

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

Validation is strict at ingest and lenient in a reader. Ingest refuses an unknown key and rewrites the manifest canonically. A reader refuses a major `version` it does not know and ignores unknown optional fields within a known major, so an older viewer opens a file that gained an optional field.

**1c. Ids** (2026-11-14). A part holds data elements, and format version 1 defines one element kind, the sweep; nearly everything an electrodiagnostic examination records is one, but a later test type may carry data of another shape, so the id scheme and the part's layout are not closed to it.

- Every id is a kind prefix followed by a sequence number: `p` for parts, `s` for sweeps, `^p[1-9][0-9]*$` and `^s[1-9][0-9]*$`. A new element kind is a new prefix, registered in the schema with its own array on the part, and leaves existing ids and files valid.
- Each kind is numbered in document order across the whole document, not per part, and ingest refuses a file whose ids are not consecutive. A converter assigns them in the order it writes the elements.
- Ids are unique across the document, so a saved marker or `defaultChild` names an element without a path. A binding names a part and a protocol step instead, so that it follows the selected trial (carried into 1e).
- An id carries no meaning beyond the order the document already has, and the pattern keeps it from carrying text the vocabulary check would not see. It also keeps a part id from colliding with a derived-child term in `defaultChild`.
- A stored file is never rewritten, so its ids are stable and a saved marker can refer to its element for good.
- Segments carry no id; a segment distance names its two sweeps (1f), since one site can be stimulated more than once in a part.

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

Nothing else sits on a part: no label, which comes from the terms, and no free text. Temperature is in neither export and would be an optional number added later.

**1e. Sweep fields** (2026-11-14).

A sweep's role in its part is three fields, and ingest refuses two sweeps of one part with the same three:

| Field | Required | Rule |
|---|---|---|
| `step` | no | Protocol step within the part, counting from 1; a sweep without one is its own step. |
| `trial` | no | Trial within the step, counting from 1; default 1. |
| `split` | sensory | `odd` or `even` where the device stores split averages; absent otherwise. |

Selection is per step: the device's chosen trial is a seed naming a trial number (1f), and a sensory pair is selected whole. A series (F-wave, H-reflex) has one sweep per step and its measurements use every step.

Every sweep:

| Field | Required | Rule |
|---|---|---|
| `id` | yes | `s<n>` (1c). |
| `samplingRate` | yes | Hz. |
| `data` | yes | `{ offset, samples }`. Samples are float32 microvolts for every sweep, a property of the format rather than a field. |
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

On a sweep, marker positions in seconds from the start of the sweep's data, the convention of `NcsChannelMarker.position` (provisional until more vendors' exports have been seen; the alternative is latency from the stimulus); a converter adds `triggerOffset` back to a stimulus-relative latency, so a seeded marker and a hand-placed one become latencies the same way. A marker the device left unset is absent. Marker names are vocabulary terms per test (point 5); a conduction sweep's set is `onset`, `negative_peak`, `positive_peak` and `end`.

```json
"seed": { "markers": { "onset": 0.01110, "negative_peak": 0.01450, "positive_peak": 0.01620, "end": 0.01690 } }
```

On a part, distances in millimetres and the selected trial of each step that has more than one:

```json
"seed": {
    "distances": [
        { "from": "s3", "mm": 72 },
        { "from": "s3", "to": "s5", "mm": 230 }
    ],
    "selected": [ { "step": 2, "trial": 1 } ]
}
```

A distance with `from` alone runs from that sweep's stimulation site to its recording site; with `to`, between the stimulation sites of two sweeps. Both sweeps are in the part, and `mm` is positive. A field with nothing to carry is absent.

A seed never holds a value computed from these numbers. Ingest checks every sweep id it names against the part and every marker name against the test's terms. A seed is not edited: a reader's moved marker or changed distance is saved as their own (point 7), and the seed stays in the stored file as the source's record.

**1g. Source names and name maps** (2026-11-14). The platform vocabulary holds canonical names only, and every name a stored file carries is one of them; the uploader is where a source's labels meet it.

A converter's output carries the names it could not resolve in `sourceNames`, an array of distinct strings after NFC normalisation, and a name field refers to one by index:

```json
"sourceNames": ["Medianus", "Ranne", "APB", "Oikea"],
"parts": [ { "id": "p1", "side": { "source": 3 }, "nerve": { "source": 0 },
             "sweeps": [ { "id": "s1", "stimulation": { "source": 1 }, "recording": { "source": 2 } } ] } ]
```

- Five fields may hold a reference in place of a term: `side`, `nerve`, `muscle`, `stimulation`, `recording`. `test` may not; the converter reads it from the export's type field and the submitter can correct it.
- A converter writes a term itself only where it resolves one deterministically, from a device's factory identifier; it never guesses from a typed label.
- A name is mapped per kind of field, so one string used as a nerve and as a site is two questions.
- Resolution runs in order: the name map the upload names, if any; exact matching on the vocabulary (design note section 4); the submitter, for whatever is left, mapping each name or marking a part or sweep to be dropped.
- Ingest then replaces every reference with its term, removes `sourceNames`, removes dropped sweeps with their data blocks, renumbers ids and offsets to stay consecutive (1c), and drops any seed entry naming a dropped sweep. All of it precedes storing; a stored file is never rewritten, and ingest refuses one that still carries a reference or `sourceNames`.
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

- **3a. One NCS resource per part, any number of recording sites.** A part may record from several sites at once — sweeps sharing step and trial with different `recording` are simultaneous responses to one stimulus — which nerve anastomosis studies, blink reflexes and somatosensory evoked potentials need. A part's `nerve` is the stimulated nerve, the recorded muscle may be supplied by another, so no per-sweep nerve override is needed (the design note's anastomosis question). A later minor version adds an optional sweep `recordingSide`, defaulting to the part's `side`, which then reads as the stimulated side; a blink reflex needs it. In the NCS module, measurements move from the stimulation site to the response, which is the channel (one stimulation site and one recording site); a site is a stimulation point without measurements; segments are grouped by recording site, two stimulation sites seen from one recording site, as `NcsNerveSegment.recordingSite` already expresses. Grouping by recording site rather than splitting the part is also the fix for the vendor API reader's motor sections collapsing. The type changes belong to points 4 and 6.

- **3b. One channel per sweep.** The channel's name is the sweep id, so markers and bindings map back to the document; its label is the stimulation site's display name, or the source name where no term is set. Every trial is a channel; trials not selected are hidden.
- **3c. Sensory pairs.** Both halves are channels, hidden by default, and the composite module adds a computed mean channel (`averaged: true`), which is what is shown and measured. The halves weigh equally until the design note's split-average question is settled.
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
    /** Sweep ids, as in a seed's distances (1f). */
    segment?: { from: string, to?: string }
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

