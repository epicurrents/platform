# recordings

EDF/BDF file upload, processing, storage, and delivery. Owns the ingest pipeline (and the registry that lets projects override it), the format-converter registry that lets non-EDF inputs be converted at ingest, the soft-delete / purge lifecycle, and the bulk-import management command.

## Lifecycle of a recording

```
upload (POST /upload)
    │
    ▼
staging directory                  ← RECORDINGS_STAGING_PATH
    │  Recording row created with status=PENDING
    │  process_recording dispatched via transaction.on_commit
    ▼
process_recording Celery task
    │  opens with_system_activity("recordings.process", interface=CELERY)
    │  if RECORDINGS_PRESERVE_MODE == "all": copy staging file
    │    to RECORDINGS_ORIGINALS_PATH (host-controlled volume)
    │  move to RECORDINGS_UPLOAD_PATH
    │  os.utime → epoch 0 (de-identification)
    │  if extension has a converter: run it, replace stored file
    │  parse EDF/BDF header, strip annotation TALs (default)
    │  de-identify channel block: canonical labels, blank
    │    transducers, reconstructed prefiltering (source_* kept)
    │  reorder channels into canonical homologous-pair order
    │    (source_index records the original position)
    │  write RecordingMeta + SignalInfo rows
    │  write Interruption + "Original annotations" rows
    │  on processing failure: populate Recording.processing_error
    │    and (if mode in {"failed", "all"}) preserve the original
    │  audit: final transition row carries a SignalInfo digest in
    │    extra_payload (see Audit-trail contribution below)
    │  push notification to author
    ▼
status = READY (or FAILED — hidden from grantees; see Phase 1 rule)
    │
    ▼
serve (GET /{hash})
    │  raw bytes for author / superuser
    │  middleware-piped for AccessRight grantees with apply_middleware=True
    ▼
soft-delete (DELETE /{hash})
    │  Recording.deleted_at set; row hidden from default queries
    ▼
purge_deleted_recordings Celery task
    │  hard-deletes rows past RECORDINGS_TRASH_RETENTION_DAYS (default 30)
    │  also reaps orphaned PENDING / PROCESSING rows past the same cutoff
```

## Models

### `Recording`

The canonical file row. Fields:

