# Composite studies — the study document

**Status:** draft v0.2, 2026-11-14. Nothing built. Decided so far: the modality is `composite`; the importer accepts canonical English names only and the submitter maps everything else during upload. v0.2 adds the vendor's database export, which carries device-recorded distances and markers that the curve export does not; whether the document carries them is open. Open questions are collected in [section 9](#9-open-questions).

## TL;DR

An electrodiagnostic examination is not one recording but a set of short, separate measurements of different kinds: motor and sensory conduction sweeps per nerve and site, F-wave series, EMG saves per muscle, later H-reflex, repetitive stimulation and motor evoked potentials. The viewer already ties such a set together for one vendor's API into a tabular study with NCS and EMG subcontexts. This note specifies a vendor-neutral **composite study document** that any converter can write, the platform can store, de-identify and serve, and the viewer can open into the same shape.

The document is a JSON manifest followed by float32 sample blocks in one file. It carries canonical names, acquisition parameters and samples, and nothing a person typed. Everything a reader produces — markers, distances, the values computed from them — is annotation, written by the viewer through the annotations API, not part of the file.

## 1. The facts that shape the design

These come from an inventory of 32 real examinations in one vendor's curve export (2728 sweeps), one demo curve export with motor evoked potentials, and one examination in the same vendor's database export.

- **The curve export is a folder tree, not a recording.** One folder per protocol, nerve or muscle, and side; a tab-separated header per folder with one row per sweep (test type, averaging count, sample count, sampling rate, stimulus intensity, file name); one text file of samples per sweep. A separate file holds the patient identification and dates.
- **The database export is a dump of the device's tables.** One XML file holds every table of the examination, with each binary column in a file of its own: the sweeps of one site in a serialised block, each EMG save as raw samples, and the reports. Identification is spread through it — the patient and study tables, operator and workstation names, a free-text note, a snapshot file, the reports as PDF or word-processor documents, and a date on almost every row. Nerve, muscle and test type are references to the device's factory tables, which the export does not include; sites carry a factory key or a label the lab typed.
- **The curve export has no measurements and no distances.** Latencies, amplitudes and conduction velocities must be computed from markers the reader places, and segment distances must be entered by the reader. A distance column found in a few local curve exports was the examiner's own note, not an export field.
- **The database export has some of both.** It stores the distances the examiner entered at acquisition, in millimetres, for the segments and sites that were measured (nine of 130 in the one examination), and per-sweep marker latencies in seconds. It stores no computed values: the analysis-result columns are empty, so amplitudes, velocities and deviations from reference are computed at display time. It also carries the lab's reference limits, as mean ± 2 SD with the mean from a regression, or as fixed bounds; the regression needs the subject's age and probably height.
- **The database export stores volts, not microvolts.** Sensory averages as float32, motor, F-wave and H-reflex sweeps as int16 normalised to the sweep's peak with a counts-per-volt scale, EMG saves as float64. Stimulus intensity is in amperes per sweep.
- **Names are configured per site, in the site's language.** Protocol, nerve, muscle and site names are whatever the lab's protocol templates say, including local-language words, abbreviations with inconsistent punctuation (`Ab._elbow`, `Ab.elbow`) and generic placeholders (`Stim_1`, `Rec_1`). A name in the file is therefore a site fingerprint as well as a label.
- **The test type is reliable; the folder is not.** Every sweep's header row names its type, and in the corpus each protocol folder maps to exactly one type. The one exception is the demo's motor evoked potentials, which the device stores as a motor-conduction type and only the folder distinguishes.
- **Shapes vary within a type.** Motor sweeps are 50 or 100 ms; EMG saves run from under 1 s to 10 s in the curve export and up to about three minutes in the database export; H-reflex sweeps are recorded at two sampling rates. Every sweep must carry its own parameters.
- **Curve-export values are microvolts clipped at the input range.** Sensory sweeps saturate at ±200 or ±300 µV (the stimulus artefact), motor at ±50 mV, EMG at ±300, ±3000 or ±6000 µV. The range itself is not exported. Polarity has not been verified against a device printout.
- **Sensory sweeps come in split-average pairs.** Each sensory site is stored as an odd and an even average of the same stimulus train, the device's reproducibility check.

