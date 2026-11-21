# dicom

DICOM file management and OHIF viewer integration for the Epicurrents platform. Lets users upload DICOM studies (CT, MRI, PET, etc.), parses and indexes them synchronously at upload, and opens them in the [OHIF Viewer](https://ohif.org) — a full-featured, open-source medical image viewer.

This plugin is the successor to the PHP `OhifController` that ran inside Nextcloud. The architecture is similar — parse tags, generate DICOMweb JSON, serve raw files — but the implementation is native to the Epicurrents platform (Django, pydicom, the platform's permission system).

## Quick start

```bash
# 1. Enable the plugin (fetches the OHIF submodule, migrates, updates
#    EPICURRENTS_PLUGINS + VITE_PLUGINS, and rebuilds the frontend)
scripts/enable_plugin.sh dicom

# 2. Build the OHIF viewer dist (requires Node ≥ 24; pnpm comes from corepack)
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

The study list is at `/dicom/studies` in the main Vue SPA. Opening a study launches OHIF at `/plugin/dicom/ohif/` — see [Viewer URL pattern](#viewer-url-pattern).

## OHIF viewer submodule

The OHIF Viewers source lives at `plugins/dicom/ohif-viewer/` as a git submodule pointing to `https://github.com/OHIF/Viewers.git`, pinned to the `v3.13.15` release tag. It is registered with `update = none`, so a default `git submodule update --init --recursive` skips it — fetch it explicitly with `git submodule update --init --checkout plugins/dicom/ohif-viewer` ([enable_plugin.sh](../../scripts/enable_plugin.sh) does this for you).

The built output goes to `plugins/dicom/ohif-dist/` (git-ignored) and is served by [views.py](views.py). Rebuild whenever the submodule moves:

```bash
./scripts/build_ohif.sh
```

The script builds the upstream viewer unmodified with `PUBLIC_URL=/plugin/dicom/ohif/`, then puts [ohif-app-config.js](ohif-app-config.js) in place of the configuration the build ships. That file is the only place the platform configures OHIF: the router base, and a single `dicomjson` data source. OHIF 3.13 builds with pnpm (the version its [package.json](ohif-viewer/package.json) names, which the script takes from corepack when pnpm is not installed) and Node ≥ 24, and the build wants about 8 GB of memory. Source maps are deleted from the dist; the `ort/` directory, about 97 MB of the 126 MB, is the ONNX runtime for OHIF's AI segmentation tools and loads only when one is used.

The script also replaces OHIF's service worker with nothing. Its registration script begins by unregistering every service worker on the origin, which on this platform is the SPA's own worker — the one push notifications are delivered through — so opening a study would silently stop them. The worker it would register imports Workbox from a Google CDN, which the platform's CSP refuses anyway, and offline caching of the viewer is not something a deployment needs. OHIF skips the registration on `localhost` and `127.*`, so the problem never shows on a development machine.

To move to a new OHIF release, check out its tag in the submodule, rebuild, and open each kind of study the deployment serves, compressed ones included: a release that changes the `dicomjson` data source, the mode routes or the codec loader breaks the viewer without failing the build. Commit the moved submodule pointer with the README's pinned tag.

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
| `rescale_slope`, `rescale_intercept` | Modality LUT, stored as strings (DICOM DS VR). Sent to OHIF so CT values display in HU. |
| `number_of_frames` | Non-null for multi-frame objects; each frame gets its own URL in the OHIF JSON. |

## Ingest

Upload and bulk import share the parse/persist logic in [ingest.py](ingest.py). Each file is header-parsed with pydicom (`stop_before_pixels=True` — pixels are never read), its study/series/instance rows are created for the uploading author, and the file lands under `DICOM_UPLOAD_PATH` with a UUID-derived name. There is no per-file Celery task: parsing a header costs microseconds once the upload itself has been received, and the synchronous flow means the study hashes in the upload response are immediately valid.

The upload endpoint stages each file (hashing as it streams), parses it, persists the whole batch's rows in one transaction, then moves accepted files into final storage and flips their instances to `ready`. Unparseable files, files missing required UIDs, oversized files, and duplicates of already-ready instances are rejected per file — reported in the response's `files` list without aborting the rest of the batch.

## API endpoints

All endpoints are mounted at `/plugin/dicom/api/v1/`.

| Method | Path | Description |
|---|---|---|
| `POST` | `/dicom/upload/` | Upload one or more DICOM files. Returns real study hashes plus a per-file accept/reject report. Optional `attached_to_type` + `attached_to_id` query params attach the resulting study to a recording. |
| `GET` | `/dicom/studies/` | List active studies owned by or shared with the caller (includes `is_author` and `can_share`). |
| `GET` | `/dicom/studies/{hash}/` | Study detail with per-series instance counts. |
| `GET` | `/dicom/studies/{hash}/ohif-json/` | DICOMweb JSON consumed by OHIF. |
| `DELETE` | `/dicom/studies/{hash}/` | Trash a study (soft delete; author, superuser, or can_write grant). |
| `GET` | `/dicom/studies/{hash}/access/` | List the study's access rights other than the author's own — see [Access control](#access-control). |
| `POST` | `/dicom/studies/{hash}/access/` | Grant a user an access right (`access_target_id`, `can_read` / `can_write` / `can_share`, `expires_at`). |
| `DELETE` | `/dicom/studies/{hash}/access/{right_id}/` | Revoke one access right. |
| `GET` | `/dicom/wado/` | WADO-URI: stream one DICOM instance file by `objectUID` (+ optional `studyUID` / `seriesUID` narrowing). |
| `GET` | `/dicomweb/studies` | QIDO-RS: search the studies the caller may list — see [DICOMweb](#dicomweb). |
| `GET` | `/dicomweb/studies/{study_uid}/series` | QIDO-RS: the series of one study. |
| `GET` | `/dicomweb/studies/{study_uid}/metadata` | WADO-RS: instance metadata of a whole study. |
| `GET` | `/dicomweb/studies/{study_uid}/series/{series_uid}/metadata` | WADO-RS: instance metadata of one series. |

Every endpoint annotates its `Activity` row via `log_activity` with a `dicom.<resource>.<action>` verb, and the purge task and the import command open a `with_system_activity` scope; the full set is in the [verb registry](#verb-registry).

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

Values are numbers where DICOM's are numeric strings (DS, IS), because OHIF does arithmetic on this metadata, and a tag the file did not carry is left out rather than sent empty — OHIF reads an empty `WindowWidth` as a zero-width window and renders every image as a black-and-white threshold. Instances are ordered by numeric `InstanceNumber`. `RescaleSlope` / `RescaleIntercept` are sent so CT values reach OHIF in HU: OHIF takes the modality LUT from this metadata, not from the file.

Multi-frame instances are expanded: each frame gets its own entry with a `&frame=N` suffix on the URL, the 1-based parameter both the `dicomjson` source and the WADO-URI image loader read. The loader strips it before fetching, so every frame is served from one request for the file.

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
| `DICOM_UPLOAD_PATH` | `<MEDIA_UPLOAD_PATH>/../dicom` (`/data/media/dicom` in the compose stack) | Directory for indexed DICOM files. On the media volume because the plugin brings no volume of its own, and that one is writable in web and celery and backed up. |
| `DICOM_STAGING_PATH` | `<MEDIA_UPLOAD_PATH>/../dicom-staging` | Temporary landing zone during upload, on the same volume so the move into storage is a rename. |
| `DICOM_OHIF_DIST_PATH` | `plugins/dicom/ohif-dist` | Filesystem path to the built OHIF dist served by Django. |
| `DICOM_OHIF_DEMO_DATA` | `False` | Development only: serve the [demo data overlay](#demo-data-for-development). Ignored unless `DEBUG` is on. |
| `DICOM_MAX_UPLOAD_FILES` | `500` | Maximum files per upload request. |
| `DICOM_MAX_UPLOAD_FILE_SIZE` | `2147483648` (2 GiB) | Maximum size of a single uploaded file, in bytes. |
| `DICOM_TRASH_RETENTION_DAYS` | `30` | Days a trashed study stays recoverable before the purge hard-deletes it. |

## Access control

`DicomStudy` participates in the platform's `AccessRight` system. A study is readable by:

- Its author (who receives a full self-`AccessRight` at study creation, mirroring recordings).
- Superusers.
- Any user with an active `AccessRight` row (`can_read=True`) pointing at the study — created through the access endpoints, which the Share dialog on the study list drives.
- Anyone who can read the study's *attached parent* (typically the recording acquired in the same session), through the `can_read_via_attachment` extension in [permissions.py](permissions.py). Attachment-inherited studies do not appear in the flat study list; they surface through their parent.

Write access (trash, future patch) requires authorship, superuser status, or a `can_write` grant; sharing requires authorship, superuser status, or a `can_share` grant. Series and instances are not independently shareable; access is always inherited from the study. Trashed studies are hidden from every surface — including attachment inheritance — and direct hash lookups return 404 rather than 403, so access denials are indistinguishable from missing studies.

**Granting** follows the dataset rules through `epicurrents.granting`, so a study and a dataset are shared alike: managing access needs authorship, superuser status or a `can_share` grant; a delegated grant confers only what its holder has; a target that already holds a right answers 409; and the author's own row is revocable only by the author or a superuser. The study list carries `can_share` per study, and the Share dialog in [DicomStudiesView.vue](../../frontend/src/plugins/dicom/DicomStudiesView.vue) is the shared `AccessRightsPanel` with only the user mode offered.

Two deliberate limits. Only user targets are accepted: group grants are wanted but not built, and neither the study list nor the [DICOMweb](#dicomweb) search would honour one yet, since both read direct user rows only. Enabling them means accepting `access_target_group_id` here, offering the panel's group mode, and widening `listed_studies` in [dicomweb.py](dicomweb.py) to the caller's groups. And no grant carries `apply_middleware`: no DICOM serving path de-identifies, so every row is written with it off rather than claiming a sanitisation that does not happen, and the dialog says the studies are served as uploaded.

Studies readable only through an attachment stay out of both study lists by design; they are reached through their parent recording.

## Viewer URL pattern

The OHIF viewer is mounted at `/plugin/dicom/ohif/` via [public_urls.py](public_urls.py), and the build is made for that mount (`PUBLIC_URL` in the build script, `routerBasename` in [ohif-app-config.js](ohif-app-config.js)); moving it means rebuilding. OHIF's routes are `/<mode>/<data source>`, and `viewer` is the longitudinal mode. Two data sources are configured, and a study opens through either:

```
/plugin/dicom/ohif/viewer?StudyInstanceUIDs=<uid>                      platform (the default)
/plugin/dicom/ohif/viewer/dicomjson?url=<encoded-ohif-json-url>       dicomjson
```

The bare `/plugin/dicom/ohif/` is OHIF's study list, filled by the `platform` source's [DICOMweb](#dicomweb) search, and opening a row uses the first form. The Vue study list opens a study in a new tab with the second form, which names the exact copy by its hash rather than resolving a UID. `/plugin/dicom/` redirects to the Vue study list. Any sub-path without a file extension falls back to `index.html` for OHIF's client-side routing, and a missing file with one answers 404 rather than the HTML document.

## DICOMweb

The `platform` data source is the plugin's own DICOMweb surface under `/plugin/dicom/api/v1/dicomweb`: QIDO-RS search over studies and over a study's series, and WADO-RS metadata for a study or a series. Everything is answered from the indexed rows in [dicomweb.py](dicomweb.py), never by reading a file, and returned as `application/dicom+json` encoded by pydicom, so each attribute carries its VR and DS / IS values are numbers. Pixels still come from [WADO-URI](#wado-uri), which the configuration names as the source's `wadoUriRoot` with `imageRendering: 'wadouri'`; there is no WADO-RS frame or bulkdata retrieval and no STOW.

**Scope and UID resolution.** The search lists what the Vue study list lists — owned studies and direct read grants, never attachment-inherited ones. A `StudyInstanceUID` is unique per author, so two users who uploaded the same study hold two copies, and DICOMweb addresses a study by UID alone. A UID therefore resolves to the caller's own copy, then to the lowest-keyed listed copy, then to the lowest-keyed copy readable by any route; the search collapses copies to the same choice, so a list row and the study it opens are one copy. A series UID is looked up only inside the resolved copy. An unreadable, trashed or unknown study or series answers 404.

**Matching.** `StudyInstanceUID` takes a list (backslash- or comma-separated); `PatientName`, `PatientID`, `AccessionNumber` and `StudyDescription` match case-insensitively on the literal part of a `*` / `?` pattern; `StudyDate` takes a date or a range open at either end; `ModalitiesInStudy` matches any of the listed modalities. Other attributes are ignored, as QIDO allows, and `includefield` is ignored too (`qidoSupportsIncludeField: false`): the response attribute set is fixed. `limit` and `offset` page the result; an unreadable value is treated as absent.

**Encoding.** An attribute the row does not hold is left out rather than sent empty, and a value that does not parse for its VR — a malformed DS, or `NaN` / `inf`, which parse but encode to JSON no browser accepts — drops only that attribute, so one bad header cannot make a study unloadable.

**Audit.** The search records the names of the filters it was given and never their values, since a filter value is whatever was typed into the study list, a patient's name included.

## Demo data for development

Trying the viewer on CT, MR, PET, SEG, SR, microscopy and the other study types OHIF handles would otherwise need radiology data of one's own. With `DICOM_OHIF_DEMO_DATA=True` and `DEBUG` on, [views.py](views.py) appends [ohif-app-config.demo.js](ohif-app-config.demo.js) to the served `app-config.js`. The overlay adds OHIF's public demo DICOMweb server as a data source named `demo`; its study list is at `/plugin/dicom/ohif/?datasources=demo`, beside the platform's own at the bare mount.

The demo origin is added to `connect-src` on the viewer's responses only while the overlay is served. The browser then talks to that server directly: the requests carry no platform cookie and no platform data, and the studies are public sample data. The `DEBUG` condition is what keeps this off a production deployment whatever its `.env` says; the overlay is not part of the OHIF build, so a package carries it but never serves it.

## Cross-origin isolation

OHIF's WASM-based decoders (JPEG-LS, JPEG 2000, HTJ2K) need `SharedArrayBuffer`, which is only available in cross-origin-isolated browsing contexts. The browser flips on `crossOriginIsolated` only when every response carries the COOP/COEP/CORP triple:

- `Cross-Origin-Opener-Policy: same-origin`
- `Cross-Origin-Embedder-Policy: require-corp`
- `Cross-Origin-Resource-Policy: same-origin`

**Set `ENABLE_CROSS_ORIGIN_ISOLATION=True` in `.env`** when enabling this plugin. The platform-wide [`CrossOriginIsolationMiddleware`](../../epicurrents/middleware.py) then sets the triple on every response — the OHIF HTML, WADO downloads, the Vue SPA, and the API alike.

The plugin does **not** set the env var itself. A deployment may have explicitly disabled cross-origin isolation (e.g. to support an embed scenario that doesn't tolerate `COOP: same-origin`), and a plugin re-enabling it from [settings.py](settings.py) would silently override that decision. The dependency is documented here; the deployment decides.

Reverse proxies (nginx, Caddy, …) in front of Django must pass these headers through or set them themselves. See `nginx.conf.example` for the required snippet.

## Content Security Policy

The image loader's codec worker compiles code with `new Function`, which the platform's `script-src` refuses, so under the production policy every compressed study (JPEG 2000 and the other WASM codecs) fails to decode while uncompressed ones render. [views.py](views.py) therefore sends the platform policy with `'unsafe-eval'` added to `script-src` on every response it serves, in the same enforce or report-only mode as the platform's. The security middleware sets its own policy with `setdefault`, so the SPA and the API keep the strict one.

The relaxation covers the whole OHIF mount rather than the worker alone because the worker is an unnamed numbered chunk whose name changes with every build. `script-src` already carries `'unsafe-inline'`, so the OHIF page loses little it had.

`dicomjson` fetches whatever `url` names: OHIF restricts the origin only when it has authentication of its own, which it does not here. The platform's `connect-src 'self'` is what refuses a crafted link pointing the viewer at another origin's metadata or pixels, so a deployment that widens `connect-src` widens what such a link can load.

## Verb registry

Audit verbs this plugin emits. A new verb belongs here in the same commit as the endpoint that emits it: the [`audit-trail-completeness`](../../.review/agents/audit-trail-completeness.md) review agent matches each endpoint's verb against this table and fails the commit when it finds no row. The heading is matched exactly, so keep it as it is.

A † marks a verb emitted from a Celery task or management command rather than an endpoint. The base actions and the distinctions between them are in [activity/README.md](../../activity/README.md#verb-taxonomy); core-app verbs are in the registry after it.

| Verb | Emitted by |
|---|---|
| `dicom.import` | `handle` † |
| `dicom.instance.download` | `wado_uri` |
| `dicom.purge` | `purge_deleted_dicom_studies` † |
| `dicom.study.access.grant` | `grant_study_access` |
| `dicom.study.access.list` | `list_study_access_rights` |
| `dicom.study.access.revoke` | `revoke_study_access` |
| `dicom.series.list.qido` | `qido_series` |
| `dicom.series.read.metadata` | `wado_series_metadata` |
| `dicom.study.list` | `list_studies` |
| `dicom.study.list.qido` | `qido_studies` |
| `dicom.study.read.ohif_json` | `get_ohif_json` |
| `dicom.study.read` | `get_study` |
| `dicom.study.read.metadata` | `wado_study_metadata` |
| `dicom.study.trash` | `delete_study` |
| `dicom.study.upload` | `upload_dicom` |

## Extension points

- **Custom OHIF config**: Set `OHIF_APP_CONFIG` before running [build_ohif.sh](../../scripts/build_ohif.sh) to install another configuration file in place of [ohif-app-config.js](ohif-app-config.js), adding customizations or data sources. Keep `routerBasename: '/plugin/dicom/ohif'` and an `httpErrorHandler` — OHIF throws on a failed image request when the handler is absent, which hides the original failure.
- **Additional metadata tags**: Add fields to `DicomInstance` and extract them in `ingest.py:extract_instance_fields` — then surface them in the OHIF JSON via `urls.py:_instance_metadata`.
- **De-identification pipeline**: Hook a pydicom de-identification stage into `ingest.py` between parsing and persistence — see the [roadmap](#roadmap).

## Gotchas

- **Non-image DICOM objects**: SR (Structured Reports), RT Plans, and similar non-image SOP classes index correctly (the pixel fields are null) but will not render in the OHIF viewer without the appropriate OHIF extension/mode loaded.
- **WASM codecs**: OHIF requires `SharedArrayBuffer`. Set `ENABLE_CROSS_ORIGIN_ISOLATION=True` in `.env` to enable it deployment-wide — see [Cross-origin isolation](#cross-origin-isolation) for the requirement and the reverse-proxy implications.
- **Multi-frame instances**: Each frame is listed as a separate OHIF instance with a `&frame=N` URL suffix. OHIF handles this correctly for CT/PET multi-frame, but older single-frame-oriented extensions may show duplicates.
- **Duplicate uploads**: Re-uploading an instance that is already `ready` in the same study is rejected per file (DICOM identity semantics — same SOP UID means same object; replacing bytes under an already-served instance would be a silent mutation). Re-uploading a `pending`/`failed` instance replaces it.

## Troubleshooting

### OHIF viewer fails to decode images or shows a blank canvas

OHIF's compressed-pixel decoders (JPEG-LS, JPEG 2000, HTJ2K) run as WASM modules that need `SharedArrayBuffer`. The browser only makes SAB available in a cross-origin-isolated browsing context — which requires the COOP/COEP/CORP triple on every response. If those headers are missing, the decoders fail silently and the viewer renders nothing for compressed studies.

Symptoms:
- Browser console: `SharedArrayBuffer is not defined`, or `crossOriginIsolated` is `false`.
- Studies stored as uncompressed render fine; compressed studies show blank canvas or a decode error.

Fix:
1. Set `ENABLE_CROSS_ORIGIN_ISOLATION=True` in `.env`, restart the stack.
2. Verify the response headers reach the browser — `curl -I https://your-host/plugin/dicom/ohif/` should show all three headers (`Cross-Origin-Opener-Policy`, `Cross-Origin-Embedder-Policy`, `Cross-Origin-Resource-Policy`).
3. If you're behind a reverse proxy (nginx, Caddy, Traefik, …) the proxy must pass through or set the headers itself. See `nginx.conf.example`.

The platform-wide middleware handles step 1's actual header-setting. See [Cross-origin isolation](#cross-origin-isolation) for the full requirement.

### Uncompressed studies render but compressed ones do not

Browser console: `EvalError: Evaluating a string as JavaScript violates the following Content Security Policy directive`, from the worker `dicomImageLoader`. The OHIF responses are missing the relaxed `script-src` described under [Content Security Policy](#content-security-policy). Check `curl -I https://your-host/plugin/dicom/ohif/` for `'unsafe-eval'`; a reverse proxy that sets its own `Content-Security-Policy` replaces the one Django sent.

### Every image renders as a black-and-white threshold, or CT values are off by 1024

The OHIF JSON is missing or misstating the windowing or rescale tags — see [OHIF JSON format](#ohif-json-format). Studies indexed before the rescale fields existed carry empty values for them; re-import them.

## Roadmap

### 🔴 De-identify DICOM on ingest

There is still no de-identification step in the ingest pipeline: files are stored unchanged, `wado_uri` streams the raw `.dcm` (every identifying header tag) to any caller with read access, and the study endpoints expose the parsed `PatientName` / `PatientID` / `PatientBirthDate` / `AccessionNumber` to every grantee. Mitigations in place: the parsed demographic fields are masked out of audit payloads (`register_masked_fields` in [apps.py](apps.py)), the retention purge bounds how long the data persists, and sharing is an explicit per-user grant. The fix is a pydicom de-identification stage (PS3.15 patient tags stripped or replaced) hooked into [ingest.py](ingest.py) before persistence — the tool comparison is in [dicom-deidentification-tools.md](../../docs/engineering-notes/dicom-deidentification-tools.md), and the decided design and phases are in [dicom-deidentification-plan.md](../../docs/engineering-notes/dicom-deidentification-plan.md).

Resolved earlier roadmap items, for the record: session-CSRF chokepoint routing (`_require_auth` now calls `enforce_session_csrf`; contract test in [tests/test_csrf.py](tests/test_csrf.py)), `log_activity` verb coverage on every endpoint ([tests/test_api.py](tests/test_api.py) `TestDicomAuditTrail`), scheduled retention purge ([Purge](#purge)), and file cleanup on account erasure (the `pre_delete` receiver in [signals.py](signals.py)).
