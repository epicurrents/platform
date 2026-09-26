# Annotation event vocabulary — the acquisition set

**Status:** v1.11, 2026-09-26. Design settled and built through the vendor mappings, the container the viewer's export produces for the platform, submitting from the viewer, the coded labels of the footer and the teaching project's submission path; what remains is planned in section 8: the teaching project's release side, and the documentation at release. Last privacy item on the [ROADMAP](../../ROADMAP.md) ("annotation event vocabulary still fingerprints the acquisition software") and Phase 4 of the [channel de-identification plan](channel-deidentification-plan.md).

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

`GenericBiosignalEvent` declares the shared set, five categories loaded from the file biosignal-events.json in a vocabulary folder beside the annotation classes in the core package. `EegEvent` declares its own, eeg-events.json beside its class in the EEG module, and its `CODED_EVENTS` getter returns the parent's categories followed by its own. Category names are unique across the chain: a subclass cannot shadow a parent category, and extending a shared category through `EegEvent` extends it for every biosignal event class, which is the intended way to add a shared term from outside.

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

**PHYSIOLOGY** (`class: event`)

| Code | Name | Notes |
|---|---|---|
| `BIO_PHYS_BRADYCARDIA` | Bradycardia | State-like; `meta.rate`. SCT 48867003. |
| `BIO_PHYS_TACHYCARDIA` | Tachycardia | State-like; `meta.rate`. SCT 3424008; MDC_EVT_ECG_TACHY. |
| `BIO_PHYS_ARRHYTHMIA` | Arrhythmia | Span for a run, instant for a beat; `meta.rhythm`. SCT 698247007; MDC_EVT_ECG_ARRHY. |
| `BIO_PHYS_ASYSTOLE` | Asystole | Span; a pause longer than the rhythm allows, up to standstill. SCT 397829000; MDC_EVT_ECG_ASYSTOLE. |
| `BIO_PHYS_APNEA` | Apnoea | Span; `meta.type` (central, obstructive, mixed). SCT 1023001; MDC_EVT_APNEA. |
| `BIO_PHYS_HYPOPNEA` | Hypopnoea | Span. No SNOMED finding for it; the sleep-scoring term has only an index. |
| `BIO_PHYS_HYPERVENTILATION` | Spontaneous hyperventilation | Span; the procedure is `EEG_ACT_HV`, which keeps the SNOMED crosswalk: a crosswalk is looked up across the merged table, so one concept names one term. |
| `BIO_PHYS_PERIODIC_BREATHING` | Periodic breathing | Span. No general SNOMED finding; the altitude and the sleep-apnoea variants are too specific. |
| `BIO_PHYS_DESATURATION` | Oxygen desaturation | Span; `meta.nadir`. SCT 449171008. |
| `BIO_PHYS_SWEATING` | Sweating | State-like. SCT 415690000. |
| `BIO_PHYS_FLUSHING` | Flushing | State-like. SCT 238810007. |
| `BIO_PHYS_PALLOR` | Pallor | State-like. The SNOMED pallor concept is inactive; no crosswalk. |
| `BIO_PHYS_CYANOSIS` | Cyanosis | State-like. SCT 3415004. |
| `BIO_PHYS_HICCUP` | Hiccup | Span for a bout, instant for one. SCT 65958008. |
| `BIO_PHYS_VOMITING` | Vomiting | Instant. SCT 422400008. |
| `BIO_PHYS_YAWNING` | Yawning | Instant. SCT 248626009. |

The cardiorespiratory and autonomic state of the patient, whether seen at the bedside or read from a polygraphic channel. The category exists because these terms are neither observations nor findings in the sense the other categories use: a technician noting tachycardia and an ECG detector raising it name the same fact about the patient, and the annotator says who marked it. What makes a term a finding is that it interprets the signal of interest, which none of these do, so the category is acquisition-scoped and a platform translates a vendor's cardiac and respiratory markers into it. The boundary with OBSERVATION is bedside behaviour and consciousness there, the body's state here. The SNOMED crosswalks were checked against the SNOMED CT release of August 2026 through a public FHIR terminology server, the IEEE 11073 event identifiers against the IHE Devices Technical Framework's event table; bradycardia has only a sustained-bradycardia event identifier there, which is not recorded as a crosswalk for the plain term.

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

