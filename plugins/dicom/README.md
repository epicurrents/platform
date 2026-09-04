# dicom

DICOM file management and OHIF viewer integration for the Epicurrents platform. Lets users upload DICOM studies (CT, MRI, PET, etc.), parses and indexes them synchronously at upload, and opens them in the [OHIF Viewer](https://ohif.org) — a full-featured, open-source medical image viewer.

This plugin is the successor to the PHP `OhifController` that ran inside Nextcloud. The architecture is similar — parse tags, generate DICOMweb JSON, serve raw files — but the implementation is native to the Epicurrents platform (Django, pydicom, the platform's permission system).

## Quick start

```bash
# 1. Enable the plugin (fetches the OHIF submodule, migrates, updates
#    EPICURRENTS_PLUGINS + VITE_PLUGINS, and rebuilds the frontend)
scripts/enable_plugin.sh dicom

# 2. Build the OHIF viewer dist (requires Node ≥ 18 + yarn)
./scripts/build_ohif.sh

# 3. Start the stack
docker compose up
```

Manual equivalent of step 1, if you prefer to wire it by hand:

```bash
git submodule update --init --checkout plugins/dicom/ohif-viewer
docker compose run --rm --no-deps -e EPICURRENTS_PLUGINS=dicom web python manage.py migrate
echo "EPICURRENTS_PLUGINS=dicom" >> .env          # comma-append if others are enabled
echo "VITE_PLUGINS=dicom" >> frontend/.env         # keep the two lists in sync
```

The study list is at `/dicom/studies` in the main Vue SPA. Opening a study launches OHIF at `/plugin/dicom/viewer/`.

## OHIF viewer submodule

The OHIF Viewers source lives at `plugins/dicom/ohif-viewer/` as a git submodule pointing to `https://github.com/OHIF/Viewers.git`. It is registered with `update = none`, so a default `git submodule update --init --recursive` skips it — fetch it explicitly with `git submodule update --init --checkout plugins/dicom/ohif-viewer` (`scripts/enable_plugin.sh` does this for you).

The built output goes to `plugins/dicom/ohif-dist/` and is served by [views.py](views.py). Rebuild whenever the submodule is updated:

```bash
./scripts/build_ohif.sh
```

The build script writes a minimal `app-config.js` that wires OHIF's `dicomjson` datasource to `/plugin/dicom/api/v1/dicom/studies/{hash}/ohif-json/` and its WADO-URI endpoint to `/plugin/dicom/api/v1/dicom/wado/`. Override either value via environment variables — see the script for details.

## Models

### `DicomStudy`

One row per DICOM study **per author**. The same `StudyInstanceUID` uploaded by two different users produces two independent rows (and two file copies), exactly as two users uploading the same EDF produce two independent `Recording` rows — this is what keeps one user's upload from attaching to, or probing for, another user's study.

| Field | Notes |
|---|---|
| `author` | FK to user; cascades on delete (the `pre_delete` receiver unlinks files on the way). |
| `study_instance_uid` | DICOM (0020,000D); unique per author (`dicomstudy_unique_uid_per_author`). |
| `study_date`, `study_time` | DICOM (0008,0020) and (0008,0030). YYYYMMDD / HHMMSS strings. |
| `study_description` | DICOM (0008,1030). |
| `patient_name`, `patient_id`, `patient_birth_date`, `patient_sex`, `patient_age` | Patient demographics, as parsed from the file. Masked out of audit payloads; ingest de-identification is a [roadmap item](#roadmap). |
| `accession_number` | DICOM (0008,0050). |
| `num_instances` | Cached count of ready instances; refreshed by `ingest.refresh_study_aggregates`. |
| `modalities` | Comma-joined set of modality codes found across all series. |
| `content_hash` | SHA-256 over `SECRET_KEY : author PK : study_instance_uid`. The public URL identifier; never expose the integer PK. |
| `attachment_content_type` + `attachment_object_id` | Optional GenericForeignKey to a parent object (today: a `Recording`). An attached study inherits the parent's read access — see [Access control](#access-control). |
| `deleted_at` | Soft delete; non-null rows are hidden from every read surface and hard-deleted by the purge task after the retention window. |
| `access_rights`, `collection_memberships`, `dataset_memberships`, `tagged_items` | Reverse `GenericRelation`s per the GenericFK target cascade pattern in AGENTS.md. |

### `DicomSeries`

One row per series within a study.

| Field | Notes |
|---|---|
| `study` | FK to `DicomStudy`; cascades on delete. |
| `series_instance_uid` | DICOM (0020,000E). Unique within a study (`dicomseries_unique_uid_per_study`). |
| `modality` | DICOM (0008,0060). E.g. `CT`, `MR`, `PT`, `NM`, `US`, `XA`. |
| `series_number`, `series_date`, `series_description`, `slice_thickness` | Standard series-level tags. |

### `DicomInstance`

One row per SOP instance (one DICOM file). Stores all pixel-geometry metadata needed to build the OHIF JSON without re-reading the file.

| Field | Notes |
|---|---|
| `series` | FK to `DicomSeries`; cascades on delete. |
| `sop_instance_uid` | DICOM (0008,0018); unique per series (`dicominstance_unique_sop_per_series`) — per-author study copies repeat it. |
| `sop_class_uid` | DICOM (0008,0016). Identifies the storage class (CT Image Storage, MR Image Storage, etc.). |
| `stored_name` | Filename under `DICOM_UPLOAD_PATH`. UUID-derived, never from user input. |
| `file_size`, `file_hash` | Size in bytes; SHA-256 of file content (also the `--resume` key for bulk import). |
| `status` | `ready` means row **and** file are in final storage. `pending` exists only inside the upload request between the DB commit and the file move; a stranded `pending` row is reaped by the purge task's orphan branch. `failed` marks a post-commit move failure; re-uploading the instance replaces it in place. |
| `columns`, `rows`, `bits_allocated`, `bits_stored`, `high_bit`, `pixel_representation`, `samples_per_pixel`, `photometric_interpretation` | Pixel encoding attributes. Nullable for non-image objects (SR, RT plans, etc.). |
| `pixel_spacing`, `image_orientation_patient`, `image_position_patient`, `image_type` | Multi-value tags stored as JSON string arrays. |
| `frame_of_reference_uid` | DICOM (0020,0052). Required for MPR / cross-study registration. |
| `window_center`, `window_width` | First value of multi-value windowing tags; stored as strings (DICOM DS VR). |
| `number_of_frames` | Non-null for multi-frame objects; each frame gets its own URL in the OHIF JSON. |

## Ingest

Upload and bulk import share the parse/persist logic in [ingest.py](ingest.py). Each file is header-parsed with pydicom (`stop_before_pixels=True` — pixels are never read), its study/series/instance rows are created for the uploading author, and the file lands under `DICOM_UPLOAD_PATH` with a UUID-derived name. There is no per-file Celery task: parsing a header costs microseconds once the upload itself has been received, and the synchronous flow means the study hashes in the upload response are immediately valid.

The upload endpoint stages each file (hashing as it streams), parses it, persists the whole batch's rows in one transaction, then moves accepted files into final storage and flips their instances to `ready`. Unparseable files, files missing required UIDs, oversized files, and duplicates of already-ready instances are rejected per file — reported in the response's `files` list without aborting the rest of the batch.

## API endpoints

All endpoints are mounted at `/plugin/dicom/api/v1/`.

| Method | Path | Description |
|---|---|---|
| `POST` | `/dicom/upload/` | Upload one or more DICOM files. Returns real study hashes plus a per-file accept/reject report. Optional `attached_to_type` + `attached_to_id` query params attach the resulting study to a recording. |
| `GET` | `/dicom/studies/` | List active studies owned by or shared with the caller (includes `is_author`). |
| `GET` | `/dicom/studies/{hash}/` | Study detail with per-series instance counts. |
| `GET` | `/dicom/studies/{hash}/ohif-json/` | DICOMweb JSON consumed by OHIF. |
| `DELETE` | `/dicom/studies/{hash}/` | Trash a study (soft delete; author, superuser, or can_write grant). |
| `POST` | `/dicom/studies/{hash}/share/` | Grant a username read access (author, superuser, or can_share grant). |
| `DELETE` | `/dicom/studies/{hash}/share/{username}/` | Revoke a user's access rights on the study. |
| `GET` | `/dicom/wado/` | WADO-URI: stream one DICOM instance file by `objectUID` (+ optional `studyUID` / `seriesUID` narrowing). |

Every endpoint annotates its `Activity` row via `log_activity` with a `dicom.<resource>.<action>` verb: `dicom.study.upload`, `dicom.study.list`, `dicom.study.read`, `dicom.study.read.ohif_json`, `dicom.study.trash`, `dicom.study.access.grant`, `dicom.study.access.revoke`, `dicom.instance.download`. The purge task and the import command use `dicom.purge` and `dicom.import` under `with_system_activity`.

### OHIF JSON format

`GET /dicom/studies/{hash}/ohif-json/` returns the DICOMweb JSON structure expected by OHIF's `dicomjson` datasource:

```json
{
  "studies": [
    {
      "StudyInstanceUID": "...",
      "PatientName": "...",
      "series": [
        {
          "SeriesInstanceUID": "...",
          "Modality": "CT",
          "instances": [
            {
              "metadata": { "Columns": 512, "Rows": 512, ... },
              "url": "dicomweb:http://host/plugin/dicom/api/v1/dicom/wado/?objectUID=..."
            }
          ]
        }
      ]
    }
  ]
}
```

Multi-frame instances are expanded: each frame gets its own entry with a `?frameNumber=N` suffix on the URL.

### WADO-URI

`GET /plugin/dicom/api/v1/dicom/wado/?requestType=WADO&objectUID={SOPInstanceUID}` streams the raw DICOM file with `Content-Type: application/dicom`. SOP UIDs are unique per series rather than globally, so the lookup collects every matching ready instance (narrowed by `studyUID` / `seriesUID` when given) and picks the caller's own copy first, then the first copy the caller can read. Missing instances and access denials both return 404, so callers cannot probe for the existence of instances they cannot read.

OHIF's WASM-based decoders (JPEG-LS, JPEG 2000, HTJ2K) need `SharedArrayBuffer`, which requires the COOP/COEP/CORP triple on every response. Those headers are set platform-wide by [`epicurrents.middleware.CrossOriginIsolationMiddleware`](../../epicurrents/middleware.py) — enable it deployment-wide with `ENABLE_CROSS_ORIGIN_ISOLATION=True` in `.env`. See [Cross-origin isolation](#cross-origin-isolation) below for the full requirement.

## Purge

`DELETE /dicom/studies/{hash}/` sets `deleted_at`; the rows and files stay on disk but drop out of every listing and read path. After `DICOM_TRASH_RETENTION_DAYS` (default 30) the `dicom.purge_deleted_dicom_studies` Celery task hard-deletes the study tree — the GDPR Art. 17 erasure half of the soft-delete contract, the imaging analog of `purge_deleted_media`. It is scheduled every 3 hours via the plugin's `CELERY_BEAT_SCHEDULE` contribution.

The task is ⚠️ load-bearing. Its purge filter (`deleted_at__isnull=False, deleted_at__lt=cutoff`) is the line between erasing patient-identifying imaging past the window and reaping live studies. File unlinking happens in the `pre_delete` receiver ([signals.py](signals.py)) inside each per-study delete, so an unlink failure rolls that study's deletion back and the next run retries — rows and files never diverge. The same receiver fires on `erase_user`'s account cascade, so account erasure cleans the stored files too. An orphan branch reaps `pending` instances older than 24 hours (rows stranded between an upload's DB commit and its file move by a crashed request). Per-row deletes fire the audit signals inside the `with_system_activity` scope, so each erasure lands in `ObjectChangeLog` under one `dicom.purge` activity. See `TestPurgeDeletedDicomStudiesContract` in [tests/test_tasks.py](tests/test_tasks.py) before modifying.

There is no trash listing / restore endpoint yet; until the retention window closes an operator can clear `deleted_at` manually.

## Management command

```bash
python manage.py index_dicom /path/to/archive/ --user admin [--dry-run] [--resume]
```

Bulk-imports an existing DICOM directory tree through the same [ingest.py](ingest.py) logic as the upload endpoint: each candidate file is parsed, copied to `DICOM_UPLOAD_PATH`, and persisted as a `ready` instance. `--resume` skips files whose content SHA-256 already appears on one of the owning user's instances, so a crashed run can be restarted without duplicates. The whole run opens a `with_system_activity("dicom.import", interface=command)` scope, so every row lands in the audit trail.

## Settings / env vars

| Variable | Default | Effect |
|---|---|---|
| `DICOM_UPLOAD_PATH` | `<RECORDINGS_UPLOAD_PATH>/../dicom` | Directory for indexed DICOM files. |
| `DICOM_STAGING_PATH` | `<RECORDINGS_UPLOAD_PATH>/../dicom-staging` | Temporary landing zone during upload. |
| `DICOM_OHIF_DIST_PATH` | `plugins/dicom/ohif-dist` | Filesystem path to the built OHIF dist served by Django. |
| `DICOM_MAX_UPLOAD_FILES` | `500` | Maximum files per upload request. |
| `DICOM_MAX_UPLOAD_FILE_SIZE` | `2147483648` (2 GiB) | Maximum size of a single uploaded file, in bytes. |
| `DICOM_TRASH_RETENTION_DAYS` | `30` | Days a trashed study stays recoverable before the purge hard-deletes it. |

## Access control

`DicomStudy` participates in the platform's `AccessRight` system. A study is readable by:

- Its author (who receives a full self-`AccessRight` at study creation, mirroring recordings).
- Superusers.
- Any user with an active `AccessRight` row (`can_read=True`) pointing at the study — created via the share endpoint.
- Anyone who can read the study's *attached parent* (typically the recording acquired in the same session), through the `can_read_via_attachment` extension in [permissions.py](permissions.py). Attachment-inherited studies do not appear in the flat study list; they surface through their parent.

Write access (trash, future patch) requires authorship, superuser status, or a `can_write` grant; sharing requires authorship, superuser status, or a `can_share` grant. Series and instances are not independently shareable; access is always inherited from the study. Trashed studies are hidden from every surface — including attachment inheritance — and direct hash lookups return 404 rather than 403, so access denials are indistinguishable from missing studies.

The frontend share dialog is not built yet; grants are API-only for now. When adding it, follow the recordings share dialog pattern (username input + grant list with revoke buttons) against `POST/DELETE /dicom/studies/{hash}/share/…` and gate the controls on `study.is_author || authStore.isSuperuser`.

## Viewer URL pattern

The OHIF viewer is mounted at `/plugin/dicom/viewer/` via [public_urls.py](public_urls.py). The Vue study list opens it in a new browser tab by constructing a URL like:

```
/plugin/dicom/viewer/?url=<encoded-ohif-json-url>
```

OHIF resolves the `url` query parameter using its `dicomjson` datasource and loads the study automatically.

## Cross-origin isolation

OHIF's WASM-based decoders (JPEG-LS, JPEG 2000, HTJ2K) need `SharedArrayBuffer`, which is only available in cross-origin-isolated browsing contexts. The browser flips on `crossOriginIsolated` only when every response carries the COOP/COEP/CORP triple:

- `Cross-Origin-Opener-Policy: same-origin`
- `Cross-Origin-Embedder-Policy: require-corp`
- `Cross-Origin-Resource-Policy: same-origin`

**Set `ENABLE_CROSS_ORIGIN_ISOLATION=True` in `.env`** when enabling this plugin. The platform-wide [`CrossOriginIsolationMiddleware`](../../epicurrents/middleware.py) then sets the triple on every response — the OHIF HTML, WADO downloads, the Vue SPA, and the API alike.

The plugin does **not** set the env var itself. A deployment may have explicitly disabled cross-origin isolation (e.g. to support an embed scenario that doesn't tolerate `COOP: same-origin`), and a plugin re-enabling it from [settings.py](settings.py) would silently override that decision. The dependency is documented here; the deployment decides.

Reverse proxies (nginx, Caddy, …) in front of Django must pass these headers through or set them themselves. See `nginx.conf.example` for the required snippet.

## Verb registry

Audit verbs this plugin emits. A new verb belongs here in the same commit as the endpoint that emits it: the [`audit-trail-completeness`](../../.review/agents/audit-trail-completeness.md) review agent matches each endpoint's verb against this table and fails the commit when it finds no row. The heading is matched exactly, so keep it as it is.

A † marks a verb emitted from a Celery task or management command rather than an endpoint. The base actions and the distinctions between them are in [activity/README.md](../../activity/README.md#verb-taxonomy); core-app verbs are in the registry after it.

| Verb | Emitted by |
|---|---|
| `dicom.import` | `handle` † |
| `dicom.instance.download` | `wado_uri` |
| `dicom.purge` | `purge_deleted_dicom_studies` † |
| `dicom.study.access.grant` | `share_study` |
| `dicom.study.access.revoke` | `revoke_study_access` |
| `dicom.study.list` | `list_studies` |
| `dicom.study.read.ohif_json` | `get_ohif_json` |
| `dicom.study.read` | `get_study` |
| `dicom.study.trash` | `delete_study` |
| `dicom.study.upload` | `upload_dicom` |

## Extension points

- **Custom OHIF config**: Set `OHIF_APP_CONFIG` before running [build_ohif.sh](../../scripts/build_ohif.sh) to point at a custom `app-config.js` that adds extensions, modes, or additional datasources.
- **Additional metadata tags**: Add fields to `DicomInstance` and extract them in `ingest.py:extract_instance_fields` — then surface them in the OHIF JSON via `urls.py:_instance_metadata`.
- **De-identification pipeline**: Hook a pydicom de-identification stage into `ingest.py` between parsing and persistence — see the [roadmap](#roadmap).

## Gotchas

- **Non-image DICOM objects**: SR (Structured Reports), RT Plans, and similar non-image SOP classes index correctly (the pixel fields are null) but will not render in the OHIF viewer without the appropriate OHIF extension/mode loaded.
- **WASM codecs**: OHIF requires `SharedArrayBuffer`. Set `ENABLE_CROSS_ORIGIN_ISOLATION=True` in `.env` to enable it deployment-wide — see [Cross-origin isolation](#cross-origin-isolation) for the requirement and the reverse-proxy implications.
- **Multi-frame instances**: Each frame is listed as a separate OHIF instance with a `?frameNumber=N` URL suffix. OHIF handles this correctly for CT/PET multi-frame, but older single-frame-oriented extensions may show duplicates.
- **Duplicate uploads**: Re-uploading an instance that is already `ready` in the same study is rejected per file (DICOM identity semantics — same SOP UID means same object; replacing bytes under an already-served instance would be a silent mutation). Re-uploading a `pending`/`failed` instance replaces it.

## Troubleshooting

### OHIF viewer fails to decode images or shows a blank canvas

OHIF's compressed-pixel decoders (JPEG-LS, JPEG 2000, HTJ2K) run as WASM modules that need `SharedArrayBuffer`. The browser only makes SAB available in a cross-origin-isolated browsing context — which requires the COOP/COEP/CORP triple on every response. If those headers are missing, the decoders fail silently and the viewer renders nothing for compressed studies.

Symptoms:
- Browser console: `SharedArrayBuffer is not defined`, or `crossOriginIsolated` is `false`.
- Studies stored as uncompressed render fine; compressed studies show blank canvas or a decode error.

Fix:
1. Set `ENABLE_CROSS_ORIGIN_ISOLATION=True` in `.env`, restart the stack.
2. Verify the response headers reach the browser — `curl -I https://your-host/plugin/dicom/viewer/` should show all three headers (`Cross-Origin-Opener-Policy`, `Cross-Origin-Embedder-Policy`, `Cross-Origin-Resource-Policy`).
3. If you're behind a reverse proxy (nginx, Caddy, Traefik, …) the proxy must pass through or set the headers itself. See `nginx.conf.example`.

The platform-wide middleware handles step 1's actual header-setting. See [Cross-origin isolation](#cross-origin-isolation) for the full requirement.

## Roadmap

### 🔴 De-identify DICOM on ingest

There is still no de-identification step in the ingest pipeline: files are stored unchanged, `wado_uri` streams the raw `.dcm` (every identifying header tag) to any caller with read access, and the study endpoints expose the parsed `PatientName` / `PatientID` / `PatientBirthDate` / `AccessionNumber` to every grantee. Mitigations in place: the parsed demographic fields are masked out of audit payloads (`register_masked_fields` in [apps.py](apps.py)), the retention purge bounds how long the data persists, and sharing is an explicit per-user grant. The fix is a pydicom de-identification stage (PS3.15 patient tags stripped or replaced) hooked into [ingest.py](ingest.py) before persistence — tool selection is tracked in docs/engineering-notes/dicom-deidentification-tools.md, which compares the candidate libraries and recommends one.

Resolved earlier roadmap items, for the record: session-CSRF chokepoint routing (`_require_auth` now calls `enforce_session_csrf`; contract test in [tests/test_csrf.py](tests/test_csrf.py)), `log_activity` verb coverage on every endpoint ([tests/test_api.py](tests/test_api.py) `TestDicomAuditTrail`), scheduled retention purge ([Purge](#purge)), and file cleanup on account erasure (the `pre_delete` receiver in [signals.py](signals.py)).
