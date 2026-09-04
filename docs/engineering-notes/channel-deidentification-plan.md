# Channel-level site de-identification + canonical channel order — execution plan

**Status: v2. Phases 1, 1b, 2 and 3 implemented; phase 4 deferred — see the sequencing block below.** The ingest pipeline de-identifies the *subject*-identifying EDF header fields in the stored file ([recordings/processors/edf.py](../../recordings/processors/edf.py) `process_edf_file` → `_build_clean_header`). When this note was written the per-signal header block — channel labels, transducer strings, prefiltering strings — and the channel *order* were copied verbatim from the source file, and the metadata API mirrored them field-for-field. Together these form a fingerprint of the acquiring institution: naming conventions, montage template order, vendor metadata channels, and device strings can narrow a recording to a vendor, a lab, or a single acquisition setup. This note is the execution plan for closing that surface, with the justification for each phase and an honest account of what remains open afterwards.

> **Revision note (v2, 2026-08-21).** v1 placed channel-block cleaning in a serve-time `EDFHeaderMiddleware` keyed off `apply_middleware`, on the principle that the author's stored file should keep full fidelity. Three subsequent platform changes invalidated that placement. First, `apply_middleware=False` grants are now a deliberate posture a project documents in its own README: subject de-identification happens at ingest in the stored file, so raw-file serving to grantees is the intended fast path — a serve-time control keyed on the flag would never reach exactly the grantee population it exists for. Second, the reverse-proxy download offload ([epicurrents/offload.py](../../epicurrents/offload.py)) never offloads middleware-applied grants (computed bytes have no file on disk), so serve-time cleaning would push every de-identified download back through gunicorn. Third, the machinery an ingest-time rewrite needs now exists: ingest already restructures data records (`normalise_edf_records`), and [recordings/metadata.py](../../recordings/metadata.py) provides the re-derive-from-disk pattern for stored-file rewrites. v2 therefore cleans the stored file at ingest — the same trade the platform already made for subject PHI — and keeps raw values as author-private database fields.

> **Margin note — sandbox timing.** The platform has no live instances and no ingested back catalogue. Every phase below therefore applies uniformly from day one; there is no retro-reprocessing campaign, no audit-digest churn on existing rows, and no index-consumer migration to plan. If a deployment goes live before Phase 1 or 3 lands, that stops being true — the ingest-time controls become non-retroactive for already-stored files and would need a reprocessing pass (the `refresh_signal_metadata` machinery is the starting point).

## Threat model and framing