**Vendor mappings.** Two consumers of one mapping, because most recordings reach the platform after conversion in the browser rather than on the server. The viewer's Nicolet reader resolves each event record's type GUID to the built-in name (its format spec, Appendix C, now carries the full GUIDs, recovered by observation from real files) and translates the acquisition types to terms: eyes open and closed, hyperventilation, a photic train at the frequency its text states, impedance check, recording paused with the length its text states, movement, talking, crying, drowsy, asleep, the patient button, an electrode fixed, the amplifier disconnected, video start and stop, lights on and off. The term's code rides on the event under its standard and the record's own text stays the display value; finding markers, software states and site-defined types get no code. The EDF export carries the codes to the platform in the container described next, so a recording coded in the viewer needs no table on the platform. For the server-side converter path the mapping is a JSON table in the platform's own format, written against the converter's sidecar and shipped beside the vendored converter under the git-ignored converters directory, never in the repository. Writing it showed two things. The sidecar converter writes each event into the EDF as an annotation record as well as into the sidecar, so a converted upload reached both ingest seams and every event was written twice; the sidecar now owns the rows when it carries any, on both ingest paths. And the observed photic protocol runs at 14 and 16 Hz, which the vocabulary has no per-frequency term for; the table and the reader fall back to the unqualified photic term, with the frequency in the code's metadata on the platform, rather than assemble a code no vocabulary has. Adding the two frequencies to the EEG set is a vocabulary decision for the viewer first.

**The container.** The viewer's export could have written each event's code into an annotation record, and the platform could have recognised the shape with one built-in mapper. It does not, for three reasons. A TAL is free text with no structure to carry a code, a class and a channel list; the de-identification pass strips TAL text for every de-identifying reader, so what the code was written into is what the platform removes; and the submission gate already refuses a file with an annotation channel, because the export was designed to carry its structured events beside the file rather than in it. The export already had that beside: a JSON sidecar of the events as templates, the interruptions, the labels, the channels and the subject fields, and an option to embed it as a footer after the last data record with a marker in the header's reserved field. So the export's `embedFooter` is the platform path, made to produce a plain EDF whatever the recording's continuity, since the footer carries the interruptions and the file no annotation channel, with the EDF+ header conventions for the identification fields, and with the footer de-identified whenever the file is. The container is an intermediary between the browser and the server and need not read as EDF anywhere else. Ingest detaches the footer on both paths before the processor runs, truncating the file and restoring a standard reserved field, and resolves each event from the code it declares before any mapper is asked, in [recordings/container.py](../../recordings/container.py). Writing the exporter's test found that the encoder took the size the marker names from a header field a resource-built header never sets, so a container from the exporter named its header alone and the platform would have truncated the recording to it; the encoder now writes the data records first and names their size. The footer's labels were first left unread, since a rater's labels are theirs and ingest writes under the system user; step G reads the coded ones (section 8). One thing is deliberately not done: the plain upload path does not refuse a container that also carries an annotation channel, which the submission gate does.

**Open, deliberately.** A validated identifier in `meta` (a SNOMED id for the drug given) is treated as text by the redaction rule today. A narrow exemption for a `meta` key whose value is checked against a registered vocabulary may be warranted; it is not part of this work.

## 7. Sequencing

