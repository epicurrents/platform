# GDPR compliance

How this platform handles personal data, which GDPR obligations the code
discharges mechanically, which ones fall to the deployment operator, and the
rules for keeping this document current. Audience: the operator (acting as
data controller) and developers extending the platform.

The platform is software, not a controller — every deployment's operator is
the controller for the data it processes. This document maps the code to the
obligations; lawful basis, DPAs with processors, and controller-to-controller
arrangements with federation peers are the operator's paperwork.

## Data subject families

The platform processes personal data of two distinct families, with different
erasure mechanics:

| Family | Who | Personal data | Erasure path |
|---|---|---|---|
| **Account subjects** | Platform users (clinicians, researchers, students with accounts) | Username, names, email, password hash, OIDC identity (`sub`, cached email), push endpoints and keys, authored free text | The `erase_user` command: cascade + file unlinks + session flush + audit-trail scrub ([user/README.md → Account erasure](../user/README.md#account-erasure-gdpr-art-17)) |
| **Recording subjects** | Patients whose signals, media, or DICOM data are uploaded | EDF/BDF signal data, uploaded filenames (`original_name`), clinical annotation text, media content, DICOM demographics (dicom project) | Soft-delete + purge pipelines ([recordings/README.md](../recordings/README.md#soft-delete-and-purge), media, library); write-time masking keeps identifiers out of the audit trail |
| **Third parties** | People mentioned in search queries, free text | Search strings (hashed since 2026-07), incidental mentions in notes | Prevented at write time where possible (hashing, masking); otherwise covered by the containing row's erasure |

Non-account data subjects (patients, or a project's participants without accounts) cannot be
keyed by a user FK, so their protection is **minimization at write time** —
masking registrations keep their identifiers out of the permanent audit
trail — plus deletion of the live rows through the owning feature's pipeline.

## Data inventory

Personal-data-bearing stores, their retention, and their erasure path. The
`gdpr-compliance` review agent blocks commits that add a personal-data model
without extending this table.

| Store | Data | Retention | Erasure |
|---|---|---|---|
| `user.User` | Identity, credentials | Life of account | `erase_user` (row + audit scrub) |
| `user.ExternalIdentity` | OIDC `sub`, cached email | Life of account | Cascade + audit scrub |
| `user.TwoFactorCredential` | TOTP shared secret, recovery-code hashes (authentication credentials bound to one account — not identity data, but account-linked secrets) | Life of the second factor / account | Cascade + audit scrub; `secret` and `backup_codes` masked in audit at write time |
| `user.UserPreference` | Client settings blob (no personal data by design — the write endpoint accepts only setting-shaped keys and primitive values) | Life of account | Cascade + audit scrub; `values` registered for scrubbing so a badly named client setting cannot strand data in the permanent trail |
| `notifications.PushSubscription` | Device endpoint, encryption keys | Life of subscription / account | Cascade + audit scrub; keys masked in audit |
| `recordings.Recording` + file | Signal PHI, `original_name`, `processing_error` | Until trashed + 30 d (`RECORDINGS_TRASH_RETENTION_DAYS`) | Purge task (READY + FAILED trash, orphan reaper); `original_name` / `processing_error` masked in audit |
| `recordings.ImportJob` / `ImportJobFile` | Operator-supplied source directory and the source filename of each imported file — `original_name` before a Recording exists — plus per-file error text | Life of the job row (operator deletes; no automatic purge) | Author cascade on the job's owner; `source_path`, `relative_path` and `error` masked in audit at write time |
| `recordings.SignalInfo` | Pre-de-identification channel descriptors (`source_label`, `source_transducer_type`, `source_prefiltering`, `source_index` — site-fingerprint metadata, author-private in the API) | Life of the recording | Cascade with recording purge; rows are digest-only in the audit trail (never serialized to `ObjectChangeLog`) |
| `media.MediaFile` + file | Media PHI, `original_name` | Until trashed + 30 d (`MEDIA_TRASH_RETENTION_DAYS`) | Purge task; `original_name` masked in audit |
| `library.Collection` / `Dataset` / items | User-chosen names (free text) | Until trashed + 30 d (`LIBRARY_TRASH_RETENTION_DAYS`) | Purge task; author cascade |
| `library.DatasetFolder` | User-chosen folder names (free text, grantee-visible) | Life of the dataset — no trash window; hard-deletes immediately on folder delete | Cascade from dataset (and so from the dataset author) |
| `library.DatasetSnapshot` | User-chosen label; manifest of opaque member hashes (not personal data — non-invertible, non-confirmable, resolve to nothing) | Life of the dataset and of both linked accounts (create-only rows, no purge window) | Cascade from dataset and from snapshot author; member erasure leaves the sealed manifest intact by design |
| `annotations.*` | Clinical/user free text, author FK | Life of target object / account | Author cascade; target-object cascade on purge (audit snapshots: see Known gaps) |
| `activity.Activity` | Actor FK, verb, path, hashed metadata | Archived after 90 d (`ACTIVITY_ARCHIVE_AFTER_DAYS`); rows kept | Actor FK nulls on account deletion; PII metadata keys scrubbed by `erase_subject` |
| `activity.ObjectChangeLog` | Serialized model states (masked fields excluded at write) | Permanent | Subject-erasure tombstones ([activity/README.md → Subject erasure](../activity/README.md#subject-erasure-gdpr-art-17)) |
| `federation.FederationAuditLog` | Peer URL, remote subject id, access records | `FEDERATION_AUDIT_RETENTION_DAYS` (default 2200 d ≈ 6 y; 0 = keep) | Pruning task; remote-subject sweep is a ROADMAP item |
| `epicurrents.AccessRight` | Grantee FK / remote subject id | Life of grant; expired rows purged | Cascade with user / target object |
| `django_session` | Session key, user pk (encoded) | 12 h in production (`SESSION_COOKIE_AGE`); expired rows reclaimed daily | Flushed by `erase_user`; excluded from audit tracking |
| Recording originals volume (`RECORDINGS_ORIGINALS_PATH`) | Raw uploads incl. PHI + manifest | Operator-controlled (write-only for the platform) | Out-of-band; `erase_user` prints the stored names to reconcile |
| Redis append-only file (`redis-data`) | Celery task arguments and results, hashed rate-limit keys, opaque federation replay `jti`s. Every task in the repo takes identifiers: the reset flow queues a user pk and mints the token in the worker, push payloads carry `display_name` and never `original_name`, and there is deliberately no generic task accepting a recipient address or message body | Until the next AOF rewrite (Redis defaults: 64 MB / 100 % growth), so effectively unbounded on a low-volume deployment | No erasure path; `erase_user` does not reach it, and `BGREWRITEAOF` only compacts. The control is therefore upstream and has to stay there — a task signature that accepts personal data puts it beyond recall |
| Backups (borgmatic) | Everything above | ~6 months (7 daily / 4 weekly / 6 monthly), encrypted | See [Erasure and backups](#erasure-procedure) |
| Project plugin stores | See each project's and plugin's README (dicom demographics; a project's participant sets, labels or roles) | Project-specific | Project-specific, registered in each project's app config: `register_masked_fields` keeps credentials and free identifiers out of audit payloads at write time, `register_subject_pii` puts the rest in scope for Art. 17 scrubbing. A project model holding data about an account holder needs the second one; the FK cascade removes the live row but reaches nothing in the permanent trail. |

## Processor and cross-controller flows

Where personal data leaves the deployment. Adding an outbound flow requires a
row here (enforced by the `gdpr-compliance` agent) and, on the operator side,
a DPA or controller arrangement.

| Destination | Role | Data | Safeguard |
|---|---|---|---|
| Federated peer instances | Separate controller | Recording bytes (de-identified by default — anonymized EDF header + stripped annotation text; raw only by explicit `--no-apply-middleware` / API opt-out), `display_name`, signal metadata; media bytes raw (ROADMAP); inbound: requesting user's pk as JWT `sub` | Per-peer trust gate, Ed25519 JWT auth, quotas, `FederationAuditLog`; grant default is de-identified |
| SMTP relay (`EMAIL_HOST`) | Processor | Recipient address, password-reset links, account emails | TLS; failure logs carry hashed recipients only |
| Web-push services (Google / Mozilla / Apple) | Processor | Device endpoint + timing; payload is end-to-end encrypted; bodies carry `display_name`, never `original_name` | RFC 8291 encryption; endpoint scrubbed from audit on erasure |
| OIDC provider (Microsoft Entra) | IdP / separate controller for its logs | Login events; inbound `sub`, email, name, tenant id | Tenant + nonce + audience checks; email-domain allowlist |
| Tailscale (optional) | Infrastructure processor | Connection metadata only; traffic stays WireGuard-encrypted between operator nodes | Metadata-only exposure |
| Backup monitor (`BORG_MONITOR_URL`, optional) | Processor | Default: the fact of a backup start / finish / failure, no content. With `BORG_MONITOR_SEND_LOGS`: the run's log tail — archive statistics, repository and hash-derived stored-file paths, database errors | Off unless the URL is set, and content off unless separately opted in; no `original_name` reaches this stream (backed-up files are addressed by `stored_name`) |

No analytics or error-reporting services are wired into the platform. The only
outbound operational telemetry is the optional backup monitor above; it is off
by default and carries no content until the operator opts in.

## Data-subject rights

| Right | Mechanism |
|---|---|
| Access (Art. 15) | `manage.py export_user --username <name>` produces a plain-text document covering every relation to the user model; the classification is validated at `manage.py check`. Operator-run, not self-service — verifying the requester is the subject is not a software problem. See [user/README.md](../user/README.md#subject-access-export-gdpr-art-15). |
| Rectification (Art. 16) | Profile PATCH (`/api/v1/user/me`); recording `display_name` PATCH; annotation CRUD. |
| Erasure (Art. 17) | Accounts: `erase_user` (inventory dry run, file unlinks, session flush, cascade, audit-trail tombstoning). Recordings/media/library: trash + purge pipelines. Audit trail: tombstone + re-seal design keeps the integrity chain verifiable — see [activity/README.md](../activity/README.md#subject-erasure-gdpr-art-17). |
| Restriction (Art. 18) | Soft-delete (trash) restricts visibility while preserving data during the retention window. |
| Portability (Art. 20) | Authors download their recordings raw; annotations/events/labels have per-type `/mine` JSON endpoints. `manage.py export_user --format json` gives the machine-readable form. |
| Transparency (Art. 13/14) | [privacy-notice-template.md](privacy-notice-template.md) — two notices, since account holders are Art. 13 and recording subjects are Art. 14 with a source-disclosure duty. Software-determined facts are filled in; controller identity, lawful basis, transfer mechanisms and retention overrides are `[FILL]` markers the operator completes, and the conditional blocks are keyed to the setting that enables each feature. Publishing it is the operator's step, and the platform has no page that serves it — see [Known gaps](#known-gaps). |

## Security of processing (Art. 32) — pointers

The measures live in code and are documented where they are enforced:

- Object-level permissions, share-token limits — [epicurrents/README.md](../epicurrents/README.md#permissions).
- TOTP second factor on password login, with hashed single-use recovery codes and a replay-guarded verification step — [user/README.md](../user/README.md#two-factor-authentication-totp). Opt-in per account by default; `TWO_FACTOR_REQUIRED_FOR_STAFF` and `TWO_FACTOR_REQUIRED_FOR_ALL` make it mandatory, and an account with no factor enrols during login rather than being locked out. Whether it is in force on a given deployment therefore remains the operator's configuration, but the software no longer leaves them without the option.
- Tamper-evident audit trail (HMAC + per-shard hash chain), credential masking, session exclusion — [activity/README.md](../activity/README.md#threat-model).
- PHI de-identification on upload and on federated serving — [recordings/README.md](../recordings/README.md), [federation/README.md](../federation/README.md#middleware-pipeline).
- `Cache-Control: no-store` on all PHI-bearing responses; HSTS, CSP, secure cookies (12-hour production sessions), throttling keyed on hashed identities.
- Security event stream for SIEM with hashed identifiers — [docs/operations.md](operations.md).
- Encrypted backups (Borg repokey); Redis password-gated; internal-network-only services.

## Erasure procedure

Operator runbook for an Art. 17 request from an account holder:

1. `scripts/manage.sh erase_user <username>` — review the printed inventory.
   **Read the recording and media counts before going further.** Erasure
   cascades from the account row, so it destroys the recordings that account
   uploaded and the annotations on them — the subject's own clinical
   contributions, not merely their identity. Where a retention duty covers
   that data, the request has to be answered in part rather than in full, or
   ownership moved first; either way it is a decision to take at this step,
   because step 2 does not ask again. The dry run is the only place the
   consequence is visible.
2. `scripts/manage.sh erase_user <username> --yes` — performs the erasure.
3. If preservation is enabled, reconcile the write-only originals volume
   using the stored names the command prints (the platform never reads or
   deletes there).
4. **Backups:** erased data persists in Borg snapshots until they rotate out
   (~6 months at default retention). Record the request date; if a restore
   is ever performed from a snapshot predating the erasure, re-run
   `erase_user --user-id <pk> --yes` immediately after the restore. Do not
   restore-and-forget.

For recording subjects, the equivalent is trashing the recording (or the
operator deleting it) and letting the purge task complete after the retention
window; preserved originals follow step 3.

## Known gaps

Tracked in [ROADMAP.md](../ROADMAP.md) with the `Privacy` / `Federation` /
`Security` prefixes; the load-bearing ones as of the 2026-08-26 audit:

- Patient-side audit snapshots (annotation content, recording states) persist
  after a recording purge — purge-time tombstoning is the planned extension.
- No *self-service* subject-access export — Art. 15 is served by the operator running `export_user`, which is a person's turnaround rather than a download link.
- No in-application surface serves the privacy notice. The template exists and
  the operator can publish it anywhere, but nothing links it from the sign-in
  page or shows it at account creation, which is where Art. 13 expects it —
  so whether a subject actually receives it currently depends on the operator
  wiring it up outside the platform.
- Remote federated subjects have no sweep command; media crosses federation
  raw.
- `compute.PipelineRunAudit` is a second permanent, hash-chained table with an
  actor FK and a free-form `meta` payload, and `erase_subject` does not walk
  it — it reaches `ObjectChangeLog` and `Activity` only. Nothing is written to
  it yet ([compute/tasks.py](../compute/tasks.py) calls it a later slice), so there is nothing to
  erase today; the gap opens on the first write. Either extend `erase_subject`
  to the table or keep `meta` to identifiers, and decide before it starts
  recording rather than after.
- Project-specific gaps live in each project README (dicom ingest
  de-identification and retention; a project's participant-identity erasure,
  where free-text feedback about a participant typically sits unmasked next
  to a masked `annotator_name`).

## Audit log

The periodic sweep required below, recorded so the next one can tell when it
is due rather than inferring it from commit dates.

| Date | Scope | Outcome |
|---|---|---|
| 2026-08-26 | Full four-lens sweep, ahead of first production deployment | Six findings, all fixed in the same commit. The serious one is on the main ingest path: `with_system_activity` stored `str(target)` in `Activity.target_identifier`, and `Recording.__str__` renders `original_name`, so every processed recording published its uploaded filename into a permanent column — past the mask that keeps it out of `ObjectChangeLog`, past the author-private gate on the API, and past every erasure path, none of which touch that column. It now stores a content-type-and-pk locator. The other five are the bulk-import path (a raw username and the operator's source directory in `Activity` metadata; the per-file source filename and error text unmasked in `ObjectChangeLog`) and the same directory-in-metadata shape in `index_dicom`. Inventory gained `ImportJob` / `ImportJobFile`; `PipelineRunAudit` recorded above as a gap that opens on first write. Retention windows, processor flows and the other project registrations verified unchanged, and the active project's recording-name column confirmed a documented pseudonym rather than a leak. Regression tests in [activity/tests/test_system_activity_identifier.py](../activity/tests/test_system_activity_identifier.py) and [recordings/tests/test_import_audit_hygiene.py](../recordings/tests/test_import_audit_hygiene.py). **Operator step:** rows written before this date still carry the names — see below. |

Existing `Activity` rows are not rewritten by the fix. A deployment that
processed recordings before 2026-08-26 has their filenames in
`target_identifier`, and the column has no erasure path, so the only remedies
are to overwrite it or to accept it. Where the deployment has not yet gone to
production, the cheapest answer is to clear the column for the affected verbs:

```python
from activity.models import Activity

LOCATOR = r"^[a-z_]+\.[a-z]+:[0-9]+$"
scoped = Activity.including_archived.filter(
    interface__in=[Activity.Interface.CELERY, Activity.Interface.COMMAND],
)
scoped.exclude(target_identifier="").exclude(target_identifier__regex=LOCATOR).update(target_identifier="")
```

The interface filter is what makes this safe to run. Only `with_system_activity`
wrote renderings; `API` rows carry the request path in the same column, put
there by the middleware, and blanking those would destroy legitimate audit
context to fix a problem they never had. The regex then spares any locator
already written under the new behaviour, so the statement is idempotent.

It is still a direct write to the audit trail, which is otherwise forbidden —
do it before there is a trail worth preserving, or not at all.

## Keeping this document current

This document is enforced, not aspirational:

- **Per-commit:** the [`gdpr-compliance` review agent](../.review/agents/gdpr-compliance.md)
  blocks commits that add a personal-data model or an outbound flow without
  extending the [Data inventory](#data-inventory) or
  [Processor and cross-controller flows](#processor-and-cross-controller-flows)
  tables, alongside its erasure-registration and log-hygiene checks.
- **Per-feature:** any change to an erasure path, retention window, masking
  or subject-PII registration, or processor updates the affected section in
  the same commit — same rule as app READMEs (AGENTS.md → Documentation
  workflow).
- **Periodic:** re-run the full GDPR audit (the four-lens sweep: inventory /
  retention, security of processing, third-party flows, project plugins)
  before each production release or every six months, whichever comes first,
  and refresh [Known gaps](#known-gaps) from the results.
- **At release:** the operator-facing fact sheet (docs.epicurrents.io →
  Platform → Data protection, source in the docs/epicurrents submodule)
  summarises this document for deployment operators. Changes to the
  processor table, the retention windows, or the erasure mechanics
  propagate there with the release notes that ship them — the fact sheet
  stays stable between releases, this document churns with the code.
- **On erasure-request friction:** if fulfilling a real request needs a step
  this document doesn't describe, the procedure section is wrong — fix it
  with the fulfilment.
