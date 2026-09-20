# Remote maintenance: shell-less platform updates with verified rollback — design note

**Status: design (2026-09-19); phase 0 (the script fixes) and phase 1 (signed packages and the hardened manual path) landed on 2026-09-20; the `maintenance` app, the lock middleware and the host agent have not.** Some deployments will run on hosts where the owner has no SSH and only the web UI. Today an update needs a shell: drop a tarball in `./update/`, run `./update.sh`, run `./update.sh --rollback` if it goes wrong. This note designs the superuser-driven equivalent — upload a signed package, apply it, confirm it works, roll back automatically if nobody confirms within a window — gated behind explicit settings, and shaped so the same machinery later carries other management operations. The two questions a reader most likely brings, "why not a Celery task" and "what happens to the data written while the update was in flight", are answered in the first two sections.

> **Revision note (2026-09-19, same day).** A clean-slate review of the first draft found two defects that would have made the first real rollback fail or strand the deployment, one privilege hole in how the agent trusted the deployment tree, and a dozen smaller gaps. Several of the defects live in the shipped `update.sh`, not in the plan, so they are listed as [prerequisites](#prerequisites-defects-in-the-shipped-scripts) and were fixed first, before any phase below. The rest are folded into the sections they affect; where a decision changed, the list at the end says so.

## Prerequisites: defects in the shipped scripts

Each of these was in [scripts/update.sh](../../scripts/update.sh) when the design was written and bit the manual path as well as the planned one. They were fixed first, as their own change on 2026-09-20, because the plan's rollback guarantees rest on them and the fakebin suite cannot see most of them; the restore is covered against a real PostgreSQL in [scripts/tests/test_update_postgres.py](../../scripts/tests/test_update_postgres.py), the rest in [scripts/tests/test_update.py](../../scripts/tests/test_update.py). The list stays as the record of what was found.

1. **Rollback leaves behind every table the failed release created.** `pg_dump --clean` emits DROP statements only for objects in the dump. After a rollback the tables are still there while the restored `django_migrations` says their migration never ran, so the next update, or a retry of the same package, dies in `migrate` on `relation already exists`, after the snapshot, and rolls back again. On a host without shell that loop is permanent. The restore must drop and recreate the schema inside the same single transaction that replays the dump. The test for this needs a real Postgres: apply a migration that adds a table, roll back, and run `migrate` again.
2. **Overdue backups can block the restore.** The borg container runs on its own schedule inside the stack and holds database connections while dumping; a DROP in the restore transaction waits behind it indefinitely. `update.sh` stops `borg` for the span of an update and a rollback and starts it again at the end.
3. **The recreate at step 7 starts `celery-beat` unconditionally, and the database scheduler fires every overdue periodic task at startup.** A three-hourly purge that came due during the build runs within seconds of the recreate and unlinks files that a rollback then cannot restore. `update.sh` gains `--skip-beat`, and the manual path documents that beat is started once the operator is satisfied.
4. **The script derives its root from its own location.** Run from a copy outside the deployment root it resolves to the copy's parent directory. It gains `--root DIR`, which the agent needs because it runs a root-owned copy (see [the host agent](#the-host-agent)).
5. **Rollback restores neither the vendored asset trees nor the collected static files and re-runs none of the checks.** Static storage is not manifest-based, so stale hashed files are harmless, but a newer Pyodide closure vendored by the failed release stays under the old viewer. Rollback re-runs the vendor checks and collectstatic after the rebuild, and health-checks the way an update does.
6. **The database dump is taken before the image build, which runs for minutes with the services still up.** Every write in that gap is lost on rollback. The lock flag described below closes it for both paths and ships with the script changes rather than waiting for the app.

The manual archive glob, `./update/epicurrents*.tar.gz`, ignores packages in subdirectories; once uploads land under `update/packages/`, the operator documentation says so.

## Why the web tier cannot perform the update

In production the code is baked into the images and no container mounts the docker socket or runs privileged ([docker-compose.prod.yml](../../docker-compose.prod.yml) drops the `.:/code` bind mount with `volumes: !override`). A Celery worker cannot rebuild images, stop `web`, or restore a database dump. Giving it the socket would hand root on the host to any web compromise, and the [intrusion-detection note](intrusion-detection-design.md) lists "socket mounted nowhere" as a rule that the auditd watch list enforces.

So the shape is: **the web tier writes an audited intent and uploads the file; a small root-owned host agent outside the containers picks the intent up and drives the existing [scripts/update.sh](../../scripts/update.sh).** Celery keeps a role for a second tier, in-container management commands that need no host privilege. Two consequences follow and are accepted:

- The release that ships this must itself be installed by someone with shell, once, and that person also installs the agent. Every later release can be applied from the UI.
- Rollback restores the pre-update database dump. Whatever the live database gained between that dump and the rollback is lost from it, which is why the platform is locked for the whole span.

## Architecture

```
superuser browser ──► Django (web, uid 1000)          host agent (root, systemd timer, 1 min)
   upload package      writes update/packages/<sha>/     verifies signature + version + disk
   request update      writes update/jobs/<id>.json  ──► runs ./update.sh --archive … --require-signature
   confirm / rollback  writes <id>.verify / <id>.rollback   health-gates, writes <id>.status.json + <id>.log
   poll status         projects status.json into DB   ◄──  rolls back on deadline / failure
```

- **Spool directory** `./update/`, which already exists as the tarball drop directory and is already excluded from the update overlay, the code snapshot and the rollback `rsync --delete`, so it survives both directions. Bind-mounted `./update:/data/update` read-write into `web` and read-only into `celery` under the production overlay. **The spool is the authoritative job state**: the rollback restores the database dump and would erase any DB-only status written after it. Django keeps `MaintenanceJob` rows as the audited request plus a projection of the spool, and re-creates rows the restore erased.
- **Signed packages, verified before extraction, from a copy the web tier cannot reach.** The packager writes a manifest (version, sha256, project, plugins, compatible range, minimum updater version) and a detached Ed25519 signature; the public key ships in the package and is copied into root-owned agent configuration at install, trust on first install. The agent copies the tarball, manifest and signature out of the spool into a root-owned temporary directory, verifies the copy with `openssl pkeyutl`, checks that the manifest's project and plugins match the deployment's `.env`, and extracts from the copy — never from the spool, where the web tier could swap the file between the check and the extraction. Django's check at upload (with `cryptography`, already a dependency) exists so the superuser gets an immediate error; it is not the boundary.
- **Version monotonic.** A request names an already-uploaded package by hash, never a version. The agent refuses a package whose version is not greater than the installed one. This is what stops a compromised superuser session from turning the feature into a downgrade-to-vulnerable endpoint. The cost is release discipline the repository does not yet have: every package meant to be applied remotely carries a strictly greater `__version__`, hotfixes included, and the packager refuses to sign a package whose version equals the last tag it can see.
- **Two executor tiers** in a new app `maintenance`: `host` (update, rollback; later backup and restore) run by the agent, and `celery` (registered management commands such as `verify_audit_integrity`, `validate_originals`, `refresh_signal_metadata`) run by a Celery task through `call_command`. An **operation registry** declares key, executor, typed argument schema and step-up requirement; there are no free-form command strings anywhere. Projects and plugins register operations from `AppConfig.ready()`.
- **Gates.** `REMOTE_MAINTENANCE_ENABLED` (master, default off; the whole API answers 404 when off), `REMOTE_UPDATE_ENABLED` (host tier, default off), plus `ENABLED=1` in the agent's root-owned configuration. Requests need superuser, password re-confirmation plus TOTP when enrolled, are rate limited, security-logged, and push- or email-notify every superuser.
- **Verification window.** After `update.sh` exits 0 and the gate passes — `/api/v1/ready` answers 200 and the running version equals the manifest version, read with `compose exec -T web python -c` rather than through a public endpoint, since the ROADMAP's federation entry refuses to advertise the version unauthenticated — the job is `awaiting_verification` with a deadline. Confirm → `succeeded`. Rollback request, deadline, or gate failure → the agent takes a `post-update-*` snapshot so window data stays recoverable, then runs `update.sh --rollback --yes --snapshot <the update's own snapshot>`.

## Holes found in the first cut and the fixes adopted

| Problem | Fix |
|---|---|
| A public version endpoint would advertise the release to the internet | Agent reads the version via `compose exec`; the UI reads it from the staff-authenticated status endpoint |
| A post-update snapshot named `pre-update-*` would be the one `--rollback` picks | `update.sh --snapshot LABEL` writes `backups/<LABEL>-<stamp>/`; `--rollback --snapshot NAME` restores a named one |
| The DB restore erases rows written after the dump, including a "rollback requested" row | Rollback is a marker on the same job; spool sync upserts rows by `job_id`; sync runs before every write endpoint |
| The auth sweep in [epicurrents/tests/test_api_auth_sweep.py](../../epicurrents/tests/test_api_auth_sweep.py) treats any `_require_` substring as a guard, so a `_require_enabled` gate would mask a missing superuser check | The gate helper is named `_gate_enabled`; a scan test asserts every operation calls both it and a tier guard |
| Docker creates a missing bind-mount source as root; a checkout deployment has no `update/` | Installer, shared bootstrap body and the packaged `start.sh` preflight create `update/`, `update/packages`, `update/jobs` owned by uid 1000 |
| The agent runs as root, so Django could never prune its files | Agent writes every file as `tmp → chown 1000:1000 → mv` |
| `update.sh`'s health poll is a warning with exit 0, and rollback does no health check | Agent runs its own gate after both update and rollback |
| `update.sh`'s podman branch hardcodes `sudo -E`, which may be absent under a root unit | Prefix `sudo -E` only when `id -u` is not 0 |
| A package may need a newer `update.sh` than the one installed | `min_updater_version` in the manifest against `UPDATER_SCRIPT_VERSION` in the script; refuse with a clear reason |
| An empty `pii_fields` is a check error in [activity/checks.py](../../activity/checks.py) | Job and package rows carry only ids, so they get the export-relation registration only; `output` is masked from the audit trail |
| Sessions are DB-backed, so sessions created after the dump vanish on rollback | The SPA treats 502, 503 and network errors as an outage, re-fetches `/me` after recovery, and only a real 401 routes to login |
| The `PROXY_MAX_BODY_SIZE` boot guard only knows the recordings cap | Extend `_guard_proxy_body_limit` in [epicurrents/apps.py](../../epicurrents/apps.py) with the package cap |
| The agent verified the package in the spool and then extracted it from the spool, a directory the web tier writes | Copy out to a root-owned directory, verify the copy, extract the copy |
| The agent ran the uid-1000 `update.sh` from the deployment tree as root | Run a root-owned copy extracted from the verified package, with `--root` |
| The drain watched `inspect active` only | Watch active, reserved and the queue length, with beat already stopped; drain again before a rollback |
| The system `openssl` on the packaging machine is LibreSSL, which cannot sign Ed25519 through `pkeyutl` | Sign through a Python helper using `cryptography`; keep `openssl` for host-side verification behind a version guard |
| Password re-confirmation answers 409 for OIDC-provisioned accounts, which the admin API can promote to superuser | Step-up accepts TOTP alone for accounts without a usable password; an OIDC superuser without TOTP cannot use the feature and the UI says so |
| A stale job left the platform locked until someone with shell arrived | A stale job whose log shows a snapshot rolls back to it; only a stale job with no snapshot stops dead |

## Maintenance lock: suspending the platform while an update is in flight

**Why it is needed beyond UX.** `update.sh` takes the database dump at step 2, builds images at step 3 (minutes on a small host) and only stops `web` at step 4. Rollback restores that dump, so every write between the dump and the stop is lost even without a verification window. And the beat purge tasks unlink files ([recordings/tasks.py](../../recordings/tasks.py), [media/tasks.py](../../media/tasks.py)); a purge that runs during the window followed by a rollback restores the rows but not the files. The lock closes both gaps and gives users a message instead of a broken page.

**Mechanism: a flag file, not a database row.** A row would be erased by the very restore the lock protects, un-locking the platform mid-rollback. `update/maintenance.json` carries `{protocol, phase: "updating" | "verifying" | "rolling_back", job_id, since, expected_until, message}`. `update.sh` writes it at step 0 and removes it at exit, so the manual path benefits too; the agent passes `--keep-lock` and owns the lifecycle: `updating` at acceptance, `verifying` at `awaiting_verification`, `rolling_back`, removed at any terminal state. A `stale` job leaves it in place, which is the right failure direction.

**Django side.** `MaintenanceLockMiddleware` in [epicurrents/middleware.py](../../epicurrents/middleware.py), placed after `AuthenticationMiddleware` (it needs `request.user`) and before `ApiThrottleMiddleware` (blocked requests create no Activity rows and burn no throttle budget), with the ordering pinned in the middleware failure-mode tests. It stats the flag with a one-second process-local cache and answers `503 {"detail": "maintenance", "phase", "since", "expected_until", "message"}` plus `Retry-After`. Policy by phase:

| Phase | Non-superuser | Superuser | Always exempt |
|---|---|---|---|
| `updating`, `rolling_back` | every request 503 (`web` is down for part of it anyway) | safe methods pass, so progress and the log stay visible; unsafe methods 503 | `/api/v1/health`, `/api/v1/ready`, `/.well-known/`, the SPA document, and the asset paths Django serves without the proxy overlay (`/static/`, `/viewer/`, `/vendor/`), so the login page renders and shows the message and federation peers keep the key document |
| `verifying` | safe methods pass; unsafe methods 503 except `POST /api/v1/user/login` and `logout` | fully exempt, and the UI says their writes are lost on rollback | as above, plus `/api/v1/maintenance/*` |

Federated peers receive the same 503 with `Retry-After`; the federation client must treat it as transient rather than as a suspended grant, which is a point to verify during implementation.

**Celery side.** Beat is the only autonomous source of work, so the agent stops `celery-beat` before it drains and `update.sh --skip-beat` leaves it stopped through the recreate; the agent starts it again at the terminal state. The drain waits up to `DRAIN_TIMEOUT` (60 s) for active tasks, reserved tasks and the broker queue to all reach zero, so an upload dispatched just before the lock finishes its staging-to-permanent move before the dump and nothing runs on the new code that a rollback would then half-undo. The same drain runs before a rollback, because tasks the new release enqueued would crash on the old worker. The worker itself stays up during the window so an explicit superuser action still works. `borg` is stopped for the same span (prerequisite 2). Nothing in the tasks changes.

**Frontend.** A response interceptor on [frontend/src/lib/http.ts](../../frontend/src/lib/http.ts) catches 503 with `detail === "maintenance"` and feeds a `maintenance` Pinia store. [App.vue](../../frontend/src/App.vue) shows a global banner with phase and expected end; the login view shows a callout and disables submit while the phase is `updating`; one `brand` toast per phase change, not one per request. While locked the SPA polls `/api/v1/user/me` every 10 s to notice release. The transition to `verifying` forces a reload, otherwise the superuser tests the new API through the old bundle, and so does the transition to `succeeded` for everyone else.

## Orphaned files: what a release removes must leave the deployment

**The gap.** Archive mode overlays with `rsync -a` and no `--delete`; only `frontend/dist` and `frontend/viewer-dist` are emptied first. A module, command, task file or migration the new release deleted stays on disk and is baked into the image by `COPY . .`, since `.dockerignore` excludes none of the app directories. Mostly inert, but not always: a removed migration alongside its replacement makes `migrate` refuse with conflicting migrations, a module turned into a package leaves both shapes present, and a stale `tasks.py` in a still-installed app is autodiscovered. Repo mode has no such problem because `git pull` deletes, and rollback is already a replace with `--delete`.

**Design: the package owns a file list, and only files the previous package shipped are ever deleted.** The packager writes `FILELIST` (sorted relative paths of every regular file in the package) into the tarball root, so the signed sha256 covers it. `update.sh` keeps the installed release's copy at `<root>/.epicurrents-files`, a root file, so the code snapshot carries it and rollback restores it. After the overlay it computes old minus new, deletes each candidate that is a regular file and matches nothing on the protect list, then removes directories left empty. Runtime-generated files — `generate_compute_static` output, the vendored viewer edition, Pyodide — are never candidates because no package listed them; operator-added files likewise. The protect list is defensive on top of that: `.env`, `backups/`, `update/`, `static/`, `frontend/vendor/`, `frontend/node_modules/`, `.git/`, `recordings/converters/*/` (vendored proprietary converters), `projects/*` containing `.git`, `local/`, `testdata/`.

**First transition.** A deployment updated from a release without `.epicurrents-files` has no old list, so that update deletes nothing and logs that orphan pruning starts with the next one. `--check-archive` additionally reports informational candidates (files under the package's top-level directories that the new package does not carry, minus the protect list) so an operator with shell can clean by hand and the agent log shows them.

## Spool protocol

`protocol: 1` in every JSON file, UTC ISO-8601 timestamps, every write is tmp + rename.

```
update/agent.json                 agent heartbeat: version, enabled, runtime, last_run, capabilities
update/lock                       agent: {pid, boot_id, job_id, since}; flock for the whole tick
update/maintenance.json           lock flag, see above
update/packages/.incoming-<tok>/  Django partial upload, never a package
update/packages/<sha256>/         package.tar.gz, manifest.json, manifest.sig, upload.json
update/jobs/<id>.json             Django request: operation, requested_by_id, args{package_sha256, target_version, verify_window_minutes}
update/jobs/<id>.verify|.rollback Django markers: {at, by_user_id}
update/jobs/<id>.status.json      agent: state, reason, step, timestamps, verify_deadline, snapshot, post_snapshot, versions
update/jobs/<id>.log              agent: tee of update.sh, head-truncated at 8 MiB
```

**State machine**, writer in parentheses:

| From | To | Writer | Trigger |
|---|---|---|---|
| — | `requested` | Django | `POST /jobs`; request file written on commit |
| `requested` | `cancelled` | Django | cancel, only while no status file exists |
| `requested` | `accepted` | agent | protocol, operation allowlisted, package present, sha256 matches request and manifest, `--check-archive` passes, disk free, no `.git`, `ENABLED=1` |
| `requested` | `failed` | agent | any check misses; `reason` is one of `refused_signature`, `refused_hash`, `refused_version_not_newer`, `refused_incompatible`, `refused_disk`, `refused_checkout`, `refused_updater_too_old`, `refused_operation`, `refused_protocol`, `refused_disabled` |
| `accepted` | `running` | agent | `update.sh` started; `step` follows the `::step=` lines |
| `running` | `failed` | agent | exit ≠ 0 with no `::snapshot=` seen; nothing changed (`update_failed_before_snapshot`) |
| `running` | `rolling_back` | agent | exit ≠ 0 after the snapshot (`update_failed`), or exit 0 but the gate fails within `HEALTH_TIMEOUT` (`health_failed`) |
| `running` | `awaiting_verification` | agent | gate passed; `verify_deadline` set from the agent's clock |
| `awaiting_verification` | `succeeded` | agent | `.verify` marker |
| `awaiting_verification` | `rolling_back` | agent | `.rollback` marker (`requested`) or deadline passed (`deadline`) |
| `rolling_back` | `rolled_back` | agent | post-update snapshot, then `--rollback --yes --snapshot <name>` exit 0, then `/ready` 200 |
| `rolling_back` | `rollback_failed` | agent | either step fails; needs shell |
| `succeeded` | `rolling_back` | agent | `.rollback` marker while the job's pre-update snapshot still exists (`late`); the same data-loss warning applies and the UI repeats it |
| any in-flight | `rolling_back` | agent, next tick | lock pid dead or boot id differs (`stale`) and the log shows a `::snapshot=` line; a host reboot mid-update must not leave a shell-less instance locked when a rollback is well defined |
| `requested`, `accepted`, `running` | `failed` | agent, next tick | stale with no snapshot in the log (`stale`); nothing was changed, and the agent never resumes a half-run update |
| any in-flight | `failed` | Django sync | neither request nor status file exists (`orphaned`) |

In-flight set: `requested, accepted, running, awaiting_verification, rolling_back`. One job is in flight at a time across both tiers, enforced by a partial unique constraint on the row and by the spool lock; `compose stop celery` would kill a celery-tier job, which is why the tiers share the limit.

**Reconciliation.** `maintenance.spool.sync()` upserts a row per `jobs/*.json` by `job_id`, re-resolving `requested_by` by id, applies a newer `status.json` under `with_system_activity`, saves only on change so the minute tick does not inflate `ObjectChangeLog`, and fires each state's notification once. It runs in every maintenance read (throttled by a five-second cache lock, because beat is down during the outage), in a beat task every 60 s, and at the top of every write endpoint so a stale `running` row cannot block a new request after a rollback.

## The `maintenance` app

**Models.** `MaintenancePackage` (`sha256` unique and the identifier, path derived and never stored; `version`, `project`, `plugins`, `platform_compatible`, `built_at`, `size`, `key_id`, `manifest`, `uploaded_by`, `uploaded_at`, `state`). `MaintenanceJob` (`job_id` UUID as the URL identifier; `operation`, `executor`, `state`, `reason`, `step`, `in_flight` with the partial unique constraint from the `ImportJob` pattern in [recordings/models.py](../../recordings/models.py); `requested_by`, `package`, `args` with ids and hashes only; the timestamps; `output` for the celery tier, tail-bounded and masked with `register_masked_fields`; version fields; `last_notified_state`). Both user FKs are `SET_NULL` and registered with `register_export_relation`; there is nothing to scrub, so no erasure registration.

**Registry.** A frozen `Operation(key, executor, label, description, args_schema, command, command_args, requires_step_up, soft_time_limit)`; duplicates raise; a system check verifies every celery-tier `command` exists. Core registrations: `activity.verify_audit_integrity`, `recordings.validate_originals`, `recordings.refresh_signal_metadata`, `platform.update`.

**API** at `/api/v1/maintenance/`. Every operation calls `_gate_enabled` (404 when the master flag is off) and then `_require_staff` or `_require_superuser`; host-tier writes also answer 403 when `REMOTE_UPDATE_ENABLED` is off.

| Route | Guard | Notes |
|---|---|---|
| `GET /status` | staff | flags, installed version, `server_now`, spool writable, agent heartbeat, in-flight job, release key present |
| `GET /operations` | staff | registry with a JSON schema per operation |
| `GET /jobs`, `GET /jobs/{job_id}` | staff | `output` only for superusers; no pks or paths in responses |
| `GET /jobs/{job_id}/log` | superuser | tail of the spool log, byte-bounded |
| `POST /jobs` | superuser + step-up | 202; 409 when in flight; celery tier dispatches on commit, host tier writes the request file on commit |
| `POST /jobs/{job_id}/cancel` | superuser | `requested` only |
| `POST /jobs/{job_id}/verify` | superuser + password | `.verify`; `awaiting_verification` only |
| `POST /jobs/{job_id}/rollback` | superuser + step-up | `.rollback`; `awaiting_verification` only |
| `GET/POST /packages`, `DELETE /packages/{sha256}` | staff / superuser | three multipart parts, streaming with a cap and incremental sha256 into `.incoming-<token>/`, verified, then renamed into `<sha256>/` |

Step-up lives in the `user` app as `confirm_step_up(request, user, *, password=None, totp_code=None)`, generalising `_confirm_password` in [user/api/v1/two_factor.py](../../user/api/v1/two_factor.py); it emits `auth.stepup_failed`. An account with a usable password confirms with the password plus TOTP when enrolled; an account without one (OIDC-provisioned) confirms with TOTP alone, and without TOTP it cannot request anything here, which the status endpoint reports so the UI can say why.

Upload checks the package version against the active project's `requires_platform` pin with `satisfies`, which only Django can evaluate; the manifest's own `platform_compatible` is informational. The agent cannot make this check, and without it the failure surfaces late, as a `manage.py check` error inside `migrate` after the snapshot.

**Settings.** `REMOTE_MAINTENANCE_ENABLED`, `REMOTE_UPDATE_ENABLED`, `REMOTE_UPDATE_VERIFY_WINDOW_MINUTES` (30, clamped 5–1440 on both sides), `REMOTE_UPDATE_MAX_PACKAGE_SIZE` (1 GiB), `REMOTE_UPDATE_KEEP_PACKAGES` (2), `REMOTE_UPDATE_RELEASE_KEY_PATH`, `MAINTENANCE_SPOOL_PATH` (`BASE_DIR/update`; `/data/update` under the production overlay), `MAINTENANCE_JOB_OUTPUT_LIMIT`. Booleans through `env_bool`. Throttle scopes for the jobs and packages routes.

**Audit and security log.** Verbs `maintenance.status.read`, `maintenance.job.{list,read,log,create,cancel,verify,rollback}`, `maintenance.package.{list,create,delete}`, and the non-request `maintenance.job.run` and `maintenance.job.sync`. Security events `maintenance.job_requested`, `maintenance.job_refused`, `maintenance.package_rejected`, `maintenance.job_state`, `auth.stepup_failed`. Metadata carries `job_id`, operation key, package hash and target version, never a path.

**Tests worth naming.** A source scan over code tokens, not docstrings, that nothing in the app imports `subprocess`, `os.system` or `os.exec*` or calls out to a container runtime, and that every API operation calls the gate plus a tier guard; the lock middleware against each phase, caller class and method; spool sync recreating rows after a simulated database restore; the celery executor running under an audited scope with `output` masked; refusal at upload for each manifest failure, with no residue in the packages directory.

## The host agent

`scripts/updater/epicurrents-updater.sh` with a `Type=oneshot` service (`TimeoutStartSec=0`, since a rollback rebuilds every image) fired by a one-minute timer, plus `install-updater.sh`, packaged as `<dest>/updater/`. Root, DB-agnostic bash under `flock`, runtime detection copied from `update.sh`, `HOST_PORT`, `EPICURRENTS_PROJECT` and `EPICURRENTS_PLUGINS` read from `.env`, JSON handled with `python3` rather than `jq`, which a minimal host does not ship. It never calls `manage.py` except for the version read through `compose exec`, and never reads request content as code. Configuration in `/etc/epicurrents-updater/` (0700): `config` (`DEPLOY_ROOT`, `ENABLED=0`, `ALLOW_CHECKOUT=0`, `MIN_FREE_BYTES`, `HEALTH_TIMEOUT=300`, `DRAIN_TIMEOUT=60`), `release.pub`, and `update.sh`, the root-owned copy the agent runs. Disk preflight: free space at least twice the package size plus 1 GiB. A deployment root containing `.git` is refused unless allowed; v1 targets distribution deployments.

**What the agent trusts.** The deployment root is owned by uid 1000, the uid every container runs as, so nothing the agent executes or interprets may come from there unverified. The package is copied out of the spool and verified as a copy; `update.sh` is run from the agent's own copy with `--root DEPLOY_ROOT`, refreshed from the verified package after each successful update; the compose files are read from the deployment root because `update.sh` has to, which is the same trust the manual path already places in them. Every state transition is also written to the system journal with `logger -t epicurrents-updater`, so the shipper on the evidence host sees the timeline while Django is down.

Tick order on an accepted job: write the lock (`updating`) → stop `celery-beat` and `borg` → drain the worker → `update.sh --root … --archive <verified copy> --require-signature --release-key /etc/epicurrents-updater/release.pub --require-newer --skip-beat --keep-lock --yes`, teeing the log and following the `::` lines → gate → lock to `verifying` → on the terminal state, start `celery-beat` and `borg` and remove the lock. A rollback repeats the drain first.

Installer wiring: the packaged `prepare-host.sh --with-updater`, a `step_updater` in the shared [scripts/lib/bootstrap_steps.sh](../../scripts/lib/bootstrap_steps.sh), and spool directory creation in the packaged `start.sh` preflight. RHEL recipients run the installer by hand, as with the existing Debian-only host preparation.

## Signing and packaging

`scripts/make-bootstrap-fixture.sh --dist --tarball --sign-key PATH` (Ed25519 PEM) writes `<pkg>.tar.gz.manifest.json` with sorted keys — `manifest_version`, `package`, `sha256`, `size`, `version`, `platform_compatible` from `compatible_range`, `project`, `plugins`, `built_at`, `min_updater_version`, `key_id` — and `<pkg>.tar.gz.manifest.sig` (raw Ed25519 signature, base64). Signing goes through [scripts/lib/release_sign.py](../../scripts/lib/release_sign.py) on `cryptography`, because the packaging machine's system `openssl` is LibreSSL and cannot sign Ed25519 through `pkeyutl`; verification on the host is `openssl pkeyutl -verify -rawin` behind an OpenSSL 3 version guard, since that is what a minimal host has, with `python3` and `cryptography` as the fallback and "unverifiable" as the answer when a host has neither. The signature is over the manifest and the manifest binds the tarball, so nothing is untarred until both checks pass. `RELEASE_KEY.pub` ships at the package root, and so will `updater/` and an `agent_version` field once phase 3 has an agent to ship; an unsigned build prints a loud warning and still writes the manifest, and a signed build whose version is not greater than the newest `v*` tag is refused. The manifest is one key per line with lists inline, which is what lets `update.sh` read it with `sed` on a host without `jq`.

`update.sh` gained `UPDATER_SCRIPT_VERSION` (2), `--require-signature` / `--release-key`, `--require-newer`, `--check-archive FILE` (all checks, touches nothing, needs no container runtime, prints `::archive=`, `::manifest=`, `::signature=`, `::sha256=`, `::version=`, `::installed=`, `::orphan_candidates=` and `::check=ok`), a project and plugin match against `.env`, tar-listing hardening (member names normalised first; no absolute paths, no `..`, no symlinks or hardlinks, no device nodes, pipes or setuid files, no `.env`, `.git/` or `backups/` members, exactly one top-level directory, a named refusal for macOS `._*` members), pruning that refuses any listed path whose parent does not resolve inside the tree — the lists are writable by the deployment account, and the script may run as root, the `::step=` / `::snapshot=` / `::health=` / `::done` / `::failed=` lines the agent parses (vocabulary pinned in [scripts/tests/test_update_targets.py](../../scripts/tests/test_update_targets.py)), `--snapshot LABEL` as a snapshot-only mode, `--rollback --snapshot NAME`, EUID-aware `sudo` for Podman, and the `FILELIST` pruning. The manual path verifies when the sidecars are present and warns when absent, so a hand-applied unsigned package keeps working. The release key at the deployment root is uid-1000-writable, which is fine for the manual path and exactly why the agent keeps its own copy.

## Phases

0. **Prerequisites** — the six script defects above, each with a test that can see it, which for the schema restore means a real Postgres rather than fakebin. Landed 2026-09-20.
1. **Signed packages and a hardened manual path** — scripts only; manual operators benefit immediately. Landed 2026-09-20; see [Signing and packaging](#signing-and-packaging) for what shipped and the two pieces (`updater/`, `agent_version`) that wait for the agent.
2. **`maintenance` app, celery tier, lock middleware, admin tab** — spool code present, host-tier operations hidden while `REMOTE_UPDATE_ENABLED` is off. A middleware that answers 503 to every API client during maintenance is a behaviour change every project frontend sees, so this release bumps the platform minor.
3. **Host tier** — spool protocol, agent and installer, compose mounts, package upload, request / verify / rollback, notifications, outage-tolerant polling, docs. This is the release that must be installed by shell once.
4. **Hardening** — code-only rollback when the release applied no migrations, standalone `platform.rollback` and `platform.backup`, cross-signed key rotation via the manifest, agent self-update opt-in.

## First-time enablement

1. Apply the release carrying phases 1–3 by shell, or deploy fresh from its distribution.
2. `sudo ./updater/install-updater.sh --root <deploy root>` (or `sudo ./prepare-host.sh --with-updater` on a fresh host); verify the release key fingerprint out of band.
3. `ENABLED=1` in `/etc/epicurrents-updater/config`.
4. `REMOTE_MAINTENANCE_ENABLED=True` and `REMOTE_UPDATE_ENABLED=True` in `.env`; recreate `web celery celery-beat`.
5. Check the Maintenance tab shows the agent seen within two minutes and the installed version; superusers enrol push notifications.

## Risk register

| Risk | Mitigation | Residual |
|---|---|---|
| Data written between the dump and the rollback is lost | Lock from acceptance to the terminal state, beat stopped, worker drained before the dump, post-update snapshot before rollback | Superuser writes made during the window (they are warned) and a superuser's post-update login session |
| Audit rows for the window are erased by the DB restore | Security log records request, verify, rollback and state changes; the spool log is retained | `Activity` and `ObjectChangeLog` rows of the window are gone, invisible until chain-head anchoring lands |
| Web compromise writes spool requests | Agent accepts only a registered host operation on a signed, newer, compatible package; single in-flight job; no request content executed | An attacker with a superuser session can force an update to a legitimate newer release or an in-window rollback: availability, not code execution |
| Compromised superuser account | Step-up, rate limit, security log, notification to every superuser, monotonic version | Same availability impact |
| Signed but buggy release | Ready and version gate, window, rollback | A bug that passes the gate and stays quiet for the window is verified in; a destructive migration is undone only from a pre-update snapshot, of which three are kept |
| Agent bug, `rollback_failed`, `stale` without a snapshot | The agent never resumes a half-run update; a stale job with a snapshot rolls back to it; states name the need for shell; fakebin coverage | Cannot be fixed remotely by design |
| The superuser is not paged | Push and email where configured; the requester's own browser is the primary channel and the SPA polls through the outage | On a host with neither push nor mail set up, a requester who walks away gets an automatic rollback |
| A package needs a newer agent or `update.sh` | `min_updater_version` refusal with a clear message | Needs shell until agent self-update |
| Release key compromise or rotation | Key kept off-host; `key_id` in the manifest | Rotation cannot travel through the channel it protects; a stolen key can ship a malicious newer release |
| Disk exhaustion | Agent free-space preflight; package keep-2, snapshot keep-3 plus post-update keep-2; proxy body guard | A full disk after the snapshot can end in `rollback_failed`, because the rollback rebuilds images too |
| Rollback rebuilds every image | Documented; outage banner; `TimeoutStartSec=0` | Minutes of 502 on small hosts |
| SELinux label on the new bind mount | Same treatment as the existing mounts | Unwritable spool on Enforcing hosts until relabelled |
| Hosts without systemd | Status banner "agent not installed"; the celery tier still works | Host tier unsupported there |

## Decisions taken, open to revision before implementation

1. Rollback always restores the DB dump in v1, even when the release applied no migrations.
2. Lock policy: full suspension while updating or rolling back; read-only for non-superusers while verifying, with login allowed; superusers exempt while verifying. The alternatives are full suspension until verified (simplest, but the superuser cannot test a write) or read-only for superusers too.
3. Window length is per job, clamped 5–1440 minutes, default 30.
4. Staff see the Maintenance tab read-only; superusers get the buttons.
5. Verification is an explicit click, never inferred from a login. A rollback may still be requested after verification for as long as the job's pre-update snapshot exists, with the same data-loss warning — changed from the first draft, which refused it, because a release that passes the superuser's check and fails for everyone else is exactly the case a shell-less host cannot otherwise recover from.
6. Checkout-mode deployments are refused by the agent.
7. Key rotation is a shell operation in v1.
8. Notifications go to every active superuser by push, and by email when mail is configured.
9. Upload is three files (tarball, manifest, signature); no new bundle format for `update.sh` to learn.
10. OIDC-provisioned superusers confirm with TOTP; without it they cannot use the feature. Re-authenticating against the provider was considered and rejected for v1 as a second login flow to maintain.
11. Every remotely applied package carries a strictly greater version, hotfixes included; the packager enforces it.