| Step | Where | Size | State |
|---|---|---|---|
| A. Class-aware statics, shared JSON and `GenericBiosignalEvent` table, type fields, tests | viewer core | M | built 2026-09-24 |
| B. EEG JSON, sensory stimuli, table corrections, statics removed, merged getter, tests | viewer eeg-module | S | built 2026-09-24 |
| C1. Pinned copies, two registered standards, digest test, README | platform annotations | S | built 2026-09-24 |
| C2. Translation registry, `Event` rows with codes, raw record kept, fail-closed placeholder | platform recordings | M | built 2026-09-24 |
| C3. Vendor mappings: the Nicolet `.e` reader in the viewer and the table for its server-side converter | viewer nic-reader, deployment data | S | built 2026-09-24 |
| E. The container: the export embeds the coded events as a footer, ingest detaches it and resolves the declared codes | viewer edf-reader, platform recordings | M | built 2026-09-25 |
| F. Submit to the platform from the viewer's file menu, for any reader, as the container | viewer core, edf-reader, interface, platform SPA, edu frontend | M | built 2026-09-25 |
| G. Coded labels in the footer become `Label` rows with their codes | platform recordings, annotations | S | built 2026-09-25 |
| H. A teaching project's span selection and coded recording labels on the submit flow | edu project | M | submission path built 2026-09-25; release side planned, section 8 |
| D. The viewer documentation's annotations page, coded annotations section | docs submodule | S | at release |

Steps A and B change published surface in two viewer packages: `CodedEventProperties` gains two optional fields and `GenericBiosignalEvent` gains a table, which is an addition and part of core's pending 2.1.0 release; `EegEvent` loses five statics it now inherits with the same signatures, which is not a change to callers.

## 8. What remains (written 2026-09-25)

The plan for the rest, written so that it can be picked up cold. Three pieces of work follow from the container, and a list of what the anonymisation work still leaves open on the platform closes the section.

### F. Submit to the platform from the viewer

**What.** A person opens a recording in the viewer, from any reader that decodes to a `BiosignalResource`, and sends it to the platform from the file menu as a de-identified EDF container: one file, cut to the chosen range, carrying the chosen channels under the target's labels, in the target's order and at the target's rate, with the codes of its events in the footer and nothing anyone typed in it. The feature is offered only by the viewer embedded in the platform's SPA, which authenticates with the session and the CSRF token its HTTP client already carries, so the platform needs no API token.

**Every transformation happens in the browser.** The platform never receives more than it keeps. Receiving the whole recording and cutting it on the server was considered and rejected (2026-09-25): the reduction would then be the platform's own processing of special-category data (¶ 37–38 of the anonymisation guidelines), needing its own Art. 6 basis and Art. 9(2) condition and an agreement with every contributing centre to cover it. It would also transfer hours of a subject's signal to keep seconds of it (Art. 5(1)(c)), and write a temporary file past the gate's in-memory size cap on the way. The submission gate stays what it is: it checks the finished file and refuses rather than repairs.

**The tools are generic viewer features, not a pipeline for one project.** Choosing a range, selecting, renaming and reordering channels, downsampling and clipping to an amplitude range are export operations any user of any reader may want. They live in the viewer core and work on any biosignal resource; an exporter accepts a description of the result and knows nothing of who asked for it; the platform's ingest profile is one source of constraints on the export dialog, not a mode of it.

**The viewer knows nothing of the platform.** No platform endpoint, route or response shape is written into any viewer package. Core keeps a registry of export targets (`SignalExportTarget`, registered with `registerSignalExportTarget`, built 2026-09-25), each a label, the file format it takes, optional constraints, exporter options and a function that receives the finished bytes, and the host application fills it: the SPA supplies the ordinary upload to the person's own recordings and a template for a batch submission, and the teaching project's frontend extension fetches its batches' profiles from the platform and registers one target per batch from the template through its `ViewerPlugin` hooks (built 2026-09-25). The batches stay the project's configuration rather than the SPA's, because a profile will need fine-tuning as the project matures. A viewer with no target configured offers no submission.

