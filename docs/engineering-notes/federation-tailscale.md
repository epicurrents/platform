# Federation — optional Tailscale network layer

Investigation into running the federated instances on a shared Tailscale tailnet and restricting inter-instance federation traffic to that network. Findings from a code trace of the federation data path plus the concrete configuration and one code change required. Nothing here is committed; this is a feasibility + design record.

## Update 2026-07-06 — direction decided, work sequenced

The feasibility below stands. This section records the decisions taken since and the order of work; the original analysis is kept intact underneath.

**Slice one (complete 2026-09-04) — the management-command layer, topology-agnostic.** Independent of which network model wins, the operator ergonomics are the same: extract the peer/grant logic from the Ninja endpoints into [federation/services.py](../../federation/services.py) so the API and a CLI share one implementation of the SSRF guard, trust gate, and audited writes, then add management commands over it — `federation_add_peer` (auto-fetch key, print fingerprint for the out-of-band TOFU check), `trust_peer` (flip `is_trusted` only if a passed fingerprint matches), `refresh_peer_key`, `grant` / `revoke_grant` / `renew_grant`, `list_peers` / `list_grants`, and the high-value `check_peer` handshake (resolve → well-known → TLS → test JWT → inbound probe, reporting which layer failed). This also closes the grant-renewal gap: `AccessRight.expires_at` has no update path today (the API is POST/DELETE only), so the slice adds a renew service function and a `PATCH /grants/{id}/` endpoint. CLI writes create peers and `AccessRight` grants, so each wraps its writes in `with_system_activity(..., interface=Activity.Interface.COMMAND)`.

**Network layer — preference is Route B (dual userspace roles).** The deployment-exposure `tailscale` service already shipped (see [getting-started → tailnet](../getting-started.md)) is userspace and **inbound only**: it proxies tailnet → `web` but gives `web` no tailnet interface, route, or MagicDNS, so it cannot make the *outbound* instance-to-instance call federation needs. Two ways to add the outbound leg:

- **Model A — one kernel-mode sidecar, `web` shares its netns** (`network_mode: service:tailscale`, needs `/dev/net/tun` + `NET_ADMIN`). Native routing, no application code change, but privileged, drops `web`'s unprivileged posture, moves `web`'s ports into the sidecar (reverse proxy must follow), and requires the sidecar to join the `epicurrents` network so `web` still reaches db/redis. This is the "Recommended topology" section below. Its SSRF change is the resolved-IP `FEDERATION_ALLOWED_PEER_CIDRS=100.64.0.0/10` carve-out, because `web` resolves MagicDNS locally to a `100.x` address.
- **Model B — dual userspace roles (preferred).** Keep the one unprivileged userspace container and add its **outbound HTTP proxy** (`--outbound-http-proxy-listen`) alongside `serve`. `web` stays on `epicurrents` with its own netns and ports; only the federation HTTP client reaches the tailnet, through a **scoped `urllib` `ProxyHandler`** (built into the shared federation opener, used in [federation/auth.py](../../federation/auth.py) and [federation/fuse_fs.py](../../federation/fuse_fs.py)) — not the process-global `HTTPS_PROXY` rejected in §4, so public egress (push, SMTP, public-peer key fetch) is untouched. The proxy does remote DNS (MagicDNS resolves inside tailscaled) and `CONNECT`-tunnels, so strict TLS stays end-to-end to the `.ts.net` name with no cert work. Cost: a small federation- client change (paid for by the services extraction) and a **different SSRF change** — because the proxy resolves remotely, `web` never sees the `100.x` IP, so the guard needs a **host-suffix allowlist** (`FEDERATION_ALLOWED_PEER_HOSTS`, e.g. `*.ts.net`) that routes matched hosts via the proxy and skips local resolution, while every other host still goes through the normal resolve-and- block path. DNS-rebinding pinning (Future-enhancement in the federation README) then lives at the proxy's trust boundary.

  Route B is preferred because it preserves the unprivileged-`web` invariant the compose file treats as load-bearing, leaves `web`'s ports and the reverse proxy alone, and composes with the deployment-exposure container (add federation outbound as a flag on the same service) rather than replacing it.

