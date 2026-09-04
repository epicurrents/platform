# Evidence host — the machine the application host cannot rewrite

A second, minimal machine holding the things a compromise of the application host must not be able to destroy: the log stream, the alerting that fires when that stream stops, and the backup repository. Its contract is two sentences — **append-only from the application host's perspective, and alerting independent of the application host's health.** Everything in this directory follows from those.

This is the first slice of the design in [docs/engineering-notes/intrusion-detection-design.md](../../docs/engineering-notes/intrusion-detection-design.md). The Layer 1 phases described there (audited-model registry, coverage checks, storage sweep, canaries) are not built yet; this host is useful before any of them, because three of its four components watch the deployment as it stands rather than as it is planned.

These files are **templates to adapt**, not a turnkey stack. Nothing here is exercised in CI — shipping the topology in the repository keeps the rules next to the taxonomy they match, and does not mean anyone tests it for you.

## Why it is in this repository but not in this deployment

The alert rules key on log lines this codebase emits, and [epicurrents/security_log.py](../../epicurrents/security_log.py) is load-bearing precisely because a silent rename leaves the application logging happily while every rule stops matching. Rules in a separate repository turn each rename into a two-repository change nobody remembers to make.

The deployment must couple to nothing. Deploy this from the same clone as the platform, update it with the same `update.sh`, reach it with the same key, and it is an expensive second copy of the thing it exists to escape. So: files here, **machine elsewhere, clone elsewhere, credentials elsewhere.**

## What's here

| File | Runs on | Purpose |
|---|---|---|
| `docker-compose.evidence.yml` | Evidence host | Caddy (auth) + Loki (sink + ruler) + Alertmanager. Standalone, not an overlay. |
| `Caddyfile` | Evidence host | Two credentials in front of Loki: a write-only one for the application host, a read one for a human. |
| `loki-config.yaml` | Evidence host | Receive, retain a year, evaluate rules. |
| `rules.yaml` | Evidence host | Absence rules (the point) plus the security-event rules. |
| `alertmanager.yml` | Evidence host | Email primary, webhook second, watchdog to an external dead-man's switch. |
| `promtail-remote.yaml` | Application host | Ships the `epicurrents.security` stream — and nothing else — to the evidence host, authenticated, labelled `host=epicurrents-app`. |
| `docker-compose.shipper.yml` | Application host | Runs the shipper on the `shipper` profile. |

The two application-host files, and this README, are bundled into a distribution package at the same path; the four evidence-host ones are not, since building that machine needs a checkout anyway and a copy riding along in every deployment invites running the sink beside the thing it exists to outlive.

The append-only Borg repository is host configuration rather than a container, and is set up below. Provisioning the machine itself is optional and provider-specific, so it lives apart: [examples/hetzner/](../hetzner/) will wait for an instance to come into stock in an EEA location and create it with a firewall already attached.

## What it holds, and what protects it

Two stores with different properties, and the difference matters more than the similarity.

| Store | At rest | Key custody |
|---|---|---|
| Borg repository | Encrypted | `repokey`: the key is **in the repository**, wrapped by `BORG_PASSPHRASE`, which lives in the application host's `.env` |
| Loki chunks | **Not encrypted** | — |
| Alertmanager state | Not encrypted | — |

`repokey` is what [scripts/bootstrap.sh](../../scripts/bootstrap.sh) initialises, so a copied repository plus the passphrase is full read access — this is not a keypair whose decrypting half stays on the application host. Borg's `keyfile` mode is that arrangement, and it improves exactly the case this host introduces: someone who takes the evidence host and not the application host holds ciphertext with no key material anywhere near it. The price is that losing the keyfile loses the backups even with the passphrase, and that two secrets need escrow rather than one. Weigh it against the passphrase-custody problem in [docs/operations.md](../../docs/operations.md), and do not switch without arranging escrow first.

The log store has no encryption at all. It holds IP addresses and actor ids at minimum, on a machine that is usually somebody else's, which is why the region and the processor agreement below are not paperwork. Full-disk encryption bounds the disposed-disk case; against the provider it does very little.

