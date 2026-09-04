# Pre-launch checklist

What stands between this repository and a first production deployment carrying real recordings. Every open item is a gap someone found by looking, not a hypothetical; every one carries a command that answers "is this still true?" so the list can be re-derived rather than trusted.

Items are ordered by consequence, not by effort. The ordering claim is deliberate: an item further down is not merely less urgent, it is one whose failure mode is recoverable.

This complements [ROADMAP.md](../ROADMAP.md) rather than duplicating it. The roadmap tracks *what the platform could become*; this tracks *what a deployment must not go live without*. An item here that needs real design gets a roadmap entry and a link.

## Contents

- [How to use this](#how-to-use-this)
- [1. Deployment hygiene](#1-deployment-hygiene)
- [2. Backups and recovery](#2-backups-and-recovery)
- [3. Image and resource posture](#3-image-and-resource-posture)
- [4. Exposure and access](#4-exposure-and-access)
- [5. Legal and compliance preconditions](#5-legal-and-compliance-preconditions)
- [Done](#done)

---

## How to use this

Run the check. If it reports the "open" value, the item stands. Checks are written to be run from the repository root and to need nothing but a shell — none of them require a running stack, because an item that can only be verified against production is not a pre-launch check.

Two things this list deliberately does not do. It does not assign effort estimates, which were wrong often enough during the work that produced it to be worth omitting. And it does not mark anything "done" on the strength of an intention — an item moves to [Done](#done) when its check reports the closed value.

An item marked **Open — operator action** is one the repository cannot close for a deployment, because closing it means supplying something only the operator has: a backup host, a custody arrangement, a decision about who may reach the admin. What the repository can do for those is make the gap impossible to leave unnoticed, and where that has been done the item says so. The item still stands until the deployment acts on it.

---

## 1. Deployment hygiene

Empty. Both items that stood here are in [Done](#done); the third was dismissed on evidence.

---

## 2. Backups and recovery

The section with the only items on this list whose failure mode is unrecoverable rather than inconvenient.

### 2.1 — Backups are local-only by default

**Open — operator action.** `BORG_REMOTE_REPO` is empty in the shipped template, so archives land in the `borg-data` volume on the same host as the data they protect. A host loss takes the recordings, the database and the backups together. The repository cannot close this for a deployment — only a real off-host target does — so what it can do it now does: the choice is no longer silent.

```bash
grep -E "^BORG_REMOTE_REPO=" .env
# open on a deployment: BORG_REMOTE_REPO=   (no value)
```

Production with an empty value logs a boot warning from `_warn_local_only_backups` in [epicurrents/apps.py](../epicurrents/apps.py) unless `BACKUP_LOCAL_ONLY` spells an affirmative (`true`, `1`, `yes`, `on`) to acknowledge it, and the borg container repeats the warning at each start. Covered by `TestLocalOnlyBackupWarning` in [test_credential_guards.py](../epicurrents/tests/test_credential_guards.py).

The destination is already designed: [docs/engineering-notes/intrusion-detection-design.md](engineering-notes/intrusion-detection-design.md) → *Layer 2 — the evidence host* specifies a Borg repo served `--append-only`, so a host-root attacker can neither destroy the backup history nor rewrite it. The client needs nothing for that — `--append-only` is enforced by the SSH forced command on the serving side, and `BORG_RSH` already defaults to `StrictHostKeyChecking=yes`. A deployment that sets `BORG_REMOTE_REPO` to a plain SSH target today upgrades to the append-only posture later without touching this repository.

That is the argument for closing this item *before* the evidence host exists rather than waiting for it: the evidence host is a second machine and a separate deployment topology, and making it a precondition would put a two-host requirement in front of the single-host bootstrap story. Any off-host SSH target closes the failure mode now and is forward-compatible with the design.

### 2.2 — The passphrase is not recoverable from the backup

**Open — operator action.** `.env` holds `SECRET_KEY`, `BORG_PASSPHRASE`, the federation Ed25519 private key, `ACTIVITY_HASH_KEYS` and the VAPID keys. It is in no archive — deliberately, since an archive containing the passphrase that decrypts it is not a control. But that means losing the host loses the ability to read the backups.

The custody requirement is now written down, in [operations.md](operations.md#keep-the-passphrase-somewhere-the-host-loss-cannot-reach): where to keep the passphrase, that the repository address has to be kept with it, and that retrieval should be rehearsed from the custody store rather than from `.env`. The passphrase is only half of it — with `repokey` the key sits inside the repository, so repository damage loses the archives even when the passphrase is correct, and a deployment with off-host backups has two independent keys to escrow rather than one. [Export the repository keys](operations.md#export-the-repository-keys-one-per-repository) covers both. Arranging the custody is the operator's step, and it is what closes the item. The intrusion-detection note records the related residual honestly: the passphrase in `.env` makes any *copied* archive readable offline, which is a custody problem rather than a storage one.

---

## 3. Image and resource posture

### 3.1 — Worker memory and CPU are unbounded by default

**Open — operator action.** `CELERY_MEM_LIMIT` and `CELERY_CPUS` now exist and are wired into the production overlay, but both default to no limit, so an unconfigured deployment is still unconstrained.

```bash
grep -E "^CELERY_MEM_LIMIT=" .env
# open on a deployment: absent, empty, or 0
```

The default is deliberate. A cap set too low converts a slow job into a killed one, and there is no number that is right for every host — which is why the item asks for a value rather than shipping a guess. What changed is that the choice is no longer silent: production warns at boot while `CELERY_MEM_LIMIT` is unset (`_warn_unbounded_workers` in [epicurrents/apps.py](../epicurrents/apps.py)); setting it to `0` records the decision to run unbounded and silences the warning, and [operations.md](operations.md#bound-the-workers) carries the sizing rule plus how to tell a too-low cap from a genuine crash.

The pool size is now bounded regardless. Celery sizes its own pool from the host's CPU count, which made the worker container's peak scale with the host rather than with the workload — four children on a 4-core host, each able to hold a whole recording. `CELERY_CONCURRENCY` pins it at 2, which bounds the multiplier without risking a job: exceeding the pool queues work rather than killing it.

The remaining operator step now has a number behind it rather than a recipe. Measured against this codebase, a lead field is 190-820 MB depending on grid resolution, and ingest is negligible, since its EDF passes read per data record. On the 8 GB host the platform is sized for, `CELERY_MEM_LIMIT=4g` over a pool of 2 holds two concurrent lead-field builds with room to spare. The pair has to be revisited when a job that allocates in proportion to recording length is added, since those scale with the data rather than with request volume, and the arithmetic on both sides is in [operations.md](operations.md#bound-the-workers).

Only the workers are bounded. They are the tier that allocates in proportion to the recording rather than to request volume, and when the host runs out the OOM killer picks by score — Postgres, large and shared-memory heavy, is a strong candidate, so an ingest job takes the database with it. `web` and `db` have no equivalent failure mode.

---

## 4. Exposure and access

### 4.2 — Content-Security-Policy is report-only

**Closed 2026-08-28.** The policy was tuned against a running deployment and is now enforced by default.

```bash
grep -oE 'CSP_REPORT_ONLY.*default=[A-Za-z]+' epicurrents/settings/production.py
# closed: default=False
```

The tuning pass removed the one third-party origin the baseline still allowed. `https://cdn.jsdelivr.net` sat in `script-src` and `connect-src`, left from before Pyodide was vendored, and the audit that removed it found the SPA's inline bootstrap still naming a jsdelivr URL pinned to a runtime nine major versions behind the one the deployment serves. Nothing had broken, because `App.vue` overrides the value after boot — allowing the origin is exactly what would have let any path reaching it first fetch quietly. The baseline now permits no external origin at all.

Verified with the browser console against the base platform and one project across the SPA, viewer, upload and library views: no violations. `'unsafe-inline'` remains in `script-src` and is the residual weakness — `index.html` is static and has no per-request nonce point. Removing it means moving or hashing the inline bootstrap scripts, tracked in [ROADMAP.md](../ROADMAP.md).

Two configurations were not covered and should run report-only for a cycle before trusting the default: the **dicom plugin**, whose OHIF viewer is a submodule that was not checked out when the policy was tuned, and any **project whose own views reach an external origin**. Both can extend `CONTENT_SECURITY_POLICY` from their own `settings.py`. Procedure in [operations.md](operations.md#security-headers).

### 4.3 — Two-factor authentication

**Closed 2026-08-26.** TOTP is available on password login, opt-in per account from the profile page, with hashed single-use recovery codes and a superuser-only reset for a lost authenticator. Mechanism and policy in [user/README.md](../user/README.md#two-factor-authentication-totp).

Two things an operator should know before launch. It is opt-in, so enabling it for the accounts that hold administrative access is a deployment step, not something the software does — check `is_2fa_enabled` on the account roster after provisioning. And it does not apply to external (OIDC) logins, where the identity provider owns the second factor; if a deployment uses OIDC for its staff accounts, the factor has to be configured at the provider instead. Making enrolment mandatory — for staff, or for every password-protected account — is tracked in [ROADMAP.md](../ROADMAP.md) under *User — enforce a second factor on password login*.

```bash
grep -c 'TwoFactorCredential' user/models.py
# closed: 1 or more
```

---

## 5. Legal and compliance preconditions

### 5.1 — No Art. 13/14 privacy notice

**Half closed 2026-08-26, and the remaining half is yours.** [privacy-notice-template.md](privacy-notice-template.md) drafts two notices — Art. 13 for account holders, Art. 14 for recording subjects, which is a separate document because it carries a source-disclosure duty and a delivery problem the first does not. Every fact the software determines is filled in and verified against the code: data categories, retention windows with their setting names, the processor list, what de-identification actually removes at ingest. Every fact only you know is a `[FILL]` marker, and the conditional blocks are keyed to the setting that turns each feature on.

```bash
ls docs/privacy* 2>/dev/null || echo "no privacy notice"
# closed: docs/privacy-notice-template.md
```

Two things stop this being fully closed, both requiring you rather than the repository.

**The `[FILL]` markers are load-bearing.** Controller identity, the lawful basis for each purpose (Art. 6 *and* an Art. 9 condition, since this is health data), where recipients are located and the transfer mechanism for any outside the EEA, your supervisory authority, and whether the deployment does automated decision-making. There is no safe default for any of them, and a notice published with them unresolved is worse than none — it is a documented misstatement. Have the result reviewed by someone qualified.

**Nothing in the application serves it.** Art. 13 expects the notice at the point of collection; today no page links it and nothing shows it at account creation, so a subject receives it only if you wire that up outside the platform. Carried in [gdpr-compliance.md](gdpr-compliance.md#known-gaps).

Settle Notice B's delivery with the institution that supplies the recordings before the first upload. The platform de-identifies at ingest and is built not to learn who the patient is, which is exactly why it cannot deliver their notice.

### 5.2 — GDPR re-audit before release

**Closed 2026-08-26.** Full four-lens sweep performed (inventory / retention, security of processing, third-party flows, project plugins); the result and its findings are recorded in the [audit log](gdpr-compliance.md#audit-log), which the document previously lacked — the six-month cadence it requires was unverifiable without one.

Six findings, all fixed in the same commit, and one of them is on the main ingest path rather than an operator tool.

**`Activity.target_identifier` carried whatever a model's `__str__` rendered.** `with_system_activity` stored `str(target)`, and `Recording.__str__` embeds `original_name` — so every recording that went through `process_recording` published its uploaded filename into a permanent column. That is past three separate controls: the `register_masked_fields` entry that keeps `original_name` out of `ObjectChangeLog`, the `_can_see_original_name` gate that keeps it off the API, and every erasure path, none of which touch that column. It now stores a content-type-and-pk locator, matching what the change-log writer already used. Confirmed by reproduction before fixing and pinned by [test_system_activity_identifier.py](../activity/tests/test_system_activity_identifier.py), which asserts the property rather than the model — the hole came from a `__str__` written for a debugger, so anchoring the test to `Recording` would let the next such model reopen it.

**This one has an operator step**, because the fix does not rewrite rows already written. If this deployment has processed recordings, their filenames are in the table now; [gdpr-compliance.md](gdpr-compliance.md#audit-log) carries a verified one-statement cleanup that blanks the renderings while sparing the request paths on API rows.

The other five are operator tools. `import_recordings` wrote the raw username and the source directory into permanent `Activity` metadata on a row targeting the import job, where `erase_subject` — which reaches `Activity` only when the target is the user model — could never remove them; the per-file source filename and error text sat unmasked in `ObjectChangeLog`; and `index_dicom` had the same directory-in-metadata shape. Regression coverage in [test_import_audit_hygiene.py](../recordings/tests/test_import_audit_hygiene.py), and the convention every other call site already followed is now a written rule in [AGENTS.md](../AGENTS.md).

The sweep also cleared things worth naming as cleared: retention windows match their settings, and no outbound flow exists that the processor table omits. One forward gap is recorded — `compute.PipelineRunAudit` is a second permanent hash-chained table that `erase_subject` does not walk, dormant today and an Art. 17 gap on its first write.

Re-run before the next production release or by 2027-02-26, whichever comes first.

---

## Done

Kept with the evidence that closed them, so a regression is recognisable as one.

- **Django admin is retired** — the `admin/` mount, the three `admin.py` registrations and the admin banner template are gone; `/admin/` now reaches the SPA like any other unknown path. Account and group management moved to `/api/v1/user/admin/` ([user/README.md](../user/README.md#account-administration)), where the audit trail, the CSRF chokepoint and the de-identification rules apply by construction rather than by instrumentation — an admin write produced zero `Activity` and zero `ObjectChangeLog` rows, measured rather than inferred. Retiring it also removed the de-identification bypass in `media/admin.py`, which listed and searched the author-private `MediaFile.original_name`. Asserted by [test_admin_is_retired.py](../epicurrents/tests/test_admin_is_retired.py): no route, no import of `django.contrib.admin` in the URLconf, no `admin.py` in any app, no first-party model on the admin site, and the replacement answering. The replacement has no in-app client yet — the SPA carries no view for it, so accounts are managed by hand against the API or through management commands until the UI entry in [ROADMAP.md](../ROADMAP.md) is done. `django.contrib.admin` stays in `INSTALLED_APPS` — dropping it also drops the `django_admin_log` table, which is tracked in [ROADMAP.md](../ROADMAP.md) as a separate step.
- **TLS termination and certificate renewal** — [docker-compose.proxy.yml](../docker-compose.proxy.yml) + [caddy/Caddyfile](../caddy/Caddyfile), opt-in via `PROXY_DOMAIN`. HSTS staged at 300 s with preload off; the ramp is in [operations.md](operations.md#security-headers).
- **Static assets off the WSGI thread pool** — the proxy serves `/static/`, `/assets/`, `/vendor/` and the viewer bundles from disk, with per-prefix cache headers pinned to [epicurrents/views.py](../epicurrents/views.py) by [test_proxy_asset_headers.py](../epicurrents/tests/test_proxy_asset_headers.py).
- **Permission-checked downloads off the thread pool** — [epicurrents/offload.py](../epicurrents/offload.py), with the `apply_middleware` interlock and an AST scan asserting every call site passes it explicitly.
- **Restart policy and log rotation** — the `x-production-runtime` anchor in [docker-compose.prod.yml](../docker-compose.prod.yml), asserted by [test_compose_runtime_posture.py](../epicurrents/tests/test_compose_runtime_posture.py) including the inverse (one-shots must not restart).
- **Placeholder `FRONTEND_URL` refuses to boot; unconfigured mail warns** — [epicurrents/apps.py](../epicurrents/apps.py), covered in [test_credential_guards.py](../epicurrents/tests/test_credential_guards.py).
- **Proxy and application body limits cannot disagree** — boot guard in [epicurrents/apps.py](../epicurrents/apps.py); the shipped default was mismatched (`2GB` decimal vs 2 GiB) and rejected the largest legal uploads.
- **Unprivileged image default, pinned base images** — `USER appuser` in the [Dockerfile](../Dockerfile); `postgres:17.11`, `redis:7.4.10`.
- **Request-body ceiling decoupled from the upload ceiling** — `DATA_UPLOAD_MAX_MEMORY_SIZE` no longer sized against `RECORDINGS_MAX_UPLOAD_SIZE`, guarded by [test_upload_limits.py](../epicurrents/tests/test_upload_limits.py).
- **Channel-block de-identification** — phases 1, 1b, 2 and 3 of the [channel-de-identification plan](engineering-notes/channel-deidentification-plan.md); only the annotation-vocabulary phase remains, carried in the roadmap.
- **Backup failures are no longer silent** — every failure writes an `EPICURRENTS_BACKUP_FAILURE` line naming the repository and the error, from borgmatic's `after: error` hook and from [borgmatic/entrypoint.sh](../borgmatic/entrypoint.sh)'s fallback for the case where borgmatic dies before running any hook. Setting `BORG_MONITOR_URL` adds a Healthchecks-compatible monitor that pings on start, finish and failure — the stronger half, since it also alerts on silence, which is what a stopped stack looks like. The ping carries no content unless `BORG_MONITOR_SEND_LOGS` opts in, and that flow is registered in [gdpr-compliance.md](gdpr-compliance.md#processor-and-cross-controller-flows). The emitted config was validated against the pinned borgmatic 2.1.3 image, and the error hook confirmed to fire against a deliberately broken run. `TestFailureVisibility` in [test_emit_runtime_config.py](../borgmatic/tests/test_emit_runtime_config.py); operator side in [operations.md](operations.md#know-when-a-backup-fails).
- **Restore has been exercised end to end** — [scripts/restore-drill.sh](../scripts/restore-drill.sh) seeds a database row and a recording file, backs both up through the production borgmatic config, destroys the database volume and unlinks the file, restores, and asserts the row returned and the file is byte-identical. It first asserts both halves were genuinely gone, so a destroy step that quietly did nothing cannot report a pass. Run on 2026-08-24 against a live Docker host: passed, with the restored file matching the seeded sha256 exactly and the host's running deployment untouched. Isolation (scratch compose project, no published port, volume removal gated on the project prefix) is pinned by [test_restore_drill.py](../scripts/tests/test_restore_drill.py).
- **Redis persists the broker** — `--appendonly yes` plus a `redis-data` volume, so a restart no longer drops queued conversions and leaves Recordings stuck in PROCESSING with no worker coming. Verified against a real container both ways: with the setting a queued task survives SIGKILL plus a restart, and on the stock image it does not. `TestBrokerPersistence` in [test_compose_runtime_posture.py](../epicurrents/tests/test_compose_runtime_posture.py). The residual is the `everysec` fsync window — work accepted in the last second before an unclean stop is still lost, and closing that is the recording state machine, carried in the roadmap.
- **Test dependencies no longer ship to production** — the Dockerfile is multi-stage: `runtime` carries the application, `test` adds `requirements-test.txt`, and the compose services name their target. `runtime` is the final stage, so a bare `docker build .` gets the lean image and shipping pytest takes a deliberate `--target test`. Confirmed by building both: the runtime image answers `manage.py check` and raises ModuleNotFoundError on `import pytest`. `TestStageLayout` and `TestComposeTargets` in [test_image_targets.py](../epicurrents/tests/test_image_targets.py).
- **Dependencies are hash-pinned** — [requirements.lock](../requirements.lock) pins all 80 packages in the production closure to specific artifacts, generated by [scripts/lock-requirements.sh](../scripts/lock-requirements.sh) (uv, run in a container pinned to the image's Python, so no host toolchain is required and the resolution is not machine-dependent). The image installs it with `--require-hashes`, verified to reject a corrupted hash rather than resolve past it. CI checks the lock for staleness and audits it instead of `requirements.txt`, so transitive packages are in scope. `TestDependencyLock` in [test_image_targets.py](../epicurrents/tests/test_image_targets.py). The resolution is pinned to a recorded cutoff instant so the staleness check compares against the lock's own state rather than against today's index, which would otherwise turn red on pull requests that changed nothing. The platform closure carries no local-path package, so PEP 517 build isolation fetches nothing unhashed; a project that adds one through its own requirements reopens that residual, since closing it would mean carrying build tooling in the deployment image.
- **Readiness split from liveness, and healthchecks on `web` and `celery`** — `GET /api/v1/ready` opens a database cursor and reads the cache, answering 503 with the failing dependency named in `checks`; `/api/v1/health` stays dependency-free so a database blip does not restart every web container at once. The `web` probe requests `/ready` over loopback and the `celery` probe is an `inspect ping` scoped to its own node, since an unscoped ping is answered by any worker on the broker. Asserted by [test_compose_runtime_posture.py](../epicurrents/tests/test_compose_runtime_posture.py) (`TestHealthchecks`) and [test_readiness.py](../epicurrents/tests/test_readiness.py). The loopback probe means `ALLOWED_HOSTS` must keep `127.0.0.1`; `_warn_healthcheck_host` in [epicurrents/apps.py](../epicurrents/apps.py) warns at boot when it does not. It also means the probe has to survive `SECURE_SSL_REDIRECT`, which is on by default in production and answered the plain-HTTP loopback request with a 301 to an `https://` URL gunicorn does not speak — every production container would have reported unhealthy while serving normally, and no test under the test settings could see it. `SECURE_REDIRECT_EXEMPT` in [production.py](../epicurrents/settings/production.py) covers the one path, asserted by `TestProbeSurvivesTheHttpsRedirect`.

### Investigated and dismissed

- **`CSRF_TRUSTED_ORIGINS`** — initially listed as a required setting. `CsrfViewMiddleware._origin_verified` passes when the Origin matches scheme + `request.get_host()`, and the SPA is served same-origin by Django, so the setting is unnecessary until the frontend moves to its own domain.
- **Ingest-time header anonymisation** — initially raised as a red gap. `process_edf_file` already de-identifies the EDF patient and recording fields at ingest, on both branches and for converted files; the claim came from grepping [recordings/tasks.py](../recordings/tasks.py) without following the call into [recordings/processors/edf.py](../recordings/processors/edf.py).
- **`GUNICORN_TIMEOUT` against the upload ceiling** — listed as an upload that dies mid-transfer above roughly 17 MB/s. The premise does not hold for the worker class the overlay pins: gthread's accept loop calls `notify()` once per iteration of a 1 s poll, and the arbiter's `murder_workers` compares only against that timestamp, so a request thread running for half an hour never trips it. The edge agrees — the [Caddyfile](../caddy/Caddyfile) sets `read_header 15s` and deliberately no `read_body` timeout. The real ceiling on concurrent uploads is `GUNICORN_WORKERS x GUNICORN_THREADS`, since each upload holds a slot for its duration; that is now documented in .env.example, alongside the warning that switching to sync workers turns the timeout into a genuine per-request limit.
