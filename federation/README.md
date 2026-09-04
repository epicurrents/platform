# federation

Inter-instance data sharing between Epicurrents installations. One instance can grant another instance read access to specific recordings (or any other object); the granting instance retains authority over what's shared and how. The mounting instance receives the data either via the standard HTTP download API or as ordinary local files through an optional FUSE virtual filesystem.

Two transport surfaces, one access model:

- **HTTP** — federated peers call the normal `/recordings/api/v1/{hash}` endpoint with `Authorization: FederatedBearer <jwt>` instead of a session cookie. The platform's permission layer treats them as another grant target.
- **FUSE** — local user runs `mount_federation_fs` to expose all remote recordings they have access to as files under a mountpoint. Each `read(path, size, offset)` is translated into an HTTP `Range:` request against the owning peer.

EDF/BDF content can be transformed on the wire by a configurable middleware pipeline — channel dropping, downsampling, header / annotation anonymisation — without ever modifying the stored file. The same middleware classes serve both the HTTP API and the FUSE filesystem.

## Identity and trust

Each instance has an Ed25519 key pair, stored in `FEDERATION_PUBLIC_KEY` / `FEDERATION_PRIVATE_KEY` env vars as URL-safe base64url strings (43 chars each, no padding — same format as VAPID). The public key is published at:

```
GET /.well-known/epicurrents-federation.json
→ {"federation_public_key": "<base64url>"}
```

Outbound HTTP requests carry a JWT signed with this instance's private key:

```
Authorization: FederatedBearer <jwt>
```

JWT claims (EdDSA-signed):

| Claim | Meaning |
|---|---|
| `iss` | Issuing instance URL (from `FEDERATION_INSTANCE_URL`). |
| `aud` | Intended recipient instance URL. |
| `sub` | Remote user identifier (string PK on the issuing instance). |
| `iat` / `exp` | Issued-at / expiry in Unix seconds. TTL configurable via `FEDERATION_JWT_TTL` (default `60`). |
| `jti` | Random UUID4 hex per token. Receiver uses it for replay detection (see below). |

Inbound requests are verified by fetching and caching the peer's public key from its well-known URL. The `exp` and `iat` checks tolerate `DEFAULT_JWT_LEEWAY` seconds (30) of clock skew between peers — without it, a brand-new token from a peer whose clock is one second ahead of this instance's would be rejected as already expired. Set leeway to 0 in tests for strict comparison; the production default is conservative enough to absorb normal NTP-managed skew while staying well below the typical 60 s TTL.

`iat` is the second axis of replay defense: `exp` bounds the validity window the issuer claims, `iat` bounds the window the verifier accepts. `DEFAULT_MAX_JWT_AGE` (60 s) caps how old an inbound token's `iat` can be — a token whose issuer chose a 1-hour TTL is rejected here even though its `exp` claims it should still be valid. Verifier-side bound, not issuer-side trust.

