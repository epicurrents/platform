# Troubleshooting

**Audience: developers and integrators.** Symptom-keyed diagnostic guide for things that have gone wrong. Each entry follows the shape: what you see → likely cause → fix → where to look for more detail. Entries marked **(developer-assisted)** need a Django shell or code knowledge.

> **Operators / system administrators:** if a service has crashed or the system is down or degraded, start with the [operator runbook](operator-runbook.md) — it is black-box (no code knowledge needed) and escalates back here when a problem turns out to be code-level.

For *intentional* operations ("I want to do X"), see [docs/operations.md](operations.md). For first-time setup, see [docs/getting-started.md](getting-started.md). For the underlying *process* of investigating a failure — reading logs, failure fields, raising verbosity, inspecting background tasks — see [docs/debugging.md](debugging.md).

## Quick diagnostics

When something feels off, two commands usually narrow the problem:

```bash
docker compose ps                # which services are up?
scripts/logs.sh <service> 200    # last 200 lines from one service
```

`scripts/logs.sh` with no argument follows all services. Common service names: `web`, `celery`, `celery-beat`, `db`, `redis`.

## Tests and development environment

### Tests fail with "DJANGO_SETTINGS_MODULE is not set" or "Apps aren't loaded yet"

The settings module wasn't on the environment. Either run from the repo root so `pytest.ini` is picked up:

```bash
cd /path/to/platform && pytest
```

Or set it explicitly:

```bash
DJANGO_SETTINGS_MODULE=epicurrents.settings.test_platform pytest <path>
```

### Project plugin tests can't find the project's models

The project app is not in `INSTALLED_APPS` for the test run. Project plugins need their own `settings_test.py` that extends the platform's test settings:

```python
# projects/<name>/settings_test.py
from epicurrents.settings.test_platform import *  # noqa: F401, F403

INSTALLED_APPS = INSTALLED_APPS + ["projects.<name>"]  # noqa: F405
```

Then run:

```bash
DJANGO_SETTINGS_MODULE=projects.<name>.settings_test pytest projects/<name>/tests/
```

## Stack and infrastructure

### `docker compose up` fails with "bind for 0.0.0.0:8000 failed: port is already allocated"

The host port the `web` service exposes is in use by something else. Set `HOST_PORT` in `.env` to an unused port and restart:

```bash
# In .env:
HOST_PORT=8080

docker compose up -d
```

### `docker compose up` fails with permission errors on a data volume

The named volumes were never created with the right ownership. Run the helper:

```bash
docker compose run --rm init-volumes
```

This sets `1000:1000` ownership on the `recordings-data`, `staging-data`, `media-data`, `celery-data`, and `static` volumes (mounted at `/recordings`, `/staging`, `/media`, `/celery`, `/static`) so the unprivileged container user can write to them. Postgres manages the ownership of its own volume.

### Celery worker exits immediately

Almost always either Redis isn't reachable or the migrations haven't completed. Inspect:

```bash
scripts/logs.sh celery 200
docker compose ps redis db
```

If `redis` or `db` are not `running`, start them first:

```bash
docker compose up -d db redis
docker compose restart celery celery-beat
```

If both are running and Celery still exits, check that `REDIS_PASSWORD` is set in `.env` — the compose stack starts Redis with `--requirepass` and injects credentialed broker URLs into the app containers, overriding the `CELERY_BROKER_URL` value in `.env`.

### API requests fail with `relation "X" does not exist`

The database is missing tables — either migrations haven't run, or you've switched projects without using the proper lifecycle commands.

Check:

```bash
scripts/manage.sh showmigrations | head -40
```

If migrations are missing, apply them:

```bash
scripts/manage.sh migrate
docker compose restart web celery celery-beat
```