**Only the security stream is sent.** `promtail-remote.yaml` allowlists `logger="epicurrents.security"` and drops everything else before it leaves the application host. The reason is that this host is off-premises: shipping every container's stdout would send gunicorn paths, celery task arguments and — the real hazard — tracebacks, and `Recording.processing_error` is author-private precisely because stack traces carry filesystem paths that can embed subject identifiers. The security stream is the one channel with a standing no-raw-PII rule at every call site, which is what makes it safe to send somewhere the deployment does not own.

The cost is forensic reach: after an incident, the application logs that would reconstruct it are still only on the host that was compromised. The allowlist is written as one deliberately — keep what is named, rather than drop what is named — so that a new, noisy logger cannot start leaking by default.

That narrowing is also why the deployment emits a **heartbeat**. A stream of security events alone is silent on a healthy system, so absence of traffic cannot mean anything; `epicurrents.tasks.emit_security_heartbeat` publishes `system.heartbeat` every five minutes, and its absence is what the dead-man rule fires on. The event carries `interval_seconds` and no identifier of any kind, so the alerting window is derived from the sender rather than written down twice.

## What it does not hold

**Audit chain heads.** Nothing here lets you detect that the audit trail was rewritten. An attacker holding `ACTIVITY_HASH_KEYS` on the application host can forge rows and re-seal the chain, and no store on this host contradicts them. That is Phase 7 anchoring in the design note — periodic signed publication of each shard's `(content_type, sequence_no, after_hash)`, under a signing key deliberately different from the hash keys, so forgery of rows and forgery of anchors require two separate thefts.

Shipped log lines are a weaker relative: what has already left the host cannot be retroactively changed. But a log line is a claim rather than a cryptographic head, and the integrity-check summary lines that would carry one come from Phase 1.

## Prerequisite: the application stack must run in production mode

The shipper allowlists `logger="epicurrents.security"`, which it can only read when the stack emits one JSON object per line — that is, under `DJANGO_MODE=production` with the prod compose overlay. A development stack logs plain text, the JSON stage finds no `logger` field, and every line is dropped. That is deliberate (a development stack should not be filling an evidence host), but it presents as a working shipper that delivers nothing, so check the mode before concluding the pipeline is broken.

`docker compose logs web | tail -1` settles it: a JSON object means yes, `2026-08-29 12:05:21,863 WARNING django.request: …` means no.

## Provisioning

A small cloud VM is enough. With the shipper narrowed to the security stream, Loki receives a few events a day plus one heartbeat every five minutes, so it idles; Caddy and Alertmanager are near-static, and `borg serve` wants roughly 250 MB of RAM per TB of repository. **2 GB works, 4 GB is comfortable**, and the disk is the only part that grows — which argues for putting the repository on a separate volume rather than buying a larger machine.

What to buy, stated as a requirement rather than a product name: a **shared-vCPU instance with at least 2 cores and 4 GB**, in an EEA location. Either architecture works — every image in this stack publishes arm64 as well as amd64 — so the ARM types are worth taking when they are cheaper and in stock. Dedicated vCPU buys consistent performance under contention, which is not this workload.

On Hetzner, as of August 2026, that is a `cax11` (Ampere ARM64) or a `cx23` (Intel). Treat those names as perishable: Hetzner renames its lines between generations, and a stale name is skipped rather than rejected by the availability API. [provision-hetzner.sh](../hetzner/provision-hetzner.sh) resolves every requested type at startup for that reason, warns per unknown name, and refuses to start — printing the current names — if none of them exist.

Two other things it does that are worth knowing whether or not you use it: it refuses a non-EEA location unless the override is set explicitly, and it attaches a firewall in the create call rather than afterwards, because a server created without one answers SSH from the whole internet between boot and the second API call.

