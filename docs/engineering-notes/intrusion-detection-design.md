# Detecting unauthorised direct database and filesystem access — design note

**Status: v2 design. The evidence host shipped as [examples/evidence-host/](../../examples/evidence-host/) and the dead-man heartbeat with it; every Layer 1 phase below is still open.** The audit trail is tamper-*evident* — [activity/audit.py](../../activity/audit.py) chains every logged change under an HMAC key and [activity/integrity_check.py](../../activity/integrity_check.py) walks the chains daily — but every control the platform has observes writes that went *through Django*. An actor who reaches Postgres with the database password, or the recordings volume with a shell on the host, operates entirely beneath that layer: the chain stays perfectly valid, it simply stops describing reality. This note sets out what can be detected from inside the Docker stack, what necessarily belongs to the VM administrator, and where the boundary between them actually falls — because the GDPR Art. 32(1)(d) story and the Art. 33 breach-notification story both depend on that boundary being stated honestly rather than blurred.

> **Revision note (v2, 2026-08-23).** A clean-slate review of v1 found one control that defeated itself (pgaudit's role scoping exempted exactly the stolen-credential attacker the threat matrix ranks most realistic), two structural blind spots (transient tampering between scan runs; no signal when the detection tasks themselves stop running), and two surfaces missing from the threat model entirely (Redis, the backup repository as an offline read path). It also concluded that three of the fixes are worth making *architectural* rather than additive: flipping audit coverage from context-gated to model-gated so the coverage check verifies a declared contract instead of a hand-maintained list; naming the off-host half of the design as an **evidence host** topology instead of a scatter of operator obligations; and **chain-head anchoring** as a cheap middle path to the hash-key custody problem v1 could only file under residual risk. v2 folds all of this in. The phase numbering changed: Phase 0 is new (the registry), Phase 7 is new (anchoring), and Layer 2 is restructured around the evidence host.

> **Margin note — sandbox timing.** The platform has no live deployments and no ingested back catalogue, so Phase 0's registry and Phase 1's coverage check can be established against a clean baseline: every row in a registered table is expected to carry audit coverage from its creation. Once a deployment goes live before those land, that stops being true and the check needs a per-model baseline watermark (rows created before coverage began are exempt), which is extra state worth avoiding if the ordering can be kept.

## Threat model and framing

The existing controls answer *"was the logged history forged?"* The question this note answers is *"did something happen that was never logged at all?"* — and that question splits into two halves with very different answers.

**Writes are detectable from inside the application.** A direct `UPDATE recordings_recording SET …` leaves no `ObjectChangeLog` row, so the newest audit row for that object no longer describes the object's current state. The platform already stores everything needed to notice: `after_hash` is computed over the post-change state (`hash_payload_state` in [activity/audit.py](../../activity/audit.py) reconstructs it from `before_state` + `changes`), and `serialize_instance` produces the comparable view of the live row. Nothing currently compares the two. That comparison is Phase 1 — with one honest bound: it sees **net divergence at scan time**. A modify-then-revert, or a transient escalation (insert a grant row, use it, delete it) that completes between two runs, leaves live state matching the newest audit row and is invisible to the check by construction. Closing that window requires write-ahead-log evidence, which is an evidence-host concern (Layer 2), not an application one.

**Reads are not detectable from inside the application, ever.** A `SELECT` against the PHI tables, or a `cp` of an EDF file off the recordings volume, changes no application-visible state. Detection requires either the database engine (pgaudit, connection logging) or the kernel (auditd, eBPF). Of those, only the database half can ship inside the compose stack. This is the single structural fact that determines the shape of everything below, and it is the one to state plainly in the compliance documentation rather than leave a reader to infer.

| Attacker | Reaches | Detected by | Notes |
|---|---|---|---|
| Credential holder, no host access | Postgres over the `epicurrents` network with `DB_PASSWORD` from a leaked `.env`, a CI secret, or a restored backup | Phase 1 (writes), Phase 3 connection anomaly (any session), honeytoken (reads) | The most realistic non-insider case. Note the pgaudit limit in Phase 3: statement-level read visibility for *this* attacker is unattainable, because they connect as the application role. |
| Redis credential holder | The broker with `REDIS_PASSWORD` from the same `.env` | Phase 6 ACLs (prevention), Falco connection rules | Injecting Celery tasks executes near-arbitrary actions in worker context; flushing the federation JWT replay cache re-enables token replay; resetting rate-limit counters unthrottles brute force *and* degrades the existing `auth.*` signal. v1 omitted this surface entirely. |
| Container-tier compromise (`web` / `celery` RCE) | uid 1000 inside the container, DB and Redis credentials in the environment, data volumes mounted | Phases 1, 3, 4; narrowed by Phase 6 | Already runs unprivileged by design; Phase 6 removes what remains. |
| Host root (SSH, docker socket) | Everything, including the ability to stop the watchers and rewrite local logs | Evidence-host controls only; Phase 7 bounds forgery | In-stack controls buy latency here, not certainty. Anchoring converts "undetectable forgery" into "divergence at the next anchor comparison". |
| Insider operator | Same as host root, legitimately | Attribution + off-host evidence, nothing else | Undetectable in principle until host access is individually attributable — see [Layer 2](#layer-2--the-evidence-host-and-the-remaining-host-controls). |
| Backup holder | Borg archives — the full PHI corpus, readable offline with the passphrase from the same `.env` | Nothing after the fact | An encryption-and-custody case, not a detection case: the archive plus one file yields everything. The evidence host's append-only repository bounds *destruction* of backups; nothing bounds reading a copied archive. |

Two properties of the current stack shape the design and are worth stating before the phases:

- **The application connects as a superuser.** `POSTGRES_USER=${DB_USERNAME}` in [docker-compose.yml](../../docker-compose.yml) creates the role, and all three settings modules connect as that same `DB_USERNAME`. `web`, `celery`, `celery-beat` and `migrate` therefore all hold superuser on the database. Nothing downstream can distinguish an application query from an operator's `psql` session, which makes both least-privilege and per-role audit scoping impossible until it is fixed.
- **Audit coverage is context-gated, not model-gated.** `_track_sender` in [activity/signals.py](../../activity/signals.py) fires for *any* model saved under `is_audited_context()` — set by [epicurrents/middleware.py](../../epicurrents/middleware.py) for requests and by `with_system_activity` ([activity/system_activity.py](../../activity/system_activity.py)) for tasks and commands. The covered-model set is therefore emergent, not declared: a legitimate write from a path that enters neither context produces no audit row, and no artifact in the codebase says which models are *supposed* to be covered. v1 worked around this with a hand-maintained settings list; v2 fixes it structurally in Phase 0, because a coverage check against a list that can drift from reality verifies the list, not the coverage.

## Design principles

- **Three trust domains, each doing what only it can.** The *application host* generates tamper evidence and verifies its own coverage; the *evidence host* holds what the application host must be able to append but never modify — logs, WAL, backups, anchors — and raises the alarm when the application host goes quiet; only *host-level instrumentation* observes who opened a file or a socket. Ship each control in the domain where it actually works, and do not package a host control as a compose service and call it portable.
- **Absence is a signal.** Every detection task emits a one-line clean-run summary (as `run_integrity_check` already does), and the evidence host alerts on the summary's *absence*, not only on findings. Without this, the cheapest attack against everything in Layer 1 is `docker stop celery-beat` — after which no anomaly is ever reported and no rule notices, because every alert fires on event presence.
- **One alerting surface.** Every new finding goes through `log_security_event` into the `epicurrents.security` stream with a well-known `security_event_type`, exactly like the existing taxonomy. Operators should not have to learn a second alerting system for this class of event, and [examples/observability/rules.yaml](../../examples/observability/rules.yaml) grows rules rather than the stack growing a parallel pipeline.
- **Report locators, never values.** A divergence report names the object and the fields that diverged. It never carries the differing values — the security stream leaves the host by design and the hashed-identifier rule in [docs/operations.md](../operations.md) applies with more force here than anywhere else, because the fields involved are PHI-bearing by definition.
- **A false alarm costs the control its credibility.** The first spurious `audit.state_divergence` page teaches the operator to ignore the second. Checks that race the running application re-verify under a consistent snapshot before emitting; checks that can fire on innocent causes get their innocent causes closed first (the admin-window interaction in Phase 1) or documented in the runbook triage path.
- **Do not oversell to the DPO.** Each phase gets an explicit statement of what it does *not* see. The compliance value of this work is that the residual risk is written down, not that the residual risk is zero.

## Architectural changes

Three of v2's changes alter structure rather than add controls. They are collected here because each pays for itself outside this note's scope too.

**Model-gated audit registration (Phase 0).** An explicit `AUDITED_MODELS` registry becomes the single source of truth for what the audit trail covers: the signal handlers consult it, the coverage check walks it, and a registered model saved outside an audited context becomes a loud write-time error instead of a divergence finding days later. This closes the loop the context-gated design cannot: the check then verifies exactly what the trail *claims* to cover, by construction. It also gives the bulk-operations gap (ROADMAP: *Activity — close the bulk-operations audit-trail gap*) the registry that entry already wanted, so the two converge instead of shipping parallel mechanisms.

**The evidence host (Layer 2).** v1 listed the off-host log sink, WAL archiving, append-only backups, and dead-man monitoring as separate operator obligations. They are one thing: a second, minimal machine — naturally a tailnet node, riding the connectivity the federation work ([federation-tailscale.md](federation-tailscale.md)) is already building — that the application host can append to but never modify. Naming it as a deployable topology converts eight "operators should…" paragraphs into one unit with a defined contract, and it is the piece that makes Layer 1 mean something: every in-stack control produces evidence on the host it is trying to protect, so the control is really the shipping latency to somewhere the attacker cannot reach.

**Chain-head anchoring (Phase 7).** The hardest problem in v1's residual-risk section was that `ACTIVITY_HASH_KEYS` lives in `.env` on the same host, so host root forges audit rows outright and Phase 1 verifies the forgery. The full fix — KMS/HSM key custody — is heavy for self-hosted deployments. Anchoring is the middle path: periodically publish each shard's chain head (`content_type`, latest `sequence_no`, `after_hash`) somewhere the host cannot rewrite. A forger can rewrite history and re-chain it under the stolen key, but cannot make the published head match; the forgery surfaces at the next anchor comparison instead of never. The federation mesh is the natural transport — peers already hold each other's Ed25519 keys, so instances can countersign each other's heads for mutual tamper evidence at zero new infrastructure — with the evidence host as the target for unfederated deployments.

## Layer 1 — controls that belong in the Docker stack

### Phase 0 — model-gated audit registration

The architectural prerequisite for Phase 1. A registry in the activity app — `register_audited_model(model, *, coverage_excluded_fields=())` at app-ready time, mirroring the shape of `register_masked_fields` — declaring every model whose rows the audit trail covers. Three consumers:

1. **The signal handlers.** `_track_sender` adds `sender in audited set` to its gate. For registered models the context check inverts from a filter into an assertion: a save of a registered model *outside* an audited context raises in development and emits `audit.unaudited_write_path` in production, turning silent coverage loss — the exact failure mode the ⚠️ load-bearing banner on [activity/signals.py](../../activity/signals.py) warns about — into a first-class event.
2. **The coverage check** (Phase 1) walks the registry, not a settings list.
3. **The bulk-operations work** (existing ROADMAP entry) gains the registry it needed to know which managers to wrap; that entry becomes a hard prerequisite for registering any model that bulk paths touch, rather than a caveat discovered on the first scan.

Registration is deliberately per-model rather than "everything currently audited": the migration to the registry *is* the audit of unaudited write paths, done model by model, and the registered set at any moment is the honest statement of coverage — which is precisely the artifact the GDPR documentation currently lacks. Unregistered models keep the current context-gated behaviour unchanged, so the migration is incremental and the load-bearing contract tests extend rather than move.

Per-model `coverage_excluded_fields` lives on the registration, not in settings — the exclusions are code-level facts about the model (`last_login`, denormalised counters, cache timestamps), shaped like `_DIGEST_EXCLUDED_SIGNAL_FIELDS` in [recordings/audit_digests.py](../../recordings/audit_digests.py), each justified in a comment because every exclusion is a field an attacker may edit freely.

### Phase 1 — audit-coverage verification (state divergence)

The reverse of the existing check. `verify_chain` asks whether the logged history is internally consistent; this asks whether live state still matches what the history says it should be. New module `activity/coverage_check.py`, consumed by the existing `verify_audit_integrity` task alongside `run_integrity_check` so operators keep one scheduled entry point.

Per object of each registered model:

1. Take the newest `ObjectChangeLog` row for `(content_type, object_id)` — by `sequence_no` for v3 rows, falling back to pk order for any legacy v1/v2 row whose `sequence_no` is NULL (moot in the sandbox, cheap to get right now).
2. Run `verify_change_hash(row)` first. A row that does not verify is a chain problem already reported by `verify_chain` — skip it rather than double-reporting, because a divergence computed against a tampered row means nothing.
3. `logged = hash_payload_state(row.action, row.before_state, row.changes)`; `live = serialize_instance(obj)`; compare key-wise minus the registered exclusions.
4. **On mismatch, re-verify before emitting.** The audit row commits atomically with the model write (post_save fires inside `save_base`'s atomic block), but the checker's two reads under autocommit can interleave with a concurrent commit and produce a transient tear. Re-read both inside one `REPEATABLE READ` transaction; emit only if the divergence survives. This is the false-alarm principle made concrete.

Findings, all new event types: `audit.state_divergence` (live state differs from the newest verified row — a direct `UPDATE`), `audit.unlogged_object` (a row in a registered table with no change-log entry — a direct `INSERT`), `audit.phantom_deletion` (the newest row is not a `delete`/`erase` and the object is gone — a direct `DELETE`).

**Cost.** Full-table comparison is fine for `Recording`, wrong for the annotation tables. Same shape as the derived-state check: a bounded sliding window per run (`ACTIVITY_COVERAGE_WINDOW_DAYS`) plus a full sweep on a longer cadence, with a watermark so successive runs advance. The task ends every run — clean or not — with the summary line the evidence host's absence alert keys on.

**What it does not see.** Net divergence only: tampering reverted between scans is invisible (WAL evidence on the evidence host bounds this — see Layer 2), and a tamper that also forges a matching audit row is caught by Phase 7's anchors, not here.

**Useful interaction.** The ROADMAP entry *Security — Django admin is an unaudited personal-data window* describes admin writes that produce no audit rows. Phase 1 turns that silent gap into a loud one: every admin edit surfaces as `audit.state_divergence`. Either close the admin gap first, or accept a permanently noisy channel — the two entries should be scheduled together.

### Phase 2 — database role separation and an append-only change log

Split the single superuser into two roles:

- `epicurrents_owner` — owner and schema authority, used by the `migrate` service only. New `DB_OWNER_USERNAME` / `DB_OWNER_PASSWORD`, generated by `init_env` alongside the existing secrets.
- `epicurrents_app` — the runtime role for `web`, `celery`, `celery-beat`. `CONNECT` plus DML on application tables; `SELECT` and `INSERT` only on `activity_objectchangelog` and `activity_activity`.

On top of the grants, three engine-level guards, each closing the previous one's bypass:

- A `BEFORE UPDATE OR DELETE` trigger on `activity_objectchangelog` that raises, so history cannot be rewritten even where a grant is later loosened by accident.
- An **event trigger** that logs (or refuses) `DROP TRIGGER` / `ALTER TABLE` against the audit tables — because the row trigger's own removal is otherwise a single silent statement for anyone holding owner credentials.
- A `pg_hba.conf` tightened alongside: the app role accepted only from the container subnet, superuser local-only. This converts "leaked password" into "leaked password *and* network position", and it is a few lines in the same deploy step.

The grants and triggers ship as an **idempotent management command run at deploy** (decided 2026-09-01), not a data migration. Two reasons. The core extraction (`core-extraction-plan.md`, kept in the archive repository's `docs/engineering-notes/`,) regenerates every migration, and a regeneration silently drops `RunSQL` operations while `migrate` reports success — the exact silent-failure shape this note exists to close. And a migration runs once against migration history, while grants and triggers are deployment state: a command can be re-run to verify or repair them, which is worth having for objects an attacker's first move is to remove.

Three things the split buys beyond least privilege: an application-tier compromise loses `COPY TO PROGRAM` and `pg_read_server_files`; a tamper attempt against the audit table becomes a failed statement that logging can catch rather than a silent success; and `epicurrents_app` becomes a stable identity that Phase 3's anomaly rules can key on.

**Open design point.** The erasure path ([activity/erasure.py](../../activity/erasure.py)) legitimately `UPDATE`s change-log rows to write `erased_at` / `erased_hash`. The trigger must either whitelist exactly that column set or the erasure routine must run under the owner role. The first keeps erasure in the normal request path but weakens the trigger to a column-level rule; the second keeps the trigger absolute but puts a privileged connection inside a user-triggered code path. Decide before writing the command.

### Phase 3 — Postgres connection and statement logging

Two tiers, and the cheap one carries most of the value.

**Cheap tier — no image change.** `log_connections=on`, `log_disconnections=on`, `log_statement=ddl`, and a `log_line_prefix` carrying user, database, application name and client host, set via `command:` on the `db` service. Set `OPTIONS: {"application_name": "epicurrents-web"}` (and the celery equivalents) in the settings modules so application sessions self-identify. The application connects from known containers, as a known role, with a known application name; **any other connection to Postgres is an anomaly worth paging on** — and with `log_statement=ddl`, so is any schema change outside a migration window, which is what catches an attempt on Phase 2's triggers. This is the highest read-detection value per unit of effort anywhere in this note, and it is roughly six lines of compose.

**Full tier — pgaudit.** Statement-level logging via a derived image (`docker/postgres/Dockerfile`: `FROM postgres:17.11` plus `postgresql-17-pgaudit`), pinned by hand under the same discipline as the existing pins, scoped by role so operator and unknown sessions are recorded statement by statement while application traffic stays out of the log.

**The scoping limit, stated honestly.** Exempting `epicurrents_app` from pgaudit exempts *anyone who connects as* `epicurrents_app` — including the leaked-`.env` attacker the threat matrix ranks most realistic. Statement-level visibility for an attacker using stolen application credentials is unattainable without logging the application's own traffic, which no deployment will sustain. The compensating control is session-level: connection logging records every session regardless of role, so a `epicurrents_app` connection from an unexpected `client_addr`, or without the expected `application_name`, is the alert for that attacker — the *session* is caught even though its statements are not. The rules in [examples/observability/](../../examples/observability/) must encode this (a connection-anomaly rule keyed on role + client address), and the operator documentation must not imply pgaudit covers the stolen-credential case.

Both tiers need the `db` container's stdout scraped by promtail. And both carry the same caveat, which belongs in the operator documentation verbatim: an attacker with host root edits `postgresql.conf`, or simply stops the container. The logging is worth exactly what the shipping latency to the evidence host is worth.

### Phase 4 — stored-file integrity sweep

`Recording.file_hash` and `MediaFile.file_hash` are both SHA-256 digests of the stored bytes, written at ingest, indexed, and never read back for verification. A new `verify_storage_integrity` task closes that for both models:

- **Every run, cheap:** `stat()` each stored file and compare size against the recorded value. File timestamps are normalised to UNIX epoch 0 by `os.utime` in `process_recording`, which makes mtime an unusually clean signal for recordings — **a non-zero mtime on a processed recording is by itself anomalous**, with no baseline state to maintain. (Any future legitimate rewrite path — a reprocessing campaign, say — must re-set the epoch-0 utime or this signal is lost; worth a comment at the `os.utime` call site.)
- **Rotating, expensive:** re-hash a slice of the corpus per run (`RECORDINGS_INTEGRITY_REHASH_FRACTION`) so the whole set cycles within a bounded period.
- **Inverse walk:** files under `RECORDINGS_UPLOAD_PATH` / `MEDIA_UPLOAD_PATH` with no corresponding database row. This is the one that catches copies staged for exfiltration, alongside the more mundane case of an interrupted purge.

Events: `storage.file_hash_mismatch`, `storage.file_size_mismatch`, `storage.file_missing`, `storage.file_unexpected` — with a `model` field distinguishing recording from media rows. Ends with a clean-run summary line for the absence alert.

The originals volume (`RECORDINGS_ORIGINALS_PATH`) stays out of scope. The write-only rule in AGENTS.md is absolute and `validate_originals` — which reads filesystem metadata and manifests only — remains the sole path that touches it. Do not let an integrity sweep become the reason a read path appears there.

### Phase 5 — canaries and a honeytoken

Two cheap constructs with near-zero false-positive rates.

**A canary recording.** A synthetic recording plus stored file, created by a management command, owned by the system user, referenced by no workflow and hidden from listings by the same mechanism as FAILED recordings. Phase 4 alarms on any change to its bytes, Phase 1 on any change to its row, and Phase 3's pgaudit on any query that touches it — no legitimate code path issues one. Cost: one row and one file. Event: `canary.touched`.

**A honeytoken credential.** A plausible but non-functional share token, planted in the database and referenced by no code path. This works because `public_share_token` is stored **in plaintext** (a unique `CharField` on `AccessRight`), so it cannot be discovered except by reading the database directly — *any presentation of it against the API is proof of a database read*. The share-token path already logs rejected credentials, so most of the detection machinery exists. This is the only read-detection primitive in this note that works without pgaudit or host tooling, which makes it disproportionately valuable relative to its size. Event: `canary.honeytoken_used`. **Standing caveat:** if share tokens are ever hashed at rest, the honeytoken silently stops working — the change that hashes them must revisit this phase.

### Phase 6 — stack hardening

Prevention rather than detection, but it narrows what a compromise reaches and keeps the signal from Phases 3 and 4 clean. Four groups:

- **Runtime posture.** Extend the `x-production-runtime` anchor in [docker-compose.prod.yml](../../docker-compose.prod.yml) with `read_only: true` plus a tmpfs for `/tmp`, `cap_drop: [ALL]`, and `security_opt: [no-new-privileges:true]`; mount data volumes `:ro` on services that only read them. A side benefit worth putting in the runbook: with `read_only` in place, `docker diff` on a running container should be empty, making it a ten-second forensic check.
- **Redis ACLs.** Redis 7 supports per-user command and key restrictions: separate users for the Celery broker, the cache, and the federation replay cache, with `FLUSHALL` / `FLUSHDB` / `CONFIG` denied to all of them. This bounds what any single leaked Redis credential can do — task injection needs the broker user, replay-cache flushing needs that user, and neither can reconfigure the server. The `requirepass` single-password model currently in [docker-compose.yml](../../docker-compose.yml) is the floor, not the ceiling.
- **Secrets posture.** Environment-variable secrets are visible in `docker inspect`, `/proc/<pid>/environ`, and crash dumps, and the single `.env` means every service holds every secret — celery does not need the VAPID private key, web does not need the Borg passphrase, and nothing but the anchoring task should hold whatever key Phase 7 signs with. File-based compose secrets plus per-service env splits shrink exactly the blast radius the residual-risk section worries about for `ACTIVITY_HASH_KEYS`.
- **Image supply chain.** The compose pins are version tags (`postgres:17.11`), not digests — a registry-side substitution serves a trojaned image invisibly. `image@sha256:…` digest pinning fits the existing deliberate-pin-bump discipline (the bump commits change one line more).

The stack already runs every service unprivileged as uid 1000 and mounts the docker socket nowhere — both are load-bearing properties for everything above, and the second deserves stating as a rule rather than remaining an accident of the current layout.

### Phase 7 — chain-head anchoring (decision-gated)

The architectural answer to the key-custody problem: publish each shard's chain head off-host on a fixed cadence, so a forger holding `ACTIVITY_HASH_KEYS` can rewrite history but cannot make the published head match.

**Why a head, and why repeatedly.** A hash chain pins *backwards*: every row's hash depends transitively on its predecessors, so one published head freezes the entire prefix behind it — rewrite row 500 and the hash at row 1000 necessarily changes. That is what makes anchoring cheap. There is no need to publish a hash per row, because the chain already relates them; and there is no value in publishing the *genesis* hash, because a chain constrains nothing ahead of the point you pinned. What must be published is the latest head, again and again. The cadence is therefore the security parameter, not an implementation detail: it is exactly the window in which an attacker can rewrite freely and never be contradicted.

**Verification needs no secret.** Checking an anchor means reading whatever row now sits at that `sequence_no` and comparing its stored `after_hash` against the anchored value — no HMAC key is involved. This is what makes an anchor usable as third-party evidence: the holder can verify without holding anything that would let them forge, so an evidence host, a federated peer and an external auditor are all equally capable of it. The signature on the bundle is therefore not part of verification; it exists so that an attacker cannot *publish* a fake anchor agreeing with their forged chain.

Mechanics:

- A Celery task collects, per content_type shard, `(content_type, latest sequence_no, latest after_hash)` plus a timestamp, signs the bundle, and delivers it to the anchor target(s).
- Verification is the receiving side's job: the anchor holder compares each new bundle's *previous* head against what the chain now says at that sequence number. A mismatch means rows at or before the anchored point were rewritten. Event: `audit.anchor_mismatch`, emitted by the verifying instance with the anchored and recomputed heads' locators.
- **Transport, in preference order.** Federated peers countersigning each other's heads — the peer channel is already Ed25519-authenticated, both sides already run this codebase, and mutual anchoring gives each instance tamper evidence hosted by an organisation the attacker has not compromised. For unfederated deployments, the evidence host is the target; failing that, any append-only medium the operator controls off-host.
- The anchor task's signing key is *not* `ACTIVITY_HASH_KEYS` — a separate key, held only by this task (see Phase 6 secrets posture), so that forging rows and forging anchors require two different thefts.
- **An anchor is a set of heads, not one value.** The chain is sharded per `content_type` and `verify_chain` walks a single shard, so a bundle that omits a shard leaves that shard free to be rewritten with nothing to contradict it. The bundle must enumerate every shard present, including shards with no new rows since the last bundle — an absent shard and an unchanged shard have to be distinguishable, or deleting a shard's rows entirely reads as quiet.
- **Anchor `after_hash`, not the row payload.** Subject erasure under Art. 17 rewrites audited rows in place: `erase_subject` scrubs the registered PII, stamps `erased_at`, and re-seals into `erased_hash` while deliberately leaving `after_hash` untouched, precisely so chain links keep verifying afterwards (see [activity/README.md](../../activity/README.md)). An anchor over `after_hash` therefore survives erasure untouched. An anchor over anything payload-derived would break on every erasure request, and the operational consequence is worse than the false alarm: whoever watches `audit.anchor_mismatch` would learn that it fires during normal business, which is how a real mismatch gets waved through.

**Why decision-gated.** The peer transport depends on the federation network layer currently mid-flight (ROADMAP: *Federation — tailnet network layer + management-command flow*); designing the anchor exchange before that stabilises risks building on moving ground. The evidence-host transport has no such dependency and could land first, with peer countersigning added as transport two. What anchoring does *not* do: it detects forgery at anchor cadence, it does not prevent it, and it says nothing about rows written and forged entirely between two anchors whose heads happen to re-converge — which the per-shard `sequence_no` monotonicity makes hard but the design should not claim impossible without analysis.

## Layer 2 — the evidence host, and the remaining host controls

### The evidence host

A second, minimal machine — a tailnet node, reachable by the application host for appends only — holding everything the application host must not be able to rewrite. This is a deployment topology to document and ship as its own small compose stack, not a feature of the platform: its contract is *append-only from the application host's perspective, alerting independent of the application host's health*. Its components:

- **The log sink.** [examples/observability/](../../examples/observability/) currently ships Loki on the same host, which is right for a worked example and worthless as tamper evidence. The evidence host runs the Loki (or equivalent) that matters, fed by a write-only token; the same-host example stays as the development story.
- **Dead-man alerting.** The ruler on the evidence host carries, alongside the event-presence rules, absence rules keyed on the clean-run summary lines: no `verify_audit_integrity` summary in 26 h, no `verify_storage_integrity` summary in its window, no log flow at all from the application host — each pages. This is the control that makes stopping `celery-beat` (or promtail, or the whole stack) a detected event instead of the perfect crime, and it *must* live off the application host to mean anything.
- **WAL archiving.** Postgres `archive_command` shipping WAL segments to the evidence host gives point-in-time forensic reconstruction of every write — the answer to Phase 1's transient-tamper blind spot, and after any alarm from any phase, the difference between "something changed" and knowing exactly what and when. It doubles as PITR backup. (A logical-decoding consumer that cross-checks each decoded change against the audit stream in near real time is the maximal version; it is real engineering and belongs in a future revision only if the batch checks prove insufficient.)
- **The backup repository, append-only.** A Borg repo on the evidence host with `borg serve --append-only` means a host-root attacker can neither destroy the backup history nor rewrite it — the archives become evidence with a retention clock, not just recovery material. Use of the `borg-restore` profile should itself be an alertable event in the log stream. What this does not fix: the passphrase in `.env` still makes any *copied* archive readable offline; that is a custody problem (Phase 6 secrets posture reduces which services hold it) and an honest residual.
- **Anchor storage** for Phase 7's non-federated transport.

**Packaging — decided 2026-08-29: shipped in this repository, deployed from a separate clone.** The question splits, and the two halves pull opposite ways. The rules need to track the platform version, because the dead-man and event-presence rules key on log lines this codebase emits; [epicurrents/security_log.py](../../epicurrents/security_log.py) is load-bearing precisely because a silent rename leaves the application logging happily while every rule stops matching, and a rule set in a separate repository turns each rename into a two-repository change nobody remembers to make. The *deployment* must not track anything, because the evidence host's whole contract is that compromising the application host does not compromise it — deploy it from the same clone, update it with the same `update.sh`, reach it with the same key, and it is an expensive second copy of the thing it exists to escape.

So the stack lives next to [examples/observability/](../../examples/observability/) and is deployed to separate hardware from its own clone, with its own credentials and its own update cadence. Make the version coupling mechanical rather than remembered: a contract test asserting that every event type referenced by the alert rules exists in the taxonomy, in the shape [epicurrents/tests/test_proxy_asset_headers.py](../../epicurrents/tests/test_proxy_asset_headers.py) uses for the Caddyfile — read the expected values out of the running definition rather than restating them, since restating them is the drift.

A git submodule is the one arrangement that fails both halves: it couples the deployment, which is the part that must stay independent, while still leaving the rules a separate commit away from the taxonomy they track.

**What it delivers before any Layer 1 phase exists**, which is what makes it a sound first slice rather than scaffolding for unbuilt work: the log sink, the dead-man rule for *no log flow at all from the application host* (which needs no phase-specific summary line and turns "someone stopped the stack" into a page), WAL archiving for point-in-time forensics, and the append-only Borg repository. That last one closes an operational gap independently of this design — `BORG_REMOTE_REPO` is unset, so there is no off-host backup today, and `borg serve --append-only` on the evidence host is one.

### Host controls proper

These cannot live in the compose stack, for a structural reason worth writing down once: a control that watches the container boundary has to sit outside it, and read visibility requires kernel-level instrumentation. Packaging one as a compose service with `privileged: true` and host mounts changes the packaging, not the trust boundary.

- **auditd watch rules** — the canonical answer to "who read this file". Watch the docker volume roots for `recordings-data`, `media-data` and `postgres-data`; `/var/run/docker.sock`; the borg client config; and `.env`, which holds `DB_PASSWORD`, `REDIS_PASSWORD`, `ACTIVITY_HASH_KEYS` and the Borg passphrase and is therefore the single most valuable file on the box. Containers are ordinary host processes, so host rules do observe container I/O — but they attribute it to uid 1000, so the rules need to filter the application's own access by pid or comm before the signal is usable. Ship the events to the evidence host.
- **Falco or Tetragon** — the highest-coverage tool for this threat model. Stock rules already cover `docker exec` into a running container, an unexpected shell spawn, reads of sensitive files, and unexpected outbound connections; worthwhile additions are any process other than `postgres` opening `postgres-data`, `pg_dump` / `psql` / `redis-cli` executed anywhere on the host, and any unexpected process connecting to 5432 or 6379. It *can* ship as an opt-in compose profile alongside `tailnet` and `observability` — but if it does, the README must say plainly that it is a host control in compose packaging.
- **Host file-integrity monitoring** (AIDE, Tripwire, or a Wazuh agent) over binaries, `sudoers`, the compose files and the built images — the control that catches persistence after a compromise. The baseline database lives on the evidence host or is signed, or the attacker simply rebaselines.
- **Full-disk encryption (LUKS).** Detects nothing, but bounds "direct file access" to a running host and covers the disposed-disk case. Borg repokey covers the archives at rest; the passphrase-custody residual above still applies.
- **Per-administrator SSH identities, no shared root, and sudo session logging.** *"Unauthorised" is undefined until authorised access is individually attributable.* Today anyone holding the deploy key is indistinguishable from the operator, so no amount of host instrumentation can produce an attributable finding — this is a precondition for the rest of Layer 2, not an extra.
- **Egress controls.** A database dump has to leave somehow. The tailnet posture already removes most of the inbound surface; an egress allowlist and volume alerting are the complement.

## New security event types

Additions to the taxonomy in [epicurrents/security_log.py](../../epicurrents/security_log.py), which is ⚠️ load-bearing — the module docstring, the contract tests (`test_security_log_taxonomy.py`), and the table in [docs/operations.md](../operations.md) all have to move in the same commit.

| Event type | Phase | Fields | Signal |
|---|---|---|---|
| `audit.unaudited_write_path` | 0 | `content_type`, `object_id` | A registered model was saved outside an audited context — a coverage bug in the platform's own code, caught at write time. |
| `audit.state_divergence` | 1 | `content_type`, `object_id`, `change_id`, `diverged_fields` | Live state differs from the newest verified audit row — a write that bypassed Django. |
| `audit.unlogged_object` | 1 | `content_type`, `object_id` | A row in a registered table with no change-log entry — a direct `INSERT`. |
| `audit.phantom_deletion` | 1 | `content_type`, `object_id`, `change_id` | A registered object is gone with no `delete` / `erase` row — a direct `DELETE`. |
| `audit.anchor_mismatch` | 7 | `content_type`, `anchored_sequence_no`, `peer` | A previously published chain head no longer matches the chain — history was rewritten, possibly under a stolen HMAC key. |
| `storage.file_hash_mismatch` | 4 | `model`, `object_id`, `stored_name` | Stored bytes no longer hash to the recorded `file_hash`. |
| `storage.file_size_mismatch` | 4 | `model`, `object_id`, `stored_name` | Cheap-tier size drift, ahead of the rehash cycle. |
| `storage.file_missing` | 4 | `model`, `object_id`, `stored_name` | A database row whose file is gone. |
| `storage.file_unexpected` | 4 | `path_digest`, `volume` | A file on a data volume with no database row — possible staged exfiltration. Hash the path; do not log it. |
| `canary.touched` | 5 | `canary_id`, `surface` | The canary row or file changed. |
| `canary.honeytoken_used` | 5 | `token_digest`, `ip` | A planted credential was presented — proof of a direct database read. |

`diverged_fields` carries field **names** only. `storage.file_unexpected` carries a path digest rather than the path, because filenames on these volumes can embed subject identifiers. Dead-man detection is deliberately *not* an event type — the whole point is that it fires when the application emits nothing, so it lives as absence rules on the evidence host.

## New settings

| Setting | Default | Notes |
|---|---|---|
| `ACTIVITY_COVERAGE_CHECK_ENABLED` | `False` | Off until Phase 0's registered set is established; enabling it is a deliberate operator act. |
| `ACTIVITY_COVERAGE_WINDOW_DAYS` | `7` | Sliding window per run, mirroring `ACTIVITY_DERIVED_CHECK_WINDOW_DAYS`. |
| `RECORDINGS_INTEGRITY_CHECK_ENABLED` | `False` | Phase 4 opt-in. |
| `RECORDINGS_INTEGRITY_REHASH_FRACTION` | `0.05` | Share of the corpus re-hashed per run; sizes the rotation period against the schedule. |
| `DB_OWNER_USERNAME` / `DB_OWNER_PASSWORD` | — | Phase 2. Generated by `init_env`; consumed by `migrate` only. |
| `ACTIVITY_ANCHOR_ENABLED` | `False` | Phase 7 opt-in. |
| `ACTIVITY_ANCHOR_TARGETS` | `[]` | Anchor destinations — peer identifiers and/or the evidence-host endpoint. |

The audited-model set and per-model field exclusions are code (the Phase 0 registry), not settings — coverage is a property of the codebase, not of a deployment.

## Documentation surface

- **[docs/gdpr-compliance.md](../gdpr-compliance.md)** — a *Detection of unauthorised access* subsection under Art. 32, stating the in-stack / host split and the three trust domains explicitly, with the evidence host and the host controls named as operator responsibilities rather than platform features. This is the section a DPO or an auditor will actually read.
- **A new operator doc for the evidence host** (or a major section in [docs/operations.md](../operations.md)) — the topology, its compose stack, the append-only contract, and the dead-man rules. This replaces the scattered "ship it off-host" advice.
- **[docs/operator-runbook.md](../operator-runbook.md)** — what each new event means and the first response to it. `audit.state_divergence` in particular needs a documented triage path, because the innocent explanation (an unaudited write path) and the hostile one look identical in the log line; the `docker diff` check from Phase 6 belongs in the first-response list.
- **[docs/operations.md](../operations.md)** — the security-event table, and the Phase 3 cheap-tier configuration.
- **[activity/README.md](../../activity/README.md)** — the threat-model section gains the registry, the coverage check, and anchoring as controls distinct from chain verification.
- **[examples/observability/rules.yaml](../../examples/observability/rules.yaml)** — rules for the new event types, the connection-anomaly rule (which has no application event behind it), and — clearly marked as evidence-host-only — the absence rules.

## Residual risk — what this does not close

- **No in-stack control detects a read.** Phases 1, 4 and 5 detect writes; the honeytoken detects the *use* of read data. Only pgaudit and host-level auditing see the read itself — and pgaudit's role scoping means the stolen-app-credential attacker's statements are structurally invisible even there; that attacker is caught at the session level (connection anomaly) or not at all.
- **Transient tampering between scans is invisible to Phase 1.** Modify-then-revert inside one scan interval leaves no net divergence. WAL archiving on the evidence host bounds this forensically (the write is reconstructible after any alarm), but nothing in-stack alerts on it in real time short of the logical-decoding consumer deliberately deferred.
- **Host root defeats Layer 1, given time — anchoring bounds the damage to a detection delay.** Every in-stack control produces evidence on the host it protects; the control is really the shipping latency to the evidence host, and for audit forgery specifically, the anchor cadence. Neither is prevention.
- **`ACTIVITY_HASH_KEYS` still lives on the application host.** Phase 7 makes forgery under a stolen key *detectable*; only off-host key custody (KMS/HSM, or a signing agent) would make it *infeasible*, and that remains out of scope and worth its own ROADMAP entry.
- **A copied backup archive is readable offline.** Append-only mode protects the repository's integrity and availability, not its confidentiality; the passphrase's custody (Phase 6) is the only lever.
- **Detection is not prevention, and it does not shorten the Art. 33 clock.** What it changes is where the clock starts: from an internal signal rather than from an outside report.
- ~~**The Django admin gap remains an in-application bypass** of the same trail~~ — closed 2026-08-25. The admin is no longer mounted and account management moved to `/api/v1/user/admin/`, which is inside the audited path set. The residual it named is now only `django_admin_log`, which holds rows written before the retirement and is tracked in [ROADMAP.md](../../ROADMAP.md) with the rest of dropping the app.
- **v1 / v2 legacy change-log rows** are outside the chain walk today (noted in [activity/integrity_check.py](../../activity/integrity_check.py)); the coverage check inherits that boundary since it verifies the row before comparing against it.

## Open decisions

1. **Append-only trigger vs the erasure path** — column whitelist, or run erasure as the owner role. Blocks Phase 2's command.
2. **The initial registered-model set for Phase 0**, and the order in which the bulk-operations work lands relative to registering the models it affects.
3. **pgaudit derived image vs cheap tier only.** The derived image gives up the "official pinned upstream image" property the compose pins maintain deliberately — and the scoping limit above caps what it buys. The cheap tier plus the honeytoken may be the right stopping point.
4. **Anchoring transport order** — evidence host first (no dependency) with peer countersigning as transport two, or wait for the federation network layer and design the peer exchange once. Gated on that work stabilising either way.
5. **Whether Falco ships as a compose profile at all**, or stays documentation plus a reference rule set.
6. **Long-term custody of `ACTIVITY_HASH_KEYS`** — anchoring reduces the urgency from "the trail is forgeable" to "forgery is detected at anchor cadence", but off-host custody remains the real fix and should be its own ROADMAP entry.
7. **Whether the honeytoken is acceptable** in a database under a PHI inventory: it is not personal data, but the erasure and data-inventory documentation should be checked before a deliberately fake credential is planted.
8. ~~**Evidence-host packaging**~~ — decided 2026-08-29: shipped in this repository, deployed from a separate clone on separate hardware. Reasoning under [The evidence host](#the-evidence-host). The residual the original framing named still stands and should be said out loud in the stack's README: shipping it does not mean CI tests it.

## Suggested order

**Resequenced 2026-08-29: the evidence host goes first**, ahead of Phase 0. The table below orders the work by dependency, and by that measure the evidence host is item 7 — but it depends on nothing, and three of its four day-one components pay off against the deployment as it stands today rather than against phases not yet written. The ordering constraint it does carry is one-directional: the phases below must not be *claimed* as controls until it exists, which is satisfied by building it first and is not satisfied by building it seventh. Everything else keeps the order shown.

| Order | Item | Rationale |
|---|---|---|
| 1 | Phase 0 — audited-model registry | Architectural prerequisite for Phase 1; the registration pass is itself the audit of unaudited write paths. |
| 2 | Phase 2 — role split + triggers + pg_hba | Unblocks Phase 3's scoping and is the largest least-privilege win independently. |
| 3 | Phase 3 — cheap tier (connections + DDL) | Highest detection value per unit of effort in the note. |
| 4 | Phase 1 — coverage check | Now verifies a declared contract; lands with its dead-man summary line. |
| 5 | Phase 4 — storage sweep | Reuses `file_hash` on both models; already indexed. |
| 6 | Phase 5 — canary + honeytoken | Small, and the honeytoken is the only in-stack read signal. |
| 7 | Evidence host — sink + dead-man rules + WAL archive + append-only borg | Where Layer 1's evidence becomes durable and its silence becomes audible. Can begin any time; must exist before the phases above are *claimed* as controls. |
| 8 | Phase 6 — stack hardening | Independent of the rest; digest pinning and Redis ACLs are quick wins inside it. |
| 9 | Host: auditd rules, then Falco (decision 5) | Read observation and container-boundary watching. |
| 10 | Phase 7 — anchoring | Decision-gated on transport (decision 4). |
| 11 | pgaudit full tier (decision 3), LUKS, per-administrator attribution | Highest cost or lowest marginal detection value; attribution is the precondition for treating any host-level finding as attributable. |