| Field | Notes |
|---|---|
| `author` | FK to user; cascades on delete. |
| `original_name` | Filename as uploaded. Visible only to the author and superusers — grantees, share-token holders, and federated peers see `null` in API responses. Can carry PHI (`MRN_12345_routine.edf` and similar) so all grantee-facing surfaces use `display_name` instead. **Not user-mutable** (PATCH does not accept it), but is rewritten by the ingest pipeline when a converter runs — for example a `.e` upload becomes `<stem>.edf` after the Nicolet converter so the filename matches the stored format. |
| `display_name` | Nullable. Grantee-visible label. Defaults to the `stored_name` hash prefix (first 8 chars, uppercase) when unset. Editable via PATCH; set to `""` to clear. The collection bulk-rename endpoint writes this field. |
| `stored_name` | Unique name under `RECORDINGS_UPLOAD_PATH`. Format: 32 hex chars + extension. Never derived from user input. **Immutable** across the recording's lifetime (the file is rewritten in place during anonymisation, but the name is not). |
| `file_extension` | Lowercased with leading dot. Updated by the converter when format is changed. |
| `file_size`, `file_hash` | SHA-256 of file bytes. Recomputed after conversion. |
| `content_hash` | SHA-256 over `file_hash + serialised(Recording)`. A content fingerprint, not the URL identifier: the public `hash` in `/recordings/api/v1/{hash}` is the 32-hex-char prefix of `stored_name`, which is random, stable for the row's lifetime, and what the by-hash resolvers match on. `content_hash` can change when the platform rewrites the file (anonymisation). |
| `status` | `pending` → `processing` → `ready` \| `failed`. FAILED recordings are visible only to the author and superusers — every grantee surface filters them out (see [FAILED-hidden rule](#failed-hidden-rule)). |
| `processing_error` | Populated when ingest fails so the FAILED state carries enough information for the author to act. Capped at 4 KB. Same visibility rule as `original_name` — author + superuser only. |
| `modality` | Inferred during processing — `eeg`, `emg`, etc. The upload API forces `eeg` regardless of header content (it's EEG-only); other paths use channel-label heuristics. |
| `power_line_frequency` | Nullable. Operator-set mains frequency in Hz (50 EU / 60 US) for the recording's environment; `null` = inherit the deployment `EEG_MAINS_HZ` default. Lives here (not on `RecordingMeta`) so it survives reprocessing. Not personal data. Batch-set via `POST /set-mains`; resolved for detectors/BIDS by the mains resolver in the compute app (precedence: explicit request value → this field → `EEG_MAINS_HZ`). The header-parsed `SignalInfo.notch` is advisory evidence only, not this value. |
| `deleted_at` | Soft-delete timestamp. `null` for active recordings. |
| `events`, `interruptions`, `labels`, `annotations` | `GenericRelation` reverse accessors for annotation rows that target this recording. |
| `access_rights`, `collection_memberships`, `dataset_memberships`, `tagged_items` | `GenericRelation` reverse accessors for `AccessRight`, `CollectionItem`, `DatasetItem`, and `TaggedItem` rows. These plus the four annotation accessors above make Django's `Collector` cascade-delete every row that targets the recording when the recording itself is hard-deleted. Soft-delete (setting `deleted_at`) does not trigger the cascade. See [AGENTS.md → GenericFK target cascade pattern](../AGENTS.md#genericfk-target-cascade-pattern) for the project-wide rule. |

### `RecordingMeta` and `SignalInfo`

Format-level and per-channel metadata extracted from the EDF/BDF header at processing time.

`RecordingMeta` is one-to-one with the recording but stored via generic FK (`content_type` + `object_id`) so the meta system can extend to other content types later. Carries `format`, `duration`, `data_record_count`, `data_record_duration`, `signal_count`, `discontinuous`, and `recording_date` — which is always nulled by de-identification at ingest — plus the montage-shape assessment: `channel_layout` (`referential` / `bipolar` / `mixed` / `unknown`, computed by `assess_channel_layout` in [processors/channel_labels.py](processors/channel_labels.py) from the resolved EEG canonicals) and `unresolved_channel_count` (non-annotation channels the canonicaliser could not resolve; `0` means fully normalised). Consumers that assume a referential montage (remontaging, trend computation, epoch generation) gate on `channel_layout` instead of discovering the shape at failure time; a shape-incompatible recording is never FAILED — it parses, views, and serves fine. Both values are content-free, served to every reader, and re-derived by `refresh_signal_metadata`. `channel_order_version` records which canonical channel-order spec (`CHANNEL_ORDER_VERSION` in [processors/channel_labels.py](processors/channel_labels.py)) the stored file was written under — `0` means unordered; it is stamped at ingest and never re-derived, since a refresh cannot know which spec wrote the bytes. Detection keys on the parsed structure, never raw label strings: platform-written `<label>_orig` derived copies (the signal-repair convention, registered as `DERIVED_COPY_SUFFIX` in [processors/channel_labels.py](processors/channel_labels.py)) classify as `misc` with a suffixed canonical, so a re-uploaded platform-processed file cannot read as a duplicate-electrode export — nor lose its `_orig` pairing to the `MISC_<n>` fallback at ingest.

When `discontinuous` is true, the detail response embeds each interruption's timing (`start`, `duration` in data-position seconds) alongside its `object_hash`, not just the reference. This lets the viewer seed a complete, trusted gap table from the metadata alone: the viewer needs the full interruption table to allow random access on a discontinuous recording, and without the embedded timing it would fall back to discovering gaps by decoding and clamp navigation to the decoded span. The starts share the data-position time base the interruption rows store, so they map to the viewer's cache timeline without conversion.

`SignalInfo` is one row per channel, ordered by `index`. Stores the full EDF channel descriptor (`label`, `canonical_label`, `signal_type`, `physical_unit`, `transducer_type`, `prefiltering`, physical/digital min/max, `units_per_bit`, `digital_offset`, `sample_count`, `sampling_rate`, parsed `highpass` / `lowpass` / `notch`, `is_annotation_channel`) plus the author-private `source_label` / `source_transducer_type` / `source_prefiltering` originals described below. Used by federation to compute download sizes without re-parsing the file — see [federation/middleware.py](../federation/middleware.py).

**Channel-block de-identification.** `deidentify_signal_infos` in [processors/edf.py](processors/edf.py) runs inside `process_edf_file`, so the stored file's channel labels, transducer fields, and prefiltering strings never carry acquisition-site conventions (the site-fingerprint threat model is in [docs/engineering-notes/channel-deidentification-plan.md](../docs/engineering-notes/channel-deidentification-plan.md)). Labels become the canonical name, or `MISC_<n>` when the resolver returns none — fail-closed, because unresolvable vendor labels are the strongest site fingerprint; annotation channels keep their spec-mandated label. The underscore in `MISC_<n>` is load-bearing: the earlier `MISC<n>` form embedded electrode tokens (`MISC3` contains `C3`), which substring-matching consumers — the viewer's setup matcher among them — resurrected as false EEG channels. Trailing prime marks are normalised away (`EEG C3'` → `C3`): a primed montage never coexists with its unprimed originals — the primes are the montage — so stripping loses nothing within a recording, keeps the channels usable by every canonical-name consumer, and removes the prime convention's own site fingerprint; the primed original survives in `source_label`. When reference stripping would collide two channels on one canonical (`Fp1-A1` + `Fp1-A2` → `Fp1`), the colliding EEG channels keep their reference (`canonicalise_label_keep_reference`), and any residual duplicate demotes to `MISC_<n>` — the written header never carries duplicate EEG labels. Transducers are blanked; prefiltering is reconstructed from the parsed filter values in the spec-suggested format. The originals are captured to `source_*` before the rewrite: they are **author-private** (serialized as `null` to everyone else, exactly like `original_name`) and are the one part of a row `refresh_signal_metadata` cannot re-derive, so the refresh carries them over by channel index.

**Canonical channel order.** `reorder_edf_channels` runs as the final ingest pass, permuting each data record so channels appear in a fixed order: EEG in the homologous-pair sequence (`CANONICAL_EEG_ORDER` in [processors/channel_labels.py](processors/channel_labels.py) — pairs adjacent, so per-channel strided reads over left/right pairs touch adjacent blocks), then EOG, EMG, EKG, then aux/unresolved channels in original relative order, annotation channels last. The acquisition-template order is itself a site fingerprint, which is the primary reason for the pass; the pair-adjacency read locality is a free rider. Sample bytes move verbatim (bit-exact, 3-byte samples for BDF), the transform streams record-by-record with bounded memory, and a short record read raises rather than permuting a truncated tail. Each channel's original file position lands in `SignalInfo.source_index` (author-private, carried over by refresh like the other `source_*` fields); `RecordingMeta.channel_order_version` names the spec the file follows.

`canonical_label` is a conservative canonical channel name derived from the raw `label` at ingest by [`processors/channel_labels.py`](processors/channel_labels.py). EEG electrodes use the 10-10 standard (`EEG T3-Ref` → `T7`; referential montages lose their reference suffix, genuine bipolar pairs become `A-B`); EOG → `LOC` / `ROC` / `EOG` (`E1`/`E2` taken as left/right per AASM; the primary of a derivation like `E1-M2` decides the side); EMG → `EMG/<site>` (`Chin`, `LegL`, `LegR`, `ArmL`, `ArmR`; derivations like `Chin1-Chin2` collapse to the site); EKG → a lead name, a kept `ECGn`, or `ECG`. Matching is conservative — anything not well-known (individual muscle names, a lone ambiguous `LAT`, bare cardiac leads) stays `''`; the channel is then written as `MISC_<n>` by the de-identification pass, with the raw name surviving in the author-private `source_label`. The same resolver (`classify_channel`) also **refines `signal_type`**: a bare `Fp1` / `C3` with no `EEG` marker is typed `eeg`, since a recognised electrode is definitionally EEG. `canonical_label` is recomputed on every reprocess from the stored `label`, so it cannot drift; it exists so `to_bids`, the spike detectors, the YASA remontage, and the forward model share one name instead of each re-implementing affix-stripping. Old→new EEG mapping covers only the unambiguous position renames (T3/T4/T5/T6 → T7/T8/P7/P8); A1/A2 are kept verbatim, never remapped to the mastoid M1/M2. Trailing primes are stripped on resolution (`C3'` → `C3`) — the channel-block de-identification paragraph earlier in this section carries the reasoning. Backfill existing rows with `python manage.py backfill_canonical_labels` (idempotent; `--show-unclassified` lists non-standard labels; it writes only `canonical_label`, never `signal_type`, so type improvements need a reprocess). `canonical_label` is **excluded** from the `audit_digests.py` SignalInfo digest below — a deterministic function of the already-covered `label`, so it adds no tamper-detection value and its backfill can't invalidate digests baselined before it existed. `signal_type`, by contrast, *is* in the digest — which is exactly why its refinement is ingest/reprocess-only.

**Both rows describe the file as it was at ingest, and nothing keeps them there.** Anything that rewrites the stored file afterwards — a project's reprocessing stage, a converter re-run — must call `refresh_signal_metadata` from [recordings/metadata.py](metadata.py) or the rows are left describing a file that no longer exists. That drift is not cosmetic and not loud: every byte-serving path sizes the EDF header as `256 * (1 + meta.signal_count)`, and a header read short of its real length parses its per-signal fields as zeroes rather than raising, so the serving pipeline computes record geometry from a record size of nothing and cuts the response at the wrong offsets. Repair an already-drifted deployment with `python manage.py refresh_signal_metadata` (`--dry-run` reports without writing, `--recording <content_hash>` scopes to one). It is idempotent, so a sweep over recordings that are already correct writes nothing.

The refresh deliberately leaves `Interruption` and "Original annotations" rows alone: both are derived from signal *content*, which a header re-read has no view of, and recreating them would duplicate rows a user may have edited.

**Audit-trail coverage.** `SignalInfo` rows are bulk-created in `_save_edf_results` and don't fire `post_save` signals individually. Per-row chain entries would inflate `ObjectChangeLog` by tens of rows per upload without analytical benefit — the channel descriptors are deterministic functions of the EDF header. Instead, [recordings/audit_digests.py](audit_digests.py) computes a sha256 digest over the full set of `SignalInfo` rows for a recording, and `process_recording` embeds that digest in `extra_payload` on the final READY transition's audit row. See [activity/README.md → Derived-row digests](../activity/README.md#derived-row-digests) for the verification contract and threat-model coverage.

### `ImportJob` and `ImportJobFile`

Progress tracking for the bulk-import management command. `ImportJob` is one row per `import_recordings` invocation; `ImportJobFile` is one row per file discovered. The job's `status` is `in_progress` → `completed` | `aborted`; only one `in_progress` job may exist at a time. Files can resume from where the previous run stopped — see the [bulk import](#bulk-import-via-import_recordings) section.

## Ingest pipelines

A *recording pipeline* is a named set of processing options applied to an EDF/BDF file at ingest. Defined in [pipelines.py](pipelines.py) as the `RecordingPipeline` dataclass:

```python
@dataclass
class RecordingPipeline:
    header: HeaderPipelineOptions = field(default_factory=HeaderPipelineOptions)
    signals: SignalPipelineOptions = field(default_factory=SignalPipelineOptions)
```

`HeaderPipelineOptions.strip_annotation_text` defaults to `True`: text TALs are removed from the stored EDF file at ingest. The text is always saved to the database as an "Original annotations" `Annotation` first, so nothing is lost — only the on-disk copy is anonymised. `SignalPipelineOptions` is currently a placeholder for future signal-level transforms (channel filtering, downsampling, etc.).

### Built-in pipelines

| Label | Used by | Defaults |
|---|---|---|
| `"web"` | `process_recording` Celery task (every HTTP upload) | `strip_annotation_text=True` |
| `"import"` | `import_recordings` management command | `strip_annotation_text=True` |

Both built-ins are identical today. They exist as separate labels so a deployment can diverge them without forking code.

### Per-request opt-out

The default strip behaviour can be turned off for a single recording without changing the pipeline definition:

- **HTTP upload** — multipart field `preserve_annotations=true`. Inspected by `process_recording(recording_id, preserve_annotations=True)`, which sets `strip_annotation_text=False` on the resolved pipeline before processing.
- **Bulk import** — `--preserve-annotations` flag on `import_recordings`.

### Overriding or adding pipelines

Set `RECORDING_PIPELINES` in settings ([common.py](../epicurrents/settings/common.py) has the documented default of `{}`). Three value forms are accepted:

```python
RECORDING_PIPELINES = {
    # 1. Dict — partial overrides merged onto the default pipeline.
    "web": {"header": {"strip_annotation_text": False}},
    # 2. Dotted import path — a RecordingPipeline instance, or a zero-arg
    #    factory callable returning one. Resolved lazily.
    "research": "mysite.pipelines.research_pipeline",
    # 3. RecordingPipeline instance — used as-is.
    "operator": RecordingPipeline(
        header=HeaderPipelineOptions(strip_annotation_text=False),
    ),
}
```

`get_pipeline(label)` resolves a label using:

1. `RECORDING_PIPELINES` if defined and the label is present.
2. Built-in `"web"` / `"import"` if the label matches.
3. `ValueError` otherwise.

New labels can be added without forking the upload endpoint — the management command takes `--pipeline <label>`. The HTTP upload endpoint always uses `"web"`.

## Format converters

A *converter* turns a non-EDF input file into an EDF before the rest of processing runs. The contract is:

```python
def convert(input_path: Path, output_dir: Path) -> Path | tuple[Path, dict | None]: ...
```

Return either the EDF path alone, or a two-tuple `(edf_path, sidecar_data)`. When a sidecar dict is returned, the [post_convert hook](#conversion-hooks) dispatcher fires registered handlers — the built-in [sidecar module](converters/sidecar.py) parses Nicolet-shaped sidecars and writes a "Source events" `Annotation` on the recording, distinct from the "Original annotations" record written from the EDF+ TAL parse. Plugin-supplied converters that emit a different sidecar shape register their own post_convert handler.

After conversion, `Recording.stored_name`, `file_extension`, `file_hash`, `file_size`, and `original_name` are updated to reflect the EDF.

### Built-in converters

| Extension | Converter | Source |
|---|---|---|
| `.csv` | Tabular signal data to EDF via a registry of per-format subconverters | [converters/csv2edf.py](converters/csv2edf.py) |

Converters for vendor formats (Nicolet/Nervus `.e`, for instance) are separate packages registered through `RECORDING_CONVERTERS`; the platform carries no vendor-specific conversion code. A converter that emits a JSON sidecar has it saved as an `Annotation` named `"Source events"` (the generic name used for sidecar-derived events from any converter). A converter that would emit more than one EDF for one input should raise `ConversionError` and fail the task rather than pick a segment.

### Registering or disabling converters

Set `RECORDING_CONVERTERS` in settings:

```python
RECORDING_CONVERTERS = {
    # Disable the built-in .e converter — uploads with .e extension fail.
    ".e": None,
    # Register a custom converter for another format.
    ".ncs": "mysite.converters.ncs.convert",
    # Or a direct callable.
    ".smr": my_smr_converter,
}
```

`get_converter(ext)` lookup order:

1. `RECORDING_CONVERTERS` if defined. `None` value explicitly disables.
2. Built-in registry (`.e` and `.csv` at present).
3. Returns `None` — no conversion, pass through to EDF processing.

Extensions are normalised to lowercase with a leading dot before lookup.

### CSV subconverters

`.csv` is not one format — the same extension covers many device exports — so [csv2edf.py](converters/csv2edf.py) splits the work. Shared logic parses the leading `# key: value` comment block, the column header, and the numeric table into a `CsvDocument`, then hands it to a list of *subconverters*. The first whose `detect()` accepts the document wins; its `build()` maps columns to `EdfChannel` objects, which the shared layer writes to a de-identified EDF through `write_edf` in [processors/edf.py](processors/edf.py). No match raises `CsvConvertError`, failing the recording with a message naming the known formats.

`write_edf` derives each channel's physical min/max from its own data range, so an axis carrying a large DC offset — the gravity-loaded accelerometer axis — keeps full 16-bit resolution over its actual span instead of a symmetric scale. It is used only for genuinely float-valued sources; EDF→EDF transforms copy the original integer samples verbatim and never round-trip through float.

Projects add formats by registering a subconverter from their `AppConfig.ready()`:

```python
from recordings.converters.csv2edf import register_csv_subconverter


class MyDeviceConverter:
    name = "my_device"

    def detect(self, doc):
        return doc.header[:1] == ["timestamp"] and "device_id" in doc.comments

    def build(self, doc):
        # return list[EdfChannel]
        ...


register_csv_subconverter(MyDeviceConverter())
```

Registered subconverters are tried before the built-ins, so a project may override a built-in format.

**Built-in format — `tremsys_acc`.** A wrist-accelerometer export: a `time` column followed by `label[unit]` signal columns (e.g. `leftwrist_x[m/s2]`), with the integer sampling rate declared in a `# resampled_fs:` comment. Each non-time column becomes an EDF signal at that rate.

## Conversion hooks

Three extension points fire around the converter call in `process_recording`. Plugins use them to observe or act on the source bytes without modifying the worker:

| Hook | When | Use cases |
|---|---|---|
| `pre_convert(recording, source_path, ext)` | Before the converter runs. `source_path` is the as-uploaded file at its original format. | Preserve source bytes for later inspection; pre-validate against a project schema; log a provenance hash to an external system. |
| `post_convert(recording, source_path, converted_path, sidecar_data)` | After the converter succeeds, before the source file is removed. Both paths still exist on disk. | Parse a converter-specific sidecar (the canonical case); maintain source-to-converted mapping records; run post-conversion validation. |
| `convert_failed(recording, source_path, exception)` | Inside the converter `except`, before the exception re-raises and the task cleanup runs. `source_path` still holds the source bytes. | Preserve source bytes when the converter raises; log a conversion-failure incident. |

### Registration

Register from your app's `ready()` to avoid import-time cycles:

```python
from recordings.pipelines import register_pre_convert, register_post_convert, register_convert_failed


class MyPluginConfig(AppConfig):
    def ready(self) -> None:
        register_pre_convert(my_pre_handler)
        register_post_convert(my_post_handler)
        register_convert_failed(my_failed_handler)
```

Registration is idempotent — the same handler registered twice yields a single entry. Handlers fire in registration order.

### Failure modes

Each `register_*` call accepts a `fail_mode` keyword:

- **`"soft"`** (default) — handler exceptions are caught and logged; ingest continues. Subsequent handlers still run. For preservation, archival, provenance, and any other observer that must never block uploads.
- **`"hard"`** — handler exceptions propagate; ingest aborts and the recording is marked `FAILED` via the outer task error path, with the exception recorded in `processing_error`. For pre-validation that must refuse the upload.

Pick `"hard"` only when the handler's purpose is *gating*. Anything that's observing for side effects (logging, copying, indexing) is `"soft"` — a failure there must not lose the user's upload.

The outer task error path catches *any* unexpected exception (a `"hard"` handler, a missing file, a database error), marks the recording `FAILED`, and writes `Unexpected processing error: …` to `processing_error` rather than deleting the row — so a failure always leaves a record the author and operator can inspect. See [docs/debugging.md → Recording processing failures](../docs/debugging.md#recording-processing-failures).

### Worked example — Nicolet sidecar parser

The Nicolet `.e` converter emits a sidecar dict; the matching post_convert handler lives at [converters/sidecar.py](converters/sidecar.py) and is registered in [apps.py](apps.py):

```python
# recordings/converters/sidecar.py
def _looks_like_nicolet_sidecar(sidecar_data: dict) -> bool:
    """Filter on shape so other converters' sidecars are not parsed by this handler."""
    if not isinstance(sidecar_data, dict):
        return False
    return isinstance(sidecar_data.get("annotations"), list) or isinstance(sidecar_data.get("events"), list)


def handle_post_convert(recording, source_path, converted_path, sidecar_data) -> None:
    if sidecar_data is None or not _looks_like_nicolet_sidecar(sidecar_data):
        return
    save_sidecar_events(recording, sidecar_data)
```

The per-item event schema (`onset_seconds` required and numeric; `duration_seconds`, `text`, `type`, `label` optional and typed) is pinned in [converters/sidecar.py](converters/sidecar.py) and validated by `save_sidecar_events` — a converter emitting different key names fails loudly per recording instead of writing rows of null onsets. Both callers catch and log the `ValueError`, so ingest continues.

The shape filter is load-bearing — the post_convert dispatcher fires every registered handler for every successful conversion regardless of source format, so each handler must self-filter to its own sidecar shape. A plugin author writing a handler for, say, `.ncs` (Neuralynx) emits its own dict shape and filters on those keys; the Nicolet handler ignores it and the Neuralynx handler ignores Nicolet sidecars.

### Why hooks instead of a single hardcoded path

The historical preservation gap motivated the protocol: when `RECORDINGS_PRESERVE_MODE="failed"` and a converter ran, the bytes preserved to the originals volume were the converted EDF, not the user-uploaded source. Special-casing each converter in `process_recording` doesn't scale (converters know their input format, not the platform's preservation policy). The `pre_convert` + `convert_failed` hooks let the preservation module ([preservation.py](preservation.py)) stash source bytes before conversion and write them on failure, without `process_recording` needing to know about the stash. The same extension surface enables plugin-supplied archival, pre-validation, and provenance handlers.

## API

Mounted at `/recordings/api/v1/`. Full request/response detail in [api/v1/ninja.py](api/v1/ninja.py).

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/upload` | Multipart upload. Returns 202 immediately with the staging metadata; poll `/status/{hash}` for processing progress. |
| `GET` | `/status/{hash}` | Current processing status of a recording. |
| `GET` | `/` | List visible recordings for the caller, paginated by `limit`/`offset`. Filterable by `status`, `trash`, and `uncollected` (only recordings in no collection — the library root). |
| `GET` | `/{hash}` | Full recording metadata including `RecordingMeta` and per-channel `SignalInfo`. |
| `GET` | `/{hash}/slice` | Metadata scoped to a time window, with annotations clamped to the slice. |
| `GET` | `/{hash}/annotations` | List events / interruptions / labels for a recording. |
| `GET` | `/{hash}/file` | Binary download. Pipes through the API serve-pipeline when `apply_middleware=True` (see [Serving](#serving-and-the-middleware-pipeline)). |
| `GET` | `/{hash}/file/slice` | Byte-range slice of the binary download. |
| `PATCH` | `/{hash}` | Update mutable metadata (`display_name`, `modality`). `original_name` is immutable — sending it is silently ignored. |
| `POST` | `/set-mains` | Batch-set (or clear, with `null`) `power_line_frequency` across a list of recording hashes. Skips invalid hashes and recordings the caller cannot write; returns `{updated, skipped}`. |
| `DELETE` | `/{hash}` | Soft-delete (sets `deleted_at`). |
| `GET` | `/{hash}/access/` | List the access rights granted on a recording. See [Access management](#access-management). |
| `DELETE` | `/{hash}/access/{right_id}` | Revoke one access right. |

### Access management

Grants on a recording are created at upload (`user_assignments` / `group_assignments` / `share_token` on the upload payload) and by `import_recordings`. These two endpoints are how they are inspected and withdrawn.

Access to *manage* access follows the collection and dataset rule: `can_modify_object` — author or superuser — or the holder of a `can_share` grant, directly or through a group. Staff is deliberately **not** a route in. Who may see a recording is the author's decision; an account administrator with no grant of their own has no business reading the guest list. The listing includes share-token rows with their token values, since every caller who reaches this point could mint or read one anyway, and a share link the owner cannot see is one they cannot audit.

A FAILED recording answers 404 here to everyone but its author and superusers, per the [FAILED-hidden rule](#failed-hidden-rule) — otherwise the access list would confirm that a failed upload exists.

**The author's own grant cannot be revoked**, by anyone, including a superuser. Reading resolves through `AccessRight` and `get_read_access_result` has no author fast-path (only superusers get one), so the self-grant written at upload is the author's sole read access. Deleting it would leave them able to rename and soft-delete the recording — `can_modify_object` *does* check author — while unable to read or download it, and no endpoint creates a grant, so there would be no way back. The refusal is a 409.

`AccessRightOut` is shared with the library endpoints from [epicurrents/api/schemas.py](../epicurrents/api/schemas.py) rather than redefined here, so a field added to `AccessRight` cannot reach one app's access screen and not the other's.

### Upload contract

The upload endpoint accepts these multipart fields beyond `file`:

| Field | Notes |
|---|---|
| `user_access` | Semicolon-separated `user_id:perms` grants. `perms` is any subset of `r`, `w`, `s` (read, write, share). A repeated id, or the uploader's own id, is a 400 — the uploader's full-rights row is created unconditionally, and `AccessRight` enforces one row per target. |
| `group_access` | Same shape as `user_access` but against group IDs. |
| `share_token` | Public read share token. Always created with `can_write=False`, `can_share=False`. A token already in use anywhere returns 409 (the column is globally unique). |
| `share_token_expires_at` | ISO-8601 timestamp. Requires `share_token`. |
| `share_token_apply_middleware` | Default `True`. Whether the token grant pipes the file through the serve pipeline. |
| `preserve_annotations` | Default `False`. When `True`, the stored EDF keeps embedded annotation text. |
| `display_name` | Optional grantee-visible label. When omitted, the field is left null and responses fall back to the `stored_name` hash prefix. The original filename is never used as the display name unless the author explicitly passes it here (or via a later PATCH). |

The endpoint wraps the `Recording` row, the `AccessRight` rows, and the `process_recording` dispatch in a single `transaction.atomic()` block — the Celery worker never sees a row that wasn't committed.

### Serving and the middleware pipeline

The download endpoint resolves the caller's access via [epicurrents.permissions.get_read_access_result](../epicurrents/permissions.py) and inspects `ReadAccessTerms.apply_middleware`:

- **Author or superuser** → raw bytes always. The flag is ignored.
- **Other readers with `apply_middleware=True`** → bytes pass through `_build_serve_pipeline()` in [api/v1/ninja.py](api/v1/ninja.py), which always applies `[AnonymizeEDFHeader, StripAnnotationTextMiddleware]`.
- **Other readers with `apply_middleware=False`** → raw bytes.

The serve pipeline is scope `"api"`; the federation FUSE filesystem builds its own pipeline scoped `"fuse"`. Both share the same middleware classes — see [federation/README.md](../federation/README.md) for the pipeline class hierarchy.

The `Content-Disposition` filename is built from the resolved `display_name` plus the recording's `file_extension` — never the original filename. The author sees their original filename only as an editable metadata field in the API response, not in the on-disk filename of any download.

### FAILED-hidden rule

Recordings with `status=FAILED` are visible only to the author and to superusers. Every grantee-facing surface filters them out so the failure state does not leak:

- `GET /` (recordings list) — omitted from the grantee branch of the queryset.
- `GET /status/{hash}`, `GET /{hash}`, `GET /{hash}/slice` — return 404 for grantees.
- `GET /{hash}/file` (download), `GET /{hash}/file/slice` — return 404 for grantees before reaching the file path.
- Federation `GET /api/v1/federation/inbound/objects/{ct}/{id}/` — collapses into the same 404 + indistinguishable body as a missing object.
- Library collection / dataset / tag item listings — dropped from `_enrich_collection_items` and the tag-items queryset.

Enforcement is two-layer. The read-visibility gate `recording_hidden_from_reader` in [permissions.py](permissions.py), registered with the permission resolver from `RecordingsConfig.ready()`, denies `can_read_object` itself for FAILED (non-author) and trashed recordings — so surfaces outside this app that resolve recordings generically (the annotations API, extension grants) hide them without knowing the rule. `_failed_hidden_for_caller(recording, user, fed)` in [api/v1/ninja.py](api/v1/ninja.py) is the endpoint-side layer: every recording surface checks it *before* its read-permission check, so a grantee with a valid `AccessRight` on a FAILED recording sees 404 rather than the resolver's 403 — indistinguishable from absence, because the data they would receive is meaningless and the failure detail is author-private.

Defense in depth on the serve path: when a recording is somehow READY but `RecordingMeta` is missing (a race window or pathological state), the serve helper refuses raw bytes to `apply_middleware=True` callers and returns 403 with `{"code": "recording_unprocessed", "detail": "..."}` rather than leaking the unrewritten header.

## Soft delete and purge

`DELETE /{hash}` sets `Recording.deleted_at` to `now()`. The row is hidden from the default list queryset but remains queryable via `Recording.objects.filter(deleted_at__isnull=False)`. Restoration is just clearing `deleted_at` — the cleanest path is via the activity rollback API, since the soft-delete is logged as a MODIFY change. See [activity/README.md](../activity/README.md#worked-example--restore-a-recording-from-trash).

`purge_deleted_recordings` ([tasks.py](tasks.py)) runs every 3 hours (scheduled in [settings/common.py](../epicurrents/settings/common.py)) and:

1. Hard-deletes soft-deleted READY recordings whose `deleted_at` is older than `RECORDINGS_TRASH_RETENTION_DAYS` (default 30). File is removed from disk first; if disk removal fails the row is left for the next run.
2. Reaps orphaned `PENDING` / `PROCESSING` rows created before the same cutoff. These are leftovers from worker crashes — the file is best-effort removed, the row is deleted.

Each hard-delete triggers the normal `pre_delete` signal so an audit `ObjectChangeLog` entry is written with the row's final state.

## Bulk import via `import_recordings`

Operator-friendly bulk ingest from a local directory tree.

```bash
docker compose run --rm web python manage.py import_recordings \
    <source_path> --username <owner> \
    [--pipeline import] \
    [--structure recursive|recursive-flat|flat] \
    [--preserve-annotations] \
    [--reprocess] \
    [--resume | --discard]
```

Arguments:

| Flag | Default | Notes |
|---|---|---|
| `source_path` | — | Directory containing EDF/BDF (and convertible `.e`) files. |
| `--username` | — | Required. Owner of all created `Recording` rows. |
| `--pipeline` | `import` | Pipeline label. Must exist in `RECORDING_PIPELINES` or be a built-in. |
| `--structure` | `recursive` | `recursive` mirrors subdirs as Collections; `recursive-flat` scans subdirs without creating Collections; `flat` scans only the top level. |
| `--preserve-annotations` | off | Don't strip annotation text from stored EDFs. |
| `--reprocess` | off | When resuming, re-process files already marked `done` (default: skipped). |
| `--resume` / `--discard` | — | Required when an `in_progress` job already exists. Mutually exclusive. |

The command shares its EDF processing path with the upload Celery task — `_save_edf_results`, `_save_sidecar_events`, `_annotation_hash`, `_determine_modality` in [tasks.py](tasks.py) are private helpers but are imported by this command. Renaming or removing any of them requires updating the command in the same commit.

Mount the source directory into the container at `RECORDINGS_IMPORT_PATH` (default `recordings_import/`) so the command can read it.

The import command honours `RECORDINGS_PRESERVE_MODE = "all"` — each imported source is also written to the originals volume so a single canonical archive covers both web uploads and imports. Mode `"failed"` is moot for imports: EDF parse errors re-raise and the row is never persisted, so there is no FAILED status to trigger the failed-mode preservation path.

## Preservation tiers

The platform optionally copies the as-uploaded file to a **host-controlled originals volume** so the operator has a regulatory backstop independent of the processed-files store. Three tiers (`RECORDINGS_PRESERVE_MODE`):

| Mode | Behaviour |
|---|---|
| `"none"` *(default)* | The platform never writes to the originals volume. Failed uploads stop at the FAILED row + the processed-files store; the originals volume can be unset. |
| `"failed"` | On ingest failure, the worker copies the current permanent file to the originals volume after the failure handler runs. Successful uploads are not preserved beyond the processed-files store. |
| `"all"` | Every upload is copied to the originals volume **before** any processing runs — the only correct time, since processing rewrites the file in place for header anonymisation. |

### The regulatory threshold

Crossing from `"none"` to `"failed"` or `"all"` is the hard line. The originals volume contains files with the original PHI-bearing headers and filenames; once that infrastructure is in place, the regulatory and operational requirements (storage encryption, access auditing, retention policy, secure disposal) attach to the volume itself, not just to the processed store. Stepping from `"failed"` to `"all"` is then mostly a storage-cost decision — the same compliance posture covers both.

### Volume layout and manifest format

Layout, per recording::

    <RECORDINGS_ORIGINALS_PATH>/
        <stored_name_prefix>/
            <sanitized-original-filename>
            manifest.json

`stored_name_prefix` is the 32-hex-char prefix of `Recording.stored_name` — unique per upload and stable across the recording's lifetime (independent of `content_hash`, which the platform rewrites during anonymisation).

`manifest.json` carries:

| Field | Type | Notes |
|---|---|---|
| `recording_pk` | int | Internal PK of the `Recording` row. |
| `stored_name` | string | Public stored name (`<32-hex>.<ext>`). |
| `original_name` | string | Filename as uploaded. May contain PHI — the file is on the operator's volume by design. |
| `file_hash` | string | SHA-256 of the original bytes. |
| `file_size` | int | Size in bytes. |
| `author_id` | int | User PK of the uploader. |
| `uploaded_at` | string | ISO 8601 datetime (`Recording.created_at`). |
| `preservation_reason` | string | `"all"` or `"failed"`. Records which tier produced the copy when multiple tiers could apply. |

### Strict write-only — the platform never reads back

`recordings/preservation.py` only writes. No code path in the platform — including the `validate_originals` command — opens the preserved file contents. The command reads filesystem metadata (`stat()`, directory listings, `manifest.json` parsing) and that is all. This isolation is deliberate: the originals volume is the operator's responsibility, and exposing a read API on it would re-introduce every PHI-leak risk the preservation tier exists to bound.

Recovery of original bytes happens out-of-band — the operator mounts the volume, finds the recording by stored-name prefix, and uses whatever tooling fits their workflow.

### `validate_originals` management command

Cross-checks the volume against the DB, read-only and metadata-only:

```bash
docker compose run --rm web python manage.py validate_originals          # human report
docker compose run --rm web python manage.py validate_originals --json   # machine-readable
docker compose run --rm web python manage.py validate_originals --no-size-check
docker compose run --rm web python manage.py validate_originals --expect-tier all
```

Reports four categories:

| Category | Meaning |
|---|---|
| **Orphans** | Directory on disk has no matching `Recording` row. Most commonly produced when a recording is purged from the platform; the operator decides whether to keep, archive, or remove. |
| **Missing** | `Recording` row that the assumed tier should preserve, but no directory exists. Useful for spotting recordings processed before the tier was switched on. |
| **Size mismatches** | On-disk file size ≠ `manifest.json` `file_size`. Indicates external mutation of the volume (the platform never rewrites preserved files). |
| **Malformed** | Stray files, missing/unreadable `manifest.json`, or directory-name vs. manifest mismatch. |

The "missing" set depends on the assumed tier:

- `"none"`: empty (nothing is expected).
- `"failed"`: every FAILED active recording.
- `"all"`: every active recording except those still in `PENDING` / `PROCESSING` (the worker has not yet written).

Use `--expect-tier` to override the current mode when auditing the volume after a tier switch. The command exits non-zero when any category has at least one row so cron / CI pickups can branch.

### Operator runbook for tier migration

| From → To | What to do |
|---|---|
| `none` → `failed` or `all` | Mount the originals volume, set `RECORDINGS_ORIGINALS_PATH`, restart the worker and web containers. The startup check fails loudly if the mode is non-default and the path is unset. New uploads start preserving immediately; existing recordings are not backfilled. |
| `failed` → `all` | Just flip `RECORDINGS_PRESERVE_MODE`. Past FAILED rows already preserved stay where they are; future *successful* uploads begin preserving as well. |
| `all` → `failed` | Flip the mode. Existing preserved successful uploads stay on disk (the platform never deletes). Run `validate_originals --expect-tier=failed` to flag the now-redundant successful-upload directories for operator-driven cleanup. |
| `failed` or `all` → `none` | Flip the mode and remove `RECORDINGS_ORIGINALS_PATH` from the environment. The volume contents are *not* deleted by the platform — operator does the cleanup, informed by `validate_originals --expect-tier=none` (which reports everything as orphans). |

The startup check in `recordings/apps.py` calls `recordings.preservation.validate_settings`; a non-default mode without a configured path raises `ImproperlyConfigured` at boot.

### Source bytes preserved under `"failed"` mode

Under mode `"failed"`, the bytes written to the originals volume are the as-uploaded source — not a converted derivative — even when a format converter runs between upload and failure. Two failure scenarios are covered:

- Converter succeeded, EDF processing on the converted file failed. The [pre_convert hook](#conversion-hooks) stashes a copy of the source bytes before the converter overwrites them; the end-of-task preservation finalises from the stash.
- The converter itself raised. The [convert_failed hook](#conversion-hooks) preserves source bytes inside the converter `except` block, before the outer task cleanup deletes them.

Mode `"all"` is unaffected — the worker writes the staging file before any conversion runs, so the originals volume already holds the source bytes regardless of what the converter does.

Native EDF/BDF uploads have no converter to interact with; the failure path preserves the file directly as before.

## De-identification

What happens at ingest, in order:

1. **Filename randomisation** — `stored_name` is a 32-hex-char token used as both the on-disk filename and the public URL hash. `original_name` is preserved on the row as an author-private reference (visible only to the author and superusers) and never used as a filesystem path.
2. **Display-name decoupling** — `display_name` is the grantee-visible label, distinct from `original_name`. Defaults to the `stored_name` hash prefix when the author does not supply a custom name. The `Content-Disposition` header on every download uses `display_name + file_extension`, so a filename that encodes subject identity never reaches anyone but the author.
3. **File timestamp normalisation** — `os.utime(path, (0, 0))` after the file lands in `RECORDINGS_UPLOAD_PATH`. The filesystem no longer reveals when the upload happened. Applied again after conversion.
4. **EDF header anonymisation** — `process_edf_file` writes the stored file with `_build_clean_header`: patient and recording identification become `X X X X`, the start date becomes `01.01.85` and the start time `00.00.00`. The header on disk therefore carries no identification. When an authorised reader (not the author / superuser) requests the file with `apply_middleware=True`, the serve pipeline additionally runs `AnonymizeEDFHeader` over the bytes on the wire — defence in depth for a file that reached storage by some other path.
5. **Annotation text stripping** — by default (`strip_annotation_text=True`) the stored EDF has its annotation TALs replaced with the minimum timekeeping records. The original text is stored in the database as an "Original annotations" `Annotation` so the author can still see what was there.
6. **FAILED-state hiding** — failed uploads (which may carry a PHI-bearing filename and an unrewritten header) are entirely invisible to grantees. See [FAILED-hidden rule](#failed-hidden-rule).
7. **PK suppression in responses** — recording responses use the `stored_name` hash prefix as the public identifier, never `id` or `author_id`. See the de-identification rule in [epicurrents/README.md](../epicurrents/README.md#cross-app-rules-this-app-enforces).
8. **Recording date stripped** — `RecordingMeta.recording_date` is always nulled at ingest, and the EDF startdate field in the stored header is the fixed `01.01.85` written by step 4.

## Settings consumed

| Variable | Default | Notes |
|---|---|---|
| `RECORDINGS_UPLOAD_PATH` | `recordings_uploads/` | Permanent storage for processed files. |
| `RECORDINGS_STAGING_PATH` | `recordings_staging/` | Temporary area for uploads in flight. Files here only exist between request commit and the Celery task completing. |
| `RECORDINGS_IMPORT_PATH` | `recordings_import/` | Source directory for bulk import. |
| `RECORDINGS_TRASH_RETENTION_DAYS` | `30` | Days before soft-deleted READY recordings are hard-purged. Also used for orphaned PENDING/PROCESSING rows. |
| `RECORDINGS_MAX_UPLOAD_SIZE` | `2 * 1024 * 1024 * 1024` (2 GiB) | Hard cap, enforced by the upload endpoint as it streams chunks to disk. Nothing in Django enforces it: Ninja streams a file part straight to disk, and `DATA_UPLOAD_MAX_MEMORY_SIZE` bounds form fields rather than files. |
| `RECORDING_PIPELINES` | `{}` | Override / extend named ingest pipelines. |
| `RECORDING_CONVERTERS` | `{}` | Override / extend the converter registry. `None` value disables a built-in. |
| `RECORDINGS_PRESERVE_MODE` | `"none"` | `"none"` \| `"failed"` \| `"all"`. See [Preservation tiers](#preservation-tiers). |
| `RECORDINGS_ORIGINALS_PATH` | unset | Mount point for the host-controlled originals volume. Required when mode is `"failed"` or `"all"`; the startup check fails loudly otherwise. Strict write-only from the platform. |
| `RECORDINGS_DISCARD_ORIGINAL_NAME` | `False` | Replace the uploaded filename with an upload timestamp before the row is written. See [Ingest privacy overrides](#ingest-privacy-overrides). |
| `RECORDINGS_DISCARD_EMBEDDED_ANNOTATIONS` | `False` | Write no annotation row for events that arrived inside the file. Gap records are unaffected. |
| `RECORDINGS_DISCARD_SOURCE_CHANNEL_METADATA` | `False` | Store no `SignalInfo.source_*` values — the pre-cleaning channel originals. The cleaned values are unaffected. |
| `RECORDINGS_ALLOW_PRESERVE_ANNOTATIONS` | `True` | When false, a caller may not ask for annotation text to be kept in the stored file. |

## Ingest privacy overrides

Four settings for a deployment whose position is that no patient personal data reaches the platform at all. All four default to the permissive value, because each discards or forbids something other deployments legitimately need — an author diagnosing a de-identification problem needs the `source_*` columns, and a research pipeline may need the annotations the file arrived with. A project whose deployment takes that position turns on all four in its `settings.py` and records why.

They stop the platform *retaining* what an anonymising client was supposed to have removed. None of them anonymises anything: that happens before upload, and these are what make the claim hold when a recording arrives some other way.

**`RECORDINGS_DISCARD_ORIGINAL_NAME`** replaces the filename with `upload-<UTC timestamp><ext>`. Clinical exports are routinely named after the patient, which makes the filename a direct identifier arriving through a field nobody classifies as one. The value is resolved by `stored_original_name` in [recordings/models.py](models.py), and every route that creates a `Recording` must obtain it there — the upload endpoint and `import_recordings` alike. A source scan in the tests fails the build if a new route assigns the field any other way — a gate that covers one route of several is a default wearing a prohibition's name.

**`RECORDINGS_DISCARD_EMBEDDED_ANNOTATIONS`** suppresses both the `"Original annotations"` row written from EDF TALs and the `"Source events"` row written from a converted Nicolet `.e` file's sidecar. Vendor event vocabularies identify the acquisition software and through it the laboratory; free-text events carry whatever the file carried. `Interruption` rows are deliberately kept — a gap is geometry rather than annotation, it carries no text, and both the viewer and the compute layer place events against it.

**`RECORDINGS_DISCARD_SOURCE_CHANNEL_METADATA`** drops the `source_label` / `source_transducer_type` / `source_prefiltering` / `source_index` capture. Applied at ingest and again on metadata refresh, which rebuilds those rows from the previous ones and would otherwise reacquire what had been dropped.

**`RECORDINGS_ALLOW_PRESERVE_ANNOTATIONS=False`** turns the strip-annotation-text default into a prohibition. The upload endpoint answers 400 so a client wired to send the flag finds out, and `process_recording` ignores the flag independently so the refusal also covers `import_recordings` and a project invoking the task directly.

Coverage is in [recordings/tests/test_ingest_privacy_overrides.py](tests/test_ingest_privacy_overrides.py), which asserts the enabled behaviour, the permissive default, and — for the filename and the annotation prohibition — every route rather than the one that happened to have a test.

## Project plugin extension points

| Hook | How |
|---|---|
| Custom ingest pipeline | Add a label to `RECORDING_PIPELINES` in your project's `settings.py`. Pass the label to `import_recordings --pipeline <label>`, or set `RECORDING_PIPELINES["web"]` to override the upload Celery task's pipeline. |
| Custom converter | Add an entry to `RECORDING_CONVERTERS`. Implement the `(input_path, output_dir) -> Path | (Path, dict | None)` contract. |
| EDF middleware in the serve pipeline | Subclass `EDFHeaderMiddleware` or `EDFSignalMiddleware` from [federation/middleware.py](../federation/middleware.py). The serve pipeline is currently hardcoded in `_build_serve_pipeline`; project-level injection of additional middleware is on the roadmap. |
| Reverse relation from project model to `Recording` | Standard FK / `OneToOneField` from your project model. See [projects/example/models.py](../projects/example/models.py) for the worked pattern. |
| Re-emit an EDF/BDF header after a structural transform | `build_header(header, signal_infos)` from [recordings/processors/edf.py](processors/edf.py) — see [Building headers](#building-headers). |
| Build recording bytes for a test | `make_edf_bytes()` from [recordings/testing.py](testing.py) — see [Test helpers](#test-helpers). |

### Building headers

`build_header(header: EdfHeader, signal_infos: list[EdfSignalInfo]) -> bytes` assembles header bytes for a channel set that no longer matches the file on disk. Any transform that changes the channel set has to re-emit the header, and both the federation middleware and the FUSE filesystem do; a project doing the same needs the same function rather than a copy of it.

It is a serializer and makes no claim about what it serializes: identification fields are copied verbatim from `header`. A caller that needs them removed combines it with an anonymisation step. The de-identifying counterpart, `_build_clean_header`, stays private on purpose — its blanking values are the platform's PHI contract rather than a parameter, and [recordings/processors/edf.py](processors/edf.py) is load-bearing for exactly that reason.

### Test helpers

[recordings/testing.py](testing.py) holds fixture builders that ship with the app rather than living in a test file, so a project plugin in its own repository can import them. `make_edf_bytes(n_channels=1, n_records=1)` returns a minimal valid plain-EDF file with zeroed samples — right for a test asserting on structure, wrong for one asserting on signal content. Its identification fields already carry the anonymised values, so a test exercising PHI removal must build its own header.

Import from here, never from a `recordings.tests.*` module. Test modules carry no stability promise, are free to be reorganised at any time, and importing one executes it.

The module is deliberately stdlib-only — no pytest, no Django — so it is importable from a plain script and from the runtime image, where the test toolchain is not installed. Roughly a dozen near-identical EDF builders remain scattered across the platform's own test files; moving them here as they are needed is the intended direction.

## Tests

```bash
pytest recordings/tests/
```

The pipeline / converter test surface lives in `recordings/tests/test_pipelines.py`. Upload and serve endpoint tests are in `recordings/tests/test_api.py`. Format-specific EDF processing has its own suite in `recordings/tests/test_edf_processor.py`. Shared fixture builders live in [recordings/testing.py](testing.py), not in the test modules.

## Gotchas

- **`file_hash` vs `content_hash` vs the public `hash`.** `file_hash` is the SHA-256 of the raw file bytes, stable across uploads of the same file (no dedup query uses it yet, so it carries no index). `content_hash` mixes `file_hash` with a serialisation of the row, so two uploads of the same file by different users differ. Neither is the public identifier: the URL `hash` is the 32-hex-char `stored_name` prefix, served by a pattern-ops index on PostgreSQL.
- **Re-uploads produce distinct annotation hashes.** `_annotation_hash(recording.pk, suffix)` is keyed on the recording PK rather than the file hash, so uploading the same file twice produces two separate sets of `Interruption` and "Original annotations" rows.
- **FAILED-state visibility is asymmetric.** Author and superusers see FAILED recordings everywhere — listings, detail, status, downloads (they receive raw bytes since author / superuser bypass the middleware) — so they can act on the failure. Every grantee surface (other readers, share-token holders, federated peers, library item listings) filters FAILED out at the queryset / response level and returns 404 on direct hash lookups. The `apply_middleware=True` bypass that previously leaked unrewritten EDF headers when `RecordingMeta` was missing now returns 403 with `{"code": "recording_unprocessed", ...}` as a defense-in-depth check. See [FAILED-hidden rule](#failed-hidden-rule).
- **Display name vs. original filename.** `original_name` is the filename as uploaded and is **not user-mutable** — PATCH does not accept it. The ingest pipeline rewrites it as a side effect of format conversion (the Nicolet `.e` converter, for example, replaces `.e` with `.edf` so the stored filename matches the format). `display_name` is the grantee-visible label, editable via PATCH. Grantee-facing responses always return `display_name` and `null` for `original_name`. The `Content-Disposition` filename on every download uses `display_name + file_extension`. Authors who want to use the original filename as the display name must opt in explicitly (upload-time `display_name` parameter or later PATCH) with a PHI acknowledgement on the UI side.
- **Pipeline mutation is local to one task.** `process_recording(..., preserve_annotations=True)` sets `pipeline.header.strip_annotation_text = False` on the *resolved* pipeline object for that task. The same is true in `import_recordings`. Don't cache a `RecordingPipeline` and reuse it across tasks if the per-task flag matters — call `get_pipeline()` afresh each time.
- **Conversion rewrites `original_name`.** After a `.e` → EDF conversion, the user-facing filename is updated from `recording.e` to `recording.edf`. This is intentional — the viewer chooses its reader by extension and would otherwise look for a `.e` reader.
- **Shared private helpers.** `_save_edf_results`, `_save_sidecar_events`, `_annotation_hash`, and `_determine_modality` live in [tasks.py](tasks.py) but are imported by [management/commands/import_recordings.py](management/commands/import_recordings.py). Treat them as a shared utility surface — renames or signature changes must update both call sites.
- **Upload atomicity is non-negotiable.** The endpoint creates a `Recording` row, one `AccessRight` per grant, and dispatches the Celery task. All of this is inside `transaction.atomic()` with the dispatch wrapped in `transaction.on_commit()`. Without this, the Celery worker can see and act on a `Recording` row whose `AccessRight` rows haven't been committed yet — leading to "you do not have permission to view this object" right after upload.
