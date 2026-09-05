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

### `DatasetMeta`

Governance metadata sidecar, one-to-one with `Dataset`, created on first write through the dataset PATCH endpoint. Holds the SPDX licence pair (`license_spdx`, `license_url`) — the only fields whose list is settled; contributors, funding, DOIs and subject-group description land here when designed. Absence means "nothing declared", and dataset responses serve `null` for both fields in that case.

### `DatasetSnapshot`

A **create-only** record of a dataset's membership at a point in time: FK to the dataset, `author`, optional `label`, a `manifest` JSONField of canonically-ordered member identities, a `manifest_hash` sealing the canonical serialisation, and its own `object_hash` for URLs. No update or delete endpoint exists; rows are written once and audited like everything else.

The manifest pins *identities*, not bytes: `content_hash` for recordings and media, `object_hash` for annotation types, `pk:<id>` as the documented last resort. Entries are sorted by `(content_type, identity)`, so equal membership always seals to equal bytes — which is what makes "model X scored Y on snapshot Z" checkable. Soft-deleted and FAILED recordings are excluded at sealing time, so the manifest matches the set every serving surface actually offers.

Because only hashes are pinned, a snapshot survives member purge or subject erasure as **unsatisfiable but still verifiable** — it proves what the set was without holding anything erasure removed, so erasure wins by construction and reproducibility degrades honestly. Pinned by `test_erasure_wins_and_verification_survives` in [tests/test_dataset_governance.py](tests/test_dataset_governance.py). Read access inherits the dataset's; creation requires write access. The author's snapshots are classified for the Art. 15 export in [apps.py](apps.py) with the manifest deliberately excluded — it holds content hashes of other subjects' recordings, and `manifest_hash` already proves what was sealed.

### `DatasetItem`

Membership record. Same shape as `CollectionItem` minus the "globally unique" constraint — an object can belong to many datasets. Indexed on `(content_type, object_id)` for the reverse lookup ("which datasets contain this object?") used by the permission extension. Carries a nullable `folder` FK placing the item in the dataset's [folder tree](#models); null means the dataset root, and the FK is `SET_NULL` so deleting a folder drops its items back to the root with membership untouched.

### `DatasetFolder`

Presentation-only folder tree inside a dataset: `dataset` FK, self-referential `parent` (CASCADE — deleting a folder removes its subtree of folder rows), grantee-visible `name`, and a `position` sibling sort key. The non-goals are the point: no `AccessRight` target, no hash identity, no reverse `GenericRelation`s, no annotation or tag attachment — visibility is the dataset's and nothing else's, so the model adds zero permission surface. Structure is cheap and regenerable, membership is precious: no folder operation can touch membership or data, and the folder-delete endpoint moves affected items to the root per row so each placement change is audited. A snapshot's `manifest_hash` covers membership only — organisation is presentation, not identity.

### `Tag`

