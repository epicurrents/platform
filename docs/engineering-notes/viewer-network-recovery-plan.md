# Viewer network recovery — implementation plan

Status: **phases 1-3 shipped; phase 4 open** — accepted 2026-07-30, state verified 2026-09-04. The primitives, the reader adoption and the platform integration are in place; the remaining importers and processors, and `core/src/util/text.ts`, still call `fetch` directly. Implements layers 4–5 deferred in `viewer-network-resilience.md` §6.1 (closed before the history reset, so it stays in the archive repository's `docs/engineering-notes/`), which stays the source for the audit and the layer 1–3 fixes already shipped. Scope is `frontend/viewer/epicurrents/*` plus a small platform integration point.

## 1. Decisions (resolved)

- **Breaker coordination — per-context, per-origin.** Each JS context (the main thread and each reader worker) keeps its own breaker registry keyed by URL origin. No shared cross-thread state; a reset is broadcast to workers as a commission, and worker breaker transitions are forwarded to the main thread for surfacing. Rationale: the block-load hot path takes no extra message round-trip, and each context self-protects even if coordination lags.
- **Auth recovery — explicit platform signal.** Persistent auth failure is sticky until the platform calls the public `notifySessionRestored()` after a successful re-login. Transient failures self-heal via a half-open probe; only the auth class waits for the external signal, so the viewer never probes while the session is genuinely gone.
- **Signal directions — method in, event out.** The dependency points one way only: the platform embeds the viewer and holds its app handle, so the inbound reset is a plain public method call (`notifySessionRestored()`), not a bus round-trip — the bus would add indirection without removing coupling for a single known caller. The outbound status goes on the event bus, where the viewer must not know its listeners. The method is framed as a neutral fact ("the session was restored"), not a command about network internals, so the platform stays ignorant of the breaker mechanism and the same hook serves any other subsystem that should re-run after re-auth.
- **Rollout — helper + reader first.** Build the primitives, adopt them in `GenericSignalReader` (the highest-traffic path) and wire the platform recovery API, then migrate importers/connectors/API processors incrementally. Lands the value fast and contains the review surface.

## 2. Primitives (Phase 1 — core)

New, all in `epicurrents/core/src/util/network/` (a new folder), exported from core so reader packages consume them.

### 2.1 Classification

`classifyFetchOutcome(input: Response | Error): { kind, retryable, tripsBreaker }`

| Input | kind | retryable | trips breaker |
|---|---|---|---|
| `ok` response | `success` | — | closes |
| 401 / 403 | `auth` | no | yes → open-auth (sticky) |
| 404 / 410 | `gone` | no | no (per-resource, not origin-wide) |
| 400 / 416 / 422 / other 4xx | `client` | no | no |
| 429 / 502 / 503 / 504 | `transient` | yes | on N consecutive → open-unavailable |
| 500 / other 5xx | `server` | yes (capped low) | on N consecutive → open-unavailable |
| `TypeError` (network **or** CORS — indistinguishable) | `transient` | yes | on N consecutive → open-unavailable |
| `AbortError` from our timeout | `timeout` | yes | counts toward transient |
| `AbortError` from caller signal | `aborted` | no | no |

The `gone`/`client` distinction matters: a single 404 must not open the origin breaker and strand every other resource on that origin.

### 2.2 `NetworkError`

`class NetworkError extends Error { kind; status?; origin? }`. Thrown by `resilientFetch` on any non-success terminal outcome. Kept context-internal — a worker converts it to the existing string `error`/`reason` on `returnFailure` (optionally forwarding `kind` as a discrete field so the main thread can branch).

### 2.3 `CircuitBreaker` + `BreakerRegistry`

Per-origin state machine:

- `closed` → normal.
- `open-auth` → sticky; `canRequest()` false; only `reset()` closes it.
- `open-unavailable` → entered after `TRANSIENT_TRIP = 3` consecutive transient/server failures; after `cooldownMs` (initial `5_000`, ×2 per re-open, cap `60_000`) → `half-open`.
- `half-open` → allows exactly one in-flight probe; success → `closed`, failure → `open-unavailable` with the next cooldown.

`BreakerRegistry` is a per-context singleton mapping origin → breaker, with an `onTransition(cb)` hook. On the main thread the hook dispatches to the event bus; in a worker it posts `{action:'network-status', origin, state}` (unsolicited, no `rn` — the same channel the reader's cache-signals update callback already uses).

### 2.4 `resilientFetch(url, init?, opts?)`

```
opts: { category?, timeoutMs?, retries?, signal?, breakerKey?, authHeader? }
```

- `breakerKey` defaults to `new URL(url).origin`.
- If the breaker is not `canRequest()`, throw `NetworkError('auth' | 'unavailable')` immediately — the storm short-circuit.
- Otherwise loop up to `retries`: build a timeout signal from the category default (below) and merge it with the caller signal via `AbortSignal.any`; `fetch`; `classifyFetchOutcome`. On success record it (close breaker) and return the guaranteed-ok Response. On a retryable outcome with budget remaining and no caller-abort, back off (base `300 ms`, ×2, ±30 % jitter, cap `3_000`) and retry. Otherwise record the failure (maybe trip the breaker) and throw `NetworkError`.
- The retry budget always yields to the caller's `AbortSignal`, so an outer bound like the op-queue `LOAD_BLOCK_TIMEOUT` stays authoritative.

Timeout categories (defaults, overridable): `range` 30 s (matches `LOAD_BLOCK_TIMEOUT`), `file` 120 s, `setup` 300 s, `config` 10 s, `default` 30 s.

### 2.5 Reset plumbing

The public entry point is `notifySessionRestored()` on the viewer app (§4) — a neutral notification, not a network-specific command. Its network-recovery subscriber resets the main-thread registry and fans the reset out to the workers:

- Main→worker: a new `reset-network` commission (`{origin?}`) handled in each reader worker → `registry.reset(origin)` (or reset-all when omitted).
- `GenericService` gains a `network-status` message branch (no `rn`) that re-emits the worker's breaker transition on the main event bus, and a `resetNetwork(origin?)` that sends the `reset-network` commission.

`notifySessionRestored()` is deliberately generic: network-breaker reset is its first subscriber, but any subsystem that must re-run after re-authentication (metadata refetch, etc.) hangs off the same hook without the platform learning about it.

## 3. Reader adoption (Phase 2 — core + edf-reader)

- `GenericSignalReader._readPartFromFile` / `readFileFromUrl`: replace the raw `fetch` + `_handleFetchFailure` + `_authFailed` guard with a single `resilientFetch(this._url, { headers }, { category:'range'|'file', signal, authHeader:this._authHeader })`; on a thrown `NetworkError` return `null` as today. The per-instance `_authFailed` flag and `_handleFetchFailure` are removed — their job is now the per-origin breaker, which is strictly better (a session loss latches every reader on that origin, not just one instance).
- The worker (`edf.worker.ts`) already replies with `returnFailure` on a thrown error (layer 2); no change beyond adding the `reset-network` handler.
- Net observable behaviour vs. today: identical storm-prevention, plus transient auto-recovery and an explicit auth reset.

## 4. Platform integration (Phase 3)

The seam is intentionally thin, and the dependency points only from platform to viewer — the viewer never references the platform.

- Public API: `notifySessionRestored()` on the viewer app/runtime. Internally its network subscriber resets the main-thread registry and calls `service.resetNetwork()` on every active reader service. It is a fact notification, not a network command: the platform announces that auth was restored and stays ignorant of breakers.
- The platform calls it after a successful re-login (the same place that already re-establishes the session). This is the single integration point the viewer work adds to the platform.
- The platform (and the interface) subscribe to the `network-status` event on the viewer's bus: `auth-failed` feeds the existing re-login prompt; `reconnecting`/`unavailable` drive a non-blocking indicator; an in-progress analysis can abort with a message rather than stall. Surfacing is emit-only — the embedded viewer renders no banner of its own.

## 5. Incremental migration (Phase 4)

Adopt `resilientFetch` at the remaining sites, one repo at a time, folding in the still-open silent-corruption long tail (`viewer-network-resilience.md` §6.2, in the archive): `EdfImporter` (tighten the layer-3 guard to the shared helper), `CsvImporter`, `WavImporter`/`WavReader`, `NicImporter`, `MarkdownProcessor`, `text.ts`, `DatabaseAPIConnector`, `RestApiProcessor` (+ its un-awaited `response.json()` bug), `VikingApiProcessor`. Each gains `AbortSignal` threading for free (F17) since `resilientFetch` takes a signal.

## 6. Tests

- Classification table — one case per row of §2.1, including the `TypeError` network/CORS conflation and both `AbortError` sources.
- Breaker state machine — auth stickiness, transient trip after N, cooldown → half-open → close/reopen, half-open single-probe concurrency, `gone`/`client` do not trip.
- `resilientFetch` — retry-then-succeed, exhaust-then-throw, open-breaker short-circuit, caller-abort mid-backoff, timeout→retry, per-category timeout selection.
- Reader — a mocked 401 stops further block loads and emits `auth-failed`; `resetNetworkFailures()` closes the breaker and a subsequent request proceeds; a transient blip recovers without external signal.
- Plumbing — worker `network-status` reaches the main event bus; `reset-network` commission reaches the worker registry.

## 7. Defaults to confirm during implementation

`TRANSIENT_TRIP = 3`; cooldown `5 s → 60 s` ×2; backoff base `300 ms`, cap `3 s`, `retries` 3 (range/config) / 2 (setup/file); timeout categories as in §2.4. All are starting values, tunable once the behaviour is observed against a real flaky link.
