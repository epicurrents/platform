# activity

Per-request audit log and field-level change tracking for the entire platform. Every API call writes an `Activity` row; every create / modify / delete on a tracked model writes an `ObjectChangeLog` entry with the before-state and a field-level diff. Both feed the rollback API, which lets authorised users undo individual changes or atomic batches.

## How it's wired

```
HTTP request                               Celery task / management command
    │                                                │
    ▼                                                ▼
ApiActivityLoggingMiddleware                with_system_activity(...)
   ← epicurrents/middleware.py                ← activity/system_activity.py
    • create Activity (interface=api)         • create Activity (interface=celery|command)
    • flip is_audited_context flag            • flip is_audited_context flag
    • store user + Activity in ContextVars    • store actor + Activity in ContextVars
    │                                                │
    └────────────────────┬───────────────────────────┘
                         ▼
              view / task body runs
                  │   model.save() / model.delete()
                  │       │
                  │       ▼
                  │   activity/signals.py    ← pre_save / post_save / pre_delete
                  │       • read current Activity from ContextVar
                  │       • write ObjectChangeLog row (before_state, diff, after_hash)
                         ▼
              entry-point layer cleans up
                  • API: fill status_code + target_object_id
                  • system_activity: nothing to fill
                  • reset ContextVars
```

Two things gate the signal handlers:

1. **`is_audited_context()`** — `True` only inside an audited scope opened by one of the entry points above. Model writes from migrations, ad-hoc shell sessions, and Celery tasks / commands that haven't opened a scope do **not** auto-log. Use the manual helpers in [audit.py](audit.py) (`record_create_change`, `record_modify_change`, `record_delete_change`) when you need audit coverage outside any scope, or — preferred — open a `with_system_activity` scope so signal writes attribute automatically.
2. **`is_change_logging_suppressed()`** — `True` when a code block has called `set_change_logging_suppressed(True)`. Used internally by `_restore_object_state` so rollback writes don't trigger their own MODIFY entries. Project code can use it for bulk operations that shouldn't pollute the audit trail.

## Models

### `Activity`

One row per auditable interaction. Lives in `activity_activity`. The row is created by one of two entry points: `ApiActivityLoggingMiddleware` for HTTP requests, `activity.system_activity.with_system_activity` for Celery tasks and management commands.

| Field | Notes |
|---|---|
| `actor` | FK to user; `null` for anonymous requests and for system-initiated rows. `SET_NULL` on user delete. |
| `interface` | `"api"` / `"celery"` / `"command"`. Indexed. Tells audit views which caller-type produced the row. Defaults to `"api"` so existing migrated rows interpret correctly without backfill. |
| `verb` | `<app>.<resource>.<action>` taxonomy string. The middleware seeds it with the lowercased HTTP method; endpoints override via `log_activity`. `with_system_activity` callers pass it explicitly. |
| `method`, `path` | HTTP method and request path. Empty strings for `celery` / `command` rows. |
| `status_code` | Filled in after the view returns. `NULL` for `celery` / `command` rows. |
| `target_content_type`, `target_object_id`, `target_identifier` | Optional pointer to the object the request operated on. The middleware sets `target_object_id` from URL kwargs (`pk`, `id`, or `*_id`) on the way out, and only when the extraction produced a value — endpoints that resolve their target from the request body set the fields themselves via `log_activity(..., target=...)` and the middleware no longer clobbers the explicit value. `with_system_activity` callers pass `target=` directly. |
| `project` | Filled from `EPICURRENTS_PROJECT` when the path contains `/project/api/`. Empty for core API requests. |
| `metadata` | `JSONField` for endpoint-specific context. **No enforced schema** — callers attach whatever shape is useful (current uses: `{"source_change_id": ...}` on rollback Activity rows). Convention: namespace ad-hoc keys to avoid future collisions (e.g. `{"federation": {"peer_id": ...}}` rather than top-level `peer_id`); never rely on a particular key being present when reading, since older rows may predate it. |
| `created_at` | `auto_now_add`. Set by the database on insert; cannot be supplied by the caller. Contrasts with `ObjectChangeLog.created_at`, which is caller-supplied — see that field's note for the reason. |
| `archived_at` | Set by `archive_old_activity`; `null` while the row is active. |

Two managers:

- `Activity.objects` — default; excludes archived rows.
- `Activity.including_archived` — full history; use for long-range audit queries.

### `ObjectChangeLog`

One row per tracked create / modify / delete / rollback. Lives in `activity_objectchangelog`. **Never archived, never deleted** — this is the permanent audit trail used by rollback.

