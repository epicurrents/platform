# maintenance

Management operations requested from the web UI, carried out by something other than the web tier. A superuser asks for an operation from the Maintenance tab; the app records the request as an audited `MaintenanceJob` row and hands it to one of two executors. The **celery tier** runs a registered management command in the worker. The **host tier** writes a request file into a spool directory that a root-owned agent outside the containers picks up, for the operations that need the container runtime: updating the platform, rolling an update back. The design, the reasons a Celery task cannot do the host tier's work, and the phases are in [docs/engineering-notes/remote-maintenance-design.md](../docs/engineering-notes/remote-maintenance-design.md); this file is the app as it exists.

**The web tier never executes a host operation.** No module in this app imports `subprocess`, opens a socket to a container runtime or reads request content as code, and [tests/test_no_host_execution.py](tests/test_no_host_execution.py) scans the source for each of those. What a job runs is a property of its registration, never of the request.

Everything answers 404 until `REMOTE_MAINTENANCE_ENABLED` is on, and host-tier operations answer 403 until `REMOTE_UPDATE_ENABLED` is too. Both default off.

## Models

### `MaintenanceJob`

One requested operation. `job_id` (a UUID) is the identifier on the wire and in the spool; the integer primary key stays internal and no response carries it.

| Field | Meaning |
|---|---|
| `operation`, `executor` | The registry key and the tier it runs on, copied from the registration at request time. |
| `state`, `reason`, `step` | The state machine below; `reason` qualifies a terminal state (`command_failed`, `orphaned`, `refused_signature`, …), `step` follows the agent's progress. |
| `in_flight` | Derived from `state`. A partial unique constraint allows one true value, so one job is in flight at a time across both tiers: `compose stop celery` during an update would kill a celery-tier job. |
| `requested_by`, `package` | `SET_NULL` foreign keys; both registered for the Art. 15 export. |
| `args` | The validated arguments. Identifiers and hashes only, never a name or a path. |
| `created_at`, `started_at`, `finished_at`, `verify_deadline`, `verify_requested_at`, `rollback_requested_at`, `spool_updated_at` | Timestamps. `created_at` is a default rather than `auto_now_add`, so a row re-created from the spool keeps the time the request was made. |
| `output` | Celery tier: the command's captured output, tail-bounded by `MAINTENANCE_JOB_OUTPUT_LIMIT`, masked out of the audit trail with `register_masked_fields`, returned to superusers only. |
| `agent_version`, `installed_version_before`, `target_version`, `running_version`, `snapshot`, `post_snapshot` | What the host agent reports. |
| `last_notified_state` | Which state superusers were told about, written in the same save as the state so a restore rolls both back together. |

**States.** `requested → accepted → running → awaiting_verification → succeeded`, with `failed`, `cancelled`, `rolling_back → rolled_back | rollback_failed` off the path. The in-flight set is `requested, accepted, running, awaiting_verification, rolling_back`. The celery tier uses `requested → running → succeeded | failed`; the rest belongs to the host agent, whose transitions and refusal reasons are tabulated in the design note's spool protocol section.

**Transitions are compare-and-set.** `MaintenanceJob.transition(expect=[...], **fields)` updates the row only if it is still in one of the expected states, derives `in_flight` from the new state, and records the change with `record_modify_change`, since a bulk update fires no signal. The API, the executor and the spool sync all write the same rows; without the guard a cancel racing the worker's pickup would be overwritten.

### `MaintenancePackage`

An uploaded distribution package: `sha256` (unique; the package directory under the spool is named after it and the path is derived, never stored), `version`, `project`, `plugins`, `platform_compatible`, `built_at`, `size`, `key_id`, the parsed `manifest`, `uploaded_by`, `uploaded_at`, `state`. A `platform.update` request must name an `available` package by hash whose version is newer than the installed one, and answers 400 otherwise.

