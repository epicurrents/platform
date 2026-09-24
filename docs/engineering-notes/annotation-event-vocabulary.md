# Annotation event vocabulary — the acquisition set

**Status:** v1.2, 2026-09-24. Design settled and built through the vendor mappings; what remains is the viewer's EDF export carrying the codes, and the documentation at release. Last privacy item on the [ROADMAP](../../ROADMAP.md) ("annotation event vocabulary still fingerprints the acquisition software") and Phase 4 of the [channel de-identification plan](channel-deidentification-plan.md).

**This is the initial vocabulary.** It carries what a reader needs to explain a change in the signal by something that happened in the room, and no more. Terms will be added along the way, and the crosswalk columns filled, as SNOMED CT concepts are adopted for them; adding a term is an entry in a JSON file and a log row below, not a redesign.

## TL;DR

The platform withholds the free text of an event from a de-identifying recipient since Phase 5 of the [anonymisation plan](anonymisation-compliance-plan.md), so an event whose meaning lives only in its `name` reaches such a recipient as a bare timestamp. A `Code` whose `standard` is registered crosses that boundary, because its `value` names a concept from a closed set rather than describing a subject. The vocabulary's purpose is therefore to give the events that explain a recording a coded form: eyes closed, hyperventilation, a trigger, a drug, a change of position, a loss of consciousness.

The vocabulary lives in the viewer, which is where events are made and read, as a shared modality-neutral set on `GenericBiosignalEvent` extended by `EegEvent`. Each set is a JSON file shipped by its package; the platform pins a copy of each and registers it as a vocabulary (`epicurrents.biosignal`, `epicurrents.eeg`). The Epicurrents code is the value; DICOM, IEEE 11073 and SNOMED CT identifiers are crosswalk columns, informational and optional. Ingest translates a vendor's event string into a coded `Event` row where a mapping exists and fails closed where none does.

## 1. The facts that shape the design

- **Core registered no vocabulary at the time of the design.** [annotations/vocabularies.py](../../annotations/vocabularies.py) was a registry with zero entries; ingest wrote one `Annotation` blob per recording ("Original annotations" from the EDF+ TALs, "Source events" from a converter sidecar) and no `Event` rows. `RECORDINGS_DISCARD_EMBEDDED_ANNOTATIONS` is the only control, and it is all-or-nothing.
- **The redaction rule makes a code worth more than a name.** Under a grant carrying `apply_middleware`, `Event.name`, `value` and a code's free-form `meta` are withheld; a code's `standard` and `value` are served. So the same event, coded, explains the signal to every reader, and, named only, to the author.
- **The viewer already has the mechanism.** `GenericAnnotation` carries `CODED_EVENTS`, `extendEvents`, `addStandardEventCodes`, `getEventForCode` and `getEventForLabel` over an empty table, and `EegEvent` re-implements all five over its own table of activation procedures and findings, with DICOM CID 3035 and IEEE MDC identifiers. Nothing else in the viewer or the platform calls any of them, so the shape is free to change.
- **An annotation carries one code per standard.** The viewer's `codes` is `Record<standard, value>`, and the platform's `Code` row is unique per `(target, standard)`. An event therefore names one concept per vocabulary, which is what a closed-set term is for; a second concept is a second event.
- **Findings are not the subject.** Spikes, sleep stages and semiology are what a reader concludes; this vocabulary is what a reader is told happened. The EEG finding categories stay in `EegEvent` for the viewer's own use and are covered, for external standardisation, by the [HED-SCORE](hed-score-integration.md) plugin. Sleep deprivation is a property of the recording, not an event, and belongs on a profile.

## 2. Layout in the viewer

`GenericAnnotation` keeps the statics, made class-aware: each walks `this.CODED_EVENTS`, so a subclass that overrides the getter is searched through its own view, and `extendEvents` writes into the table that owns the category it names. They were arrow-function properties bound to the base class, which is why `EegEvent` had to copy them; as methods they inherit.

`GenericBiosignalEvent` declares the shared set, four categories loaded from the file biosignal-events.json in a vocabulary folder beside the annotation classes in the core package. `EegEvent` declares its own, eeg-events.json beside its class in the EEG module, and its `CODED_EVENTS` getter returns the parent's categories followed by its own. Category names are unique across the chain: a subclass cannot shadow a parent category, and extending a shared category through `EegEvent` extends it for every biosignal event class, which is the intended way to add a shared term from outside.