## 2. Terms

| Term | Meaning |
|---|---|
| Composite study | One examination: the set of parts recorded in one session. The recording's modality is `composite`. |
| Part | One measurement of one kind on one nerve or muscle and side: a motor conduction study of the left median nerve, an EMG of the right tibialis anterior. A part has its own modality (`ncs`, `emg`, later `mep`) and test. |
| Sweep | One stored trace: a stimulus response, an averaged response, an EMG save. |
| Site | A canonical stimulation or recording location. |
| Segment | A pair of sites over which a reader measures conduction. Segments are defined by the reader, not by the file. |

## 3. The document

One file, extension to be settled ([section 9](#9-open-questions)):

```
offset 0   8 bytes   magic "EPCSTDY1"
offset 8   4 bytes   uint32 little-endian: manifest length in bytes (N)
offset 12  N bytes   manifest, UTF-8 JSON
           padding   zero bytes up to the next multiple of 8
           data      float32 little-endian samples, in microvolts, sweep after sweep
```

Every sweep names its block by byte offset from the start of the data section (`data.offset`) and its sample count (`data.samples`), so a sweep can be served by byte range without reading the rest. Sizes are dominated by EMG saves: about 1 MB each in the curve corpus, so a typical examination is around 10 MB, and up to 16 MB for a single long save from the database export.

### Manifest

```json
{
    "format": "epicurrents.composite",
    "version": "1.0",
    "modality": "composite",
    "kind": "edx",
    "vocabulary": { "standard": "epicurrents.edx", "version": "1.0" },
    "parts": [
        {
            "id": "p1",
            "modality": "ncs",
            "test": "motor",
            "nerve": "median",
            "side": "left",
            "sweeps": [
                {
                    "id": "s1",
                    "stimulation": "wrist",
                    "recording": "abductor_pollicis_brevis",
                    "intensity": 0.032,
                    "averages": 1,
                    "samplingRate": 48000,
                    "triggerOffset": 0,
                    "data": { "offset": 0, "samples": 2400 }
                }
            ]
        }
    ]
}
```

Rules:

- **Every name is a term of the declared vocabulary** ([section 4](#4-the-canonical-vocabulary)). A stored manifest with a value outside it is refused, and the stored document has no free-text field. The converter's output differs in one place only, the `sourceNames` table that ingest resolves and removes ([section 5](#5-mapping-names-during-upload)).
- **`parts` and `sweeps` keep acquisition order**, which is the only timing the document carries. There are no dates, times or durations between parts, even where the source has them.
- **`intensity` is in amperes**, `null` when the device does not report one (magnetic stimulation).
- **`triggerOffset`** is the sample index of the stimulus, 0 when the sweep starts at the stimulus.
- **A sensory sweep carries `split: "odd" | "even"`** and the two halves of a pair share `stimulation`, `recording` and `intensity`. The viewer shows their mean and can overlay the halves.
- **An F-wave or H-reflex part is a series**: its sweeps share sites, may differ in intensity, and keep their order.
- **An EMG part has no sites.** Its `nerve` field is replaced by `muscle`; each sweep is one save.
- **`kind` is optional** and labels the field of the examination for display; grouping and serving use only `modality`.
- **Several parts may share test, nerve and side.** A short-segment sensory study of the median nerve is a second sensory part beside the routine one, not merged into it.

### Test types

| `test` | Part modality | Source sweeps | Status |
|---|---|---|---|
| `motor` | `ncs` | motor conduction | first build |
| `sensory` | `ncs` | sensory conduction, split pairs | first build |
| `fwave` | `ncs` | F-wave series | first build |
| `emg` | `emg` | EMG saves | first build |
| `hreflex` | `ncs` | H-reflex recruitment series | later |
| `rns` | `ncs` | repetitive stimulation trains | later |
| `mep` | `mep` | motor evoked potentials | later |

`hreflex` is already a `SupportedNcsStudyType` in the viewer. Adding a test later is a new row here and a new vocabulary version; the file layout does not change.

## 4. The canonical vocabulary

`epicurrents.edx` is a closed vocabulary of English terms in snake_case, registered on the platform like the other core vocabularies and shipped by the viewer as JSON. A term has a code (what the manifest carries) and a display name. Codes never change once shipped.

The initial set below covers every English-named site, nerve and muscle in the inventory corpus. Placeholder sites and local-language names in the corpus are not terms; a submitter maps them during upload ([section 5](#5-mapping-names-during-upload)).

**Sides:** `left`, `right`.

**Nerves:** `median`, `ulnar`, `radial`, `fibular`, `superficial_fibular`, `tibial`, `sural`, `lateral_antebrachial_cutaneous`, `medial_antebrachial_cutaneous`, `posterior_antebrachial_cutaneous`.

**Stimulation sites:** `palm`, `wrist`, `forearm`, `below_elbow`, `elbow`, `above_elbow`, `erb_point`, `ankle`, `mid_lower_leg`, `calf`, `below_knee`, `above_knee`, `popliteal_fossa`.

**Sensory recording sites:** `digit_1` to `digit_5` (hand), `extensor_pollicis_longus_tendon`, `wrist`, `mid_forearm`, `lateral_malleolus`, `dorsum_of_foot`.

**Muscles** (motor recording sites and EMG): `abductor_pollicis_brevis`, `opponens_pollicis`, `first_dorsal_interosseous`, `abductor_digiti_minimi`, `extensor_indicis`, `extensor_digitorum`, `flexor_carpi_radialis`, `flexor_carpi_ulnaris`, `brachioradialis`, `biceps_brachii`, `triceps_brachii`, `deltoid_posterior`, `infraspinatus`, `trapezius`, `rectus_abdominis`, `gluteus_maximus`, `tensor_fasciae_latae`, `adductor_magnus`, `vastus_medialis`, `tibialis_anterior`, `gastrocnemius_medial_head`, `soleus`, `extensor_hallucis_longus`, `flexor_hallucis_longus`, `extensor_digitorum_brevis`, `abductor_hallucis`.

**Matching** at ingest is exact on the code, or on the display name after case folding and treating space, underscore, hyphen and full stop as one separator. Nothing looser: an abbreviation such as `APB` is a mapping, not a match, because accepting abbreviations is where site conventions creep back in.

## 5. Mapping names during upload

The converter is name-agnostic. It reads the vendor's folder tree and writes the document with each name as found, in a `sourceNames` table the parts refer to by index, plus the test type it read. The platform does the rest:

1. Ingest resolves every source name it can match ([section 4](#4-the-canonical-vocabulary)).
2. If any name is left, the upload stops in a mapping state and the submitter is shown the unresolved names, with the test, nerve and side they occur in, and picks a term for each — or marks a sweep or part to be dropped.
3. Ingest rewrites the manifest with terms only and removes `sourceNames`. The stored file carries no name the submitter did not confirm.

Source names live only in the author-private staging state while the upload waits, under the same rule as `Recording.original_name`. A submitter may save their mapping for reuse; it is their own data, never shipped with the platform. Per-language mapping tables are not part of the platform for now.

The test type comes from the converter, but the submitter can correct it in the same step, which is how the demo's motor evoked potentials (stored by the device as motor conduction) get their real test once `mep` exists.

## 6. What the converter must and must not do

The converter is an external program configured in `RECORDING_CONVERTERS`, like the existing ones; nothing vendor-specific enters the platform. From the inventory, a converter for a curve export of this kind must:

- read header columns by name, since the column set varies between exports, and ignore columns it does not know;
- split sample files on any line ending and refuse a sweep whose sample count disagrees with its header;
- compare file names after Unicode NFC normalisation, since network shares may list them decomposed;
- skip empty folders and files the header does not list;
- never read the identification file beyond confirming it exists, and write nothing from it.

A converter for a database export must:

- read an allowlist of tables and columns, and nothing else — never the patient, study or journal tables, operator or workstation columns, free-text notes, the snapshot file or the reports;
- drop every date and time, including those inside the serialised sweep blocks;
- resolve nerve, muscle and test type from its own table of the device's factory identifiers, and refuse a reference it does not know rather than guess;
- put each site's factory key or typed label into `sourceNames`, never into a part directly;
- convert samples to microvolts float32, applying each sweep's own scale.

Neither converter may compute measurements, guess distances, rename sites or invert polarity.

## 7. Platform obligations

Storing a format that is not EDF touches every place that assumes EDF. Each needs a deliberate entry before the first composite study is stored:

- **Modality.** Add `composite` to `RECORDING_MODALITIES` in [recordings/modalities.py](../../recordings/modalities.py). It is always declared by the converter's output, never inferred by the channel vote.
- **De-identification pass.** A processor that validates the manifest against the schema and the vocabulary, refuses unknown keys, and rewrites the manifest canonically, stamped with its own version beside `DEIDENTIFICATION_VERSION` and pinned the same way. The manifest holds no identification by construction, so the pass is a validator, not a scrubber; refusing is what keeps it so.
- **Serving.** `_build_serve_pipeline` and the proxy offload assume EDF. A composite file is identical for every reader, so it needs an explicit pipeline entry that passes it through, and the offload interlock must stay keyed on `apply_middleware` and not on format.
- **Assessment.** New given-data rows in [docs/anonymisation-compliance.md](../anonymisation-compliance.md): acquisition order and parameters, stimulus intensities and EMG save lengths are what a recipient receives.
- **Annotations.** Reader measurements are rows on the recording, under the annotation-text rule ([section 8](#8-measurements)).

## 8. Measurements

Markers, distances and the values computed from them (latency, amplitude, area, duration, conduction velocity, F-wave persistence and latencies) are the reader's work. The viewer computes them from markers, as `NcsChannelMarker` already does for a single trace, and binds the tab-module's table rows to them, so moving a marker updates the table.

Persisting them is the one structural decision left. A `Label` carries them today but its value is text, withheld from every de-identifying recipient. A confined vocabulary `epicurrents.edx.measure`, whose every part is a term, a sweep id or a number, would let them cross that boundary as codes; confined is the right class because the writer is the viewer, a known writer, not a file. This note recommends the confined vocabulary and leaves the decision open.

## 9. Open questions

- **Extension and magic.** `.ecs` with the magic above is a placeholder.
- **Fibular or peroneal.** The note uses `fibular`, the current anatomical term; many labs still say peroneal, which would be its display alias.
- **Split-average count.** Whether a sensory sweep's averaging count is per half or for the pair; it decides how the mean is weighted.
- **Polarity.** The sign convention of the samples, to be verified against a device printout before the viewer draws anything.
- **Placeholder sites.** Whether the vocabulary should offer numbered neutral sites (`additional_site_1` …) for a submitter who cannot name an anatomical location, or whether such sweeps are dropped.
- **Anastomosis studies.** A Martin-Gruber check stimulates the median nerve and records from an ulnar-innervated muscle within an ulnar part; the sweep may need its own `nerve`.
- **Measurement persistence** ([section 8](#8-measurements)).
- **Device-recorded distances and markers.** The database export carries examiner-entered distances and marker latencies. They are numbers between canonical sites and sweeps, so they hold no text, but they are the examiner's work rather than acquisition. Either the document carries them as an optional numeric layer the viewer pre-fills and the reader can override, or the converter drops them and the reader re-enters them. Carrying them is the recommendation, since a distance cannot be re-measured afterwards.
- **Reference limits.** The lab's limits are configuration, not part of the examination, and a regression limit needs the subject's age. Whether the platform offers reference tables of its own, and whether a converter may write an age band, are both open; the age band is a de-identification decision for the assessment, not the converter.

## 10. Viewer side

- Split the existing API processor into "vendor response → composite document" and "composite document → tables and NCS / EMG templates", so the API and file paths share the second half and the API's output stays unchanged.
- Fix the API processor's known issue where several motor recording positions collapse into one section while splitting.
- Register `ncs` and `tab` in the builder's full profile, which currently leaves them out for lack of a registrar.
