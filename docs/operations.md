# Operations cookbook

**Audience: operators and developers.** Intent-keyed reference for day-to-day work on a running stack. Each entry is "I want to do X" → command(s). The stack-management, backup, and update sections are operator-facing; the "After making code changes" and "Development-only operations" sections are for developers.

> For an incident — a crashed or degraded system — start with the [operator runbook](operator-runbook.md) rather than scanning this cookbook. For diagnosing a specific symptom, see [docs/troubleshooting.md](troubleshooting.md). For first-time setup, see [docs/getting-started.md](getting-started.md).

All commands below assume you are in the platform repository root and the Docker stack is running (`docker compose up -d`) unless noted.

## After making code changes

### I changed a Python file

```bash
scripts/apply-changes.sh
```

Builds the frontend, runs any pending migrations, and restarts `web`, `celery`, and `celery-beat` so all Python services pick up the change. Use this as the default after touching any backend code.

If you only changed a view (no model or task changes), `docker compose restart web` is enough and faster.

### I changed a model

```bash
docker compose exec web python manage.py makemigrations
scripts/apply-changes.sh
```

`makemigrations` generates the migration file from your model change; `apply-changes.sh` then runs `migrate` and restarts services. Commit the generated migration alongside the model change.

### I changed a frontend file

If the Vite dev server is running (`npm run dev` from `frontend/`), changes are picked up automatically — nothing to do.

If you are running the built frontend served by Django:

```bash
scripts/rebuild-frontend.sh             # platform-only iteration (assumes viewer dist/ is current)
scripts/rebuild-frontend.sh --viewer    # also rebuild viewer per-workspace dist/ (after clean clone or viewer-source change)
```

Rebuilds the frontend bundles and restarts `web`.

### I changed `.env`

Restart the services that read the changed variable. For most variables `docker compose restart` is enough:

```bash
docker compose restart web celery celery-beat
```

Special cases:

