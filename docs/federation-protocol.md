# Federation protocol

Specification of the protocol by which one Epicurrents instance requests data from another: how instances identify themselves, how trust between them is established, how a request is authenticated and bound, how the owning instance decides what to serve and on what terms, and what each side records. It is written so that the protocol can be assessed, and reimplemented, without reading the code.

**Status.** Describes the protocol as implemented in platform 0.1, with request binding and a mandatory `jti`. Where the implementation departs from what the protocol ought to guarantee, the departure is stated in [Open issues](#open-issues) rather than smoothed over in the body. Architecture, models, settings and operator commands are documented in [federation/README.md](../federation/README.md), which is authoritative where the two overlap; this note specifies the wire contract and its security properties.

## Conventions

The key words MUST, MUST NOT, SHOULD and MAY are used as in RFC 2119 and describe what an implementation needs to do to interoperate and to preserve the security properties below. Sentences without them describe the reference implementation.

- **base64url** is the URL-safe alphabet of RFC 4648 §5 with padding removed on output and tolerated on input.
- **Times** are integer Unix seconds (UTC).
- **Instance URL** comparisons are exact string comparisons unless a normalisation step is named.
- Code references point at the reference implementation: token handling in [federation/auth.py](../federation/auth.py), trust operations in [federation/services.py](../federation/services.py), authorisation in [epicurrents/permissions.py](../epicurrents/permissions.py).

## Roles and terms

| Term | Meaning |
|---|---|
| Instance | One deployment of the platform, identified by its instance URL and an Ed25519 key pair. |
| Owning instance | The instance holding the requested object. It verifies the request, makes the authorisation decision and serves the response. |
| Requesting instance | The instance issuing the request on behalf of one of its local users. |
| Peer | Another instance as recorded in an instance's peer table (`FederatedPeer`). Whether a peer is trusted is decided independently on each side. |
| Local user | An account on the requesting instance. Its identifier travels as `sub`. |
| Remote user | The same account from the owning instance's point of view: an identifier asserted by a peer, never authenticated by the owning instance. |
| Grant | An `AccessRight` row on the owning instance targeting a peer and, optionally, a single remote user. |
| Operator | A person with superuser or shell access to an instance. |

## Security goals

What the protocol is designed to guarantee, assuming the [trust assumptions](#trust-assumptions) hold:

1. **Peer authentication.** A request is accepted only if it is signed by the current or announced-next key of a peer the owning instance has explicitly marked trusted.
2. **Request binding.** A token authorises exactly one HTTP request: one method, one path, one `Range` value. It cannot be redirected to a different operation or object.
3. **Freshness and single use.** A token is accepted only within a short window and at most once.
4. **Owner authority.** Every authorisation decision is taken by the owning instance from its own grants. No decision is delegated to the requesting instance or to a group defined there.
5. **Fail-safe sanitisation default.** A federated grant de-identifies served signal bytes unless the grantor explicitly declines it.
6. **Accountability.** Every request that reaches the authorisation decision is recorded on the owning instance with its outcome; every failed authentication is emitted to the security log.

Non-goals, stated so they are not inferred:

- **Authenticating the end user.** The owning instance authenticates the peer and receives an assertion about the user. See [Security considerations](#security-considerations).
- **Controlling data after disclosure.** Nothing in the protocol constrains retention or copying on the requesting side. Revocation prevents future access and cannot be retrospective.
- **Confidentiality of the federation relationship.** The discovery document is public, and the existence of a peer relationship is visible to anyone observing traffic.
- **Protection against a compromised owning instance.** The owning instance holds the data; the protocol protects its data from others, not others from it.

## Trust assumptions

1. The requesting instance asserts `sub` honestly. A compromised peer, or an operator of one, can assert any identifier.
2. The WebPKI is sound for the instance URLs in use: a TLS certificate valid for an instance's host is held only by that instance.
3. Instance clocks agree to within 30 seconds.
4. The owning instance's nonce store is shared by every worker process, available, and not writable by an attacker.
5. Private keys are readable only by the instance processes that sign with them.
6. Out-of-band fingerprint verification at trust establishment is performed and is itself authentic.

## Instance identity

An instance is identified by its **instance URL**: the HTTPS base URL configured as `FEDERATION_INSTANCE_URL`, with no trailing slash. The same string is the peer's identity in every peer table, the `iss` of every token it issues and the `aud` of every token issued to it, so an instance MUST use one URL consistently for all three.

Each instance holds an Ed25519 key pair. Keys are encoded as the raw 32-byte key in base64url, 43 characters. Federation is enabled only when the instance URL and both halves of the key pair are configured; at startup the reference implementation refuses to run if the public key does not derive from the private key, and applies the same check to an announced next pair.

## Discovery document

```
GET {instance URL}/.well-known/epicurrents-federation.json
```

```json
{
  "federation_public_key": "<base64url Ed25519 public key>",
  "federation_public_key_next": "<base64url Ed25519 public key>"
}
```

`federation_public_key_next` is present only while a [key rotation](#key-rotation) overlap is announced. An instance with federation disabled answers 404. The document carries no version, no instance metadata and no signature; its authenticity rests entirely on TLS.

A fetching instance MUST:

- refuse a URL whose host resolves to an address that is not globally routable, unless the address lies inside a network the operator has explicitly listed, and refuse NAT64 translation prefixes unconditionally (the SSRF guard, `_check_url_is_safe`);
- require a certificate valid for the host, with hostname checking and TLS 1.2 or later;
- reject a response larger than 64 KiB;
- reject the document if `federation_public_key` is absent or does not parse as an Ed25519 key, and if `federation_public_key_next` is present but does not parse.

## Establishing trust

Trust is directional and explicit. Each side decides separately whether to accept requests from the other.

1. **Register.** An operator adds the peer by its instance URL. The instance fetches the discovery document and stores the key with the peer marked **untrusted**. An untrusted peer's requests are refused.
2. **Verify.** The operator obtains the peer's key fingerprint through a channel independent of the network path used in step 1 and compares it with the stored key. The fingerprint is the lower-case hex SHA-256 digest of the raw 32-byte public key; comparison ignores case, colons and spaces.
3. **Promote.** The operator marks the peer trusted. The reference implementation's command-line path (`federation_trust_peer --fingerprint`) refuses the promotion when the supplied fingerprint does not match the stored key.
4. **Reciprocate.** For requests to flow in both directions, the other instance performs steps 1–3 for this one.

**Key refresh.** An operator can re-fetch a peer's discovery document. The stored current and next keys are replaced with what the document holds, and a changed key is logged with both fingerprints and audited. The peer's trust state is unchanged by a refresh.

**Revocation of a peer.** Marking a peer untrusted refuses its future requests. Deleting it additionally deletes every grant that targets it. Neither is communicated to the peer, and there is no mechanism for distributing a revocation to other instances.

## Authenticating a request

### Transport

Every federated request is an HTTPS request carrying

```
Authorization: FederatedBearer <token>
```

The requesting instance MUST mint a fresh token for every HTTP request, immediately before sending it. A token is spent by its first acceptance, so a token reused for a second request is refused as a replay.

### Token

A token is a JWS in compact serialisation:

```
base64url(header) "." base64url(payload) "." base64url(signature)
```

- **header** is the JSON object `{"alg":"EdDSA","typ":"JWT"}`.
- **payload** is a JSON object carrying the claims below.
- **signature** is the Ed25519 signature, under the issuer's private key, over the ASCII bytes of `base64url(header) "." base64url(payload)`.

| Claim | Type | Issuer sets | Verifier requires |
|---|---|---|---|
| `iss` | string | Issuer's instance URL | Present; after trimming whitespace and trailing `/`, equal to the URL of a trusted peer. |
| `aud` | string | Owning instance's URL, as registered in the issuer's peer table | Exactly equal to the verifier's own instance URL. |
| `sub` | string | Local user identifier on the issuer | Passed to authorisation unchanged. Not validated. |
| `iat` | integer | Current time | Present, integer, not more than 30 s in the future, not more than 90 s in the past. |
| `exp` | integer | `iat + 60` | Present, integer, not more than 30 s in the past. |
| `jti` | string | Random UUID4, hex | Present, non-empty, not previously accepted. |
| `htm` | string | Request method, upper-case | Present; equal to the received request's method, upper-cased. |
| `htp` | string | Request path, see Request binding below | Present; equal to the received request's path. |
| `bnd` | string | Digest of bound request context, see Request binding below | Present; equal to the digest computed from the received request. |

Every claim in the table is mandatory. A verifier MUST NOT read an absent claim as "not applicable": the sender chooses which claims to send, so an optional check is one an attacker can switch off. Time claims MUST be JSON integers: a verifier MUST refuse a float, a numeric string, a boolean, and the `NaN` and `Infinity` literals some JSON parsers accept, since every comparison with NaN is false and an `exp` compared as received would never expire.

### Request binding

Binding ties a token to the request it authorises, following the construction of DPoP (RFC 9449) but inside the existing signed token rather than a separate proof.

**`htm`** is the HTTP method in upper case.

**`htp`** is the request path. The two sides derive it differently because they see the path at different stages of decoding, and they agree only if the path is decoded exactly once in total:

- The **issuer** takes the URL it is about to request, keeps only the path component (discarding scheme, authority, query and fragment), percent-decodes it once, and prefixes `/` if absent.
- The **verifier** takes the request path as its HTTP framework presents it after decoding, and neither decodes it again nor splits anything off it. A path segment that arrived as `%3F` is a literal `?` at this point and is part of the path.

The consequence for deployments: a reverse proxy between the two instances MUST NOT rewrite the path, and an instance served under a path prefix binds that prefix on both sides.

**`bnd`** covers request context that changes what is served but appears in neither the method nor the path:

```
bnd = base64url( SHA-256( UTF-8( "range=" + trim(Range) ) ) )
```

where `Range` is the value of the `Range` request header, or the empty string when the header is absent. Additional context is added as further `key=value` lines joined by `\n` in a fixed key order, so adding a field never changes the digest of an existing one.

Test vectors:

| `Range` header | `bnd` |
|---|---|
| absent | `1Z5b5D_gTiKkUz2EchvsjI-gKZXveyz5qnWghrjEIeE` |
| `bytes=0-1023` | `DekZFqbfZIyXbxXUC-3j1WqCEBvW5LsRkv7dPq6odpk` |

**Deliberately unbound:**

- **The absolute URI.** `aud` already pins scheme and host; reconstructing them on the verifying side from forwarded headers behind a proxy fails opaquely when that configuration drifts.
- **The query string**, for the reason DPoP omits it: intermediaries rewrite it. A query parameter that changes what is served belongs in `bnd`. The slice endpoints are the current exception — see [Open issues](#open-issues).

### Issuing a request

1. Build the full request URL and decide the `Range` header, if any.
2. Build the claims: `iss` own instance URL, `aud` the peer URL, `sub` the local user identifier, `iat` now, `exp` `iat + 60`, `jti` a fresh UUID4, and `htm` / `htp` / `bnd` from step 1.
3. Sign with the current private key.
4. Send the request with the `Authorization` header and exactly the `Range` header used in step 2, over a TLS connection with certificate and hostname verification and TLS 1.2 or later.

### Verifying a request

The owning instance performs these steps in order and stops at the first failure.

1. Federation is enabled on this instance; otherwise **403**.
2. The `Authorization` header uses the `FederatedBearer` scheme; otherwise **401**.
3. The token has three dot-separated parts; otherwise **401**.
4. Decode the payload **without** verifying it and read `iss`, trimmed of whitespace and trailing `/`; absent → **401**. This value is used only to select a key; nothing else from the unverified payload is trusted.
5. Look up a peer whose URL equals `iss`. None, or not trusted → **401**.
6. Verify the token against the peer's current key:
   1. the header is a JSON object whose `alg` is `EdDSA`;
   2. the signature is valid;
   3. the payload is a JSON object;
   4. `exp` is present and a JSON integer, and `exp + 30 ≥ now`;
   5. `iat` is present and a JSON integer, `iat − 30 ≤ now`, and `iat + 60 + 30 ≥ now`;
   6. `aud` equals this instance's URL;
   7. `htm`, `htp` and `bnd` are present and equal to the values derived from the received request — never from the token.
7. If step 6 failed **on the signature** and the peer has an announced next key, repeat step 6 with that key. A failure of any other kind is not retried, so a rotation cannot be used to bypass the time, audience or binding checks.
8. `jti` is present and non-empty; otherwise **401**.
9. Atomically record `jti` in the shared nonce store until the token could no longer pass step 6 — the earlier of `exp + 30` and `iat + 90`, read from the token — plus a margin of 5 s. If it was already recorded → **401** (replay).
10. Accept: the request is from this peer, on behalf of remote user `sub`.

Every failure at steps 2–9 emits a `federation.auth_failed` security event carrying the client address, the peer if one was resolved, and the reason. Values claimed by the token — the algorithm, the audience and the binding claims — appear in the reason only as truncated digests: the first 16 hexadecimal characters of SHA-256 over the value's string form. They are text chosen by the sender, and for the algorithm by anyone at all, since the header is read before the signature is verified; the security log must not carry them verbatim.

A request that carries both a valid session cookie and a federated token is authenticated as the session user; the token is not examined.

### Freshness and replay

With the lifetimes above, a token is accepted from 30 s before its `iat` until 90 s after it by the verifier's clock. The upper bound is the same whether it is reached through `exp` or through `iat`, so an issuer choosing a longer lifetime gains nothing: the verifier caps token age independently of the issuer's claim.

Within that window the nonce store makes a token single-use. Because the token is bound, a replay that does get through can only repeat the original request — the same method, path and byte range, returning the same bytes. The nonce's lifetime is anchored to the token rather than to its arrival. A verifier whose clock lags the issuer's sees a fresh token up to 30 s before its own `iat`; a lifetime counted from arrival would expire while the token is still acceptable, and the same token would pass again. The behaviour when the store is unavailable is undefined — see [Open issues](#open-issues).

There is no revocation of an individual token. A token that has been issued remains valid until its window closes.

## Key rotation

Rotation replaces an instance's signing key without an outage, provided every peer refreshes during the overlap.

1. **Announce.** The rotating instance generates a new pair and publishes the public half as `federation_public_key_next` while continuing to sign with the current key.
2. **Refresh.** Each peer's operator refreshes the rotating instance's key. The peer now holds both keys and accepts signatures under either (verification step 7).
3. **Promote.** The rotating instance moves the next pair into the current slots, stops announcing a next key, and signs with the new key. Peers that refreshed in step 2 continue to verify; peers that did not refuse every request until they refresh.

An emergency rotation replaces the current pair in one step, with no overlap, for use when the key is believed compromised. Every peer refuses the rotating instance's requests until it refreshes. There is no channel by which the rotating instance can tell peers that its previous key must no longer be accepted.

## Authorisation

Authentication yields a peer and an asserted `sub`. The owning instance then decides, for the object named by the request path, whether to serve it and on what terms. The decision is taken entirely from the owning instance's own grants.

### Grants

A federated grant is an `AccessRight` row with:

- `federated_peer` — the peer;
- `remote_user_id` — one remote user's identifier, or the empty string meaning **any user asserted by that peer**;
- `can_read`, `can_write`, `can_share` — permission flags; only `can_read` is consulted by any federated endpoint;
- `apply_middleware` — whether served signal bytes are sanitised; defaults to true for a federated grant;
- `expires_at` — optional expiry.

An object carries at most one grant per `(peer, remote_user_id)` pair. Grants can target a peer or a user asserted by a peer, never a group defined on the requesting instance, because a remote group would place the decision in the hands of that instance's administrators.

Only an object's author, a holder of `can_share` on it, or a superuser may create a federated grant.

### Resolution

For a peer `P`, an asserted user `U` and an object `O` (`get_federated_read_access_result`):

1. **Visibility gates.** If a registered gate hides `O` from an unprivileged caller — a recording in the trash, or one whose processing failed — the result is *denied*, before any grant is read.
2. **Direct grants.** Select unexpired grants on `O` for `P` with `can_read`, where `remote_user_id` is empty or equal to `U`. If both a wildcard and an exact grant match, the exact grant wins. A match returns *granted* with that grant's `apply_middleware`.
3. **Extensions.** Only when no direct grant matched, consult registered federated extensions. The reference implementation registers one: membership of `O` in a dataset shared with `P` (and `U`). When several extensions grant access, the grant that sanitises wins.
4. Otherwise *denied*.

There is no author or superuser shortcut for federated callers, and no interpretation of `sub` beyond equality with `remote_user_id`.

Listing endpoints apply the same resolution in batch, so an object appears in a peer's listing if and only if the per-object check would grant it, with the same sanitisation terms.

### Order of checks at an endpoint

On the endpoints that serve recordings, a federated request is processed as: authenticate → resolve the object from its hash (absent → 404) → failed or trashed → 404 → authorisation (denied → 403) → per-peer limits where they apply (exceeded → 429) → audit → serve.

## Federated endpoints

Every federated endpoint is a `GET`. None modifies data on the owning instance; each writes only its audit records.

| Path | Serves | Sanitisation applies | Per-peer limits | Audit rows on the owning instance |
|---|---|---|---|---|
| `/recordings/api/v1/` | Listing of readable recordings, failed ones excluded, with per-recording `download_size` | — | none | one summary row per call |
| `/recordings/api/v1/status/{hash}` | Processing status | — | none | one per request |
| `/recordings/api/v1/{hash}` | Recording metadata; events and labels as object hashes, interruptions with their timing | — | none | one per request |
| `/recordings/api/v1/{hash}/slice` | Metadata for a time window, including each overlapping event's `name` and `value` | **no** | none | one per request |
| `/recordings/api/v1/{hash}/file` | File bytes; `Range` supported | yes, per grant | download rate, daily bytes | one per request |
| `/recordings/api/v1/{hash}/file/slice` | Signal file bytes for a time window given in the query | yes, per grant | download rate, daily bytes | one per request |
| `/media/api/v1/{hash}` | Media file metadata | — | none | one per request |
| `/media/api/v1/{hash}/file` | Media file bytes; `Range` supported | **no** | none | refusals on every request; successful reads on the first request of a playback only |
| `/api/v1/federation/inbound/objects/{ct_id}/{object_id}/` | Whether the object is readable: `{content_type_id, object_id, model, app_label}` | — | inbound rate | one per request |

`{hash}` is the opaque 32-character identifier of a recording or media file. Author-private fields — a recording's original filename and processing error — are always null for a federated caller.

The inbound check endpoint answers every non-success outcome — unknown content type, unknown object, no grant, failed recording — with the same 404 body, so a peer cannot use it to learn which objects exist. It decides access with the same resolver as the serving endpoints, but addresses objects by integer primary key rather than by the opaque hash the rest of the surface uses; see [Open issues](#open-issues).

## Sanitisation

`apply_middleware` on the matching grant decides the form in which recording **bytes** are served:

- **True** — the served bytes pass through the owning instance's serve pipeline: the fixed EDF/BDF header is stripped of patient and recording identification and its channel block de-identified, and annotation text inside the signal file is replaced by the mandatory timekeeping records. The pipeline is built at a single construction site for every byte-serving path — full file, byte range, time slice and the size advertised in listings — and a contract test fails if any path assembles its own.
- **False** — the stored file is served. Stored files already carry ingest-time header and channel-block de-identification; annotation text is present in a stored file only if it was preserved at upload.

The grant selects whether the pipeline runs, not what it contains; every recipient under a sanitising grant receives the same transformation.

Sanitisation applies to recording bytes only. It does not apply to metadata responses — in particular, event names and values in the slice response are served as stored — and no de-identification exists for media files.

The requesting instance may apply its own transformations to received bytes (for example, dropping channels in a FUSE mount). These carry no privacy guarantee, since they run on the side that has already received the data.

## Audit

### Owning instance

Each request that reaches the authorisation decision writes a `FederationAuditLog` row: peer (and its URL, retained if the peer is deleted), asserted `sub`, endpoint name, target object — or the probed identifiers when no object resolved — HTTP status, and time. Rows are written for granted, denied, hidden and rate-limited outcomes alike. Authentication failures are not written there; they go to the security log as `federation.auth_failed`.

The rows do not record the byte range served, whether sanitisation was applied, or the number of bytes transmitted.

Grant creation, renewal and revocation, peer registration, trust changes and key refreshes are recorded in the platform's general audit trail, as are the grant rows deleted when a peer is deleted.

### Requesting instance

A FUSE mount records one `federation.remote.read` activity per recording per mount, on the first byte actually read, attributed to the local user the mount runs as and identified by the recording's hash. The listing fetched at mount time is recorded once per peer. Individual range requests are not recorded on the requesting side.

## Rate limits

The owning instance bounds what a single peer can pull, per peer and independently of which remote user is asserted.

| Limit | Applies to | Default |
|---|---|---|
| Download requests per minute | `/{hash}/file`, `/{hash}/file/slice` | 60 |
| Bytes per UTC day | `/{hash}/file`, `/{hash}/file/slice` | 1 TiB |
| Inbound checks per minute | inbound check endpoint | 600 |

The daily budget is charged the **full file size** on every download request, including range and slice requests, so the budget bounds what a peer can obtain rather than what it chooses to consume. Exceeding a limit returns 429 and is audited. Counters live in the same shared cache as the nonce store.

## Requesting-instance behaviour

The reference client is the FUSE filesystem ([federation/fuse_fs.py](../federation/fuse_fs.py)); the `federation_check_peer` command issues a single signed probe for diagnosis.

- **Identity asserted.** The mount runs as one local user, named by the operator when mounting (`--user-id`). Every token the mount issues carries that identifier as `sub`.
- **Signing key.** The mounting process signs with the instance's own federation private key, so it needs read access to that key.
- **Listing.** At mount time the client fetches the listing from every peer it trusts, up to 200 recordings per peer, and holds it for the life of the mount.
- **Reads.** A read of bytes not yet held locally issues one range request with one freshly minted, range-bound token. A read spanning the header and signal regions issues two requests with two tokens.
- **Caching.** Each recording's header is fetched once and served from memory for the life of the mount. With a local transformation that needs the whole file, the whole transformed file is held. Nothing is evicted.
- **Transport.** Requests use the same strict TLS settings as the discovery fetch, to the peer URL stored at registration.

## Error responses

| Status | Meaning to a peer |
|---|---|
| 401 | Authentication failed. On federation endpoints the body names the reason; on recording and media endpoints an invalid token is treated as no credentials and the body does not. |
| 403 | Federation is disabled on the owning instance (federation endpoints only; recording and media endpoints answer 401), or the object exists and no grant covers it. |
| 404 | No such object, or the object is hidden (failed or trashed). The inbound check endpoint also uses 404 for "no grant". |
| 410 | Media file of a type the owning instance no longer serves. |
| 422 | A slice requested from a file that cannot be sliced. |
| 429 | A per-peer limit was exceeded. |

Error body text is diagnostic and not a stable interface.

## Versioning and compatibility

The protocol carries no version identifier, and instances do not negotiate. Compatibility is enforced by the verifier's requirements: when a release makes a claim mandatory, instances on earlier releases are refused, with a message naming the missing claims.

Such a change breaks in one direction. An upgraded instance still reaches an older peer, which ignores claims it does not check, but refuses that peer's requests; both sides must upgrade for traffic to flow both ways. Request binding and mandatory `jti` are the current instances of this. A signed version claim, with a per-instance minimum and suspension of grants for peers below it, is planned in the [ROADMAP](../ROADMAP.md).

## Security considerations

- **The asserted user is only as trustworthy as the peer.** A compromised peer, or a sufficiently privileged operator of one, can assert any identifier and so obtain anything granted to any user of that peer. Narrowing a grant to one remote user constrains an honest peer's population, not a dishonest peer. On the reference client the asserted identifier is a mount-time argument chosen by the operator.
- **No evidence of how the user authenticated reaches the owner.** The owning instance cannot require multi-factor authentication or a particular identity provider for remote users.
- **Wildcard grants** extend access to every user the peer asserts, including accounts created there after the grant.
- **Trust on first use.** The key stored at registration is whatever answered on the network path at the time; out-of-band fingerprint verification is what makes it trustworthy, and it is enforced only on one promotion path. A key refresh on an already-trusted peer is not re-verified.
- **Key custody.** Private keys are held in the instance's environment file, which also holds its database, cache and backup credentials. Reading that file yields the ability to sign as the instance.
- **The nonce store is a security dependency.** Anyone able to write to the shared cache can remove recorded nonces and re-enable replay within the acceptance window; binding restricts such a replay to the original request.
- **Revocation is prospective.** Removing a grant or distrusting a peer stops future requests, including through an established FUSE mount for bytes it has not cached. It does not reach bytes already disclosed, the requesting side's caches, or copies made from them.
- **The FUSE mount is part of the instance's trust boundary.** It holds the signing key and caches received data in memory, and inside a container it requires the `SYS_ADMIN` capability and `/dev/fuse`, which substantially weaken container isolation.
- **Existence disclosure is uneven.** The inbound check does not distinguish a missing object from an unauthorised one, but the recording endpoints answer 404 for a missing object and 403 for an existing one without a grant. The 32-character identifiers make that difference hard to exploit by guessing, not impossible to observe for a known identifier.
- **Outbound URL safety** is checked when a peer's discovery document is fetched and when the diagnostic probe is issued, not on listing or byte requests, which go to the stored peer URL. DNS rebinding between the check and the connection is not addressed.

## Open issues

Departures from the goals above, and gaps the protocol does not yet close. Items with a ROADMAP entry say so.

1. **Nonce store unavailability is undefined.** An error from the cache propagates as an unhandled exception. The request fails, but by accident; the intended behaviour is an explicit 503 with a security event.
2. **Event text reaches peers regardless of sanitisation.** The slice metadata endpoint returns each overlapping event's `name` and `value` to any peer holding a read grant, with no regard to `apply_middleware`. A grant configured to strip annotation text from the signal file still discloses structured events through this path. No test covers what a federated caller receives from it.
3. **The inbound check addresses objects by integer primary key.** Every other peer-facing route names an object by its opaque hash, so this one exposes a key scheme the rest of the surface avoids, and the count and creation order it implies. Changing it alters the path both sides sign, so it belongs with peer version gating.
4. **Query-carried parameters are unbound.** `/{hash}/file/slice` takes its time window in the query, so a token for one window can be replayed for another window of the same object. This grants nothing beyond the grant, and no client mints tokens for the endpoint yet; binding the named parameters into `bnd` is on the ROADMAP.
5. **Two settings are inert.** `FEDERATION_JWT_TTL` and `FEDERATION_KEY_FETCH_TIMEOUT` are defined and documented but never read. Token lifetime is fixed at 60 s and the discovery fetch timeout at 10 s.
6. **Trust verification is partial.** The API promotion path takes no fingerprint, and a key refresh replaces a trusted peer's key without re-verification or loss of trust.
7. **`sub` is linkable.** The issuer sends its local integer user identifier to every peer, so peers can correlate one user across instances. A per-peer pseudonym is on the ROADMAP.
8. **No end-user assertion.** Carrying an identity-provider-signed assertion about the user, which the owning instance validates itself, would remove the dependence on the peer's honesty for user identity and carry authentication strength with it.
9. **One capability.** Read access covers listing, metadata, time slices, byte ranges and whole files identically, and `can_write` / `can_share` on a federated grant have no effect. The distinctions a governance decision needs — metadata only, sanitised signal, raw file, annotations, bulk retrieval — cannot be expressed.
10. **Media is served without sanitisation or per-peer limits.** Keeping media out of federated grants unless explicitly acknowledged is on the ROADMAP.
11. **Audit rows lack the terms of disclosure.** Byte range, sanitisation applied and bytes transmitted are not recorded, so the log shows that a disclosure happened but not what it contained.
12. **No revocation distribution and no version gating.** A compromised instance can be distrusted locally but not announced to others; peer version gating is on the ROADMAP.
13. **Outbound URL checks are not repeated**, and DNS rebinding is open. Tracked in [federation/README.md](../federation/README.md).
14. **Signing keys live in the environment file.** A restricted key file or an external key service would separate signing capability from the rest of the instance's secrets.