If the missing tables belong to a project you thought was active, the project activation is broken. Use [`scripts/switch_project.sh`](../scripts/switch_project.sh) to do the switch cleanly, or see [docs/getting-started.md](getting-started.md#starting-a-new-project-plugin) step 10.

> **Critical:** never run `python manage.py migrate` directly on the host while the Docker stack is up. The host's SQLite dev DB and the container's PostgreSQL are different databases; running migrations on the host applies them to the wrong place and corrupts the Docker stack's state.

## Authentication and login

### Login returns 429 "Too many failed login attempts"

The login rate limit has kicked in for this username. Default: 10 consecutive failures → 5 minute lockout. Two options:

- **Wait 5 minutes.** The lockout key in the Django cache expires on its own.
- **Clear the lockout immediately** from a Django shell:

  ```bash
  scripts/manage.sh shell
  >>> import hashlib
  >>> from django.core.cache import cache
  >>> key = hashlib.sha256("yourusername".lower().encode()).hexdigest()
  >>> cache.delete(f"login_lockout:{key}")
  >>> cache.delete(f"login_attempts:{key}")
  ```

Counter resets on the next successful login. See [user/README.md](../user/README.md#login-rate-limiting).

### Logged out immediately after successful login

Usually a cookie issue when running behind a misconfigured proxy:

- If using HTTPS termination at a reverse proxy, set `USE_X_FORWARDED_PROTO=True` in `.env` and make sure the proxy is sending `X-Forwarded-Proto: https`.
- If hitting Django directly over plain HTTP in production, `SECURE_SSL_REDIRECT=True` will fight the session cookie. Set `SECURE_SSL_REDIRECT=False` for plain-HTTP testing only.

### Forgot the admin password

If at least one superuser exists:

```bash
scripts/manage.sh changepassword <username>
```

If no superuser exists at all (e.g. you wiped the DB but kept `.env`):

```bash
scripts/manage.sh createadmin
```

This reads `ADMIN_USERNAME` / `ADMIN_PASSWORD` / `ADMIN_EMAIL` from `.env` and creates the user. To pick up a new `ADMIN_PASSWORD`, set it in `.env` first.

## Recordings

### Recording stuck in `PROCESSING` indefinitely

The Celery worker that picked up the `process_recording` task crashed or was killed. Inspect:

```bash
scripts/logs.sh celery 500 | grep -i recording
```

Recovery **(developer-assisted — requires a Django shell)**. An operator's first move is simply to restart `celery` (below), which lets *new* uploads process again; clearing the *already-stuck* rows is the shell step here:

```bash
scripts/manage.sh shell
>>> from recordings.models import Recording
>>> r = Recording.objects.get(content_hash="<hash>")
>>> r.status = Recording.Status.FAILED
>>> r.save()
```

Then re-upload the file. The original is preserved in `RECORDINGS_UPLOAD_PATH` and can be cleaned up manually if no longer needed.

If recordings consistently get stuck, the Celery worker has a persistent problem — restart it:

```bash
docker compose restart celery
```

Recordings older than `RECORDINGS_TRASH_RETENTION_DAYS` (default 30) that are still stuck in `PENDING` / `PROCESSING` are automatically reaped by `purge_deleted_recordings`.

### Recording marked `FAILED`

Processing failed but the row is preserved. Read the reason first — it's
written to `Recording.processing_error` (author/superuser-only):

```bash
scripts/manage.sh shell -c \
  "from recordings.models import Recording; \
   r = Recording.objects.get(content_hash='<hash>'); \
   print(repr(r.processing_error))"
```

A handled EDF/BDF parse failure records the parser's message; an unexpected
error records `Unexpected processing error: …`. Either way the worker log has
the full traceback:

```bash
scripts/logs.sh celery 500 | grep -i "EDF\|process_recording"
```

Common causes: corrupt or truncated upload, an EDF+D file with structure the
parser doesn't handle, or a converter (e.g. `.csv` → EDF) that failed. The
recording fork and how to correlate to the log line are covered in
[docs/debugging.md → Recording processing failures](debugging.md#recording-processing-failures).

### Upload returns `409` — "already in another collection"

Each recording can belong to **at most one collection globally**. The recording you're trying to add is already in a different collection. Either remove it from the existing collection first, or move it. See [library/README.md](../library/README.md#collectionitem) for the design rationale (two `UniqueConstraint`s on `CollectionItem`).

### Upload returns `413` — "File exceeds maximum upload size"

The file is larger than `RECORDINGS_MAX_UPLOAD_SIZE` (default 2 GiB). Either split the file or raise the limit in `.env`:

```bash
# In .env (value in bytes):
RECORDINGS_MAX_UPLOAD_SIZE=4294967296   # 4 GiB

docker compose restart web
```

Django's `DATA_UPLOAD_MAX_MEMORY_SIZE` is a separate, much smaller ceiling and does not need raising with it: it bounds non-file request data held in memory, and Django's multipart parser never applies it to a file part. What does have to move in step is `PROXY_MAX_BODY_SIZE` when the reverse-proxy overlay is deployed — the app refuses to start if the proxy's ceiling is the lower of the two.

### "I re-uploaded a file and now I have two sets of annotations"

Working as designed. `_annotation_hash(recording.pk, suffix)` is keyed on the recording PK, and re-upload creates a new Recording row with a new PK. The annotations from the previous upload are still attached to the previous Recording.

If you want a single set, delete the previous Recording (soft-delete via `DELETE /recordings/api/v1/{hash}`); annotations cascade-delete with their target. See [annotations/README.md](../annotations/README.md#gotchas).

## Annotations

### `400 Bad Request` on annotation create — "annotator identifier is required"

The request authenticated via `share_token` but the payload didn't include the `annotator` field. Token holders are anonymous to the platform; an explicit name is required so the annotation has an attributable author. Add `annotator: "<name>"` to the request body.

See [annotations/README.md](../annotations/README.md#annotator-field-on-annotation-in-schemas).

### Annotations don't appear after recording import

The author who ran `import_recordings` doesn't have an explicit `AccessRight` row on the recording, and the listing endpoint requires read access via `AccessRight` (not auto-granted by authorship). Two fixes:

- Create the access right manually for the recording author.
- Inspect the list as a superuser to confirm the annotations exist.
- Run `scripts/manage.sh explain_access recordings.recording <id> <username>` to see exactly which resolver step denies the read.

See the `AccessRight` for listing annotations note in [AGENTS.md](../AGENTS.md#testing-conventions).

## Federation

### Inbound federation request returns `401` "Unknown or untrusted federation peer"

Two cases:

- The peer is not registered. A superuser registers them via `POST /api/v1/federation/peers/`.
- The peer is registered but `is_trusted` is still `False`. By design, a superuser must flip this flag explicitly after verifying the peer out-of-band:

  ```bash
  scripts/manage.sh shell
  >>> from federation.models import FederatedPeer
  >>> FederatedPeer.objects.filter(url="https://peer.example.com").update(is_trusted=True)
  ```

Compare key fingerprints with the peer admin out-of-band before flipping. See [federation/README.md](../federation/README.md#identity-and-trust).

### JWT verification fails with "audience mismatch"

The `FEDERATION_INSTANCE_URL` configured on the receiving side doesn't match what the issuing peer puts in the JWT `aud` claim. The `aud` claim is matched **literally** — scheme, host, and trailing slash all count:

- This instance's `FEDERATION_INSTANCE_URL` in `.env` must match what other peers have registered as your URL.
- The remote peer's `FEDERATION_INSTANCE_URL` must match what you have registered for them in `FederatedPeer.url`.

Trailing-slash differences on `aud` are a common cause. Normalise to no trailing slash.

The `iss` claim, by contrast, is normalised server-side (`.strip().rstrip("/")`) before peer lookup, so trailing-slash variants on the sending peer's `FEDERATION_INSTANCE_URL` resolve correctly without operator intervention.

### Peer registration fails with `502` "URL ... resolves to non-public address — refusing to fetch (SSRF guard)"

`POST /api/v1/federation/peers/` resolves the candidate URL's hostname and rejects any address that isn't globally routable (loopback, RFC 1918 private, cloud metadata, link-local). This is the federation app's SSRF guard ([federation/README.md](../federation/README.md#outbound-url-safety-ssrf-guard)).

- **In production**: the URL is wrong. A federated peer should always be reachable at a public HTTPS address.
- **In dev / staging** where you legitimately federate two instances on the same host: set `FEDERATION_ALLOW_PRIVATE_PEER_URLS=True` in `.env` and restart `web`. Never carry that setting into production.

### Federated `inbound_check_object` returns `429` "Per-minute inbound rate limit exceeded"

A peer has hit `FEDERATION_PEER_INBOUND_RATE_LIMIT` requests per minute on the access-check endpoint (default 600/min). Two real causes:

- **Aggressive peer client doing per-object access pre-checks.** Legitimate use should batch or cache; if not feasible, raise the limit via `.env`.
- **Object-id enumeration by a compromised peer.** Check `FederationAuditLog` filtered to that peer + `status_code=429` — sustained 429s at the limit, mixed with 404s on objects that don't belong to the peer, is the enumeration signature. Investigate the peer's credentials before raising the limit.

### Federated download returns `429` "Daily byte budget exceeded" or "rate limit exceeded"

The peer has hit one of the per-peer download limits — by default 1 TiB/day of bytes and 60 requests/minute (see [federation/README.md](../federation/README.md#rate-limiting-and-quotas)). Two real causes:

- **Legitimate bulk download exceeding the conservative default.** Raise the limit via `FEDERATION_PEER_DAILY_BYTE_LIMIT` / `FEDERATION_PEER_DOWNLOAD_RATE_LIMIT` in `.env`, or set to `0` to disable per-peer. The default exists for the abuse case, not as guidance for normal sizing.
- **Compromised or buggy peer client retrying aggressively.** Inspect `FederationAuditLog` filtered to that peer + `status_code=429` (see [operations.md](operations.md#query-the-federation-audit-log)) — a sustained pattern of small requests at the rate limit is the telltale of a runaway client.

The byte budget resets at the next UTC day boundary; the rate limit resets at the next UTC minute boundary. Both counters are per-peer — one peer hitting a limit does not affect any other peer.

### Inbound federation request returns `401` "JWT replay detected"

The receiver's nonce cache has already seen this token's `jti` claim within the validity window. Three common causes, in order of likelihood:

- **Legitimate client retry / proxy retry of the same request.** Each federation request must carry a fresh token; if the peer's client retries a failed request without re-signing, the second attempt is rejected. The peer code needs to issue a new token per attempt.
- **An actual replay attempt** by something sitting between the peer and this instance. Investigate the request source via the federation audit log (`FederationAuditLog` — see [operations.md](operations.md#query-the-federation-audit-log)).
- **Cache misconfiguration in production.** A degraded cache backend (e.g. `LocMemCache` in a multi-worker deployment, which is per-process) makes "first seen" non-deterministic across workers and *would not* produce false-positive replays — but it would silently weaken protection. The cache itself doesn't cause replay errors; it just fails to prevent real ones. See [federation/README.md](../federation/README.md#gotchas) for the cross-process cache requirement.

### Inbound federation request returns `401` "JWT 'iat' is too old"

The peer's token claims an `iat` more than `DEFAULT_MAX_JWT_AGE + DEFAULT_JWT_LEEWAY` seconds in the past (90 s with the defaults). This is the verifier-side cap on token age — independent of whatever TTL the issuer chose. Causes:

- The peer's clock is significantly behind this instance's. Run `date -u` on both ends and fix NTP.
- The peer's federation client is using over-generous TTLs and a slow path between issuance and request. Issue tokens just before the request.
- The token sat in a queue / retry buffer for too long. Issue tokens at the moment of dispatch, not at higher layers.

### Inbound federation request returns `401` "JWT has expired" on a freshly-issued token

`verify_jwt` tolerates up to 30 seconds (`DEFAULT_JWT_LEEWAY` in [federation/auth.py](../federation/auth.py)) of clock skew between peers, which absorbs normal NTP-managed drift. If a peer reports "JWT has expired" on tokens that were just issued, real clock drift is involved:

- Run `date -u` on both ends and compare; if they differ by more than the leeway, NTP is broken or absent on at least one host.
- Inside Docker, the container clock follows the host; fix NTP on the host.
- The JWT TTL itself (`FEDERATION_JWT_TTL`, default 60 s) plus the leeway is the total validity window. A peer with extreme network latency to this instance could also see expiry if the token spends ~90 s in transit, but this is not the usual cause.

### Service won't start: `ImproperlyConfigured: FEDERATION_PUBLIC_KEY_NEXT and FEDERATION_PRIVATE_KEY_NEXT must be set together`

A rotation overlap was started but only one half of the NEXT pair is set. The two are required together — see [federation/README.md](../federation/README.md#key-rotation) for the two-phase rotation flow.

- If you ran `rotate_federation_keys --announce`, the command writes both halves. If only one made it to `.env`, the file was edited mid-write — re-run `--announce` to regenerate a consistent pair.
- If you edited `.env` by hand, set both `FEDERATION_PUBLIC_KEY_NEXT` and `FEDERATION_PRIVATE_KEY_NEXT` (or clear both) and restart.

### Service won't start: `ImproperlyConfigured: FEDERATION_PUBLIC_KEY does not match...`

`FederationConfig.ready()` derives the public key from `FEDERATION_PRIVATE_KEY` at startup and refuses to start the process if it doesn't match `FEDERATION_PUBLIC_KEY`. This usually means a key rotation completed only partially:

- The `.env` file was edited but the service wasn't restarted (so the old keys are still in memory), or
- Only one of the two values was updated.

Fix: re-run `scripts/manage.sh rotate_federation_keys --apply` to regenerate a consistent pair, or manually verify both `FEDERATION_PUBLIC_KEY` and `FEDERATION_PRIVATE_KEY` in `.env` come from the same generation. Then `docker compose restart web celery celery-beat`. After rotation, every remote peer must call their own `POST /peers/{id}/refresh-key/` before they will accept tokens you sign with the new key.

If federation is not in use, leave all three of `FEDERATION_INSTANCE_URL`, `FEDERATION_PUBLIC_KEY`, `FEDERATION_PRIVATE_KEY` blank; the check is a no-op when federation is unconfigured.

### `mount_federation_fs` fails with "FUSE not available" or "fusepy is not installed"

Inside Docker: the container needs `--cap-add SYS_ADMIN --device /dev/fuse` and `libfuse2` installed (the platform Dockerfile installs it). Outside Docker: install fusepy (`pip install fusepy`) and the platform's libfuse package (`apt install libfuse2` on Debian/Ubuntu, macFUSE on macOS).

If the mount succeeds but hangs on read, the remote peer is unreachable or slow — there's currently no circuit breaker. See [federation/README.md](../federation/README.md#future-enhancements) for the planned mitigation.

## Push notifications

### `subscribe` succeeds but no notifications arrive

Most often `WEBPUSH_VAPID_PRIVATE_KEY` in `.env` is empty. The `send_push_to_user` task is designed to silently no-op in that case so development works without push setup — but in production it means delivery is broken without an error.

Check:

```bash
scripts/manage.sh shell
>>> from django.conf import settings
>>> bool(settings.WEBPUSH_VAPID_PRIVATE_KEY)
False    # → broken
```

Generate a fresh keypair if needed:

```bash
scripts/manage.sh generate_vapid_keys
# Copy the output into .env, then:
docker compose restart web celery
```

Existing browser subscriptions stay valid; just delivery resumes.

### iOS user never receives notifications

iOS Safari supports Web Push **only when the site is installed as a PWA** (since iOS 16.4). A regular Safari tab cannot receive notifications regardless of permission state. The user has to:

1. Open the site in Safari.
2. Tap the share button → "Add to Home Screen".
3. Open the resulting home-screen icon (not the Safari tab).
4. Grant notification permission from inside the PWA.

If "Add to Home Screen" isn't producing an installable shortcut, the generated `manifest.json` is probably missing a required field. The service worker itself is not optional — `vite-plugin-pwa` is registered for every real bundle and disabled only on the Vite dev server. See the roadmap entry "Document the iOS PWA install flow for push notifications".

## Frontend

### Viewer route shows a blank page

When the Epicurrents viewer mounts non-embedded, it strips `<link rel="stylesheet">` tags from the page whose filename does **not** contain the string `"epicurrents"`. The Vite build is configured to name every CSS bundle `epicurrents-platform-[hash].css` so it survives the strip.

If you've changed the build config and the platform's main CSS bundle no longer matches that pattern, the viewer will strip it on mount and the page goes blank. Check the generated bundle name in `frontend/dist/assets/` and the `assetFileNames` setting in `vite.config.ts`.

A second cause, if CSP has been switched to enforcing (`CSP_REPORT_ONLY=False`): a too-strict `CONTENT_SECURITY_POLICY` blocking a script, style, or the Pyodide CDN shows as blocked-resource errors in the browser console. Set `CSP_REPORT_ONLY=True`, reproduce, and tune the policy from the reported violations before enforcing again — see [docs/operations.md → Security headers](operations.md#security-headers).

### WebAwesome (`wa-*`) components render incorrectly

Two common causes:

- **`setBasePath` not called.** WebAwesome looks up its own assets relative to a base path set in `frontend/src/main.ts`. If you removed or changed that call, components can't find their internal stylesheets.
- **Version mismatch between platform and viewer.** The platform installs `@awesome.me/webawesome` from npm via `frontend/package.json`; the viewer's interface (`frontend/viewer/interface/`) pins its own WA in its own `package.json`. Both must be the same version, or `customElements.define` registers two different implementations and the older one wins. Update both together — see [frontend/README.md](../frontend/README.md).

### EEG trends (aEEG, spectrogram, …) don't appear or compute extremely slowly

The viewer has two signal-storage paths — a `SharedArrayBuffer`-backed mutex (the fast path that workers read from in place) and a JS-heap fallback (every read copies through the main thread). Trends commission a worker to walk the entire recording in epoch-sized chunks; on the SAB path that's cheap, on the heap fallback it's slow enough that long recordings appear to never finish.

Walk through the chain:

1. **Open the EEG settings panel and verify *Use memory manager* is on.** If it's already on but the trend computation is still slow, the SAB path is actually in use and the slowdown is elsewhere — file an issue with the recording length, channel count, and sample rate.
2. **If *Use memory manager* is disabled (greyed out), the browser doesn't have `SharedArrayBuffer` available.** Open the browser DevTools console on the viewer page and type `crossOriginIsolated`. It should print `true`. If it prints `false`, the response headers needed for cross-origin isolation aren't reaching the page.
3. **Enable cross-origin isolation on the instance.** Set `ENABLE_CROSS_ORIGIN_ISOLATION=True` in `.env` and restart the stack. The platform-wide [`CrossOriginIsolationMiddleware`](../epicurrents/middleware.py) sets the COOP/COEP/CORP triple on every response when the setting is true. Once the browser re-fetches the page with the new headers, `crossOriginIsolated` flips to `true`, the *Use memory manager* toggle becomes enabled, and turning it on routes the viewer onto the SAB path.
4. **If there's a reverse proxy (nginx, Caddy, Traefik, …) in front of Django**, it must pass the headers through or set them itself. Verify with `curl -I https://your-host/` against the proxied URL — all three of `Cross-Origin-Opener-Policy`, `Cross-Origin-Embedder-Policy`, `Cross-Origin-Resource-Policy` must be present.

`ENABLE_CROSS_ORIGIN_ISOLATION` is off by default because `COEP: require-corp` rejects any cross-origin subresource that doesn't send `Cross-Origin-Resource-Policy` — an embed scenario that relies on third-party resources without CORP would break. Audit your deployment's external fetches before enabling.

### Analysis tools fail with "Failed to fetch dynamically imported module: .../vendor/pyodide/<version>/pyodide.mjs"

The Python interpreter is served from the deployment's own origin, from a tree that is generated at deploy rather than shipped: it is gitignored, roughly 47 MiB, and excluded from the update rsync so each deployment keeps its own copy. A fresh host, a restored snapshot, or a working checkout that has never run `bootstrap.sh` therefore has nothing at that path, and the import 404s.

Vendor it:

```
docker compose -f docker-compose.yml -f docker-compose.prod.yml \
    --profile vendor run --rm --no-deps vendor python manage.py vendor_pyodide
```

The `vendor` service rather than `web`, and the production overlay rather than the base file: web mounts the tree read-only, so the same command in it fails on a read-only filesystem. In a development checkout `docker compose --profile vendor run --rm --no-deps vendor python manage.py vendor_pyodide` is the same thing without the overlay.

The command is idempotent, so it is safe to run against a tree that is merely incomplete. `--check` reports what is wrong without downloading.

If the error names a **different version** than the one you just vendored, the frontend and the backend disagree. The version appears in three places — `PUBLIC_VIEWER_MODES` in [epicurrents/settings/common.py](../epicurrents/settings/common.py), [frontend/index.html](../frontend/index.html) and [frontend/src/App.vue](../frontend/src/App.vue) — and the command warns when the frontend files name a version other than the one it is vendoring. Bring them into step and rebuild the frontend.

A later failure — the runtime loads, then package installation fails on mne — means the tree was built for a different package set. Re-run without `--check`; the pruned lock and the wheels on disk are written together, so they cannot disagree after a successful run.

### Source localisation is slow, or unavailable offline

The tool prefers a pre-computed lead field from `/vendor/leadfields/` and falls back to the compute API when the bundle is absent, so a deployment missing it works but computes each montage per request against the database, and the service worker has nothing to cache for offline use. Check whether the manifest is being served:

```
curl -s -o /dev/null -w '%{http_code}\n' https://your-host/vendor/leadfields/manifest.json
```

A 404 means the bundle was never generated on this deployment. Generate it — the stack must be up and migrated, since the generator also refreshes the rows the compute API serves from:

```
docker compose -f docker-compose.yml -f docker-compose.prod.yml \
    --profile vendor run --rm --no-deps vendor python manage.py generate_compute_static
```

It takes seconds. Blob filenames carry a content hash, so regenerating a field that has not changed keeps its name and does not invalidate a cached copy.

If the tool reports a montage as unavailable rather than being slow, neither source has it: the montage is outside the generated set (the default is `standard_1020`) and outside what the compute API will compute on demand.

## When your issue isn't here

1. **Capture context.** Run `scripts/logs.sh <service> 500 > issue.log` for the service involved and include it when asking for help.
2. **Check the relevant app's README** — every app under the repo has its own README with a "Gotchas" section that may match your symptom. The "Backend apps" table in [AGENTS.md](../AGENTS.md#backend-apps) is the index.
3. **Search [ROADMAP.md](../ROADMAP.md)** — known issues and limitations are tracked there with workarounds where they exist.
4. **Working with an AI assistant?** Describe the symptom, paste the log excerpt. The assistant can read this file, the app READMEs, and the ROADMAP to triangulate.
5. **Still stuck?** Open an issue on GitHub with: symptom, what you tried, log excerpt, environment (Docker version, host OS, project plugin if any).