Each JSON file names its standard and version at the top and carries categories, each with a scope, keyed terms and a description. A term is a `CodedEventProperties` object: `code`, `name`, optional `description`, `class` (the event class it is created with), `meta` (the keys a `Code.meta` for this term is expected to carry, each with a one-line meaning) and `standardCodes` (the crosswalk). The two new optional fields on the type are the only type change.

Code prefixes: `BIO_TECH_`, `BIO_INT_`, `BIO_OBS_` for the shared set; `EEG_ACT_` and the existing finding prefixes for EEG. A code never changes once shipped; a term that turns out wrong is deprecated in place with a `description` saying what replaced it.

## 3. The shared set (`epicurrents.biosignal` 1.0)

Event `class` decides display priority in the viewer (`technical` 100, `event` 400) and is stored as `Event.event_class` on the platform.

**TECHNICAL** (`class: technical`)

| Code | Name | Notes |
|---|---|---|
| `BIO_TECH_CALIBRATION` | Calibration | Span or instant. |
| `BIO_TECH_IMPEDANCE` | Impedance check | Span or instant; readings, if recorded, in `meta.readings` keyed by channel label. |
| `BIO_TECH_MONTAGE_CHANGE` | Montage or reference change | State-like: instant marks the change. `meta.montage` names the new one only if it is a canonical name. |
| `BIO_TECH_PAUSE` | Recording paused | Span covers the pause; instant marks its start. |
| `BIO_TECH_RESUME` | Recording resumed | Instant. |
| `BIO_TECH_TRIGGER` | Trigger | Instant; `meta.number` carries the trigger or stimulus number. |
| `BIO_TECH_VIDEO_START` | Video start | Instant. |
| `BIO_TECH_VIDEO_STOP` | Video stop | Instant. |
| `BIO_TECH_ELECTRODE_FAULT` | Electrode fault | Span or instant; `meta.electrode` names the electrode by its canonical label. A loose, dried, bridged or detached electrode makes an artifact that can pass for cerebral activity, so it is its own term. In the viewer the event is also bound to the channel through its channel list; the index is not carried on the platform, whose ingest reorders channels. |
| `BIO_TECH_ELECTRODE_FIX` | Electrode fixed | Span or instant; `meta.electrode` as above. The handling itself, rubbing and pressing, makes artifacts of its own, so a span covers it. |
| `BIO_TECH_FAULT` | Technical fault | Span or instant; `meta.component` for the part affected other than an electrode (amplifier, cable, software). |

**INTERVENTION** (`class: event`)

| Code | Name | Notes |
|---|---|---|
| `BIO_INT_MEDICATION` | Medication given | Instant; `meta.drug`, `meta.dose`, `meta.route`. |
| `BIO_INT_CARE` | Care and handling | Span or instant: turning, suction, washing, examination. `meta.action`. |
| `BIO_INT_PATIENT_BUTTON` | Patient event button | Instant; the patient pressed the marker. |
| `BIO_INT_PROCEDURE` | Procedure | Span or instant; `meta.procedure` names it when none of the enumerated ones fits. |
| `BIO_INT_CPR` | Cardiopulmonary resuscitation | Span. |
| `BIO_INT_DEFIBRILLATION` | Defibrillation or cardioversion | Instant; `meta.energy`. |
| `BIO_INT_INTUBATION` | Intubation or extubation | Instant; `meta.action` is `intubation` or `extubation`. |
| `BIO_INT_NEEDLE` | Blood sampling or needle procedure | Instant. |
| `BIO_INT_OPERATION` | Operative procedure | Span; `meta.stage` (incision, clamp, reperfusion, closure) where the stage matters to the reading. |

Procedures are enumerated only where knowing which one changes the reading; everything else is `BIO_INT_PROCEDURE` with the specifics in `meta`, which the redaction rule treats as text.

**OBSERVATION** (`class: event`)