**States.** `available` from the upload; `applied` once a job that named it was confirmed, set by the spool sync; `pruned` once its files are gone, whether a superuser removed it, the keep limit pushed it out, or the sync found the directory missing. Rows are never deleted, since a job's `package` link is part of its audit record; uploading a pruned package's hash again revives its row.

**Upload** ([packaging.py](packaging.py)) takes the three files the packager writes as three multipart parts, `package`, `manifest` and `signature`. The order of checks is what makes a refusal cheap: the signature over the manifest's exact bytes against `REMOTE_UPDATE_RELEASE_KEY_PATH`, then the manifest's shape (format version 1, a usable hash, size and semver version), then what this deployment is — the version newer than `epicurrents.version.__version__`, the size within `REMOTE_UPDATE_MAX_PACKAGE_SIZE`, the project and plugin set equal to `EPICURRENTS_PROJECT` and `EPICURRENTS_PLUGINS`, and the version satisfying every installed project's and plugin's `requires_platform`, the one check the agent cannot make. Only then, and only when the spool's disk has room for twice the declared size (the agent copies the package out before it verifies it), is the tarball streamed into `packages/.incoming-<token>/`, hashed as it is written and refused when the hash or the size disagrees with the manifest, and the directory renamed to `packages/<sha256>/` holding `package.tar.gz`, `manifest.json`, `manifest.sig` and `upload.json` (the uploader's id and the time, for reconciliation). A refusal answers `{detail, reason}` with 400, 409 (`duplicate`, `key_missing`, `disk`, `spool`) or 413 (`too_large`), removes the incoming directory and is logged as `maintenance.package_rejected`. After an upload, `prune` removes the files of every package beyond the `REMOTE_UPDATE_KEEP_PACKAGES` newest, never one a job in flight refers to.

These checks are for the superuser's benefit, not the security boundary: the host agent verifies its own copy of the package against a root-owned key before anything runs. Refusing at upload turns a `failed` job a minute later into an immediate, worded answer.

**Reconciliation** (`packaging.reconcile`, run by every `spool.sync`) brings rows and directories in line both ways: a directory with no row, which is what a rollback's database restore leaves behind, or a package an operator placed by hand, gets a row from its manifest and `upload.json`; a row whose directory is gone is marked `pruned`. Incoming directories and anything not named by a 64-hex hash are not packages, and an incoming directory older than a day, which a process that died mid-upload leaves behind, is swept.

## The operation registry