| Field | Notes |
|---|---|
| `activity` | FK to the parent `Activity`. `SET_NULL` so deleting an old Activity doesn't cascade to its change entries (although in practice we don't delete Activity rows). |
| `content_type`, `object_id`, `content_object` | Generic FK to the affected object. `content_type` cascades on delete (a removed model means the change log can no longer be replayed against it). |
| `action` | `"create"` / `"modify"` / `"delete"` / `"rollback"`. |
| `project` | Mirror of `Activity.project`; explicit so post-archive queries can still filter by project without joining. |
| `performed_by` | FK to user; `SET_NULL` on user delete. |
| `before_state` | `JSONField`. For CREATE entries this stores the initial values (so rollback-of-rollback can recreate). For MODIFY/DELETE/ROLLBACK it stores the state at the start of the change. |
| `changes` | `JSONField` of `{field: {"from": ..., "to": ...}}`. Populated only for MODIFY entries. |
| `after_hash` | 32-char hex prefix computed under `hash_algorithm` over the payload (`after_state` + identity metadata + (for v3) `prev_hash`). See [Threat model](#threat-model) for the per-version guarantee. |
| `hash_algorithm` | `"v1"` (legacy sha256), `"v2"` (HMAC-sha256), `"v3"` (HMAC + chain). Set at write time from `current_write_hash_version()`; verification dispatches on this field so each row reads under the algorithm it was written with. |
| `hash_key_version` | Integer key version naming which entry in `settings.ACTIVITY_HASH_KEYS` was used. `NULL` for v1; required for v2/v3. |
| `prev_hash` | For v3 rows: the previous row's `after_hash` in the same per-content_type shard, or the per-shard genesis sentinel for the first row. Empty string for non-v3 rows so they're skipped by chain verification. |
| `sequence_no` | For v3 rows: monotonic counter scoped to `content_type`. `NULL` for non-v3 rows. The composite index `(content_type, sequence_no)` keeps chain walks linear over the shard. |
| `extra_payload` | `JSONField` carrying caller-supplied derived-state digests (see [Derived-row digests](#derived-row-digests)). Empty dict for signal-driven rows. Mixed into `after_hash` so naive tampering with the column breaks chain verification; the live derived state is checked separately by `verify_derived_state`. |
| `created_at` | Set explicitly by the writer (not `auto_now_add`). Required because the same timestamp value is fed into `compute_audit_hash` — if Django generated it server-side at insert time, the hash on the row would never match a recompute. Also lets reconstructed-history imports preserve original timestamps. |

## Action types

| Action | When | `before_state` | `changes` | Rollback effect |
|---|---|---|---|---|
| `create` | After `post_save` with `created=True` | Snapshot of the new object | `null` | Deletes the created object — soft (sets `deleted_at`, preserving the model's trash / retention pipeline) when the model supports it, hard otherwise. The deletion is itself recorded as `rollback` so it can be undone. |
| `modify` | After `post_save` with `created=False`, when fields actually changed | Pre-save snapshot | Field-level diff | Restores `before_state`. |
| `delete` | Before `pre_delete` fires | Final snapshot of the doomed object | `null` | Re-creates the object using `before_state`. |
| `rollback` | After any rollback executes | Object state at the start of the rollback | Diff between that and the restored state | Same shape as `modify`, so rollback-of-rollback is well-defined. |
| `erase` | Appended by `erase_subject` after a GDPR Art. 17 scrub | Scrub summary (per-model row counts, reason) — no object state | `null` | None — erasure records refuse rollback. |

The `before_state → changes → after_hash` triple is enough to (a) reconstruct the object at any historical point and (b) detect *accidental* corruption or *naive* tampering with stored audit rows (see [Threat model](#threat-model) for the precise scope).

## Threat model

`after_hash` is computed under a versioned algorithm — the row's `hash_algorithm` field names which version, and `hash_key_version` names the HMAC key for keyed versions. The attacker matrix differs by row algorithm:

| Attacker capability | v1 (sha256) | v2 (HMAC) | v3 (HMAC + chain) |
|---|---|---|---|
| No code, no DB write access | n/a | n/a | n/a |
| DB write access, no awareness of the hash | ✅ Yes — recomputed hash mismatches | ✅ Yes | ✅ Yes |
| DB write access, aware there's a hash but not the algorithm | ✅ Yes — preimage problem | ✅ Yes | ✅ Yes |
| Code-level access (DBA reading [audit.py](audit.py); SQL injection executing Python; leaked Django shell) **without** the HMAC key | ❌ No — attacker re-runs `compute_audit_hash` and writes the matching hash | ✅ Yes — function needs the key | ✅ Yes — function needs the key |
| Code-level access **with** the HMAC key, tampering one row + recomputing that row's hash | ❌ No | ❌ No — single-row check passes | ✅ Yes — the *next* row's `prev_hash` still references the old `after_hash`; the link check at row N+1 catches the tamper |
| Code-level access **with** the HMAC key, willing to rewrite every row from the tampered point to the chain tail | ❌ No | ❌ No | ❌ No — attacker has everything the legitimate writer has |
| Reorder or delete middle rows | ❌ No — each row's hash is independent | ❌ No — same | ✅ Yes — gap in `sequence_no` is reported by `verify_chain`; reorder breaks the prev_hash link |
| Lift a row from another content_type's chain into this one | n/a | n/a | ✅ Yes — the per-shard genesis sentinel encodes `content_type_id`; mismatched sentinel on the first row fails the genesis check |
| Replay an old row's contents with a new timestamp | ❌ No | ❌ No | ❌ No — row validates against its own contents |
| Tamper with derived rows covered by a parent's `extra_payload` digest (e.g. `SignalInfo` rows under a `Recording`'s READY audit row) | ❌ No — `verify_derived_state` recompute would catch it, but the stored digest can be edited freely | ❌ No — same; the column is still mutable | ✅ Yes — editing the column breaks `after_hash`; editing the dependent rows alone is caught by `verify_derived_state` (see [Derived-row digests](#derived-row-digests)) |

**v1 rows** exist for backwards compatibility — they pre-date the keyed algorithm and remain valid under their original (unkeyed) verification path. The platform never writes new v1 rows once a key is configured.

**v2 rows** are forgery-resistant against any attacker without the HMAC key. The key lives only in `settings.ACTIVITY_HASH_KEYS` (env-var-loaded), so DB write + source access alone cannot produce a matching hash without separately compromising the operator's secret store.

**v3 rows** add a per-content_type chain. Each row's `prev_hash` references the previous row's `after_hash`; the link is checked when [`verify_chain`](audit.py) walks the shard. An attacker with the HMAC key can still forge a single row, but the next row's stored `prev_hash` references the *old* `after_hash` — to hide the tamper they must also rewrite every subsequent row's `after_hash`, and a chain-walk that finds zero breaks confirms either no tampering or end-to-end rewriting.

**The key is the only piece of audit-trail integrity that does not live in the repo.** Treat it with the same care as `BORG_PASSPHRASE`: losing it means no past keyed row will verify. `init_env` generates `ACTIVITY_HASH_KEY_V1` on fresh deploys; the `rotate_activity_hash_key` management command rolls forward to a newly-staged version while keeping the old key reachable for past rows.

**Write-path serialisation.** v3 rows acquire a Postgres advisory lock per content_type during write (`pg_advisory_xact_lock(namespace, content_type_id)`), so the read-tail-then-insert sequence is serialised against concurrent writers on the same shard. SQLite is a no-op (whole-DB write serialisation already provides the property). Lock contention is bounded by per-shard write rate; shards do not block each other.

## Subject erasure (GDPR Art. 17)

The audit trail is append-only, but a data subject's erasure request has to reach it: `before_state` / `changes` payloads embed usernames, emails, OIDC subjects, and push endpoints, both where the user was the actor and where the change targeted their account. [erasure.py](erasure.py) (⚠️ load-bearing) resolves the tension between "never mutate audit rows" and "erase on request" with tombstones:

- **Scrub** — each matching row's registered PII fields are replaced by `"<erased>"` in `before_state` and `changes`. Matching is by `object_id` for the user model itself and by the serialized owner FK (`before_state.user_id`) for dependent models.
- **Seal** — `after_hash` is left untouched, so every downstream row's `prev_hash` link keeps verifying and the chain never breaks. Content integrity of the scrubbed payload is instead sealed by `erased_hash`, an HMAC (or SHA-256 fingerprint on unkeyed deployments) over the post-scrub payload bound to the original `after_hash`, stored with its own `erased_hash_algorithm` / `erased_hash_key_version`. `verify_change_hash` branches on `erased_at`: erased rows verify against the seal, so post-erasure tampering is still detectable.
- **Record** — the scrub appends a chained `erase` row on the user shard summarising what was erased (row counts per model, no PII), so the erasure event is itself part of the tamper-evident record.
- **Refuse rollback** — erased rows and erasure records raise on `rollback_change`; their state payload is gone by design.

`Activity` rows targeting the subject get their PII-bearing metadata keys (currently `email_hash`) replaced by the sentinel. Integer FK values referencing the user elsewhere in the trail are retained deliberately: once the account row and its payloads are erased, a bare pk no longer relates to an identifiable person.

Which models and fields participate is a registration, not a hardcode: owning apps call `register_subject_pii(model_label, owner_field=..., pii_fields=...)` from `AppConfig.ready()`. Core registrations live in [user/apps.py](../user/apps.py) (`user.user`, `user.externalidentity`) and [notifications/apps.py](../notifications/apps.py) (`notifications.pushsubscription`). A project plugin whose models store user-linked personal data registers the same way.

A registered model that is later dropped from the schema keeps its registration, with `historical=True`: the audit rows outlive the model and still need scrubbing, and the scrub itself needs only the ContentType row, which survives a model deletion. The flag tells the system check in [checks.py](checks.py) not to expect the model — and to error the other way, when a historical registration's model turns out to still exist. It also forfeits the check's typo protection, so every historical registration needs a test scrubbing synthesized rows end-to-end. The shape is a project that dropped a profile model: the registration stays in its `apps.py` with the flag, and its erasure test pins the scrub.

The surviving ContentType row is load-bearing, which is why `ObjectChangeLog.content_type` is `on_delete=PROTECT`: `manage.py remove_stale_contenttypes` offers to delete exactly that row, and under CASCADE a confirmed run would take the whole shard with it — outside the sanctioned tombstoning path, and silently unhooking the scrub. With PROTECT the command fails loudly on any ContentType the trail still references; leave stale ContentTypes in place.

The operator-facing entry point is the [`erase_user` management command](../user/README.md#account-erasure-gdpr-art-17) in the user app, which handles the account row, owned files, and sessions before calling `erase_subject`. Calling `erase_subject(user_pk)` directly covers the audit trail only.

### Credential masking at write time

Complementing retroactive erasure, `serialize_instance` masks registered credential fields *before* they reach any audit payload: the value is replaced by `"<masked:<digest12>>"`, a truncated SHA-256 of the stored value. Equal secrets mask identically (no phantom diffs), changed secrets produce a visible-but-opaque diff. Registered via `register_masked_fields` from the owning app's `ready()`: the user's `password` hash and the push subscription's `p256dh` / `auth` keys. Rollback skips masked sentinels so restoring an old state never clobbers a live secret with the placeholder string.

Session rows are excluded from audit tracking entirely (`EXCLUDED_MODELS` in [signals.py](signals.py)): auditing them would write `session_key` — a live bearer credential — into the permanent trail on every login.

## Derived-row digests

Some operations bulk-create dependent rows that don't fit the per-row chain pattern — typically because the rows are deterministic functions of an external input (e.g. `SignalInfo` derived from an EDF header) or are too numerous to be worth a chain entry each. The cross-cutting rule in [AGENTS.md](../AGENTS.md#bulk-orm-operations-bypass-the-audit-signal) calls for a single audit row on the *parent* transition that embeds a digest of the dependent rows. The digest rides on `ObjectChangeLog.extra_payload` and is mixed into the row's `after_hash`.

**How writers attach a digest.** Pass `extra_payload={"<key>": "<hex digest>"}` to `record_modify_change` (or `record_create_change` / `record_delete_change`) when computing a state transition:

```python
record_modify_change(
    actor=None,
    obj=recording,
    before_state=before,
    extra_payload={
        SIGNAL_INFO_DIGEST_KEY: compute_signal_info_digest(recording),
    },
)
```

The chain-write helper stores the dict on the row and mixes it into the HMAC payload (for v3) or the v1 / v2 payload, so tampering with the column breaks the row's own `after_hash`.

**How verifiers re-derive.** Apps register a recompute function per `(target_model, key)` pair at `AppConfig.ready` time:

```python
register_derived_state_digester(
    target_model=Recording,
    key=SIGNAL_INFO_DIGEST_KEY,
    digester=compute_signal_info_digest,
)
```

`verify_derived_state(change)` loops over each key in the row's `extra_payload`, looks up the registered digester, runs it against the live target, and compares the result to the stored hex. The result object reports per-key verdicts: `"ok"`, `"mismatch"`, or `"no_digester"` (the row carries a digest under a key no app has registered). The recompute path is on-demand; the future periodic-integrity Celery task will call it on a sliding window.

**Two layers of detection.** The chain catches naive tampering with the stored digest column (recomputed `after_hash` no longer matches). The recompute catches tampering with the dependent rows themselves (live digest differs from stored). To hide a derived-row tamper an attacker would have to recompute the digest, update the audit row's column, recompute its `after_hash` — and *then* rewrite every subsequent row in the same chain shard so the link forward stays consistent.

Canonical example: [recordings/audit_digests.py](../recordings/audit_digests.py) hashes all `SignalInfo` rows attached to a `Recording`; the digest rides on the final READY transition's audit row.

## API

Mounted at `/api/v1/activity/`. All endpoints require an authenticated session.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/changes/` | List recent change entries the caller may roll back. Filters: `action`, `model`, `activity_id`, `limit` (1–200, default 50). The `activity_id` filter returns every row that shares a parent `Activity`, which is how the full set of rows produced by a cascade deletion is discovered. |
| `POST` | `/rollback/{change_id}` | Roll back a single entry. |
| `POST` | `/rollback/bulk` | Roll back a list of entries atomically (all succeed or none do, in a single DB transaction). |

### Who can roll back what — `can_rollback_change()`

The permission check ([audit.py](audit.py) `can_rollback_change`) returns `True` if any of these hold, in order:

1. The caller is a superuser.
2. The caller is the object's author (`can_modify_object`), when the object still exists.
3. The caller (or a group they're in) holds an active `AccessRight` with `can_write=True` on the target object.

**CREATE entries are stricter:** only steps 1 and 2 apply. Rolling back a creation destroys the object, and `can_write` is a collaboration grant, not a deletion grant — a grantee who could roll back the CREATE entry of a shared object would hold a destruction path its owner never delegated. The check fetches the object itself when the caller passes `existing_obj=None`, so listing pre-flights resolve authorship the same way execution does.

Note: there is no stored-`author_id` fallback. If the target object has been deleted and no `AccessRight` row survives the deletion, only a superuser can rollback the DELETE entry. The fallback was removed because it implicitly trusted every tracked model to maintain `author_id` honestly against API write paths — see the ROADMAP item that drove this decision.

The bulk endpoint does a **pre-flight pass**: every ID in `change_ids` is validated for existence and permission *before* any data is touched. A single 404 or 403 aborts the whole batch.

### Worked example — restore a recording from trash

Soft-delete sets `Recording.deleted_at`, which the auto-signal records as a MODIFY. To restore:

```bash
# Find the soft-delete entry:
GET /api/v1/activity/changes/?model=recording&action=modify
# response includes `{"id": 142, "changes": {"deleted_at": {"from": null, "to": "2026-05-12T..."}}, ...}`

# Restore:
POST /api/v1/activity/rollback/142
```

The recording reappears in the active list. The rollback itself is logged as ACTION_ROLLBACK on entry 143, so "I restored this by mistake" is also undoable.

## Management command

### `rollback_change`

Host-side equivalent of the rollback endpoint, useful for incident recovery when the API is offline.

```bash
docker compose run --rm web python manage.py rollback_change <change_id> --user-id <user_pk>
```

The user must satisfy the same permission rules as the API. Errors raise `CommandError`; on success the command prints the restored model label and PK.

## Known limitations

The auto-signal trail is **not** a complete record of every destructive change in the database. Two gaps share the same root cause: the signal handlers in [signals.py](signals.py) only run when (a) the underlying write fires `pre_save` / `post_save` / `pre_delete` and (b) `is_audited_context()` returns `True`.

**Gap A — Bulk DML on tracked models.** Django's `Model.objects.filter(...).update(...)`, `bulk_create`, `bulk_update`, and raw SQL do not fire the relevant signals. Any of these on a tracked model leaves no `ObjectChangeLog` entry, even within an audited scope. The cross-cutting "Bulk ORM operations bypass the audit signal" rule in [AGENTS.md](../AGENTS.md#bulk-orm-operations-bypass-the-audit-signal) covers the closure pattern: load the affected rows, iterate the explicit `record_*_change` helpers inside `transaction.atomic()`, then bulk-write. Known in-tree sites that rely on this deliberately:

- `annotations.models.AnnotationBase.recompute_content_hash` — uses `.update()` so a parent annotation's `content_hash` change from a Code mutation is invisible. See [annotations/README.md](../annotations/README.md#update_parent_hash_on_code_change) for the full context.
- `activity.tasks.archive_old_activity` — batched archive of old Activity rows; self-referential (the audit trail archiving itself).

**Gap B — Non-API code paths.** The `_track_sender` guard requires `is_audited_context()`, which is False by default outside an HTTP request. The closure mechanism is [`activity.system_activity.with_system_activity`](system_activity.py): opening that context manager creates a parent `Activity` row (tagged `interface="celery"` or `"command"`), flips the audit flag for the scope, and lets nested signal-driven writes attribute correctly. See [Audited scope for non-request callers](#audited-scope-for-non-request-callers) below.

**Consequence for rollback.** The bulk rollback path in [api/v1/ninja.py](api/v1/ninja.py) assumes the trail is complete. If a `.update(...)` or an unaudited non-API path removed the row the operator wants to restore, the changelog has no record of the prior state and rollback fails. For now: use the `Manual recording` helpers below or the `Suppression mechanism` (also below) explicitly when these paths matter.

Closure of the in-tree bulk-write sites is tracked under the "Activity — close the bulk-operations audit-trail gap" entry in [ROADMAP.md](../ROADMAP.md).

## Audited scope for non-request callers

Celery tasks and management commands open an audited scope with `with_system_activity`:

```python
from activity.models import Activity
from activity.system_activity import with_system_activity


@shared_task
def process_recording(recording_id: int):
    recording = Recording.objects.get(pk=recording_id)
    with with_system_activity(
        "recordings.process",
        interface=Activity.Interface.CELERY,
        target=recording,
        metadata={"recording_id": recording_id},
    ):
        # ORM writes that go through .save() / .delete() are audited
        # via the existing signal layer. Bulk writes still need explicit
        # record_*_change calls (see Manual recording below).
        ...
```

The scope creates one parent `Activity` row whose `interface` distinguishes the caller (`celery` for tasks, `command` for management commands). Verbs follow the same `<app>.<resource>.<action>` taxonomy as HTTP endpoints. `actor` is `None` unless a management command knows a triggering user (e.g. a `--user` flag).

Skip the scope when the operation makes no model writes (file-system housekeeping, log shipping). Apply it to all new Celery tasks and management commands that touch user-owned data.

## Endpoint annotation — overriding the middleware default

`ApiActivityLoggingMiddleware` creates the request's `Activity` row with a default verb (lowercased HTTP method, e.g. `"get"`) and no semantic target. Endpoints override those defaults with the operation-specific verb / target / metadata using the **`log_activity`** helper in [audit.py](audit.py):

```python
from activity.audit import log_activity


@api.post("/subscribe")
def subscribe(request, payload: SubscribeIn):
    subscription, created = PushSubscription.objects.update_or_create(...)
    log_activity(
        verb="notifications.subscription.create",
        target=subscription,
        metadata={"subscription_id": subscription.pk, "upserted": not created},
    )
    return {"status": "ok"}
```

`log_activity(verb, *, target=None, metadata=None)` resolves the request's `Activity` via the request-context ContextVar, sets the fields, and persists with a scoped `update_fields`. Key behaviours:

- **No active Activity** → silent no-op. Safe to call from code paths that may run outside an API request (signal handlers, helpers reused by Celery tasks).
- **`target`** → optional model instance. When passed, `target_content_type` is set from the instance's content type and `target_object_id` from its `pk`. Listing endpoints and bulk actions without a single target pass `None`.
- **`metadata`** → merged into the existing `metadata` dict on the row; existing keys are preserved unless the new dict overrides them.
- **`save(update_fields=...)`** is scoped to fields the helper actually mutated, so concurrent writes by the middleware (most importantly the exit-time `status_code`) survive.

Which verb to use is settled by the [verb taxonomy](#verb-taxonomy) below; the per-app tables of verbs already in use are in the [verb registry](#verb-registry) after it.

## Verb taxonomy

The [verb registry](#verb-registry) says which verbs exist. This says which one to reach for when adding an endpoint, and which distinctions the platform already draws that are easy to get wrong.

### Shape

A verb is dotted segments running general to specific, starting with the Django app label (or the project / plugin name for project verbs):

```
library.collection.access.grant
│       │          │      └── the operation
│       │          └───────── narrowed scope
│       └──────────────────── the resource
└──────────────────────────── the app
```

Where an operation is a variant of one that already exists, the variant is appended to the operation rather than replacing it — `recordings.read.slice` beside `recordings.read`, `activity.rollback.bulk` beside `activity.rollback`, `user.password.reset.confirm` as a step of a reset. That holds for a variant of any kind: a range of the same object (`read.slice`) and an alternate representation of it (a study's viewer manifest is a `read` variant, not a fourth thing) append the same way. A representation that replaces the base action instead disappears from any query for reads of that resource. That is also why a middle segment is sometimes a noun and sometimes a verb: the segments narrow, and only the whole string names the operation.

The last segment names the operation, so a base action goes there whenever one describes it. Promoting a resource noun into that slot hides the operation — a verb ending in `status` where the endpoint returns one identified job is a `read`, and calling it anything else costs the trail the ability to answer "every read of this resource" in one query. The same word can serve as a resource elsewhere (a project's `status.list` verb lists status rows); what matters is that the final segment says what was done.

An `<app>.<action>` verb with no middle segment is for an operation that acts on the app rather than on one resource in it — `recordings.upload`, `library.purge`.

### The base actions

Five actions cover just over half the registry, and reaching for one of them is almost always right:

| Action | Use when |
|---|---|
| `create` | A new row comes into existence. |
| `read` | One identified object is returned. |
| `list` | A collection is returned, whatever the filters. |
| `update` | An existing row's fields change. |
| `delete` | A row is removed with no recovery path. |

Then the sets that carry a distinction worth keeping:

| Actions | Domain |
|---|---|
| `trash` / `restore` / `purge` | The soft-delete lifecycle. |
| `add` / `remove` / `move` | Membership of a container, not existence. |
| `grant` / `revoke` | Access rights on an object. |
| `upload` / `download` | Bytes crossing the boundary. |
| `mine` | A list narrowed to the caller. |
| `import` / `export` | Bulk movement in or out of the platform. |

### Distinctions the platform already draws

**`trash` vs `delete` vs `purge`.** Three words for making something go away, applied consistently across sixteen verbs. `trash` sets `deleted_at` on a model that has it, and is reversible. `delete` removes a row that has no soft-delete column, so there is nothing to reverse. `purge` is the retention-window sweep that finally removes soft-deleted rows and their files, and is only ever emitted by a Celery task. Picking `delete` for a soft-deletable model is the common error; it reads as destructive in the audit trail when the row is still there.

**`read` vs `list` vs `mine`.** `read` returns one identified object, `list` returns a collection. A filtered, paginated or searched collection is still `list` — the filters are metadata on the Activity row, not part of the verb. `mine` exists because a caller-scoped list answers a different question about a person than a filtered one does, and the audit trail is asked that question.

**One endpoint, one verb.** A separate verb needs a separate endpoint behind it. `recordings.status` earns its own name because it is a distinct route serving a deliberately light payload for polling, next to `recordings.read` and its full metadata. Where a single route answers both the poll and the finished result, varying only its response as the state advances, that is one `read` — the state belongs in the Activity row's metadata, which is what makes it filterable, rather than in a second verb that no second endpoint backs.

**`add` / `remove` vs `create` / `delete`.** Membership is not existence. Putting a recording into a collection is `library.collection.item.add`; the recording is untouched and would survive the collection. Use `create` / `delete` only when the row itself begins or ends.

**`upload` / `download` vs `create` / `read`.** Bytes crossing the boundary get their own verbs, because the interesting fact for an audit reader is that a file moved, not that a row appeared. A recording's metadata is `recordings.read`; its bytes are `recordings.download`.

**`update` vs a named operation.** A bulk or semantically specific mutation earns its own verb when "what changed" would otherwise be recoverable only by reading the diff — `recordings.set_mains`, `library.collection.recordings.bulk_rename`. A plain field edit is `update`.

### When nothing fits

Roughly a fifth of the registry is a one-off: `login`, `logout`, `rollback`, `process`, `import`, `run`, `probe`, `erase`. That is the taxonomy working. An operation the base actions do not describe should be named in the app's own language rather than forced into `update` or `create`, which would leave the audit trail saying less than it could.

The test is whether the verb tells an audit reader what happened without opening the row. `user.password.reset.request` and `user.password.reset.confirm` are two verbs rather than one because the two steps mean different things to someone reading the trail months later.

### Adding one

Reuse before coining: if the operation already has a name in the app's table, use that name — a synonym beside an existing verb (`delete` where the app says `trash`) makes the trail unfilterable, and the review agent rejects it on exactly that basis. When the operation genuinely has no name yet, add the row to the app's table in the same commit as the endpoint, and say so in the commit message so the taxonomy is reviewed alongside the code.

## Verb registry

The authoritative list of every verb in use across the core apps. A new verb belongs here in the same commit that introduces it: the [`audit-trail-completeness`](../.review/agents/audit-trail-completeness.md) review agent matches each endpoint's verb literal against this table and fails the commit when it finds no row, so an endpoint whose verb never reached the registry does not merge.

The match is on the exact string, which is the point — it catches the case where an endpoint invents a synonym for an operation the app already names, the drift that leaves a trail unfilterable because half the deletions are `trash` and half are `delete`. Being in the table makes a verb known, not correct; whether it is the right verb for the operation is still a judgement the taxonomy check and the reviewer make.

Unless marked, a verb is emitted from an endpoint in that app's v1 Ninja API module. A † marks a non-request caller — a Celery task or management command inside a [`with_system_activity`](#audited-scope-for-non-request-callers) scope — where the same verb often has a second emitting site. A verb with two entries is emitted from both, which is deliberate for the federation operations that are reachable as an API call and as an operator command alike.

To check the table against the tree, walk the AST for `log_activity` / `with_system_activity` calls and compare the constant first argument or `verb=` keyword; a plain grep for `verb="` misses the positional `with_system_activity` sites and picks up test fixtures.

### `activity`

| Verb | Emitted by |
|---|---|
| `activity.changelog.list` | `list_change_logs` |
| `activity.rollback` | `rollback_change_endpoint` |
| `activity.rollback.bulk` | `rollback_bulk_endpoint` |

### `annotations`

| Verb | Emitted by |
|---|---|
| `annotations.annotation.create` | `create_annotation` |
| `annotations.annotation.delete` | `delete_annotation` |
| `annotations.annotation.list` | `list_annotations` |
| `annotations.annotation.mine` | `list_my_annotations` |
| `annotations.annotation.update` | `update_annotation` |
| `annotations.annotator.list` | `list_export_annotators` |
| `annotations.code.create` | `create_code` |
| `annotations.code.delete` | `delete_code` |
| `annotations.code.update` | `update_code` |
| `annotations.event.create` | `create_event` |
| `annotations.event.delete` | `delete_event` |
| `annotations.event.list` | `list_events` |
| `annotations.event.mine` | `list_my_events` |
| `annotations.event.update` | `update_event` |
| `annotations.export` | `export_annotations` |
| `annotations.interruption.create` | `create_interruption` |
| `annotations.interruption.delete` | `delete_interruption` |
| `annotations.interruption.list` | `list_interruptions` |
| `annotations.interruption.mine` | `list_my_interruptions` |
| `annotations.interruption.update` | `update_interruption` |
| `annotations.label.create` | `create_label` |
| `annotations.label.delete` | `delete_label` |
| `annotations.label.list` | `list_labels` |
| `annotations.label.mine` | `list_my_labels` |
| `annotations.label.update` | `update_label` |

### `compute`

| Verb | Emitted by |
|---|---|
| `compute.analysis.run` | `_record_launch` † |

### `epicurrents`

| Verb | Emitted by |
|---|---|
| `epicurrents.access_rights.purge` | `purge_expired_access_rights` † |
| `epicurrents.viewer_config.read` | `get_viewer_config` |
| `epicurrents.viewer_config.update` | `update_viewer_config` |

### `federation`

| Verb | Emitted by |
|---|---|
| `federation.grant.create` | `create_grant` †, `handle` † |
| `federation.grant.list` | `list_grants` |
| `federation.grant.renew` | `handle` †, `renew_grant` † |
| `federation.grant.revoke` | `handle` †, `revoke_grant` † |
| `federation.inbound.probe` | `deny`, `inbound_check_object` |
| `federation.peer.create` | `handle` †, `register_peer` † |
| `federation.peer.delete` | `delete_peer` † |
| `federation.peer.list` | `list_peers` |
| `federation.peer.read` | `get_peer` |
| `federation.peer.refresh_key` | `handle` †, `refresh_peer_key` † |
| `federation.peer.update` | `handle` †, `set_peer_display_name` †, `set_peer_trust` †, `update_peer` |
| `federation.remote.access` | `_record_federation_access` † |
| `federation.remote.read` | `_record_federation_read` † |

### `library`

| Verb | Emitted by |
|---|---|
| `library.collection.create` | `create_collection` |
| `library.collection.export` | `export_collection_to_dataset` |
| `library.collection.item.add` | `add_item` |
| `library.collection.item.list` | `list_items` |
| `library.collection.item.move` | `move_item` |
| `library.collection.item.remove` | `remove_item` |
| `library.collection.list` | `list_collections` |
| `library.collection.read` | `get_collection` |
| `library.collection.recordings.bulk_rename` | `bulk_rename_recordings` |
| `library.collection.restore` | `restore_collection` |
| `library.collection.trash` | `delete_collection` |
| `library.collection.update` | `update_collection` |
| `library.dataset.access.grant` | `grant_dataset_access` |
| `library.dataset.access.list` | `list_dataset_access_rights` |
| `library.dataset.access.revoke` | `revoke_dataset_access` |
| `library.dataset.create` | `create_dataset` |
| `library.dataset.folder.create` | `create_dataset_folder` |
| `library.dataset.folder.delete` | `delete_dataset_folder` |
| `library.dataset.folder.list` | `list_dataset_folders` |
| `library.dataset.folder.update` | `update_dataset_folder` |
| `library.dataset.item.add` | `add_dataset_item` |
| `library.dataset.item.list` | `list_dataset_items` |
| `library.dataset.item.move` | `move_dataset_item` |
| `library.dataset.item.remove` | `remove_dataset_item` |
| `library.dataset.list` | `list_datasets` |
| `library.dataset.read` | `get_dataset` |
| `library.dataset.snapshot.create` | `create_dataset_snapshot` |
| `library.dataset.snapshot.list` | `list_dataset_snapshots` |
| `library.dataset.snapshot.read` | `get_dataset_snapshot` |
| `library.dataset.trash` | `delete_dataset` |
| `library.dataset.update` | `update_dataset` |
| `library.purge` | `purge_deleted_library` † |
| `library.tag.create` | `create_tag` |
| `library.tag.delete` | `delete_tag` |
| `library.tag.item.add` | `tag_item` |
| `library.tag.item.list` | `list_tagged_items` |
| `library.tag.item.remove` | `untag_item` |
| `library.tag.list` | `list_tags` |
| `library.tag.read` | `get_tag_detail` |
| `library.tag.update` | `update_tag` |

### `media`

| Verb | Emitted by |
|---|---|
| `media.download` | `download_media` |
| `media.list` | `list_media` |
| `media.purge` | `purge_deleted_media` † |
| `media.read` | `get_media_detail` |
| `media.trash` | `delete_media` |
| `media.update` | `patch_media` |
| `media.upload` | `upload_media` |

### `notifications`

| Verb | Emitted by |
|---|---|
| `notifications.subscription.create` | `subscribe` |
| `notifications.subscription.delete` | `unsubscribe` |
| `notifications.subscription.purge_stale` | `send_push_to_user` † |

### `recordings`

| Verb | Emitted by |
|---|---|
| `recordings.access.list` | `list_recording_access` |
| `recordings.access.revoke` | `revoke_recording_access` |
| `recordings.annotations.list` | `list_recording_annotations` |
| `recordings.download` | `download_recording` |
| `recordings.download.slice` | `slice_recording` |
| `recordings.import` | `_run_job` † |
| `recordings.list` | `list_recordings` |
| `recordings.metadata.refresh` | `handle` † |
| `recordings.process` | `process_recording` † |
| `recordings.purge` | `purge_deleted_recordings` † |
| `recordings.read` | `recording_detail` |
| `recordings.read.slice` | `recording_detail_slice` |
| `recordings.set_mains` | `bulk_set_mains` |
| `recordings.status` | `recording_status` |
| `recordings.trash` | `delete_recording` |
| `recordings.update` | `update_recording` |
| `recordings.upload` | `upload_recording` |

### `user`

| Verb | Emitted by |
|---|---|
| `user.2fa.backup.regenerate` | `regenerate_backup_codes` |
| `user.2fa.confirm` | `confirm_enrolment` |
| `user.2fa.disable` | `disable` |
| `user.2fa.enroll` | `start_enrolment` |
| `user.2fa.read` | `get_status` |
| `user.account.2fa.reset` | `reset_account_two_factor` |
| `user.account.create` | `create_account` |
| `user.account.erase` | `_scrub_only` †, `handle` † |
| `user.account.groups.set` | `set_account_groups` |
| `user.account.list` | `list_accounts` |
| `user.account.password.set` | `set_account_password` |
| `user.account.read` | `get_account` |
| `user.account.update` | `update_account` |
| `user.group.create` | `create_group` |
| `user.group.delete` | `delete_group` |
| `user.group.list` | `list_groups`, `list_group_details` |
| `user.group.members.set` | `set_group_members` |
| `user.group.update` | `rename_group` |
| `user.login` | `login_endpoint`, `login_two_factor_endpoint`, `oidc_callback` |
| `user.login.challenge` | `login_endpoint` |
| `user.login.initiate` | `oidc_start` |
| `user.logout` | `logout_endpoint` |
| `user.password.change` | `change_password_endpoint` |
| `user.password.reset.confirm` | `confirm_password_reset` |
| `user.password.reset.request` | `request_password_reset` |
| `user.preferences.read` | `get_preferences` |
| `user.preferences.update` | `put_preferences` |
| `user.profile.read` | `me_endpoint` |
| `user.profile.update` | `update_profile_endpoint` |
| `user.role.list` | `list_role_providers` |
| `user.search` | `search_users` |

Project and plugin verbs are not listed here. They live in `projects/<name>/README.md` or `plugins/<name>/README.md`, per the segregation rule that keeps a project's surface documented with the project, and a project is expected to leave this repository once it is finished — a core-side list of them would go stale the first time one did. They follow the same taxonomy with the project or plugin name as the leading segment.

For those verbs to be enforced rather than merely written down, the README needs a section headed exactly `Verb registry` or `Audit verbs`, with the verbs inside it. The review agent matches only within such a section, and only on that exact title: a verb named anywhere else in a README cannot be distinguished from one proposed in an open TODO or given as an example of the naming pattern, and treating those as registrations would pass an unregistered verb. A project that carries no such section, or no README at all, has its verbs reported as unchecked rather than enforced.

## Manual recording — for non-API code paths

Code outside an audited scope (Celery tasks or management commands that haven't opened `with_system_activity`, signal handlers on non-tracked events) bypasses the auto-signal. The preferred path is to wrap the operation in `with_system_activity` (see above) and use the helpers below for the bulk-write subset that signals don't cover. Helpers in [audit.py](audit.py):

| Helper | When |
|---|---|
| `record_api_activity(request, verb, ...)` | Endpoint wants a custom `verb` or extra `metadata` on the parent Activity row. Returns the Activity. |
| `log_activity(verb, target=..., metadata=...)` | Endpoint wants to override the middleware default on the live Activity row (see above). |
| `record_create_change(actor=..., obj=..., activity=..., project=...)` | Manually log a creation. |
| `record_modify_change(actor=..., obj=..., before_state=..., activity=..., project=...)` | Manually log a modification with a captured before-state. |
| `record_delete_change(actor=..., obj=..., before_state=..., activity=..., project=...)` | Manually log a deletion. |

`before_state` must be obtained from `serialize_instance(obj)` *before* the write (the signal can't reach across function boundaries).

## Suppression mechanism

```python
from activity.request_context import set_change_logging_suppressed, reset_change_logging_suppressed

token = set_change_logging_suppressed(True)
try:
    # bulk writes that should not produce ObjectChangeLog entries
    ...
finally:
    reset_change_logging_suppressed(token)
```

The token is per-`ContextVar`-scope so nested suppress blocks compose correctly. Two production uses:

- **Rollback execution** in [audit.py](audit.py) — the `save()` (or `delete()` on a CREATE rollback) performed during a rollback would otherwise fire the auto-signal and write a MODIFY (or DELETE) entry. Suppression silences that path; `rollback_change` writes its own `ACTION_ROLLBACK` entry directly via `ObjectChangeLog.objects.create()` immediately afterwards, so the change is logged with the correct action type and only once.
- `_save_edf_results` in [recordings/tasks.py](../recordings/tasks.py) — annotations created from an EDF parse are pre-existing data, not user-driven changes.

Project code can use suppression for the same reason: bulk import, fixture loading, or any path where the audit row would be noise.

## Soft archive of `Activity`

The `archive_old_activity` Celery task ([tasks.py](tasks.py)) marks Activity rows older than `ACTIVITY_ARCHIVE_AFTER_DAYS` (default 90) as archived. Archived rows stay in the database — they're hidden from `Activity.objects` but accessible via `Activity.including_archived`.

| Setting | Default | Effect |
|---|---|---|
| `ACTIVITY_ARCHIVE_AFTER_DAYS` | `90` | Archive cutoff. Set to `0` to disable archiving entirely. |

Scheduled by [Celery Beat](../epicurrents/settings/common.py) under `archive-old-activity`, runs every 24 h. Rows are archived in batches of 1 000 to keep lock windows small.

`ObjectChangeLog` rows are **never** archived. They are the permanent audit trail and rolling them up would defeat the rollback guarantee.

## Periodic integrity check

The `verify_audit_integrity` Celery task ([tasks.py](tasks.py)) runs daily and proactively walks the audit trail looking for tamper. Two phases share one task invocation:

1. **Chain verification.** For every `content_type` that has v3 rows, the task calls `verify_chain` and emits security events for any anomaly: `audit.chain_break` (content tamper or link break), `audit.chain_gap` (missing `sequence_no`), `audit.genesis_invalid` (lifted shard), `audit.hash_key_missing` (key version absent from `settings.ACTIVITY_HASH_KEYS`).
2. **Derived-state recompute.** For each row in a sliding window (`ACTIVITY_DERIVED_CHECK_WINDOW_DAYS`, default 7) with a non-empty `extra_payload`, the task calls `verify_derived_state` and emits `audit.derived_state_mismatch` per diverging digest. Older rows are covered by chain verification (which catches tampering with the stored digest); the recompute layer additionally catches tampering with the *dependent* rows the digest covers and is bounded to recent rows because the digester cost is per-row.

Clean runs log a single INFO line. Anomalies emit one `epicurrents.security` WARNING per class via `log_security_event`, plus a WARNING summary line at end-of-run. The event-type set is enumerated in [epicurrents/security_log.py](../epicurrents/security_log.py) and pinned by the security-log taxonomy test. SIEM rules pivot on `security_event_type=audit.*`.

The function is also callable directly for ad-hoc forensics: `python manage.py shell` → `from activity.integrity_check import run_integrity_check; run_integrity_check(derived_window_days=30)`.

## Settings consumed

| Variable | Default | Source | Notes |
|---|---|---|---|
| `ACTIVITY_ARCHIVE_AFTER_DAYS` | `90` | `.env` / `epicurrents/settings/common.py` | `0` to disable Activity archive. |
| `ACTIVITY_DERIVED_CHECK_WINDOW_DAYS` | `7` | `.env` / `epicurrents/settings/common.py` | Sliding-window length for the derived-state phase of `verify_audit_integrity`. `0` skips that phase. Chain verification is always full-scope. |
| `ACTIVITY_PATH_SKIP_LIST` | `("/api/v1/health", "/annotations/api/v1/health", "/api/v1/notifications/vapid-public-key")` | `epicurrents/settings/common.py` | Exact-match paths that bypass `Activity`-row creation in `ApiActivityLoggingMiddleware`. Used for operational endpoints whose volume would drown the data-interaction signal. Per-endpoint policy in [`.review/exemptions/audit-trail-completeness.md`](../.review/exemptions/audit-trail-completeness.md). |
| `EPICURRENTS_PROJECT` | `""` | `.env` | Read by `ApiActivityLoggingMiddleware`; populates `Activity.project` on requests to `/project/api/`. |

## Project plugin extension points

Tracking is fully generic — project models are audited automatically:

- Any model under `projects.<name>` is included in the auto-signal coverage (the excluded models are `Activity`, `ObjectChangeLog`, and `Session`; see `EXCLUDED_MODELS` in [signals.py](signals.py)).
- API endpoints in `projects/<name>/urls.py` get their parent `Activity` row from the middleware and don't need to do anything else.
- The `project` field on both models is populated automatically when the request path contains `/project/api/`.

If a project model needs to be excluded from auto-logging (e.g. an ephemeral cache table that writes frequently), use the suppression helpers around its writes; don't extend `EXCLUDED_MODELS` directly.

Two registries exist for personal-data handling, both called from `AppConfig.ready()` (see [Subject erasure](#subject-erasure-gdpr-art-17)):

- `activity.erasure.register_subject_pii` — declare which of the project model's audited fields carry a user's personal data, so `erase_subject` scrubs them on an Art. 17 request.
- `activity.audit.register_masked_fields` — declare credential fields that must never reach the audit trail in the clear.

Field names in both are the **serialized attnames** `serialize_instance` writes, so a foreign key is `user_id` rather than `user`. Getting one wrong used to be free: a bad model label makes `erase_subject` skip the spec, a bad `owner_field` matches no rows, and a bad `pii_fields` entry is never found — all three leaving the erasure summary reporting zero for the model, which is what a legitimately clean run reports too. [activity/checks.py](checks.py) closes that with a Django system check validating every registration against the real model, so a typo fails `manage.py check` rather than surfacing as an unfulfillable erasure request months later.

A system check rather than a test because it runs after every app's `ready()` — it therefore covers whatever project and plugins a deployment has active, where a test under the platform test settings sees only the core registrations. One deliberate non-check: a field registered for both masking and erasure looks like dead configuration but is not, and `user.user` does it with `password` on purpose.

## Tests

```bash
pytest activity/tests/
```

Patterns to follow:

- **`test_audit.py`** — direct use of `record_*_change` helpers + `rollback_change`. Good model for testing audit coverage of new endpoints.
- **`test_api.py`** — `client.force_login(user)` + `post_json` (fixture in [conftest.py](../conftest.py)). Includes the pre-flight semantics of bulk rollback.
- **`test_tasks.py`** — exercises `archive_old_activity`. Uses `_backdate` to set `created_at` on existing rows (regular `.save()` doesn't override `auto_now_add`; use `.update()` instead — see [signals.py](signals.py) gotcha below).

## Gotchas

- **`auto_now_add` cannot be overridden by `save()`.** Tests that need rows with old `created_at` values must use `Model.objects.filter(pk=...).update(created_at=...)` after creation. The `_backdate` helper in [test_tasks.py](tests/test_tasks.py) is the canonical example.
- **Signals fire only on API requests.** A `model.save()` from a management command writes no ObjectChangeLog entry by default. Wrap such writes with `record_*_change` if audit coverage is required, or run the command via the API instead.
- **`changes` is `null` on CREATE and DELETE entries.** The diff is only meaningful for MODIFY (and ROLLBACK, which is implemented as a MODIFY-shaped change). Filter accordingly when listing.
- **Rollback of a CREATE is destructive.** It deletes the object. The deletion is itself logged as ROLLBACK with `before_state` capturing the deleted fields, so "undo my rollback" works — but until the rollback-of-rollback runs, the object is gone from regular queries.
- **`Activity.project` ≠ `ObjectChangeLog.project` reliability.** When a request hits `/api/v1/...` (core, not project), both fields are empty. When a request hits `/project/api/v1/...`, both are set to `EPICURRENTS_PROJECT`. Don't filter the change log by `project` alone if your handler can be called from either path.
- **Direct DB writes (`.update()`, raw SQL, `bulk_create`) bypass the signals entirely.** This is sometimes desirable (the archive task uses `.update()` for exactly this reason) and sometimes a bug. If you write a Celery task that mutates user data, prefer per-row `.save()` so it gets logged.