| Code | Name | Notes |
|---|---|---|
| `BIO_OBS_MOVEMENT` | Movement | Span or instant; `meta.part` for the body part. |
| `BIO_OBS_TALKING` | Talking | Span or instant. |
| `BIO_OBS_COUGHING` | Coughing | Instant. |
| `BIO_OBS_CRYING` | Crying | Span or instant. |
| `BIO_OBS_POSITION_SUPINE` | Supine | State-like. |
| `BIO_OBS_POSITION_PRONE` | Prone | State-like. |
| `BIO_OBS_POSITION_LEFT` | Left lateral | State-like. |
| `BIO_OBS_POSITION_RIGHT` | Right lateral | State-like. |
| `BIO_OBS_POSITION_UPRIGHT` | Upright | State-like: sitting or standing. |
| `BIO_OBS_POSITION_HEAD_DOWN` | Head down | State-like: the Trendelenburg position. |
| `BIO_OBS_LOC_ALERT` | Alert | State-like. |
| `BIO_OBS_LOC_VOICE` | Responds to voice | State-like. |
| `BIO_OBS_LOC_CONFUSED` | Confused | State-like: responds to voice, disoriented. |
| `BIO_OBS_LOC_SOMNOLENT` | Somnolent | State-like: responds to voice, drifts back. |
| `BIO_OBS_LOC_ASLEEP` | Asleep | State-like: the behavioural observation, not the EEG stage. |
| `BIO_OBS_LOC_PAIN` | Responds to pain | State-like. |
| `BIO_OBS_LOC_UNRESPONSIVE` | Unresponsive | State-like. |

**ENVIRONMENT** (`class: event`)

| Code | Name | Notes |
|---|---|---|
| `BIO_ENV_DISTURBANCE` | Mechanical disturbance | Span or instant; the bed, a cable or the equipment bumped, pulled or moved. `meta.source`. |
| `BIO_ENV_ELECTRICAL_DEVICE` | Electrical device nearby | State-like; the usual source of mains interference. `meta.device`. |
| `BIO_ENV_LIGHT` | Light change | State-like; `meta.change` (on, off, dimmed, daylight). |
| `BIO_ENV_NOISE` | Noise | Span or instant; a sound other than speech. `meta.source`. |
| `BIO_ENV_PERSON` | Person entering or leaving | Instant for the entry or exit; a span covers a stay. |
| `BIO_ENV_VOICES` | Voices nearby | Span or instant; speech not addressed to the patient. Speech addressed to the patient is verbal stimulation, and the patient speaking is an observation. |

External influences are what reaches the signal or the patient without anyone intending it: the distinction from INTERVENTION and from the activation set is intent, and from OBSERVATION it is that the patient is not the source.

Body position is an observation because it originates with the patient as often as with the examiner (a turn during polysomnography, a head-down tilt after a faint), and its values are positions rather than a change event. Level of consciousness is the behavioural state on the AVPU scale with three qualified forms of "responds to voice" as values of their own, because a value crosses the redaction boundary and a qualifier in `meta` does not; a GCS or RASS score goes in `meta.gcs` or `meta.rass`.

**The instant-or-span convention**, stated once for every state-like term: an event with duration 0 marks entry into the state, which holds until the next event of the same family; an event with a duration bounds the state to that span. A recording that was supine throughout carries one instant at 0 seconds. The families are position, level of consciousness, eyes, video, montage, light and electrical device.

## 4. The EEG set (`epicurrents.eeg` 1.0)