**Pick an EEA region.** This host will hold security logs containing IP addresses and actor ids, and backup archives of a system processing personal data. On Hetzner that means Falkenstein, Nuremberg or Helsinki — not Ashburn, Hillsboro or Singapore. Choosing an EEA region is what keeps the privacy notice's international-transfer answer as "none"; choosing otherwise makes it a question requiring a transfer mechanism.

**Accept the provider's data processing agreement** and add them to the notice's recipients table. The evidence host's operator is a processor for both the logs and the backups, exactly as the application host's provider is.

Then:

0. Upload the SSH keys for administration, generated for this host rather than reused — **keys, plural, and decided now**: Hetzner injects them at creation only, so a key not in that list can afterwards be added only from a session you still have, and losing all of them means rescue mode. A working key and a recovery key kept elsewhere is the minimum; a second administrator's key belongs here too if one is ever likely. Note that this is **not** the key the backups use: the application host gets its own, whose public half goes into `~borg/.ssh/authorized_keys` behind the forced command below. Sharing one key between the two would give the backup credential a shell, which is the thing the forced command exists to prevent.
1. Join it to the tailnet, with MagicDNS enabled so it has a stable name. **Settle the tailnet name first.** MagicDNS names are `<machine>.<tailnet>.ts.net`, and renaming the tailnet later changes every one of them — the shipper's push URL and `BORG_REMOTE_REPO` are a config edit, but federated peer identity *is* the URL string, so a rename after peers exist is a coordinated change on both instances. Before anything is wired, it costs nothing.
2. Close the public firewall to everything. Administrative access over the tailnet only; there is no reason for this machine to answer the internet at all.
3. Set `BIND_ADDR` to its tailnet address before starting the stack. It defaults to loopback, so an unconfigured start publishes nothing — but on a public cloud VM, `0.0.0.0` would put the log sink and Alertmanager on the internet.
4. Generate the two Caddy credentials (`caddy hash-password`), put the hashes in `Caddyfile` and the shipper's plaintext on the application host.
5. Fill in every `[FILL]` in `alertmanager.yml`, create `smtp-password`, and start the stack.

**Ownership of the bind-mounted files matters**, because none of these images run as root and a `600 root:root` config is invisible inside the container:

| File | Owner | Mode |
|---|---|---|
| `alertmanager.yml`, `smtp-password` | `65534:65534` (nobody) | 600 |
| `loki-config.yaml`, `rules.yaml` | `10001:10001` (loki) | 640 |
| `Caddyfile` | `0:0` | 600 |

Only Caddy runs as root. Named volumes are fine without intervention — Docker initialises a new volume from the image, ownership included.

## Wiring the application host

Three things have to be true on the application host, and only the last is obvious.

**It must be on the tailnet — as a host, not as a container.** The shipper is a container, and it reaches the sink by routing out through the host, so the host itself needs Tailscale installed:

```bash
./scripts/tailscale-join.sh --authkey tskey-... --hostname epicurrents-app
# from a distribution tarball, where there is no scripts/ directory:
./start.sh --tailscale-authkey tskey-... --tailscale-hostname epicurrents-app
```