[operations.py](operations.py). An `Operation` is a frozen declaration: `key` (`scope.name`), `executor` (`celery` or `host`), `label`, `description`, `args_schema` (a Ninja `Schema` the request's `args` are validated against, whose JSON schema the API publishes for the form), and for the celery tier `command` plus `command_args`, a function from the validated schema to argv. `requires_step_up` (default on) asks for a fresh credential confirmation at request time; `soft_time_limit` bounds the command.

`register_operation` refuses a malformed key, an unknown executor, a celery operation without a command, a host operation with one, and an argument schema carrying a field named `command`, `argv`, `shell`, `cmd` or `script`. [checks.py](checks.py) additionally verifies at `manage.py check` that every celery-tier command exists, because a registration is strings and a wrong one would fail from a superuser's browser with nothing to say why.

Core registrations, made from `MaintenanceConfig.ready`:

| Key | Tier | Runs | Step-up |
|---|---|---|---|
| `activity.verify_audit_integrity` | celery | `verify_audit_integrity [--derived-window-days N]` | no |
| `recordings.validate_originals` | celery | `validate_originals --json [--no-size-check]` | no |
| `recordings.refresh_signal_metadata` | celery | `refresh_signal_metadata [--dry-run]` | yes |
| `mail.send_test` | celery | `send_test_email` | no |
| `platform.update` | host | the agent applies the named package | yes |

Projects and plugins register their own from `AppConfig.ready()`. A read-only operation may turn `requires_step_up` off; anything that writes keeps it.

`mail.send_test` is here for the deployment shape this whole app exists for. A relay is configured entirely through `.env`, and on a host with no shell the only way to learn whether the credentials, the port and the sender domain are right is to send something — which otherwise means a password reset or an `awaiting_verification` notice is the first real message, and the answer arrives at the moment it matters least. It takes no arguments and reads the superuser roster for its recipients, so a request cannot name an address to send to from the deployment's own sender domain. Its report carries the relay host, the sender and the truncated recipient hashes the mail path logs, never an address: the output lands in `MaintenanceJob.output`, and the rows here are kept to ids and hashes so that nothing in them needs scrubbing when an account is erased.

## API

Mounted at `/api/v1/maintenance/`. Every operation calls `_gate_enabled` (404 while the master flag is off, and before any authentication so a disabled deployment reveals nothing about the route) and then `_require_staff` or `_require_superuser`; the gate is deliberately not named `_require_*`, which the auth sweep would otherwise mistake for a guard. Every maintenance read runs the spool sync first.

| Route | Guard | Notes |
|---|---|---|
| `GET /status` | staff | flags, installed version, `server_now`, whether the spool is writable, whether the release key is present, the agent heartbeat, the lock flag, the in-flight job, and how the caller confirms a step-up (or why they cannot) |
| `GET /operations` | staff | the registry with each operation's JSON schema and whether its tier is switched on |
| `GET /jobs`, `GET /jobs/{job_id}` | staff | newest first; `output` for superusers only |
| `GET /jobs/{job_id}/log` | superuser | the tail of the agent's log (`LOG_TAIL_BYTES`), or the captured output of a celery-tier job |
| `POST /jobs` | superuser, step-up when the operation asks | `{operation, args, password?, totp_code?}`; 202 with the job, 400 for an unknown operation, invalid arguments or a package that is not available and newer, 403 for a host operation while the host tier is off, 409 while a job is in flight. A `verify_window_minutes` the request leaves out is filled from `REMOTE_UPDATE_VERIFY_WINDOW_MINUTES` before the request file is written, so the agent applies the deployment's window rather than its own |
| `POST /jobs/{job_id}/cancel` | superuser | `requested` only, and not once the agent has written a status file |
| `POST /jobs/{job_id}/verify` | superuser, password | `awaiting_verification` only; writes the `.verify` marker |
| `POST /jobs/{job_id}/rollback` | superuser, step-up | `awaiting_verification`, or `succeeded` while the job's snapshot exists; writes the `.rollback` marker |
| `GET /packages` | staff | newest first, pruned ones included; `applicable` says whether a request may name a package now |
| `POST /packages` | superuser; 403 while the host tier is off | the three multipart parts; 201 with the package, or `{detail, reason}` with 400, 409 or 413 |
| `DELETE /packages/{sha256}` | superuser | removes the files and marks the row `pruned`; 409 for a package a job in flight names or one already removed |

The row is created first and the executor engaged in `transaction.on_commit`: a Celery dispatch for the celery tier, the request file for the host tier. Responses carry usernames and hashes, never primary keys or paths.

**Step-up** is `user.stepup.confirm_step_up` (see [user/README.md → Step-up confirmation](../user/README.md#step-up-confirmation)): the password, plus a TOTP or recovery code when the account has a confirmed second factor. An account with no usable password confirms with its factor alone, and without one cannot use the feature, which `GET /status` reports so the UI can say why.

## The maintenance lock

`update.sh` writes `update/maintenance.json` before it changes anything and removes it at exit; the host agent owns the same file across a remote update. [lock.py](lock.py) reads it (a one-second process-local cache, failing closed on a file it cannot parse) and `MaintenanceLockMiddleware` in [epicurrents/middleware.py](../epicurrents/middleware.py) answers `503 {"detail": "maintenance", "phase", "since", "expected_until", "message"}` with a `Retry-After`. The policy by phase:

| Phase | Non-superuser | Superuser | Always |
|---|---|---|---|
| `updating`, `rolling_back` | every API request refused | safe methods pass, unsafe refused | `/api/v1/health`, `/api/v1/ready`, and every safe non-API request (the SPA document, assets, the federation key document) pass; an unsafe non-API request is refused |
| `verifying` | safe methods pass; unsafe refused except login and logout | exempt | as above |

The middleware sits after `AuthenticationMiddleware` and before the throttle and the audit middleware, so a refused request burns no budget and writes no `Activity` row; [tests/test_maintenance_lock.py](tests/test_maintenance_lock.py) pins the order. A file rather than a row because the rollback restores the database dump and would erase a row half-way through the operation it announces.

## The spool

`MAINTENANCE_SPOOL_PATH`: `./update` in the deployment root, which `update.sh` already excludes from every sync and snapshot; the production overlay bind-mounts it and sets the path to the mount point. The file layout and the state machine are in the design note's spool protocol section; [spool.py](spool.py) is the reader and writer on this side, every write a temporary file renamed into place and every JSON file carrying `protocol: 1` (a newer protocol is ignored with a warning).

The other side of the spool is the host agent, [scripts/updater/epicurrents-updater.sh](../scripts/updater/epicurrents-updater.sh), shipped in every distribution as `updater/` and installed once by the operator; [scripts/updater/README.md](../scripts/updater/README.md) describes what a tick does, the refusal reasons a `failed` request can carry (`refused_signature`, `refused_hash`, `refused_version_not_newer`, …) and the two states that need a shell. Its heartbeat file carries `capabilities`, the operation keys it carries out, and `updater_script`, the `UPDATER_SCRIPT_VERSION` of the `update.sh` copy it runs; `agent_summary` passes both through to the status endpoint.

`spool.sync()` is the reconciliation: it applies each newer `status.json` to its row, re-creates a row for a request file that has none, and fails an in-flight host-tier row older than a minute that has neither file as `orphaned`. The spool, not the database, is the record of a host-tier job, because a rollback restores the pre-update dump and erases every row written since; re-creating rows from the request files is what makes the job page survive its own rollback. It runs under a five-second cache lock (every maintenance read, the top of every write, and the `sync_spool` beat task call it) and opens an audited scope — `maintenance.job.sync` — only when it has something to write, so the minute tick does not inflate the audit trail. A state the agent reported is also written to the security log as `maintenance.job_state`, which a restore cannot erase.

## Celery tasks

| Task | Schedule | Does |
|---|---|---|
| `run_job(job_pk)` | dispatched on commit of a celery-tier request, with the operation's `soft_time_limit` | Runs the registered command with the typed argv inside `with_system_activity("maintenance.job.run", target=job)`, captures output, records `succeeded` or `failed` (`command_failed`, `timeout`, `refused_operation`), notifies. Exits quietly when the row is no longer `requested`, which is how a cancel before pickup works. |
| `sync_spool` | every 60 s | `spool.sync()`. |
| `prune_spool` | daily | Removes the spool files of host-tier jobs finished more than 30 days ago; rows stay, they are the audit record. |

**Notifications** ([notify.py](notify.py)): every active superuser gets a push through `send_push_to_user` and, when `EMAIL_BACKEND` is not the console backend, a mail, once per attention state (`awaiting_verification`, `succeeded`, `failed`, `rolling_back`, `rolled_back`, `rollback_failed`). Delivery failures are logged and never fail the job.

## Settings

| Setting | Default | Meaning |
|---|---|---|
| `REMOTE_MAINTENANCE_ENABLED` | `False` | Mounts the API and the admin tab. |
| `REMOTE_UPDATE_ENABLED` | `False` | Makes host-tier operations requestable. Needs the agent installed; the check `maintenance.W001` warns when it is on without the master flag. |
| `REMOTE_UPDATE_VERIFY_WINDOW_MINUTES` | 30, clamped 5–1440 | Default verification window. |
| `REMOTE_UPDATE_MAX_PACKAGE_SIZE` | 1 GiB | Upload cap. |
| `REMOTE_UPDATE_KEEP_PACKAGES` | 2 | Packages kept beside the applied one. |
| `REMOTE_UPDATE_RELEASE_KEY_PATH` | `RELEASE_KEY.pub` in the deployment root | The key the upload endpoint verifies against. |
| `MAINTENANCE_SPOOL_PATH` | `BASE_DIR/update` | The spool. |
| `MAINTENANCE_JOB_OUTPUT_LIMIT` | 64 KiB | Output kept per celery-tier job. |
| `API_THROTTLE_RATE_MAINTENANCE` | 120/min | Throttle scope of the jobs routes; wide enough for the tab to poll a job, since the step-up lockout is the bound on guessing. |

The booleans read through `env_bool`.

## Audit and security log

Verbs: `maintenance.status.read`, `maintenance.operation.list`, `maintenance.job.{list,read,log,create,cancel,verify,rollback}`, `maintenance.package.{list,create,delete}`, and the non-request `maintenance.job.run`, `maintenance.job.sync` and `maintenance.package.sync`, all in the registry in [activity/README.md](../activity/README.md#verb-registry). Security events: `maintenance.job_requested`, `maintenance.rollback_requested`, `maintenance.job_state`, `maintenance.package_uploaded`, `maintenance.package_rejected`, `maintenance.package_removed`, `auth.stepup_failed`. Every job and package endpoint targets the row, so the Activity metadata carries only what the row does not: the package hash on a job request, the byte count on a log read, nothing on a package upload or removal; the security events carry `job_id`, the operation key, the state, a package hash and its version, never a path.

## The frontend

The Maintenance segment of the administration tabs (`/admin/maintenance`, `/admin/maintenance/{job_id}`) is the client; [frontend/README.md → Maintenance](../frontend/README.md#maintenance) describes it. Two contracts it depends on: the status endpoint's 404 while the master flag is off, which is how the tab decides not to render, and the `503 {"detail": "maintenance", …}` body of the lock, which the SPA's HTTP layer recognises by that literal.

## Extension points

| Hook | How |
|---|---|
| Add an operation | `register_operation(Operation(...))` from your `AppConfig.ready()`. Celery tier: name one of your management commands and map the schema to argv. Host tier: only if the agent allowlists the key, which means a platform release. |
| React to a job | Nothing to hook; read `MaintenanceJob` rows, or the `maintenance.job_state` events in the log stream. |

## Gotchas

- **A sync between the row's commit and its on-commit file write** would see an in-flight row without a file. The orphan rule therefore ignores rows younger than a minute; do not shorten `ORPHAN_GRACE` below the time an on-commit write can take.
- **`status.json` is applied by `updated_at`, not by arrival.** A file whose timestamp is not newer than the row's `spool_updated_at` is ignored, so an agent whose clock runs behind the web tier's would have its updates dropped; the agent stamps from its own clock and the comparison is against its own earlier stamps, which keeps a skew harmless as long as it is constant.
- **The celery-tier executor runs eagerly in tests** (`CELERY_TASK_ALWAYS_EAGER`), inside the request's own on-commit callback. Use `django_capture_on_commit_callbacks(execute=True)` to see it run.
- **The lock cache.** A test that writes the flag and requests in the same second must call `maintenance.lock.invalidate_cache()`; the `write_flag` fixture does.
- **A package row without its directory is pruned by the next sync.** The reconciliation treats a missing directory as a removed package, which is right for a deployment and a trap for a test that creates a `MaintenancePackage` row directly: the next maintenance request marks it `pruned` and answers "no uploaded package has that hash". [tests/conftest.py](tests/conftest.py) has `place_package_files` for that.
- **The sync lock spans requests.** `spool.sync` runs at most once per five seconds per process; a test that uploads and then expects the reconciliation to have run again calls `spool.sync(force=True)`.
- **A cancel can lose to the agent's pickup.** `cancel_job` checks for a status file, marks the row cancelled and removes the request; the agent may write `accepted` between the check and the removal. The spool is authoritative, so the next sync moves the row back to `accepted` and the update proceeds. The window is a tick's read of one file, and the job page shows what happened; do not "fix" it by having the API delete the status file, which is the agent's.
