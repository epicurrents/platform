# Python DICOM de-identification tools — comparison and recommendation

Tool selection for the dicom plugin's "De-identify DICOM on ingest" roadmap
item ([plugins/dicom/README.md → Roadmap](../../plugins/dicom/README.md#roadmap)).
The integration point is a hook in [ingest.py](../../plugins/dicom/ingest.py)
between pydicom header parsing and persistence, running synchronously inside
the upload request (optionally as a Celery stage later). The ideal shape is
therefore pydicom `Dataset` in / `Dataset` out, with no file-format
round-trip. Facts below are as of 2026-07; release data from the PyPI JSON
API.

---

## Requirements recap

1. **PS3.15 Annex E conformance** — the [Basic Application Level
   Confidentiality Profile](https://dicom.nema.org/medical/dicom/current/output/chtml/part15/chapter_E.html)
   defines per-tag action codes (D = dummy, Z = zero-length, X = remove,
   K = keep, C = clean, U = remap UID). A tool that ships the Annex E tag
   table is auditable against the standard; a "best effort" list is not.
2. **UID remapping consistency** — a study arrives across multiple upload
   requests (the endpoint caps a batch at `DICOM_MAX_UPLOAD_FILES` = 500),
   potentially handled by different worker processes on different days.
   The same original UID must map to the same replacement every time, or
   the study/series hierarchy shatters and the per-author
   `(author, study_instance_uid)` dedup key stops matching re-uploads.
   Any scheme that is only consistent *within one process* or *within one
   anonymizer instance* fails this requirement out of the box.
3. **Burned-in pixel annotations** — ultrasound, secondary captures, and
   some CR/DX exports carry PHI rendered into the pixels. Header
   de-identification alone does not touch it.
4. **Dependency weight** — the hook runs inside the Django web container;
   a torch-sized dependency stack is disqualifying for the synchronous path.

## Candidates

### KitwareMedical dicom-anonymizer

[github.com/KitwareMedical/dicom-anonymizer](https://github.com/KitwareMedical/dicom-anonymizer)
— v2.0.0, 2026-07-10. BSD-3-Clause. Dependencies: pydicom, tqdm.

The closest direct implementation of Annex E among the Python candidates.
The tag table is generated from the **2023e edition** of the standard
(`dicomfields_2023.py`; older editions selectable), and the action set maps
one-to-one onto the PS3.15 codes: `replace` (D), `empty` (Z), `delete` (X),
`replace_UID` (U), plus the composite codes (`Z/D`, `X/Z`, `X/D`, `X/Z/D`,
`X/Z/U*`) resolved per the standard's Type 1/Type 2 conformance notes.
`anonymize_dataset(dataset, extra_anonymization_rules=None,
delete_private_tags=True)` mutates a pydicom `Dataset` in place — exactly
the hook shape — and `extra_anonymization_rules` overrides the action for
any tag with a plain callable, which is how both custom keeps and a custom
UID strategy are injected. Private tags are deleted wholesale by default
(`Dataset.remove_private_tags()`), with rule-listed exceptions restored.

The one real defect for our use: default U-tag handling generates a
*random* replacement UID cached in a module-level dict, so the old→new
mapping is consistent only within one process lifetime. Multi-request /
multi-worker uploads of the same study would diverge. Fixable cleanly via
`extra_anonymization_rules` — see the integration sketch below.

No pixel-data handling of any kind. Kitware states the project is in
maintenance mode ("no plan to implement new features") but it accepts fixes
and shipped a major release in July 2026, so it is alive where it counts.

### pydicom/deid

[github.com/pydicom/deid](https://github.com/pydicom/deid) — v0.4.12,
2026-01-12. MIT. Dependencies: pydicom, numpy, matplotlib, python-dateutil.

Self-described "best effort anonymization"; the rule model deliberately
[mirrors RSNA CTP](https://pydicom.github.io/deid/). Behaviour is driven by
a recipe DSL (`ADD` / `REPLACE` / `REMOVE` / `BLANK` / `KEEP` / `JITTER`)
with pluggable Python functions, so tag coverage and UID strategy are
whatever the recipe says — maximally customisable, but the shipped default
recipe is not the Annex E table and the project makes no PS3.15 conformance
claim. Auditing our deployment against the standard would mean maintaining
our own recipe that reimplements Annex E, which is precisely the work the
tool was supposed to save.

`DicomParser` accepts an in-memory `pydicom.Dataset`
("dicom_file: Path to a dicom file or instance of a pydicom.Dataset"), and
`replace_identifiers(..., save=False)` returns modified `Dataset` objects,
so the hook shape is available. deid is also the only header-tool candidate
with burned-in pixel handling: `DicomCleaner` implements CTP-style
rectangle blanking driven by header heuristics (`BurnedInAnnotation`,
modality/manufacturer-specific coordinate lists) — no OCR, so it only
catches annotations at known machine-specific locations
([docs](https://pydicom.github.io/deid/getting-started/dicom-pixels/)).
Actively maintained under the pydicom org.

### TIO-IKIM medical_image_deidentification (MEDE)

[github.com/TIO-IKIM/medical_image_deidentification](https://github.com/TIO-IKIM/medical_image_deidentification)
— v0.0.12, 2026-03-26. Apache-2.0.

The strongest conformance story on paper: implements the PS3.15 profiles by
name (`basicProfile` plus the retain/clean options — `cleanDescOpt`,
`cleanGraphOpt`, `rtnLongFullDatesOpt`, `rtnSafePrivOpt`, `rtnUIDsOpt`,
etc., combinable per run), and the only candidate with OCR-based burned-in
text removal (EasyOCR), plus defacing/skull-stripping for 3D volumes.
Peer-reviewed (European Radiology, 2025).

Disqualified for the ingest hook by interface and weight: the documented
surface is a CLI (`mede-deidentify`) over directories, not a Dataset-level
API, and the dependency pins include `torch==2.2.2`, `easyocr`, `timm`,
`torchio`, `torchvision` — a multi-gigabyte stack pulled into the web
container to scrub headers. It also pins `numpy==1.26.4` and
`pydicom==3.0.1` exactly, which would fight the platform's own pins.
Still on a 0.0.x version, so treat it as a research pipeline rather than a
library to depend on.
Worth revisiting as an *offline* batch tool if OCR redaction becomes a
requirement.

### dicognito

[github.com/blairconrad/dicognito](https://github.com/blairconrad/dicognito)
— v0.19.0, 2025-10-10. MIT. Dependency: pydicom only.

The best ergonomics: `Anonymizer().anonymize(dataset)` in place, all PN /
DA / DT / TM / UI elements handled by VR plus ~40 named attributes, and
uniquely, order-preserving date shifting (dates move into the past but
relative order survives), which matters if longitudinal ordering must
outlive de-identification. No PS3.15 conformance claim and no published
Annex E mapping, so the audit question "which action code was applied to
tag X" has no answer. UID consistency is scoped to a single `Anonymizer`
instance ("use a single Anonymizer on datasets that might be part of the
same series"), which fails the multi-request requirement the same way
Kitware's default does — but without an equivalent override hook for
injecting a deterministic strategy. Single-maintainer project, steady
releases. A good fallback, not the primary.

### presidio-image-redactor (burned-in text companion)

[github.com/microsoft/presidio](https://github.com/microsoft/presidio) —
v0.0.59, 2026-06-28. MIT.

`DicomImageRedactorEngine.redact(dataset)` takes and returns a pydicom
`Dataset`, OCRs the pixel data with Tesseract, classifies hits with
presidio's NLP models (spaCy `en_core_web_lg`), and blacks out PII boxes.
Explicitly pixel-only: "this class only redacts pixel data and does not
scrub text PII which may exist in the DICOM metadata" — by design a
companion to a header tool, not a replacement. Dependencies are moderate
(Tesseract binary, spaCy model, opencv, presidio-analyzer; no torch) but
still too heavy and too slow for the synchronous upload path; the project
itself labels the module beta / not production-ready. The right fit is a
later optional Celery stage gated on modalities that plausibly carry
burned-in text (US, SC, CR/DX, XA).

### RSNA CTP (prior art, non-Python)

The [CTP DICOM Anonymizer](https://mircwiki.rsna.org/index.php?title=The_CTP_DICOM_Anonymizer)
is the de-facto standard rule model (TCIA uses it): a per-tag script of
functions like `@remove()`, `@empty()`, and `@hashuid(@UIDROOT, this)`.
Java, so not integrable here, but two of its ideas transfer directly:
deid's whole recipe system is modelled on it, and `@hashuid` — a keyed hash
of the original UID under a fixed root — is the deterministic UID-remapping
scheme our integration needs, because it makes consistency a property of
the *function* rather than of shared mutable state.

## Comparison summary

| | Kitware dicom-anonymizer | pydicom/deid | MEDE (TIO-IKIM) | dicognito | presidio-image-redactor |
|---|---|---|---|---|---|
| PS3.15 Annex E | Full 2023e tag table; D/Z/X/U + composites | No shipped profile; recipe DSL | Named profiles incl. retain/clean options | No claim; VR-based + curated list | n/a (pixel only) |
| Rule customisability | Per-tag override dict with callables | Full recipe DSL + Python funcs | Profile flags (CLI) | Minimal | n/a |
| UID remapping | Random + process-local cache; overridable per tag | Recipe-defined (bring your own func) | `rtnUIDsOpt` retain option; else profile-driven | Consistent per `Anonymizer` instance only | Untouched |
| Burned-in pixels | None | CTP-style rectangle blanking, no OCR | EasyOCR detection + removal | None (flag check only) | OCR + NLP redaction |
| Private tags | Delete all by default, rule exceptions | Recipe-controlled | Profile-controlled (`rtnSafePrivOpt`) | One known private element handled | Untouched |
| Dataset in/out | Yes, in place | Yes (`DicomParser` accepts `Dataset`) | No (CLI over directories) | Yes, in place | Yes |
| Per-file cost | Header-only tag ops; sub-ms | Header ops; pixel clean reads pixels | OCR/GPU scale | Header-only; sub-ms | OCR; seconds |
| Dependencies | pydicom, tqdm | pydicom, numpy, matplotlib, dateutil | torch, easyocr, timm, … (GB-scale) | pydicom | tesseract, spacy model, opencv, … |
| Licence | BSD-3-Clause | MIT | Apache-2.0 | MIT | MIT |
| Health (2026-07) | v2.0.0 2026-07; maintenance mode, responsive | v0.4.12 2026-01; active | v0.0.12 2026-03; research-grade | v0.19.0 2025-10; single maintainer | v0.0.59 2026-06; module in beta |

## Recommendation

**Primary: KitwareMedical dicom-anonymizer.** It is the only candidate that
combines an auditable Annex E implementation, a `Dataset`-in-place API that
drops straight into the ingest hook, and a dependency footprint of
essentially nothing beyond pydicom (which ingest already requires). Its one
gap — non-deterministic UID remapping — is closed with a small
`extra_anonymization_rules` override, and unlike dicognito the override
point exists. deid loses on conformance auditability and drags in
matplotlib; MEDE loses on interface and weight; dicognito loses on
conformance and on having no clean place to inject a deterministic UID
function.

**Companion (deferred): presidio-image-redactor** for burned-in pixel text,
as an optional Celery stage — never synchronously. Until it lands, the
honest posture is to record the `BurnedInAnnotation` flag at ingest and
surface it, not to pretend header scrubbing covers pixels.

## Integration sketch

**Hook location.** A `deidentify_dataset(ds)` function in
[ingest.py](../../plugins/dicom/ingest.py) (or a sibling `deidentify.py`),
called by the upload endpoint and `index_dicom` after `parse_dicom_header`
and before `persist_instance` / field extraction. Because field extraction
runs on the already-scrubbed dataset, the `patient_*`, `study_*`, and
`accession_number` model fields automatically store the post-de-id values —
no model or extraction change needed. Order matters: de-identify first,
extract second.

**File rewrite.** Ingest currently parses with `stop_before_pixels=True`
and stores the uploaded bytes verbatim. De-identification changes that: the
staged file must be re-read in full (pixels included), scrubbed, and
written back via `Dataset.save_as` before the move to final storage, with
`file_size` and `file_hash` recomputed from the rewritten file. Pixel bytes
pass through untouched, so the cost is one extra read+write of the file —
negligible against upload I/O for slice-sized files. Files approaching
`DICOM_MAX_UPLOAD_FILE_SIZE` (2 GiB) are the argument for the optional
Celery stage; defer it until such files actually appear. Note the `--resume`
consequence for `index_dicom`: the stored hash is of the de-identified
bytes, so resume detection must hash after scrubbing (or keep matching on
the source hash recorded separately).

**Deterministic UID remapping.** Override every U-coded tag with a CTP
`@hashuid`-style function: HMAC-SHA256 of the original UID under a
deployment key, rendered as a `2.25.<decimal-of-128-bits>` UID
([PS3.5 B.2](https://dicom.nema.org/medical/dicom/current/output/chtml/part05/sect_B.2.html)).
Same input → same output across requests, workers, and restarts, with no
shared state; split-batch uploads converge on one study row and re-upload
dedup keeps working, since `study_instance_uid` stores the replaced UID.
Key the HMAC from `SECRET_KEY` the way `DicomStudy.make_content_hash`
already does — inheriting the same key-rotation hazard flagged as gap 3 in
`dicom-integration-gaps.md` (kept in the archive repository's `docs/engineering-notes/`); resolve both the
same way.

**Setting.** `DICOM_DEIDENTIFY_ON_INGEST` (default `False` for the first
iteration — existing deployments hold non-de-identified studies, and
flipping the default mid-life would create a mixed archive without an
operator decision). When enabled: staged files are scrubbed before
persistence, `PatientIdentityRemoved=YES` and `DeidentificationMethod` are
written into the dataset, and the study endpoints / OHIF JSON / `wado_uri`
all serve de-identified content for free because storage itself is clean.
When disabled, behaviour is unchanged from today.

**Out of scope for the first iteration:**

- Burned-in pixel redaction (record `BurnedInAnnotation`, defer redaction
  to the presidio Celery stage).
- Retroactive de-identification of already-stored studies (needs a
  management command with its own audit story).
- The retain-options profiles (dates, patient characteristics) — first
  iteration applies the basic profile as Kitware ships it, which dummies
  `StudyDate` and empties `PatientSex`; deployments that need longitudinal
  dates can drive a retain rule through `extra_anonymization_rules` later.
- Private-tag allowlists — `delete_private_tags=True` wholesale.