**Only a locally opened file is offered.** A recording the viewer opened from a remote source, the platform itself or any other URL or connector, already lives somewhere; offering to upload it again would duplicate it, and for a de-identified copy served under a grant would send a recipient's copy back in as new data. A resource counts as local when every data file of its source study carries a `File` object, which a study loader sets only for a file the person picked or dropped; a URL, a connector or a source that says nothing counts as remote. `getSignalExportTargets(resource)`, the only read of the registry, answers with no targets for a remote resource, the menu entries are enabled only through it, and the dialog resolves the target through it again at send time. The rule trusts the host: one that wraps downloaded bytes in a `File` defeats it, which the platform's own frontends do not do.

| Layer | What it holds | Knows the platform |
|---|---|---|
| core | `SignalExportSelection` (a recording-time range, an ordered channel list naming a source channel and an optional output label, an output rate, an optional amplitude range), the pure function applying it to signals, events and interruptions, an anti-aliased `downsampleSignal`, `SignalExportConstraints` and the check of a selection against them, and the config slot for export targets | No |
| edf-reader | a `selection` option on `EdfExportOptions`, applied in `_gatherPayload`, which then reads only the selected range | No |
| interface | a generic export dialog (range, free or from a list of durations; channel table; rate), used for both export to file and to a configured target; constraints pre-fill and restrict it; a file-menu entry per configured target | No |
| SPA | the upload target, registered for a signed-in session, posting the container to the upload endpoint with the session and CSRF token; the submission template, which turns a published profile into constraints, declares the file's hash in the sidecar and posts both to a batch | Yes |
| edu frontend | fetches the profiles of the batches the person may submit to and registers a target per batch from the SPA's template, with its own label and, later, the sidecar keys its profile requires | Yes |
| platform | an endpoint serving a batch's ingest profile in its public shape | Yes |

**The transform's rules.**
- The range is in recording time. Its data-time bounds come from the recording's interruptions; an end of the range that falls inside a gap moves to the edge of the data, so the result begins and ends with signal. Events and interruptions overlapping the range are clipped to it and moved to start from its beginning; those outside it are dropped.
- The channel list is the output order, and each source channel appears in it at most once. An event addressing channels, by index or by label, is remapped to the output: references to dropped channels are removed, an event whose every referenced channel is dropped is dropped with them, and an event addressing no channels covers the whole recording and is kept.
- Downsampling only. A source channel below the output rate is refused, never upsampled. The anti-aliasing low-pass is a zero-phase Butterworth from the core DSP layer; `resampleSignal` uses Largest-Triangle-Three-Buckets for display, aliases, and is not used for export.
- Clipping to an amplitude range clips the samples; the encoder writes the range into the header.

**The published profile.** `GET /recordings/api/v1/submissions/batches/{hash}/profile` (built 2026-09-25) serves the profile's public shape, which the teaching project's frontend turns into constraints: the ordered channel template, which the dialog lets the person fill from nonstandard source labels with suggestions from the viewer's own label matching, and whose every label passes the gate when written verbatim, since registration refuses a profile channel the platform resolves to another label; one exact output rate, stated with the rule that a source must be at least that fast, because the output rate is a centre's fingerprint and a single rate is part of what hides origin in the pool; the list of durations the range is picked from; the amplitude range and unit; and the forbidden sidecar keys, which the exporter's sidecar de-identification strips and a profile may extend. The gate keeps refusing a forbidden key that arrives anyway, because its arrival means the browser step failed.

**The export path.** `EdfExporter.exportStudyToDataset` encodes the active resource with the selection, `deidentify: true` and `embedFooter: true`, and hands the bytes to the target's function; a target may ask for the separate sidecar as well, encoded with `deidentifySidecar: true`, which a pooled submission's gate reads. What the function does with them is the host's business: the SPA's posts to `POST /recordings/api/v1/upload`, the teaching project's to `POST /recordings/api/v1/submissions/batches/{hash}/files`.