The compose `tailscale` service ([scripts/tailscale-serve.sh](../../scripts/tailscale-serve.sh)) does not do this by itself. It runs in userspace mode with no TUN device, publishes the web UI inbound, and offers no route a sibling container could take — a shipper next to an unconfigured one retries forever against a name that does not resolve. If installing packages on the host is not an option, see [shipping without touching the host](#shipping-without-touching-the-host) below.

**Two values have to exist.** `EVIDENCE_PUSH_URL` in `.env`, pointing at the sink's MagicDNS name, and the plaintext credential in a file at the deployment root:

```bash
echo "EVIDENCE_PUSH_URL=http://epicurrents-evidence.<tailnet>.ts.net:3100/loki/api/v1/push" >> .env
printf '%s' 'the-write-only-password' > ./shipper-password
chmod 600 ./shipper-password
```

`shipper-password` is the plaintext whose bcrypt hash sits in this host's `Caddyfile`. Keep it out of git; the repository's `.gitignore` already names it.

**Then start the shipper**, with the overlay added to whatever set the deployment already runs — dropping one of them makes compose treat the running containers as orphans:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.proxy.yml \
    -f examples/evidence-host/docker-compose.shipper.yml \
    --profile shipper up -d promtail-evidence
```

Containers started before the host joined the tailnet keep the old resolver, so restart the shipper if it was already running when Tailscale went on.

Confirm delivery from the evidence host rather than from the shipper's logs, which report a successful POST to a URL that resolves whether or not anything stored the batch:

```bash
curl -su reader:<password> 'http://127.0.0.1:3100/loki/api/v1/query' \
    --data-urlencode 'query=count_over_time({host="epicurrents-app"}[15m])'
```

### Shipping without touching the host

For a machine where installing packages is not an option. The compose `tailscale` container can run an outbound HTTP proxy, and the shipper can be pointed at it, so the tailnet is reached with nothing added to the host. Three values, all default-off:

```bash
TS_OUTBOUND_HTTP_PROXY_LISTEN=:1055
TAILNET_PROXY_URL=http://tailscale:1055
EVIDENCE_PUSH_URL=http://epicurrents-evidence.<tailnet>.ts.net:3100/loki/api/v1/push
```

Then register the node with [scripts/tailscale-serve.sh](../../scripts/tailscale-serve.sh) and start the shipper as above. The proxy resolves MagicDNS on the client's behalf, so the shipper needs no tailnet resolver of its own — which is what makes this work at all.

**`TAILNET_PROXY_URL` is not an `HTTP_PROXY` variable, and substituting one for the other fails.** Promtail's client is built on Prometheus's HTTP configuration, which leaves the transport's proxy unset unless one is named in the client block, so `HTTP_PROXY` and `HTTPS_PROXY` in the environment are ignored and promtail dials the target itself. This was measured rather than reasoned about: a shipper carrying only the environment variables failed every push with `dial tcp: lookup … server misbehaving`, and the same container with `proxy_url` set delivered. Assume nothing about other clients either — check each one, because the failure looks like a network problem rather than a configuration one.

Two limits decide whether this is enough for a given deployment. The proxy carries **HTTP only**, so remote Borg backups, which go over `ssh://`, are not covered — a deployment wanting both off-host logs and off-host backups needs [scripts/tailscale-join.sh](../../scripts/tailscale-join.sh) regardless. And every container that needs the tailnet must name the proxy itself, so the arrangement grows a configuration step per client rather than being a property of the host.

Weigh one thing before enabling it: the proxy has no per-client authorisation, so anything on the compose network can reach every node on the tailnet through it. A host-level install has the same reach via NAT, so this is not a reason to prefer one over the other — but it is worth knowing that neither arrangement confines the capability to the one container you meant to give it to.

## The append-only backup repository

This closes a gap that exists independently of intrusion detection: `BORG_REMOTE_REPO` is unset in a default deployment, so there is no off-host backup at all.

On the evidence host, borg itself and a dedicated user whose SSH key is restricted to one forced command. `borg serve` runs *here*, so the package is a prerequisite rather than a convenience, and its major version must match the application host's client — 1.x and 2.x repositories are not interchangeable:

```
sudo apt-get install -y borgbackup
sudo adduser --disabled-password --gecos "" borg
sudo -u borg mkdir -p /srv/borg
```

On the application host, a key used for nothing else. Sharing the administrator's key would give the backup credential a shell, which is what the forced command exists to prevent:

```
ssh-keygen -t ed25519 -N "" -f ~/.ssh/id_borg -C "borg@epicurrents-app"
```

Put its public half in the borg user's `authorized_keys` on the evidence host, on one line:

```
command="borg serve --append-only --restrict-to-repository /srv/borg/epicurrents",restrict ssh-ed25519 AAAA... borg@epicurrents-app
```

Back on the application host, capture the evidence host's key into a known_hosts file of its own. The backup container has `StrictHostKeyChecking=yes` and no other route to a host key, so skipping this fails every run:

```
ssh-keyscan -t ed25519 <evidence-host> > ~/.ssh/known_hosts_borg
```

Then three values in `.env`, and initialise the repository — borgmatic does not create a missing one, it fails:

```
BORG_REMOTE_REPO=ssh://borg@<evidence-host>/srv/borg/epicurrents
BORG_SSH_KEY_PATH=/home/<user>/.ssh/id_borg
BORG_SSH_KNOWN_HOSTS_PATH=/home/<user>/.ssh/known_hosts_borg
```

```
docker compose ... run --rm --entrypoint borg borg init --encryption repokey "$BORG_REMOTE_REPO"
```

The forced command permits that: `init` creates segments rather than removing them. It also creates a **second** repository key, unrelated to the local repository's — see [export the repository keys](../../docs/operations.md#export-the-repository-keys-one-per-repository), because escrowing one does not cover the other.

Confirm the restriction did what it claims by asking the key to run something else. It should answer with borg rather than with your command:

```
ssh -i ~/.ssh/id_borg borg@<evidence-host> "id"
```

**Two things about append-only mode that surprise people.** A `prune` or `delete` run from the application host *appears to succeed* — the client stops seeing the archives — while the data remains and the repository keeps growing. Reclaiming space is a deliberate act by an administrator on this host, `borg compact /srv/borg/epicurrents` run as the borg user, after deciding the discarded archives really are discardable. That is the property being bought: an attacker with root on the application host cannot destroy backup history, and neither can a mistake.

The second is a limit rather than a surprise. The archives are encrypted with a passphrase held on the application host, so an attacker there can still *read* them, and this host cannot verify their contents. Append-only protects integrity and availability, not confidentiality.

## Verify the dead-man, or it is decoration

An alerting path that has never delivered is indistinguishable from a quiet system, which is the failure this host exists to catch. Three checks, in order of how often they are skipped:

1. **Stop the shipper on the application host and wait.** `ApplicationHostHeartbeatMissing` should fire after 20 minutes — three missed beats — and reach both channels. Stopping `celery-beat` instead is the same test through a different break, and worth doing once as well, since that is the one an attacker would choose.
2. **Pause the watchdog** — stop Alertmanager, or the whole evidence host — and confirm the *external* dead-man's switch raises an alarm. This is the one that covers this machine dying, and it is the one nobody tests.
3. **Confirm backups are monitored separately.** Borgmatic logs to its own container stdout, which the narrowed shipper does not send, so nothing here watches backup liveness. That is `BORG_MONITOR_URL`'s job — see [docs/operations.md](../../docs/operations.md#know-when-a-backup-fails). Two mechanisms for two streams, rather than widening this one.

A fourth check, learned the hard way on the first deployment: **confirm an alert actually arrives at Alertmanager, not merely that a rule fires.** Loki's ruler defaults to Alertmanager's v1 alerts API, which Alertmanager 0.27 removed; every alert is answered `410 Gone` and dropped. Nothing reports a problem — the rules evaluate, Alertmanager runs, `amtool check-config` passes. `enable_alertmanager_v2: true` in `loki-config.yaml` is what prevents it, and `curl .../api/v2/alerts` on the Alertmanager is what proves it.

Re-run the first two after any change to the shipper's `external_labels`, or to the heartbeat's schedule — those two values are the whole of what ties the absence rule to reality, and neither end fails loudly when they stop agreeing.

## Not in this slice

**WAL archiving.** Postgres `archive_command` runs inside the database container, so shipping segments off-box means putting an SSH client and key in there or adopting pgBackRest — a larger change to the running stack than the other three components combined. It is the answer to the transient-tamper blind spot and it should follow.

**The Layer 1 phases.** Nothing here detects a direct database write; it makes the evidence of one durable, and makes stopping the detectors audible. The commented-out `AuditIntegrityCheckMissing` rule is the hook for Phase 1.
