# media

Non-signal media files for the epicurrents platform. Sister app to
[recordings](../recordings/README.md): where `Recording` carries biosignal
files with a format-specific processing pipeline, `MediaFile` carries
opaque media that the viewer dispatches to per-format readers.

Supported media types are `document` (markdown, HTML, PDF — rendered by the
viewer's [doc-module](../frontend/viewer/epicurrents/doc-module/)) and `video`
(MP4, served inline with HTTP Range support and synced to the recording cursor
by the viewer's ACC module). `image` and `audio` remain taxonomy seats; the
storage and access plumbing is media-type-agnostic, so they need only the
viewer dispatch table and the per-project allowlist extended when they land.

## Models

### `MediaFile`

| Field | Type | Notes |
|---|---|---|
| `media_type` | `CharField(choices=MediaType)` | `document` or `video` |
| `author` | FK → User | Owner; AccessRight grants `can_read`/`can_write` to anyone else |
| `original_name` | `CharField` | Author-private; never returned to grantees, share-token holders, or federated peers |
| `display_name` | `CharField`, nullable | Grantee-visible label; falls back to the stored-name hash prefix |
| `stored_name` | `CharField`, unique | `<hex hash><.ext>` — the on-disk filename |
| `file_extension` | `CharField` | Lower-cased, dot-prefixed |
| `file_size`, `file_path`, `file_hash` | — | Standard file identity |
| `content_hash` | `CharField` | De-identified identifier used in URLs (the public `hash` parameter) |
| `attachment_content_type` + `attachment_object_id` | GenericForeignKey | Optional parent object (video-EEG, audio per polysomnogram, supplementary doc per case). See [Attachments](#attachments) |
| `time_offset` | `FloatField`, nullable | Position in seconds of the media on the attached parent's timeline — a video/audio start offset, an image pin point. Null for non-time-aligned media. See [Attachments](#attachments) |
| `created_at`, `modified_at`, `deleted_at` | — | Soft-delete via `deleted_at` |

Reverse `GenericRelation`s match `Recording` so the same library / access /
audit-trail plumbing applies: `access_rights`, `collection_memberships`,
`dataset_memberships`, `tagged_items`.

## Storage

Uploads stream into `MEDIA_STAGING_PATH` and are moved to
`MEDIA_UPLOAD_PATH` at the end of the request, before the `MediaFile` row
is created — a row never points at a staging path. There is no processing
step between the tiers (unlike recordings, where a Celery task validates
and moves), so the move is synchronous.

In the compose stack both paths are subdirectories of the single
`media-data` volume (`/data/media/uploads`, `/data/media/staging`), which
makes the move an atomic same-filesystem rename and gives the files a home
that survives container rebuilds. Borg backs up only the uploads subtree;
staging holds in-flight uploads that are moved or deleted within a single
request.

**Migrating pre-existing deployments.** Rows created before the move step
existed carry a `file_path` inside the staging directory, and in
production those bytes live in the container's writable layer — copy them
into the `media-data` volume's `uploads/` subdirectory and update each
row's `file_path` before recreating the containers, or the files are lost
on the next deploy.

## Purge

`DELETE /{content_hash}` sets `deleted_at`; the row and file stay on disk but
drop out of every listing and read path. After `MEDIA_TRASH_RETENTION_DAYS`
(default 30) the `purge_deleted_media` Celery task hard-deletes the row and
unlinks the file — the GDPR Art. 17 erasure half of the soft-delete contract,
the non-signal analog of `recordings.tasks.purge_deleted_recordings`. It is
scheduled every 3 hours in `CELERY_BEAT_SCHEDULE`, alongside the recordings
purge.

The task is ⚠️ load-bearing. Its single filter
(`deleted_at__isnull=False, deleted_at__lt=cutoff`) is the line between erasing
PHI past the window and reaping live data. It unlinks the file before removing
the row; an unlink failure leaves the row in place for the next run rather than
orphaning the file. There is no orphan-reaper branch — media has no processing
step, so an active row (`deleted_at` null) survives any age. Per-row
`media.delete()` fires `pre_delete` inside the audited scope, so each erasure
lands in `ObjectChangeLog` under one `media.purge` activity. See AGENTS.md →
*Load-bearing files* and `TestPurgeDeletedMediaContract` before modifying.

## Allowed-extensions setting

`MEDIA_ALLOWED_UPLOAD_EXTENSIONS` is a list of lower-cased, dot-prefixed
file extensions consulted at both upload and download. The platform
default is empty — media uploads are disabled until a project opts in by
declaring the list in its `settings.py`:

```python
# projects/<name>/settings.py
MEDIA_ALLOWED_UPLOAD_EXTENSIONS = [".md", ".pdf", ".mp4"]
```

A project that accepts video also raises `MEDIA_MAX_UPLOAD_SIZE` above the
256 MB platform default (2 GB, say), since clips dwarf the
documents the default was sized for.

### Upload-side

Uploads with an extension outside the list return `400`. An empty
allowlist returns `403` ("media uploads are disabled for this project")
to make the disabled state distinguishable from a one-off rejection.

### Download-side

The same list is checked again when the file is requested. An
already-uploaded row whose extension has since fallen out of the live
allowlist (typically because the operator switched to a project with a
narrower list) returns `410 Gone`. The detail and list endpoints still
surface the row with `"is_supported": false` so the frontend can grey it
out and explain why.

This dual check means a project switch retroactively gates files without
mutating the database — the rows stay, the bytes stay on disk, only the
read path closes off.

## Attachments

`MediaFile.attachment_content_type` + `attachment_object_id` form a
generic foreign key to an optional parent object. The most common use is
attaching a video clip to its companion EEG recording, but the GFK keeps
the door open for future targets (collections, sessions, …) without
touching the schema.

The wire shape mirrors the pattern across the rest of the platform:

```json
"attached_to": { "type": "recording", "id": "<content_hash>" }
```

`type` is the lowercased model name; `id` is the public identifier
appropriate for that type — `content_hash` for recordings. New types
slot into `_ATTACHMENT_TARGETS` in [api/v1/ninja.py](api/v1/ninja.py) with
a `resolve_for_attach(user, public_id)` callback and a
`public_id(target)` reader.

### Timeline position

`time_offset` (seconds, nullable) is where the media sits on the attached
parent's timeline — the offset that aligns a video or audio clip to the
recording's `t=0`, or the moment an image pins to. It is settable on upload
and via PATCH, and surfaces in every read response. Because `null` is a
meaningful value (clear the alignment), the PATCH handler decides presence via
`model_fields_set` rather than treating `null` as "leave unchanged": omitting
the field keeps the current value, sending `"time_offset": null` clears it.

### Orphan policy

The parent target deliberately does **not** declare a reverse
`GenericRelation` to `MediaFile`. The asymmetry is intentional: when a
parent is hard-deleted (purged), the media row outlives it as an orphan
rather than cascade-deleting alongside. This is the GFK equivalent of
`SET_NULL` on a real FK.

Stale `(attachment_content_type, attachment_object_id)` pairs are handled
at serialise time: if the target row no longer exists, the response
returns `"attached_to": null` and the row reads as detached. Operators
can re-attach via PATCH.

## API

Mounted at `/media/api/v1/`.

| Method | Path | Auth | Description |
|---|---|---|---|
| `POST` | `/upload` | Authenticated | Upload a media file. Validates extension against `MEDIA_ALLOWED_UPLOAD_EXTENSIONS`; optional `attached_to_type` + `attached_to_id` attach to a parent object atomically; optional `time_offset` sets the timeline position |
| `GET` | `/` | Authenticated | List media files visible to the caller. Optional filters: `media_type`, `attached_to_type` + `attached_to_id` |
| `GET` | `/{content_hash}` | Authenticated | Detail. `original_name` is null for non-author / non-superuser callers (PHI policy mirrors `Recording`) |
| `GET` | `/{content_hash}/file` | Authenticated | Stream the file bytes with HTTP Range support. Video is served inline so the browser can play and seek; other types download as an attachment. `410` when the extension is no longer in the live allowlist; `404` for missing on disk. See [Serving and Range requests](#serving-and-range-requests) |
| `PATCH` | `/{content_hash}` | Author / superuser | Update `display_name`, `media_type`, `attached_to`, or `time_offset`. Pass `attached_to: {"type": "", "id": ""}` to detach; `time_offset: null` to clear the timeline position |
| `DELETE` | `/{content_hash}` | Author / superuser | Soft-delete (sets `deleted_at`) |

## Serving and Range requests

`GET /{content_hash}/file` serves bytes with HTTP Range support (RFC 7233),
ported from the recordings download path. `Accept-Ranges: bytes` is always
advertised; a `Range: bytes=` request gets a `206 Partial Content` stream, an
unsatisfiable range gets `416`, and a request without a range gets the full
`200`. Range support is what lets a `<video>` element seek.

Video is served **inline** with its real MIME type (`Content-Type: video/mp4`)
so the browser plays it in place; every other media type keeps the
download-as-attachment behaviour. The branch is in `_serve_disposition`.

**Download logging is deduplicated.** A seeking video client issues one range
request per seek; logging each would flood the audit trail. The endpoint logs
`media.download` only on the first request of a playback session — a full
download or a `bytes=0-` range (both report a start offset of 0). Mid-file
seeks (`206` with start > 0) and unsatisfiable ranges (`416`) are not logged.

### Federated video playback is not supported

A federated peer authenticates with a `FederatedBearer` JWT in a request
header, but a browser `<video src>` cannot attach custom headers, so a peer
cannot stream video the way it fetches an EDF. Federated video would need
signed URLs; until then, video playback is session- and `?share_token=`-only.
The Range-serving path itself is auth-agnostic, so a programmatic federated
fetch with an explicit `Range` header still works — only `<video>` playback is
constrained.

### PHI in video content is not removable

EDF headers are de-identified on the wire by the federation middleware
pipeline. There is no equivalent for video: a patient's face cannot be
stripped from MP4 bytes by a header rewrite. `original_name` is author-private
and `AccessRight` still gates who may read, but the *content* is PHI. Whether
to upload, share, or federate face-bearing video is a deployment policy
decision the platform does not — and cannot — enforce at the serving layer.

## Permissions

`MediaFile` participates in the same `AccessRight` / library-extension
machinery as `Recording`:

- The author always has full access (granted at upload time).
- Other users gain access via explicit `AccessRight` rows or via the
  library extensions (collection / dataset memberships).
- `can_read_object` / `can_write_object` / `can_annotate_object` all work
  unchanged — the extensions look up GenericFK memberships, so they pick
  up `MediaFile` automatically.

## De-identification

Same rules as `Recording`:

- `original_name` is author-private. The `_can_see_original_name` helper
  in [api/v1/ninja.py](api/v1/ninja.py) is the single check; grantees see
  `null`.
- URLs use `content_hash` rather than the integer PK.
- The `Content-Disposition` filename on download is built from
  `display_name + file_extension`, never from `original_name`.

## Audit trail

Every mutating endpoint calls `log_activity` with a verb from the
`media.*` namespace: `media.upload`, `media.download`, `media.update`,
`media.trash`. The `ApiActivityLoggingMiddleware` opens the audit-trail
scope so the standard `ObjectChangeLog` signals fire for every model
write. The `purge_deleted_media` Celery task adds `media.purge` under an
`interface=celery` scope (see [Purge](#purge)).

## Library integration

`CollectionItem` and `DatasetItem` are already GenericFK over any model,
so a media file goes into a collection or dataset through the same
generic library endpoint as a recording:

```
POST /api/v1/library/collections/{id}/items/
POST /api/v1/library/datasets/{id}/items/
{ "content_type_id": <media.mediafile CT id>, "object_id": "<content_hash>" }
```

The endpoint resolves a media `content_hash` to its internal PK the same
way it does for recording hashes — the frontend never deals with
integer PKs.

The list endpoints (`GET /collections/{id}/items/`,
`GET /datasets/{id}/items/`) return both recording and media rows in one
mixed-type response. Each row carries:

| Field | Recording | MediaFile |
|---|---|---|
| `object_type` | `"recording"` | `"mediafile"` |
| `object_hash` | content hash | content hash |
| `object_name` | display name | display name |
| `media_type` | null | `"document"` (today) |
| `file_extension` | null | `".pdf"`, `".md"`, … |
| `is_supported` | null | true / false (live allowlist check) |

The frontend's [DatasetView](../frontend/src/views/DatasetView.vue) and
[CollectionView](../frontend/src/views/CollectionView.vue) dispatch on
`object_type` to pick the right row component — `RecordingListRow` or
[MediaListRow](../frontend/src/components/MediaListRow.vue) — so the
recording flow is left untouched while media rows render with a
file-type icon and greyed + lock styling for `is_supported: false`.

Soft-deleted media files and soft-deleted recordings are both dropped
from listings; unsupported media stay visible (greyed) so the operator
sees what's there.

## Viewer integration

The platform's [ViewerView](../frontend/src/views/ViewerView.vue) loads
media items alongside recordings in three URL modes:

- `?dataset=<id>` and `?session=<token>` — load every item in the
  dataset(s) the URL points at; the dispatch reads `object_type` on
  each item and picks the right loader.
- `?media=<hash>` — open one or more media items standalone (same
  shape as `?files=<hash>` for recordings).
- `?files=<hash>` and `?media=<hash>` can be combined on the same URL
  to load a mixed bundle in declared order.

The viewer's UMD lib build runs the standalone viewer's
[setups/standalone.ts](../frontend/viewer/interface/src/setups/standalone.ts)
entry point, which already registers `doc/htm-file`
for markdown and `doc/pdf-file` for PDF — the platform does not (and
must not) re-register these because PdfImporter mutates a static worker
URL on construction. ViewerView's dispatcher maps `.md → doc/htm-file`
and `.pdf → doc/pdf-file`. Extensions outside this set are surfaced as
`is_supported: false` so the frontend can grey them out, and the viewer
loop skips them with a console warning.

## Read auth — three modes

`GET /media/api/v1/{hash}` (detail) and `GET /media/api/v1/{hash}/file`
(download) accept three auth modes in the same precedence order as the
recordings API:

1. **Django session** — the caller is the platform user; cookies travel
   with the worker fetch whenever the project's own join flow has set the
   session cookie.
2. **FederatedBearer JWT** — the caller is a trusted federated peer.
   The access check uses `get_federated_read_access_result` over
   `AccessRight` rows where `federated_peer` matches the peer and
   `remote_user_id` either matches the caller's `sub` or is blank
   (wildcard for any authenticated user from that peer). A
   `media_detail` / `media_download` audit row is emitted on every
   federated branch via `log_federation_access`.
3. **`?share_token=<token>`** query param — anonymous; the
   `AccessRight` row carrying that public token grants read. The
   library extension (`can_read_via_dataset`) propagates parent grants
   to attached media automatically, so a dataset share token reads any
   media row that sits inside the dataset.

A request that supplies none of the three returns 401 — not 404 — so
the caller can distinguish "missing credentials" from "no such
resource". A `share_token` that exists but doesn't grant this media
returns 404 (same shape as the recordings endpoints).

Write endpoints (upload, patch, soft delete) and the list endpoint
remain session-only.

## Upload UI

The platform's [MediaPickerDialog](../frontend/src/components/MediaPickerDialog.vue)
component embeds the upload flow inside the "Add media" picker used by
[DatasetView](../frontend/src/views/DatasetView.vue) and
[CollectionView](../frontend/src/views/CollectionView.vue). One picker
serves both consumers; the parent provides the list of hashes already
in its container so the picker excludes them.

Clicking **Upload new file** opens a hidden file input scoped to the
project's allowlist (`.md`, `.pdf`, `.mp4`, for instance). On change
the file streams to `POST /media/api/v1/upload` via
[uploadMedia](../frontend/src/api/media.ts) with an Axios upload
progress hook; the button label reports `Uploading… N%` while the
request is in flight. On success the new row appears at the top of the
picker list and is auto-checked so confirming the selection is a single
extra click; the existing rows stay where they were so the user doesn't
lose track of what they had picked before uploading.

Errors come back as toasts and as a callout at the top of the dialog:
the server's `detail` message is surfaced verbatim when present (e.g.
extension outside the allowlist, file too big), with a generic fallback
otherwise.

The dialog never sets `attached_to_*` on upload — phase 5 covers
stand-alone media. Attachment is settable later via the patch endpoint;
a future iteration will surface it in a dedicated media library view.

## What's not in phase 5

- **HTML documents.** The doc-module renders markdown and PDF today;
  HTML (`.htm` / `.html`) needs a separate importer wiring in the viewer.
  Once `doc/html-file` is registered there, the allowlist can grow
  without touching ViewerView's dispatcher.
- **Video player UI.** The backend serves MP4 inline with Range support
  and carries `time_offset`; the viewer's ACC video pane — a cursor-synced
  `<video>` reusing `useMediaCursorSync` — is a separate frontend task.
- **Standalone media library page.** No `/media` route yet for
  rename / delete / attach across the user's whole library. The picker
  covers the immediate add-to-container case; a dedicated page lands
  when the workflow demands it.
- **Attach-on-upload UI.** The endpoint accepts `attached_to_type` +
  `attached_to_id`; the dialog doesn't expose them. Attaching to a
  parent recording is a PATCH from the future library page.
