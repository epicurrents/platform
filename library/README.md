# library

Three organisational primitives for arbitrary platform objects: **Collections** (folder-like trees), **Datasets** (flat sets that propagate read access to their members), and **Tags** (a hierarchical label taxonomy). All three associate to objects through generic `content_type` + `object_id` membership rows, so any Django model is fair game — today the primary item type is `Recording`, but Datasets, Collections, and Tags can hold each other or any future model without schema changes.

Two read-permission extensions are registered here that the rest of the platform consumes implicitly: a `can_read` `AccessRight` on a Dataset propagates to every contained item; a `can_read` `AccessRight` on a Collection propagates to its immediate child items (capped by what the collection author can read).

## How the three differ

```
Collections                Datasets                  Tags
─────────────────         ─────────────────         ─────────────────
folder tree               flat membership            label taxonomy tree
(parent FK to self)       (no nesting)              (parent FK to self)

one object → at most      one object → any           one object → any
one collection            number of datasets         number of tags

private to the author     share-via-dataset          (no permission
(no sharing surface)      extension grants            propagation)
                          read to members

soft-delete via           soft-delete via            hard-delete only
deleted_at                deleted_at

identified by             identified by              identified by
INTEGER PK                opaque hash                INTEGER PK
```

Pick by intent:

- **Collection** when the goal is *arrangement*. Folders, project trees, "my sleep studies / my epilepsy cases". A recording lives in at most one collection.
- **Dataset** when the goal is *bulk sharing*. "Share these 200 recordings with this research group" without touching each recording's individual access rights. A recording can be in many datasets.
- **Tag** when the goal is *cross-cutting categorisation*. "Artifact / movement", "review-pending". Tags don't grant access; they're labels for filtering and search.

## Models

### `Collection`

A folder-like container with `author`, `name`, `description`, `parent` FK to self (`SET_NULL` on parent delete, so children become roots when the parent is hard-deleted), and `deleted_at` for soft-delete.

Collections are the author's private organisational tree: `can_read_collection` / `can_write_collection` in [permissions.py](permissions.py) grant only the author and superusers, no `AccessRight` may target a collection, and collection membership grants nothing on the items. Sharing what a collection organises goes through the [Collection → Dataset export](#api) — datasets are the platform's only sharing unit.