**What the sidecar must be.** The de-identified sidecar blanks `subject` and empties each event's and label's `text`, which the gate refuses anyway, because it forbids those keys at any depth rather than their values: a filter that leaves the key and blanks the value is not distinguishable, from the outside, from one that stopped working. A target's constraints therefore name the keys the destination forbids (`forbiddenMetadataKeys`, the profile's forbidden sidecar keys), the export dialog passes them to the exporter as `removeMetadataKeys`, and the EDF encoder leaves every property so named out of the sidecar and the embedded footer, at any depth. The gate also needs `recording_sha256`, the hash of the bytes it receives; that is platform knowledge, so the SPA's template computes it after the project has added its own keys, and the gate still checks it against what arrives. A profile whose digital range is not the encoder's −32768 to 32767 gets no target, since no export can meet it and core's constraints have no digital range to state it in.

**The file menu.** An entry per configured target, under the label the host gives it, enabled when the active resource is a biosignal recording. The flow: the export dialog under that target's constraints, a summary of what leaves the browser (channels, length, rate, the number of coded events, and the word de-identified, never anonymised), progress, and the message the target's function returns, which for the platform is a recording's hash for an upload and the batch's count for a submission. The viewer never shows the container to the person.

**Round trip.** The platform serves the stored EDF without the footer and the events as rows, so nothing needs to read a container back. The EDF decoder should still tolerate one, since a person may keep the file: read the marker, ignore the footer bytes when decoding the records, and take the events and interruptions from the footer when there is no annotation channel. Small, and separate from this step.

### G. Coded labels from the footer

**What.** The footer's `labels` are read by nothing today. A label in the viewer is a recording-level annotation (class `evaluation`, `label` or `technical`) with a value and, like an event, optional `codes` keyed by standard. A teaching project's recording-level facts, the age band, the sex, the recording type, an ICD-10 code, are exactly this shape. The platform should write each footer label that declares a code under a registered standard as a system-authored `Label` row with a `Code`, and nothing for a label that declares none: a label without a code carries only text, and text from the file is what the whole vocabulary exists to keep out, so there is no placeholder to write, only a count in the log.

**Where.** A `save_footer_labels` beside `save_footer_events` in [recordings/container.py](../../recordings/container.py), called from the same two places, and callable by a project's `IngestProfile.ingest` on the separate sidecar of a pooled submission, so both paths share one reader. Validation goes through the vocabulary registry in [annotations/vocabularies.py](../../annotations/vocabularies.py) rather than `find_acquisition_term`, because these codes are not acquisition events: `icd10` is a registry identifier the platform already reserves, and a project registers its own set for the subject facts under `epicurrents.<project>.subject` or a name of its choosing. The `Label` row's `name` is the term's name where the standard can give one and the code otherwise; its `value` is the code, never the template's free value. Under the annotation-text rule the name and value are text and the code crosses, which is the point: a de-identifying reader learns the age band as a code of a registered vocabulary and nothing else.

**As built (2026-09-25).** `save_footer_labels` in [recordings/container.py](../../recordings/container.py). The footer is the viewer's sidecar appended to the file, and a pooled submission sends the same sidecar beside a plain EDF, so one writer, `save_viewer_sidecar`, takes the events, the interruptions and the coded labels on every path: the upload and import paths on the detached footer, and the pooled ingest on the submitted sidecar before the profile's `ingest` runs. It checks the whole document before writing any of it, and the submission gate refuses a sidecar it could not read. The paths are parallel feeds of one pool, since an upload joins a release-gated dataset through a release run, so they must arrive in the same shape: the pooled ingest always discards the file's text, and a release-gated deployment refuses to boot without `RECORDINGS_DISCARD_EMBEDDED_ANNOTATIONS` (`library.E002`), which makes the upload path do the same. A code is accepted when a vocabulary is registered for its standard and its validator accepts the value; an unregistered standard is refused even where the API would accept it, since outside strict mode the API lets one through unvalidated, and a validator that fails in any other way refuses the code. A label keeps every accepted code as a `Code` row, and the first accepted code, in the order the footer declares them, gives its name and value. The name comes from the registry's new optional `term_name` lookup, which the core vocabularies fill from their pinned terms, and falls back to the code. Labels are written under `RECORDINGS_DISCARD_EMBEDDED_ANNOTATIONS` too, as the translated events are: the setting removes text, and these carry none.