- `EPICURRENTS_PROJECT` — use [`scripts/switch_project.sh`](../scripts/switch_project.sh) instead; changing the value alone leaves the database in an inconsistent state.
- `DB_*` — `docker compose down && docker compose up -d` so the `db` service picks up the new credentials.
- `WEBPUSH_VAPID_*` or `FEDERATION_*` keys — restart `web` and `celery`; existing browser subscriptions and federated peers may need to re-register against the new keys. The federation app refuses to start if `FEDERATION_PUBLIC_KEY` does not match the key derived from `FEDERATION_PRIVATE_KEY` — see [troubleshooting.md](troubleshooting.md#service-wont-start-improperlyconfigured-federation_public_key-does-not-match) if the restart fails. For routine federation key rotation, prefer the two-phase overlap flow (`rotate_federation_keys --announce` → `--promote`) over editing the env vars directly; see [federation/README.md](../federation/README.md#key-rotation).

### I changed `requirements.txt` or `Dockerfile`

The image needs rebuilding:

```bash
docker compose build web celery celery-beat
docker compose up -d
```

The Dockerfile is multi-stage. `runtime` is what the application services build and is the default for a bare `docker build .`; `test` is `runtime` plus `requirements-test.txt` and is what the `test` and `test-postgres` profile services build. A dependency that only pytest needs goes in `requirements-test.txt`, which never reaches a production container. After changing test dependencies, rebuild with the profile so the right stage is picked up:

```bash
docker compose --profile test build test
```

**Changing a production dependency also means regenerating the lock.** `requirements.txt` is the file you edit; `requirements.lock` is generated from it and is what the image installs, with `--require-hashes`, so an artifact that does not match the resolution fails the build instead of shipping:

```bash
scripts/lock-requirements.sh
```

That runs uv inside a container pinned to the image's Python, so you need no uv on the host and the resolution does not depend on your machine. Commit the regenerated lock alongside the `requirements.txt` change — CI runs `scripts/lock-requirements.sh --check` and fails if the two have drifted, because a stale lock means the vulnerability audit and the image disagree about what is installed. `pip-audit` runs against the lock rather than `requirements.txt`, so transitive packages are in scope.

Adding a package to `requirements.txt` and forgetting to regenerate installs the old closure silently; the contract test in [test_image_targets.py](../epicurrents/tests/test_image_targets.py) catches that case without waiting for a build.

### I changed `package.json`

```bash
cd frontend && npm install && cd ..
scripts/rebuild-frontend.sh
```

## Stack management

### Start the stack

```bash
docker compose up -d
```

First start runs migrations and creates the admin user from `ADMIN_*` env vars. Subsequent starts skip the bootstrap and come up faster.

### Stop the stack

```bash
docker compose down          # preserves volumes (database, recordings, etc.)
docker compose down -v       # DESTROYS volumes — all data lost (dev only)
```

The `-v` form is destructive. For dev resets use [`scripts/reset.sh`](../scripts/reset.sh) — it refuses to run unless `DJANGO_MODE=development`.

### Restart a single service

```bash
docker compose restart <service>     # web | celery | celery-beat | db | redis
```

Restart is faster than `down`/`up` and preserves volumes. Use it after any change to a service's environment or after a worker crash.

### TLS certificates

Only relevant with the bundled proxy (`PROXY_DOMAIN` set in `.env`; see [getting-started.md](getting-started.md#exposure-and-tls)). Caddy obtains and renews certificates on its own — renewal starts about a third of the way through the lifetime and needs no cron entry — so these are diagnostic commands, not routine ones.

```bash
docker compose logs caddy | grep -iE "certificate|acme"        # what issuance actually did
docker compose exec caddy ls -R /data/caddy/certificates       # what is currently held
```

There is no `caddy list-certificates` subcommand; the on-disk layout under `/data/caddy/certificates/<ca>/<domain>/` is the readable answer, and each domain directory holds the `.crt`, `.key` and a `.json` with the issuer and expiry.

Certificates and the ACME account key live in the `caddy-data` volume. Treat it as state: losing it forces re-issuance on the next start, and Let's Encrypt's duplicate-certificate limit refuses after a handful of those in a week.

After editing `caddy/Caddyfile`, reload without dropping connections:

```bash
docker compose exec caddy caddy reload --config /etc/caddy/Caddyfile
```

If issuance fails, the cause is almost always outside the stack: the domain does not resolve to this host yet, or port 80 is closed and the HTTP-01 challenge cannot complete. Work it out against Let's Encrypt's staging directory rather than the production one — set `PROXY_ACME_CA=https://acme-staging-v02.api.letsencrypt.org/directory`, which has far higher rate limits and issues untrusted certificates. Clear the staging certificate afterwards (`docker compose exec caddy rm -rf /data/caddy`) or the browser keeps warning.

### Byte-serving offload

With the proxy overlay deployed, a raw recording download is handed to Caddy instead of streaming through a gunicorn thread for the whole transfer. `PROXY_FILE_OFFLOAD_ENABLED` is set by [docker-compose.proxy.yml](../docker-compose.proxy.yml) and is off everywhere else, because the capability is a statement about deployment topology — the proxy has to be in front *and* mounting `recordings-data` read-only at `/srv/protected/recordings` — rather than a judgement about risk.

Django is not bypassed. It handles, authenticates, authorises and audits every request, including every Range request the viewer issues while seeking; it simply answers with an empty 200 and an `X-Serve-Path` header instead of the bytes. So the `Activity` trail is unchanged, and a 403 or a 404 is produced by the same code as before. What Django no longer observes is whether the transfer *completed* — which it never observed anyway, since a client can abandon a streamed response mid-flight.

It never applies to a grant with `apply_middleware=True`. Those bytes are computed per request, so there is no file to hand over; the request streams as it always did. Enabling middleware for a population therefore also disables their offload, which is worth knowing before changing a grant on a deployment that depends on the throughput.

To confirm it is working, download a recording as a grantee whose access right has `apply_middleware=False` and look at the proxy's access log rather than the app's: the app logs a small 200, the proxy logs the full byte count.

```bash
docker compose logs --tail=50 caddy | grep '"uri":"/recordings'
```

The proxy's `PROXY_MAX_BODY_SIZE` and the application's `RECORDINGS_MAX_UPLOAD_SIZE` are two ceilings on the same thing, set in different files. If the proxy's is the lower of the two, uploads in the gap pass every check the operator can see and then die at the edge with a bare 413. Django refuses to boot on that mismatch rather than letting it surface as a mystery upload failure, so a stack that comes up has already agreed with itself. Mind the units when overriding: Caddy reads `2GB` as 2,000,000,000 and only `2GiB` as 2,147,483,648, so the plausible-looking `2GB` is *below* the 2 GiB application default.

A download that 404s for one specific recording while the metadata endpoint works is the path mapping having drifted — Django, `caddy/Caddyfile` and the compose mount all have to agree on `/srv/protected/recordings`. `epicurrents/tests/test_offload.py` pins that pairing, so run it before assuming a data problem.

### Tail the logs

```bash
scripts/logs.sh              # follow all services, last 100 lines
scripts/logs.sh web          # follow one service
scripts/logs.sh web 500      # custom tail line count
```

Equivalent to `docker compose logs -f --tail=N [service]`.

### Raise log verbosity

Set `LOG_LEVEL` (all application modules) and/or `DJANGO_LOG_LEVEL` (request
handling, ORM) in `.env`, then restart the service you're watching:

```bash
# .env
LOG_LEVEL=DEBUG
```

```bash
docker compose restart web celery
```

Lower it again afterwards — `DEBUG` is noisy and increases log-shipper volume
in production. Reading failure traces, the `processing_error` field, and
background-task state is covered in [docs/debugging.md](debugging.md).

### Run a management command

```bash
scripts/manage.sh <command> [args...]
```

Always runs inside the Docker container so it targets PostgreSQL. See [docs/management-commands.md](management-commands.md) for the full index.

### Open a Django shell

```bash
scripts/manage.sh shell
```

An interactive Python shell with Django configured. `Recording`, `User`, and other models are accessible after importing.

### Open a database shell

```bash
docker compose exec db psql -U $DB_USERNAME $DB_NAME
```

Direct PostgreSQL shell. Useful for ad-hoc queries; for writes prefer the Django shell or a management command so audit trails are kept.

## Audit and compliance

### Query the federation audit log

`FederationAuditLog` records one row per inbound federation request that reached the access-decision stage — successful accesses and access denials, but **not** auth failures (those are in the Django `WARNING` log). The table answers the compliance question "which peer, acting on whose behalf, accessed which object when, with what outcome". See [federation/README.md](../federation/README.md#federationauditlog) for the model schema and operational policy (append-only, retention, indistinguishability invariant).

Common queries via the Django shell:

```python
scripts/manage.sh shell
>>> from federation.models import FederationAuditLog

# What did peer X access in the last 24 hours?
>>> from datetime import timedelta
>>> from django.utils import timezone
>>> cutoff = timezone.now() - timedelta(hours=24)
>>> FederationAuditLog.objects.filter(
...     peer__url="https://peer.example.com",
...     created_at__gte=cutoff,
... ).order_by("-created_at")

# Who accessed recording with pk=42?
>>> from django.contrib.contenttypes.models import ContentType
>>> from recordings.models import Recording
>>> ct = ContentType.objects.get_for_model(Recording, for_concrete_model=False)
>>> FederationAuditLog.objects.filter(
...     target_content_type=ct, target_object_id="42"
... ).order_by("-created_at")

# All denials by a remote user in the last week
>>> FederationAuditLog.objects.filter(
...     remote_user_id="user-42",
...     status_code__in=(403, 404),
...     created_at__gte=timezone.now() - timedelta(days=7),
... )
```

Retention is the deployment's regulatory minimum — HIPAA-style deployments must keep at least 6 years. A management command for CSV export (SAR / breach response) is planned; query via shell or admin until then.

**Where to look for grant-deletion history.** `FederationAuditLog` records inbound access events, not administrative grant changes. When investigating "what happened to peer X's grants" — for example after deleting a peer, which cascades to every `AccessRight` row for that peer (see [federation/README.md](../federation/README.md#peer-deletion)) — look at `activity.models.ObjectChangeLog` instead. Each cascaded grant produces a delete entry with the full `before_state`, captured by the activity app's `pre_delete` signal.

```python
>>> from activity.models import ObjectChangeLog
>>> from django.contrib.contenttypes.models import ContentType
>>> from epicurrents.models import AccessRight
>>> ar_ct = ContentType.objects.get_for_model(AccessRight)
>>> ObjectChangeLog.objects.filter(
...     content_type=ar_ct, action=ObjectChangeLog.ACTION_DELETE
... ).order_by("-created_at")[:20]
```

The activity signal only fires inside an API request context, so peer deletions performed via the Django shell or admin bypass it. Use the federation API endpoint for any deletion where audit retention matters.

**Tuning per-peer download limits.** Federated download paths enforce a per-peer daily byte budget and per-minute request rate to bound exfiltration by a compromised peer. Defaults (1 TiB/day, 60 req/min) are conservative for the abuse case; legitimate bulk workflows may need to raise them. Set `FEDERATION_PEER_DAILY_BYTE_LIMIT` and/or `FEDERATION_PEER_DOWNLOAD_RATE_LIMIT` in `.env` (`0` disables the corresponding limit) and restart `web`. Investigate suspected runaway clients via `FederationAuditLog` filtered on `status_code=429`. Full design rationale and the charging model (which charges full file size even for Range / slice requests) is in [federation/README.md](../federation/README.md#rate-limiting-and-quotas).

### Security log stream

Separate from `FederationAuditLog` and the `activity` audit trail (which record *data access* and *data changes*), the application emits a stream of security-relevant *operational* events — authentication failures, permission denials, rate-limit hits, federation auth failures, and audit-integrity alarms — to a dedicated logger so an external SIEM or log shipper can alert on them.

- **Logger name:** `epicurrents.security`
- **Level:** `WARNING`
- **Pivot field:** `security_event_type` — emitted as a discrete JSON key in production (alongside structured fields like `actor_id`, `ip`, `reason`), not just inside the message text. Alert rules filter on it.

The well-known event types (the authoritative per-event field list lives in the [epicurrents/security_log.py](../epicurrents/security_log.py) module docstring, which a contract test keeps in sync):

| Prefix | Event types | Signal |
|---|---|---|
| `auth.*` | `login_failed`, `login_lockout`, `login_blocked`, `password_reset_rate_limited` | Credential attacks, brute force |
| `permission.*` | `denied` | IDOR probing, escalation attempts |
| `federation.*` | `auth_failed` | Cross-instance auth probing or a misconfigured/compromised peer |
| `notifications.*` | `subscription_rejected` | SSRF probe via a push endpoint |
| `audit.*` | `hash_verification_failed`, `chain_break`, `chain_gap`, `genesis_invalid`, `hash_key_missing`, `derived_state_mismatch`, `derived_state_no_digester` | Audit-trail tampering or corruption — highest severity |
| `system.*` | `heartbeat` | The one event that reports nothing wrong. Every five minutes, carrying `interval_seconds` and no identifier. Alert on its **absence**: a stream of security events alone is silent on a healthy system, so an off-host sink cannot tell calm from a severed connection without it. Its silence covers the beat scheduler, the worker, the logging configuration, the shipper and the network at once |

**Ship it off-host.** All services log to container stdout; a compromise of `web` or `celery` can rewrite or truncate local logs, so the security stream must be shipped somewhere the application cannot reach. A worked Loki + Promtail + Grafana setup — parsing the JSON, promoting `security_event_type` to a label, and a starter set of alert rules (audit tampering, federation auth failures, login brute force, permission-denial spikes) — is in [examples/observability/](../examples/observability/):

```bash
docker compose -f docker-compose.yml \
  -f examples/observability/docker-compose.observability.yml \
  --profile observability up -d
```

That example runs the sink on the application host, which makes it a good way to learn the queries and no use at all as tamper evidence — an attacker who can rewrite the logs can rewrite the copy sitting beside them. For the arrangement that satisfies the sentence above, the sink lives on a second machine along with the alerting and the backup repository: [examples/evidence-host/](../examples/evidence-host/). Its dead-man rules are the part with no local equivalent, because they fire on the application host going quiet, and nothing hosted there can report on its own silence.

**Celery tasks log through Django, not Celery.** `CELERY_WORKER_HIJACK_ROOT_LOGGER = False` is what makes that true, and it is load-bearing for this stream rather than a style preference. `epicurrents.security` has no handler of its own and propagates to root; Celery replaces root's handlers on worker startup by default, so with the hijack on, every security event emitted from a task is plain text while the rest of the production stream is JSON. The events that suffer most are the ones that matter most — `audit.chain_break`, `audit.chain_gap`, `audit.genesis_invalid` and `audit.derived_state_mismatch` all come from `verify_audit_integrity`, a beat task. Any consumer parsing the line as JSON matches none of them, and nothing reports the mismatch.

**Never log raw PII.** Callers hash usernames and emails before passing them to the logger (`actor_id`, an integer FK, is fine); this matters more once the stream leaves the host. The rule is enforced by convention at the call site — see [AGENTS.md → Log security-related activity](../AGENTS.md#log-security-related-activity).

### Security headers

Production emits, in addition to nosniff / X-Frame-Options:

- **Strict-Transport-Security** — see the HSTS ramp below.

- **Referrer-Policy: `same-origin`** (via `SECURE_REFERRER_POLICY`).
- **Permissions-Policy** denying browser features the platform does not use (camera, microphone, geolocation, …) — override with `PERMISSIONS_POLICY`.
- **Content-Security-Policy** — see the rollout below.

CSP is **enforced** in production (`CSP_REPORT_ONLY=False`). It shipped report-only while the policy had never been checked against a running deployment; that pass is done, and a control that is off unless an operator knows to turn it on is off nearly everywhere. The baseline permits no third-party origin at all — Pyodide's runtime is vendored same-origin under `/vendor/pyodide/<version>/`. `'unsafe-inline'` remains in `script-src` because the statically served `index.html` carries inline bootstrap scripts and has no per-request nonce injection point.

**Nothing collects violation reports server-side.** Reading them means a person with the browser console open, not a log to grep. That is the whole reason enforcement is the default: an enforced policy fails visibly, where report-only on a deployment nobody is watching reports to no one.

The tuning pass covered the base platform and an installed project plugin across the SPA, viewer, upload and library views. Two configurations were **not** covered — the dicom plugin, whose OHIF viewer is a submodule that was not checked out at the time, and any project whose own views reach an external origin. If you are running either, go report-only for a cycle first:

1. Set `CSP_REPORT_ONLY=True` in `.env` and restart `web`.
2. Exercise the app — especially the viewer and any project-specific view — with the browser console open.
3. Extend `CONTENT_SECURITY_POLICY` (in `.env`, or in the project's `settings.py`) until no legitimate use produces a violation.
4. Remove the `CSP_REPORT_ONLY` override so the enforcing default applies again. Restart `web`.

Adding a `report-uri` / `report-to` directive to `CONTENT_SECURITY_POLICY` lets a collector gather violations instead, which is worth doing on a deployment with users you cannot ask.

The baseline policy is in [epicurrents/settings/production.py](../epicurrents/settings/production.py); override it wholesale via the `CONTENT_SECURITY_POLICY` env var. Dropping `'unsafe-inline'` from `script-src` (the strongest hardening) requires moving the inline scripts out of `index.html` or hashing them — a frontend change, tracked separately.

#### The HSTS ramp

`SECURE_HSTS_SECONDS` defaults to **300 seconds**, not the year that hardening guides recommend. The header is a one-way promise: a browser that has seen `max-age=31536000` refuses plain HTTP to the host for a year, and there is no mechanism to recall that from clients which already cached it. On a deployment whose certificate renewal has not yet been proven, a year-long value converts a lapsed certificate — or a domain that has to move — from a rollback into a year-long outage for everyone who visited in the meantime. Five minutes gives the same protection against an active downgrade attempt, and costs nothing to raise.

Raise it in steps, each after the previous value has served without incident, and only once the certificate has renewed at least once on the real domain:

| Value | When |
|---|---|
| `300` | Default. First deployment, certificate not yet renewed. |
| `3600` | Issuance and renewal both observed working. |
| `604800` | A week of normal operation. |
| `31536000` | Steady state. |

`SECURE_HSTS_PRELOAD` stays `False` until the year-long value has been serving without incident *and* every subdomain of the registered domain is HTTPS-only. Submission to the browser preload list is effectively irreversible — removal requests take months to propagate and cannot be forced.

The header comes from Django, never from the proxy, so the two layers cannot drift apart. Caddy-served static responses do not carry it; they do not need to, since the browser applies the policy per host and every SPA document response comes from Django.

### CSRF enforcement

The REST API is built on Django Ninja with the mounts running `csrf_exempt`, so cross-site request forgery protection on state-changing requests does not come from Django's middleware. It comes from two layers working together:

- **SameSite=Lax cookies** stop the browser attaching the session cookie to cross-site requests in the first place.
- **An explicit token check** runs for every session-authenticated unsafe request (POST/PUT/PATCH/DELETE) through `enforce_session_csrf` in [epicurrents/auth.py](../epicurrents/auth.py). The SPA reads the `csrftoken` cookie — seeded on the served `index.html` document — and echoes it back in the `X-CSRFToken` header; the backend rejects a session write that lacks a valid token with HTTP 403.

Federated peers (FederatedBearer JWT) and share-token callers authenticate with credentials a browser does not send automatically, so they are not a CSRF vector and bypass the check by design.

The master switch is `SESSION_CSRF_ENFORCED`:

- **Production** defaults it to `True`. Leave it on. Turning it off removes the token layer, leaving only SameSite.
- **Development** defaults it to `False`, because the Vite dev server serves the SPA from a different origin than the API and cannot read the `csrftoken` cookie to echo it back, and host tooling (httpie, curl) would otherwise need a token on every write. Set `SESSION_CSRF_ENFORCED=true` in a dev `.env` to exercise the production path locally — you then need to send the `csrftoken` cookie value back as the `X-CSRFToken` header on each write.

A 403 with "CSRF verification failed" on a logged-in user's write almost always means the SPA did not receive or could not read the `csrftoken` cookie. Confirm the served `index.html` set the cookie (check the response headers on the document request) and that the browser is sending it back on the API call.

### API rate limiting

A global request-rate throttle ([epicurrents/throttle.py](../epicurrents/throttle.py)) caps how fast a single identity can hit the REST API, complementing the auth-specific throttles (login per-username, password-reset per-email, federation per-peer). It bounds upload/annotation/library flooding and share-token enumeration.

The key design point is that it never keys naively on client IP. Deployments serve NAT'd shared-egress groups — a classroom annotating from one access point, a hospital behind a corporate proxy — that all present as a single address, so a per-IP throttle would lock out a whole room for one user. The throttle resolves an identity in priority order: authenticated user → `share_token` → session → client IP, and only the last-resort IP tier (for callers with none of the others) shares a ceiling across a NAT.

It is **on by default in production**, off in development, gated by `API_THROTTLE_ENABLED`. Limits:

- `API_THROTTLE_RATES` — per-minute ceilings for identified callers, keyed by scope. Defaults: `default` 300/min, `upload` 30/min. Override the individual values with `API_THROTTLE_RATE_DEFAULT` / `API_THROTTLE_RATE_UPLOAD`.
- `API_THROTTLE_SCOPE_MAP` — ordered `(path-prefix, scope)` pairs mapping paths to a scope; first match wins, everything else is `default`.
- `API_THROTTLE_IP_RATE` — the single high ceiling (default 1000/min) for unidentified IP-keyed callers. Set to `0` to defer IP-level limiting entirely to the reverse proxy.

A throttled request gets HTTP 429 with a `Retry-After` header and emits a `throttle.rate_limited` security-log event (the event carries the identity *kind*, not the identity itself). The throttle fails open: a cache outage lets requests through rather than 500-ing the API.

A project plugin that runs a shared-account population (the canonical case is a classroom on one student login) can disable or raise the limits in its `settings.py` — set `API_THROTTLE_ENABLED = False`, or widen `API_THROTTLE_RATES`. Because such a deployment keys everyone onto one `user` identity, the per-user ceiling is the one that bites; raise it or turn the throttle off there.

### Bound the workers

`CELERY_MEM_LIMIT` and `CELERY_CPUS` cap the worker container; `CELERY_CONCURRENCY` sets how many jobs run inside it. The two caps default to unset, which compose emits as no limit, and production warns at boot while the memory limit is unset. Setting `CELERY_MEM_LIMIT=0` is how you record a deliberate decision to run unbounded; it silences the warning without applying a cap. The pool size does carry a default of 2, because celery's own default is the host's CPU count, which sizes the container's peak by the host rather than by the workload.

The `0` idiom does not carry across to the pool. Celery reads a non-positive `--concurrency` as no value at all and falls back to the CPU count, so `CELERY_CONCURRENCY=0` restores the unbounded pool instead of recording a choice about it. Production warns at boot if it finds one.

The workers are the only tier worth capping. A denoise or a leadfield computation allocates in proportion to the recording rather than to request volume, so one large job can consume the host — and when it does, the kernel's OOM killer chooses by score, not by blame. Postgres is large, long-lived and shared-memory heavy, which makes it a strong candidate, so an ingest job takes the database down with it. `web` and `db` have no equivalent failure mode and are left unbounded.

**What the host can spare.** For a single-host deployment:

1. Start from total host RAM.
2. Subtract what Postgres will use (`shared_buffers` plus roughly `work_mem` × `max_connections` in the worst case; the stock container settles around 1–2 GB on a small host).
3. Subtract the web tier: roughly 200–400 MB per gunicorn worker, so `GUNICORN_WORKERS` × that.
4. Subtract about 1 GB for Redis, the proxy and the operating system.
5. Give the remainder to `CELERY_MEM_LIMIT`, rounded down.

On an 8 GB host with two gunicorn workers that lands near 4 GB.

**What the jobs will ask for.** A worker child sits at about 160 MB once it has run a signal job, and the job itself is added on top of that:

| Job | Peak above the floor |
|---|---|
| EEG lead field | ≈ 190 MB at the 7.5 mm default grid, ≈ 820 MB at 5 mm on a 10-05 montage |
| Recording ingest | Negligible — the EDF passes are per-data-record rather than whole-file |

**Check the two against each other.** Multiply the largest job you expect by `CELERY_CONCURRENCY` and confirm it fits inside `CELERY_MEM_LIMIT`. The defaults — a pool of 2 under a 4 GB cap — hold two concurrent lead-field builds at the 5 mm grid with room to spare, and the cap is what stops a finer grid, or a signal-processing job added later, from taking the host down with it. The same arithmetic is why the pool is pinned rather than left to celery's default: on a 4-core host the cap would have to hold four jobs instead of two.

Set `CELERY_CPUS` only if the workers are starving the web tier of CPU. A limit there bounds throughput rather than survival, and it does not change the pool size, so setting it below `CELERY_CONCURRENCY` leaves the children contending for a fraction of a core each.

**Telling a limit that is too low from a job that is genuinely broken.** A container killed for exceeding `mem_limit` exits 137 and the task disappears without a Python traceback, which reads like a crash:

```bash
docker inspect --format '{{.State.ExitCode}} {{.State.OOMKilled}}' $(docker compose ps -q celery)
```

`OOMKilled` true means the cap, not the code. Raise `CELERY_MEM_LIMIT` and re-run the same recording before looking for a bug.

## Backups

### Take a backup

```bash
scripts/backup.sh
```

Creates a Borg backup (database dump + recordings) and applies the retention policy. Backups land in the `borg-data` named volume, mounted at `/backup` in the borg container.

### List existing backups

```bash
scripts/backup.sh --list
```

### Restore from a backup

```bash
scripts/restore.sh
```

Interactive: choose an archive, choose what to restore (database, recording and media files, or both). File extraction runs through the `borg-restore` compose service, which mounts the data volumes read-write. **Stop the stack first** unless you really mean to overwrite a running system.

### Rehearse a restore

```bash
scripts/restore-drill.sh
```

An untested restore is a hypothesis. The drill seeds a database row and a recording file, backs both up through the same borgmatic config production uses, destroys the database volume and unlinks the file, restores, and asserts the row came back and the file is byte-identical. It also asserts both halves were genuinely gone before the restore, so a drill whose destroy step quietly did nothing cannot report a pass.

Everything runs under a dedicated compose project with its own network and volumes, and it publishes no host port, so it is safe to run alongside a live deployment. Pass `--keep` to leave the scratch stack up for inspection instead of tearing it down. Expect a few minutes: the run builds the image, initialises Postgres twice and creates a real Borg archive.

What the drill does not cover: an off-host repository (it exercises the local one), and whether anyone would notice a failing backup — that is [monitoring](#know-when-a-backup-fails).

### Know when a backup fails

A backup that has been failing for six weeks looks exactly like one that has been working, until a restore is attempted. Two independent reports close that:

- Every failure writes an `EPICURRENTS_BACKUP_FAILURE` line to the `borg` container's log, naming the repository and the error. The token is stable operator-visible API — build the log-shipper rule on it. Both borgmatic's own error hook and the entrypoint's fallback emit it, so the rule also catches borgmatic failing before it runs any hook at all.
- Setting `BORG_MONITOR_URL` to a Healthchecks-compatible ping URL (healthchecks.io, a self-hosted Healthchecks, anything speaking the same protocol) makes each run ping on start, finish and failure. The ping carries no content by default — the monitor is a third-party destination and "a backup failed" is the whole signal it needs. `BORG_MONITOR_SEND_LOGS=true` attaches the run's log tail, which is worth turning on when whoever gets paged has no shell access to the host; it is registered as a processor flow in [gdpr-compliance.md](gdpr-compliance.md#processor-and-cross-controller-flows).

The monitor is the stronger half, because it also alerts on *silence*. A hook inside the container cannot report that the stack is stopped; a monitor expecting a periodic ping notices exactly that. Treat the URL as a secret — anyone holding it can report your backups as healthy.

The borg container warns at start when either `BORG_MONITOR_URL` or `BORG_REMOTE_REPO` is unset, and the web container refuses to leave the local-only case unacknowledged: production with an empty `BORG_REMOTE_REPO` logs a warning at boot unless `BACKUP_LOCAL_ONLY=true` says the choice was deliberate.

### Choosing which repositories to write to

Two tiers, selected independently, and they are not substitutes — they fail in different ways:

| Failure | Local repository | Remote, append-only |
|---|---|---|
| Operator error, a bad migration | Yes, and fastest — no network in the path | Yes |
| Disk failure | Only if the volume is separate storage | Yes |
| Host loss | No | Yes |
| Host compromise, an attacker with root deleting archives | No | Yes |
| Provider or datacentre loss | No | Yes |

The bottom two rows are why the remote tier is append-only. An attacker with root on the application host holds the passphrase and can destroy any repository that host can write to, so a second copy is only worth what its write restriction is worth. Moving the local repository onto a separately mounted volume raises it one row and no further — it is still writable from the same machine.

By default the local tier is on. It is worth keeping: it is the one used for the ordinary case — something deleted an hour ago, restored at disk speed without depending on the remote host being reachable — and dedup plus compression mean the second copy is far smaller than the data it covers. Note that a stock deployment puts it on the same disk as the data, so out of the box it protects against mistakes rather than against hardware.

Set `BACKUP_LOCAL_ENABLED=false` for the remote-only arrangement, which is the right choice when disk is tight and an append-only remote is already in place. `BORG_REMOTE_REPO` must be set if you do: with both tiers off there is nowhere to write, and rather than emit a configuration that backs up nothing while reporting success, the backup container refuses to start. The web container logs the same at boot, because a refusal inside a container nobody opens is not a signal.

A value outside `true`/`false`/`yes`/`no`/`on`/`off`/`1`/`0` is also refused rather than treated as the default. `BACKUP_LOCAL_ENABLED=flase` quietly keeping the local tier would leave an operator believing they had turned it off, with nothing about the running system distinguishing the two.

### Keep the passphrase somewhere the host loss cannot reach

`BORG_PASSPHRASE` lives in `.env`, and `.env` is deliberately in no archive — an archive carrying the passphrase that decrypts it is not a control. The consequence is that losing the host loses the ability to read the backups, off-host copies included. The passphrase therefore needs custody arranged *before* it is needed, not after:

- Store it in a password manager the deployment's operators can reach without this host, or seal it in an envelope held wherever the organisation keeps its other break-glass credentials.
- Record alongside it which repository it opens and where that repository lives. A recovered passphrase with no repository address is not a recovery.
- Rehearse the retrieval as part of the [restore drill](#rehearse-a-restore) at least once, from the custody store rather than from `.env`.

The same applies to the rest of `.env`: `SECRET_KEY`, `ACTIVITY_HASH_KEYS`, the federation Ed25519 private key and the VAPID keys are all unrecoverable from a backup. Losing `ACTIVITY_HASH_KEYS` specifically means the restored audit trail can no longer be verified, which is a compliance problem rather than an availability one.

The residual worth stating plainly: because the passphrase sits in `.env` on the host, anyone who copies both an archive and `.env` can read the archive offline. That is a custody problem, not a storage one — it is not fixed by where the archives are kept.

### Export the repository keys, one per repository

`repokey` encryption keeps the key *inside* the repository, wrapped by the passphrase. So an intact repository plus the passphrase is everything you need, and the export protects against a different failure from the one above: damage to the repository itself. Lose the few hundred bytes of the repository config and the archives beside it become unreadable with a passphrase that is perfectly correct.

**A deployment with off-host backups has two keys, not one.** The local repository and the remote one are initialised separately, and each `borg init` generates its own key. They are unrelated, and exporting one leaves the other unrecoverable. This is the part that is easy to get wrong, because both repositories hold the same archives and open with the same passphrase, so one export looks like it covers both.

Run the export in the borg container, which is where borg and the passphrase already are. From the deployment root, with a directory bind-mounted for the output because nothing writable is mounted there by default:

```bash
mkdir -p ~/borg-key-export
docker compose -f docker-compose.yml -f docker-compose.prod.yml run --rm -T \
    -v "$HOME/borg-key-export:/out" --entrypoint borg borg \
    key export /backup /out/borg-key-local.txt
docker compose -f docker-compose.yml -f docker-compose.prod.yml run --rm -T \
    -v "$HOME/borg-key-export:/out" --entrypoint borg borg \
    key export "$BORG_REMOTE_REPO" /out/borg-key-remote.txt
```

Add whatever other compose overlays the deployment runs with, or the run reports the deployment's own containers as orphans. The container writes as root, so `chown` the results before copying them off.

Then move them off this host and delete the originals. Leaving them here protects against very little: the machine already holds the passphrase, and host loss is what the custody arrangement above is for.

Store the exports **apart from the passphrase**. Each export is still wrapped by it, so a file on its own is not a credential — but a file plus the passphrase is full read access to every archive in that repository, which is the pair an escrow arrangement exists to keep separated. `borg key export --paper` prints a checksummed block that can be put on paper and retyped, for the case where the storage holding your exports has failed too.

Re-export after any `borg key change-passphrase`, and after initialising any further repository.

## Updates

### Update to a new release

`scripts/update.sh` updates a running deployment and recreates its stack from one of two sources, then runs a shared tail: back up, rebuild the image, apply **all** pending migrations, collect static files, and recreate the application containers on the production overlay.

**From a distribution archive (default)** — for a deployment installed from a distribution tarball, with no git checkout. Drop the newer `epicurrents*.tar.gz` into `./update/`, then run `./update.sh` from the deployment root. The newest matching archive is applied over the deployment, preserving `.env` and the data volumes.

**From git** — for a checkout that follows the upstream platform:

```bash
scripts/update.sh --from repo            # git pull + frontend build, then the tail
scripts/update.sh --from repo --no-pull  # rebuild the current checkout (e.g. in CI, or after a manual checkout)
```

Before migrating, `update.sh` writes a pre-update snapshot — a database dump plus `.env` — under `./backups/` (and a full borg backup when borg is enabled). Undo the last update with `./update.sh --rollback`, which restores the most recent snapshot's database and `.env` and recreates the stack. Rollback covers data and config, not the code/image; re-apply the previous archive or git ref if a new build is itself the problem.

For development checkouts where you want manual control:

```bash
git pull
git submodule update --init --recursive
scripts/apply-changes.sh
```

### Dev vs production compose

The platform uses a base [docker-compose.yml](../docker-compose.yml) plus a [docker-compose.prod.yml](../docker-compose.prod.yml) overlay. The Python image is identical in both — what differs is what gets mounted at runtime and how the frontend bundles arrive on disk.

- **Dev (default)** — `docker compose <command>`. The web/celery containers bind-mount `.:/code` so the host source tree is what runs. Frontend bundles come from the host's `frontend/dist` and `frontend/viewer-dist`, built locally via `scripts/rebuild-frontend.sh` (which runs the Node toolchain on the host machine).
- **Production** — `docker compose -f docker-compose.yml -f docker-compose.prod.yml <command>`. The overlay replaces the `.:/code` bind-mount with selective read-only mounts of `frontend/dist` and `frontend/viewer-dist` (plus the runtime data volumes). The deploy host doesn't need Node installed; the frontend is built by the on-demand `frontend-build` compose service.

`scripts/update.sh` wires the overlay and the `frontend-build` invocation in, so the typical operator never types either flag manually. For ad-hoc operations against a running production stack, set the env var once per shell:

```bash
export COMPOSE_FILE=docker-compose.yml:docker-compose.prod.yml
```

…and plain `docker compose logs`, `docker compose ps`, etc. now operate on the production view of the stack.

### The `frontend-build` service

A Node 20 container defined in `docker-compose.yml` under the `build` profile. Profile-gated so it never starts with `docker compose up`. Invoke explicitly:

```bash
docker compose --profile build run --rm frontend-build
```

Runs `npm ci` in `frontend/`, then the same in `frontend/viewer/`, then `npm run build:viewer && npm run build`. Outputs `frontend/dist` and `frontend/viewer-dist` directly to the host filesystem via bind-mount. node_modules go to named volumes (`frontend-node-modules`, `frontend-viewer-node-modules`) so the container's linux-x64 binaries don't collide with the host's npm install if a developer also runs npm locally.

**Two use cases:**

1. **Deploy host (production)** — invoked by `scripts/update.sh --from repo` as the canonical way to refresh the frontend bundles on each deploy. The host carries the frontend source and viewer submodule on disk; the Node toolchain is what we don't want installed there, and the `frontend-build` service provides it via Docker without polluting the host.
2. **Dev convenience** — developers without Node installed on their machine can use it as an alternative to `scripts/rebuild-frontend.sh`. Slower than running npm directly on the host (container start overhead), but doesn't require Node installation.

> **Production-deploy prerequisites.** A clean clone is self-sufficient: `git clone + git submodule update + scripts/update.sh --from repo` produces a working stack with no manual prep. The `frontend-build` container runs `npm run build:tsc-all` in the viewer submodule before the platform build, which populates the per-workspace `dist/` directories (`viewer/util/*/dist`, `viewer/epicurrents/*/dist`) that the platform's vite aliases reference. tsc-only — webpack UMD outputs are not needed for the deploy path.

### Update submodules to their latest commits

```bash
git submodule update --remote --recursive
git add .gitmodules <any-updated-submodule-paths>
git commit -m "Update submodules"
```

`--remote` pulls the latest commit of each submodule's tracked branch, regardless of what the platform repo had pinned. Review the diff before committing.

## Development-only operations

### Reset everything

```bash
scripts/reset.sh
```

Destroys all containers, volumes, and data. Refuses to run unless `DJANGO_MODE=development` is set in `.env`. Optionally redeploys after the reset. Use this when the local stack is in a state you don't care to repair.

### Switch the active project

```bash
scripts/switch_project.sh <name>
```

Stops application services, deactivates the current project (archives its tables), edits `.env`, activates the new project, rebuilds the frontend, restarts services. Keeps `db` and `redis` running throughout. **Not intended for production** — the project layer is designed for one active project per deployment.

### Run the frontend against a mock backend

```bash
cd frontend
# Edit .env: set VITE_BACKEND_URL=mock
npm run dev
```

The Vite dev server serves an in-memory API with seeded data (see [`frontend/mocks.ts`](../frontend/mocks.ts) and [`frontend/README.md`](../frontend/README.md)). State resets on every full page reload. Useful when working on Vue components without needing the full backend stack.