**Replay detection via `jti`.** Each outbound token carries a random `jti`; on receipt, the verifier checks Django's cache for that `jti`, accepts the token if absent (and remembers it for `max_age + leeway` seconds), rejects it as a replay if present. The check is atomic via `cache.add`, which works correctly across gunicorn workers when backed by Redis. Tokens that arrive without a `jti` claim authenticate successfully but produce a `WARNING` log line — backwards-compat for peers that haven't yet upgraded. See [Future enhancements](#future-enhancements) for the migration-to-required plan.

Mutual trust must be established before federation works in either direction:

1. Local superuser registers the remote peer via `POST /api/v1/federation/peers/` — this fetches the remote's public key automatically.
2. Local superuser sets `is_trusted=True` on the peer (the API creates rows with `is_trusted=False`; explicit promotion is required).
3. The remote instance must do the same for this one.

All federation crypto and inbound-auth parsing live in [auth.py](auth.py). `parse_federation_auth(request)` is the single entry point used by both the federation API ([federation/api/v1/ninja.py](api/v1/ninja.py)) and the recordings API ([recordings/api/v1/ninja.py](../recordings/api/v1/ninja.py)); hook here when adding new endpoints that accept federated peers.

**Startup consistency check.** `FederationConfig.ready()` runs `assert_local_keys_consistent()` ([auth.py](auth.py)) at app load — if `FEDERATION_PUBLIC_KEY` does not match the public key derived from `FEDERATION_PRIVATE_KEY`, the process refuses to start with `ImproperlyConfigured`. This catches half-completed key rotations (env edited, service not restarted, or the reverse) at the deploy that introduced them rather than at the next peer interaction. No-op when federation is not configured.

**Peer key change is logged.** `POST /peers/{id}/refresh-key/` writes a `WARNING` log line via `federation.api.v1.ninja` whenever the fetched key differs from the stored one, with both fingerprints truncated to 12 chars. Normal rotation produces one such line per peer refresh; an unexpected change is also how a MITM would manifest, so the audit value is preserved either way.

## Outbound URL safety (SSRF guard)

The only outbound HTTP call from the federation app is `fetch_peer_public_key` — used during peer registration and on every `POST /peers/{id}/refresh-key/`.  Without a guard, a compromised superuser could register a peer URL pointing at internal services (RDS, the cloud metadata endpoint at `169.254.169.254`, localhost on the web container, etc.) and use the well-known fetch to probe them.

`_check_url_is_safe` ([auth.py](auth.py)) resolves the URL's hostname via `socket.getaddrinfo` and rejects any non-globally-routable address before the urllib call.  Blocked categories: RFC 1918 private (10/8, 172.16/12, 192.168/16), loopback (127/8, ::1), link-local (169.254/16, fe80::/10), multicast, and reserved ranges.

**Override for dev.** Local-to-local federation testing (e.g. two Django dev instances on the same host) needs `http://localhost:...` to work.  Set `FEDERATION_ALLOW_PRIVATE_PEER_URLS=True` in `.env` to disable the guard.  **Never enable in production** — that re-opens the SSRF vector against a compromised superuser account.

**Known limitation.** The guard does not defend against DNS rebinding (TOCTOU between the resolution here and the connection made by `urllib.request.urlopen`).  Closing that gap requires pinning the resolved IP for the urllib call, which is non-trivial — see [Future enhancements](#future-enhancements).

## Peer deletion

`DELETE /api/v1/federation/peers/{id}/` removes a peer and **cascades to every `AccessRight` row that targeted it** — there is no peer-less federation grant.  The cascade is the only sensible policy: orphaning the grants would violate the `CheckConstraint` on `AccessRight` (exactly one of `access_target` / `access_target_group` / `public_share_token` / `federated_peer` must be set), and forcing operators to revoke grants explicitly before deletion would make peer cleanup gratuitously fiddly for a common operation.

The audit trail of cascaded grants is preserved by the activity app: the `pre_delete` signal fires for cascaded objects too, so each removed `AccessRight` produces an `ObjectChangeLog` row with the deleting user, the timestamp, and the full `before_state`.  Reconstructing "who had access at time T" after a peer is gone is therefore an `ObjectChangeLog` query, not a `FederationAuditLog` one.

**Caveat:** the activity signal only fires within an API request context (`is_api_request_context()` check).  Deleting a peer via the Django admin or `shell` bypasses this — the cascade still happens but no `ObjectChangeLog` row is written.  Always use the API endpoint when audit retention matters.

## Rate limiting and quotas

The federated download paths (`download_recording`, `slice_recording`) enforce two per-peer limits to bound how much a single compromised peer can exfiltrate before anyone notices.  Local users and non-federation API consumers are unaffected — the limits only fire when the request is authenticated via a `FederatedBearer` JWT.

| Limit | Setting | Default | What it bounds |
|---|---|---|---|
| Daily byte budget | `FEDERATION_PEER_DAILY_BYTE_LIMIT` | 1 TiB | Total bytes served to one peer in a UTC day. Even at maximum throughput the peer cannot exceed this. |
| Per-minute download request rate | `FEDERATION_PEER_DOWNLOAD_RATE_LIMIT` | 60 / min | Burst protection on download paths — catches "1000 requests/minute" abuse patterns where each request is individually small. |
| Per-minute inbound check rate | `FEDERATION_PEER_INBOUND_RATE_LIMIT` | 600 / min | Throttles `inbound_check_object` to slow object-id enumeration. Distinct counter from the download rate so routine checks don't share state with the exfil bound. |

Set either setting to `0` to disable that limit.  Both default to values that an honest peer will not bump into during normal use; they exist to cap a compromised peer, not throttle the happy path.

**Charging model.** Both limits charge the *full file size* against the daily budget when a download is requested, even if the request is a partial Range or a time slice.  This is intentional: bounding by actual bytes served would let a peer fetch the whole file as 1000 small slices and bypass the daily budget entirely.  The bias is conservative in the direction of safety.

**Response code.** Limit exceeded → `429 Too Many Requests`.  The peer's federation client is expected to back off; the per-minute counter resets on minute boundaries (UTC) and the byte budget resets at the next UTC day boundary.

**Cache backend.** The limit counters use Django's cache, same as the replay-protection nonce cache (see "Identity and trust").  Production must use a process-shared backend (Redis); the included `production.py` does.  `LocMemCache` is fine for single-worker dev / test.

**Audit trail.** A request rejected by either limit is logged to `FederationAuditLog` with `status_code=429` and a `WARNING` line in `federation.limits`.  Operators investigating "why is peer X getting 429s" should look at that audit table first.

## Key rotation

Rotating this instance's signing key is a two-phase operation by default, using an *overlap window* that lets peers refresh their cache before this instance starts signing with the new key. The one-step rotation that was previously the only option is preserved as an emergency lever (`--apply`) but should not be the routine choice — it forces every peer offline until they refresh.

**Phase 1 — Announce.** Generate a new pair and write it to the `FEDERATION_*_KEY_NEXT` env vars. The current signing key is untouched. The well-known endpoint now publishes both keys; peers calling `POST /peers/{id}/refresh-key/` populate both `public_key` and `public_key_next` on their `FederatedPeer` row, so they can verify tokens signed with either.

```bash
scripts/manage.sh rotate_federation_keys --announce
# Restart web to start publishing the NEXT key at /.well-known/.
docker compose restart web
# Ask each peer admin to refresh: POST /api/v1/federation/peers/{id}/refresh-key/
```

**Phase 2 — Promote.** Move the NEXT pair into the current slots and clear NEXT. Outbound signing switches to the new key on the next restart; peers that refreshed during phase 1 verify successfully via their cached `public_key_next`. Peers that haven't refreshed will fail (and need to refresh, or the rotation isn't complete).

```bash
scripts/manage.sh rotate_federation_keys --promote
docker compose restart web celery celery-beat
```

**Emergency one-step rotation.** Use this only when the current key is suspected to be compromised — the overlap flow is otherwise strictly better.

```bash
scripts/manage.sh rotate_federation_keys --apply  # immediate replace; expect a brief outage
```

Implementation details:

- The well-known endpoint emits `federation_public_key_next` only when set, so old peers (not aware of the overlap field) silently ignore it and continue to work via `federation_public_key`.
- `parse_federation_auth` tries `peer.public_key` first; if signature verification fails *and* `peer.public_key_next` is set, retries with that. Non-signature failures (expired, wrong audience, malformed) do not trigger the retry — those are not rotation symptoms and bypassing them would weaken the time / audience checks.
- The startup consistency check ([auth.py](auth.py)) also validates the NEXT pair when set: both halves must be present together, and the public half must derive from the private half. Half-completed NEXT configurations refuse to start with `ImproperlyConfigured`, matching the existing current-pair check.

## Models

### `FederatedPeer`

One row per remote instance this platform exchanges data with.

| Field | Notes |
|---|---|
| `url` | Canonical HTTPS base URL of the peer. Acts as the unique identity — same value used in `iss` / `aud` JWT claims. `unique=True`. |
| `display_name` | Optional human label for admin UIs. |
| `public_key` | Ed25519 public key as URL-safe base64url, fetched from the peer's well-known URL. |
| `public_key_fetched_at` | Last successful fetch time. |
| `is_trusted` | **Must be set to `True` by a superuser** before any inbound request from this peer is accepted. Auto-discovered peers start as `False`. |
| `added_by` | Superuser who registered the peer. `SET_NULL` on user delete. |

### `FederationAuditLog`

One row per inbound federation request that reached the access-decision stage — both successful accesses and access denials.  Answers the compliance question "which peer, acting on whose behalf, accessed which object on this instance when, with what outcome".

| Field | Notes |
|---|---|
| `peer` | FK to `FederatedPeer`, `SET_NULL` on peer deletion. |
| `peer_url` | Denormalised peer URL — survives peer deletion so the audit row remains attributable. |
| `remote_user_id` | JWT `sub` claim (the actual end user on the remote peer). Always populated for rows that reached the access decision. Distinct from `AccessRight.remote_user_id`, which may be `""` to denote a wildcard grant. |
| `action` | Endpoint name (e.g. `inbound_check_object`, `download_recording`). |
| `target_content_type` / `target_object_id` | Generic FK to the resource accessed. `target` resolves the model instance via Django's `GenericForeignKey`. May be unset for `list_recordings` (summary row) or for probes of non-existent objects (where the path params are preserved instead — see "indistinguishability" below). |
| `status_code` | HTTP status returned to the peer. |
| `created_at` | `auto_now_add`, indexed for time-based queries. |

**Operational policy** (enforced by convention, not by DB constraints):

- **Append-only.** Application code never updates or deletes rows. The retention pruning job is the only intentional deletion path.
- **Retention.** The deployment's regulatory minimum — for HIPAA-style deployments, at least 6 years. Lower thresholds are configuration choices, not code choices.
- **Export.** Until a dedicated management command lands (see [Future enhancements](#future-enhancements)), query via the Django shell or admin.

**Scope choices worth knowing:**

- **Auth failures are *not* audited here.** Bad-token / untrusted-peer attempts surface in Django's `WARNING` log via `federation.api.v1.ninja` and `recordings.api.v1.ninja`. The compliance question is "what did authenticated peers access", and the audit log is scoped to that.
- **`list_recordings` writes one summary row per call, not one per recording listed.** This keeps the audit table compact on routine listings; reconstructing the exact recording set listed at a given moment is possible by querying the `AccessRight` table at that timestamp.
- **Indistinguishability invariant for `inbound_check_object` is preserved at the response layer, not the audit layer.** The peer-facing 404 is identical regardless of whether the object was missing or the grant was absent — but the audit row records the actual outcome with the resolved target (when one existed) or the path-param identifier (when the probe was for a non-existent object). Forensics can therefore distinguish "peer probing IDs" from "peer keeps hitting revoked grants" even though the peer cannot.

The audit-log helper `log_federation_access` ([federation/audit.py](audit.py)) is the single write path; call sites in [federation/api/v1/ninja.py](api/v1/ninja.py) and [recordings/api/v1/ninja.py](../recordings/api/v1/ninja.py) invoke it after the access decision.

### Federation grants

Federation grants are not a separate model — they're rows in the core `AccessRight` table ([epicurrents/models.py](../epicurrents/models.py)) with `federated_peer` set instead of `access_target` / `access_target_group` / `public_share_token`. The four-target `CheckConstraint` on `AccessRight` enforces that exactly one target is set per row.

| Field | Federation-grant meaning |
|---|---|
| `federated_peer` | The peer this grant applies to. |
| `remote_user_id` | The remote user (their PK on the peer) this grant is for. **Empty string is a wildcard** — grants apply to any authenticated user from that peer. |
| `can_read` / `can_write` / `can_share` | Same semantics as regular grants. Federation is read-oriented today; write/share is technically possible but no UI surfaces it. |
| `apply_middleware` | When `True`, served EDF/BDF content passes through the configured middleware pipeline before transmission. |
| `expires_at` | Optional, same as regular grants. |

## API

Mounted at `/api/v1/federation/`. Full request/response detail in [api/v1/ninja.py](api/v1/ninja.py).

### Peer management (superuser only)

| Method | Path | Notes |
|---|---|---|
| `GET` | `/peers/` | List all known peers. |
| `POST` | `/peers/` | Register a peer. Fetches the public key from the peer's well-known URL automatically. Creates the row with `is_trusted=False`. |
| `GET` | `/peers/{id}/` | Detail. |
| `PATCH` | `/peers/{id}/` | Update `display_name` / `is_trusted`. **Trusting a peer is a superuser action that must be done explicitly.** |
| `DELETE` | `/peers/{id}/` | Remove. Cascades to all grants targeting this peer. |
| `POST` | `/peers/{id}/refresh-key/` | Re-fetch the peer's public key. Use after the peer rotates keys. |

### Grants (any authenticated user)

Symmetric to local grants — the caller must hold `can_share=True` on the target object, or be its author, or be a superuser.

| Method | Path | Notes |
|---|---|---|
| `GET` | `/grants/` | List federation grants the caller has issued. |
| `POST` | `/grants/` | Create a grant. Payload: `federated_peer_id`, `content_type_id`, `object_id`, `remote_user_id` (empty for wildcard), permission flags, optional `expires_at`. Returns 409 when a grant for the same `(peer, remote_user_id)` already exists on the object — revoke it first. |
| `PATCH` | `/grants/{id}/` | Update the grant's expiry (renew). Body: `expires_at` — the value replaces the current expiry; `null` makes the grant non-expiring. Only the original `access_giver` or a superuser. |
| `DELETE` | `/grants/{id}/` | Revoke. Only the original `access_giver` or a superuser may revoke. |

The peer and grant endpoints share their implementation with the [management commands](#management-commands) through [`services.py`](services.py) — one copy of the SSRF guard, the trust gate, the object-level share check, and the audited writes. The endpoints keep the request-layer concerns (session-CSRF, superuser gating); the services carry the domain logic.

### Inbound (called by remote instances)

| Method | Path | Notes |
|---|---|---|
| `GET` | `/inbound/objects/{ct_id}/{object_id}/` | Check whether the calling peer's user may read the specified object. Authenticated via `FederatedBearer` JWT. Used by the remote instance when one of its local users requests an object that lives here. |

This endpoint is model-agnostic — it resolves the target via `ContentType.model_class().objects.filter(pk=…)`, so any model that participates in the `AccessRight` GenericFK surface is reachable. `Recording` and [`media.MediaFile`](../media/README.md) both qualify; new federatable models slot in by declaring the same reverse `GenericRelation` for `AccessRight`.

In addition to the dedicated check endpoint, the recordings API (`/recordings/api/v1/{hash}` and `/{hash}/detail`) and the media API ([`/media/api/v1/{hash}` and `/{hash}/file`](../media/README.md#read-auth--three-modes)) accept `FederatedBearer` JWT directly on their read paths. The two surfaces serve different purposes — the inbound check is the "may I" probe a peer's frontend uses before requesting bytes; the per-resource endpoints serve those bytes once the probe has cleared.

The well-known endpoint is mounted separately at `/.well-known/epicurrents-federation.json` (not under `/api/v1/`) so it follows the RFC 8615 convention. Returns `404` if `FEDERATION_PUBLIC_KEY` is unset.

## Middleware pipeline

EDF/BDF content can be transformed on the wire by a `MiddlewarePipeline` (a list of middleware instances) before being served. The same pipeline machinery serves both the HTTP API and the FUSE filesystem, scoped by the `targets` attribute on each middleware.

### Three middleware ABCs

| Class | Constraint | Use for |
|---|---|---|
| `EDFHeaderMiddleware` | Output header length **must equal** input header length (isometric). | Field rewrites in the fixed EDF header (patient ID, recording ID, etc.). |
| `EDFSignalMiddleware` | Transforms header and each data record independently. `output_record_size` must equal the byte length of `transform_record`'s output. `size_invariant=True` opts out of size bookkeeping when bytes per record don't change. | Per-record transforms — channel dropping, downsampling, annotation TAL stripping. Range-aware: only the overlapping records are read for any HTTP range request. |
| `EDFFullFileMiddleware` | May return header and signal bytes of arbitrary lengths. Must implement `compute_output_size` for FUSE `stat()`. | Anything that needs the whole file as one unit. Buffers the entire transformed file in memory once accessed — use sparingly. |

Execution order in a pipeline: all `EDFHeaderMiddleware` first, then `EDFSignalMiddleware`, then `EDFFullFileMiddleware` (each group in list order).

### Scope targeting

Each middleware sets `targets` to control where it applies:

```python
targets = frozenset({"fuse"})  # FUSE only
targets = frozenset({"api"})  # HTTP download only
targets = frozenset({"fuse", "api"})  # both (default)
```

The pipeline filters by scope when servicing a request — same middleware list, two viewpoints.

### Built-in middleware

| Class | Type | Effect |
|---|---|---|
| `AnonymizeEDFHeader` | Header | Removes patient and recording identifiers from the fixed header, and applies the ingest channel-block de-identification (canonical / `MISC_<n>` labels, blanked transducers, reconstructed prefiltering) as defense in depth — a no-op for files this platform ingested, since the stored bytes already carry both transforms. |
| `DropChannelsMiddleware` | Signal | Removes the named channels from every data record, rewrites the header. |
| `DropAnnotationChannelsMiddleware` | Signal | Variant of `DropChannelsMiddleware` targeting EDF+/BDF+ annotation channels only. |
| `DownsampleMiddleware` | Signal | Integer-factor decimation of the named channels. Updates `samples_per_record` in the header. |
| `StripAnnotationTextMiddleware` | Signal (`size_invariant=True`) | Replaces annotation TAL text with the mandatory timekeeping records only. Each record stays the same byte length so the pipeline is size-preserving. |

### Pipeline size properties

| Property | Meaning |
|---|---|
| `is_empty` | No middleware in the list. |
| `is_isometric` | Only `EDFHeaderMiddleware` entries — no record transforms, total file size unchanged. FUSE can stream signal bytes directly without buffering. |
| `is_size_preserving` | Total output size equals input size. Includes isometric pipelines *and* signal pipelines whose every `EDFSignalMiddleware` has `size_invariant=True`. Use for `download_size` calculation. |
| `has_signal_middleware` | At least one `EDFSignalMiddleware` is present. Affects serving strategy selection. |

### `SignalPipelineContext`

Precomputed layout returned by `MiddlewarePipeline.build_signal_context(raw_header, n_records)`. Holds the transformed header, input/output record sizes, total record count, and output file size — everything needed to serve arbitrary byte ranges without buffering the file. Cached per-recording in FUSE (`FederationOperations._signal_contexts`) so range requests don't recompute the layout.

`build_signal_context_from_infos(signal_infos, bps, n_records, header_size, raw_header=None)` is the alternate constructor: builds the context from structured signal metadata (DB rows or catalogue dicts) without parsing raw EDF bytes. Each signal info element must expose `label`, `sample_count`, `is_annotation_channel` (duck-typed; both `recordings.models.SignalInfo` rows and the `_SignalInfoLike` NamedTuple satisfy the interface). `raw_header=None` produces a context suitable for size computation only — the header bytes can be fetched lazily when actually needed.

Each `EDFSignalMiddleware` must implement `transform_signal_infos(signal_infos) -> list` (default: identity) so the pipeline can propagate channel-layout changes through structured metadata without bytes.

## Serving pipelines

### Server-side (HTTP API)

The recordings download endpoint always applies `_build_serve_pipeline()` from [recordings/api/v1/ninja.py](../recordings/api/v1/ninja.py), which is hardcoded to:

```python
MiddlewarePipeline(
    [
        AnonymizeEDFHeader(),
        StripAnnotationTextMiddleware(),
    ]
).for_scope("api")
```

This pipeline runs for any caller other than the recording author or a superuser, provided the matching `AccessRight` has `apply_middleware=True`. See [recordings/README.md](../recordings/README.md#serving-and-the-middleware-pipeline).

### Mounting-side (FUSE) — Layer 2

The FUSE mount can additionally apply its own pipeline for analysis convenience — drop irrelevant channels, downsample, etc. This is **purely local post-processing**; it carries no privacy guarantee because the mounting instance controls it. Privacy is enforced upstream by the serving instance's `apply_middleware` setting.

Mount-time pipeline configuration is currently programmatic (pass a `MiddlewarePipeline` to `FederationOperations`); the management command exposes only the unmodified default of an empty pipeline. Extending the command to accept a pipeline spec is on the roadmap.

## Federated FUSE filesystem

`federation/fuse_fs.py` implements a `fusepy` `Operations` subclass (`FederationOperations`) that exposes federated recordings as a read-only directory tree.

Layout:

```
<mountpoint>/
    <peer-slug>/          one directory per trusted peer
        <filename>        one file per accessible recording
```

`peer-slug` is the peer URL with non-alphanumeric characters replaced by underscores (`https://neuro.example.com` → `neuro.example.com`).

Lifecycle:

1. `load_catalogue()` runs once at mount time. For each trusted peer it issues `GET /recordings/api/v1/?status=ready` (authenticated with a fresh JWT), receives the recording list including the `signals` array, and builds an in-memory catalogue. For EDF recordings under a signal pipeline it also constructs the `SignalPipelineContext` from the catalogue data — zero extra network requests.
2. `getattr(path)` / `readdir(path)` are served from the in-memory catalogue. `st_size` reflects the post-pipeline byte count.
3. `read(path, size, offset)` maps the output byte range to overlapping input records, issues a single HTTP range request to fetch those records, and (if a local pipeline is active) transforms them before returning.

Non-EDF files are proxied verbatim. EDF/BDF content goes through the layered pipeline described above.

### Running it

```bash
docker compose run --rm \
    --cap-add SYS_ADMIN \
    --device /dev/fuse \
    web python manage.py mount_federation_fs /mnt/epicurrents-fed --user-id 1 --foreground
```

Requirements: `fusepy` (in [requirements.txt](../requirements.txt)) plus `libfuse2` (in the [Dockerfile](../Dockerfile)). The `SYS_ADMIN` capability and `/dev/fuse` device are mandatory for FUSE inside a container.

Flags: `--foreground` keeps the process in the foreground (default: daemonize); `--debug` enables FUSE kernel-level logging; `--no-threads` runs single-threaded.

## Management commands

| Command | Purpose |
|---|---|
| `mount_federation_fs <mountpoint> --user-id <id>` | Mount the federated FUSE filesystem. See above. |
| `rotate_federation_keys [--announce \| --apply \| --promote] [--env PATH]` | Generate a new Ed25519 keypair and update `.env`. Prefer `--announce` followed by `--promote` for the two-phase [overlap rotation](#key-rotation); use `--apply` only for emergency one-step replacement. Bare invocation prints to stdout for review without touching anything. |

The peer and grant operators below are CLI equivalents of the [API](#api), sharing [`services.py`](services.py). They are the operator surface for tailnet setup (register / trust / grant while SSH'd into the instance); each write opens an audited `COMMAND`-interface scope. `--peer` accepts a numeric id or the peer URL.

| Command | Purpose |
|---|---|
| `federation_add_peer --url <url> [--display-name] [--user]` | Register a peer, fetch its key (untrusted), and print the SHA-256 key fingerprint for out-of-band verification. |
| `federation_trust_peer --peer <ref> [--fingerprint <fp>] [--untrust]` | Set the trust flag. With `--fingerprint`, the flip fails unless the stored key matches — turning the documented TOFU check into an enforced one. |
| `federation_refresh_peer_key --peer <ref>` | Re-fetch the peer's key after a rotation; warns on an unexpected change. |
| `federation_list_peers` | List peers with trust state and key fingerprints. |
| `federation_grant --peer <ref> --giver <user> (--recording <hash> \| --content-type <app.model> --object-id <id>) [--remote-user] [--no-read] [--write] [--share] [--apply-middleware] [--expires <iso>]` | Grant a peer (optionally a specific remote user) access to an object. `--giver` must hold share rights on it. |
| `federation_renew_grant --grant-id <id> (--expires <iso> \| --no-expiry) [--actor]` | Set or clear a grant's expiry — the CLI side of `PATCH /grants/{id}/`. |
| `federation_revoke_grant --grant-id <id> [--actor]` | Revoke a grant. |
| `federation_list_grants [--giver <user>]` | List federation grants (peer, remote user, target, permissions, expiry). |
| `federation_check_peer (--peer <ref> \| --url <url>) [--no-probe]` | Diagnose the peer handshake: reachability, TLS, and key (Level 1), then a signed round-trip to the peer's inbound endpoint that distinguishes "peer trusts us" (404) from "peer rejects us" (401) (Level 2). The single best command for debugging an opaque federation 401. |

`init_env` ([../epicurrents/management/commands/init_env.py](../epicurrents/management/commands/init_env.py)) generates the initial keypair on first deployment, so manual rotation is only needed for key compromise or scheduled rotation.

## Settings consumed

| Variable | Default | Purpose |
|---|---|---|
| `FEDERATION_PUBLIC_KEY` | `""` | Ed25519 public key, URL-safe base64url. Published at the well-known URL. |
| `FEDERATION_PRIVATE_KEY` | `""` | Ed25519 private key, URL-safe base64url. Never expose. |
| `FEDERATION_PUBLIC_KEY_NEXT` | `""` | Announced next public key during a [rotation overlap](#key-rotation). Empty when no rotation is in progress. |
| `FEDERATION_PRIVATE_KEY_NEXT` | `""` | Private key corresponding to `FEDERATION_PUBLIC_KEY_NEXT`. Must be set together with the public half. |
| `FEDERATION_INSTANCE_URL` | `""` | Canonical HTTPS base URL of this instance. Used as the JWT `iss` claim and must match the value remote instances have registered for this peer. No trailing slash. |
| `FEDERATION_JWT_TTL` | `60` | Outbound JWT lifetime in seconds. Keep short — JWTs are issued per request. |
| `FEDERATION_KEY_FETCH_TIMEOUT` | `10` | Seconds to wait when fetching a peer's public key from its well-known URL. |
| `FEDERATION_PEER_DAILY_BYTE_LIMIT` | `1099511627776` (1 TiB) | Maximum bytes served per peer per UTC day. Set to `0` to disable. See [Rate limiting and quotas](#rate-limiting-and-quotas). |
| `FEDERATION_PEER_DOWNLOAD_RATE_LIMIT` | `60` | Maximum federated download requests per peer per minute. Set to `0` to disable. |
| `FEDERATION_PEER_INBOUND_RATE_LIMIT` | `600` | Maximum `inbound_check_object` requests per peer per minute. Set to `0` to disable. |
| `FEDERATION_ALLOW_PRIVATE_PEER_URLS` | `False` | Dev-only escape hatch — when `True`, the SSRF guard does not reject peer URLs resolving to private IPs. **Never enable in production.** |

Leave all three of `FEDERATION_PUBLIC_KEY`, `FEDERATION_PRIVATE_KEY`, `FEDERATION_INSTANCE_URL` blank to disable federation entirely. The well-known endpoint returns `404` in that case.

## Project plugin extension points

| Hook | How |
|---|---|
| Custom EDF middleware | Subclass `EDFHeaderMiddleware` / `EDFSignalMiddleware` / `EDFFullFileMiddleware` from this module. Implement the required methods and set `targets` to the scopes it should run in. |
| Add middleware to the server-side serve pipeline | Currently the serve pipeline is hardcoded in `_build_serve_pipeline()` in [recordings/api/v1/ninja.py](../recordings/api/v1/ninja.py). Extending it to read from a settings hook is on the roadmap; today, override the function in a fork. |
| Add middleware to the FUSE mount pipeline | Pass a `MiddlewarePipeline` instance to `FederationOperations(pipeline=...)`. The management command doesn't expose this yet — a project plugin can subclass the command and wire it through. |

## Tests

```bash
pytest federation/tests/
# Excludes the FUSE test by default — it needs libfuse2 in the env.
pytest federation/tests/ --ignore=federation/tests/test_fuse_fs.py
```

The default platform CI run excludes `federation/tests/test_fuse_fs.py` because libfuse2 isn't available in every CI environment. Add it back in environments where it is.

## A design rule that should not be relaxed

**Do not add federated group targets** — i.e. don't add an `access_target_group`-style field that points to a group on a remote instance. Federation grants stay scoped to a specific peer + (optional) specific remote user. Granting "to a group on the remote side" would delegate the access decision to a group administrator on that remote installation, who is outside your trust boundary; by the design of this system, only the local data owner decides who gets access to local data. If you need to share with many users on a remote instance, use the wildcard `remote_user_id=""` form, which is a deliberate, visible "any authenticated user from that peer".

## Gotchas

- **`is_trusted=False` until promoted.** The `POST /peers/` endpoint creates rows with `is_trusted=False`. Inbound requests from a peer are rejected until a superuser flips the flag. This is the trust gate — registering a peer is not the same as trusting it.
- **`apply_middleware` is the privacy switch, not the pipeline definition.** A federation grant with `apply_middleware=True` makes the server run its configured pipeline. The grant doesn't pick which middleware runs — that's the server's `_build_serve_pipeline()` config. Different recipients of the same recording get the same anonymisation, by design.
- **FUSE serving strategy depends on pipeline shape.** Header-only pipelines stream signal bytes raw and substitute the new header per-read. Signal pipelines map output ranges back to input records and fetch only those. Full-file pipelines buffer the entire transformed file on first access. Choose the right middleware type for the transform you need; misclassifying as `EDFFullFileMiddleware` when an `EDFSignalMiddleware` would work explodes memory usage on large recordings.
- **Layer 2 (FUSE-side pipeline) carries no privacy guarantee.** It runs on the mounting instance, which is the recipient of already-served bytes. Any privacy decision belongs in Layer 1 (server-side `apply_middleware`). Use Layer 2 for analysis convenience, not security.
- **Key rotation is a two-step operation.** `rotate_federation_keys --apply` rewrites this instance's `.env`. Until each remote instance refreshes its cached copy of this peer's public key (via the remote's `POST /peers/{id}/refresh-key/`), tokens this instance issues will be rejected. Plan rotations during low-traffic windows or warn peers in advance.
- **TOFU at peer registration — verify the fingerprint out-of-band before trusting.** `POST /peers/` auto-fetches the public key from the peer's well-known URL and stores it with `is_trusted=False`. Before flipping `is_trusted=True`, ask the peer admin out-of-band (phone, IRL, signed email) for the fingerprint of their public key and compare. The auto-fetch is convenient but it's first-use trust — anyone in the network path could have answered with a key of their choice. The trust gate is the explicit `is_trusted=True` flip, not the registration.
- **No in-flight JWT revocation.** Standard JWT trade-off: once a token is issued, it's valid until `exp`. There is no way to revoke a single in-flight token. The 60-second default `FEDERATION_JWT_TTL` is what bounds the damage window if a private key is compromised. If you suspect compromise, rotate (`rotate_federation_keys --apply`) and accept the few seconds of grace where in-flight tokens still verify.
- **JWT `aud` is matched literally; `iss` is normalised.** The `aud` claim must match the receiving instance's `FEDERATION_INSTANCE_URL` exactly (including scheme, host, no trailing slash). The `iss` claim is normalised with `.strip().rstrip("/")` before peer lookup, so trailing-slash variants on the sending peer's `FEDERATION_INSTANCE_URL` resolve to the right DB row. Mis-configured `FEDERATION_INSTANCE_URL` on the receiving side produces opaque 401s.
- **`fetch_peer_public_key` rejects URLs that resolve to private IPs.** A peer registered with a `localhost` or `10.x.y.z` URL fails with "non-public address ... refusing to fetch". Set `FEDERATION_ALLOW_PRIVATE_PEER_URLS=True` in dev environments that need it; never in production. See [Outbound URL safety](#outbound-url-safety-ssrf-guard).
- **Federated downloads charge the full file size against `FEDERATION_PEER_DAILY_BYTE_LIMIT`, even for Range / slice requests.** A peer issuing many small slices does not bypass the budget. Disable per-setting with `0` if your deployment has different exfil-defense controls. See [Rate limiting and quotas](#rate-limiting-and-quotas).
- **`peer.delete()` CASCADEs to every federation grant for that peer.** Audit trail is captured by the activity app when the deletion goes through the API (`pre_delete` signal logs each cascaded `AccessRight`). Shell / admin deletions skip the audit hook. See [Peer deletion](#peer-deletion).
- **Replay protection requires a cross-process cache in production.** The `jti` nonce check uses Django's default cache. `LocMemCache` (dev/test) is per-process, so a multi-worker production deployment would only protect replays within one worker. Production settings (`epicurrents/settings/production.py`) configure `RedisCache` on Redis DB 2 by default; if you customise the cache backend, ensure it is process-shared (Redis, Memcached) or replay protection silently degrades.
- **`/inbound/objects/{ct_id}/{object_id}/` returns 404 for both "missing" and "unauthorized".** A remote peer must not be able to probe object IDs to learn which ones exist on this instance. The endpoint deliberately collapses missing-object, missing-content-type, and no-grant outcomes into the same `404 "Object not found or access denied"` response. Auth failures (bad token, untrusted peer) keep their distinct 401 — those signal peer-side credential problems, not object-existence questions. If a peer reports 404 on something they believe they have a grant for, check the grant via `/api/v1/federation/grants/` on this instance rather than assuming the object is gone.
- **FUSE catalogue is loaded once at mount time.** `mount_federation_fs` calls `load_catalogue()` at startup and serves all subsequent `getattr` / `readdir` calls from that in-memory snapshot. Recordings added, removed, or re-trusted on the peer after mount are invisible until remount. Long-running mounts should be remounted periodically; a `refresh-catalogue` IOCTL or scheduled reload would help but is not implemented.

## Future enhancements

These items were intentionally scoped out of the federation hardening initiative and are tracked here so they don't get lost — each is a hardening improvement, not a blocker for production deployment.

- **CSV export of `FederationAuditLog`.** A management command for SAR / breach response — `export_federation_audit --since <ts> --until <ts> --peer <id>` writing a flat CSV. Until it lands, query via the Django shell. Referenced from [`FederationAuditLog`](#federationauditlog) and from [docs/operations.md](../docs/operations.md#query-the-federation-audit-log).
- **Tighten `jti` from optional-with-warning to required.** Today the verifier accepts tokens without a `jti` claim and logs a `WARNING`, for backwards-compat with peers that haven't upgraded. Once the federation network has migrated, flip `parse_federation_auth` to reject `jti`-less tokens with 401. Referenced from [Identity and trust](#identity-and-trust).
- **Automate the announce → promote handoff in `rotate_federation_keys`.** Today the operator runs `--announce`, waits an arbitrary period, then runs `--promote`. Detecting "all peers have refreshed" automatically would let the command self-pace — e.g. by polling each peer's `/.well-known/` for the new key, or by surfacing a per-peer "has refreshed since announce" flag via a status endpoint. Referenced from [Key rotation](#key-rotation).
- **True concurrent-connection limit on download paths.** The current per-peer download limits (daily byte budget + per-minute request rate) bound total exfil regardless of concurrency. A separate concurrent-connections cap would slow sustained bulk pulls more aggressively, but requires reliable stream-cleanup handling in WSGI (decrement the counter on response completion / abort). The byte budget alone is sufficient for the stated threat model; this is defense-in-depth. Referenced from [Rate limiting and quotas](#rate-limiting-and-quotas).
- **DNS rebinding defense for `fetch_peer_public_key`.** The SSRF guard resolves the URL's hostname and rejects private IPs, but `urllib.request.urlopen` does its own DNS resolution at connect time — a hostile peer could return a public IP on the first lookup and a private IP on the second. Closing the gap requires pinning the resolved IP for the urllib call, e.g. by passing a custom `HTTPAdapter` / `URLOpener` that bypasses DNS. Referenced from [Outbound URL safety](#outbound-url-safety-ssrf-guard).
- **Circuit breaker on outbound FUSE reads.** A flaky or unreachable peer can hang FUSE reads for the `urllib` timeout. Wrap the remote calls with a circuit breaker and surface failed peers as inaccessible files (e.g. zero-byte `getattr` with `EIO`) rather than hanging the FUSE process. Touched on in [docs/troubleshooting.md](../docs/troubleshooting.md#mount_federation_fs-fails-with-fuse-not-available-or-fusepy-is-not-installed).