`EegEvent` keeps its existing ACTIVATION category (eyes closed and open, hyperventilation with start and end, photic stimulation enumerated per frequency to mirror the viewer's markers) and gains the sensory stimuli a technologist applies to provoke a change, kept apart the way ACNS reports them:

| Code | Name | Notes |
|---|---|---|
| `EEG_ACT_STIM_AUDITORY` | Auditory stimulation | Instant; a sound, not speech. |
| `EEG_ACT_STIM_VERBAL` | Verbal stimulation | Instant; calling the patient. |
| `EEG_ACT_STIM_TACTILE` | Tactile stimulation | Instant. |
| `EEG_ACT_STIM_NOXIOUS` | Noxious stimulation | Instant; `meta.method` (nail bed, sternal rub, trapezius). |
| `EEG_ACT_STIM_VISUAL` | Visual stimulation | Instant; non-photic, such as a pattern or a threat. |
| `EEG_ACT_PASSIVE_EYE_OPENING` | Passive eye opening | Instant; the examiner opens the eyes. |

The finding categories (ARTIFACT, BACKGROUND, EPILEPTIFORM, PAROXYSM, SLEEP, TRANSIENT) stay in the file and the JSON with `"scope": "finding"` on the category, and the platform's `epicurrents.eeg` validator accepts only the categories scoped `acquisition`. That keeps the registered vocabulary to what this note is about and leaves the findings to HED-SCORE; widening it is one entry in the category list, not a design change.

Three corrections to the existing EEG table, checked against CID 3035 on 2026-09-24: eye movement carried the external-interference code (2:24272) and an EOG identifier, and now carries 2:24040 `MDC_EEG_EXT_CRTX_EYE_MVMT_MULT`; "Polysike" and "Sharply countered" are spelt. Two things that look wrong and are not: slow eye movement (2:24064) shares the nystagmoid reference identifier in the standard's own table, and chewing and swallowing share 2:24264 because the standard has one code for both.

## 5. Crosswalk policy

The Epicurrents code is the value the platform stores and validates. `standardCodes` maps it to `dicom` (the CID 3035 code string), `ieee` (the MDC reference identifier) and `snomed` (the SNOMED CT concept id) where one exists. CID 3035 has no code for calibration, impedance, montage change, photic stimulation or any intervention, so the shared set's DICOM column is empty at 1.0; its eyes-open and eyes-closed codes are EOG movement codes and stay as they are.

A crosswalk identifier is entered only after a checked lookup in the standard's own browser, and the log row that adds it says so. None of the SNOMED CT ids that were candidates on 2026-09-24 could be checked unattended (the public browser refuses non-interactive queries), so the initial list ships with an empty SNOMED column rather than an unverified one. A wrong crosswalk id in a shipped vocabulary is worse than a missing one.

## 6. The platform side

Viewer first, platform second: nothing here is touched until the JSON files exist in the two packages.

**Pinned copies.** The two files under [annotations/vocabulary/](../../annotations/vocabulary/) are byte copies of the viewer files, and [annotations/core_vocabularies.py](../../annotations/core_vocabularies.py) pins each copy's version and digest; [annotations/tests/test_core_vocabularies.py](../../annotations/tests/test_core_vocabularies.py) fails when a copy drifts from its pin and, where the viewer checkout is present, from the viewer's file, so a term added on one side without the other fails the suite. The viewer is the source of truth; the platform copies so that a deployment validates without a build of the viewer at hand.

**Two registered standards.** `epicurrents.biosignal` and `epicurrents.eeg`, registered from `AnnotationsConfig.ready()` with a membership validator over the pinned copies; the EEG validator delegates an unknown value to the shared one, so an EEG event may carry a shared term under either standard. This changes the sentence in [annotations/README.md](../../annotations/README.md) that core ships zero vocabularies: core ships its own two and no external one, and [annotations/tests/test_code_vocabulary.py](../../annotations/tests/test_code_vocabulary.py) proves the mechanism with a third registered inside the test.

**Ingest translation.** The registry designed in [hed-score-integration.md §8](hed-score-integration.md), built as [recordings/event_translation.py](../../recordings/event_translation.py) and consulted from `save_sidecar_events` and from the TAL path in `_save_edf_results`. A mapper answers a code, and the standard is the one that owns the term, resolved through `find_acquisition_term` in [annotations/core_vocabularies.py](../../annotations/core_vocabularies.py), rather than picked by modality as first designed: the code's prefix says which vocabulary it belongs to, and a shared term on an EEG recording is written under `epicurrents.biosignal` whichever class the viewer reads it through. The rule is fail-closed: a source event a mapper translates becomes an `Event` row with `name` set to the term's `name`, `event_class` to its class and a `Code` carrying the standard and code; a source event nothing translates, or that a mapper translates to a code no pinned vocabulary has, becomes a placeholder `Event` named `Source annotation` or `Source event` by its kind, timed and carrying no text. The raw vendor string is written nowhere but the raw record the seams already keep, the "Original annotations" and "Source events" bundles. A correction to the design as first written: those bundles are not author-private the way `SignalInfo.source_*` is; they are system-authored rows that follow the annotation-text rule, served under a raw grant and withheld under a de-identifying one, which is what the raw grant is for, since the stored file has its text stripped. Making them author-only would take the file's own annotations from every raw-grant collaborator, and is a separate decision. Two kinds of mapper, in order: callables registered with `register_event_translation(mapper, name=...)` from an `AppConfig.ready()`, and JSON tables named in `RECORDING_EVENT_TRANSLATIONS`, read at `manage.py check`, which is how a mapping ships with a converter the platform runs at arm's length: as data beside the program, never in core, because a mapping from a Nicolet event type is a fact about Nicolet. `RECORDINGS_DISCARD_EMBEDDED_ANNOTATIONS` skips the raw record and the placeholders and keeps the translated events, which is what a teaching deployment needs: the condition markers, without the vendor's vocabulary or the file's text. Mechanics in [recordings/README.md → Event translation](../../recordings/README.md#event-translation), tests in [recordings/tests/test_event_translation.py](../../recordings/tests/test_event_translation.py).

**Vendor mappings.** Two consumers of one mapping, because most recordings reach the platform after conversion in the browser rather than on the server. The viewer's Nicolet reader resolves each event record's type GUID to the built-in name (its format spec, Appendix C, now carries the full GUIDs, recovered by observation from real files) and translates the acquisition types to terms: eyes open and closed, hyperventilation, a photic train at the frequency its text states, impedance check, recording paused with the length its text states, movement, talking, crying, drowsy, asleep, the patient button, an electrode fixed, the amplifier disconnected, video start and stop, lights on and off. The term's code rides on the event under its standard and the record's own text stays the display value; finding markers, software states and site-defined types get no code. The EDF export does not yet write the codes into the file it produces, which is the remaining step for the browser path: until it does, an exported recording arrives with the vendor's strings as TALs and the platform's tables translate them. For the server-side converter path the mapping is a JSON table in the platform's own format, written against the converter's sidecar and shipped beside the vendored converter under the git-ignored converters directory, never in the repository. Writing it showed two things. The sidecar converter writes each event into the EDF as an annotation record as well as into the sidecar, so a converted upload reached both ingest seams and every event was written twice; the sidecar now owns the rows when it carries any, on both ingest paths. And the observed photic protocol runs at 14 and 16 Hz, which the vocabulary has no per-frequency term for; the table and the reader fall back to the unqualified photic term, with the frequency in the code's metadata on the platform, rather than assemble a code no vocabulary has. Adding the two frequencies to the EEG set is a vocabulary decision for the viewer first.

**Open, deliberately.** A validated identifier in `meta` (a SNOMED id for the drug given) is treated as text by the redaction rule today. A narrow exemption for a `meta` key whose value is checked against a registered vocabulary may be warranted; it is not part of this work.

## 7. Sequencing

| Step | Where | Size | State |
|---|---|---|---|
| A. Class-aware statics, shared JSON and `GenericBiosignalEvent` table, type fields, tests | viewer core | M | built 2026-09-24 |
| B. EEG JSON, sensory stimuli, table corrections, statics removed, merged getter, tests | viewer eeg-module | S | built 2026-09-24 |
| C1. Pinned copies, two registered standards, digest test, README | platform annotations | S | built 2026-09-24 |
| C2. Translation registry, `Event` rows with codes, raw record kept, fail-closed placeholder | platform recordings | M | built 2026-09-24 |
| C3. Vendor mappings: the Nicolet `.e` reader in the viewer and the table for its server-side converter | viewer nic-reader, deployment data | S | built 2026-09-24 |
| D. The viewer documentation's annotations page, coded annotations section | docs submodule | S | at release |

Steps A and B change published surface in two viewer packages: `CodedEventProperties` gains two optional fields and `GenericBiosignalEvent` gains a table, which is an addition and part of core's pending 2.1.0 release; `EegEvent` loses five statics it now inherits with the same signatures, which is not a change to callers.

## Log

| Date | Change |
|---|---|
| 2026-09-24 | v1.0. Design from the 2026-09-24 discussion written up; DICOM crosswalk checked against CID 3035; SNOMED column left empty pending checked lookups. Steps A, B and C1 built the same day; electrode fault and electrode fixed added to the shared set on review, the electrode carried by its canonical label because ingest reorders channels; ENVIRONMENT category added for external influences. |
| 2026-09-24 | v1.2. Step C3 built: the viewer's Nicolet reader names every built-in event type by GUID and codes the acquisition ones with vocabulary terms; the server-side converter's table written against its sidecar and validated against the real files' events. Two findings: a converted upload reached both ingest seams and doubled its events, fixed by letting the sidecar own the rows; 14 and 16 Hz photic trains have no per-frequency term and take the unqualified one. |
| 2026-09-24 | v1.1. Step C2 built: mappers and JSON tables, `Event` rows with a `Code` under the owning standard, text-free placeholders, the raw record kept under the annotation-text rule. Two corrections to section 6 as first written: the standard is resolved from the term rather than the modality, and the raw record is not author-private but follows the annotation-text rule, which is left as it was. The sidecar schema now accepts a null `type` or `label`, which the Nicolet converter writes and the pinned schema had refused. |