**What it must not do.** Write a label from a code the registry does not know (fail-closed, as for events); write the template's `text` or `annotator` anywhere; or let a project's subject vocabulary carry a value fine-grained enough to be a quasi-identifier the release gate does not count, which is a rule for the project's vocabulary rather than for this reader, and belongs in the teaching project's dataset note beside its k and m.

### H. The teaching project's side of the submit flow

Built on F and G, in the edu repository, after the platform has its profile and selector work: the project's `IngestProfile`, whose public shape the project's frontend fetches and hands to the viewer as constraints, so the range, channels and rate are chosen in the generic dialog; entering the age band and the sex, and later an ICD-10 code, as coded labels from closed lists the project's vocabulary defines, never as free text. Sex is configured per class: a class that lists no values does not collect it, and the project's dataset note records the cost, since it halves k. The platform writes the sidecar's events, interruptions and coded labels itself (step G), and the project's `IngestProfile.ingest` writes only what it derives beyond them. The edu items from the anonymisation plan stand as listed there: the release selector, the equivalence-class function, `LIBRARY_RELEASE_GATED_DEPLOYMENT = True`, and the dataset note's work-required list.

**As built (2026-09-25), the submission path.** The project registers one profile per recording class, with the version in the key. Only adult routine EEG exists so far, `edu.adult-routine.v1`: the nineteen 10-20 electrodes and ECG in the platform's canonical order, 200 Hz, 5, 10 or 20 minutes. It also registers three vocabularies, `epicurrents.edu.recording_class`, `epicurrents.edu.age_band` and `epicurrents.edu.sex`, from one JSON file that the project's frontend reads as well.
- The age band and the sex are chosen in a dialog the project's `extendSidecar` opens at send time. Each travels as a coded label, which step G writes like any other. Adult routine EEG accepts female, male or not known.
- The recording class follows from the profile, so the profile's `ingest` writes it as a coded label. That makes it the one row the project derives beyond the sidecar.
- The profile's sidecar check refuses a sidecar without exactly one band and one sex from the class's lists, a sex where the class collects none, or a class of its own.

The release side, the embargo attestation and the at-least-one-event rule remain, and are listed in the project's dataset note.

### What the anonymisation work still leaves open on the platform

The phased plan in [anonymisation-compliance-plan.md](anonymisation-compliance-plan.md) is shipped through Phase 8. What remains on the platform side of that plan, and of this note, is small and listed here so it is not lost; the ROADMAP's privacy and recordings sections carry the longer list of adjacent items, which are not part of this plan.

