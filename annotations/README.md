# annotations

A generic annotation system: any platform object can carry any number of annotations created by any user with read access to the object. Four concrete annotation types (`Annotation`, `Event`, `Interruption`, `Label`) attach via a generic FK pair, and a fifth model (`Code`) attaches standardised classification codes to the codeable subset (`Event`, `Interruption`, `Label`).

The annotation layer is intentionally lower-bar than write access: a user who can read a recording can annotate it. Editing or deleting an annotation requires authorship of that annotation, not write access to the recording itself.

## The four annotation types

All four extend the abstract `AnnotationBase`, which provides the generic FK target, identity (`object_hash`), integrity (`content_hash`), authorship, and timestamps. Pick the type by intent:

| Type | Purpose | Type-specific fields |
|---|---|---|
| `Annotation` | Free-form structured note bundling arbitrary JSON content. The "everything else" type — use when none of the others fit. | `name`, `content` (JSONField) |
| `Event` | Time-stamped event at a specific position in a signal. Optional duration (point-in-time when null). | `name`, `timestamp`, `duration?`, `value?`, `event_class` (project-write-only: absent from the core `EventIn` / `EventPatch` schemas, written by project endpoints and read by export) |
| `Interruption` | Signal interruption / gap in a recording. | `timestamp`, `duration` |
| `Label` | Named label with optional structured value. Distinct from `Event` in that it's not necessarily time-positioned. | `name`, `value?` |

`Event`, `Interruption`, and `Label` all support `Code` attachments (`codes = GenericRelation(Code)`). `Annotation` does not.

## Identity and integrity

### `object_hash`

32-character alphanumeric string supplied by the caller, unique per `(target_content_type, target_object_id, concrete_type)`. It's the *public* identifier — annotation CRUD endpoints use `/{object_hash}`, never the integer PK. Two callers can use the same hash against different target objects without colliding; the same caller can't use the same hash twice against the same target with the same annotation type.

Convention: stored uppercase. The `save()` method uppercases the value before writing.

For server-generated annotations created during recording ingest, [recordings/tasks.py](../recordings/tasks.py) `_annotation_hash(recording_pk, suffix)` derives a deterministic hash from the recording PK + a per-annotation suffix (`"original-annotations"`, `"interruption:<position>"`, `"source-events"`). Keyed on the PK so re-uploading the same file produces a fresh set of hashes against the new Recording row.

### `content_hash`

SHA-256 hex of a sorted JSON payload covering author, target, `object_hash`, the type-specific fields, *and* any attached codes. Recomputed on every `save()` and (for codeable types) after every `Code` mutation via a `post_save` / `post_delete` signal — the parent's hash stays in sync with its codes without the caller doing anything.

Two consequences:

- **The hash is meaningful.** Two annotations with the same `content_hash` are bit-for-bit equivalent. Use it for dedup checks and integrity verification.
- **Adding a field to `AnnotationBase` invalidates every stored hash.** Don't add fields there for project-specific concerns; use `Code` (see [Project-specific labelling via `Code`](#project-specific-labelling-via-code)).

## API

Mounted at `/annotations/api/v1/`. The four annotation types each get the same CRUD shape under their own router, plus a `/codes/` router for `Code` operations and a `/content-types` helper. Full request/response detail in [api/v1/ninja.py](api/v1/ninja.py).