**Grant-request flow — deferred design fork.** Grants are owner-push today (the data owner creates the `AccessRight`; there is no request protocol, and the rule is "only the local data owner decides"). "Requesting a grant from a peer" is therefore either: **Option A** — no protocol; an out-of-band ask, the owner runs `federation_grant`, the requester runs `check_peer` to confirm reachability; or **Option B** — a JWT-authenticated inbound `GrantRequest` endpoint + model on the owner, with `federation_request_grant` / `review_requests` / `approve_request` commands and explicit owner approval (never auto-grant, to keep owner authority). Start with Option A ergonomics; add the protocol only if operator-to-operator coordination proves to be the bottleneck.

**Other points to resolve after slice one.** Hook `FEDERATION_INSTANCE_URL` = MagicDNS URL into the tailnet registration flow so `iss` / `aud` / well-known stay consistent (the opaque-401 failure mode is unforgiving); the `export_federation_audit` command (federation README Future enhancements) ties in for tailnet SAR/breach response.

## The question that decides everything

**Does federated data flow instance-to-instance, or must every user accessing federated resources also be on the tailnet?**

It flows **instance-to-instance (server-to-server)**. A user only ever talks to their own home instance, which fetches from the peer on their behalf and relays the bytes back. So **users do not need to join the tailnet** — only the instances do. That is what makes the whole idea clean: the sensitive inter-instance path moves onto WireGuard while end-user access over the public internet / hospital LAN is completely unaffected.

## How federated data actually flows

Traced end to end for a user on instance A opening a recording that lives on peer instance B:

```
browser ──▶ home instance A ──▶ peer instance B
  GET /recordings/api/v1/{hash}    GET /recordings/api/v1/{hash}
  (session cookie)                 Authorization: FederatedBearer <Ed25519 JWT>
                                   Range: bytes=...
        ◀── relayed bytes ──               ◀── 206/200 (PHI-sanitised) ──
```

- The **browser never contacts instance B.** Instance A is a relay. The download endpoint is [download_recording](../../recordings/api/v1/ninja.py) in `recordings/api/v1/ninja.py`.
- The outbound authenticated request to the peer is issued **by instance A's `web` container** (the Django process), not Celery. The HTTP client is plain `urllib.request` — the range client in [federation/fuse_fs.py](../../federation/fuse_fs.py) (`_http_range`) and the key-fetch client in [federation/auth.py](../../federation/auth.py) (`fetch_peer_public_key`).
- The **FUSE filesystem** (`mount_federation_fs`) is the same server-side story: a long-running management command on the app server that signs a JWT per read and pulls peer bytes over HTTP. It is not something an end user runs.
- PHI sanitisation runs on **B** before the bytes leave it (the federation middleware pipeline), so the relay never sees raw PHI headers.

## Recommended topology

Only the instances join the tailnet:

- **Tailscale sidecar container** (official `tailscale/tailscale` image), behind a compose profile so default deployments never pull it. A lib is not an option — there is no in-process Python client (tsnet is Go-only).
- The **`web` container shares the sidecar's network namespace** (`network_mode: service:tailscale`), so it sees the tailnet interface, the `100.64.0.0/10` routes, and MagicDNS. In kernel mode (needs `/dev/net/tun` + `NET_ADMIN`) Tailscale adds only the `100.x` route — normal egress (push notifications, SMTP, etc.) still leaves via the default route, so nothing else is captured.
- **`tailscale serve https`** on the sidecar publishes the app to the tailnet under a MagicDNS name with a real Let's Encrypt certificate (see TLS below).
- A **Tailscale ACL** restricts which nodes may reach the federation port; optionally combine with a reverse-proxy rule so `/api/v1/federation/*` and the federated download branch are reachable only over the tailnet.