Recording start dates are already anonymised at ingest, so site identification matters here as a **k-anonymity reducer**, not as direct identification: knowing "this file came from institution X's EMU, on vendor Y hardware" shrinks the candidate subject pool that other quasi-identifiers (age, recording length, pathology visible in the signal) then cut further. The control is worth building, but it must not be oversold — see [Residual risk](#residual-risk-what-this-plan-does-not-close).

Ranking the leaking fields by pool-shrinking power, which is what justifies the phase order:

1. **Transducer strings** (`transducer_type`, 80 bytes/signal) — often a literal device or electrode-model string; close to naming the vendor outright.
2. **Prefiltering strings** (`prefiltering`, 80 bytes/signal) — vendor-specific formatting, and `N:50Hz` vs `N:60Hz` narrows to a mains region.
3. **Vendor event vocabulary** — converter-derived event ID strings and taxonomies are near-signatures of the acquisition software (partially out of scope here; see Phase 4).
4. **Channel labels** — naming conventions (`EEG Fp1-A1` vs `Fp1-REF` vs `Fp1:M1`), auxiliary/DC channel names, case and spacing habits.
5. **Channel order** — the acquisition-template order verbatim; labels vary between sites on the same hardware, the template order tends not to.
6. **Header numerics** — physical/digital ranges, sampling rates, record duration are vendor defaults; not fixable without resampling/requantisation, so accepted as residual.

The same information leaks through two surfaces that must be kept in agreement:

- **Bytes**: the served EDF/BDF file (download, range, time-slice, proxy offload, FUSE).
- **Metadata**: `SignalInfoOut` in [recordings/api/v1/ninja.py](../../recordings/api/v1/ninja.py), which serves `label`, `transducer_type`, `prefiltering`, and `index` verbatim to every reader with read access — grantees, share-token holders, and federated peers — in structured, machine-readable form. This is the *louder* leak: no byte parsing required.

Cleaning at ingest keeps the two surfaces in agreement by construction: both describe the file on disk.

## Design principles

- **The stored file is the clean artifact; raw values are author-private database fields.** This is how subject PHI already works — `process_edf_file` rewrites the header in place for every upload (both the restructuring and in-place branches, converter output included), and the raw upload survives only on the write-only originals volume. Channel metadata follows the same shape, with one refinement: raw labels, transducer strings, and prefiltering strings have clinical QA value (`EEG Fp1-A1` vs `EEG Fp1-REF` is a semantic statement about the reference scheme), so they are captured into `source_*` fields on `SignalInfo` before the rewrite and served only to the author, mirroring the `original_name` / `display_name` split.
- **One transform site.** The cleaning is a pure transform over the parsed `EdfSignalInfo` list (`deidentify_signal_infos`), applied by ingest before the header rewrite and by the federation defense-in-depth middleware after parsing. `_build_clean_header` stays what it is — a serializer of whatever it is given — so its existing ⚠️ load-bearing subject-PHI contract is untouched, and there is no second implementation of the cleaning rules to drift from the first.
- **Fail closed on unresolved labels.** `canonicalise_label` ([recordings/processors/channel_labels.py](../../recordings/processors/channel_labels.py)) returns `""` for anything it cannot resolve. A policy of "keep the raw label when unresolved" would leak precisely where it matters most — DC channels, `X1`–`X11`, photic, trigger, and vendor aux channels are simultaneously the hardest to canonicalise and the strongest site fingerprint. Unresolved channels are named `MISC_1..n` (numbered in channel order, stable per recording), never their raw label — underscored, because the unseparated `MISC<n>` form embedded electrode tokens (`MISC3` ⊃ `C3`) that substring-matching consumers resurrected as false EEG channels. Trailing prime marks (`C3'`, modified-position notation) are normalised away on resolution: a primed montage never coexists with its unprimed originals, so stripping keeps the channels usable by every canonical-name consumer and removes the prime convention's own site fingerprint, while `source_label` keeps the primed original author-private. The annotation channel label (`EDF Annotations` / `BDF Annotations`) is preserved verbatim — EDF+ readers and EDF+D timekeeping depend on it.
- **Ingest placement fails closed at the pipeline level too.** A file the parser cannot handle fails processing, and FAILED recordings are hidden from everyone but the author. This is strictly stronger than the v1 serve-time design, which inherited the middleware layer's deliberate fail-open-on-parse-error.
- **Database rows describe the file on disk.** `SignalInfo.label` / `transducer_type` / `prefiltering` hold the *cleaned* values after Phase 1, exactly as [recordings/metadata.py](../../recordings/metadata.py) `refresh_signal_metadata` expects — a re-derive from disk reproduces them. The `source_*` fields are the one thing a re-derive cannot reproduce, so the refresh path must preserve them explicitly; this is called out in Phase 1 rather than left to be discovered.

## Phase 1 — ingest-time channel-block de-identification + source capture

The core phase; covers both surfaces at once because both derive from the cleaned state.

**Transform** (`deidentify_signal_infos`, in [recordings/processors/edf.py](../../recordings/processors/edf.py) beside the parsing it consumes), per signal:

| Field | Cleaned value |
|---|---|
| `label` | `canonicalise_label` result when non-empty, else `MISC_<n>`; annotation channels untouched |
| `transducer_type` | `""` |
| `prefiltering` | reconstructed from the parsed `(highpass, lowpass, notch)` values in a fixed format (e.g. `HP:0.5Hz LP:70Hz N:50Hz`, omitting unset values) — the *information* survives, the vendor's formatting fingerprint does not |

Before overwriting, the transform records each signal's original `label`, `transducer_type`, and `prefiltering` on the `EdfSignalInfo` (new `source_*` attributes), so ingest persistence sees both.

**Duplicate-canonical collisions.** Reference stripping can make distinct source channels collide on one canonical name — `Fp1-A1` + `Fp1-A2` both canonicalise to `Fp1` (a mixed-reference export), and writing duplicate labels into the header would destroy montage identity for every reader. Colliding EEG channels fall back to the reference-preserving form (`canonicalise_label_keep_reference`: `Fp1-A1`, with the reference token normalised through the electrode table), keeping labels unique and the ipsi/contra distinction intact. A channel still duplicated after that (true duplicates in the source) keeps its first occurrence and demotes the rest to `MISC_<n>`. Non-EEG duplicates (two `ECG` channels) are left as-is — they match vendor reality and carry no montage identity.

**Call site**: `process_edf_file`, after parsing and before either rewrite branch (`normalise_edf_records` and the in-place `rewrite_edf_header` path), so the cleaned values land in the written header on both. The returned `signal_infos` then describe the file on disk, which is the function's documented postcondition already.

**Persistence**: three new author-private columns on `SignalInfo` — `source_label`, `source_transducer_type`, `source_prefiltering` — written by `_save_edf_results` from the captured values. `refresh_signal_metadata` preserves existing `source_*` values when replacing rows (a re-derive from the cleaned file has nothing to derive them from).

**API**: `SignalInfoOut` gains the three `source_*` fields, populated only when the caller passes the author-fields check that already gates `original_name` / `processing_error` (`_build_recording_out` computes `can_see_author_fields` for exactly this purpose); `null` otherwise. The primary `label` / `transducer_type` / `prefiltering` fields need no gating — they are clean by construction. `index`, `sample_count`, `sampling_rate` stay verbatim and accurate for the served layout (federated peers use them to compute download sizes; see [recordings/README.md → RecordingMeta and SignalInfo](../../recordings/README.md#recordingmeta-and-signalinfo)). Physical/digital ranges stay verbatim as accepted residual (header numerics, above).

Non-author readers keep *usable* channel names because the cleaned labels are canonical electrode names — the viewer builds montages from them directly.

**Contract tests**:

- Byte-level, `TestRewriteEdfHeader`-style: per-field assertions on the written header (canonical label bytes, blanked transducer, reconstructed prefiltering, `MISCn` for unresolvables, annotation-channel label preserved verbatim), on both the in-place and restructuring branches, EDF and BDF.
- Disk–DB agreement: after ingest, header labels parsed from the file equal `SignalInfo.label` rows.
- Metadata gating: author sees `source_*`, grantee / share-token / federated caller sees `null`; a raw transducer string appears nowhere in a non-author response.
- Refresh preservation: `refresh_signal_metadata` on a cleaned file keeps `source_*` intact.

## Phase 1b — montage-shape assessment

Some exports cannot feed the downstream consumers that assume a referential montage (remontaging, trend computation, epoch generation) — bipolar-chain exports and mixed-reference exports being the observed cases. Detection keys on the *parsed structure*, not raw label strings: `classify_channel` already distinguishes bare canonicals (referential), `A-B` pairs (bipolar), and unresolved channels, and duplicate bare canonicals before collision handling are the mixed-reference signature. Raw-string duplicate detection is specifically wrong here: a project's EDF middleware can write `<label>_orig` source-copy channels into stored files (one does), and platform-processed files can be re-uploaded, so a suffix-blind detector would false-positive on the platform's own output.

As implemented:

- `assess_channel_layout` in [recordings/processors/channel_labels.py](../../recordings/processors/channel_labels.py) — pure, duck-typed over parsed infos and model rows alike, and fully re-derivable from a *cleaned* file: kept-reference collision forms re-strip to duplicate bares (`mixed` survives a reprocess) and `MISC_<n>` labels stay unresolved.
- `RecordingMeta.channel_layout` — `referential` / `bipolar` / `mixed` / `unknown`, written at ingest and recomputed by `refresh_signal_metadata`.
- `RecordingMeta.unresolved_channel_count` — how many non-annotation channels fell back to `MISC_<n>`; `0` means the cleaner fully normalised the recording. Denormalised so re-sweeps after vocabulary improvements and capability gates need no `SignalInfo` join; both fields are content-free and served to every reader.
- `DERIVED_COPY_SUFFIX = "_orig"` registered in `channel_labels`, referenced by a project's derived-channel writer and detector code. `classify_channel` types `<label>_orig` as `misc` with the suffixed canonical (`Fp1_orig`) when the base resolves — so the pairing convention survives re-ingest through the de-identification pass — and fail-closed `MISC_<n>` when it does not. The base is resolved under biological-modality priors only, keeping re-classification idempotent. (The viewer's `correctedChannelSuffix` constant is the same convention on the frontend side; it stays literal there.)
- A shape-incompatible recording is **not** FAILED — it parses, views, and serves fine. FAILED-hiding semantics (invisible to grantees) would be wrong for a viewable file; instead, capability gates on the consumers that need a referential layout check `channel_layout` and refuse with a clear message. The gates themselves land with their consumers, not here.

Contract tests: [recordings/tests/test_channel_layout.py](../../recordings/tests/test_channel_layout.py) — derived-copy classification (resolution, fail-closed base, idempotency, constant value), the layout verdicts per shape, ingest persistence, refresh re-derivation, `_orig` survival through the cleaning pass, and grantee-visible serving.

## Phase 2 — federation middleware parity (defense-in-depth)

`AnonymizeEDFHeader` ([federation/middleware.py](../../federation/middleware.py)) parses the raw header, runs `deidentify_signal_infos` over the parsed infos, and delegates to `_build_clean_header` — the serve-time layer applies the same channel cleaning as ingest, with no separate middleware class and no pipeline-shape change. Because the cleaning lives on the class itself, the FUSE default — which instantiates `AnonymizeEDFHeader` directly — carries it with no separate wiring; the HTTP and FUSE surfaces cannot diverge. For locally ingested files this is a no-op — the stored file is already clean — which is precisely the role the middleware already plays for subject PHI. It exists for the margins: files ingested before Phase 1 (none in the sandbox; margin note above) and any future path that serves bytes not produced by this platform's ingest.

The middleware layer keeps its deliberate fail-open-on-parse-error behaviour; with ingest as the primary control, that trade now bounds only the defense-in-depth layer, not the control itself.

[federation/middleware.py](../../federation/middleware.py) is ⚠️ load-bearing: the cleaning contract covers the channel block alongside the subject fields, and the contract tests in [federation/tests/test_middleware.py](../../federation/tests/test_middleware.py) pin both — channel-field assertions (cleaned labels, blanked transducers, reconstructed prefiltering, raw-fingerprint byte absence, isometry, annotation-label preservation) next to the existing `"X X X X"` checks. The AGENTS.md load-bearing table entry names the full contract.

## Phase 3 — canonical channel order at ingest

**Primary justification is de-identification, not performance.** The acquisition-template order is a sharper vendor/site fingerprint than the labels: labels vary between sites on identical hardware, the template order tends to be the vendor default verbatim. The performance argument alone would not justify this phase — within one EDF data record all channels are already contiguous, so whole-record readers gain nothing, and per-channel strided reads gain only a constant factor from pair adjacency. But since de-identification forces the choice of *some* single fixed canonical order, choosing **homologous-pair order** costs nothing extra and takes the trend-computation win as a free rider. (If this phase were ever dropped, the correct performance answer is a derived per-channel store, not permuting the archival file.)

Implemented as `reorder_edf_channels` ([recordings/processors/edf.py](../../recordings/processors/edf.py)), the final pass of `process_edf_file` — it runs after de-identification and after either rewrite branch (in-place or `normalise_edf_records` restructuring), so it always operates on the settled record layout. Each data record is permuted locally by slicing the per-channel blocks and concatenating them in canonical order, streaming record-by-record with bounded memory; sample bytes move verbatim (bit-exact, including annotation TALs), and a short record read raises `EdfParseError` (→ FAILED recording) rather than permuting a truncated tail. Never touches the originals-preservation copy, which stays byte-identical to the upload by design.

**Canonical order spec** — `CANONICAL_EEG_ORDER` + `CHANNEL_ORDER_VERSION` in [recordings/processors/channel_labels.py](../../recordings/processors/channel_labels.py):

1. EEG channels in the fixed 10-10 homologous-pair sequence (anterior→posterior, pairs adjacent, lateral before medial, midline after its row's pairs), keyed by `eeg_order_rank` — a bare electrode sorts immediately before its derivations, so kept-reference and bipolar forms stay next to their primary.
2. EOG, then EMG, then EKG (each keeping original relative order internally).
3. Everything else — aux, trigger, unresolved `MISC_<n>`, derived `_orig` copies — in original relative order.
4. Annotation channels last.

`RecordingMeta.channel_order_version` records which spec a file was written under (`0` = unordered), so downstream code (viewer montage setup, compute caching, future spec revisions) never has to guess the layout. It is stamped at ingest and deliberately not touched by `refresh_signal_metadata` — a refresh cannot know which spec wrote the bytes.

**Invertibility**: the originals volume is write-only, so the served file is the only readable copy — the permutation must be recoverable from DB state alone. `SignalInfo.index` remains the on-disk position (consistent with the describe-the-disk rule); the `source_index` column stores the original file position, author-private like the other `source_*` fields and preserved by `refresh_signal_metadata` under the same label-identity guard.

**The mechanical traps are pinned by tests** ([recordings/tests/test_channel_order.py](../../recordings/tests/test_channel_order.py)): bit-exact per-channel sample movement under mixed sampling rates for both EDF (2-byte) and BDF (3-byte) widths, an annotation channel starting mid-file landing last with its timekeeping TALs intact, `source_index` recording the permutation, a canonical-order file passing through unchanged, and — via `compute_signal_info_digest` being computed at ingest after reordering — digest consistency by construction. *(Margin note: with a live back catalogue the reorder would be a re-digest campaign; in the sandbox it is a non-event.)*

## Phase 4 — annotation vocabulary (scope statement, deferred)

Ingest extracts annotation text to the database and (by default) strips it from the stored file, but the annotations API serves DB-stored event labels verbatim. Converter-derived events carry vendor taxonomies (event ID strings are close to a software signature on their own), and free-text events routinely embed original channel names ("spike Fp1-A1"), which reintroduces exactly the labels Phases 1–3 scrub. A vendor converter (the Nicolet `.e` converter installs as a separate package and registers through `RECORDING_CONVERTERS`) already drops or cleans a large class of these at conversion time, which bounds but does not close the surface.

Deferred because the right fix (canonical channel-name substitution inside event text, vocabulary mapping for ID strings) needs its own design pass. Until then it is a **documented limitation**: if site de-identification is stated as a control anywhere user-facing, the annotations API must be named as out of its scope.

## Residual risk — what this plan does not close

- **Channel count and modality set.** "This site records 4 DC channels and SpO2" survives every phase — mapping exotic channels to `MISC` keeps the recording intact but preserves the set. Closing it would mean dropping data; out of scope.
- **Header numerics.** Physical/digital calibration ranges, sampling rates, and record duration are vendor defaults, unfixable without resampling/requantisation. Out of scope.
- **Mains frequency.** The notch value survives in the canonical prefiltering string because the filtering UI needs it; it discloses a 50 Hz vs 60 Hz region.
- **Reference designators on collision fallback.** A mixed-reference export keeps its reference tokens (`Fp1-A1` vs `Fp1-REF`) for the colliding channels — a partial naming-convention residue, bounded to exactly the channels where uniqueness forces it.
- **Fail-open parsing in the defense-in-depth layer.** The federation middleware serves malformed or vendor-extended headers raw (inherited, deliberate). The primary control does not share this: an unparseable file fails ingest and is FAILED-hidden.
- **Author-side disclosure.** The author's own downloads carry the cleaned file (as with subject PHI today); raw channel metadata survives only as `source_*` fields and on the operator's originals volume.
- **Annotation vocabulary** until Phase 4.
- **The control is pool-shrinking mitigation, not anonymisation.** It raises the k in k-anonymity; it does not make recordings unattributable, and no user-facing text should claim otherwise.

## Sequencing

1. **Phase 1** — transform + collision fallback + `source_*` capture + persistence + API gating + contract tests. No dependencies. **Implemented.**
2. **Phase 1b** — `channel_layout` + `unresolved_channel_count` + `_orig` suffix registration. **Implemented.**
3. **Phase 2** — one-line middleware parity + contract-test extension. **Implemented.**
4. **Phase 3** — reordering + `source_index` + `channel_order_version`. **Implemented.**
5. **Phase 4** — deferred; carried as a documented limitation and a ROADMAP entry.

Review agents: `phi-exposure` covers Phases 1–2 diffs (Out-schema changes are its explicit trigger); `load-bearing-diff-reviewer` fires on [recordings/processors/edf.py](../../recordings/processors/edf.py), [federation/middleware.py](../../federation/middleware.py), and the audit-digest file; `gdpr-compliance` on the `source_*` model fields (they carry no *personal* data of the platform user, but the agent should see and record that judgment). The clean-slate pass runs at the end of each phase per AGENTS.md.