- Step D of this note: the viewer documentation's annotations page, at the release that ships the vocabularies.
- A home for the deployment's translation table for the server-side converter. It ships beside the vendored converter under the git-ignored directory and no repository of the operator's holds it yet; the External converters table in [recordings/README.md](../../recordings/README.md#external-converters) is where its existence is recorded.
- Two things this note left deliberately undone: the plain upload path does not refuse a container that also carries an annotation channel, which the submission gate does; and the event serialiser does not serve `event_class`, so the viewer derives the class from the code.
- The open question in section 6: a validated identifier in a code's `meta` is text under the redaction rule.
- From the ROADMAP, the privacy items that touch the same surfaces and would be reviewed by the same agents: purge-time tombstoning of patient-side snapshots in the change log, retention-window enforcement, ownership transfer before account erasure, the Art. 13/14 notice surface, retiring `preserve_annotations`, carrying the annotation-text rule into the viewer's rendering, and the two `import_recordings` items (source paths in logs, pruning of finished job rows).

## Log

| Date | Change |
|---|---|
| 2026-09-26 | v1.11. Sex added to the teaching project's submission facts as a coded term, configured per class, with not known among the adult values. The excerpt duration stays out of the equivalence class. |
| 2026-09-25 | v1.10. Step H's submission path built in the teaching project. It registers an adult routine profile and two coded vocabularies; the age band is entered at send time and the recording class is written by the profile's ingest. The H paragraph is corrected: sex is not collected, as the project's dataset note had already decided. |
| 2026-09-25 | v1.9. Step G built: coded footer labels become system-authored `Label` rows through `save_footer_labels`, fail-closed against the registry, which gains an optional `term_name` lookup to name them. One writer for the footer and a pooled submission's sidecar, since both feed the same pool: the pooled ingest writes the rows itself with the text discarded, the gate refuses a sidecar it could not read, and a release-gated deployment must discard embedded text on the upload path too. Step F's row in section 7 marked built. |
| 2026-09-25 | v1.8. The targets built: the SPA registers the upload and holds a submission template, and the teaching project registers its batches from it, so the profiles stay project configuration. The exporter removes a target's forbidden metadata keys at any depth, since the gate forbids keys the de-identification only blanks; the template declares the file's hash. The SPA's viewer lib registers the EDF exporter, which it had lacked. |
| 2026-09-25 | v1.7. Only a resource the viewer opened from a local file can be sent to a target; the loader records the origin, and an unknown origin counts as remote. The viewer holds no knowledge of the platform: the host fills a generic export-target slot in the viewer config, the SPA with the upload and the teaching project's frontend with its batches and their constraints, which it fetches from the platform. No platform connector in core. |
| 2026-09-25 | v1.6. Step F redesigned: every transformation happens in the browser, because a reduction on the platform would be its own processing of special-category data; the range, channel and rate operations are generic viewer-core export features with the platform's profile as one source of constraints; event channel references follow the channel selection. The span selection moves from H to F. |
| 2026-09-25 | v1.5. Section 8 written: the plan for submitting from the viewer through a platform connector on the WebDAV seam (F), coded labels from the footer as `Label` rows (G), the teaching project's span, age, sex and recording labels (H), and the list of what the anonymisation work still leaves open on the platform. |
| 2026-09-25 | v1.4. Step E built: the viewer's EDF export carries the codes to the platform in the container its `embedFooter` option produces, a plain EDF with the sidecar as a footer, and ingest detaches the footer on both paths and resolves each event from the code it declares. Decided against codes in annotation records, for the reasons in section 6. The encoder's marker named the header alone for a resource-built header; fixed. |
| 2026-09-24 | v1.0. Design from the 2026-09-24 discussion written up; DICOM crosswalk checked against CID 3035; SNOMED column left empty pending checked lookups. Steps A, B and C1 built the same day; electrode fault and electrode fixed added to the shared set on review, the electrode carried by its canonical label because ingest reorders channels; ENVIRONMENT category added for external influences. |
| 2026-09-24 | v1.3. PHYSIOLOGY category added to the shared set (biosignal vocabulary 1.1): the cardiorespiratory and autonomic terms the Nicolet polygraphic event types and bedside notes need, which were neither observations nor findings as the categories were drawn; acquisition-scoped, so ingest translates them. SNOMED and IEEE 11073 crosswalks recorded where a checked lookup exists. |
| 2026-09-24 | v1.2. Step C3 built: the viewer's Nicolet reader names every built-in event type by GUID and codes the acquisition ones with vocabulary terms; the server-side converter's table written against its sidecar and validated against the real files' events. Two findings: a converted upload reached both ingest seams and doubled its events, fixed by letting the sidecar own the rows; 14 and 16 Hz photic trains have no per-frequency term and take the unqualified one. |
| 2026-09-24 | v1.1. Step C2 built: mappers and JSON tables, `Event` rows with a `Code` under the owning standard, text-free placeholders, the raw record kept under the annotation-text rule. Two corrections to section 6 as first written: the standard is resolved from the term rather than the modality, and the raw record is not author-private but follows the annotation-text rule, which is left as it was. The sidecar schema now accepts a null `type` or `label`, which the Nicolet converter writes and the pinned schema had refused. |