**Operational wrinkle.** With the shared network namespace the `web` container no longer owns its own ports (they live in the sidecar's netns), so the public reverse proxy has to reach the app through the sidecar's published port. Doable, but it is the one part of the compose wiring that needs care.

## Required changes and gotchas

### 1. SSRF guard blocks the Tailscale range (the one code change)

Tailscale assigns addresses in `100.64.0.0/10` (CGNAT). The federation SSRF guard `_check_url_is_safe` in [federation/auth.py](../../federation/auth.py) rejects any address for which Python's `ipaddress` reports `is_global == False`, and CGNAT is non-global — so peer **registration / key-fetch** against a `100.x` or MagicDNS URL fails today. (Data downloads reuse the already-registered `FederatedPeer.url` and do not re-run the guard; registration is the gate.)

The existing `FEDERATION_ALLOW_PRIVATE_PEER_URLS` flag unblocks it but is a **blanket disable** of the whole guard — it also re-opens RFC-1918, loopback, and the cloud metadata endpoint (`169.254.169.254`) to a mis-registered peer URL. Registration is an admin action, so the risk is bounded, but it is coarser than necessary.

**Recommended enhancement:** add a narrow `FEDERATION_ALLOWED_PEER_CIDRS` setting (e.g. `100.64.0.0/10`) that the guard treats as allowed **while still blocking** every other private / special-use range. This keeps SSRF protection against the genuinely dangerous ranges and only opens the tailnet. Small, well-scoped change to `_check_url_is_safe`, and worth a contract test asserting that the metadata endpoint and RFC-1918 stay blocked when only the CGNAT range is allowed.

### 2. Strict outbound TLS — solved by MagicDNS certificates

The outbound peer client builds a strict TLS context in [federation/auth.py](../../federation/auth.py) (`_build_tls_context`): `CERT_REQUIRED`, `check_hostname=True`, TLS 1.2+. Plaintext `http://` and self-signed certs both fail. This is why the MagicDNS route matters: `tailscale serve` / `tailscale cert` provisions **publicly-trusted Let's Encrypt certificates** for `*.<tailnet>.ts.net` names, which the default SSL context accepts with no custom-CA work. No code change needed — just address peers by their MagicDNS HTTPS URL.

(The guard from #1 is still required even with a MagicDNS name, because the name resolves to a `100.x` address and the SSRF check runs on the resolved IP.)

### 3. Peer identity is the URL string

JWT `iss` / `aud` are exact-match against each instance's `FEDERATION_INSTANCE_URL`, and the `.well-known` document must be reachable at that same URL (`verify_jwt` / `parse_federation_auth` in [federation/auth.py](../../federation/auth.py)). This works fine with tailnet addressing — set each instance's `FEDERATION_INSTANCE_URL` to its MagicDNS HTTPS URL and register peers by that URL. Consistency is the only requirement.

### 4. Why not the proxy-env shortcut

`urllib` honours `HTTPS_PROXY`, and Tailscale userspace mode can expose an HTTP proxy, so in principle the sidecar could proxy outbound federation calls without sharing a netns. Avoid it: the proxy env var is **process-global**, so it would also route push-notification delivery and any other public egress through a tailnet-only proxy and break them. The shared-netns approach routes only `100.x` over the tailnet and leaves public egress alone.

## Security posture — defence-in-depth, not authority

A Tailscale node is identified by a WireGuard private key stored as a software secret on disk (`tailscaled.state`), not hardware-bound by default — so anyone with root on the host, or a copy of that state file, can clone the node identity onto another machine. The pre-auth `TS_AUTHKEY` is a separate bearer secret that enrols *new* nodes. Mitigations: tailnet lock, ephemeral tagged keys from an OAuth client, short expiry, ACL tags.

Crucially, the platform's **Ed25519 per-peer JWT auth remains the authority**: a spoofed tailnet node still has to pass federation auth, so Tailscale hardens the wire but never replaces application-layer trust. Federation endpoints are already auth-gated (only registered peers with a valid JWT get through), so exposing them publicly is not itself a vulnerability — restricting them to the tailnet is defence-in-depth. Keep the whole layer strictly optional.

## Implementation checklist (if pursued)

- **Code:** `FEDERATION_ALLOWED_PEER_CIDRS` carve-out in `_check_url_is_safe` + contract test (metadata endpoint / RFC-1918 stay blocked; CGNAT allowed).
- **Compose:** Tailscale sidecar service behind a profile; `web` shares its netns; sidecar `tailscale serve` config; public reverse-proxy path to the app.
- **Config:** per-instance `FEDERATION_INSTANCE_URL` = MagicDNS HTTPS URL; `FEDERATION_ALLOWED_PEER_CIDRS=100.64.0.0/10`; peers registered by MagicDNS URL.
- **Docs:** federation README section + an operator runbook for tailnet enrolment, ACLs, and cert provisioning.
- **Test:** ties into the existing Low-tier *"Testing — federation integration test harness (mock peer + two-instance smoke suite)"* item — a two-instance smoke over the tailnet is the natural coverage.

## Open decisions

- Blanket `FEDERATION_ALLOW_PRIVATE_PEER_URLS` vs the narrower `FEDERATION_ALLOWED_PEER_CIDRS` (recommended).
- Whether to additionally firewall the federation endpoints to tailnet-only at the reverse proxy, or rely on the JWT auth gate alone.