`Collection` declares reverse `GenericRelation` fields for `TaggedItem` and the four annotation types so that hard-deleting a collection cascades through every row that targets it — but not for `AccessRight`, which cannot target a collection. Soft-delete does not cascade. See [AGENTS.md → GenericFK target cascade pattern](../AGENTS.md#genericfk-target-cascade-pattern).

### `CollectionItem`

Membership record. Generic FK to the contained object via `content_type` + `object_id`, plus a `deleted_at` for the recursive-trash soft-delete (see [Recursive trash and restore](#recursive-trash-and-restore)). Two `UniqueConstraint`s, both **partial** on `deleted_at IS NULL`:

| Constraint | Effect |
|---|---|
| `library_item_unique_per_collection` | Same object cannot appear twice in one *active* membership of a collection. |
| `library_item_unique_per_object` | Same object cannot be *actively* filed in two different collections. |

The second constraint is what makes the "one collection per item" rule above true. Adding a recording to a collection while it is already actively in another returns **409**. Because the constraints are partial, a membership whose collection is trashed no longer occupies the slot, so the recording can be re-filed elsewhere while its collection sits in the trash.

### `Dataset`

Flat named set. Same shape as `Collection` minus the `parent` FK — no nesting. Owns the downward read-access propagation: a `can_read` `AccessRight` on the `Dataset` grants read on every contained item via the `can_read_via_dataset` extension. Write access is never inherited.

Also carries a `viewer_config` JSONField — a flat per-dataset viewer-settings override map (same shape as `epicurrents.ViewerConfigOverride.overrides`) applied on top of the deployment's project-level config when the dataset is opened in the viewer — and an `object_hash`, a 32-character opaque public identifier generated at save. The hash is what retires the integer-PK exposure described under [identifiers](#identifiers); responses serve it, and URL addressing moves to it with the frontend switch.

Like `Collection`, `Dataset` declares reverse `GenericRelation` fields for `AccessRight`, `CollectionItem`, `TaggedItem`, and the four annotation types — same rationale, same cross-reference.

A `release_gated` flag turns on [release gating](#release-gating): members stay hidden from every reader but the dataset's managers until a release run publishes them, no member resolves for a request carrying a share token, and a released member is dated by its release month. Only the author or a superuser may change the flag through `PATCH /datasets/{id}/`, since turning it off publishes every unreleased member at once. Three more fields make a dataset a [submission pool](#submission-pools): `submission_profile` (the key of a registered ingest profile, empty when the dataset is not a pool), `submission_group` (a one-to-one `PROTECT` FK to the pool's dedicated `auth.Group`) and `submissions_open` (intake). They are set through the pool endpoints, never the dataset PATCH.

### `DatasetMeta`

Governance metadata sidecar, one-to-one with `Dataset`, created on first write through the dataset PATCH endpoint. Holds the SPDX licence pair (`license_spdx`, `license_url`) — the only fields whose list is settled; contributors, funding, DOIs and subject-group description land here when designed. Absence means "nothing declared", and dataset responses serve `null` for both fields in that case.

### `DatasetSnapshot`

A **create-only** record of a dataset's membership at a point in time: FK to the dataset, `author`, optional `label`, a `manifest` JSONField of canonically-ordered member identities, a `manifest_hash` sealing the canonical serialisation, and its own `object_hash` for URLs. No update or delete endpoint exists; rows are written once and audited like everything else.

The manifest pins *identities*, not bytes: `content_hash` for recordings and media, `object_hash` for annotation types, `pk:<id>` as the documented last resort. Entries are sorted by `(content_type, identity)`, so equal membership always seals to equal bytes — which is what makes "model X scored Y on snapshot Z" checkable. Soft-deleted and FAILED recordings are excluded at sealing time, so the manifest matches the set every serving surface actually offers.

Because only hashes are pinned, a snapshot survives member purge or subject erasure as **unsatisfiable but still verifiable** — it proves what the set was without holding anything erasure removed, so erasure wins by construction and reproducibility degrades honestly. Pinned by `test_erasure_wins_and_verification_survives` in [tests/test_dataset_governance.py](tests/test_dataset_governance.py). Read access inherits the dataset's; creation requires write access. The author's snapshots are classified for the Art. 15 export in [apps.py](apps.py) with the manifest deliberately excluded — it holds content hashes of other subjects' recordings, and `manifest_hash` already proves what was sealed.

### `DatasetItem`

Membership record. Same shape as `CollectionItem` minus the "globally unique" constraint — an object can belong to many datasets. Indexed on `(content_type, object_id)` for the reverse lookup ("which datasets contain this object?") used by the permission extension. Carries a nullable `folder` FK placing the item in the dataset's [folder tree](#models); null means the dataset root, and the FK is `SET_NULL` so deleting a folder drops its items back to the root with membership untouched. A nullable `release` FK names the [release run](#release-gating) that published the item; null means unreleased, which in a release-gated dataset hides the member. `RESTRICT` on the release: deleting a run on its own would silently unpublish its members, while deleting the dataset or its author takes the runs and the items together, which `PROTECT` refused. `released_deidentification_version` is the pass version the member carried when the run released it, so the anonymity report compares each member against its own release.

### `DatasetRelease`

One release run of a release-gated dataset and the record it leaves: `dataset`, `author` (who ran it, nullable), `released_on`, `release_month` (the first instant of the run's month in UTC, derived at save and the one source of the month readers see), `profile_version`, `deidentification_versions` (the sorted distinct pass versions of the recordings released, filled by the run), `k`, `m`, `assessment_reference` and `member_count`. Who signed the run off is `DatasetReleaseSignOff` rows (`release`, `user` `SET_NULL`), one per existing account, so the sign-off is a relation the subject export sees; each is exported under the signer with the run and the date. The row is the process record EDPB Guidelines 02/2026 ¶ 41 asks to be kept with a release; the per-grant assessment fields on `AccessRight` are the general-sharing counterpart and are not used for dataset members. Exported under the runs a subject performed. The assessment reference is the runner's free text: scrubbed from the audit trail and cleared on the live row when their account is erased (a `pre_delete` receiver on the user model).

### `MemberApproval`

One curator's approval of one member of a release-gated dataset: `item` (the `DatasetItem`, cascade), `reviewer` (`SET_NULL`, one approval per curator per member) and `created_at`. A project's selector asks for a number of them before a member is published; see [curator review](#curator-review). A veto is not a row. Exported under the curator's approvals with `created_at` only, since the recording approved is another subject's data.

### `DatasetFolder`

Presentation-only folder tree inside a dataset: `dataset` FK, self-referential `parent` (CASCADE — deleting a folder removes its subtree of folder rows), grantee-visible `name`, and a `position` sibling sort key. The non-goals are the point: no `AccessRight` target, no hash identity, no reverse `GenericRelation`s, no annotation or tag attachment — visibility is the dataset's and nothing else's, so the model adds zero permission surface. Structure is cheap and regenerable, membership is precious: no folder operation can touch membership or data, and the folder-delete endpoint moves affected items to the root per row so each placement change is audited. A snapshot's `manifest_hash` covers membership only — organisation is presentation, not identity.

### `Tag`

Hierarchical label. `author`, `name`, `description`, `parent` FK to self (adjacency list, `SET_NULL` on parent delete), `curated`. Creating a tag is reserved for staff while `LIBRARY_TAG_CREATION_REQUIRES_STAFF` is on, which is the default, and a tag created by staff is stamped `curated`: it is the deployment's vocabulary and every authenticated user can list and apply it. An uncurated tag is reached only by its author and by readers of the objects it decorates ([tag reach](#tag-reach)). The stamp is taken at creation rather than derived from the author's current flag, so a later promotion or demotion does not change who sees what was typed. Only the tag author or a superuser can edit or delete the tag definition.

Tags are not soft-deleted. Removing a tag removes every `TaggedItem` row that references it.

### `TaggedItem`

Association between a `Tag` and any object. Same generic-FK pattern as the other two `*Item` models. An object may carry a given tag at most once.

## API

Mounted at `/api/v1/library/`. Full request/response detail in [api/v1/ninja.py](api/v1/ninja.py). All endpoints require an authenticated session unless noted.

### Collections

| Method | Path | Notes |
|---|---|---|
| `POST` | `/collections/` | Create. Optional `parent_id`. |
| `GET` | `/collections/` | List collections the caller can read. Filter by `parent_id` / `root` / `author`. |
| `GET` | `/collections/{id}/` | Detail. |
| `PATCH` | `/collections/{id}/` | Update name / description / parent. Requires write access. |
| `DELETE` | `/collections/{id}/` | Recursively soft-delete the collection, its sub-collections, and all memberships — see [Recursive trash and restore](#recursive-trash-and-restore). |
| `POST` | `/collections/{id}/restore` | Lift a trashed collection and its subtree back out of the trash. |
| `GET` | `/collections/{id}/items/` | List items the caller can read (per-item access filtering — see [N+1 gotcha](#gotchas) below). |
| `POST` | `/collections/{id}/items/` | Add an item. Returns 409 if the item is already in another collection. |
| `DELETE` | `/collections/{id}/items/{item_id}/` | Remove. |
| `POST` | `/collections/{id}/items/{item_id}/move` | Move an item to another collection in one request. Requires write access on both source and target; a move to the same collection is a no-op. |
| `POST` | `/collections/{id}/recordings/bulk-rename` | Assign sequential `display_name` values (`"{prefix} 1"`, `"{prefix} 2"`, …) to the recordings in this collection. See the Bulk-rename section under [API](#api). |
| `POST` | `/collections/{id}/export/` | Copy the collection subtree into a new dataset owned by the caller — see the Collection → Dataset export section under [API](#api). |

### Datasets

Same CRUD + items shape as Collections at `/datasets/`, plus the access surface collections deliberately lack:

- No `parent_id` — datasets don't nest.
- `POST /datasets/{id}/items/` does **not** raise 409 on multi-membership; an item can belong to many datasets.
- Access rights propagate downward to items via the permission extension (see [Permission extensions](#permission-extensions)).
- `viewer_config` — returned in the dataset response and settable via `PATCH /datasets/{id}/` (write access required, validated as a flat object). The viewer layers it on top of the deployment's project-level config when the dataset is opened.
- `release_gated` — returned in the dataset response and settable via `PATCH /datasets/{id}/` by the author or a superuser only (403 for a `can_write` grantee). In a gated dataset `GET /datasets/{id}/items/` lists unreleased members to managers only, orders by name (lower-cased display name, else stored name) instead of `added_at`, carries each row's `release_month`, and serves the release month as `added_at` to readers who are not managers. `GET /datasets/{id}/`, the item, folder and snapshot listings all answer 403 to a share-token request on a gated dataset, and a snapshot's manifest omits unreleased members. See [release gating](#release-gating).
- `GET /datasets/{id}/reviews/`, `POST` and `DELETE /datasets/{id}/reviews/{recording_hash}/approval` and `POST /datasets/{id}/reviews/{recording_hash}/veto` are the [curator review](#curator-review) of a release-gated dataset, for its managers only.
- `GET`, `POST`, `PATCH` and `DELETE /datasets/{id}/pool/` read, configure, change and dissolve the dataset's [submission pool](#submission-pools), for the author or a superuser only. On a pool, the PATCH answers 409 to a change of `release_gated`, `POST` and `DELETE` on the items answer 409, and so does the dataset DELETE while the pool is configured and unfilled or holds members or spooled files.
- `object_hash`, `license_spdx`, `license_url` — dataset responses carry the opaque identifier and the [DatasetMeta](#models) licence pair (`null` when undeclared); the licence fields are settable via the same PATCH.
- Snapshots: `POST /datasets/{id}/snapshots/` (write access) seals the current membership; `GET /datasets/{id}/snapshots/` lists newest-first without manifests; `GET /datasets/snapshots/{hash}/` returns one with its manifest. No update or delete routes exist — see [DatasetSnapshot](#models).
- All `/datasets/{id}/...` routes resolve the dataset `object_hash` or the integer PK — see [Identifiers](#identifiers).
- Folders: `GET /datasets/{id}/folders/` lists the tree as a flat list ordered `(parent, position, name)` (read access mirrors the dataset's, share tokens included); `POST`, `PATCH /{folder_id}/`, and `DELETE /{folder_id}/` under the same path manage it (dataset write access). `POST /datasets/{id}/items/{item_id}/move` places an item in a folder or back at the root (`folder_id: null`). Item listings report each item's `folder_id`.
- Deleting a folder cascades its sub-folders and drops the contained items to the dataset root — membership is never touched. Moving a folder under its own descendant is rejected with 400.
- `PATCH /datasets/{id}/access/{right_id}/` records, updates or clears the sharer's contextual assessment on a grant (`assessment_reference` + `assessment_date`, both or neither; a future date is 400), and `POST /datasets/{id}/access/` accepts the same pair at creation. The caller needs the access-management authority and then must be the row's giver, the dataset's author or a superuser; the listing serves the pair to the same three and `null` to anyone else, a `can_write` grantee included. The record's meaning and its subject surfaces are in [epicurrents/README.md → AccessRight model](../epicurrents/README.md#permissions). The dataset page's access panel writes the reference with a kind prefix chosen from a selector (`Contextual assessment: `, `DPIA: `, `Data-sharing agreement: ` or `Published dataset: `, fixed English tokens defined in [frontend/src/lib/assessment.ts](../frontend/src/lib/assessment.ts)) and explains what the document should contain in an expandable note; the API sees one free-text field either way.

### Grant delegation cap

`POST /datasets/{id}/access/` is the platform's only grant-creation surface on existing objects, and every call routes through [epicurrents/granting.py](../epicurrents/granting.py): a grant created by anyone but the author or a superuser may confer only rights the grantor holds. `can_write` requires holding write; `apply_middleware=False` requires the grantor's own read access to be raw; a conferred `expires_at` must not outlive the grantor's latest active `can_share` expiry; expired share rows qualify for nothing. Share-token rows are refused `can_write` / `can_share` for every grantor, authors included — refusal beats silent downgrade. Granting a user or group that already holds a row on the dataset returns 409 (`AccessRight` enforces one row per target); revoke the existing row first. Revocation is capped from the same module: the author's own row is revocable only by the author or a superuser, because several read paths resolve the author's access through it. Amplification refusals are security-logged (`permission.grant_amplification_refused`, `permission.author_grant_revoke_refused`). Contract tests: [tests/test_grant_capping.py](tests/test_grant_capping.py).

### Tags

| Method | Path | Notes |
|---|---|---|
| `GET` | `/tags/` | List the tags within the caller's reach. Filter by `parent_id`; a parent outside the caller's reach is 404. |
| `POST` | `/tags/` | Create. Staff only by default (403 otherwise); optional `parent_id`, which must be within reach. Returns `warnings` for a name or description that looks like an identifier. |
| `GET` | `/tags/{id}/` | Detail. 404 outside the caller's reach, the same answer as for a missing tag. |
| `PATCH` | `/tags/{id}/` | Update. Requires tag authorship; a new parent must be within reach. Returns `warnings`. |
| `DELETE` | `/tags/{id}/` | Hard-delete. Requires tag authorship. |
| `GET` | `/tags/{id}/items/?include_children=true` | List items tagged with this tag, by default including items tagged with any descendant. Set `include_children=false` to exclude descendants. 404 outside the caller's reach. |
| `POST` | `/tags/{id}/items/` | Tag an object. Requires write access to the object and a tag within the caller's reach. |
| `DELETE` | `/tags/{id}/items/{item_id}/` | Untag. Requires write access on the object **or** authorship of the tag. |

`include_children` uses `_get_tag_subtree_ids` — a single DB query + in-memory BFS to expand the tag's descendant set, so a deeply-nested taxonomy doesn't fan out into many queries.

### Tag reach

A tag name is free text typed by whoever created it, and the listing used to hand every name to every authenticated user. [tag_scope.py](tag_scope.py) decides what a caller can list, read and apply: a superuser reaches everything; everyone else reaches the curated tags, the tags they authored, the tags on objects they can read, and the ancestors of all of those, so the tree stays browsable from its root. Reach through an object uses the same per-item check as the item listings, over the distinct objects carrying a tag not already in reach, so the cost is bounded by what people have tagged rather than by what the platform holds. Every tag endpoint answers a tag outside the caller's reach with the 404 it gives a missing tag, so the id space cannot be walked for names. Untagging is unchanged: it already requires write access on the object or authorship of the tag.

### Free-text warnings

Every endpoint that writes a grantee-visible label returns a `warnings` list beside its result: collection and dataset create and update (`name`, `description`), folder create and update (`name`), tag create and update (`name`, `description`) and the bulk-rename (`prefix`). Each row is `{field, kind, message}`, produced by `name_warnings` in [epicurrents/text_hygiene.py](../epicurrents/text_hygiene.py) when the text reads as a personal name, contains a run of six or more digits, contains a date, or matches one of the deployment's own `TEXT_HYGIENE_PATTERNS`. The write has happened regardless; the frontend surfaces each row as a warning toast against the field. Read responses carry no `warnings` key.

### Bulk-rename

`POST /collections/{id}/recordings/bulk-rename` assigns sequential `display_name` values to the recordings in a collection, ordered by `added_at`. Payload: `{"prefix": "<prefix>"}` (defaults to `"Recording"`). Each writable recording gets `display_name = "{prefix} {n}"` with `n` starting at 1. Non-writable, FAILED, or soft-deleted recordings are skipped *without* advancing the counter, so the resulting numbers always form a contiguous 1..N sequence across the rows actually renamed. Returns `{"renamed": N, "skipped": M, "warnings": [...]}`, the warnings covering the prefix. The prefix is user-typed text and stays out of the activity row's metadata; the renamed rows' own change records carry the resulting names.

Requires read access on the collection and write access on each affected recording. Wrapped in `transaction.atomic()` so a partial run never leaves a half-renumbered collection.

### Collection → Dataset export

`POST /collections/{id}/export/` copies a collection's subtree into a freshly created dataset owned by the caller, who needs read access to the collection. Payload: optional `name` and `description` (default to the collection's) and `materialise_hierarchy` (default true) — when set, active sub-collections become `DatasetFolder` rows mirroring the tree and each item lands in the folder of its source collection; root-collection items land at the dataset root. Membership is copied, never moved: the source collection and its memberships are untouched.

The per-item read check caps what crosses over — items the caller cannot read, soft-deleted objects, and FAILED recordings are skipped and counted in the response's `skipped_count`, so an export can never surface more than the caller already holds. Trashed sub-collections and their memberships stay behind. This endpoint is what replaced collection sharing: a collection's contents convert to a shareable dataset without manual rebuilding.

### Item listings hide FAILED recordings

`_enrich_collection_items` (used by collection and dataset item listings) and the tag-item queryset both filter out recordings with `status=FAILED` for **every viewer, including the author**. The author's view of FAILED recordings is the dedicated recordings list — surfacing them in collection / tag listings would expose their PHI-bearing `original_name` to grantees who share the collection, and the same hiding rule applies platform-wide per [recordings/README.md → FAILED-hidden rule](../recordings/README.md#failed-hidden-rule). Soft-deleted recordings are dropped the same way.

`object_name` on collection / dataset item responses resolves through `recordings.api.v1.ninja._resolve_display_name` so grantees see the recording's `display_name` (or its hash-prefix default) — never `original_name`.

### Mixed-type listings (Recording + MediaFile)

The same `_enrich_collection_items` helper resolves [`media.MediaFile`](../media/README.md) rows alongside `Recording` rows. Each item carries an `object_type` discriminator (`"recording"` / `"mediafile"`) and, for media rows, additional fields the frontend uses for type-specific rendering:

- `media_type` — `"document"` today; future media types slot in here.
- `file_extension` — lowercase, dot-prefixed.
- `is_supported` — false when the file's extension is no longer in the live `MEDIA_ALLOWED_UPLOAD_EXTENSIONS`, so a project switch retroactively greys out items the active project can't open.

Soft-deleted media files are dropped from listings the same way soft-deleted recordings are; unsupported media stay listed so users see what's there. The `add_item` endpoint accepts a media `content_hash` as `object_id` and resolves it server-side via `_resolve_media_object_id`, mirroring the recording-hash resolver — the frontend never deals with integer PKs.

## Recursive trash and restore

Trashing a collection is an OS-trash-can operation: `delete_collection` walks the subtree (`_subtree_collection_ids`) and soft-deletes the collection, every sub-collection, and every `CollectionItem` beneath it, all under one shared `deleted_at` timestamp. Per-object `.save()` (not a bulk `update`) so the audit signals fire for each trashed row.

`restore_collection` (`POST /collections/{id}/restore`) reverses it, lifting exactly the rows that share the collection's `deleted_at` — so a sub-collection trashed separately, earlier, stays trashed. A membership is skipped if the object has since been filed into a live collection: an explicit re-filing wins over the restore.

The referenced objects are never trashed by this — only their membership. So a recording whose sole collection is trashed:

- **drops out of the collection tree** (its membership is soft-deleted; `list_items` and the collection listings hide it);
- **surfaces at the library root** — the recordings `?uncollected=true` filter excludes only recordings with a *live* membership, so trashed-only ones count as unfiled and stay first-class, deletable objects;
- **carries a `trashed_collection` cue** (`{id, name}` on `RecordingOut`, populated on the uncollected listing) naming the collection it will drop back into if that collection is restored — distinguishing it from a genuinely-never-filed recording.

Because both `CollectionItem` uniqueness constraints are partial on `deleted_at IS NULL`, the trashed membership does not block re-filing the recording elsewhere while its collection sits in the trash. There is no collection purge job, so a trashed collection (and its trashed memberships) persists until restored or the objects are individually reassigned.

## Permission extensions

Registered in [apps.py](apps.py) `ready()`:

```python
register_read_permission_extension(can_read_via_dataset)
register_federated_read_extension(
    check=can_read_via_dataset_federated,
    visible_terms=federated_dataset_visible_terms,
)
```

Consulted only when no direct `AccessRight` row matches the caller and the target object (the early-return rule in `get_read_access_result` — see [epicurrents/README.md](../epicurrents/README.md#permissions)).

### `can_read_via_dataset`

Grants read on `obj` when `obj` is a member of a non-deleted `Dataset` for which the caller (user, group, or supplied share token) holds an active `can_read` `AccessRight`. The returned `ReadAccessTerms.apply_middleware` reflects the matching Dataset right's `apply_middleware` flag, so EDF files served through dataset-inherited reads honour the sharer's de-identification choice. Write is never inherited.

### `can_read_via_dataset_federated` and `federated_dataset_visible_terms`

The same rule for a federated peer, registered as a pair through `register_federated_read_extension`. A separate implementation is not duplication: the local checker matches an `AccessRight` against a user, their groups or a share token, and a peer has none of those — only a peer row and an opaque remote user id. A wildcard grant (`remote_user_id=""`) covers every user from that peer, an exact one covers a single user, and `apply_middleware` comes from the dataset's grant row exactly as in the local path.

The `visible_terms` half answers for listing endpoints, which resolve access for many rows at once with a batch query rather than a per-object check. It returns terms rather than bare ids because the federated listing also advertises a download size, which depends on whether the bytes are transformed on the way out. Both halves register together because the failure that matters is disagreement: a listing that omits what the object endpoint serves hides shared data, and one that includes what the object endpoint refuses advertises 404s. Disagreement over the *terms* is the same failure wearing a quieter costume — a peer is shown a download size computed one way and handed bytes decided the other — so both halves rank a dataset's grants identically: an exact-user row outranks the peer-wide wildcard, and among rows of equal specificity the de-identifying one wins. That second key is not decoration. A recording in two datasets shared with the same peer produces two wildcard rows with nothing to choose between them, and without it the database's row order decides whether the peer receives the de-identified file or the raw one.

Placement inside a dataset is not carried across federation yet — `DatasetItem.folder` describes a tree on the owning side, while a peer sees a flat list. A recipient expecting a layout (BIDS, say) has to rebuild it from names.

No collection counterpart exists: collections grant nothing on their items, and `TestCollectionRowsGrantNothing` in [tests/test_permissions.py](tests/test_permissions.py) pins that a stale collection-targeted `AccessRight` row stays inert.

The same `ready()` registers two read-visibility gates from [release.py](release.py), consulted before any grant is read: `member_hidden_from_reader` for `recordings.recording` and `dataset_hidden_from_reader` for `library.dataset`. See [release gating](#release-gating).

## Release gating

A release-gated dataset is a pool whose members must not surface one by one as they arrive: the multi-centre teaching dataset the [anonymisation plan](../docs/engineering-notes/anonymisation-compliance-plan.md) describes under Phase 7, where the arrival time and order of a member is the centre fingerprint the pooling exists to hide. Four rules apply to a dataset with `release_gated` set, all in [release.py](release.py). Each acts only on memberships the gating party controls: a member the dataset's author authored, or in a [submission pool](#submission-pools) a pooled recording, one the system user owns, which only the pooled ingest creates (`controls_membership`). Turning the gate on answers 409 while any recording or media member is anyone else's, adding an object to a gated dataset answers 409 unless the object's author is the dataset's author, superusers included, and a membership that got in past both hides, dates and withholds nothing. Without the rule, a reader of a released pool member could gate a dataset of their own holding it and hide it from every other reader.

- **Unreleased members are hidden.** A member whose `DatasetItem.release` is null resolves for the dataset's managers only: the dataset author, a holder of a `can_write` grant on the dataset, the member's own author and superusers. Everyone else, a direct grantee included, is denied inside `get_read_access_result` before any grant is read, so every surface that resolves through the permission layer honours it. The gate is registered for recordings and media files. The recording endpoints shape the denial as 404 through `_hidden_for_caller` in [recordings/api/v1/ninja.py](../recordings/api/v1/ninja.py), the FAILED-hidden pattern; the recording listing's federated branch subtracts `unreleased_member_ids` the same way. A member that is unreleased in one gated dataset is hidden everywhere, whatever other datasets or direct grants reach it.
- **No member resolves for a request carrying a share token**, released or not, whoever holds the token, and the gated dataset itself is invisible to share-token callers. `can_read_via_dataset` skips gated datasets for a share-token request, so a token on a gated dataset reaches none of its members whoever controls them. A forwardable link is the onward transfer the gate exists to prevent, and the access-control argument needs an individual account.
- **A member's stored digest reaches its managers only.** `stored_hash` is served empty on the recording detail, listing and slice responses to every reader who is not the recording's author, a superuser or a manager of a gated dataset holding it, federated peers included, and `?expect_stored_hash=` answers 400 to the same readers whatever the value, since a pin answers whether a guessed digest matches. A pooled contributor holds a receipt naming the submitted bytes by their SHA-256, and nothing guarantees the ingest pass changes every file, so a served digest could equal a receipt and let whoever holds one find the member. The decision is `stored_digest_withheld_ids` in [release.py](release.py), and for a pooled recording it holds whatever has become of the pool: trashing it or turning its gate off serves no digest. Readers lose the content pin with it, which nothing reading a pool uses; the keyed digest described below this list would give it back.
- **A released member is dated by its release month.** `DatasetRelease.release_month` replaces `created_at` on the recording detail, listing and slice responses for every reader who is not the recording's author or a superuser (the platform-wide month truncation is the floor; the dataset replaces the value), and the recording listing sorts a member as if uploaded at that instant, then by name among members released in the same month. The dataset item listing orders by name whatever the member's type (recordings and media by display name, else their stored name; nested datasets by name; anything else last, by object id) and serves the month as `added_at`. A pooled recording keeps the month of its latest release when its pool is trashed or ungated, and one never released is dated `WITHHELD_DATE` (1970-01-01), since its ingest month is the arrival time. The item listing serves `id` and `object_id` as null to a reader who does not manage the dataset, both being sequential, and the same for a pooled recording in any dataset or tag listing unless the caller is a superuser or manages a pool holding it; `object_hash` names the member. Any dataset listing, gated or not, drops a member the gate would hide from the caller: every gated member for a share-token request, an unreleased one for a non-manager. Nothing a reader sees says when a member arrived.

**Release runs** happen on a monthly cadence through `manage.py release_dataset <hash>`: a member uploaded in month M is eligible from the run at the start of M+2 (`eligibility_cutoff`), so each waits between one and two months and a month's submissions from every contributor surface together, backlog and new submissions alike. Trashed and FAILED recordings are never eligible. The cadence is the platform's; which eligible members a run publishes is the project's, through `register_release_selector(fn)` from the project's `apps.py::ready()`. The selector is called as `fn(dataset, eligible, as_of=date)` and returns a `ReleaseDecision` naming the subset to publish and whatever it knows of the record fields (profile version, k and m, sign-offs, assessment reference); the command line fills the rest, and the sign-offs of both are kept. Without a selector a run publishes everything eligible. A run always leaves a `DatasetRelease` row, even one that released nothing, audited as `library.dataset.release` against the dataset with each published item's change recorded under it; `--dry-run` reports the eligible and selected members and writes nothing. `run_release` takes the dataset's row lock and the unreleased items' before it selects, so two runs cannot release the same member, and refuses an `as_of` after today (UTC) or before the dataset's latest release: the first would move the cadence forward and stamp a month that has not happened, the second would date members before ones already published. The maintenance operation records the requesting superuser as the run's author (`--actor-id`, supplied by the executor from the job) and takes no assessment reference, since a job's arguments are served to every staff caller and kept past every erasure path; a run that needs one is run with the command.

**Withdrawal** is `manage.py purge_dataset_recordings` in the recordings app, keyed on `Recording.file_hash` and scoped to pooled recordings, system-authored members of a submission pool, so no withdrawal reaches a platform user's own recording whatever dataset holds it; its per-hash report is the platform's only answer to whether a submitted hash exists ([recordings/README.md → Soft delete and purge](../recordings/README.md#soft-delete-and-purge)). A deployment that sets `LIBRARY_RELEASE_GATED_DEPLOYMENT` refuses to boot with `RECORDINGS_ORIGINALS_PATH` configured ([checks.py](checks.py)), because a pool keeps no copy its contributors do not also hold and the purge never reaches that volume. It also refuses to boot without `RECORDINGS_DISCARD_EMBEDDED_ANNOTATIONS`: an ordinary upload joins the pool through a release run as a submission does through the pooled ingest, and the pooled ingest keeps none of a file's text, so an upload must not either.

**Feeding the pool** is the validating submission path in the recordings app ([recordings/README.md → Submissions](../recordings/README.md#submissions)): members of a [submission pool](#submission-pools)'s group submit prepared files, each checked against the pool's ingest profile and written nowhere unless it passes; an hourly run ingests accepted files from every contributor's ledger in random order under the system user, as unreleased members. A release run is also registered as the `library.release_dataset` maintenance operation, so a curator with no shell runs it from the Maintenance tab.

**Two reports** sit beside the pool's written assessment, both read-only, both in [reports.py](reports.py) and both also maintenance operations. `manage.py dataset_access_report <hash> [--days N | --since D]` is the evidence for the access-control argument and the input to the six-monthly sweep: per recording member, the requests, byte-serving requests and distinct authenticated readers the `Activity` trail shows over the window (half a year by default), the requests that carried no account, the month of the last read, and per dataset the distinct readers, the refused requests and the largest and median request count per reader. Counts only; no reader is named. It reads archived rows as well as live ones, since the archiver's default window is shorter than the sweep's, and listing requests are not per member and are not counted. `manage.py dataset_anonymity_report <hash> [--release ID]` is the ¶ 41 record per release, re-run after every release run and every profile change: the record the run stored, the members still present (live and READY, the set `select_by_class_size` counts) and the number withdrawn since, a trashed or FAILED member among them, the pass version now stamped on each with a member re-written since the run flagged, and the equivalence-class sizes over the pool as released up to that run, from which it derives the minimum k, the fraction of members below the recorded k, the prosecutor risk and the entropy in bits. What a class is comes from the project, through `register_equivalence_class(fn)` beside the selector: `fn(recording)` returns a class key or `None`, and a selector is expected to apply the same function so the k it records and the k the report finds agree. `select_by_class_size(dataset, eligible, k=k)` is that condition for a selector to build on: an eligible recording is selected once its class, counted over the released and still readable members plus the eligible ones, holds k; unclassified members and members that are not recordings never are. Without a registration the report prints the record and says so. Two things are repeated rather than recomputed, and the report says which: m, the contributor count the pool was held to at ingest, since the pooled ingest keeps no contributor per recording, and journalist risk, which needs population frequencies the platform does not hold. Both runs are audited against the dataset, as `library.dataset.access_report` and `library.dataset.anonymity_report`, with counts in the metadata.

**A keyed digest for pinning** is not built, and is recorded for when a study needs to pin pooled content. Instead of withholding `stored_hash`, a gated member could serve an HMAC-SHA256 of its stored digest under a server-side key, and the pin would compare against that. The pin would keep working, because the server does the comparison, and the served value could not be matched against a receipt or recomputed by anyone who holds the original and can re-run the pass, since neither holds the key. Three costs kept it out: a reader could no longer check a downloaded file against the value themselves, rotating the key breaks every pin taken under the old one unless the value carries a key version as the audit hashes do, and it is a second digest to keep in step with `refresh_signal_metadata` whenever the platform rewrites a file. Withholding is the smaller change while nothing reading a pool pins its content.

## Curator review

A release-gated dataset's members are reviewed by its managers (`is_dataset_manager`: the author, a superuser, a holder of a `can_write` grant), who can already read unreleased members, for content that identifies a patient whatever the class size: a rare condition, a skull defect, a device, an unusual protocol. No count-based gate catches those.

- **An approval** is a `MemberApproval` row, one per curator per member, given and withdrawn only while the member is unreleased (409 after). `GET /datasets/{id}/reviews/` answers each recording member's approval count and whether the caller gave one, and the approvals a pool's profile asks for (`IngestProfile.approvals`, one when unset; null for a dataset that is not a pool). It never names who approved: that is the run's record, not something one curator reads about another. A FAILED member is left out of the listing and answers 404 to a curator who is not a superuser, under the same [FAILED-hidden rule](../recordings/README.md#failed-hidden-rule) as any grantee.
- **A veto** removes the member at once, released or not, and cannot be undone. A pooled recording vetoed in its pool goes whole: `veto_member` unlinks the file, then deletes the recording and with it the membership and its approvals. Anything else only leaves the dataset: a recording a platform user owns, since managing a dataset confers no right to delete another user's data, and a pooled recording someone placed in a dataset other than its pool, since managing that dataset confers no right over the pool. The activity (`library.dataset.member.veto`), like the two approval activities, targets the membership and carries the reason code, whether the member had been released and what was removed (`recording` or `membership`). The reason is one of `VetoReason` and never free text, because a typed reason tends to describe what identifies the patient and the trail is permanent. A contributor is not told: the platform cannot name them, and a later withdrawal of the hash reports it not found.
- **What a release takes from it** is the project's: `approved_items(items, required=n)` keeps the members approved by n distinct curators, and `approvers_of(items)` is the sign-off of a run publishing them. Only an approval whose curator still manages the dataset counts, so a curator whose write grant is revoked or expires releases nothing more and signs nothing off; one whose account was deleted still counts. The run's sign-off rows are the union of the selector's and `--sign-off`, so a run signed off as a whole by someone who reviewed nothing records them too.
- **Who asks** is checked before what: a caller who does not manage the dataset gets 404 whether or not it is gated, so the review routes do not tell anyone which datasets are pools.

Nothing links a recording to its contributor, so the platform cannot keep a curator from reviewing a recording their own centre sent.

## Submission pools

A *pool* is a release-gated dataset fed only through the validating submission path, configured from the dataset page by its author or a superuser, so a deployment with no shell can set one up. A contributor's *ledger* (`recordings.SubmissionLedger`) is their record for one pool: created by the server with their first accepted file, one per contributor, never shown to them, and the audit target of their accepted submissions. A pool is *filling* once any ledger exists. Accepted files wait in the spool for the pooling delay, and until the pool has the profile's m contributors, then as unreleased members until their equivalence class holds k recordings and a release run publishes it; the equivalence class is the unit of release, never a ledger. m is a condition on the pool rather than on each class, because counting contributors per class means holding which ledger fed which class, and beside the ingest runs that joins a recording back to its ledger. The rules are in [pools.py](pools.py) and the endpoints in [api/v1/ninja.py](api/v1/ninja.py) translate a refusal into its status.

- **Configured on an empty dataset only**, because every member must have passed the same gate and a member added another way carries whatever its uploader's site left in it. `POST /datasets/{id}/pool/` with `{"profile": key}` sets the profile, creates the dedicated group (named after the dataset, with a hash tag on a name collision), turns the gate on and leaves intake closed so the group can be filled first. 409 on a dataset with members or already a pool, 400 for an unregistered profile.
- **Intake** opens and closes with `PATCH {"open": bool}` in every state. Opening answers 409 while the contributor group has fewer active members than the profile's `m`; closing is never refused, and a member leaving later does not close it, since the pooled ingest holds the pool until m of those members have contributed. Closing refuses new files and leaves accepted ones to be ingested.
- **Before it fills**, `PATCH {"profile": key}` changes the profile and `DELETE` dissolves the pool: the configuration and the group go and the gate turns off.
- **Once filling**, the profile, the group and the gate are fixed, since every member was checked against them; the profile PATCH and the dissolve answer 409. The pool's membership is never edited by hand in any state: items are neither added nor removed through the item routes, and withdrawal goes through `purge_dataset_recordings`. The dataset can be trashed once it has no members and nothing in the spool. A hard-deleted pool takes its group with it, since the group would otherwise outlive it as an ordinary one; every pool write and each accepted submission take the dataset's row lock, so a dissolve and a submission do not interleave.
- **The group grants nothing else.** [pools.py](pools.py) registers the pool groups with `user.dedicated_groups` ([user/README.md → Dedicated groups](../user/README.md#dedicated-groups)), so an access grant targeting one is refused on the dataset and upload grant paths, it is left out of the grant-target group listing, a project role on it is refused, and the group admin endpoints refuse to delete it and report its pool as `dedicated_to`. `library.W001`, a database check run by `migrate` and `check --database default`, reports a pool group that acquired a grant or a role through another path.
- **`GET /datasets/{id}/pool/`** answers the state the dataset page renders: `profile`, `group_id` and `group_name`, `open`, `filling`, `configurable` (not a pool and empty), the pending, failed and ingested file counts over every ledger together, never per contributor, and `contributor_count` (active group members) beside `contributors_required` (the profile's `m`, or null).

Every operation audits against the dataset, whose change record carries the fields: `library.dataset.pool.read`, `.create`, `.update` (the fields changed) and `.delete` (the member count of the deleted group, which nothing else records).

## Identifiers

Datasets are addressed by `Dataset.object_hash` — a 32-character random identifier that leaks nothing about creation order or count. Every `/datasets/{id}/...` route resolves either the hash or the integer PK (`_get_active_dataset` mirrors the dual resolution recordings use), the frontend builds its dataset URLs and viewer `?dataset=` links from the hash, and the PK form stays accepted for internal callers and old links. Snapshots are hash-addressed from birth.

Collections and Tags are identified by **integer PK** in URLs (`/collections/42/`, `/tags/3/`). A collection PK reveals nothing about the contained data — items keep their own opaque identifiers — and the tag taxonomy is global and browseable by all authenticated users, so PK leakage is not meaningful for either.

Items inside collections / datasets / tags are still referenced by the contained object's opaque identifier (e.g. recording `content_hash`), not the membership row's PK.

## Settings consumed

| Variable | Default | Notes |
|---|---|---|
| `LIBRARY_TAG_CREATION_REQUIRES_STAFF` | `True` | Reserve tag creation for staff; a tag created by staff is curated and listed to everyone. Off, anyone creates uncurated tags, listed by [reach](#tag-reach). |
| `LIBRARY_RELEASE_GATED_DEPLOYMENT` | `False` | Declares a deployment whose datasets are [release-gated](#release-gating); with it on, `manage.py check` refuses a configured `RECORDINGS_ORIGINALS_PATH` and requires `RECORDINGS_DISCARD_EMBEDDED_ANNOTATIONS`. Set by the project's settings. |
| `TEXT_HYGIENE_PATTERNS` | `{}` | Core setting; the deployment's own identifier patterns for the [free-text warnings](#free-text-warnings). |

The app also consumes the cross-app `AccessRight` model from `epicurrents`.

## Project plugin extension points

| Hook | How |
|---|---|
| Make a project model a collection / dataset / tag target | Nothing required — the generic-FK membership rows accept any Django model. Add an instance via `POST /collections/{id}/items/` with the model's `content_type` and `object_id`. |
| Register an additional read-permission rule | `register_read_permission_extension(callable)` from your project's `apps.py::ready()`. Your extension is consulted alongside the two library extensions. See [epicurrents/README.md](../epicurrents/README.md#permission-extensions). |
| Decide what a release run publishes | `register_release_selector(fn)` from your project's `apps.py::ready()`; see [release gating](#release-gating). The platform applies the monthly cadence and hands the selector the eligible members. |
| Add project-specific tag namespacing | The platform has no namespace mechanism for tags today — tag names are a flat global string. If a project needs namespaces, the cleanest path is a prefix convention (`epicurrents.<project>.<concept>`, mirroring the `Code.standard` pattern) enforced at the project's API layer. |

## Tests

```bash
pytest library/tests/
```

The permission tests live in [tests/test_permissions.py](tests/test_permissions.py) and cover `can_read_via_dataset`, the author-only collection gate, and the inertness of stale collection-targeted rows. API tests are in [tests/test_api.py](tests/test_api.py). Release gating, the cadence and the `release_dataset` command are in [tests/test_release.py](tests/test_release.py); the two reports and their commands in [tests/test_reports.py](tests/test_reports.py); the curator review, its endpoints and the run's sign-off in [tests/test_review.py](tests/test_review.py).

## Gotchas

- **Per-item access filtering is N+1.** [api/v1/ninja.py](api/v1/ninja.py) `_filter_readable` and `user_can_read_item` in [item_access.py](item_access.py) check read access for each item individually when listing collection or tag contents, and the tag reach in [tag_scope.py](tag_scope.py) does the same over the distinct tagged objects. This is fine when the collection or tag scope is bounded and most items are readable. It becomes expensive if a large tag contains many items the caller cannot access — the iterator has to scan past every unreadable item to fill one page. **If a global tag browser is ever added** (listing all items with a tag across all users, or any view where the caller is expected to have access to only a small fraction), switch to the batched check: one `AccessRight.objects.filter(content_type__in=..., object_id__in=..., can_read=True)` over the full page plus a separate `DatasetItem` + `AccessRight` batch query. That drops per-page query count from O(items) to O(distinct content types).
- **Extensions may return a plain `bool`.** The extension protocol normalises `True` to `ReadAccessTerms(granted=True, apply_middleware=False)`, so bool-returning project extensions work. Return `ReadAccessTerms` when you need to propagate middleware behaviour, as `can_read_via_dataset` does.
- **Don't extend `register_read_permission_extension` lightly.** Each new extension is consulted on every read check that doesn't hit a direct `AccessRight`. The existing extension is scoped to single reverse-lookup queries; an extension that triggers heavy work per call will degrade every authorisation path that reaches it. Profile before registering.
- **Rollback does not reach pool or release state.** [rollback_guards.py](rollback_guards.py) registers an `activity.audit.register_rollback_guard` for each model the pool and release rules govern: a dataset rollback that would change `submission_profile`, `submission_group`, `submissions_open` or `release_gated`, the rollback of a pool's creation, any rollback of a release run, a sign-off, an approval, a ledger or a spooled file, a membership rollback in a gated dataset or a pool or one moving a release pointer, and any rollback of a pooled recording. A refused rollback answers 400, and the bulk pre-flight 403; superusers are refused too. A rename or a licence change of a pool still rolls back.
- **Dataset writes read under the row lock.** `PATCH /datasets/{id}/` re-reads the dataset under `select_for_update` and saves only the fields it sets, and `DELETE` checks the pool rule on the locked row, since the pool writes and each accepted submission take the same lock; a whole-row save from an unlocked read once put back pool state a concurrent configure had just written.
- **`purge_deleted_library` deletes row by row in savepoints.** A row that cannot be deleted is logged and left for the next run, counted under `failed`, rather than stopping the purge of everything after it.
- **The two `CollectionItem` uniqueness constraints test independently.** When adding tests for the constraints, exercise each one separately — there are two different failure paths (409 from "already in this collection" vs 409 from "already in another collection") and the API caller may want to distinguish them.