| Type | List | Create | Update | Delete | Caller's own |
|---|---|---|---|---|---|
| Annotation | `GET /annotations/` | `POST /annotations/` | `PATCH /annotations/{hash}` | `DELETE /annotations/{hash}` | `GET /annotations/mine` |
| Event | `GET /events/` | `POST /events/` | `PATCH /events/{hash}` | `DELETE /events/{hash}` | `GET /events/mine` |
| Interruption | `GET /interruptions/` | `POST /interruptions/` | `PATCH /interruptions/{hash}` | `DELETE /interruptions/{hash}` | `GET /interruptions/mine` |
| Label | `GET /labels/` | `POST /labels/` | `PATCH /labels/{hash}` | `DELETE /labels/{hash}` | `GET /labels/mine` |
| Code | (via parent's `codes` field) | `POST /codes/` | `PATCH /codes/{id}` | `DELETE /codes/{id}` | — |

List endpoints accept `target_content_type_id` + `target_object_id` (required) and optional `author_id` / `limit` / `offset`. `/<type>/mine` lists the caller's own across all targets.

`GET /content-types` returns the available `(app_label, model, id)` triples for use as `target_content_type_id`. Filterable by `app_label` / `model`.

`GET /export` downloads events and labels in bulk, `GET /export/annotators` maps the exported annotator ids back to identities — see [bulk export](#bulk-export).

`GET /health` returns `{"status": "ok"}`.

## Bulk export

`GET /export` answers the question the per-target list endpoints cannot: "give me the rows across targets, attributable per annotator". It backs the **Export annotations** page in the SPA and serves the research / QA workflow where rater output has to leave the platform for pandas or R. Implementation in [export.py](export.py).

| Parameter | Meaning |
|---|---|
| `types` | `events`, `labels`, or `events,labels` (default). |
| `format` | `json` (default) or `csv`. |
| `recording` | Recording `content_hash`. Repeat the parameter for several. |
| `dataset_id` | Restrict to the recordings in one Dataset. |
| `annotator_id` | Annotator user id. Repeat the parameter for several. Staff only. |
| `since` / `until` | Inclusive bounds on `created_at`. |
| `version_id` | Restrict to annotations bound to one signal version. |

**Access follows the staff tier.** A staff or superuser caller exports across all annotators; every other caller is restricted to their own rows. The restriction is applied to the queryset rather than checked afterwards, so no filter combination widens it, and a non-staff caller naming anyone but themselves in `annotator_id` gets a 403 plus a `permission.denied` security-log entry. Passing `recording` and `dataset_id` together intersects them.

### Export extensions

A project plugin extends the exported rows rather than overriding the endpoint, so the access tiers, target hiding, and de-identification above keep applying to project data unchanged. `annotations.export.register_export_extension(target_model_label, columns=..., resolver=...)`, called from the project's `AppConfig.ready()`, complements every row whose target is an instance of `target_model_label` (`"app_label.model"`, the same string the export emits as `target_type`) with the extension's columns.

The resolver is called once per export with `caller` and the distinct, access-filtered target instances, and returns `{str(pk): {column: value}}`; every row on a target inherits its values, and omitted targets or columns fall back to `None`. The caller is passed so a resolver can apply field-level gates of its own. CSV headers always carry every registered column — empty for rows the extension does not cover — so the file shape depends on the deployment, not on which targets matched the filters, and the metadata header lists the active deployment's additions under `extension_columns`. Registered columns are additive and do not bump `format_version`; a column colliding with a base column raises at registration. The shape is a project that annotates derived objects and wants their identifiers as export columns.

### Annotator roster

`GET /export/annotators` lists every user who has authored an Event or a Label as `{id, username, name, events, labels}`, sorted by username. Staff only; non-staff callers get a 403 plus a `permission.denied` security-log entry. It is the in-platform counterpart of the export's `author_id` values: the Export annotations page renders it both as the annotator filter and as the mapping an exporter keeps for resolving ids in an exported file back to people. An erased account leaves the roster together with its annotations (the author FK cascades), so ids in previously exported files become permanently unresolvable — the erasure doing its job.

**JSON is the lossless format.** `Event.value`, `Label.value`, and `Code.meta` are `JSONField`s whose shape varies per annotation, so a fixed column set cannot hold them; the CSV path serialises each into one cell instead. CSV also carries one type per file — events and labels have different columns — so `format=csv` with both types is a 422 rather than a silently truncated file.

Both formats open with a metadata header: the exporter's user id, when, which filters were applied, and the roster of annotator ids whose rows are present with a per-type count for each. In JSON it is a `metadata` object; in CSV it is a block of leading `#` comment lines, which a reader has to be told to skip (`pandas.read_csv(path, comment='#')`). `format_version` in the header is bumped when the field set changes in a way a downstream parser could trip over; version 2 replaced `author_username` with `author_id` and stripped names and usernames from the header.

### What the rows deliberately omit

`created_at` and `modified_at` are not exported. The de-identification rule in AGENTS.md requires annotation-type responses to omit them, and a bulk export is the last surface that should carry fields the narrower per-target endpoints withhold. Row order still encodes the sequence — the queryset sorts on `created_at` before serialising — so a consumer keeps the ordering without the absolute times. A deployment that genuinely needs timestamps for its analysis should add an entry to [.review/exemptions/phi-exposure.md](../.review/exemptions/phi-exposure.md) with a justification, rather than adding the columns quietly.

No annotator identity is exported. Rows carry `author_id` and the metadata roster carries the same ids with counts; names and usernames resolve through the [annotator roster](#bulk-export) endpoint, inside the platform. `exported_by` is an id for the same reason — the audit trail, not the file, records the actor with full attribution.

Targets are identified by the most opaque public identifier the target model offers: `content_hash` for a Recording, `object_hash` for an annotation or a project-plugin model that publishes one, and the primary key only as a last resort for targets that publish neither. FAILED-hidden recordings are dropped, reusing `_failed_hidden_for_caller` from the recordings API rather than restating the rule, and soft-deleted recordings are dropped alongside them so a trashed recording cannot return through an export. `Recording.original_name` never appears; `target_label` carries the grantee-visible display name instead.

### Audit trail

Every export writes an `annotations.export` Activity row recording the format, types, applied filters, returned counts, and `annotator_ids` — the user ids whose rows left the system. Every filter is either non-personal or an opaque user id, so the row carries no username or name: the audit trail is permanent, and the row targets no user, so `erase_subject` can never select it to scrub. A username written there would outlive the account it names. Roster reads write an `annotations.annotator.list` row with the annotator count only, for the same reason.

**Exports carry no personal data; the roster endpoint does, and stays inside.** Anything written into an exported file leaves the platform's erasure reach the moment it is saved — an erasure request under GDPR Art. 17 covers the audit trail and the database, not copies an operator has already distributed. Identifying annotators by bare user id keeps rater attribution intact while keeping names and usernames behind authentication, where erasure still works. Exported files remain PHI-adjacent (annotation text itself can carry anything a rater typed); govern them with the same retention policy as the recordings they describe, and treat any saved copy of the roster as personal data under that policy too.

## Permission model

Three different checks, applied at the right point in the lifecycle:

| Operation | Function | Required |
|---|---|---|
| List annotations for a target | `can_read_object(user, target)` | Read access to the **target** (e.g. the recording). |
| Create an annotation | `can_annotate_object(user, target, share_token=..., annotator=...)` | Read access to the **target**, **or** target authorship. Lower bar than write — see below. |
| Update an annotation | `can_modify_object(user, annotation)` | Authorship of the **annotation**. Read access to the target is not enough. |
| Delete an annotation | `can_modify_object(user, annotation)` | Same as update. |
| Create a `Code` | `can_modify_object(user, parent_annotation)` | Authorship of the **parent annotation**. |

The lower bar for `can_annotate_object` exists because annotations are inherently personal: a user reading a shared recording should be able to attach their own observations without needing write access to the recording itself. Update / delete is then gated by authorship of the annotation, so the original annotator stays in control of their own work.

See [epicurrents/README.md](../epicurrents/README.md#permission-functions) for the full permission function table.

### `annotator` field on annotation `*In` schemas

All four create-payload schemas (`AnnotationIn`, `EventIn`, `InterruptionIn`, `LabelIn`) carry an optional `annotator: str | None = None`. Today it is **ignored for authenticated session callers** — the annotation is attributed to `request.user`. It becomes required when the caller authenticates via a `share_token`, because the platform needs *some* identifier to attribute the annotation to (the token holder is anonymous to the platform).

`can_annotate_object` enforces this: if `share_token` is supplied without a non-empty `annotator`, the function returns `False` and the API returns `400` from `ensure_can_annotate_object`. See [epicurrents/permissions.py](../epicurrents/permissions.py).

## `Code` — standardised classification

`Code` attaches a standardised classification code (ICD-10, SNOMED, LOINC, or any custom standard) to an `Event`, `Interruption`, or `Label`. Three fields:

| Field | Notes |
|---|---|
| `standard` | `CharField(64)`. Identifier for the coding system. External standards use their own registry identifier (`"hed"`, `"icd10"`, `"snomed"`, `"loinc"`) — the string *is* the meaningful public identifier there. Project-specific codes use `"epicurrents.<project>.<concept>"` (see below). |
| `value` | `TextField`. The code value within that system (e.g. `"G40.9"` for ICD-10 epilepsy, `"correct"` / `"incorrect"` for a project mark, a grouped HED string running several hundred characters). Not indexed, so width carries no index consequence. |
| `meta` | `JSONField(null=True, blank=True)`. Arbitrary structured data accompanying the code (e.g. `{"score": 8.5}`). The API schemas type it as the same JSON union the annotation `value` fields use, so objects round-trip; on PATCH, explicit `null` clears the field while an absent key leaves it unchanged. Keep flat; `meta` is not indexed and shouldn't be used as a hot-path filter target. |

Generic FK pair (`content_type`, `object_id`) targets the parent annotation. A given annotation can have multiple codes from the same or different standards.

### Project-specific labelling via `Code`

Use `Code` rather than adding fields to `AnnotationBase` when a project needs to attach semantic labels or scores to annotations. `AnnotationBase` is abstract and its fields participate in `content_hash` — adding a field there requires migrations on all four concrete types and invalidates stored hashes.

**Naming convention:** `standard = "epicurrents.<project>.<concept>"`.

| Use | `standard` | `value` examples | `meta` |
|---|---|---|---|
| Instructor evaluation in a teaching project `course` | `"epicurrents.course.mark"` | `"correct"`, `"incorrect"`, `"reference"` | `{"score": 8.5}` |

**Wrap project-specific `Code` interaction in dedicated API endpoints** rather than exposing the `standard` string directly to callers. The endpoint validates allowed `value`s and handles `update_or_create` keyed on `(content_type, object_id, standard)` to prevent duplicate rows. Callers send the domain payload (`{mark: "correct", score: 8.5}`); the `standard` string is an implementation detail they never see.

### Vocabulary registry

[vocabularies.py](vocabularies.py) holds a registry mapping a `standard` identifier to a validator callable, registered from the owning `AppConfig.ready()`:

```python
from annotations.vocabularies import register_vocabulary

register_vocabulary("hed", label="HED", version="8.3.0", validator=my_validator)
```

The validator receives `(value, meta)` and raises `ValueError` naming the offending term; `create_code` and `update_code` translate that into a 422 before opening their transaction. Update validation runs against the combined prospective row state, so patching only `meta` is still checked against the row's `standard` and `value`. A callable rather than a term list, because vocabulary rules are not always membership — HED has value placeholders and group structure, ICD-10 has check characters.

Core ships the mechanism with **zero vocabularies**; the contract is proven by a vocabulary registered inside the test suite ([tests/test_code_vocabulary.py](tests/test_code_vocabulary.py)). Two enforcement modes:

- Default: an unregistered `standard` is accepted unvalidated — existing rows and project-local codes keep working.
- `ANNOTATION_CODE_STRICT_VOCABULARY = True` (a project-settings decision, not `common`): an unregistered `standard` is rejected with 422.

Enforcement is at the API layer, not in `Code.save()`, and that is a choice: the untrusted writer is a user reaching the API, while ingest, management commands, and fixtures are the platform's own code. The control supports the claim "users cannot write non-conforming codes through the API" — not "this deployment's database contains only validated terms", which would need model-level enforcement. The reasoning lives in the module docstring of [vocabularies.py](vocabularies.py).

### Code mutations and `content_hash`

Adding, updating, or removing a `Code` fires `update_parent_hash_on_code_change` in [signals.py](signals.py), which calls `recompute_content_hash` on the parent annotation. This is intentional — a marked annotation is semantically different from an unmarked one and the audit trail should record the change.

If a project ever uses `Code` for labels that should *not* affect the parent's `content_hash` (rare and discouraged), the workaround is `Type.objects.filter(pk=...).update(content_hash=...)` after the Code is created to restore the previous hash. Don't do this casually — it breaks the "content_hash uniquely identifies semantic content" guarantee.

## Signals

[signals.py](signals.py) wires one `post_*` handler.

### `update_parent_hash_on_code_change`

`post_save` and `post_delete` on `Code` recompute the parent annotation's `content_hash`. Uses `Type.objects.filter(pk=...).update(...)` to avoid re-triggering the parent's own `pre_save` / `post_save` signals (which would loop).

**Audit-trail gap.** Because `recompute_content_hash` uses `.update()` instead of `.save()`, the activity-audit signals don't fire — so the resulting `content_hash` change on the parent annotation produces no `ObjectChangeLog` row. The triggering `Code` mutation IS logged (it's a normal save/delete), so the chain of events can still be reconstructed by reading the Code audit entry. But a reader querying "what happened to this Event/Interruption/Label row?" by `content_type` will not see the hash-change events.

This is the same class of gap as bulk DML on tracked models — covered by the "Activity — close the bulk-operations audit-trail gap" entry in [ROADMAP.md](../ROADMAP.md). The fix isn't applied at this site directly because it would couple `annotations` to `activity`; the broader fix (queryset extension or opt-in `AUDITED_MODELS` tracking) will catch this case without per-site changes.

### `Code` uniqueness

`Code` declares a `UniqueConstraint` on `(content_type, object_id, standard)`, enforcing the contract that the canonical write idiom `update_or_create(content_type=..., object_id=..., standard=..., defaults={...})` relies on. Without the constraint, two concurrent requests setting the same `standard` on the same target could both decide to INSERT and leave duplicate rows; with it, the second hits a constraint violation and the application can retry with the row that won.

### Orphan-on-delete handling

Annotation rows do not orphan when their target is deleted: every annotatable target model (`Recording`, `Collection`, `Dataset`) declares reverse `GenericRelation` fields for `Annotation`, `Event`, `Interruption`, and `Label`, so Django's `Collector` cascades through them in the same transaction as the target delete. This is the canonical pattern documented as a cross-cutting rule in [AGENTS.md → GenericFK target cascade pattern](../AGENTS.md#genericfk-target-cascade-pattern). Project-plugin models that want to be annotatable must declare the same fields. Soft-delete (setting `deleted_at`) does not trigger the cascade — only an actual row removal does.

The previous `cascade_delete_annotations_for_target_object` `post_delete` signal handler was removed in favour of this approach. The two differ in three ways: the cascade fires `pre_delete` for each row (audit trail captures), the cleanup runs in the same transaction as the parent delete (no race window), and the design is uniform with how every other reference-row type (`AccessRight`, `CollectionItem`, …) is handled.

## Server-generated annotations

[recordings/tasks.py](../recordings/tasks.py) writes annotations during recording ingest, attributed to the system user (`get_system_user()` in [epicurrents/system_user.py](../epicurrents/system_user.py)). The system user is permanently inactive (`is_active=False`) so it can't authenticate.

| Source | Type | `name` | `object_hash` suffix |
|---|---|---|---|
| EDF+ TAL text events parsed from header | `Annotation` | `"Original annotations"` | `"original-annotations"` |
| EDF+ data record gaps | `Interruption` (one per gap) | — | `"interruption:<data_pos>"` |
| Converter sidecar (`.e` → EDF today; generic across future converters) | `Annotation` | `"Source events"` | `"source-events"` |

All three use `_annotation_hash(recording.pk, suffix)` keyed on the recording PK. See [recordings/README.md](../recordings/README.md) for the ingest pipeline that produces them.

## Settings consumed

| Setting | Default | Effect |
|---|---|---|
| `ANNOTATION_CODE_STRICT_VOCABULARY` | `False` | When `True`, `Code` API writes with an unregistered `standard` are rejected with 422 — see [Vocabulary registry](#code--standardised-classification). A project-settings decision, not `common`. |

## Project plugin extension points

| Hook | How |
|---|---|
| Attach project-specific labels/scores to annotations | Use `Code` with `standard = "epicurrents.<project>.<concept>"`. Wrap the interaction in a project API endpoint that hides the `standard` string. |
| Register a coding-standard vocabulary | `register_vocabulary(standard, label=..., validator=..., version=...)` from [vocabularies.py](vocabularies.py) in the owning `AppConfig.ready()`. |
| Annotate project models | Project models can be annotation targets without any registration — the generic FK accepts any `(content_type, object_id)` pair. Just pass the model's content type ID to the create endpoint. |
| Make a project model annotatable through the viewer UI | Frontend concern — see the viewer's `DatabaseAPIConnector` setup. |

## Tests

```bash
pytest annotations/tests/
```

## Gotchas

- **`object_hash` is caller-supplied, not generated.** The platform validates the format (32 alphanumeric chars) but doesn't generate it for you. Tests that create multiple annotations on the same target must use distinct hash values, or the unique constraint fires.
- **Re-uploading a recording produces fresh annotation hashes.** `_annotation_hash` is keyed on the recording PK, and re-upload creates a new Recording row with a new PK. The annotations from the previous upload remain attached to the previous PK. This is the intended behaviour (file identity is per-row, not per-content), but worth knowing if you're chasing "why are there two sets of annotations on what looks like the same file".
- **`AnnotationBase` is abstract — don't add fields there.** Every field on `AnnotationBase` participates in `content_hash` via subclass `_hash_fields()`, and adding a field requires a migration on all four concrete tables plus invalidates every stored hash. Use `Code` for project-specific labelling instead. The full rationale is in [Project-specific labelling via `Code`](#project-specific-labelling-via-code).
- **`list_annotations` requires the recording author to have an explicit `AccessRight`.** `can_read_object` doesn't auto-grant on authorship for the read-list path — it goes through the standard `AccessRight` lookup. In tests, this means `baker.make(Recording, author=user)` is not enough on its own; create an `AccessRight` row for the author too if the test exercises a list endpoint. (Tests that target a single annotation directly aren't affected.)
- **`Annotation` is the only type without `Code` support.** If a project needs to attach a code to a bundle-style annotation, the workaround is to use one `Event` (with `timestamp=0` if positional context doesn't matter) as the code carrier.
- **All API endpoints declare `auth=None`** in their decorators. Authentication is handled inline by `_require_auth(request)` (or `can_annotate_object` for share-token paths). Don't change this without checking every endpoint — the inline check is what handles the dual session-cookie / share-token flow.
- **Share-token paths require an `annotator` string.** Create-payloads accept `annotator: str | None = None`. The field is ignored for authenticated sessions but required when the caller has only a `share_token`. Returns 400 if missing in that case.
