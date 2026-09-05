# epicurrents

The platform's core app. Owns the access-control model that every other app builds on, the request-logging middleware that wires audit signals, the project loader that activates per-deployment customisation, the settings module, and the management commands for bootstrapping and lifecycle work.

There are no domain endpoints here — no recordings, no annotations, no library. The only HTTP surface is a one-route health endpoint. Everything else is plumbing other apps depend on.

## Permissions

The permission system is the load-bearing piece of this app. Three layers compose to answer "can `user` do `X` to `obj`?":

1. **`AccessRight` rows** — generic, per-object grants stored in `epicurrents_accessright`. One row per `(content_type, object_id, target)` grant, enforced by three partial unique constraints (one per user target, group target, and `(federated_peer, remote_user_id)` pair; share-token rows are covered by the token's global uniqueness).
2. **Permission functions** in [permissions.py](permissions.py) — `can_read_object`, `can_write_object`, `can_modify_object`, `can_annotate_object` plus their `ensure_*` exception-raising counterparts. Endpoints call these; they never query `AccessRight` directly.
3. **Permission extensions** registered via `register_read_permission_extension` — checked only when no direct `AccessRight` row matches. Library uses this to grant read access via Dataset membership.

Above the three grant layers sits their restrictive counterpart: **read-visibility gates** registered via `register_read_visibility_gate(model_label, gate)`. A gate receives `(user, obj, share_token)` and returns `True` when the object must be treated as invisible to that caller; the resolver consults gates after the superuser fast-path and before any grant, so no `AccessRight` row or extension can surface an object its model's gate hides. The federated resolver consults the same gates with `user=None`, which a gate must treat as a fully unprivileged caller. Recordings register the only core gate ([recordings/permissions.py](../recordings/permissions.py)): FAILED recordings are hidden from everyone but the author, trashed recordings from every caller. Register gates from `AppConfig.ready()`; registration is idempotent.

### `AccessRight` model

Generic FK to the target object (`content_type` + `object_id`), with exactly one of four target types per row:

| Target type | Field | Used for |
|---|---|---|
| Local user | `access_target` | Direct share between two users on this instance. |
| Local group | `access_target_group` | Share with everyone in a Django group. |
| Public share token | `public_share_token` | Unauthenticated read access via a URL containing the token. Token rows are always `can_write=False`, `can_share=False`. |
| Federated peer | `federated_peer` (+ optional `remote_user_id`) | Grant to a trusted remote instance. `remote_user_id=""` is a wildcard for any authenticated user from that peer. |

The four-way disjunction is enforced by `access_right_exactly_one_target` (`CheckConstraint`). A second check, `access_right_requires_some_permission`, requires at least one of `can_read` / `can_write` / `can_share` to be set. Per-target uniqueness is enforced by three partial `UniqueConstraint` rows (`access_right_unique_user_target`, `access_right_unique_group_target`, `access_right_unique_federated_target`), so granting the same target twice on one object is a constraint violation — the grant endpoints answer it with 409 before the database has to.

Permission booleans:

| Flag | Default | Effect |
|---|---|---|
| `can_read` | `True` | List + view + download. |
| `can_write` | `False` | Modify fields the author can modify, soft-delete. |
| `can_share` | `False` | Create new `AccessRight` rows that re-share this object. |
| `apply_middleware` | `False` | Pipe EDF/BDF content through the configured pipeline when serving downloads. No effect on the recording author, superusers, or non-EDF files. |

Other fields:

| Field | Notes |
|---|---|
| `access_giver` | The user who granted this right. Cascades on user delete. |
| `expires_at` | Optional. Expired rows are filtered out by `AccessRight.objects.active()` and hard-deleted on a 6-hourly schedule by `purge_expired_access_rights` ([tasks.py](tasks.py)). |
| `created_at`, `modified_at` | Standard timestamps. |

`AccessRight` rows are cleaned up when their target object is hard-deleted via reverse `GenericRelation` declarations on every target model (`Recording`, `Collection`, `Dataset`, plus any project-plugin model that wants to be accessible). New target-capable models must declare `access_rights = GenericRelation("epicurrents.AccessRight")` to participate. See [AGENTS.md → GenericFK target cascade pattern](../AGENTS.md#genericfk-target-cascade-pattern) for the full rule.

### Permission functions

All four are in [permissions.py](permissions.py). Each returns `bool`; the `ensure_*` variants raise `HttpError(403)` instead.

| Function | Grants when |
|---|---|
| `can_read_object(user, obj, share_token=None)` | Superuser, **or** — provided no read-visibility gate hides the object from this caller — an active `AccessRight` with `can_read=True` matching the user / one of their groups / the supplied token, **or** a registered read-permission extension returns truthy. |
| `can_write_object(user, obj)` | Superuser, **or** object author, **or** an active `AccessRight` with `can_write=True` matching the user / one of their groups. **Never** granted via share token or extension. |
| `can_modify_object(user, obj, author_field="author")` | Superuser or object author only. Used for endpoints where authorship alone matters (e.g. updating one's own annotation). |
| `can_annotate_object(user, obj, share_token=None, annotator=None)` | Lower bar than write: object authorship **or** any read right suffices. Required for annotation creation — see the "lower bar" rationale below. Returns `False` if a `share_token` is supplied without a non-empty `annotator`. |

The lower bar for `can_annotate_object` exists because annotations are inherently personal: a user with read access to a shared recording should be able to attach their own observations without the sharer also having to grant write access on the recording. Update/delete of an annotation still requires `can_modify_object` against the annotation itself, so authorship of the annotation gates further changes.

### `ReadAccessTerms`

`can_read_object` returns a bool. When the caller needs the underlying `AccessRight.apply_middleware` flag too (for choosing whether to pipe an EDF file through the middleware pipeline), use `get_read_access_result` instead:

```python
result = get_read_access_result(user, recording, share_token=token)
if not result.granted:
    raise HttpError(403)
if result.apply_middleware:
    # serve through the EDF middleware pipeline
else:
    # serve raw bytes
```

`ReadAccessTerms.__bool__` returns `granted`, so `if result:` works the same as `if result.granted:`.

Extension-granted reads always carry `apply_middleware=False` (extensions have no backing `AccessRight` row). Direct-match reads return whatever `apply_middleware` is on the matching row.

### Resolution order

`get_read_access_result` checks in this order; the first match wins:

1. Superuser → `granted=True`, default metadata.
2. Read-visibility gates → `granted=False` when any gate hides the object from this caller.
3. Direct `AccessRight` query matching user, group, or supplied share token. Returns the matching row's `apply_middleware`.
4. Registered extensions in registration order (see below). Always yield `apply_middleware=False`.

Within step 3, a caller can match through several targets at once — their own user row plus group rows, or a group row plus a presented share token. Per-target uniqueness caps each target at one row, and the query orders the survivors so the winner is defined rather than creation-order accident, in two tiers. The caller's direct user row wins outright, whatever its `apply_middleware` — the same [explicit-grants-win rule](#gotchas) that lets a direct row beat a more-sanitizing extension grant. Among the remaining group and token rows, the de-identifying row (`apply_middleware=True`) wins, so within that tier an ambiguous overlap never serves raw bytes a stricter grant would have sanitized.

The early-return on step 3 is important: if a direct row matches and has `apply_middleware=False`, extensions are not consulted even if they would have granted middleware-piped access. This is intentional — explicit grants win.

### Federated read access

`get_federated_read_access_result(peer, remote_user_id, obj)` is the federation analogue: matches `AccessRight` rows where `federated_peer` equals the peer and `remote_user_id` is blank (wildcard) or matches exactly. When both a wildcard row and an exact-user row exist, the exact row's terms win. There is no superuser/author fast path for federated callers — peers are always governed by explicit grants. See [federation/README.md](../federation/README.md) once it's written.

### Permission extensions

```python
# In your app's ready():
from epicurrents.permissions import register_read_permission_extension


def can_read_via_my_rule(user, obj, share_token=None):
    # return bool or ReadAccessTerms
    ...


register_read_permission_extension(can_read_via_my_rule)
```

Extensions are only consulted when no direct `AccessRight` row matches. Each callable receives `(user, obj, share_token)` and may return:

- `False` / `None` → no opinion, fall through to the next extension.
- `True` → grant, with default metadata (`apply_middleware=False`).
- `ReadAccessTerms(granted=True, apply_middleware=...)` → grant with custom metadata. Use this when the extension's source object has its own middleware setting (e.g. dataset membership carries `apply_middleware` from the dataset's `AccessRight`).

Registration is idempotent. The core production registration lives in [library/apps.py](../library/apps.py): `can_read_via_dataset` (dataset membership). Project plugins can register their own from `apps.py`.

To see which step of the resolver answers for a concrete (object, caller) pair — superuser fast-path, author shortcut, direct row with its `apply_middleware` value, or a named extension — run `manage.py explain_access <app.model> <pk> [username] [--share-token ...]`. Read-only; it mirrors the resolver's order and reports the first step that grants.

Two registry-wide contract tests back the permission surface: [tests/test_api_auth_sweep.py](tests/test_api_auth_sweep.py) walks every mounted Ninja operation and fails any route without a recognised authentication shape or a reviewed allowlist entry, and [tests/test_access_matrix.py](tests/test_access_matrix.py) pins the cross-cutting outcomes (FAILED-hidden, soft-delete hiding, author-private nulls, sanitised bytes for middleware grants) as one caller × state × route table. Adding a route means the sweep sees it automatically; adding a caller class or surface means adding its row to the matrix.

## Middleware

`ApiActivityLoggingMiddleware` in [middleware.py](middleware.py) creates an `Activity` row on entry to every API request, stores the user and Activity in ContextVars, and fills in `status_code` plus `target_object_id` (extracted from URL kwargs) on exit. The signal handlers in [activity/signals.py](../activity/signals.py) read those ContextVars to attach `ObjectChangeLog` entries to the current request. See [activity/README.md](../activity/README.md) for the full picture.

The middleware also populates `Activity.project` from `EPICURRENTS_PROJECT` when the request path contains `/project/api/`, which lets the audit layer filter changes by project after the active project has been switched.

**What counts as an "API request"** is decided by `_API_PATH_RE` — a regex matching `/api/v<N>/...` (core / user / activity / notifications / library / federation) and `/<app>/api/v<N>/...` (annotations / compute / recordings / the active project). The matcher is the **load-bearing path-classification decision** in this app; mis-classifying silently strips the audit trail off every endpoint mounted under the affected prefix. Two contract tests backstop it: [`tests/test_middleware_path_recognition.py`](tests/test_middleware_path_recognition.py) walks `urlpatterns` and asserts every mounted `api/v<N>/` path matches the regex, and [`tests/test_middleware_audit_trail.py`](tests/test_middleware_audit_trail.py) makes a real request to each API mount and asserts the `Activity` row appears. AGENTS.md → *Load-bearing files* records the convention.

`ApiThrottleMiddleware` runs just before the audit middleware (and after `AuthenticationMiddleware` / `SessionMiddleware`, whose state it reads) and rejects API requests that exceed a per-identity request-rate ceiling. The logic lives in [throttle.py](throttle.py): it keys on the authenticated user, then `share_token`, then session, then client IP, so a NAT'd shared-egress group is throttled per identity rather than per address. It runs before the audit middleware so a throttled flood creates no `Activity` rows, and it [fails open](#gotchas) — a cache outage lets requests through rather than erroring the API. See [docs/operations.md → API rate limiting](../docs/operations.md#api-rate-limiting) for the operator-facing tuning.

## Versioning and the platform pin

The platform version lives in [version.py](version.py) as `__version__`, re-exported as `epicurrents.__version__`. It is [semantic](https://semver.org): a release tags that exact value as `v<version>`. Releases are plain `MAJOR.MINOR.PATCH` — pre-release and build metadata are part of semver but are rejected rather than half-supported, because their precedence rules are subtle and nothing needs them yet.

### Below 1.0

The platform is on `0.x`, which is semver's provision for initial development and is a statement about the surface below, not about the code: it is feature-complete, but the extension points a project builds on are still moving. The number goes to 1.0 when they stop.

**The consequence is that the breaking bump is the minor, not the major.** `0.1 → 0.2` is allowed to break anything; `0.1.0 → 0.1.7` is not. So a cap is the next *minor* — `>=0.1,<0.2` — and a cap of `<1` would silently admit every breaking release there is. `compatible_range()` in [version.py](version.py) encodes that rule and the check's hint reads it from there, so the two cannot disagree. From 1.0 onwards it returns the familiar `>=1.4,<2`.

The module imports nothing but `re`. `epicurrents/__init__.py` imports the Celery app, so a version read through the package drags Celery in, and the readers include a system check that runs before most of Django is up.

### What a major version promises

A version is only worth the scope it covers, and this one is deliberately narrower than the codebase. A number promising compatibility across every module would have to bump its major on nearly every change, which promises nothing.

Covered — a breaking change to any of these bumps the major:

- The four permission functions and `register_read_permission_extension`.
- The pipeline, converter, CSV-subconverter and conversion-hook registries, and the settings keys that configure them.
- The EDF middleware ABCs in [federation/middleware.py](../federation/middleware.py) and `build_header` / `recordings.testing` in [recordings](../recordings/README.md).
- The audit recorders in [activity/audit.py](../activity/audit.py) and `with_system_activity`, which cross-cutting rules require projects to call directly.
- The AppConfig contract a project or plugin implements: `requires_platform`, `plugin_url_namespace`, `requires`.
- The URL mount points (`/project/api/v1/`, `/project/<name>/`, `/plugin/<name>/`).
- Core model names and their identity fields, which project foreign keys point at.

Not covered: everything else, including any name not documented as an extension point. The surface is expected to widen as more of it is written down; it is not expected to shrink.

The REST API is versioned separately in its own path segment and is not what this number tracks.

### Declaring a pin

A project or plugin states the range it was built against, on its `AppConfig`:

```python
class MyprojectConfig(AppConfig):
    name = "projects.myproject"
    label = "myproject"
    requires_platform = ">=0.1,<0.2"
```

The AppConfig rather than `settings.py`, because several plugins can be enabled at once and the settings merge replaces scalars — each loaded plugin would silently overwrite the last one's value. Every app config is its own declaration.

Clauses are comma-separated and ANDed, with `>=`, `>`, `<`, `<=`, `==`, `!=`. A version on the right may have one, two or three components and is padded with zeroes, so `<2` means `<2.0.0` and `>=1.4` means `>=1.4.0`. Note what that does to equality: `==1.4` is `==1.4.0` and matches no other patch release, so write a range to mean "any 1.4".

[checks.py](checks.py) verifies every declaration at `manage.py check`, which Django runs before `runserver` and before `migrate` — so a mismatched pair stops the stack coming up rather than being found by whatever it corrupts first. An unsatisfied pin is an error and blocks the boot; an absent one is a warning, because that is what every project written before this looked like and refusing to start over a missing declaration is a worse outage than the drift it guards against.

The pin **gates, it does not drive**. Nothing reads it to select a platform checkout. Do not downgrade a platform a deployment has already migrated: migrations are forward-only in practice, and the audit chain carries a `hash_key_version` and per-content-type chain state that an older platform will not verify.

## Settings architecture

Three modules under [settings/](settings/):

| Module | When | Source of `DEBUG`, `DATABASES`, security headers |
|---|---|---|
| `common.py` | Loaded by both modes | Defines defaults shared by development and production. Calls `apply_project_settings()` at the end to merge the active project's overrides. |
| `development.py` | `DJANGO_MODE=development` (or unset) | SQLite by default, Postgres if `DB_DEV_ENGINE=postgres`. `DEBUG=True`. |
| `production.py` | `DJANGO_MODE=production` (Docker default) | Postgres, `DEBUG=False`, HTTPS security headers, Redis cache, JSON logging to stdout. |
| `test_platform.py` | Selected via `pytest.ini` | In-memory SQLite, MD5 hasher, `CELERY_TASK_ALWAYS_EAGER=True`, locmem email, in-process cache. |

[`settings_mode.py`](settings_mode.py) picks the right module from `DJANGO_MODE`; `DJANGO_SETTINGS_MODULE` is a fallback when `DJANGO_MODE` is unset. Setting both with conflicting values emits a `RuntimeWarning` and `DJANGO_MODE` wins.

### Settings the core app consumes directly

| Variable | Default | Purpose |
|---|---|---|
| `EPICURRENTS_PROJECT` | `""` | Active project name. Read by `apply_project_settings` and the middleware. Must be set *before* the Django process starts. |
| `RECORDINGS_TRASH_RETENTION_DAYS` | `30` | Days before soft-deleted recordings are purged. |
| `RECORDINGS_MAX_UPLOAD_SIZE` | `2 GiB` | Hard cap on an uploaded recording, enforced by the upload view as it streams chunks to disk. |
| `DATA_UPLOAD_MAX_MEMORY_SIZE` | `10 MiB` | Django's ceiling on non-file request data held in memory. Unrelated to the recording cap despite the name — the multipart parser applies it to form fields only. |
| `ACTIVITY_ARCHIVE_AFTER_DAYS` | `90` | `0` disables archive. |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` / `ADMIN_EMAIL` | `admin` / `admin` / `admin@epicurrents.local` | Bootstrap admin credentials used by `createadmin`. |
| `RECORDING_PIPELINES` | `{}` | Override / extend named ingest pipelines. See [recordings/README.md](../recordings/README.md). |
| `FEDERATION_*` | `""` | Federation key pair, instance URL, JWT TTL. See [federation/README.md](../federation/README.md). |
| `WEBPUSH_VAPID_*` | `""` | VAPID key pair for web push. See [notifications/README.md](../notifications/README.md). |
| `SESSION_CSRF_ENFORCED` | `True` (prod), `False` (dev) | Master switch for the `enforce_session_csrf` chokepoint in [auth.py](auth.py). |
| `API_THROTTLE_ENABLED` | `True` (prod), `False` (dev) | Master switch for the per-identity API request-rate throttle in [throttle.py](throttle.py). |
| `API_THROTTLE_RATE_DEFAULT` / `API_THROTTLE_RATE_UPLOAD` / `API_THROTTLE_IP_RATE` | `300` / `30` / `1000` (per minute) | Throttle ceilings. `API_THROTTLE_RATES` and `API_THROTTLE_SCOPE_MAP` carry the full scope structure; see [docs/operations.md → API rate limiting](../docs/operations.md#api-rate-limiting). |
| `DISABLE_NO_STORE_HEADERS` | `False` | PHI hygiene: `SecurityHeadersMiddleware` sets `Cache-Control: no-store` on every response (via `setdefault`) unless this is set; non-PHI static assets opt back into caching themselves. See AGENTS.md → *PHI no-store caching*. |
| `ENABLE_PUBLIC_VIEWER` | `False` | Serves the standalone, auth-free viewer at `/viewer/<mode>`. See [Public viewer](#public-viewer). |
| `PUBLIC_VIEWER_MODES` | one `public` mode | Per-mode viewer lib + SETUP for the public viewer; project-overridable. See [Public viewer](#public-viewer). |

Project plugins extend the settings via `projects/<name>/settings.py`. The merge rules — list-append for `INSTALLED_APPS` / `MIDDLEWARE` / a few other named lists, dict-merge for `CELERY_BEAT_SCHEDULE`, replace for everything else — are defined in [project_loader.py](project_loader.py).

## Project loader

The project layer is designed for one active project per deployment. Switching projects exists to support development and onboarding new deployments — it is not intended as a runtime operation in production. Each switch stops the application services, renames live tables, runs migrations, and rebuilds the frontend; none of that is production-friendly.

[`project_loader.py`](project_loader.py) is what makes the project-plugin system work. It exposes:

- `get_active_project()` — returns `EPICURRENTS_PROJECT` (stripped) or `""`.
- `apply_project_settings(globals())` — called once from `common.py`. Validates that `projects/<name>/apps.py` exists, appends `projects.<name>` to `INSTALLED_APPS`, then merges the project's `settings.py` into the active globals using the rules above.
- `get_project_state()` / `set_project_state()` — read/write `projects/.state.json`. The state file is only touched by the lifecycle management commands; the running Django server never reads it. A malformed state file (truncated write, manual edit that broke JSON) is not swallowed — `JSONDecodeError` propagates so corruption is loud.
- `rename_db_table(cursor, old, new)` — vendor-neutral `ALTER TABLE ... RENAME TO`. Identifier quoting goes through `connection.ops.quote_name`, not raw f-strings. Used by `activate_project` / `deactivate_project` to archive and restore project tables.
- `ARCHIVE_PREFIX` — the `_archived_` table-name prefix used by the three lifecycle commands. Defined here so the commands cannot drift apart on the value.

The whole project layer is invisible to runtime code paths — projects appear as ordinary Django apps once activated.

## Public viewer

A standalone, auth-free viewer page at `/viewer/<mode>` ([views.py](views.py) `public_viewer_view`), separate from the project SPA. It opens the viewer with no platform data, no auth, and no URL parameters — for demos, embedding, or a public landing viewer. Off unless `ENABLE_PUBLIC_VIEWER` is set; the view 404s when disabled even though the route is mounted.

The page sets its own `COOP: same-origin` + `COEP: require-corp`, so the document is cross-origin isolated and the viewer's SharedArrayBuffer memory manager works regardless of the site-wide `ENABLE_CROSS_ORIGIN_ISOLATION`. Same-origin viewer assets carry `CORP: same-origin` from the asset views.

Each mode in `PUBLIC_VIEWER_MODES` names a viewer lib (`lib_path`) and a fixed `setup` object injected into the page; the lib is loaded as a classic-script UMD that sets `window.Epicurrents`. The bundle's filename comes from the mode's optional `lib_file`, because the two viewer builds name theirs differently: the builder edition copied into `/viewer/` emits `epicurrents-lib.umd.js`, while the per-project builds under `/viewer/<project>/` emit `epicurrents-lib.umd.cjs` — which is the default when a mode omits the key. The built-in `public` mode loads the builder edition at `/viewer/`. A project's `settings.py` can add a mode or override `public` through the settings dict-merge — e.g. pointing `lib_path` at its own `/viewer/<project>/` build so the public page carries the project's setups, montages and trend derivations, which the generic build does not. The route's mode segment is built from the configured keys, so only configured modes resolve.

## Viewer config overrides

A deployment can override viewer settings — `eeg.defaultMontage`, `eeg.trends.amplitude.epochLength`, any dotted-path settings field — without patching and rebuilding the viewer source. The effective configuration the frontend applies on launch is two layers merged, overrides winning:

- **Seed** — a viewer-config.json file in the active project's source directory. Read-only, ships with the project source (and its distribution bundle). A flat map of dotted-path field to value (`{"eeg.defaultMontage": "lon"}`). Absent, malformed, or non-object files resolve to an empty seed.
- **Overrides** — a `ViewerConfigOverride` row ([models.py](models.py)), keyed one-per-project, editable from the frontend. Same flat-map shape as the seed.

[`viewer_config.py`](viewer_config.py) exposes `load_viewer_config_seed(project)`, `get_project_overrides(project)`, and `get_effective_viewer_config()` (the merge for the active project). The frontend fetches the effective config and applies each entry through the viewer's interface-then-core `setFieldValue` fallback; an entry naming an unknown field is skipped with a console warning rather than failing the launch.

Write access is gated on **staff** (`is_staff` or `is_superuser`), not superuser. This is a deliberate departure from the platform's usual write→superuser rule: deployment operators who tune the viewer (e.g. course instructors) are staff but not superusers, and the override surface holds no PHI and is reversible. The departure is documented inline at the endpoint. Reads are open to any authenticated user, since the viewer applies the same config for everyone.

The staff editor dry-validates the overrides before saving. Since validity depends on the live settings tree (which spans both the interface and core layers and only exists after a viewer loads its modules), the editor launches a hidden, data-less viewer instance and runs each field through the same `setFieldValue` the applier uses. An unknown field or a wrong-typed value is reported up front and blocks the save, rather than being silently dropped at viewer launch. If the hidden viewer cannot launch, the editor saves without validation rather than blocking.

## Vendored browser assets

`VENDOR_DIR` (default `frontend/vendor`) holds version-pinned assets the browser loads directly, served at `/vendor/<path>` by [views.py](views.py) `vendor_view`. The tree is gitignored, generated at deploy, and served rather than bundled — it is not part of `collectstatic` or the Vite build. `vendor_view` tags each response with `Cross-Origin-Resource-Policy` so the files load under the viewer's `COEP: require-corp` isolation, and caches the version-pinned files as `immutable` while letting `pyodide-lock.json` revalidate.

Two producers write into it, and [bootstrap.sh](../scripts/bootstrap.sh) and [update.sh](../scripts/update.sh) run both: `vendor_pyodide` for the Python interpreter, and `generate_compute_static` ([compute/](../compute/management/commands/generate_compute_static.py)) for the pre-computed lead fields. They differ in kind — one downloads a pinned distribution, the other computes from the platform's own MNE install — which is why one is skipped when a hash check passes and the other simply runs.

Both run in the `vendor` compose service rather than in `web`. Production mounts this tree into web read-only — web serves the interpreter every viewer session executes, so a write path from the request-handling container would reach every visitor's browser — which leaves web the one container that cannot populate it. The `vendor` service carries the same image, settings and database with that single mount inverted, sits behind a compose profile so `up` never starts it, and runs as root because the bind is a host directory Docker creates on first use, owned by root.

### The Pyodide runtime

The viewer's analysis tools run Python in the browser, and the interpreter comes from the deployment's own origin — `pyodideAssetPath` in `PUBLIC_VIEWER_MODES` names `/vendor/pyodide/<version>/`, and the production CSP allows no third-party origin, so a CDN fallback is refused rather than silently taken.

`vendor_pyodide` populates that path with the runtime core and only the wheels the viewer loads: the closure of numpy, scipy and matplotlib, plus mne and whatever mne needs that the distribution lacks. That is 26 packages out of the distribution's 354, around 47 MiB. It writes a pruned `pyodide-lock.json` describing exactly what is on disk — a full lock over a partial tree would let `loadPackage` name a wheel that was never downloaded, turning a resolution error into a 404.

mne is resolved from PyPI because Pyodide un-bundled it after 0.28. Only pure-Python wheels are accepted; a dependency needing compilation has to come from the distribution, and one that satisfies neither aborts the run rather than being dropped to surface later as an `ImportError` in someone's browser.

Two things follow from the path carrying a version. Setting `pyodideAssetPath` at all commits the runtime to self-hosted package resolution — every package must resolve from that folder's lock — so pointing it at an upstream CDN takes the same branch against a lock with no mne and fails. And the version string appears in three places: this setting and the two frontend entry points that seed the viewer SETUP. The command reads the setting and warns when a frontend file disagrees, because a mismatch is otherwise invisible until the browser requests a path nothing populated.

`--check` verifies an existing tree against its own lock (every file present, every hash matching, no dependency the lock omits) and downloads nothing. [scripts/update.sh](../scripts/update.sh) runs it on every update and re-vendors only when it fails — the tree is excluded from the update rsync so each deployment keeps its own copy, which also means a fresh host or a restored snapshot arrives with nothing to serve.

### Static lead fields

`generate_compute_static` writes pre-computed lead fields to `/vendor/leadfields/` for the viewer's source-localisation tool, which prefers them over the compute API: a manifest fetch plus a blob, no metadata round-trip, and cacheable by the service worker so the tool works offline. Blob filenames carry a content hash, so a regenerated field that has not changed keeps its name and every cached copy stays valid.

The consequence of an absent bundle is milder than a missing interpreter: [leadFields.ts](../frontend/src/viewer/leadFields.ts) falls back to `/compute/api/v1/eeg/leadfield/`, so the tool works but computes per montage against the database and loses offline use. A montage missing from both sources reports itself as unavailable rather than failing.

Generation reads and writes `LeadFieldCache`, so unlike the Pyodide vendoring it needs a migrated database and cannot run before migrations. It takes seconds, so both deploy paths run it unconditionally — which is also the only way a change to the generator's montages or grid parameters reaches a deployment.


## Management commands

All commands run inside the Docker stack (`docker compose run --rm web python manage.py <command>`) so they target PostgreSQL, not the host SQLite dev DB. Running them on the host while the Docker stack is up applies migrations to the wrong database and breaks the stack.

| Command | Purpose |
|---|---|
| `init_env` | Fill empty values in `.env` with generated secrets — `SECRET_KEY`, `BORG_PASSPHRASE`, `ADMIN_PASSWORD`, VAPID keypair, federation Ed25519 keypair. Never overwrites a value you have already set. Creates `.env` from `.env.example` if missing. |
| `createadmin` | Create a superuser from `ADMIN_USERNAME` / `ADMIN_PASSWORD` / `ADMIN_EMAIL`. No-op when any superuser already exists. Run automatically by [entrypoint.sh](../entrypoint.sh) on first start. |
| `generate_vapid_keys` | Print a freshly-generated VAPID keypair to stdout for manual paste into `.env`. Use `init_env` instead for the normal bootstrap path. |
| `activate_project <name> [--fresh]` | Restore `_archived_<name>_*` tables (default) and run `migrate`; or clear migration history and start fresh. `EPICURRENTS_PROJECT` must equal `<name>`. |
| `deactivate_project` | Rename live tables of the currently-active project to `_archived_<name>_*`. `EPICURRENTS_PROJECT` must match the currently-active project. |
| `remove_project_data <name>` | Irreversibly drop `_archived_<name>_*` tables. Prompts for confirmation. `EPICURRENTS_PROJECT` is not required. |
| `vendor_pyodide [--check] [--pyodide-version V] [--package NAME[==VERSION]] [--force]` | Download the self-hosted Pyodide runtime and the wheel closure the viewer loads into `VENDOR_DIR`, and write a pruned lock. Idempotent. Run by [bootstrap.sh](../scripts/bootstrap.sh) and, when the tree fails `--check`, by [update.sh](../scripts/update.sh), in the `vendor` service rather than `web` — see [vendored browser assets](#vendored-browser-assets). |
| `generate_compute_static` | Regenerate the compute-side vendored assets (lead-field blobs plus their manifest); lives in [compute/](../compute/management/commands/generate_compute_static.py). Needs a migrated database. Run by both deploy scripts, in the `vendor` service rather than `web` — see [vendored browser assets](#vendored-browser-assets). |
| `sync_prod_to_dev` | Copy data from production database to the development database via an intermediate JSON dump. Excludes contenttypes, auth.permission, admin.logentry, sessions.session by default. |

For the recommended end-to-end project-switch flow use [scripts/switch_project.sh](../scripts/switch_project.sh) — it sequences deactivate → env edits → activate → frontend rebuild → restart and keeps `db` and `redis` running throughout.

## Celery tasks

Defined in [tasks.py](tasks.py), scheduled in [settings/common.py](settings/common.py) under `CELERY_BEAT_SCHEDULE`:

| Task | Schedule | Purpose |
|---|---|---|
| `purge_expired_access_rights` | every 6 h | Hard-delete `AccessRight` rows whose `expires_at` has passed. Expired rights are already inactive via `AccessRight.objects.active()`; this just compacts the table. |
| `clear_expired_sessions` | daily | Delegate to Django's built-in `clearsessions`. Sessions expire on their own; the task just reclaims the rows. |

The schedule itself also includes `purge-deleted-recordings` (owned by `recordings`) and `archive-old-activity` (owned by `activity`).

## API

The endpoints owned by this app:

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/v1/health` | Liveness. Returns `{"status": "ok", "mode", "debug"}` without touching any backing service. |
| `GET` | `/api/v1/ready` | Readiness. Checks the database and cache; 200 `{"status": "ready", "checks"}` when both answer, 503 otherwise. The `web` container healthcheck polls this. |
| `GET` | `/api/v1/viewer-config` | Returns `{seed, overrides, effective}` for the active project. Any authenticated user. |
| `PUT` | `/api/v1/viewer-config` | Replace the editable overrides (request body is the flat overrides map). Staff only. |

All other `/api/v1/*` mounts are owned by other apps — see [urls.py](urls.py) for the full list.

## System user

[`system_user.py`](system_user.py) exposes `get_system_user()`, which returns the `__system__` account, creating it on first call. The account has `is_active=False` so it can never authenticate; use it as the `author` for Celery-generated or signal-generated objects that have no human author (e.g. annotations parsed from an uploaded EDF file).

## Cross-app rules this app enforces

These belong to the platform as a whole; the core app is where they live in code.

- **Multi-step write atomicity.** Endpoints that perform two or more DB writes must wrap them in `transaction.atomic()`. Use `transaction.on_commit()` for side effects (e.g. dispatching Celery tasks) that must only run after a successful commit. The canonical example is the recording upload endpoint in [recordings/api/v1/ninja.py](../recordings/api/v1/ninja.py), which creates the `Recording` and the `AccessRight` in one transaction.
- **De-identification.** Recording responses never include the integer PK or `author_id`. Annotation-type responses omit `id`, `created_at`, `modified_at`; `author_id` is kept. CRUD endpoints use `/{object_hash}` not `/{id}`. The exception is Datasets, which are identified by integer PK in viewer URLs because their content set is mutable and the PK conveys nothing about the contained data.

## Project plugin extension points

| Hook | How |
|---|---|
| Add models, endpoints, settings, middleware | Standard project structure — see [projects/example/](../projects/example/) for the scaffolded template. |
| Add a read-permission rule | `register_read_permission_extension(callable)` from your project's `apps.py::ready()`. |
| Override settings | `projects/<name>/settings.py`. Merge rules in [project_loader.py](project_loader.py). |
| Add EDF middleware | Subclass `EDFHeaderMiddleware` / `EDFSignalMiddleware` from [federation/middleware.py](../federation/middleware.py), register via `RECORDING_PIPELINES`. See [recordings/README.md](../recordings/README.md). |
| Add Python dependencies | `projects/<name>/requirements.txt` plus a lock — see [Project dependencies](#project-dependencies). |

## Project dependencies

A project needing Python packages the platform does not declares them in `projects/<name>/requirements.txt` and ships a hash-pinned lock beside it:

```bash
scripts/lock-requirements.sh --project <name>
```

The Dockerfile's `project-reqs` stage picks that lock out of `projects/` using the `EPICURRENTS_PROJECT` build argument (interpolated from `.env` by compose) and installs it as a further `pip install --require-hashes`, after the platform's own closure. A project with no `requirements.txt` needs no lock and nothing runs; a `requirements.txt` with no lock beside it fails the build with the command to generate one.

The reason for the separate lock command, rather than running `uv` against the project's requirements directly, is that the two closures overlap and pip does not treat the overlap as a conflict. `numpy` is in the platform's lock and in any project doing array work. A project lock resolved independently that names a different version installs cleanly over the platform's, reports nothing, and leaves an image running a version neither lock was audited against. `--project` resolves against the platform lock's exact versions as constraints so the overlap agrees by construction.

Regenerating the platform's `requirements.lock` invalidates every project lock resolved against the previous one. Each project lock records a `platform-versions` digest of the constraints it used, so `--check` reports that by name rather than as generic staleness. Re-run `--project` after any platform relock.

Registering a project's requirements is not needed anywhere else — no settings entry, no `apps.py` call. The presence of the file is the registration.

## Tests

```bash
pytest epicurrents/tests/
```

| File | What it covers |
|---|---|
| `test_permissions.py` | Resolution order through `get_read_access_result`, the extension protocol, and `apply_middleware` propagation. |
| `test_models.py` | `AccessRight` model + `AccessRightQuerySet` (`active`, `for_object`, `for_target`, `has_permission_for_token`). |
| `test_tasks.py` | `purge_expired_access_rights` + `clear_expired_sessions` Celery tasks. |
| `test_middleware_path_recognition.py` | Contract — every API mount in `urlpatterns` is matched by `_API_PATH_RE`. Backstop against the 2026-05-29 audit-trail middleware regression. |
| `test_middleware_audit_trail.py` | End-to-end — one GET per API mount creates an `Activity` row. |
| `test_middleware_failure_modes.py` | MIDDLEWARE ordering, `transaction.on_commit` runs inside request context, Activity-insert failure degrades gracefully, federated inbound carries `actor=None`. |
| `test_security_log_taxonomy.py` | Contract — every `log_security_event(...)` call site uses a documented event-type, and every documented event-type has at least one call site. |

## Gotchas

- **Extension order matters when grants disagree.** Step 3 (direct `AccessRight`) wins over step 4 (extensions) even when an extension would have granted more. A user with a direct `can_read=True, apply_middleware=False` row will get raw downloads even if an extension would have granted `apply_middleware=True`. This is intentional — explicit grants are the source of truth. The same precedence applies inside step 3: a direct user row beats a sanitizing group row, so granting a group member an unrelated direct right with `apply_middleware` left at its default (`False`) switches that member's reads to raw. The exposure is bounded by the grant capping in [granting.py](granting.py) — a grantor whose own read access is sanitized cannot confer `apply_middleware=False`, so only a principal already authorized for the raw bytes (author, superuser, raw-holding sharer) can create that row — but a raw-holding grantor who intends to preserve the group's sanitizing policy must set `apply_middleware=True` on the direct row explicitly.
- **Share-token rows must stay restrictive.** Always create them with `can_write=False` and `can_share=False`. Destructive operations require an authenticated session for audit attribution; token holders can't be held accountable.
- **Federated grants don't honour authorship.** A peer is never automatically granted access to objects its remote users authored on the local instance. Only explicit `AccessRight` rows with `federated_peer` set grant federated access.
- **`EPICURRENTS_PROJECT` is read at process start.** Changing the variable while the Django/Celery containers are running has no effect — restart `web`, `celery`, and `celery-beat` after switching projects, which `scripts/switch_project.sh` does automatically.
- **`apply_project_settings` is no-op when the variable is empty.** Setting it to a name that doesn't exist as a directory raises `ImproperlyConfigured` at startup, which is the right behaviour but can be confusing in dev when typos happen.
- **The API throttle fails open.** Any cache error during `check_request_throttle` lets the request through rather than erroring the API. For a defence-in-depth rate limit, an outage on the happy path is worse than a brief unthrottled window — so a misconfigured or unreachable cache silently disables throttling rather than blocking traffic. The throttle is not a substitute for the auth-specific limits (login / reset / federation), which enforce independently.