Hierarchical label. `author`, `name`, `description`, `parent` FK to self (adjacency list, `SET_NULL` on parent delete). The tag taxonomy is global: any authenticated user can browse the tag list and apply tags. Only the tag author or a superuser can edit or delete the tag definition.

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
- `object_hash`, `license_spdx`, `license_url` — dataset responses carry the opaque identifier and the [DatasetMeta](#models) licence pair (`null` when undeclared); the licence fields are settable via the same PATCH.
- Snapshots: `POST /datasets/{id}/snapshots/` (write access) seals the current membership; `GET /datasets/{id}/snapshots/` lists newest-first without manifests; `GET /datasets/snapshots/{hash}/` returns one with its manifest. No update or delete routes exist — see [DatasetSnapshot](#models).
- All `/datasets/{id}/...` routes resolve the dataset `object_hash` or the integer PK — see [Identifiers](#identifiers).
- Folders: `GET /datasets/{id}/folders/` lists the tree as a flat list ordered `(parent, position, name)` (read access mirrors the dataset's, share tokens included); `POST`, `PATCH /{folder_id}/`, and `DELETE /{folder_id}/` under the same path manage it (dataset write access). `POST /datasets/{id}/items/{item_id}/move` places an item in a folder or back at the root (`folder_id: null`). Item listings report each item's `folder_id`.
- Deleting a folder cascades its sub-folders and drops the contained items to the dataset root — membership is never touched. Moving a folder under its own descendant is rejected with 400.

### Grant delegation cap

`POST /datasets/{id}/access/` is the platform's only grant-creation surface on existing objects, and every call routes through [epicurrents/granting.py](../epicurrents/granting.py): a grant created by anyone but the author or a superuser may confer only rights the grantor holds. `can_write` requires holding write; `apply_middleware=False` requires the grantor's own read access to be raw; a conferred `expires_at` must not outlive the grantor's latest active `can_share` expiry; expired share rows qualify for nothing. Share-token rows are refused `can_write` / `can_share` for every grantor, authors included — refusal beats silent downgrade. Granting a user or group that already holds a row on the dataset returns 409 (`AccessRight` enforces one row per target); revoke the existing row first. Revocation is capped from the same module: the author's own row is revocable only by the author or a superuser, because several read paths resolve the author's access through it. Amplification refusals are security-logged (`permission.grant_amplification_refused`, `permission.author_grant_revoke_refused`). Contract tests: [tests/test_grant_capping.py](tests/test_grant_capping.py).

### Tags

| Method | Path | Notes |
|---|---|---|
| `GET` | `/tags/` | List tags. Filter by `parent_id` / `root` / `author`. |
| `POST` | `/tags/` | Create. Optional `parent_id`. |
| `GET` | `/tags/{id}/` | Detail. |
| `PATCH` | `/tags/{id}/` | Update. Requires tag authorship. |
| `DELETE` | `/tags/{id}/` | Hard-delete. Requires tag authorship. |
| `GET` | `/tags/{id}/items/?include_children=true` | List items tagged with this tag, by default including items tagged with any descendant. Set `include_children=false` to exclude descendants. |
| `POST` | `/tags/{id}/items/` | Tag an object. Requires write access to the object. |
| `DELETE` | `/tags/{id}/items/{item_id}/` | Untag. Requires write access on the object **or** authorship of the tag. |

`include_children` uses `_get_tag_subtree_ids` — a single DB query + in-memory BFS to expand the tag's descendant set, so a deeply-nested taxonomy doesn't fan out into many queries.

### Bulk-rename

`POST /collections/{id}/recordings/bulk-rename` assigns sequential `display_name` values to the recordings in a collection, ordered by `added_at`. Payload: `{"prefix": "<prefix>"}` (defaults to `"Recording"`). Each writable recording gets `display_name = "{prefix} {n}"` with `n` starting at 1. Non-writable, FAILED, or soft-deleted recordings are skipped *without* advancing the counter, so the resulting numbers always form a contiguous 1..N sequence across the rows actually renamed. Returns `{"renamed": N, "skipped": M}`.

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

Grants read on `obj` when `obj` is a member of a non-deleted `Dataset` for which the caller (user, group, or supplied share token) holds an active `can_read` `AccessRight`. The returned `ReadAccessTerms.apply_middleware` reflects the matching Dataset right's `apply_middleware` flag, so EDF files served through dataset-inherited reads honour the sharer's anonymisation choice. Write is never inherited.

### `can_read_via_dataset_federated` and `federated_dataset_visible_terms`

The same rule for a federated peer, registered as a pair through `register_federated_read_extension`. A separate implementation is not duplication: the local checker matches an `AccessRight` against a user, their groups or a share token, and a peer has none of those — only a peer row and an opaque remote user id. A wildcard grant (`remote_user_id=""`) covers every user from that peer, an exact one covers a single user, and `apply_middleware` comes from the dataset's grant row exactly as in the local path.

The `visible_terms` half answers for listing endpoints, which resolve access for many rows at once with a batch query rather than a per-object check. It returns terms rather than bare ids because the federated listing also advertises a download size, which depends on whether the bytes are transformed on the way out. Both halves register together because the failure that matters is disagreement: a listing that omits what the object endpoint serves hides shared data, and one that includes what the object endpoint refuses advertises 404s. Disagreement over the *terms* is the same failure wearing a quieter costume — a peer is shown a download size computed one way and handed bytes decided the other — so both halves rank a dataset's grants identically: an exact-user row outranks the peer-wide wildcard, and among rows of equal specificity the de-identifying one wins. That second key is not decoration. A recording in two datasets shared with the same peer produces two wildcard rows with nothing to choose between them, and without it the database's row order decides whether the peer receives the de-identified file or the raw one.

Placement inside a dataset is not carried across federation yet — `DatasetItem.folder` describes a tree on the owning side, while a peer sees a flat list. A recipient expecting a layout (BIDS, say) has to rebuild it from names.

No collection counterpart exists: collections grant nothing on their items, and `TestCollectionRowsGrantNothing` in [tests/test_permissions.py](tests/test_permissions.py) pins that a stale collection-targeted `AccessRight` row stays inert.

## Identifiers

Datasets are addressed by `Dataset.object_hash` — a 32-character random identifier that leaks nothing about creation order or count. Every `/datasets/{id}/...` route resolves either the hash or the integer PK (`_get_active_dataset` mirrors the dual resolution recordings use), the frontend builds its dataset URLs and viewer `?dataset=` links from the hash, and the PK form stays accepted for internal callers and old links. Snapshots are hash-addressed from birth.

Collections and Tags are identified by **integer PK** in URLs (`/collections/42/`, `/tags/3/`). A collection PK reveals nothing about the contained data — items keep their own opaque identifiers — and the tag taxonomy is global and browseable by all authenticated users, so PK leakage is not meaningful for either.

Items inside collections / datasets / tags are still referenced by the contained object's opaque identifier (e.g. recording `content_hash`), not the membership row's PK.

## Settings consumed

None directly — the library app reads no `LIBRARY_*` env vars. It does consume the cross-app `AccessRight` model from `epicurrents`.

## Project plugin extension points

| Hook | How |
|---|---|
| Make a project model a collection / dataset / tag target | Nothing required — the generic-FK membership rows accept any Django model. Add an instance via `POST /collections/{id}/items/` with the model's `content_type` and `object_id`. |
| Register an additional read-permission rule | `register_read_permission_extension(callable)` from your project's `apps.py::ready()`. Your extension is consulted alongside the two library extensions. See [epicurrents/README.md](../epicurrents/README.md#permission-extensions). |
| Add project-specific tag namespacing | The platform has no namespace mechanism for tags today — tag names are a flat global string. If a project needs namespaces, the cleanest path is a prefix convention (`epicurrents.<project>.<concept>`, mirroring the `Code.standard` pattern) enforced at the project's API layer. |

## Tests

```bash
pytest library/tests/
```

The permission tests live in [tests/test_permissions.py](tests/test_permissions.py) and cover `can_read_via_dataset`, the author-only collection gate, and the inertness of stale collection-targeted rows. API tests are in [tests/test_api.py](tests/test_api.py).

## Gotchas

- **Per-item access filtering is N+1.** [api/v1/ninja.py](api/v1/ninja.py) `_filter_readable` / `_user_can_read_item` check read access for each item individually when listing collection or tag contents. This is fine when the collection or tag scope is bounded and most items are readable. It becomes expensive if a large tag contains many items the caller cannot access — the iterator has to scan past every unreadable item to fill one page. **If a global tag browser is ever added** (listing all items with a tag across all users, or any view where the caller is expected to have access to only a small fraction), switch to the batched check: one `AccessRight.objects.filter(content_type__in=..., object_id__in=..., can_read=True)` over the full page plus a separate `DatasetItem` + `AccessRight` batch query. That drops per-page query count from O(items) to O(distinct content types).
- **Extensions may return a plain `bool`.** The extension protocol normalises `True` to `ReadAccessTerms(granted=True, apply_middleware=False)`, so bool-returning project extensions work. Return `ReadAccessTerms` when you need to propagate middleware behaviour, as `can_read_via_dataset` does.
- **Don't extend `register_read_permission_extension` lightly.** Each new extension is consulted on every read check that doesn't hit a direct `AccessRight`. The existing extension is scoped to single reverse-lookup queries; an extension that triggers heavy work per call will degrade every authorisation path that reaches it. Profile before registering.
- **The two `CollectionItem` uniqueness constraints test independently.** When adding tests for the constraints, exercise each one separately — there are two different failure paths (409 from "already in this collection" vs 409 from "already in another collection") and the API caller may want to distinguish them.
