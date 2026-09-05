# Epicurrents — Roadmap

Tracked work items across bugs, technical debt, features, investigations, documentation, and architectural improvements. Each entry is tagged with a visual priority marker; investigations are deliberately a different shape because they're decisions to be made, not tasks to be done.

| Marker | Meaning |
|---|---|
| 🔴 | High — bugs affecting users; work blocking other work; security-relevant. |
| 🟡 | Medium — debt or features with real ROI; schedule when convenient. |
| 🟢 | Low — nice-to-have; opportunistic. |
| 🔍 | Investigation — decision pending on whether to do it. |

Items are grouped by priority. Within a tier the order is loose — re-sort by topic when scanning if it helps. Finished work is removed rather than kept here; this file describes what is still to do.

## Contents

### 🔴 High

The two security entries below were gated on the evidence host, which is now in place. Ordering rationale in each entry.

- Security — Phase 7 chain-head anchoring, immediately after a successful deployment (not in the first test version, but next: without it the audit trail's tamper-evidence stops at whoever holds `ACTIVITY_HASH_KEYS`, since a host-root attacker can forge rows and re-seal the chain with nothing anywhere to contradict them. The evidence host as built stores backups and logs and detects none of that. A Celery task publishes each shard's `(content_type, sequence_no, after_hash)` on a cadence, signed with a key deliberately **not** `ACTIVITY_HASH_KEYS` so forging rows and forging anchors need two separate thefts; the receiver compares each bundle's previous head against what the chain now claims at that sequence number and emits `audit.anchor_mismatch` on divergence. The design note prefers federated peers as transport — mutual anchoring hosted by an organisation the attacker has not compromised — with the evidence host as transport two; the evidence-host path has no dependency and is what makes this deployable right after the first deployment succeeds)
- Federation — peer version gating, suspension, and a reason the other side can act on (a peer running a release with a known flaw is a hole in *this* instance's perimeter, since grants let it reach local data. Each side enforces its own `FEDERATION_MINIMUM_PEER_VERSION` rather than negotiating one; the version travels as a claim inside the signed JWT and over an authenticated peer-info endpoint, **not** in `/.well-known/epicurrents-federation.json`, which is unauthenticated and would advertise to the whole internet which release each instance is running. Existing grants are *suspended* with a reason and a timestamp rather than deleted, so an upgrade can restore them; rejection returns a machine-readable reason — `peer_version_unsupported`, the minimum required, and the version seen — which the outdated instance stores and surfaces to its own operator as "peer X suspended this connection, upgrade to Y". Fail closed on an absent or unparseable version. Distinguish this from compromise: version gating catches *outdated*, and revoking an instance known to be compromised is peer revocation, which stays manual until there is a signed advisory channel to distribute. Depends on `epicurrents.version`; follows the evidence host because the suspension and rejection events are among the first things worth shipping off-host)
- Platform — operator-triggered upgrade for instances without shell access (a deployment maintained by a hosting service has no SSH for its owner, so a suspended federation grant or a security release leaves them unable to act — which is why this follows the version gate rather than preceding it. The constraint that shapes the design: the web application must never hold the capability to execute a host operation, or a web compromise becomes host code execution and the containment model inverts. So the UI writes an audited intent row and nothing more, and a privileged agent outside the web container — a host systemd unit, not a compose service holding `/var/run/docker.sock`, since that socket is root on the host and is on the auditd watch list in the intrusion-detection note — picks it up and invokes `update.sh`. Most of the work already exists there: acquire source, snapshot the database and `.env`, build, stop application services, migrate, collectstatic, recreate, health-check — plus `--rollback`, which restores the snapshot. The agent's job is to choose the source, gate on the health check and call `--rollback` when it fails, not to reimplement any of that. **Archive mode is the vehicle**: a distribution tarball ships prebuilt bundles, so it needs no node and no frontend build on the target, which is what makes an upgrade viable on a small hosted instance. **The gap it must close is verification** — `update.sh` performs none: it checks the file exists and has a docker-compose.yml at its root, then extracts. Correct when a person puts the tarball in ./update/, since that person is the trust decision; unusable when a web request selects it. A detached signature checked before extraction is the missing piece, and it belongs in `update.sh` behind a setting rather than only in the agent, so the manual path gets it too. Note also that step 4 stops the application services: the UI that requested the upgrade goes away mid-run, which is the reason the request has to be a persisted intent row rather than a synchronous call. **Measured on a real deployment 2026-08-29**: the outage is ~17 s and presents as `502` from Caddy rather than a refused connection, because the TLS terminator stays up while only the backend cycles — short enough for a polling client to ride out and reconnect, so the UI can show progress rather than lose the session. No instability followed, so a health gate can accept the first `200` without a settling period. **And the failure path is not `--rollback` alone**: rollback restores the database and `.env`, and warns that it restores neither code nor image. An update carrying migrations that then failed its health check would be rolled back onto new code with an old schema — worse than the failure being recovered from — so the agent must retain the *previous archive* and re-apply it, not just the snapshot. Rollback is otherwise verified working, including its refusal to touch anything when a snapshot is incomplete, and a single-transaction restore that leaves the database unchanged on failure. Superuser plus a second factor to request, rate limited, security-logged; no arbitrary version input, only the pinned channel's latest, or the upgrade endpoint becomes a downgrade-to-vulnerable endpoint. Confirm first that nobody has shell in the target case: if the hosting service maintains the instance, upgrades may simply be their job)
- User — account and group management UI (the API at `/api/v1/user/admin/` shipped 2026-08-25 as the first half of replacing the Django admin and the SPA has no client for it; scope, the decisions the API already enforces, and the traps a client will hit are in the entry)

### 🟡 Medium
- Infra — a deployment-local compose overlay the deploy scripts honour, for pins and tweaks that must survive an update (explicit `-f` flags suppress Docker's automatic `docker-compose.override.yml`, so today there is nowhere for them to live)
- Compute — ship pre-generated lead fields as PWA-cached static files (backend, deploy wiring, service-worker rules and the SPA's fetch all shipped; what remains is confirming the caching behaviour against a deployment that serves the generated tree)
- Viewer — finish the worker settings auto-sync
- Additional getting-started scenarios
- Recordings — contract tests + load-bearing flag for `preservation.write_original` byte integrity
- Shared batched-permission helper + paginated list endpoints (activity, library, annotations)
- Activity — optional periodic integrity-check Celery task
- Activity — close the bulk-operations audit-trail gap (the core closure shipped; the optional opt-in registry and lint follow-on did not)
- Tooling — pre-commit / CI banner when a LOAD-BEARING file is in the diff
- Tooling — additional review-agent workflows for load-bearing features without coverage (PHI sanitisation, object-level permissions, multi-step atomicity, audit-trail tamper-detection)
- Testing — integration-test layer for cross-app contracts
- Testing — container-based integration tests for the bootstrap pipeline (Tier 3)
- Recordings — duplicate-content detection on upload (file_hash collision policy)
- Recordings — share an individual recording after upload (the grant endpoints are read and revoke only; there is no POST to create one)
- Viewer — consume the viewer as a published edition rather than a submodule build (decision 2026-09-03, recorded in the entry; the setup-skeleton half of that entry shipped)
- CI/CD — full-stack pipeline: build → setup scripts → init → unit + integration (consolidates the two Testing entries below; the `stack-smoke` and `bootstrap-fixture-smoke` jobs are the first rung, the compose integration suite and the production-overlay path remain)
- CI/CD — run `vue-tsc` / `vite build` in CI (vitest runs in the `frontend` job; the typecheck and build are blocked on the viewer's types being a declared dependency — see the viewer distribution decision under the manifest-driven setup entry)
- Setup — single discoverable entrypoint (Makefile / justfile) for setup and management tasks
- Setup — `manage.py doctor` preflight (env completeness, Docker version, free ports, DB / Redis reachability)
- Federation — tailnet network layer + management-command flow (slice one — `federation/services.py` extraction + peer/grant/`check_peer` commands + grant-renew — in progress; network layer prefers Route B dual userspace roles; grant-request protocol and SSRF host-suffix carve-out deferred; see [engineering note](docs/engineering-notes/federation-tailscale.md))
- Privacy — purge-time audit tombstoning for patient-side snapshots (Recording / annotation content persists in `ObjectChangeLog` after a recording purge; extend the erasure engine to tombstone the purged object's audit rows after a retention window — the subject-erasure tombstone mechanics in [activity/erasure.py](activity/erasure.py) are the building block)
- Privacy — *self-service* Art. 15 export endpoint (the operator command shipped 2026-08-28; `user.export.export_user_data` is already separate from the command so an endpoint would reuse it) (compile every row keyed to a user id — recordings, annotations, media, access rights, subscriptions, identity, audit references — into a portable bundle; complements the erase_user inverse)
- Recordings — retention-window enforcement (the privacy notice states a ten-year retention for recordings and nothing implements it; `purge_deleted_recordings` only reaps rows already soft-deleted, so today the ten years elapse and the recording stays. Needs an age-based sweep keyed to a configurable window, an operator confirmation step rather than a silent mass delete, and a decision on whether the clock runs from upload or from the acquisition date the header no longer carries)
- Recordings — transfer ownership, or release to an anonymised pool, before account erasure (`Recording.author` is `on_delete=CASCADE` and the reverse `GenericRelation`s cascade with it, so erasing an account destroys the uploader's recordings **and every annotation on them, including other people's**. That is the intended policy for recordings whose original annotations are in the database, but it leaves no option for data colleagues depend on. The notice discloses the consequence in the meantime)
- Infrastructure — revisit Borg encryption mode once passphrase custody exists (`bootstrap.sh` initialises `--encryption repokey`, which stores the key inside the repository wrapped by `BORG_PASSPHRASE`. With an off-site evidence host holding that repository, a copy plus the passphrase is full read access. `keyfile` mode keeps the key on the application host and leaves the remote repository undecryptable on its own, closing exactly the case an off-premises evidence host opens. It is not a free swap: losing the keyfile loses the backups even with the passphrase, and it turns one escrowed secret into two — so it follows the passphrase-custody arrangement in [docs/operations.md](docs/operations.md), never precedes it. Revisit deliberately rather than switching a flag)
- Annotations — a server-side vocabulary allowlist for `Code` (**mechanism shipped 2026-09-02**: the registry in [annotations/vocabularies.py](annotations/vocabularies.py) validates API writes per `standard`, with `ANNOTATION_CODE_STRICT_VOCABULARY` for the closed-set posture — what remains is registering a real vocabulary and the free-text decision below. Original rationale: `annotations.Code` stored `standard` and `value` as free-form fields with no validation, and annotation bodies carry free text, so a project whose data-protection position depends on annotations carrying only a closed set of terms has a convention rather than a control. A teaching project is exactly that case: its position that the platform receives no patient personal data rests on coded annotations being recording-condition markers only — eyes open/closed, photic, hyperventilation, calibration, impedance — with everything clinical added afterwards *on* the platform. A per-project allowlist rejecting out-of-vocabulary writes, plus a decision on whether free-text annotation bodies are permitted at all for such a project, converts that into something describable to a supervisory authority. Same shape as the upload-disable switch below, and for the same reason: a position resting on client behaviour is falsified by one curious user, retroactively, for a patient who never consented to anything)
- Annotations — adopt HED-SCORE as the first external `Code` vocabulary (the allowlist above, generalised: the registry now exists in core; what remains is a `hedscore` provider plugin shipping the vendored CC-BY schema. A teaching project's condition-marker set — manual eye closure and opening, photic stimulation, hyperventilation graded by effort — is one branch of that schema, so restricting the project to it converts the position into something rejected at the API. Also supplies Phase 4 of the channel-de-identification plan with a target vocabulary, though tagging alone does not stop `Event.name` serving the vendor string. Design in [engineering note](docs/engineering-notes/hed-score-integration.md))
- Recordings — a deployment switch that disables upload entirely (browser-only deployments — a project that opens recordings from the user's own machine and processes them in the browser — exist; nothing derived from a local file reaches the server, which is what lets their privacy notice omit Art. 14 entirely and avoid an Art. 9(2) condition. But the upload endpoint is still mounted and reachable by any authenticated user, so that position rests on convention rather than on a control — one curious tester falsifies it, and the notice becomes untrue retroactively for a person who never consented to anything. A `RECORDINGS_UPLOAD_ENABLED=False` setting returning 404 from the upload endpoints — 404 rather than 403, so a disabled deployment does not advertise the route — plus hiding the frontend upload surface, would let such a deployment state that recordings *cannot* be received rather than asking that they not be sent. Worth a contract test, since the failure mode is an endpoint quietly staying reachable)
- Privacy — Art. 13/14 privacy-notice frontend surface (the template shipped 2026-08-26 as [docs/privacy-notice-template.md](docs/privacy-notice-template.md); what remains is serving it at the point of collection — nothing in the application links or shows it today — tracked in [docs/gdpr-compliance.md](docs/gdpr-compliance.md#known-gaps))
- Privacy — annotation event vocabulary still fingerprints the acquisition software (**now blocking for a teaching project**, which was not the case when this was deferred: that project's position is that the platform receives no patient personal data, and a converter-derived event string that identifies the acquisition software — and through it the acquiring laboratory — is site metadata surviving inside the annotations that position depends on. Phase 4 of the channel-de-identification plan; converter-derived event ID strings and taxonomies are near-signatures of the vendor software. The translation mechanism — a per-vendor mapping registry beside the Phase 0 validator, plus the blob-vs-`Event`-row storage decision — is designed in [hed-score-integration.md §8](docs/engineering-notes/hed-score-integration.md); the shipped registry needs no change to accommodate it. Phases 1, 1b, 2 and 3 — ingest-time channel-block cleaning with author-private `source_*` capture, montage-shape assessment, federation-middleware parity, and canonical channel order — are implemented; see [engineering note](docs/engineering-notes/channel-deidentification-plan.md))
- Federation — erasure path for remote data subjects (a `(peer, remote_user_id)` sweep across `AccessRight` grants and `FederationAuditLog` rows for Art. 17 requests arriving from a peer's user)
- Federation — media files cross to peers raw (no de-identification concept exists for media; block media types from federated grants unless explicitly acknowledged, and strip image metadata longer-term)
- Infrastructure — extend the reverse-proxy byte offload to media downloads (recordings shipped; media still streams through a gunicorn thread)
- Recordings — retire `preserve_annotations` in favour of permissioned annotation publication (the flag puts a second, permission-free copy of clinical text in the stored file, which a project's raw-serving grants hand out verbatim; the annotations are already in the database behind permission checks)
- User — drop `django.contrib.admin` from INSTALLED_APPS (the admin surface is retired; what remains is the `django_admin_log` table, which sits outside the hash chain and outside `erase_subject`, plus the static tree and the `messages` dependency the app keeps load-bearing)
- Evidence host — support more than one instance sending to it (a separate evidence host per deployment is largely redundant, but the current one is single-tenant in the half that matters. Borg is nearly there: a second instance needs only its own key, its own `--restrict-to-repository` path and its own forced-command line, and each repository is encrypted with its own passphrase so the host holds ciphertext it cannot read. Loki is not: there is one shared `shipper` credential, so revoking one instance revokes all; the `host` label that distinguishes instances is asserted by the client, so any instance holding the credential can forge or suppress another's stream, including its dead-man's absence rule; and the rules hardcode `host="epicurrents-app"`. Real multi-tenancy means a credential per instance with Caddy injecting `X-Scope-OrgID`, absence rules generated from a roster of expected instances rather than one literal, and per-instance Alertmanager routing. Storage needs a quota per instance too, or one runaway deployment fills the disk for everyone. **And note the non-technical half:** hosting another controller's security logs makes the evidence host operator their processor, requiring an agreement — logs carry IP addresses and actor ids in the clear, unlike the backups)
- Security — detect unauthorised direct database / filesystem access (the audit trail only sees writes that went through Django; a direct `UPDATE` or a shell on the recordings volume leaves the hash chain valid but no longer descriptive — three architectural moves (model-gated audit registration, an append-only evidence-host topology, off-host chain-head anchoring with a federation-countersigning option) plus eight in-stack phases from an audit-coverage check through DB role separation, Postgres connection/DDL logging, a stored-file sweep, a honeytoken, and stack hardening, and the host controls that cannot live in compose; see [engineering note](docs/engineering-notes/intrusion-detection-design.md))


### 🟢 Low
- Context menus for annotations and signal selections
- Annotations — wire share-token attribution end-to-end
- Annotations — seed the `AnnotationKind` token vocabulary (possibly project-specific)
- Viewer annotation persistence through the platform API
- `scoped-event-log` — JSON export / import + hide empty context line
- Viewer — Migrate epicurrents packages from webpack to Vite
- Viewer — defer resource open until activation setup completes (replace the re-dispatch band-aid)
- Viewer — additional EEG trend types (BSR/iBI, rEEG)
- Viewer — click-to-seek inside trend strips
- Viewer — live cascade montage mode for streaming single-channel recordings (EMG, NCS, …)
- Viewer — phase 2/3 of the cascade montage: per-row time axis, annotation routing, cursor-to-time inverse mapping, navigator viewport sized to `rowCount * pageLength`
- Viewer — cascade montage worker enablement (filters, derivations); phase 1 ships worker-bypassed reading raw source — see [cascade entry](#-viewer--cascade-montage-worker-enablement)
- Document the iOS PWA install flow for push notifications
- Borgmatic — README for the backup/restore workflow (the security pass is done; this is the operator walkthrough)
- Frontend README review
- Activity — split `audit.py` into focused submodules
- Testing — federation integration test harness (mock peer + two-instance smoke suite)
- Activity — document and auto-handle rollback ordering for cascade deletions
- Compute — generalise channel-type assumption beyond EEG (PSG / MEG)
- Compute — check whether MNE community has already discussed the sphere-centre singularity
- Compute — MNI152 cortical surface mesh for 3-D source-localisation view
- Compute — EEG processing-tool expansion (SpikeNet + ML models, preprocessing / qEEG) — survey + phased plan in engineering notes
- Normalise hex hash convention to lowercase across recordings + annotations
- Infrastructure — make the web image's entrypoint skip the postgres wait for DB-less commands
- Infrastructure — revert to single `data` volume + `volume.subpath:` once Podman's Docker-API translates it
- Recordings — recoverable ingest state machine so a dropped Celery task is re-derivable from `Recording.status` (the broker now persists with an `everysec` fsync window; this closes the remaining second and also covers a worker dying mid-task)
- Tooling — schedule periodic `phi-exposure` full-surface sweeps (prompt keyword `full-surface`); the per-commit gate stays diff-scoped
- Compute — move lead-field computation into a Celery task (bounded synchronous computation shipped as the interim guard)
- Dependencies — migrate `psycopg2-binary` to psycopg 3 (or source-built psycopg2) so libpq/OpenSSL fixes reach production
- Testing — Playwright end-to-end suite against the running stack (login → upload → view)
- Documentation — root `CONTRIBUTING.md` pointer to `docs/developing.md`
- Recordings — `import_recordings` logs operator source paths that can embed patient identifiers; log the job-file id / stored_name instead
- Recordings — retention pruning for completed / aborted `ImportJob` rows (source paths + error text currently persist indefinitely)
- Federation — per-peer opaque pseudonym for the outbound JWT `sub` (the local integer pk is stable across every peer, so peers can correlate a user; also disclose the flow in the privacy notice)
- User — single-bundle Art. 20 export convenience (per-type `/mine` feeds + recording downloads already exist; a one-call bundle is polish)

### 🔍 Investigation
- Investigate the project loader ignoring `.env` (host runs and the CI drift gate silently load no project)
- Investigate a native notification companion app
- Investigate activity / changelog API user-data isolation (GDPR Art. 15 inverse)
- Investigate type-checking the frontend test files (nothing does today)
- Investigate Norwegian BankID as an additional authentication method
- Investigate Windows integrated login (Edge / Kerberos) as an additional authentication method

---

## 🟡 Viewer — finish the worker settings auto-sync

**The three-step fix below is superseded and must not be followed as written.** `syncSettings` now carries an `@deprecated` block saying "Do not call this function", and the canonical pattern is shipping `SETTINGS._CLONABLE` in the `setup-worker` commission, as `MontageService` and `TrendService` do. The problem this entry describes is still real — nothing relays a settings change to a worker after setup — so what is needed is a live-change relay on top of `_CLONABLE`, not the revival of `syncSettings`. The steps are kept below as a record of the original diagnosis.

The design exists in `epicurrents/core/src/util/worker.ts` (`syncSettings` helper) and `epicurrents/core/src/assets/service/GenericService.ts` (the `update-settings` action handler that registers main-thread property-change listeners and posts changes back to the worker). The intent: each worker subscribes to the fields it cares about, and any subsequent `coreApp.configure()` / `SettingsDialog` change on the main thread automatically flows to the worker.

Three gaps stop it from functioning today:

**1. No worker calls `syncSettings(map, postMessage)` to initiate subscription.** `syncSettings` is exported but never imported anywhere outside its own definition. Without that initial call, `GenericService.handleMessage` never receives a `{ action: 'update-settings', fields: [...] }` from a worker, so it never registers the property-update listeners that would send changes back.

**2. `syncSettings`'s internal model doesn't match how workers actually read settings.** The helper writes received `{ name, value }` pairs into a `Map<string, value>` supplied by the caller — but workers read their settings as `this.SETTINGS.app.maxLoadCacheSize` etc., from the bundled `core/config/Settings.ts` module-level singleton. Even if a worker subscribed, the resulting Map would be a parallel data structure that nothing reads from.

**3. The format worker's hand-rolled `update-settings` action handler is incompatible with what `GenericService` sends.** `edf.worker.ts` expects `data.settings = { ...whole object... }`; `GenericService` line 287–298 sends `{ field, value }` per individual change. The newer `edf.worker.ts` handler I added for the rolling-cache rollout (`Object.assign(SETTINGS.app, data.settingsApp)`) explicitly snapshots the whole app settings at `setup-worker` time — that's currently the only path that actually transports settings to the EDF worker, and it only fires once at startup.

### Fix plan

A self-contained cleanup PR, separate from the rolling-cache work:

1. **Reconcile `syncSettings`'s model with `SETTINGS`.** Rewrite it to update the worker's `SETTINGS` module singleton via dotted-path assignment (`field = 'app.maxLoadCacheSize'` → `SETTINGS.app.maxLoadCacheSize = value`), or — slightly cleaner — accept a target object and mutate it. The Map model is the wrong abstraction given the established way workers read settings.

2. **Call `syncSettings(SETTINGS, postMessage)` at worker boot** in every format/service worker (`edf.worker.ts`, `montage.worker.ts`, `pyodide.worker.ts`, …). One line each.

3. **Replace each worker's hand-rolled `update-settings` handler** with `syncSettings(SETTINGS, message)`. Removes the divergent message-shape expectations.

4. **Decide what to do about the initial state.** `addPropertyUpdateHandler` only fires on subsequent changes, not on registration. Three options:
   - Keep the explicit `settingsApp` snapshot at `setup-worker` time as the "initial state" mechanism (smallest delta, but leaves two parallel sync paths).
   - Make `addPropertyUpdateHandler` fire once on registration with the current value (cleanest, but might trigger unexpected handlers elsewhere — audit needed).
   - Have the worker's "subscribe me to fields X" message return the current values immediately as the response (custom protocol, no global behaviour change).

Recommended: option 3 — it keeps the auto-sync self-contained without changing the broader property-handler semantics.

Once finished, the rolling-cache snapshot in `EegService.setupWorker` (which manually packs `dataBlockDuration`, `maxLoadCacheSize`, etc. into the `setup-worker` commission payload) can be deleted.

---

## 🟡 Viewer — consume the viewer as a published edition rather than a submodule build

**The setup-skeleton half of this entry shipped; what remains is the distribution decision at the end.** The problem it solved was that the UMD lib build entered through a demo-wired setup where every module registered unconditionally, so a deployment needing only EEG, markdown and PDF still shipped DICOM and Pyodide. That entry point is gone: `setups/index.ts` in the viewer's interface package is the empty-default framework setup, registering nothing and taking a consumer callback, with `full.example.ts` as the all-in reference. The builder went further than the sketch below and trims the registry at build time rather than reading a runtime manifest — `vite.config.lib.ts` rewrites the setup registry to import only the active profile's registrars. The platform-side switch landed with it: [frontend/src/viewer/base.ts](frontend/src/viewer/base.ts) passes its own module registration into the framework app.

The sections below are kept because the reasoning still explains the shape of what shipped.

### Two shapes considered

| Approach | Boundary | Notes |
|---|---|---|
| Platform overrides the setup entirely | The platform calls `new Epicurrents()` directly and registers each module / importer itself. | Forces the platform to import every reader package and manage worker URLs (PdfImporter mutates `PdfWorkerSubstitute.source` on construction; the Vite `?raw` + `inlineWorker` glue lives in `setups/default.ts`). Duplicates worker-bundle plumbing across two build systems. |
| Manifest-driven skeleton in the viewer | A new `setups/skeleton.ts` exports a `createEpicurrentsApp(config)` that reads a list of module specs from `config.activeModules` (or a richer manifest) and registers only those. The standalone demo keeps `setups/default.ts` for its all-in build. | Keeps the worker-bundle / `inlineWorker` glue on the viewer side where the build already knows about it. Platform passes a manifest via `plugin.extraSetup` (e.g. `activeModules: ['eeg', 'doc-md', 'doc-pdf']`). Build command becomes `SETUP=setups/skeleton npm run build`. |

Recommended path is the second: keep the worker bundles and the Vite-specific `?raw` imports inside the viewer monorepo; let the consumer declare what it wants. The first option spreads the worker contract across two build pipelines and loses the cleanly bounded responsibility the viewer has today.

### Sketch

```ts
// frontend/viewer/interface/src/setups/skeleton.ts
export const createEpicurrentsApp = async (config?: ApplicationInterfaceConfig) => {
    Object.assign(SETUP, config)
    const coreApp = new Epicurrents()
    // …common pre-launch configure() block…
    for (const spec of SETUP.activeModules) {
        const module = MODULE_REGISTRY[spec]
        if (!module) {
            Log.warn(`Unknown module ${spec} requested`, 'setups/skeleton')
            continue
        }
        await module.register(coreApp, USE_SAB)
    }
    // …interface registration + launch()…
}
```

Each `MODULE_REGISTRY[name]` is a thin record holding the importer constructor and the `registerStudyImporter` calls — essentially the per-module block currently inlined in `setups/default.ts`, extracted into named units (`registerEegEdf`, `registerDocMd`, `registerDocPdf`, …). The default setup becomes a one-line list of every key in the registry; the skeleton takes only what the caller passed.

### Platform-side switch

After the skeleton lands:

1. Pass `SETUP=setups/skeleton` in `scripts/rebuild-frontend.sh`.
2. Platform's `ViewerPlugin.extraSetup` declares the manifest: `{ activeModules: ['eeg', 'doc-md', 'doc-pdf'], usePyodide: false }`.
3. Pyodide-dependent features (topomap, Python-side analysis) stay opt-in per deployment via the same `extraSetup`.

### Not in scope

- Refactoring the standalone `setups/default.ts`. It stays as-is; the demo build keeps loading everything.
- Trying to lazy-load modules at runtime. The manifest is build-time — we want the bundle to drop unused code entirely, not defer it.

### Refinement (2026-06-13): empty-default framework setup + publish to NPM

Clarifying the recommended second option above. The framework setup ships with no modules registered — just the interface — and the consumer registers the resource modules, their importers, and the services it needs through a small registration API (the parts the default setup currently hard-codes). This is a contained change, not a runtime-lazy-loading rewrite: strip the default setup down to the common pre-launch block, and expose the module registry to the caller. The build-time-vs-runtime tradeoff in the original "Not in scope" note is unaffected — the consumer still decides what to import, the setup just stops hard-coding it.

Concrete naming (decided): the framework setup becomes the default export at `setups/index`, and the all-in `setups/default` is renamed `setups/full` (the standalone demo build). The full implementation plan lives in the viewer repo's roadmap.

Two payoffs beyond per-deployment bundle trimming:

1. **Publish the framework (and modules) to NPM.** The consumer installs a prebuilt, versioned package instead of running the clone-and-build monorepo setup, which fetches each workspace package from its own repo and builds them in dependency order — the step that makes a clean-room / CI frontend build fragile (surfaced by the bootstrap-fixture work; the standalone smoke deliberately does not build the frontend for this reason). This is the platform-facing, hermetic-build enabler. Concretely, as of 2026-08-29 a fresh clone **cannot build the frontend at all**: the `frontend-build` service runs `npm ci` inside the viewer submodule but never `npm run setup`, which is what creates the `epicurrents/*` workspaces `npm ci` is then meant to install — and `node:20-bookworm-slim` carries no `git` for setup to shell out to. Both are fixable in place and neither is worth fixing, because publication removes the clone step rather than repairing it. Until then a deploy host must receive prebuilt bundles, which is what a distribution does.
2. **A zero-module build is trivial.** The empty framework with no modules is exactly what a bootstrap-smoke or a minimal embed needs — buildable without any module setup at all.

Both are viewer-repo work; the platform's interest is the NPM-published build.

### Platform decision (2026-09-03): a published edition, not a submodule build

The platform will not pin the builder as a submodule long-term. The intended supply of the viewer distributable is a builder-generated edition release — the builder's `<edition>-v<major>.<minor>.<patch>` tags attach the built edition (`epicurrents-lib.*` plus the standalone `index.html`) and its reproducibility manifest to a GitHub release — fetched into `frontend/viewer-dist/` at deploy by a script that pins edition, version and checksum in the tree, and later the npm packages above. The `frontend/viewer` checkout then becomes the development route for building the viewer from source, documented as such rather than required. Until that lands the submodule pin stands and the deploy host builds the viewer from it.

What has to move first is the SPA's build-time coupling to the checkout, which is small and known: the core types imported through the `#epicurrents/core/dist/types` alias (`@epicurrents/core` is on npm and exports every name the SPA uses), the interface's toast helper and `ToastStack` component imported from `viewer/interface/src` together with its `announce` augmentation (`@epicurrents/interface` is not published; the platform was the original home of that code and can carry a copy), and the `scoped-event-log` alias (published at the same version the checkout carries). The prototype base viewer build under `src/viewer/` bundles the interface from source and stays on the checkout route. No edition release exists yet; tagging the first one is the prerequisite for the fetch script to have a target.

---

## 🟢 Viewer — defer resource open until activation setup completes (replace the re-dispatch band-aid)

`setActiveResource` ([runtime/index.ts](frontend/viewer/epicurrents/core/src/runtime/index.ts)) flips `resource.isActive = true` with no `isReady` check, and the loader starts `prepare()` without awaiting it ([core/index.ts](frontend/viewer/epicurrents/core/src/index.ts)). So a recording — and its `EegViewer` — can be activated *before* it is prepared and its montages / signal cache exist. The ACTIVATE handler's one-time setup is gated on `state === 'ready'`, so it was silently skipped and the recording opened permanently stuck on "Setting up montage 1 of N" with stacked channel labels and no cached data, recovering only on reopen.

**Interim fix shipped (band-aid).** `EegRecording.prepare()` applies the default setups before flipping `state` to `'ready'`, then — if the recording was already activated — **re-dispatches the ACTIVATE event** so the deferred setup runs. This stops the permanent stuck state: the unlucky opens now show a brief "Setting up montage…" flash and recover within ~1 s. Two smells: re-dispatching ACTIVATE **fires the post-activation event an extra time**, so every ACTIVATE listener runs twice (only the guarded setup handler today, but fragile for any future listener); and delaying `'ready'` widened the pre-ready window, so the flash now appears *more often* than the old stuck state, just self-healing.

**Proper fix.** Defer the *open* until the recording's activation setup is complete, instead of mounting the viewer early and self-healing by re-firing a lifecycle event. Distinguish prepared (`state === 'ready'`), activated, and **display-ready** (montages built + first cache window available); the interface shows one unified loading state until display-ready and only then mounts the modality viewer — no internal "Setting up montage" flash and no double-fired event. Concretely: gate `setActiveResource` (or the interface's resource-view switch) on readiness and surface an explicit "setup complete" signal the viewer awaits, or have the activation path await `prepare()` + the ACTIVATE setup before committing the active resource.

Once the deferral lands, remove the `prepare()` re-dispatch + its `Log.debug` in `EegRecording`. `AccRecording` and the other modules share the same early-activation exposure and benefit from the same fix.

Rare in practice — it only triggers when an item is auto-opened at the same moment it is loaded (the first item in a dataset on page load), and even then only under specific timing; the interim fix recovers within ~1 s. Hence 🟢. But the band-aid (the double-fired event) should not outlive a proper deferral.

---

## 🟡 Additional getting-started scenarios

[docs/getting-started.md](docs/getting-started.md) currently covers deployment, dev clone, and creating a new project plugin's backend layer. Several more scenarios — for plugin authors and operators — are real friction points today and worth documenting as their own walkthroughs.

### Scaffolding a frontend module for a new project

`frontend/src/projects/<name>/` is the entry point for a project's frontend customisation — adding routes, nav links, icons, viewer setup hooks, and registering the project against the `ViewerPlugin` contract. Currently documented piecemeal in [`frontend/README.md`](frontend/README.md) and reverse-engineerable from the existing project sources. A worked walkthrough covering the directory structure, the `index.ts` plugin contract, route / navLink / icon registration, and the build-time tree-shaking via `VITE_PROJECT` would let plugin authors scaffold the frontend without reading existing project code.

### Listening to and reacting to events from the EventBus

`scoped-event-bus` ([`frontend/viewer/util/scoped-event-bus/`](frontend/viewer/util/scoped-event-bus/)) is the cross-component communication primitive in the viewer. The pattern of "project listens to viewer events and reacts" — e.g. syncing user changes back to the backend on annotation-change events — is a recurring need but isn't documented as a general recipe. A scenarios doc would cover: subscribing to property-change events, annotation lifecycle events, recording open / close events, and debouncing strategies to avoid noisy backend traffic.

### Verifying that user annotations are being written to the database

After implementing a project annotation flow, a contributor wants to confirm annotations are actually persisting. Diagnostic recipe: inspect `ObjectChangeLog` entries via the activity API or shell; query the relevant `Annotation` / `Event` / `Interruption` / `Label` table directly; use the annotations list endpoint. This belongs in [docs/operations.md](docs/operations.md) under "verifying X works" or as a [docs/troubleshooting.md](docs/troubleshooting.md) entry for "I expected annotations and they aren't there".

### Setting up borgmatic backups for a new deployment

Once a deployment is running, configuring backups properly is the next critical step but currently has no walkthrough. The pieces are scattered across [`.env.example`](.env.example) (the `BORG_*` env vars), [`scripts/backup.sh`](scripts/backup.sh) (the trigger), [`scripts/restore.sh`](scripts/restore.sh) (the restore workflow), and the borgmatic config files in [`borgmatic/`](borgmatic/) themselves. A walkthrough would cover: setting `BORG_PASSPHRASE` securely and recording it where it survives a server loss; initialising the local repo with `borg init --encryption=repokey`; setting up remote SSH backup (key generation, `BORG_SSH_KEY_PATH`, `BORG_REMOTE_REPO`); running the first backup; verifying with `scripts/backup.sh --list`; and rehearsing a restore so the operator knows the path works before they actually need it. Closely related to the borgmatic README entry below — the README is the reference doc, this is the task walkthrough.

The four scenarios are independent — pick up individually as authors / operators hit the friction.

---

## 🟡 Shared batched-permission helper + paginated list endpoints (activity, library, annotations)

Both the activity changelog list and the library collection / tag item lists currently run a per-row read-permission check during listing — `can_rollback_change` for each `ObjectChangeLog` in [activity/api/v1/ninja.py::list_change_logs](activity/api/v1/ninja.py), and `_user_can_read_item` (called by `_filter_readable`) for each `CollectionItem` / tag item in `library/api/v1/ninja.py`. Each call does its own `AccessRight` lookup, so a page of N rows costs O(N) queries.

The activity endpoint additionally has no cursor or offset — it fetches the top 1000 rows by `created_at`, filters them in Python, and slices to `limit`. A user whose readable rows lie deeper than the top-1000 window will see an artificially short result with no indication that more exists.

The library endpoints have similar per-item N+1 behaviour documented in [library/README.md](library/README.md#gotchas) with a recommended switch to batched checks; this item is the unified treatment.

The annotations list endpoints are a shape-different third case: they don't have the N+1 (a single `can_read_object` check on the parent target gates the whole result set), so the *batched helper* doesn't strictly apply. But they share the **offset-pagination concern**. The defaults were set high (`limit=500`, max `1000`) on the assumption that practical annotation counts on a recording fit in one page; once that assumption breaks — e.g. a long-running clinical EEG with thousands of events, or a project that uses Codes liberally and inflates per-event payload — the same cursor-on-`(created_at, id)` migration applies. The list endpoints to convert: `/annotations/`, `/annotations/mine`, `/events/`, `/events/mine`, `/interruptions/`, `/interruptions/mine`, `/labels/`, `/labels/mine` ([annotations/api/v1/ninja.py](annotations/api/v1/ninja.py)). Same response-shape change (`{items, next_cursor}`) and same frontend coordination required.

**Proposed shape — single helper, used from both apps.**

```python
# epicurrents/permissions.py
def batched_readable_pks(
    user,
    content_type,
    object_ids: Iterable[str],
    *,
    permission: str = "can_read",
) -> set[str]:
    """Return the subset of object_ids the user has the named permission on."""
```

- One `AccessRight.objects.filter(content_type=..., object_id__in=..., <permission>=True)` over the page.
- Returns a `set[str]` for O(1) lookup per row when iterating the page.
- Caller decides which permission to gate on (`can_read` for library listings, `can_write` for the activity changelog).
- Extension protocol (see [epicurrents/README.md](epicurrents/README.md#permissions) "register_read_permission_extension") still applies for the per-row decision — the helper covers the direct-row path; extensions handle the rest.

**Pagination shape.**

Cursor over `(created_at, id)` (descending). Query param `?before=<iso-timestamp>:<id>`. The endpoint returns `{ "items": [...], "next_cursor": "..." | null }`. Cheap deep-paging, stable under concurrent writes; no offset arithmetic.

**Migration order.**

1. Add `batched_readable_pks` in `epicurrents/permissions.py`. Test it against direct AccessRight rows + extension-granted rows.
2. Switch [library/api/v1/ninja.py](library/api/v1/ninja.py) `_filter_readable` to the helper. Existing tests cover the behaviour; no API contract change.
3. Switch [activity/api/v1/ninja.py](activity/api/v1/ninja.py) `list_change_logs` to the helper *and* add cursor pagination. Response shape changes from `list[ChangeLogOut]` to `{items, next_cursor}` — coordinated frontend change required.
4. Apply the helper anywhere else the pattern shows up; grep for `for ... in queryset[:1000]` or per-row `AccessRight` calls.
5. Convert the annotations list endpoints to the same cursor-pagination response shape, *without* the batched helper (single-target read check already short-circuits the N+1). Same frontend-coordination story as step 3.

**Tests to add (new):** helper returns only matching pks; respects the `permission` argument; honours direct-row `apply_middleware=False` correctly (does *not* call extensions for grants that have an explicit AccessRight row). Activity / library tests get rewritten to assert pagination boundaries (last cursor returns empty page) and per-row visibility.

---

## 🟡 Recordings — share an individual recording after upload

Recording grants are created at upload and by `import_recordings`, and `GET` / `DELETE /recordings/api/v1/{hash}/access/` now list and revoke them. What is still missing is the third verb: granting access to a recording that already exists. Today the only way to share one after the fact is to put it in a collection or dataset and share that, which is disproportionate when the intent is "let this one colleague see this one recording".

The endpoint is a `POST` on the same path, mirroring `POST /collections/{id}/access/`: same `GrantAccessIn` shape, same owner-or-`can_share` gate, same `AccessRightOut` response. Two things need care that the collection version does not face as sharply:

- **`apply_middleware` is the de-identification flag**, and on a recording it decides whether the grantee is served the anonymised bytes or the original file. The default must be the safe one, and the field must be explicit in the UI rather than a checkbox nobody reads. See [recordings/README.md → Serving](recordings/README.md#serving-and-the-middleware-pipeline).
- **The `can_share` cap.** A grantee sharing onward must not be able to grant more than they hold — the same cap `can_read_via_collection` enforces in [library/permissions.py](library/permissions.py).

Once this exists, the revoke endpoint's refusal to delete the author's own self-grant could in principle be relaxed, since the grant would become recreatable. It should stay anyway: an author locking themselves out of their own recording is a bad outcome whether or not it is recoverable, and the 409 costs nothing.

### Not in scope

`PATCH` on an existing grant. Editing a grant's flags in place is a worse expression of every intent it could serve — narrowing access reads more clearly as revoke-and-regrant, and the audit trail says what happened rather than that some bits changed. Decided 2026-08-25.

---

## 🔴 User — account and group management UI

The API half of the admin replacement shipped 2026-08-25; the client half was never built. Thirteen endpoints stand at `/api/v1/user/admin/` — account list, read, create, edit, set-password and second-factor reset, group list, create, rename and delete, membership from either direction, and the project roles a deployment defines — documented in [user/README.md](user/README.md#account-administration) and covered by tests. Nothing in the SPA calls them: no view, no route, no module in [frontend/src/api/](frontend/src/api/), no nav link. Account administration is driven by hand against the API, with a session cookie and a CSRF token, or through management commands on the host.

**Why it went untracked.** The plan entry that carried this scope — *User — replace the Django admin with an in-app account surface, then retire it* — was closed when the API landed, and the UI half went with it. What stayed is the separate [`INSTALLED_APPS` cleanup](#-user--drop-djangocontribadmin-from-installed_apps), whose text says account management "lives at" the API, and the matching *Done* line in [docs/pre-launch-checklist.md](docs/pre-launch-checklist.md). Both are true about the backend and both read as finished, so nothing marked the remaining half as remaining.

**What its absence costs.** Creating an ordinary account has no operator-facing path at all: `createadmin` makes a superuser and only when none exists, `createsuperuser` makes a superuser, and everything else is the API. The pre-launch checklist's own second-factor step asks the operator to check `is_2fa_enabled` across the roster after provisioning — `GET /admin/accounts` returns that field on every row and nothing renders it. What is left is a shell on the host or a hand-built request, and a deployment maintained by a hosting service has neither — the premise of *Platform — operator-triggered upgrade for instances without shell access* in the High list above. None of this is a security gap the way the admin was — it is the audited replacement being unreachable from the application it belongs to.

### Scope

| View | Holds |
|---|---|
| Account roster | Search over username, name and email (`q`), `limit` / `offset` paging, inactive accounts included and marked as such, `is_2fa_enabled` per row. |
| Account detail | The editable fields — email, first / last name, `is_active`, `is_staff`, `is_superuser` — plus group membership, set-password, and the second-factor reset. Username is not editable and roles are not per-account. |
| Group roster | Groups with `member_count` and `grant_count`, and the project roles each carries. |
| Group detail | Rename, role assignment, and membership from the group side. |

Around them: a client module beside the others in [frontend/src/api/](frontend/src/api/), one route with a nav link, and handlers in [frontend/mocks.ts](frontend/mocks.ts).

### Locked design decisions

Carried from the closed plan entry. The API enforces each already, so the UI's job is to present them, not to re-decide them.

- **No account deletion anywhere in the surface.** [`erase_user`](user/README.md#account-erasure-gdpr-art-17) is the sanctioned path because it unlinks owned recording and media files, which FK cascade never does. There is no delete route to call; the account page points at the command instead of offering a control.
- **Staff sees, superuser writes.** The route gates on `requiresStaff` — [the navigation guard](frontend/src/router/guard.ts) has no superuser branch and needs none, since the roster is staff-readable — and write controls gate inside the component on `authStore.isSuperuser`, per the view-gating rule in AGENTS.md.
- **Refusals are surfaced as the API reports them.** The grant count blocking a group deletion, the last-active-superuser guard, the password validators' joined messages and the duplicate username / group-name conflicts are each decided against server state the client sees stale or not at all. Re-implementing one client-side means two rules to keep in step, and the API's is the one that binds.

### Traps

- **A group's roles are a partial map, and the danger is the padding, not the omission.** `PATCH /admin/groups/{id}` leaves an absent key untouched and treats an explicit `null` as "clear this role". Omitting a provider is therefore safe by construction — a client that has never heard of a project's role cannot destroy it — while a form that submits a complete map padded with `null` for every key it did not render clears exactly those roles. Send the keys the form rendered and nothing else.
- **Setting a password deliberately does not end the account's sessions.** The copy must not imply that it does; deactivation is the control for a suspected compromise, and that does flush them.
- **`AccountOut` carries `email` where `UserSearchOut` withholds it.** It is a staff-gated payload and must not reach a component that renders outside the staff gate.
- **The roster returns a bare list with no total.** Paging can show "there are more" but not "N of M" until the endpoint grows an envelope; that is the same shape as [paginated list endpoints](#-shared-batched-permission-helper--paginated-list-endpoints-activity-library-annotations) and should converge with it rather than growing a private variant.

### Not in scope

Retiring `django.contrib.admin` from `INSTALLED_APPS`, which is [its own entry](#-user--drop-djangocontribadmin-from-installed_apps) and independent of this one — the admin surface is already gone either way.

---

## 🟡 User — drop `django.contrib.admin` from INSTALLED_APPS

The admin surface is gone: the `admin/` mount, the three `admin.py` registrations and the `admin/base_site.html` banner override were removed, and [epicurrents/tests/test_admin_is_retired.py](epicurrents/tests/test_admin_is_retired.py) holds the line. Account and group management lives at `/api/v1/user/admin/`, where the audit trail, the CSRF chokepoint and the de-identification rules apply by construction — though the SPA has no client for it yet, tracked under 🔴 *User — account and group management UI* and independent of this entry. What remains here is the app itself.

Keeping it installed costs three things. `django_admin_log` stays in the database — a record of who changed what, outside the hash chain and unreachable by `erase_subject`, which is the wrong shape for a store that can name a user. `collectstatic` keeps collecting the admin's CSS and JS into the served static tree. And the admin's system checks keep `django.contrib.messages` and two context processors load-bearing, so nothing else can drop them.

The work is small but not a one-liner. `manage.py migrate admin zero` has to run *before* the app leaves `INSTALLED_APPS`, or the table is orphaned rather than dropped, and that ordering is an operator step on every existing deployment rather than a code change. Existing `django_admin_log` rows are history that predates the retirement: decide whether to export them for the record or accept losing them, and say which in the release note. Afterwards, check whether `django.contrib.messages` still has a consumer.

The `debug_mode` context processor already survives with no consumer ([epicurrents/context_processors.py](epicurrents/context_processors.py)); the SPA reads the same signal from `/api/v1/health`. It is three lines and any future template wants exactly it, so it stays until something either uses it or replaces it.

### Not in scope

Removing `createadmin` / `createsuperuser`. Bootstrap must not depend on a UI that requires an account to reach.

## 🟡 Recordings — duplicate-content detection on upload (file_hash collision policy)

The upload endpoint in [recordings/api/v1/ninja.py](recordings/api/v1/ninja.py) computes `file_hash = sha256(file bytes)` during the streaming write, then unconditionally calls `Recording.objects.create(...)`. `Recording.content_hash` is **indexed but not unique**, and `Recording.file_hash` — the field a dedup check would query — carries **no index at all**, so adding one is part of this work rather than a precondition it can assume ([recordings/models.py](recordings/models.py)). So byte-identical uploads produce N distinct Recording rows sharing a `file_hash` — visible to the author as N library entries that are the same recording. The flat-upload path that lands in the same commit cycle as this entry makes the failure mode slightly easier to hit: dragging a folder twice (or once with the new `(N)`-suffix dedup masking the collision) produces silent duplicates.

`content_hash` is **not** the right dedup signal: it's `sha256(file_hash + serialize_instance(recording))`, and the serialised payload includes `original_name`, so a `(2)`-suffixed copy gets a different `content_hash` despite identical bytes. `file_hash` is the right signal.

### Design choices to settle before implementing

1. **Scope of the dedup check.**
   - *Per-author.* Allows two clinicians to legitimately upload the same source file (e.g. shared dataset, separate annotation intents). Catches the accidental-double-drop case in one user's library. Probably the right default.
   - *Per-collection.* Even narrower: same file in the same collection is forbidden, but the same file in different collections of the same author is allowed. Useful if recordings get shared across study folders.
   - *Global.* Strict. Saves storage but blocks legitimate cross-author use cases.
2. **Block vs. warn.**
   - *Hard 409.* Reject the upload; surface the existing recording's hash so the frontend can link to it. Simplest to reason about.
   - *Soft warn.* Allow the upload but include `{ "duplicate_of": "<hash>" }` in the response. Frontend shows a toast / dialog asking the user to confirm. Preserves user agency at the cost of a less crisp contract.
3. **Storage policy.** If we land any form of dedup, do we still write the duplicate bytes to disk? Hard-block on file-system write is the safer side; soft-warn implies writing the second copy.
4. **Federation interaction.** A federated peer pushing a recording that matches a locally-authored file should never block — different ownership, different access semantics. The check is local-uploads-only.

### Suggested minimum viable shape

Per-author, hard 409, no disk write on rejection. Response body: `{"code": "duplicate_content", "duplicate_of": "<hash-prefix>", "stored_name": "<existing>"}`. Frontend surfaces a toast linking to the existing recording. The user can explicitly confirm "upload anyway" with a follow-up POST that carries a `allow_duplicate=true` query param (so the soft-warn case is available behind an explicit flag without complicating the default).

### Not in scope

- Retroactive dedup of existing rows (operator concern; one-shot SQL if it ever matters).
- Content-aware similarity (e.g. matching on signal content with a fingerprint tolerance). Byte-identical only.

Surfaced 2026-06-05 alongside the flat-upload PHI fix; the flat path made the duplicate failure mode more visible because the `(N)`-suffix dedup on names masks the underlying byte-identical collision.

---

## 🟡 Testing — integration-test layer for cross-app contracts

The 2026-05-29 audit-trail middleware bug shipped through a fully unit-tested codebase. The middleware regex passed its own tests; the URL config passed its own tests; the audit signals passed their own tests; the chain between them — middleware → signals → `ObjectChangeLog` — had no test at all. Every unit was correct in isolation; the contract between units was broken silently.

The same shape — three correctly-tested units, untested contract — exists elsewhere:

- **Upload pipeline.** Endpoint accepts a file ✓, Celery task processes it ✓, federation grants get created ✓, but does the full chain end-to-end produce the expected `AccessRight` rows in the expected order? Untested as a whole.
- **Federation read-with-middleware.** Permission check returns `apply_middleware=True` ✓, EDF middleware pipeline applies header sanitization ✓, but does an actual federated download through the actual pipeline produce a sanitized file? Partially covered by `recordings/tests/test_federation.py`; the full chain isn't.
- **Collection share inheritance.** Permission extension is registered ✓, `can_read_via_collection` walks parents ✓, but does an actual list endpoint for collection items honour the inheritance at the response-shape level? Worth confirming.
- **Project plugin lifecycle.** `activate_project` / `deactivate_project` rename tables ✓, migrations apply ✓, but does the full switch leave the running application in a coherent state (no orphan tables, no missing columns, beat schedule rebuilt)?

### Specific contract gaps observed during the middleware audit (2026-05-29)

These are the audit-trail-side failure modes that were untested before the fix, plus a couple that remain untested after. They are listed here so a future integration-test pass picks them up without re-deriving them.

- **`transaction.on_commit()` callbacks run inside the request context** (pinned by `test_on_commit_callback_runs_inside_request_context`). Anything that changes the middleware's context lifetime — moving the reset earlier, wrapping in `ATOMIC_REQUESTS`, etc. — breaks this contract.
- **`MIDDLEWARE` ordering / presence** (pinned by `test_middleware_is_registered` and `test_middleware_runs_after_authentication`). Removing the audit middleware or moving it before `AuthenticationMiddleware` silently degrades every Activity row.
- **Activity-row insert failure logs WARNING and continues** (pinned by `test_activity_creation_failure_does_not_break_request`). Without SIEM integration, this gap could persist invisibly in production. The SIEM ROADMAP item closes the loop.
- **Federated inbound requests carry `actor=None`** (pinned by `test_federated_inbound_request_logs_with_no_actor`). Intentional — federation audit lives in `FederationAuditLog` — but worth keeping pinned so a "fix" doesn't silently start double-attributing.
- **Bulk DML and Celery tasks bypass signals** — documented in `activity/README.md` *Known limitations*. The audited-models opt-in registry (see [activity bulk-operations gap](#-activity--close-the-bulk-operations-audit-trail-gap)) is the long-term fix.
- **`target_object_id` extraction is name-sensitive.** The middleware's response-phase loop matches kwargs named `pk`, `id`, or `*_id`. Endpoints that use `{recording_hash}` or `{object_hash}` paths (most of `/recordings/`, `/annotations/`) leave `target_object_id` empty even though there's an obvious target. Doesn't break the audit trail but makes filtering by target harder. An integration test that exercises a representative endpoint per kwarg style and asserts `target_object_id` is populated would surface the gap.
- **`public_urls.py` routes are not audit-tracked.** Documented in AGENTS.md *Project system*; the next step is a test that demonstrates the gap so the documentation has a backing assertion.
- **Async views are untested** against this middleware shape. Worth checking when the first async view actually lands; until then, asserting hypothetical behaviour adds noise.

### Proposed shape

1. **`tests/integration/` at the repo root** (or `tests_integration/` if name collision with pytest config is an issue). One directory, with subdirectories per chain (`audit_trail/`, `upload/`, `federation_read/`, `share_inheritance/`, `project_lifecycle/`).
2. **Granularity is *contract*, not *function*.** Each test names the chain it guards (`"middleware → signals → ObjectChangeLog when a tracked model is created via API"`) and exercises the full chain end-to-end. If the test passes, the contract holds. If it fails, the contract is broken — even if every component unit test still passes.
3. **Contract tests are themselves load-bearing.** Each new file lands with a `⚠️ LOAD-BEARING — contract test for X` header and an entry in AGENTS.md → *Load-bearing files*.
4. **Pivot order, lowest-risk first:**
   1. Audit-trail chain — already started this session; the path-recognition contract test and the failure-mode tests are the first integration coverage. Move them into `tests/integration/audit_trail/` when the directory exists.
   2. Federation read-with-middleware — the security-sensitive chain.
   3. Upload pipeline — the largest cross-app surface.
   4. Share inheritance, project lifecycle, anything else identified later.

### Why not "convert all tests to integration"

Unit tests remain the right shape for the things they're testing — `compute_audit_hash`, `_API_PATH_RE`, individual permission predicates. They're cheap, they pinpoint regressions quickly, and they catch real bugs in the unit. The pivot isn't "replace unit tests"; it's "add a layer that asserts the contracts between units." Many of the failures we'd otherwise miss are at the seams, and seams are where contract tests live.

### Why yellow

The platform has shipped without this layer up to now; nothing is on fire. But the 2026-05-29 incident shows the failure mode is real and the cost of a regression is high (audit-trail gap invisible until forensics). Worth doing on a multi-week timescale; not blocking.

---

## 🟡 Testing — container-based integration tests for the bootstrap pipeline (Tier 3)

**The Docker half of the scope below has since shipped as the `stack-smoke` and `bootstrap-fixture-smoke` jobs in [.github/workflows/ci.yml](.github/workflows/ci.yml)**, which build the image, run `manage.py check` against the built image with the source unmounted, bring the stack up through migrate and `createadmin`, poll the health endpoint and tear down with volumes — covering scope items 1, 5 and 6. `bootstrap-fixture-smoke` also assembles a `--demo` package and runs its own start script end to end, which is the runner a recipient uses and shares no code with the fixture's — over a stubbed UI bundle and a world-writable tree, since a runner can neither build the real frontend nor become the uid a deployment runs as. What remains is the explicit `showmigrations --plan` assertion, the superuser assertion, the volume-ownership check (item 4 below, which is the container's view of /data/recordings and not the package-tree ownership the start script checks), and the whole Podman job.

Tier 1 (shellcheck + `bash -n`) and Tier 2 (mocked dry-run via the `fakebin` fixture in [scripts/tests/](scripts/tests/)) ship and catch typos, wrong flags, reordered steps, missed distro branches, and management-command regressions. What they cannot catch is the actual container-runtime behaviour that broke the most recent fresh-deploy attempt: volume permissions, `init-volumes` ordering vs `db`, postgres-data-not-empty on `subpath:`-incompatible runtimes, network labels conflicting across compose backends, frontend-build `EACCES` on first-write to a named volume. Every one of those manifested only when a real Docker / Podman daemon executed the compose graph.

### Scope

A CI job that spins up an actual container runtime (`docker compose up -d --wait` for the Docker variant; `sudo podman compose up -d` for the Podman variant) against a fixture `.env`, asserts the resulting state, and tears down. Distinct from the existing per-app pytest suite — those run *inside* a single container against an in-memory SQLite. This harness validates the **outside** of the platform: the compose graph itself.

### Per-runtime coverage

Two CI jobs (Docker, Podman), each running:

1. **Stack-up smoke.** `bootstrap.sh --no-start` produces a clean image; then a manual `compose up -d --wait` brings up db / redis / migrate / web. Assert `compose ps` shows every service `running` (or `migrate` `exited 0`).
2. **Migrations applied.** `compose exec web python manage.py showmigrations --plan | grep '\[ \]'` is empty.
3. **Admin user created.** `compose exec web python manage.py shell -c "from django.contrib.auth import get_user_model; assert get_user_model().objects.filter(is_superuser=True).exists()"`.
4. **Volume ownership.** `compose exec celery stat -c '%U:%G' /data/recordings` returns the expected UID:GID (the `init-volumes` chown gap that broke `frontend-build` on first-write).
5. **HTTP reachability.** `curl -sf http://localhost:$HOST_PORT/api/v1/ready` returns 200 with `{"status":"ready"}` — the readiness probe, so the assertion covers database and cache reachability rather than only that the process answers.
6. **Teardown is clean.** `compose down -v --remove-orphans`; `compose ps` is empty; `volume ls` shows no `platform_*` volumes.

### Why yellow

Tier 1+2 land 80% of the regression-catching value for ~1 day of work. Tier 3 lands the remaining 20% but costs ~3 days of harness work plus ongoing maintenance against distro image churn and compose-spec drift. The Podman job specifically needs nested-container privileges (rootful podman or `--privileged`) which most CI providers gate behind self-hosted runners or paid tiers. Worth doing once Tier 2 misses start to outnumber Tier 2 catches — until then, the Tier 2 + management-command coverage is the better value-per-engineering-hour.

### Reference

The Tier 1+2 scaffolding that this entry sits on top of: [scripts/tests/](scripts/tests/), [scripts/lint-shell.sh](scripts/lint-shell.sh), the `shell` and `test` jobs in [.github/workflows/ci.yml](.github/workflows/ci.yml). Future Tier 3 jobs would mirror that placement under a new `e2e-docker` and `e2e-podman` workflow file gated by a path filter or a manual `workflow_dispatch` so they don't run on every PR.

---

## 🟡 Tooling — pre-commit / CI banner when a LOAD-BEARING file is in the diff

The [Load-bearing files](AGENTS.md#load-bearing-files) registry in `AGENTS.md` lists a handful of files where silently breaking a contract has no visible failure mode in unit tests. **The defensive layer this entry proposes already exists, and it blocks rather than warns**: [.review/agents/load-bearing-diff-reviewer.md](.review/agents/load-bearing-diff-reviewer.md) checks a diff against the registry and runs each named contract test, and `scripts/git-hooks/pre-commit` refuses the commit while any findings file is non-empty. What is left of this entry is the cheap always-on half — a `scripts/check_load_bearing.sh` and a CI job-summary line — for the case where the agent has not been run; the rationale below for warning rather than blocking no longer matches how the repository actually works. The convention when this was written was "header in the module docstring + AGENTS.md registry"; the next defensive layer is to surface a banner whenever a diff touches one of these files.

**Shape.** A small `scripts/check_load_bearing.sh` (or a ruff rule, or a pre-commit hook) that:

1. Scans the staged / pushed diff for any of the registered LOAD-BEARING files.
2. Prints a banner naming the file, the feature at stake, and the contract test to run.
3. Does **not** block the commit / push — the registry is a hint, not a gate. The contract tests themselves are the hard backstop; the banner just makes it harder to skim past.

**Minimum viable form.**

- A check-in script under `scripts/` that takes the list of changed paths (from `git diff --name-only` or the pre-commit hook's argv) and matches against a hardcoded list of LOAD-BEARING files. Prints a banner per match.
- A GitHub Actions step that runs the same script against the PR's changeset and posts the banner as a job summary so it's visible in the PR view.

**Why yellow rather than red.** The convention plus AGENTS.md + the contract tests already cover the failure mode. The banner is belt-and-suspenders — most valuable in multi-actor AI work where the agent may not read AGENTS.md in full before editing, or in human work where the file looks small enough that no-one stops to check the registry. Worth doing, doesn't block production.

**Why not block.** Blocking on touching a LOAD-BEARING file punishes the legitimate refactor case (which is the most common LOAD-BEARING edit). The hard guarantee should live in the contract test, not in pre-commit. A banner is the right friction level: visible enough to interrupt the skim, soft enough not to obstruct the legit case.

---

## 🟡 Tooling — additional review-agent workflows for load-bearing features without coverage

The `.review/agents/audit-trail-completeness.md` reviewer covers one load-bearing feature group (audit trail — middleware, signals, audit, request_context, settings). Several other load-bearing files have no dedicated coverage reviewer, just the generic `load-bearing-diff-reviewer` banner. The gap is silent-failure-shaped: a new endpoint that misses a PHI check, a permission gate, or a transaction wrapper passes unit tests, looks fine in PR review, and ships.

### Gap analysis against the AGENTS.md load-bearing table

| Load-bearing feature group | Files | Dedicated reviewer? |
|---|---|---|
| Audit trail | middleware, signals, audit, request_context, settings | ✅ audit-trail-completeness |
| Object-level access control | epicurrents/permissions, library/permissions | ❌ |
| SIEM rule surface | security_log | ❌ (covered by contract tests) |
| Federated JWT auth | federation/auth | ❌ (covered by 40+ contract tests) |
| PHI sanitisation | federation/middleware, recordings/processors/edf | ✅ shipped as [.review/agents/phi-exposure.md](.review/agents/phi-exposure.md), with an exemption registry |

Plus AGENTS.md cross-cutting rules with the same silent-failure shape that aren't on the LOAD-BEARING table: multi-step write atomicity, de-identification (`original_name` vs `display_name`), FAILED-recording hiding, GenericFK reverse relations.

### Reviewers to design, in priority order

**1. PHI / de-identification reviewer.** Highest payoff. The rules are scattered across recordings + annotations + library + federation; a misstep at any endpoint leaks PHI on the wire. Sweeps every API endpoint that returns recording / annotation / metadata bytes and checks: opaque hashes (not integer PKs) in URLs and response shapes; `display_name + file_extension` in `Content-Disposition`, never `original_name`; `_failed_hidden_for_caller` applied; `_can_see_original_name` gating author-private fields; EDF byte responses route through `federation/middleware.py` with the correct `apply_middleware` propagation through any `can_read_via_*` extension. Exemption registry for endpoints that legitimately bypass (download endpoints with apply_middleware off because they're caller-authenticated for unaltered bytes — currently zero, but the slot needs to exist).

**2. Object-level permission coverage reviewer.** Same shape as `audit-trail-completeness`: walk every Ninja router endpoint, check for a `can_*_object` / `ensure_*_object` call or a `_require_staff` / `_require_superuser` gate before serve / write. Exemption registry for endpoints that legitimately bypass (health, public keys, well-known, OPTIONS, vapid-public-key — bounded set). Catches the failure shape where a new endpoint fetches `Recording.objects.get(content_hash=...)` and serves it without consulting permissions.

**3. Multi-step write atomicity reviewer.** Narrower scope. Scans POST/PUT/PATCH/DELETE endpoints for ≥2 ORM writes (`.save()` / `.create()` / `.update()` / `.delete()`) not wrapped in `transaction.atomic()`. Catches the canonical hazards AGENTS.md spells out: object + AccessRight together, parent + children, token TOCTOU. Lower hit rate than the PHI sweep but bounded and mechanical.

**4. Audit-trail tamper-detection reviewer.** Distinct from `audit-trail-completeness` (which checks endpoint Activity-row annotation) and from `load-bearing-diff-reviewer` (which runs contract tests when [activity/audit.py](activity/audit.py) / [activity/signals.py](activity/signals.py) appear in the diff). Sweeps for code paths that bypass the canonical write / verify flow:

- New `ObjectChangeLog.objects.create(...)` outside [activity/audit.py](activity/audit.py) and [activity/signals.py](activity/signals.py) — manual audit-row writes that skip `current_write_hash_version()` and write under the default `hash_algorithm="v1"` instead of the configured keyed algorithm.
- New references to `compute_audit_hash`, `current_write_hash_version`, or `_compute_audit_hash_v{N}` outside the canonical helpers and tests.
- New reads of `change.before_state` followed by writes to the target model — restoration logic that bypasses [`verify_change_hash`](activity/audit.py) and the rollback path's `ChangeHashMismatch` guard.
- New migrations that modify the `after_hash`, `hash_algorithm`, `hash_key_version`, `before_state`, or `changes` columns on existing rows. The historical claim that "the hash was written at the time of the event" is destroyed by retroactive rewrites; any such migration needs explicit sign-off.
- New writes to `ObjectChangeLog` from a `RunPython` migration — same concern as above plus the migration runs outside the request-context bridge so the actor field defaults to NULL.

Lower hit rate than the first three. Belt-and-suspenders over the load-bearing-diff coverage on the relevant files plus the contract tests in [activity/tests/test_audit.py](activity/tests/test_audit.py). The marginal value is catching the case where someone adds a NEW write/read site outside the canonical locations; load-bearing-diff only fires when the canonical files are touched directly.

### Shape of each agent (reference: `.review/agents/audit-trail-completeness.md`)

- Markdown frontmatter naming the agent + tool set (Bash, Read, Grep, Write).
- A "The invariant you enforce" section spelling out the rule.
- A 5-6 step procedure: (1) identify changed files in the diff, (2) enumerate in-scope endpoints / models / writes, (3) filter against the exemption registry, (4) verify each remaining item against the rule, (5) compose the report with `PASS` / `FAIL` verdict, (6) write the findings file (empty = clean, non-empty = blocks pre-commit hook).
- A "What you will NOT do" section bounding scope (don't edit source, don't run tests, don't flag unrelated regressions).
- Per-reviewer exemption registry at `.review/exemptions/<name>.md` for the legitimately-bypassing endpoints.
- A findings file at `.review/findings/<name>.md` that the pre-commit hook checks for size.

### Why yellow

The three reviewers cover real silent-failure surfaces, but the existing combination of contract tests + load-bearing banner + cross-cutting rules in AGENTS.md catches most violations already. The reviewers are belt-and-suspenders for the cases where the cross-cutting rule is most likely to be missed (PHI surface is broad and easy to miss one field; permission gates are easy to forget on a new endpoint). Worth doing in sequence, not blocking on any one feature.

---

## 🟡 Activity — optional periodic integrity-check Celery task

**Most of this shipped; only the orphan-`Activity` check below is open, and the settings table further down describes settings that do not exist.** [activity/integrity_check.py](activity/integrity_check.py) walks every chain shard, and `verify_audit_integrity` in [activity/tasks.py](activity/tasks.py) runs it daily from the beat schedule, emitting `audit.chain_break`, `audit.chain_gap`, `audit.genesis_invalid`, `audit.hash_key_missing` and `audit.derived_state_mismatch`, with one INFO line on a clean run. It is unconditional rather than operator-gated, verifies full scope rather than a lookback window, and has no notification step — so none of `ACTIVITY_INTEGRITY_CHECK_ENABLED`, `_LOOKBACK_DAYS`, `_BATCH_SIZE` or `_NOTIFY_ON_MISMATCH` exists; the only related setting is `ACTIVITY_DERIVED_CHECK_WINDOW_DAYS`. What remains is detecting `Activity` rows left with a null `status_code` past some threshold, which nothing checks. The original framing follows.

An audit trail is only as good as the discipline of verifying it. At the time of writing nothing recomputed `ObjectChangeLog.after_hash` against the stored row contents after the fact — a tampered row would sit silently in the database until the next rollback against it happens to fail (and even then, the failure mode is "rollback restores wrong state," not "tampering detected"). A periodic Celery task that re-validates recent rows would catch silent corruption while the relevant operators / Activity rows are still in scope.

**Scope (initial).** A `verify_audit_trail_integrity` task that:

1. **Recomputes the hash** for every `ObjectChangeLog` row created within the last `ACTIVITY_INTEGRITY_LOOKBACK_DAYS` and compares against the stored value. Mismatches are logged at ERROR with the row pk, content_type, action, and `performed_by_id`. The task does not delete or modify the row.
2. **Detects orphan Activity rows** — rows where `status_code` is `null` more than N minutes after `created_at`. These represent requests that the middleware started but the response phase never finalised (process kill, exception inside response post-processing, etc.). Useful operational signal even outside the integrity story.
3. **Surfaces results** — logger at INFO when clean, ERROR per mismatch; optionally a `send_push_to_user` to all superusers when any mismatch is found.

**Scope (gated on later work).**

- **Chain validation — done.** The chain-hash hardening landed, and `verify_chain` in [activity/audit.py](activity/audit.py) walks each shard end to end verifying `prev_hash` continuity, over full scope rather than a lookback window. A break in the chain is the canonical "row N was inserted out of band" signal.
- **Bulk-bypass detection.** If `AUDITED_MODELS` opt-in tracking lands (see [bulk-operations audit-trail gap](#-activity--close-the-bulk-operations-audit-trail-gap)), cross-check that the database row count for each audited model matches the implied count from `ObjectChangeLog` (create entries minus delete entries, give or take pre-tracking baseline). Wide discrepancies indicate bypassed writes.

**Configuration.**

| Setting | Default | Notes |
|---|---|---|
| `ACTIVITY_INTEGRITY_CHECK_ENABLED` | `False` | Off by default — operator must opt in. Mirrors the "optional" framing. |
| `ACTIVITY_INTEGRITY_LOOKBACK_DAYS` | `7` | Bound the lookback window so the task stays bounded as the change-log grows. |
| `ACTIVITY_INTEGRITY_CHECK_BATCH_SIZE` | `1000` | Batch the hash recomputes the same way `archive_old_activity` batches its update. |
| `ACTIVITY_INTEGRITY_NOTIFY_ON_MISMATCH` | `True` | If a `send_push_to_user` to superusers is desired on detected tampering. |

Beat schedule: weekly or daily depending on data volume and operator preference. Run at off-peak hours (the task is read-heavy but does recompute hashes for thousands of rows per day's worth of activity).

**Why optional.** The integrity check makes most sense once the hash itself is HMAC- or chain-hardened (otherwise tampering attackers also re-hash). For a sandbox deployment with the fingerprint-only hash and no production data, the task is mostly useful as a "did I accidentally break the change-logging pipeline" canary — still valuable, but not load-bearing. Enabling it should be an explicit operator decision per deployment, especially because the bypass / orphan checks may produce noisy results until the surrounding hardening lands.

This is the natural complement to the hash-hardening and bulk-gap items above — together they form "the audit trail is forgery-resistant *and* actively verified."

---

## 🟡 Security — detect unauthorised direct database / filesystem access

Every detection control the platform has observes writes that went *through Django*. `verify_audit_integrity` proves the logged history was not forged; nothing asks whether something happened that was never logged at all. An actor who reaches Postgres with `DB_PASSWORD` — from a leaked `.env`, a CI secret, or a restored Borg archive — or who gets a shell on the host and reads the recordings volume, operates entirely beneath the audit layer: the chain stays valid, it simply stops describing reality. Writes of this kind *are* detectable from inside the application (the newest `ObjectChangeLog` row's reconstructed after-state stops matching `serialize_instance` of the live row, and nothing currently compares the two); reads are not detectable from inside the application at all, and need either pgaudit or host-level kernel instrumentation. Redis and the backup repository are direct-access surfaces of the same class: a leaked `REDIS_PASSWORD` allows Celery task injection and federation replay-cache flushing, and a copied Borg archive plus the passphrase from the same `.env` is the full PHI corpus readable offline.

The note's v2 (2026-08-23, after a clean-slate review of v1) makes three of the fixes architectural rather than additive:

- **Model-gated audit registration** — an explicit `AUDITED_MODELS` registry replaces the emergent context-gated coverage set as the source of truth: the signal handlers consult it, the coverage check walks it, and a registered model saved outside an audited context becomes a loud write-time event instead of a divergence finding days later. This absorbs the registry the [bulk-operations audit-trail gap](#-activity--close-the-bulk-operations-audit-trail-gap) already wanted, and becomes a hard prerequisite for the coverage check.
- **An evidence-host topology** — *shipped 2026-09-04 at [examples/evidence-host/](examples/evidence-host/), except WAL archiving; the heartbeat became a dedicated `emit_security_heartbeat` beat task rather than the clean-run summary lines described here* — the off-host log sink, WAL archiving, the append-only Borg repository, and dead-man absence alerting (the tasks' clean-run summary lines are the heartbeat; stopping `celery-beat` must page, not silence everything) named as one deployable append-only unit on the tailnet, replacing scattered per-control operator obligations.
- **Chain-head anchoring** — periodic off-host publication of each shard's `(content_type, sequence_no, after_hash)` head, so an attacker holding `ACTIVITY_HASH_KEYS` from the same host can forge rows but not make the published head match; forgery becomes detectable at anchor cadence. Federated peers countersigning each other's heads over the existing Ed25519 channel is the preferred transport (decision-gated on the federation network layer stabilising), the evidence host the fallback.

Around those, eight in-stack phases (registry; coverage / state-divergence check with re-verify-before-alarm discipline; DB role separation plus append-only + event triggers and `pg_hba` tightening; Postgres connection + DDL logging, then pgaudit with its stolen-app-credential scoping limit stated honestly; a stored-file sweep over `Recording.file_hash` *and* `MediaFile.file_hash`; a canary recording plus a plaintext-share-token honeytoken — the one in-stack read signal; stack hardening covering runtime posture, Redis ACLs, file-based secrets and image digest pinning; anchoring) and the host controls that cannot live in compose (auditd, Falco, host FIM, LUKS, per-administrator attribution, egress). New `epicurrents.security` event types, settings, the documentation surface, and an honest residual-risk section — transient modify-then-revert between scans is invisible to the coverage check (WAL evidence bounds it forensically), and statement-level read visibility for an attacker using stolen application credentials is unattainable, with session-level connection anomaly as the compensating control — in **[docs/engineering-notes/intrusion-detection-design.md](docs/engineering-notes/intrusion-detection-design.md)**.

Interacts with three entries here: the unaudited Django admin window (the coverage check turns that silent gap into a loud one — every admin edit surfaces as `audit.state_divergence`, so the two should be scheduled together), the [bulk-operations audit-trail gap](#-activity--close-the-bulk-operations-audit-trail-gap) (converges into the Phase 0 registry rather than shipping a parallel mechanism), and the [periodic integrity-check task](#-activity--optional-periodic-integrity-check-celery-task), whose scheduled entry point the coverage check reuses. Off-host custody of `ACTIVITY_HASH_KEYS` (KMS / signing agent) is deliberately out of the note's scope and should become its own entry — anchoring reduces its urgency from "the trail is forgeable" to "forgery is detected at anchor cadence", no further.

---

## 🟢 Activity — close the bulk-operations audit-trail gap

Core closure landed in five phases (`git log --grep "with_system_activity\|extra_payload\|recordings.purge\|recordings.import\|access_rights.purge"`):

1. **Audited scope for non-request callers** — `activity.system_activity.with_system_activity(verb, *, interface, target, metadata)` opens an `Activity` row (`interface ∈ {api, celery, command}`) and flips the audit-context ContextVar so nested signal-driven writes attribute correctly. The gate is `is_audited_context()`.
2. **Derived-row digests** — `ObjectChangeLog.extra_payload` (`JSONField`, mixed into the hash) carries caller-supplied digests of bulk-created dependent rows; `activity.derived_state.verify_derived_state(change)` recomputes them against live state via a per-`(target_model, key)` digester registry. The canonical example is the `SignalInfo` digest on `Recording`'s READY transition in [recordings/audit_digests.py](recordings/audit_digests.py).
3. **`recordings.process` (Celery)** — `process_recording` wraps the body in `with_system_activity` and emits three `Recording` modify rows for the state transitions, with the final READY transition carrying the SignalInfo digest.
4. **`recordings.purge` (Celery)** — `purge_deleted_recordings` wraps both the soft-delete and orphan-reaper passes; per-row `pre_delete` produces DELETE entries.
5. **`recordings.import` (command)** — `import_recordings._run_job` wraps under `interface=command`; the per-recording final-transition `.update()` carries the same SignalInfo digest as the Celery path.
6. **Cleanup tasks** — `purge_expired_access_rights` and the stale-subscription cleanup inside `send_push_to_user` both audit each removed row.

Two known sites are intentionally NOT audited:

- [annotations/models.py::recompute_content_hash](annotations/models.py) — fires on every `Code` mutation; the parent's `content_hash` change is invisible by design. Documented in [annotations/README.md](annotations/README.md#update_parent_hash_on_code_change).
- [activity/tasks.py::archive_old_activity](activity/tasks.py) — batched archive of old Activity rows. Self-referential (the audit trail archiving itself).

The cross-cutting rule "Bulk ORM operations bypass the audit signal" in [AGENTS.md](AGENTS.md#bulk-orm-operations-bypass-the-audit-signal) names the closure pattern and the digest-in-parent recipe. The [activity/README.md → Derived-row digests](activity/README.md#derived-row-digests) section documents the verifier.

**Plugin punt.** Each project's and plugin's `tasks.py` / management commands have their own bulk-write sites (state-transition `.update()` calls, bulk delete + `bulk_create` swaps where digest-in-parent is the natural shape); the closure work for those lives in the owning README.

### Optional follow-on — opt-in tracking + bypass lint

The opt-in / lint shape originally sketched here remains a clean-up worth doing but no longer load-bearing:

- Replace `signals.py`'s implicit "track every non-`Activity`/`ObjectChangeLog` model" gate with explicit `AuditedModel` opt-in tracking (settings list or marker mixin).
- Then write a `ruff` / `pre-commit` rule that flags `.update(...)`, `.bulk_create(...)`, `.bulk_update(...)` on opted-in models, with a grep-able `# noqa: ACT001 — see <site>` escape hatch.

Order of work: opt-in tracking first, then the lint rule against that subset. Both can land as one PR.

---

## 🟡 Recordings — contract tests + load-bearing flag for `preservation.write_original` byte integrity

[recordings/preservation.py](recordings/preservation.py) — `write_original` is the operator's regulatory-backstop write path: under `RECORDINGS_PRESERVE_MODE=all`, every uploaded recording's raw bytes go to the host-controlled originals volume before any sanitisation step touches them. **The mode names used below are wrong — the three modes are `none`, `failed` and `all`, not strict / weak / disabled — and the test half of this entry has largely landed**: [recordings/tests/test_preservation.py](recordings/tests/test_preservation.py) covers idempotency, a missing source, path traversal, manifest clobbering, the converter-rename case, per-mode gating on both the Celery and import paths, and the `validate_originals` command. What is genuinely still open is the flag itself: the module carries no `⚠️ LOAD-BEARING` block and is not in the AGENTS.md registry. The function's invariants are tight: byte-identical write, hash recomputation that matches the source, no half-files left on failure, no programmatic read path (the volume is operator-managed, not platform-managed).

### Why load-bearing

The preservation tier exists to satisfy a regulatory claim the operator makes to auditors. A function that writes "mostly the bytes" or "the bytes minus a trailing newline" silently breaks that claim — externally invisible until a re-hash compares the preservation copy against the upload-time hash and disagrees. Silent-failure surfaces:

- Byte drift — a future text-mode open (`"w"` instead of `"wb"`), an encoding-detection step, a "normalise line endings" helper applied in the wrong place. Produces a file whose hash no longer matches the source.
- Hash recomputation skipped — a future code path that writes the file but trusts the upload-time hash without re-verifying. The on-disk copy could be corrupt and no-one would know until an external audit.
- Partial-write tolerance — a write that fails midway and gets logged-and-swallowed leaves a half-file with no DB record that it's bad. The next operator scan thinks the recording is preserved.
- Read-path drift — a future "convenience" management command that adds a read on the preservation volume reintroduces the PHI surface the volume exists to bound (already covered by the PHI-exposure C7 rule, but a contract test pins the invariant from the other side).

### Shape of the contract test set

- A successful write produces a file whose SHA-256 matches the upload-time hash byte-for-byte.
- A partial-write failure (simulated via a mid-write exception) raises, does not log-and-continue, and leaves the volume without a half-file (clean up partial bytes).
- The write is binary, never text-mode, regardless of source content.
- The preservation-mode setting gates the call site, not the function — `write_original` writes when called, period; the caller decides whether to call.
- The manifest entry (`manifest.json`) is written atomically with the file write — both succeed or neither does.

### What to do when scoping the work

1. Read `write_original` end-to-end; identify the current safety guarantees (atomic move? lockfile? hash recompute on write?).
2. Sketch the contract test set; flag any invariant the current code does not actually preserve.
3. Add the file to the AGENTS.md load-bearing table.

### Why yellow

The function is small and the preservation tier already works for the documented cases. Hardening, not fixing. Pairs naturally with the erasure-pathway entry above since both deal with the regulatory-backstop side of the recording lifecycle.

---

## 🟢 Context menus for annotations and signal selections

**Half of this is already written but unreachable.** The viewer ships a `ContextMenu` component whose `annotation` branch offers a type submenu and a delete action, but the only two call sites that build a menu context both pass `plot` as the target, so the annotation branch is dead code. The missing piece is the right-click handler that identifies an annotation under the cursor and opens the menu against it — not the menu itself. Selection actions are partly there too: cancelling a pending selection is a menu item, and inspect is wired to right-click directly rather than through the menu.

- Right-clicking an existing annotation marker in the navigator or trace should open a context menu with quick actions: edit, lock/unlock, delete, copy time.
- Right-clicking a completed signal selection should offer: annotate selection, inspect selection (open analysis tool), clear selection.

Referenced in the [Annotations](docs/epicurrents/src/docs/latest/annotations.md) documentation page.

---

## 🟢 Annotations — wire share-token attribution end-to-end

[`can_annotate_object`](epicurrents/permissions.py) and `ensure_can_annotate_object` already enforce that share-token callers must supply a non-empty `annotator` string — the contract is in place. No annotation endpoint currently accepts a `share_token`, though: all four create / update / delete sites in [annotations/api/v1/ninja.py](annotations/api/v1/ninja.py) authenticate via session cookie and pass `annotator=payload.annotator` to `can_annotate_object` without forwarding a token. The token branch is therefore dead code on the live API.

To make share-token annotation usable end-to-end:

1. **Accept `share_token` on the relevant annotation endpoints.** Query parameter or `X-Share-Token` header, matching whatever the recording-download path settles on. Pass it through to `ensure_can_annotate_object` so the existing 400 / 403 split fires correctly.
2. **Persist the `annotator` string on the annotation row.** Today the `*In` schemas carry `annotator` only as a permission-check witness; nothing on the saved annotation records who created it when the author is the platform's `__system__` user (the fallback for token callers). Add an `annotator: str = ""` field on `AnnotationBase`, populate it from `payload.annotator` when authentication is via share token, and surface it on the `*Out` schemas.
3. **Audit-trail attribution.** `Activity` rows currently record `user` from the session. For share-token writes there is no session user; either record the annotator string in `Activity.notes` or extend the audit model with a `nominal_actor` column. Decide before step 2 lands so the migration doesn't ship without an audit story.

Until this happens, the share-token branch in `can_annotate_object` is contract-only — kept so the eventual implementation has the validation already in place, but unreachable in practice.

---

## 🟢 Annotations — seed the `AnnotationKind` token vocabulary (possibly project-specific)

The [`AnnotationKind`](annotations/models.py) table (added with the pipeline-persistence §F version binding) is the controlled vocabulary of semantic kind tokens that analysis producers advertise (`compute.AnalysisRun.produces_kind`) and consumers require — the registry from [retention-and-lifecycle-plan §1.1](recordings/retention-and-lifecycle-plan.md). It ships **empty**: the canonical tokens (`hypnogram`, `spike_events`, `artifact_spans`, …) still need populating before an analysis dispatch DAG can resolve a consumer's required kind against a producer's advertised one.

Seeding is deferred deliberately — the canonical vocabulary is a domain decision, not a schema one, and it is likely **project-specific** rather than a single global registry:

- A sleep-study deployment cares about `hypnogram`, `spindles`, `arousals`, `respiratory_events`; an epilepsy deployment cares about `spike_events`, `seizure_events`, `hfo`. One global list serves neither cleanly.
- `AnnotationKind` is currently **global** (no project scope), so `token` is globally unique. If the vocabulary becomes project-specific, the table needs a project scope and `token` uniqueness moves to per-project — a table-shape change. Settle the scoping question *before* seeding, since it changes the model.
- Seeding then happens either as a data migration (global set) or a project-bootstrap step that installs the deployment's starter set (per-project).

Until seeded, `produces_kind` still works as a free token (the string is the identity); the registry simply isn't yet enforcing a controlled set. Populate it — globally or per-project — when the analysis dispatch layer that resolves kind dependencies is built.

---

## 🟢 Viewer annotation persistence through the platform API

Annotations created through the viewer UI are stored **in-memory only** — the viewer has no `DatabaseAPIConnector` configured, so nothing is written to the Django `annotations` tables. As a result, a project panel that offers "my annotations" has nothing to load on page reload.

Two approaches to fix this:

**Option A — Configure `DatabaseAPIConnector` in the viewer setup** (`ViewerView.vue → Epicurrents.createEpicurrentsApp(setup)`). The connector needs the platform's annotation API base URL (`/annotations/api/v1/`) and the session auth token forwarded as a header. Once configured, the viewer will persist `Event`, `Label`, and `Interruption` writes to the database automatically, and a project panel can load them back via the annotation list API (`GET /annotations/api/v1/events/?target_object_id=<recording_pk>&author_id=<user_pk>`).

**Option B — a project-side submission flow** (a teaching project has one: join a session under a staff account, annotate, submit, mark the set as the reference). Simpler — works with the existing infrastructure today, but only for a project that carries such a flow.

Until one of these is implemented, a project's restore path can only fall back to resetting the annotations.

---

## 🟢 `scoped-event-log` — JSON export / import + hide empty context line

Lives in `frontend/viewer/util/scoped-event-log`.

**JSON export / import.** Add a method on `Log` (e.g. `Log.exportAsJson()` / `Log.importFromJson(blob)`) that serialises the in-memory event buffer to a JSON object containing timestamp, level, scope, message and context, and a matching importer that re-populates the buffer for inspection. Useful when an issue only reproduces on the user's machine — they can capture the log on a freeze that doesn't kill the renderer, hand the JSON file to an agent/dev, and the same events can be re-played through `LogInspector` for triage. There is already `Log.exportToJson(level?)` which returns events filtered by level; consider whether the import side just rehydrates that output or whether we want a richer format.

**Hide the empty `[object Object]` context print — done.** `Log.print` now makes a single console call, joining the message with the formatted extra, and returns nothing for an absent extra. Only the JSON import half of this entry remains. As originally written: every `Log.add(...)` call printed two lines to the console: the formatted message and the context object. When the context is `{}` (the common case — most call sites pass no context), the second line is just `[object Object]` and adds noise that drowns useful output (the EegTrend mount/freeze investigation is a recent example). In `Log.print` (or wherever the context dump is wired), skip the second `console.<level>(...)` call when the context has no own enumerable keys, or when it equals `Object.create(null)` / `{}`.

Both items are small but compounding wins for debuggability.

---

## 🟢 Viewer — Migrate epicurrents packages from webpack to Vite

All 14 packages under `frontend/viewer/epicurrents/` currently build with webpack + ts-loader. Three liabilities motivate the migration: declaration files leak into `dist/` during the UMD pass, `tsconfig-replace-paths` is a fragile post-processing step that creates a corruption window between `tsc` and path replacement, and `libraryTarget: 'umd'` is the legacy module format. Vite's library mode (powered by Rollup) addresses all three — esbuild handles transpilation without declaration emit, `#`-aliases resolve at bundle time, and Rollup emits dual ESM + CJS output.

In progress — `core` migrated on 2026-08-20 and inlines its own workers; the sibling packages remain on webpack. Full plan including shared config design, worker handling, `dts` plugin setup, three-batch migration order (easiest → most complex), per-conversion considerations (`package.json` exports, `moduleResolution`, Python `?raw` imports, sourcemaps), and post-migration devDependency cleanup in **[docs/engineering-notes/vite-migration.md](docs/engineering-notes/vite-migration.md)**.

---

## 🟢 Viewer — additional EEG trend types (BSR/iBI, rEEG)

Four trend types ship today (aEEG, spectrogram, frequency ratio, pdBSI). The next two clinically-meaningful additions are burst-suppression metrics and a percentile-based range-EEG.

- **BSR (burst-suppression ratio)** — fraction of each epoch spent below an amplitude threshold; relevant in coma and anaesthesia monitoring. Distinct math from aEEG envelope detection.
- **iBI (inter-burst interval)** — mean or median time between bursts; usually paired with BSR.
- **rEEG (range-EEG)** — same envelope idea as aEEG but with 5th and 95th percentiles in place of min/max, less artefact-sensitive.

Adding a trend type is mostly the math plus a renderer Vue component — lifecycle, settings, worker commissions, and UI scaffolding are all in place. Follow the seven-step recipe in CLAUDE.md ("Biosignal trends — Adding a new trend type").

---

## 🟢 Viewer — click-to-seek inside trend strips

Clicking inside a trend band (aEEG, spectrogram, pdBSI, ratio) currently does nothing. A natural UX win is to seek the EEG view to the clicked time, mirroring `EegNavigator`'s behaviour. Mechanically straightforward: a pointer-event handler in `EegTrend.vue` that converts canvas X to recording time and dispatches the existing `set-view-start` action.

---

## 🟢 Viewer — cascade montage worker enablement

Phase 1 of the cascade montage (shipped 2026-06-06) deliberately bypasses the montage worker — `GenericBiosignalCascadeMontage.getAllSignals` reads the source channel directly via `recording.getAllRawSignals` and slices on the main thread, and `GenericBiosignalResource.addCascadeMontage` skips the `setup-worker` / `setup-cache` / `set-interruptions` commission chain.

This works for the polygraphic-scanning use cases that motivated phase 1 (visual inspection of EKG, breathing, EMG bursts) where raw signal is acceptable. It does not support:

- **Filters.** Highpass / lowpass / notch are applied by the montage worker's `MontageProcessor`. Cascade montages currently render raw bytes regardless of the user's filter settings. Most clinical EKG scanning expects a 0.5–40 Hz band; EMG often wants higher cutoffs.
- **Derivations.** The cascade always reads one source channel as-is. There's no way today to express, say, an EKG cascade against a (EKG-L − EKG-R) bipolar derivation, or a cleaned-up EOG derived against the ground.
- **Project-specific processing.** Any modality-specific pipeline that lives in the worker (Pyodide-based processing, custom filtering) is unreachable from cascade.

### What the worker enablement needs

The worker doesn't know what a cascade montage is — it expects a `ConfigMapChannels` to map the setup channels to N derivations, and a setup-worker commission carrying that config. The cascade montage's channel definitions are constructed manually (N identical entries pointing at the same source index), so a synthetic config that the worker's `mapMontageChannels` can accept needs to be either: (a) derived from the cascade's per-row spec at commission time, or (b) replaced by a "raw-source-N-rows" mode the worker understands directly. (b) is the smaller surface change but requires `MontageProcessor.setupChannels` to learn a new branch.

Once the worker is wired:
- `GenericBiosignalResource.addCascadeMontage` restores `setupWorker` + `setupServiceWithInputMutex` / `setupServiceWithCache` + `setInterruptions` calls.
- `GenericBiosignalCascadeMontage.getAllSignals` goes through the worker again (`super.getAllSignals` with `include: [0]`), so filters and any future per-derivation processing apply.
- **Remove the no-op overrides** on `GenericBiosignalCascadeMontage`: `setInterruptions` (currently swallows the call) and `updateFilters` (currently returns `{ success: true, updated: false }` without commissioning). Each has a `GOTCHA` block on the method docstring pointing back here so the cleanup is locally discoverable. Leaving them in place after the worker lands would silently drop interruption updates and filter changes — the most insidious "looks right but isn't" failure mode the codebase has, because the local `this._filters` state still updates so the UI continues to show the user's filter selection as active.
- The mock + slice test get updated to mirror the worker path.

Defer until cascade is actually being used in clinical workflows where filtered visualization matters; the visual-scan use case shipped in phase 1 is the larger value delivery.

---

## 🟢 Viewer — live cascade montage mode for streaming single-channel recordings

`GenericBiosignalCascadeMontage` (shipped 2026-06-05) implements a static page-stacked view: N time-shifted slices of one source channel laid out as N rows on a finished recording, page-turns advance `viewStart` by the full visible reach. The natural extension is a **live mode** where the bottom row scrolls in real time as new samples arrive from a streaming source — paper-strip readout semantics — older rows shifting up as the bottom fills.

The clinical use cases all involve a single continuously-streaming channel: real-time EMG monitoring (motor unit firing, fatigue), NCS during nerve-conduction studies, single-channel EKG / respiration in a bedside setup, eventually live EEG when streaming ingest lands. Polygraphic scanning of a finished recording (the v1 static cascade) and live monitoring share the same visual shape but have very different data-flow assumptions.

### Phase outline (rough)

1. **Streaming source contract** — define how the cascade montage learns about new samples (event on the recording? polling `signalCacheStatus`?). The viewer already has `signalCacheStatus[1]` as a "data is loaded up to here" marker; live mode could watch that property and trigger a redraw shift each time it advances by ≥ 1 row's worth.
2. **Row shift logic** — when the cached end advances past the current bottom row's window, drop the top row, shift remaining rows up, append a new bottom row whose data is the freshly cached samples. The renderer doesn't change shape — only the data fed to each WebGL trace.
3. **viewStart semantics in live mode** — auto-track the trailing edge of the cache. The user can scroll backward into history (where it behaves like the static cascade) and a button / hotkey snaps back to "follow live".
4. **Modality wrappers** — `EmgCascadeMontage` (analogue to `EegCascadeMontage`), eventual `NcsCascadeMontage`. Each just plugs into the existing template-method hook on its resource (`_constructCascadeMontage`) and sets the worker. The streaming logic stays in `GenericBiosignalCascadeMontage`.
5. **Cache cooperation** — live mode is the case where the rolling-cache work matters most: a streaming source feeds into the same cache, the cascade reads from it, the renderer reads the cascade. Was gated on the rolling cache stabilising; that entry closed 2026-09-04, so the gate is lifted.

### Out of scope for v1 live mode

- Backend ingestion of live streams (LSL, ad-hoc REST, federation channels, ...) — separate workstream.
- Multi-channel live cascade (multiple sources stacked in parallel) — the v1 shape is one source × N rows; multi-source is a different layout problem.

Filed separately from the EEG-specific roadmap items because the use cases (EMG / NCS / live EEG) and the time-flow model (trailing-edge tracking) are modality-agnostic and don't share design surface with the EEG aEEG / spectrogram entries above.

---

## 🟢 Document the iOS PWA install flow for push notifications

iOS Safari supports Web Push only when the site is installed as a PWA via "Add to Home Screen" (Apple-imposed since iOS 16.4). Users on iPhone who visit the site in a regular Safari tab and grant notification permission silently receive nothing — there is no error and no guidance.

To-do:

- Add a short onboarding page (or section in the existing profile / settings flow) that detects iOS and walks the user through "Add to Home Screen". On Android / desktop, hide the guidance.
- Verify the build produces a valid `manifest.json` (proper `name`, 192×192 and 512×512 icons, `display: "standalone"`). Largely satisfied already: [frontend/vite.config.ts](frontend/vite.config.ts) registers `vite-plugin-pwa` unconditionally with a manifest carrying the name and both icon sizes, so only `display` is worth checking. There is no `VITE_ENABLE_PWA` switch — the opt-in was retired when singlefile builds were.
- Document the iOS limitation in the external docs / onboarding so users (and the support channel) know what to expect.

Cheap to write; meaningfully reduces "I never get notifications" support churn on iOS.

---

## 🟢 Borgmatic README + frontend README review

Two documentation debts left over from the per-app README cascade.

**Borgmatic.** The borgmatic backup service has no README of its own. The configuration model, retention policy, SSH key setup for remote backups, and the restore workflow are documented only inline in `.env.example`, in [`scripts/backup.sh`](scripts/backup.sh) / [`scripts/restore.sh`](scripts/restore.sh), and in the borgmatic config files themselves. A short `borgmatic/README.md` covering where backups land, the keep-daily / weekly / monthly retention semantics, the remote-repo bootstrap (`borg init`), key rotation, and restore-from-archive would close the gap.

**Frontend README review.** [`frontend/README.md`](frontend/README.md) is comprehensive but predates the AI-friendly restructure and the in-repo README style guide. A tone pass for consistency with the per-app READMEs is overdue — specifically: check for "draw a picture" codas, restated explanations, prose-bold emphasis used outside of warnings, and any sections that have drifted out of sync with the current frontend code.

Both deferred from the original documentation audit. Low priority; debt items to close opportunistically.

---

## 🟢 Activity — split `audit.py` into focused submodules

**The sizing and the inventory below are both stale, and the split is no longer cosmetic.** The file is now 1356 lines and additionally carries the versioned hash dispatcher and its three algorithms, the erased-hash sealing surface, the masked-field registry, the chain machinery and the single chained write entry point — none of which has a home in the four-module layout proposed here. It is also in the AGENTS.md load-bearing registry, so a split is a change to a guarded contract rather than the "purely readability, no functional impact" refactor described below. Re-plan before acting.

As originally written: [activity/audit.py](activity/audit.py) is ~425 lines and does four jobs: serialization (`_json_safe`, `serialize_instance`, `compute_audit_hash`, `diff_states`), recording (`record_api_activity`, `record_*_change`), permission (`can_rollback_change`, `_has_write_access_for_ref`), and rollback execution (`rollback_change`, `_restore_object_state`, `_create_rollback_activity`). Each concern is independently testable and has a natural module home.

**Proposed layout:**

```
activity/audit/
    __init__.py       # re-exports the public API for backwards compatibility
    serialize.py      # _json_safe, serialize_instance, compute_audit_hash, diff_states, snapshot_instance
    record.py         # record_api_activity, record_create_change, record_modify_change, record_delete_change
    permission.py     # _has_write_access_for_ref, can_rollback_change
    rollback.py       # _restore_object_state, _create_rollback_activity, rollback_change
```

Benefits beyond surface-area reduction: each file ends up small enough that internal ordering (alphabetical vs. dependency-bottom-up — the debate touched on during the audit pass) stops mattering, and changes to one concern have a smaller blast radius on diffs.

**Constraints to respect:** the `_audit_before_state` piggy-back attribute between `signals.py` `pre_save` and `post_save` handlers must keep working; the rollback path's transaction boundaries (currently inside `_restore_object_state` and `rollback_change`) must not be split across files in a way that obscures atomicity; tests in `activity/tests/test_audit.py` import from `activity.audit` and should continue to work via the `__init__.py` re-exports.

Low priority — purely a readability / maintainability gain; no functional impact.

---

## 🟢 Activity — document and auto-handle rollback ordering for cascade deletions

When an API DELETE on a parent cascades to N child rows (e.g. an `Event` with attached `Code` rows, or a future `Recording` with its annotations), `activity/signals.py` records one `ObjectChangeLog` DELETE entry per row. All entries share the same `activity_id` (now discoverable via `?activity_id=` on `/changes/`).

Two sharp edges remain for callers wanting to undo the whole cascade:

**1. Rollback ordering is the caller's responsibility.** `rollback_bulk_endpoint` iterates `change_ids` in the order supplied. Cascade deletions log leaves-first (FK constraints force the SQL DELETE order), so a caller listing the entries by `?activity_id=` and passing them through to bulk-rollback as-is will try to restore a `Code` before its parent `Event` exists — the generic-FK target is gone, the restore fails. The caller must **reverse** the listing order to restore parent-first.

**2. The rollback endpoint's docstring doesn't mention this.** A developer hitting this for the first time would have to derive the rule from a failure.

### Two fixes — pick one, possibly both

- **Document it.** Add a "Rolling back a cascade" section to [activity/README.md](activity/README.md) walking through the `?activity_id=` filter, the reverse-order rule, and a concrete example. Low effort, addresses the discoverability concern.
- **Auto-order in `rollback_bulk_endpoint`.** The endpoint could topologically sort `change_ids` by dependency before iterating: CREATE entries first (parents have to exist before children that FK-point at them), then MODIFY, then DELETE in reverse-cascade-dependency order. Higher effort, but eliminates a class of "you held it wrong" foot-guns. Requires the endpoint to look up the content_type → FK-graph for each row, which Django's `Collector` already encodes — but accessing that from outside the delete path needs some careful plumbing.

Recommendation: ship the docs entry first (1-2 hour task); revisit auto-ordering if the manual approach proves error-prone in practice. Together with the `activity_id` filter already in `list_change_logs`, the docs path closes the practical gap.

---

## 🟢 Testing — federation integration test harness (mock peer + two-instance smoke suite)

The existing federation unit tests in [federation/tests/](federation/tests/) exercise auth functions, middleware, limits, and the FUSE filesystem in isolation, but mock `urllib.request.urlopen` for outbound calls and use Django's in-process test `client` for inbound. Either side can drift away from production behaviour without the suite noticing — URL handling, `iss` normalisation, header forwarding, replay-cache wiring, and the well-known endpoint shape are all candidates for "passes unit tests, breaks federation in production."

### Phase 1 — Mock-peer fixture (✅ landed)

[`federation/tests/conftest.py`](federation/tests/conftest.py) provides a `mock_federated_peer` fixture that stands up a real HTTP server via [`pytest-httpserver`](https://pytest-httpserver.readthedocs.io/), registers a corresponding `FederatedPeer` row, serves a real `.well-known/epicurrents-federation.json`, and exposes a `sign_jwt(audience=..., subject=..., jti=...)` helper for impersonating the peer. SSRF guard is opened (`FEDERATION_ALLOW_PRIVATE_PEER_URLS=True`) for the test scope so loopback addresses pass.

[`federation/tests/test_integration.py`](federation/tests/test_integration.py) demonstrates the fixture with seven passing tests covering:

- Real `.well-known/` fetch through `urllib.request` (replaces the existing `urlopen`-mocked test of `fetch_peer_public_key`).
- Inbound auth at the request layer: valid JWT + grant → 200, tampered signature → 401, JTI replay → 401 on second use, audience mismatch → 401, peer-rotation overlap accepts the `next_key`, untrusted peer rejected even with a valid signature.

Combined runtime: ~2 s. Safe to run on every push.

### Recommended next steps for Phase 1

The current seven tests cover the inbound auth pipeline well, but several federation paths are still mock-only or untested:

- **Recording download with range header.** `_serve_recording_with_middleware` exercises the EDF middleware pipeline plus HTTP range handling — neither is covered by `inbound_check_object`. Build a recording with on-disk EDF bytes, grant the peer read access, request a byte range with the federated bearer, assert range response shape.
- **Outbound peer registration via the API.** `POST /api/v1/federation/peers/` triggers `fetch_peer_public_key` server-side. Use the mock peer as the registration target and assert the resulting `FederatedPeer` row matches.
- **`refresh-key` rotation overlap propagation.** Confirm that flipping `public_key_next` server-side via the rotation endpoint immediately affects which signatures the inbound pipeline accepts.
- **Inbound rate limiter.** Send N+1 requests within the rate window, expect the (N+1)th to return 429.
- **Audit-log writes on inbound failures.** Assert that `FederationAuditLog` rows are written for 401 / 403 / 404 outcomes with the right `target` resolution (the indistinguishability invariant in `inbound_check_object` is a forensics gotcha worth pinning).

Each is ~30–50 lines on top of the existing fixture; doable as a single follow-up PR.

### Phase 2 — Two-instance smoke suite (still future)

A small `docker-compose.federation-test.yml` bringing up two complete stacks on different ports with `mkcert`-issued self-signed certs and `/etc/hosts` entries (`alpha.local`, `beta.local`). The suite is small (5–10 tests) and targets the paths Phase 1 fundamentally cannot prove:

- TLS chain validation on outbound `urllib.request.urlopen()`.
- `iss` claim normalisation across mixed `http://` / `https://` schemes.
- SNI handling if peers share IP infrastructure.
- FUSE filesystem mounting a remote peer's recording — needs a real remote.
- Clock-skew behaviour (single-process Phase 1 has only one clock).

Runs in a dedicated CI job, not on every push.

### Open questions to settle when picking up Phase 2

- **Database isolation between stacks.** Each Docker stack has its own Postgres — standard.
- **CI cost.** Phase 2 adds ~30–60 s of stack boot. Run only on PRs that touch `federation/` or in a nightly job.
- **Local dev ergonomics.** Phase 2 requires `mkcert` installed. Document in `docs/developing.md`. Avoid `verify=False` as a fallback — that's a foot-gun.

### Why this is 🟢 rather than 🟡

The federation surface is well-tested in isolation today, and the production deployment is small (single instance, occasional federation). The cost of *not* having this layer is bounded — a subtle inter-instance bug in production would be caught by user reports rather than production data loss. Worth doing before public release / multi-instance scale-up but not blocking current work.

---

## 🟡 Compute — ship pre-generated lead fields as PWA-cached static files

Standard-montage lead fields are small (10-20 ≈ hundreds of KB, mid-density 10-10 still modest) and identical for every user, so shipping them as static, service-worker-cached assets — like the Pyodide wheels — lets the browser source-localisation script start instantly and offline, falling back to the compute API only for montages/params that aren't pre-generated.

**Format decision.** The static file is the **raw `float64` blob** — byte-identical to the API's `/eeg/leadfield/{montage}/data/` body — with the shape, channel names, and section byte-lengths carried in `manifest.json`. Chosen over a self-describing container (npz) because the viewer fetches and slices the lead field in **JavaScript** (`Float64Array` views) before handing it to Pyodide; a raw blob is trivial to slice in JS and keeps the door open to JS-side lead-field algorithms, whereas npz would need a zip/`.npy` parser in the browser.

**Backend — done.** [compute/eeg/leadfield_io.py](compute/eeg/leadfield_io.py): the raw-blob serialisation, a content hash (arrays + params) for cache-busting filenames, and the manifest-entry builder. The `generate_static_leadfields` command writes the mid-density default set (`standard_1020`, fixed orientation, 7.5 mm) as content-addressed `.bin` files + a `manifest.json`, removing stale hashes on re-run, and caches each row so the existing `/data/` endpoint serves the byte-identical field ad-hoc (the API fallback — no new endpoint needed). Covered by `test_leadfield_io.py` + `test_generate_static_leadfields.py`. The service worker ([frontend/vite.config.ts](frontend/vite.config.ts)) runtime-caches the manifest (StaleWhileRevalidate) and the `.bin` blobs (CacheFirst).

**Generated, not committed — vendored beside the viewer.** Output goes to `frontend/vendor/leadfields/`, served at `/vendor/leadfields/` by [`vendor_view`](epicurrents/views.py) — the same mechanism (and directory tree) that serves the self-hosted Pyodide runtime. That follows the `VENDOR_DIR` rule that vendored assets live *beside their consumer*: lead fields are consumed only by the viewer, are deploy-generated and gitignored (`frontend/.gitignore` ignores `vendor`), and are **served, not bundled** — not part of `collectstatic` or the Vite build, so the old `STATIC_ROOT`/named-volume-shadow problem is gone entirely. `vendor_view` already tags each response with `Cross-Origin-Resource-Policy` (so blobs load under the viewer's `COEP: require-corp` document) and applies exactly the cache split these assets want (`manifest.json` revalidates; content-hashed `.bin` blobs are `immutable`). An umbrella `generate_compute_static` command runs every compute-static generator (just the lead-field one for now) as the single deploy entry point, and both [bootstrap.sh](scripts/bootstrap.sh) and [update.sh](scripts/update.sh) call it beside `vendor_pyodide`, which fills the interpreter side of the same tree. Neither depends on `collectstatic`; both need a migrated database, which is what fixes their position in each script.

**Frontend — shipped, and not where this entry expected.** The manifest fetch, the entry lookup, the byte-slicing and the API fallback are all in platform code at [frontend/src/viewer/leadFields.ts](frontend/src/viewer/leadFields.ts): the viewer no longer knows where lead fields come from, because the platform injects a provider into its setup. The deploy wiring is in place too: both deploy scripts invoke `generate_compute_static`. The original framing: the lead-field fetch lives in the `frontend/viewer` **git submodule** (separate repo; only its built `dist` is in this tree). Change its acquisition to: fetch `manifest.json` (SW-cached) → find the entry for `(montage, n_orient, grid)` → fetch the `.bin` (SW-cached) → slice into `Float64Array`s using the manifest's `lead_field_bytes`/`src_pos_bytes` → **fall back to the existing `/data/` API on miss** (same blob, lengths from headers). Deployment: none extra — `vendor_view` already serves `/vendor/leadfields/` with the `Cross-Origin-Resource-Policy` header, and the SW `/leadfields/` rule matches on that path segment unchanged. Also decide the final montage set (default `standard_1020`; add whichever 10-10 montage was validated for size).

---

## 🟢 Compute — generalise channel-type assumption beyond EEG (PSG / MEG)

[`compute_eeg_lead_field`](compute/eeg/forward.py) currently calls `mne.create_info(ch_names=ch_names, sfreq=1.0, ch_types="eeg")`, hard-coding every channel name from a standard montage as an EEG sensor. This is correct for the current EEG-only use-case — standard MNE montages return only EEG sensor labels in `ch_names` and store fiducials separately — but the assumption needs to be revisited when the compute app gains support for other modalities:

- **MEG.** MEG forward modelling is the more common use of analytical sphere models historically (Sarvas 1987 et al.). A `compute.meg` module would need to mark channels as `mag`, `grad`, or a mix per the MEG montage's actual sensor layout, and the sphere model parameters (radius, centre, conductivities) differ from EEG defaults.
- **PSG.** Polysomnography montages mix EEG, EOG, EMG, ECG, and respiratory channels in a single recording. Only the EEG subset participates in source-localisation; the others have no forward-model meaning and must not be passed to `make_forward_solution` as EEG. The compute side should accept either a pre-filtered EEG-only montage or a typed channel list.

When generalising, also revisit:

- Splitting `compute/eeg/forward.py` into a modality-agnostic core (`compute/_forward.py` or similar) plus per-modality entry points, so the `n_orient`, sphere, and source-space machinery is shared.
- Adding `ch_types` to the `LeadFieldCache` model (or a new per-modality cache table) so the cached row can self-describe which channels are present and of what type.
- Verifying the "source space inside a sphere" choice is still appropriate for MEG — for MEG the Berg series is sometimes used; for PSG only the EEG subset is forward-modelled at all.

Surfaced during the manual audit of the compute app on 2026-05-26 — the EEG-only assumption is documented but not currently obstructed by anything in the design.

---

## 🟢 Compute — check whether MNE community has already discussed the sphere-centre singularity

[`compute_eeg_lead_field`](compute/eeg/forward.py) hits a coordinate singularity in MNE's analytical-sphere forward formula whenever a source grid point coincides with the sphere centre `r0` — the `1.0 / rd2` division in `mne/forward/_compute_forward.py` produces NaN / Inf for the corresponding lead-field column. We work around it with a post-hoc filter that drops affected sources and logs a warning. This is well-known in the EEG / MEG forward-modelling literature (Sarvas 1987 et al.) and almost certainly has been raised on the MNE issue tracker or discourse forum at some point, but a quick search has not been done.

Action: search MNE-Python's GitHub issues, discussions, and the MNE discourse site for prior threads on:

- `make_forward_solution` / `_compute_forward` divide-by-zero or NaN with analytical sphere models.
- Whether `make_forward_solution` or `setup_volume_source_space` should warn / auto-exclude sources at the sphere centre.
- Existing recommendations for "min distance from sphere centre" or "exclude origin" patterns in user code.

Possible outcomes:

- **Prior discussion exists** — link it from [compute/eeg/forward.py](compute/eeg/forward.py) and the [compute README](compute/README.md) so the workaround has provenance, and adopt any upstream-recommended pattern that's stronger than the current post-hoc filter.
- **No prior discussion** — open a discourse thread (not a bug, more "should this raise / warn?") with the minimal reproducer (10 mm grid spacing, sphere centred at `(0, 0, 0.04)`). Add the link back to this entry when filed.
- **Already fixed upstream in a newer MNE version** — bump MNE in [requirements.txt](requirements.txt), drop the workaround, keep the guard.

Low priority — the workaround is correct and observable (the warning fires when it triggers); this is provenance / community-engagement work, not a blocker.

---

## 🟢 Compute — MNI152 cortical surface mesh for 3-D source-localisation view

The current source-localisation 3-D viewer renders activation points over a head-sphere wireframe. An MNI152 cortical surface mesh would give clinical anatomical context that the sphere cannot. Three questions to settle before starting:

1. **Mesh source** — MNE's built-in `fsaverage`, a pre-processed asset shipped alongside the lead-field endpoint, or a WebGL-friendly glTF served from the compute app.
2. **Rendering path** — `mpl_toolkits.mplot3d` inside Pyodide is unlikely to handle a dense surface mesh at interactive frame rates; a dedicated Three.js renderer on the JS side, with mesh data passed from Python, is the more probable approach.
3. **Coordinate alignment** — the source grid uses MNI-like mm coordinates from the spherical head model; any surface mesh must share the same origin and scale.

Likely architecture: Python computes `{pos_mm, power}` source activations and returns them as JSON; JS renders points over a simplified (~5 k vertex) glass-brain mesh using Three.js point sprites with power-coded colour.

---

## 🟢 Compute — EEG processing-tool expansion (SpikeNet + ML models, preprocessing / qEEG)

A literature survey of openly published EEG tools deployable in Python or as Docker containers, plus a phased plan to fold the useful ones into the compute app alongside the lead-field work. Detector scaffolds for SpikeNet 1 and 2 were built against this plan and stayed in the archive repository when the platform went public: their weights are CC BY-NC licensed and the platform ships no limitedly-licensed model artefacts, so re-homing them means the operator-provisioned, sidecar-shaped pattern documented in [compute/README.md](compute/README.md). The engineering notes below carry the full detail:

- Survey of ML models (SpikeNet 1/2, seizure / sleep / foundation models) and preprocessing / qEEG / connectivity toolkits, each with licence, weights availability, and Python/Docker deployability — `eeg-tooling-literature-search.md`, kept in the archive repository.
- Phased integration roadmap (detection MVP → automated preprocessing QC → qEEG feature layer → seizure detection + the SzCORE Docker I/O contract → foundation models), with per-item compute/Pyodide/sidecar placement and licensing gates — **[docs/engineering-notes/eeg-tooling-roadmap.md](docs/engineering-notes/eeg-tooling-roadmap.md)**.
- SzCORE / BIDS integration — storage decision (BIDS as export-only, no layout migration), detector dispatch + communication chain (sidecar-runner model, DERIVE-phase ledger mapping, HED-SCORE → `Event` + `Code`), and transport (materialise-to-tempdir, hardlink not symlink) — **[docs/engineering-notes/szcore-bids-integration.md](docs/engineering-notes/szcore-bids-integration.md)**.
- `to_bids` export — privacy / audit / GDPR threat model for materialising a de-identified BIDS view to an untrusted detector container (reuse the federation de-id boundary, two-entry `DetectorRunAuditLog`, no-DPA analysis for `--network=none`, erasure reachability, load-bearing PHI-leak tests) — **[docs/engineering-notes/bids-export-privacy-design.md](docs/engineering-notes/bids-export-privacy-design.md)**.

---

## 🟢 Normalise hex hash convention to lowercase across recordings + annotations

The platform's public-facing 32-character hashes — `Recording.stored_name` prefix, `AnnotationBase.object_hash` on all four annotation types — are currently stored uppercase. The transform was originally added for display purposes and propagated into normalisation paths over time. It runs against the hex norm everywhere else in the ecosystem: SHA-256 digests, git commit IDs, npm package hashes, IPFS / content-addressed storage all default to lowercase.

### Current state

- [`AnnotationBase.save()`](annotations/models.py) uppercases `object_hash` on every save.
- [Recordings API endpoints](recordings/api/v1/ninja.py) call `(hash or "").strip().upper()` on every URL-kwarg lookup.
- [`_resolve_recording_object_id`](library/api/v1/ninja.py) does the same when accepting recording hashes as collection / dataset / tag items.
- Tests across all three apps pass uppercase literals (`"A" * 32`, `"E" * 32`, etc.).

### Why change

Consistency with content-addressed storage conventions outside the platform. A hash that lands in a log line, a SIEM event, an external script, or a federated peer's database should look like a hash, not like something with a custom display convention. Lowercase is also what every other tool the project integrates with produces (git, npm, the OS-level `sha256sum`).

### Migration plan

1. Strip the `.upper()` call from `AnnotationBase.save()`.
2. Strip the `.strip().upper()` chain from every recordings endpoint lookup. Keep `.strip()`.
3. Strip `.upper()` from `_resolve_recording_object_id`.
4. Switch hash-based lookups to case-insensitive (`stored_name__istartswith`, `object_hash__iexact`) so existing-uppercase rows and incoming-lowercase rows both resolve.
5. One-shot data migration: `UPDATE annotations_annotation SET object_hash = LOWER(object_hash)` and equivalents for `Event`, `Interruption`, `Label`, and the recordings `stored_name` column. Run inside the deploy window so the case-insensitive lookups don't outlive the need.
6. Update tests across `recordings/tests/`, `annotations/tests/`, `library/tests/` to use lowercase literals (mechanical sed pass).
7. Drop the case-insensitive lookups once a release window has passed and any cached / bookmarked uppercase URLs are confirmed obsolete.

### Why not now

Orthogonal to any single feature, touches ~6 files plus their tests, and any frontend URL bookmarks or third-party scripts that copied a hash from a URL would break case sensitivity if we don't include the case-insensitive lookup step. Best done as a deliberate dedicated commit + data-migration commit pair, not folded into unrelated work.

---

## 🟢 Infrastructure — make the web image's entrypoint skip the postgres wait for DB-less commands

[entrypoint.sh](entrypoint.sh) unconditionally blocks on `nc -z $DB_HOSTNAME $DB_PORT` before executing the passed command. Every DB-less management command — `init_env`, `generate_vapid_keys`, ad-hoc Django shell invocations that don't touch models — has to bypass the entrypoint with `--entrypoint ""` (or `--entrypoint python`, the form [scripts/bootstrap.sh:184](scripts/bootstrap.sh#L184) uses). This is documented in [docs/epicurrents/src/docs/latest/platform/deployment.md:39](docs/epicurrents/src/docs/latest/platform/deployment.md#L39) as the operator workaround, and it works, but it leaks a Docker-quirk into every operator's muscle memory and produces friction in alternate runtimes (`podman compose` is fussier about `--entrypoint ""` syntax than Docker Compose).

### Options

- **Env-var opt-out.** `SKIP_DB_WAIT=1` in the environment short-circuits the wait. Simplest change; every DB-less compose run sets the var inline.
- **Command sniff.** The entrypoint inspects `$1` (and maybe `$2`) against a known set of DB-less management commands and skips the wait when matched. No env-var burden on callers, but the list becomes a source of truth that drifts.
- **Bounded timeout.** Replace the infinite loop with a 30s timeout so a misconfigured DB stops hanging the run forever. Doesn't solve the friction but does solve the related operator hazard of "init_env hung — what's wrong?".

Recommend the env-var opt-out plus a bounded timeout; the sniff approach inverts the responsibility wrongly (the entrypoint shouldn't know which management commands need a DB).

### Touchpoints

- [entrypoint.sh](entrypoint.sh) — the wait loop.
- [scripts/bootstrap.sh](scripts/bootstrap.sh) — the canonical `--entrypoint python` workaround, now at line 435; two more call sites have appeared since, in [scripts/bootstrap-podman.sh](scripts/bootstrap-podman.sh) and [scripts/make-bootstrap-fixture.sh](scripts/make-bootstrap-fixture.sh). All three could drop the override once the env var lands.
- [docs/epicurrents/src/docs/latest/platform/deployment.md:39](docs/epicurrents/src/docs/latest/platform/deployment.md#L39) — operator doc that names the workaround; needs to learn the env-var form.

---

## 🟢 Infrastructure — revert to single `data` volume + `volume.subpath:` once Podman's Docker-API translates it

The compose files used to declare a single `data` named volume and reference per-domain subpaths via `volume.subpath:` (postgres / recordings / staging / celery / borg). The structure was reverted to five independent named volumes because **Podman's Docker-API socket drops `subpath:` when translating Compose mounts to native mounts**: Postgres mounts the whole data volume root, sees the sibling subdirectories created by `init-volumes`, and refuses initdb. docker-compose v2/v5 emits the spec correctly; the gap is in Podman's API implementation. Verified end-to-end against Podman 5.8.2 + docker-compose 5.1.4 on RHEL 9.

### Why revert eventually

The subpath shape is the more operator-friendly default: one volume to back up, one volume to provision via a Docker volume driver (NFS / cloud storage / encrypted block device), and the runtime composes the subdirectory layout. The per-domain volumes work for every runtime today but multiply the operator's backup and provisioning surface by 5×, which matters for any deploy that lives behind a managed-volume driver.

### When to revert

Watch upstream Podman for the volume-subpath translation landing in its Docker-API compatibility layer. Search [github.com/containers/podman/issues](https://github.com/containers/podman/issues) for `volume.subpath` and the matching `compatibility` label.

### How to revert

Mechanical:

1. In [docker-compose.yml](docker-compose.yml), collapse the seven top-level volumes (`postgres-data`, `redis-data`, `recordings-data`, `staging-data`, `media-data`, `celery-data`, `borg-data`) back into a single `data:` entry. Take the list from the file rather than from here — `media-data` and `redis-data` were added after this recipe was written, and a collapse that omits one silently leaves its data behind.
2. Restore the long-form mount block (`type: volume` / `source: data` / `volume.subpath:`) on every service: `db` (postgres), `web` (recordings, staging), `celery` (recordings, staging, celery), `borg` (recordings ro, borg).
3. Restore `init-volumes`'s shared-data mount + `mkdir -p` for each subdirectory, plus the `depends_on init-volumes` block on `db`.
4. Repeat the matching changes in [docker-compose.prod.yml](docker-compose.prod.yml) — same five `subpath:` mounts on `web` and `celery`.
5. Remove the inline reversion comment block at the top of `docker-compose.yml`.

Both shapes work on Docker Engine ≥ 25; the revert simply trades 5 volumes for 1.

---

## 🟡 Infrastructure — extend the byte offload to media downloads

Recording downloads offload; media downloads still stream through a gunicorn thread. The mechanism is already built and media is the easier case — `media/api/v1/ninja.py` applies no middleware at all, so every media file is offload-eligible and the interlock is trivially satisfied.

What is not yet settled is the path mapping. `MEDIA_UPLOAD_PATH` is `/data/media/uploads`, a *subdirectory* of the `media-data` volume, and Docker cannot mount a volume subpath (the same Podman limitation documented at the top of [docker-compose.yml](docker-compose.yml)). So either the proxy mounts the whole volume and Django emits volume-root-relative paths — which puts the staging directory inside the proxy's root, where a path-mapping bug becomes an exposure rather than a 404 — or the media layout changes. Decide that before wiring it.

Also worth folding in: `_serve_media_file` returns `(response, range_start)` so the caller logs only on `range_start == 0`, deliberately not writing one audit row per video seek. Offloaded responses must preserve that sampling rather than quietly losing it.

### Why yellow

Media files are documents today — small, and nothing like the multi-hundred-megabyte recordings that motivated the offload. It becomes worth doing when a project enables video.

---


## 🔍 Investigate a native notification companion app

**Motivation.** A teaching project's feedback workflow has a delay built in — students submit their annotations, the instructor reviews later (hours to days), the student needs to find out when feedback is ready. Web push from a browser tab works on desktop and Android, and on iOS only via the PWA-installed flow. For students who never install the PWA, never get notifications. A native companion app would give reliable cross-platform push without depending on the user installing a home-screen shortcut.

**The question.** Is a thin notification-only native app worth the engineering cost, or is the existing PWA path enough if we document it well?

**What "minimal" could look like.** Strictly a notification relay — no platform UI inside the app:

1. App registers for native push tokens (APNs on iOS, FCM on Android) on first launch.
2. App sends the token to a new `/api/v1/notifications/native-subscribe` endpoint, scoped to the authenticated user.
3. `send_push_to_user` learns to dispatch to both web push and native push targets for a user, using the existing `PushSubscription`-style storage extended with a `kind` field.
4. Tapping a notification opens the platform site in the system browser (or the PWA if installed), pre-filled with the action target — e.g. `/project/<name>/feedback/{token}`.

The app itself is essentially a launcher with a login screen. No EDF viewing, no annotation UI, no offline functionality.

**Implementation options, ordered by effort.**

| Option | Effort | Wins | Costs |
|---|---|---|---|
| **Capacitor** wrapper around the existing Vue frontend | Lowest | Single codebase. Native push, badges, deep links. The wrapped web app is the same Vue build, so all features work on day one. | One additional build pipeline, App / Play Store submission process. |
| **Expo / React Native** thin app | Medium | Cleaner native feel. Smaller bundle than Capacitor. | Separate codebase from the web app — duplicate the login + push registration UI. |
| **Native Swift + Kotlin** notification apps | Highest | Fullest control, smallest binary. | Two codebases, two toolchains, dedicated maintenance. |
| **Third-party relay** (ntfy, OneSignal, Pushover) | Lowest infra, highest data risk | No native app to maintain. | User PHI / clinical context flows through a third party. Probably a dealbreaker for clinical deployments; possibly acceptable for a non-clinical teaching deployment. |

**Capacitor is the most credible "minimal" path.** The existing Vue frontend would run unchanged inside the native shell, with a small Capacitor plugin handling the push token registration and `notificationclick` deep-link routing. Both stores accept it as a regular app.

**Open questions.**

- Does a single-institution teaching deployment's scale (predictable user count) justify the App Store / Play Store accounts and yearly Apple developer fee?
- Would students actually install a project-specific app, or is the PWA install friction roughly equivalent to app-install friction in practice?
- Notification badge counts are an iOS-only feature; is "you have N feedbacks waiting" worth the trade-off vs. just sending one notification per feedback?
- Does any future use of the platform (clinical deployment, federated research) also benefit, or is this purely a teaching concern?

**Recommendation for the investigation.**

1. First, ship the iOS PWA documentation entry above and measure: how many iOS users of such a deployment install as PWA voluntarily? Could be a settings-page flag or just survey data.
2. If PWA install rate is low and notification engagement is bottlenecking that workflow, prototype a Capacitor wrapper in a branch. The wrapper itself is a 1–2 day exercise; the surrounding plumbing (token registration endpoint, `send_push_to_user` dual-dispatch, Store submission, certificates) is the bulk of the work — call it 1–2 weeks for a polished v1.
3. If PWA install rate is acceptable, defer indefinitely.

Not currently blocking — but a teaching workflow is the most concrete justification the platform has for native push, and worth deciding intentionally rather than letting it drift.

---

## 🔍 Investigate activity / changelog API user-data isolation (GDPR Art. 15 inverse)

**Motivation.** GDPR Art. 15 gives every data subject the right to see what is held about them. The audit trail and changelog rollback APIs in [activity/api/v1/ninja.py](activity/api/v1/ninja.py) are the natural surface for that right — a user pulling their changelog gets the "everything that happened to my data" feed. The inverse failure is the dangerous one: a regression that lets user A see entries about user B's data is a silent compliance breach, not a user-visible bug.

The audit trail is comprehensive (intentionally — that's the whole point of the load-bearing audit-trail layer), so the scoping decision sits on the API read side. There are two scope flavours to keep separate:

- **Entries the caller performed** — `Activity.actor = caller`. Simple identity filter.
- **Entries that targeted the caller's data** — `ObjectChangeLog.target_object_id` resolves to an object the caller can read. Requires walking the access-rights model.

A regression in either filter shows the wrong other-user entries; a regression that mixes the two could silently merge them, producing a feed that looks right to the user but to a passing auditor reads as a privacy breach.

### The investigation

1. Read the activity ninja module end-to-end and enumerate every endpoint that returns `Activity` or `ObjectChangeLog` rows — list / detail / rollback / bulk-rollback / changes feed.
2. For each, identify the current filter: is it `actor=caller`, `target ∈ caller-readable`, or `is_staff` short-circuit? Document the matrix.
3. Identify which filters are mechanical (one ORM `filter()` call) vs. which delegate to `can_read_object` / `get_read_access_result` / a permission helper.
4. Decide whether any filter looks too loose for GDPR Art. 15 inverse — e.g. a staff endpoint that surfaces everyone's entries (legitimate, but should be staff-gated explicitly), or a permission-helper call that doesn't propagate `apply_middleware` in a way that affects what fields land in the response.
5. Confirm test coverage — does any contract test currently assert "user A cannot see user B's changelog entry"? If not, that's the gap.

### Possible outcomes

- **No real gap.** The current filters cover both scope flavours correctly; we add a contract test that pins the inverse-read invariant and add `activity/api/v1/ninja.py` to the load-bearing table. Smallest landing.
- **Filter gap on a specific endpoint.** Fix the filter, add the contract test, add to the load-bearing table. Mid-sized landing depending on which endpoint.
- **Architectural gap.** The scoping model needs to choose explicitly between actor-scoped vs. target-scoped feeds, or merge them with documented precedence. Larger landing that touches the API shape and probably the frontend that consumes it.

### Why investigation, not direct addition

The contract test set depends on what the current code does. Sketching contract tests without first knowing the existing filters would either re-derive the obvious cases (and miss the subtle ones) or pin the wrong invariant. The investigation is small — read the module, write the matrix, decide.

### Why this matters

The audit trail surface is normally thought of in terms of completeness (every action logged, the audit-trail-completeness reviewer covers this). The Art. 15 inverse is the other side: every action logged means every action *readable* if scoping breaks. A two-sided invariant deserves a two-sided guard, and right now only one side is load-bearing-flagged.

---

## 🟢 Security sweep follow-ups — deferred items from the 2026-06 sweep

The P0–P3 remediation batch closed every code-level finding; these are the
items deliberately left open, recorded here so the deferral is visible
rather than forgotten. Green-lit work is indexed in the Contents tiers
above; the rest is documented as considered-and-declined.

### Green-lit (indexed above)

- **Compiled lockfile** (Medium) — *shipped: [requirements.lock](requirements.lock).* `constraints.txt` pinned CVE floors for
  transitives, but every image build still resolved fresh. A hashed
  lockfile makes the audited environment the deployed environment.
- **Lean production image** (Low) — *shipped: the multi-stage Dockerfile.* It was blocked on the multi-stage build
  noted in [docs/developing.md](docs/developing.md); the
  `test`-profile service was why test deps lived in the runtime image.
- **Restore drill** (Low) — *shipped 2026-08-24: [scripts/restore-drill.sh](scripts/restore-drill.sh).* The `borg-restore` extraction path was pinned
  by dry-run tests only; a real archive restored into a scratch stack is
  the proof that counts.
- **Periodic full-surface PHI sweep** (Low) — the per-commit `phi-exposure`
  gate is diff-scoped by design; latent gaps in untouched code only
  surface through the opt-in `full-surface` mode.
- **Lead-field computation to Celery** (Low) and **psycopg 3 migration**
  (Low) — both flagged during the sweep, both feature-sized.
- **`can_share` delegation semantics** (Investigation) — **closed
  2026-09-02: capped.** The publication review settled the product
  question: the uncapped form let a de-identified share-holder grant
  *themselves* `apply_middleware=False` and read a dataset's raw bytes
  through the read extension — a PHI exposure, not a delegation-style
  preference. Grants now cap at the delegator's own rights
  ([epicurrents/granting.py](epicurrents/granting.py)): write requires write, raw requires raw,
  conferred expiry cannot outlive the delegator's share expiry, expired
  share rows qualify for nothing, share-token rows refuse write/share
  for everyone, and the author's own row is revocable only by the author
  or a superuser. Contract tests in
  [test_grant_capping.py](library/tests/test_grant_capping.py).

### Considered and declined

- **Per-service env files / Docker secrets.** Every app container
  currently receives the full secret set (celery-beat sees the federation
  private key it never uses). Splitting env files per service is invasive
  compose restructuring for modest gain while all containers run the same
  first-party code — revisit if a third-party container ever joins the
  app network.
- **fusepy replacement.** Upstream is unmaintained, but it is a thin
  ctypes wrapper, optional, and not network-facing by itself. Acceptable
  while the FUSE mount stays an optional operator feature; revisit if it
  graduates to a supported deployment mode.

---

## 🟡 Recordings — retire `preserve_annotations` in favour of permissioned annotation publication

`preserve_annotations` was built for a teaching use: keep the original clinical annotations in the recording so students meet a case as a clinician would. The goal is right and the mechanism is wrong, in a way that only became visible once a project's raw-serving grants were examined alongside it.

Annotation text is **always** extracted to the database at ingest, whatever `preserve_annotations` is set to — `HeaderPipelineOptions` documents this. The annotations an instructor wants shown are therefore already stored and already behind the permission checks that govern every other annotation. `preserve_annotations` adds a second copy *inside the file*, where no permission check reaches, and a project's `apply_middleware=False` grants serve that copy verbatim to any holder of a share link.

So the flag is strictly worse than the database copy even for its own purpose. It gives the instructor no control over which annotations are shown, no way to withhold a diagnosis until after submission, and no audit of the disclosure — while exposing everything the acquisition system happened to record.

### The replacement

The project side is a session-planning UI that surfaces the extracted annotations and lets the instructor publish a chosen set to a session, as an ordinary permissioned grant over `annotations.Annotation` rows. That gives the authentic-case experience the flag was reaching for, plus selection, timing (withhold the reading until submissions are in) and an audit trail. The viewer needs no change: it already renders annotations it is allowed to read.

### Until then

Ingest strips by default, upload preserves nothing by default, and no core code requests preservation; the project that motivated this pins those facts, and the raw-serving posture of its grants, in its own tests. A deployment can now refuse the parameter outright: `RECORDINGS_ALLOW_PRESERVE_ANNOTATIONS=False` makes the upload endpoint, the Celery task and `import_recordings` each reject it. Under the shipped default of `True`, though, nothing stops an author passing it on an individual upload.

Closing that properly needs a persisted marker on `Recording` recording whether the stored file retains annotation text, so the serving path can refuse to hand a retained-text file to a raw grant. There is no such field today, and the only trace is `preserve_annotations` in the `recordings.process` Activity metadata, which is an audit record and has no business gating a serving decision. Add the field with the publication work rather than before it — once publication exists, the flag has no remaining use case and can be retired instead of guarded.

---

## 🔍 Investigate type-checking the frontend test files (nothing does today)

**Symptom.** Surfaced on 2026-08-26 while adding a required `is_2fa_enabled` field to `AuthUser`. [frontend/src/stores/auth.test.ts](frontend/src/stores/auth.test.ts) declares a `const STUDENT: AuthUser = { ... }` literal that does not satisfy the interface with that field on it. Both gates stayed green: `vue-tsc -b` never saw the file, and vitest strips types rather than checking them. It was found by reading, which is not a gate.

**Cause.** [frontend/tsconfig.app.json](frontend/tsconfig.app.json) excludes `src/**/__tests__/**`, `src/**/*.test.ts` and `src/**/*.spec.ts`, and no other project in the references picks them up — [frontend/tsconfig.node.json](frontend/tsconfig.node.json) covers only [frontend/vite.config.ts](frontend/vite.config.ts), [frontend/mocks.ts](frontend/mocks.ts) and one declaration file. So the test files belong to no compilation unit at all. The exclusion is right on its own terms (the test files import vitest globals the app build should not see); what is missing is the second project that claims them.

**Open questions.**

- Add a `tsconfig.vitest.json` extending the app config, including the excluded patterns plus `"types": ["vitest/globals"]`, and reference it from [frontend/tsconfig.json](frontend/tsconfig.json) so `vue-tsc -b` builds it too. Confirm that is enough, or whether vitest's own transform needs the same config pointed at it.
- Find out what it surfaces first. Three test files have been written with no type checking over them (the `AuthUser` literal that motivated this entry has since been fixed), so the first run is likely to report pre-existing errors unrelated to whatever prompts the change — worth knowing the size before starting.
- Decide whether [frontend/mocks.ts](frontend/mocks.ts) deserves the same look. It is type-checked, via [frontend/tsconfig.node.json](frontend/tsconfig.node.json), but shares no types with the API client it imitates, which is how its `GET /me` handler drifted to returning a bare user while the SPA read `data.user`. A shared response type would have caught that; the mock deliberately not importing from `src/` is what prevents it.

**Timing.** Not urgent and not blocking. The failure mode is a stale test literal compiling silently, which costs a confusing test failure at worst — but it is a gate that is not running, and the cheap fix may not be cheap once the backlog is visible.

---

## 🔍 Additional authentication methods — to investigate

Two optional authentication paths raised during the 2026-06 security
discussion. None is committed; each is recorded here for evaluation. They
share one enabling prerequisite, which should land first.

### Enabling prerequisite — pluggable authentication layer

Login today is a single `authenticate()` + `login()` call in
[user/api/v1/ninja.py](user/api/v1/ninja.py) against Django's default
`ModelBackend`; `AUTHENTICATION_BACKENDS` is unset and there is no
`RemoteUserMiddleware`. Each method below is an *additional* or
*alternative* path, so the high-leverage first step is a pluggable
authentication-backend layer — populate `AUTHENTICATION_BACKENDS` and let
a deployment or project plugin register extra login routes/backends —
rather than three bespoke patches to the user app. This also subsumes the
existing Medium-tier TOTP item, which becomes the in-house baseline factor.

### Norwegian BankID as an additional factor

Modern BankID is consumed as a standard OIDC authorization-code flow
through a broker (Vipps MobilePay operates BankID OIDC; Signicat is the
common aggregator). The platform already validates JWTs for federation, so
the token-verification machinery exists. Store the broker-scoped pseudonym
(PID) or a salted hash, never the raw fødselsnummer — the national ID
number is itself sensitive under Norwegian law. The long pole is a broker
merchant agreement, not the code; brokers provide sandbox test users so it
is developable before the contract is production-grade.

### Windows integrated login (Edge / Kerberos)

Edge and Chrome on a domain-joined Windows machine perform Negotiate /
SPNEGO automatically against a site in the Local Intranet zone, passing
the Windows logon identity with no prompt. Preferred shape: terminate the
Kerberos handshake at the reverse proxy (`mod_auth_gssapi` on Apache, or
an nginx SPNEGO module) and pass the principal to Django as `REMOTE_USER`
via `RemoteUserMiddleware` + `RemoteUserBackend` — keeping GSSAPI out of
the Python process. Requires a domain-joined deployment with an `HTTP/`
SPN and keytab in AD, and the site in clients' Intranet zone. It works
only for domain-joined Windows + a configured browser, so it must stay
optional alongside password and BankID. Prefer Kerberos over NTLM, require
HTTPS, and treat `REMOTE_USER` with the same trust discipline as
`X-Forwarded-Proto`: the proxy must be the only path to the app and must
strip any client-supplied header, or a forged `REMOTE_USER` is an auth
bypass. Worth a contract test on that invariant if built.

---

## 🔍 Federation — optional Tailscale network layer

An optional WireGuard tailnet as network-layer defence-in-depth for
inter-instance federation. The finding that decides the shape: federated data
flows **server-to-server** — a user's home instance relays peer bytes and the
browser never contacts the peer — so **only the instances join the tailnet;
users do not**. Recommended shape: a Tailscale sidecar behind a compose
profile, the `web` container sharing its network namespace, and `tailscale
serve` for a real Let's Encrypt cert on the `*.ts.net` MagicDNS name. The one
code change is a narrow, settings-gated carve-out in the federation SSRF guard
([federation/auth.py](federation/auth.py)) so peer URLs in CGNAT space
(`100.64.0.0/10`) are accepted — they are non-global and the guard rejects them
today. The Ed25519 per-peer JWT auth stays the authority (a tailnet node
identity is a software secret and can be cloned), so this hardens the wire but
never replaces application-layer trust. Strictly optional.

Full data-flow trace, topology, the three configuration gotchas (SSRF range,
strict TLS solved by MagicDNS certs, `iss`/`aud` pinning), the security
posture, and an implementation checklist in
**[docs/engineering-notes/federation-tailscale.md](docs/engineering-notes/federation-tailscale.md)**.

---

## 🟡 Hardening, setup, and CI backlog — 2026-06 review

A review pass after the security sweep and the diagnostic-logging work
surfaced these. The serious security holes were already fixed; what follows
is the next tier of hardening, the setup-convenience gaps for less technical
operators, and the path to full-stack CI. Recorded here so they don't have to
be re-derived.

### Security

- **Content-Security-Policy.** *(Shipped 2026-08-28 — enforced by default; procedure in [docs/operations.md → Security headers](docs/operations.md#security-headers).)* Production set HSTS, `nosniff`, X-Frame, and COOP/COEP-when-enabled, but no CSP, Referrer-Policy, or Permissions-Policy. For a Vue SPA that mounts the viewer and renders user-supplied annotation content, a baseline `default-src 'self'` CSP (via `django-csp` or a middleware, tuned for the viewer) is the main XSS blast-radius reducer. Highest-value new security item.
- **Global API throttling.** *(Shipped 2026-06-13 — [epicurrents/throttle.py](epicurrents/throttle.py).)* Login (per-username), password-reset (per-email), and federation (per-peer) were the only rate-limited paths; uploads, annotations, library, and the anonymous share-token reads had no ceiling, leaving share-token enumeration and abusive load unbounded. `ApiThrottleMiddleware` now caps the whole API surface per identity, resolved in priority order — authenticated user → `share_token` → session → client IP — so a NAT'd shared-egress population (a classroom on one access point, a hospital behind a proxy) is keyed per identity rather than per address. The IP tier is a last resort with its own high ceiling. The original constraints all hold in the implementation: project-disablable and per-scope tunable via `API_THROTTLE_ENABLED` / `API_THROTTLE_RATES` / `API_THROTTLE_SCOPE_MAP` / `API_THROTTLE_IP_RATE` (on in production, off in development); identity-keyed over IP; the IP ceiling can be zeroed to defer IP-level limiting to the reverse proxy. Fails open on cache error, emits `throttle.rate_limited`, and returns 429 with `Retry-After`.
- **Full CSRF enforcement.** *(Shipped 2026-06-13 — [epicurrents/auth.py](epicurrents/auth.py).)* The Ninja mounts use `auth=None` plus a manual `_require_auth(request)` that reads `request.user`, so no Ninja auth class runs and `csrf=True` would be a no-op — Django Ninja enforces CSRF only inside its auth classes (`check_csrf` in `APIKeyCookie.__call__`). The chokepoint approach landed: `enforce_session_csrf(request)` runs Django's token check for unsafe methods on session-cookie callers, and every per-app `_require_auth` helper plus the session branch of `_require_auth_or_federated` calls it. FederatedBearer-JWT and share-token paths authenticate outside the chokepoint and are exempt by construction (a federated peer or anonymous share-token writer has no CSRF token). Frontend plumbing: axios is configured with `xsrfCookieName: "csrftoken"` / `xsrfHeaderName: "X-CSRFToken"`, and the served `index.html` seeds the `csrftoken` cookie. Gated by `SESSION_CSRF_ENFORCED` (production on, development off because the cross-origin Vite SPA and host tooling cannot echo the token). The Django test client's CSRF exemption keeps the suite green; `csrf-coverage` is a new review agent guarding future endpoints. The alternative — a sweeping Ninja `django_auth` refactor across every endpoint's auth declaration — was rejected as higher-risk for the same protection.
- **Disclosure policy.** *(Shipped 2026-06-13 — [SECURITY.md](SECURITY.md).)* For a PHI platform a missing security-disclosure document is both a governance gap and a practical one — a researcher has nowhere to report. The policy covers private reporting, response expectations, scope, supported versions, and PHI/secret handling; the contact address is a placeholder to set before publishing.

### Dependencies

- **Automated update PRs.** *(Shipped 2026-06-13 — [.github/dependabot.yml](.github/dependabot.yml).)* Version pins rot between manual audits. Dependabot now opens grouped weekly PRs for the pip, npm, docker (Dockerfile base), and github-actions ecosystems. Still manual: docker-compose image pins (Dependabot's docker ecosystem scans Dockerfiles only) and the git submodules, which carry their dependencies in their own repos.

### Setup convenience and AI-assisted management

[scripts/bootstrap.sh](scripts/bootstrap.sh) is already a clean two-pass one-script setup, and the AGENTS.md getting-started flow gives an AI assistant a guided routing script. Two additions serve the less-technical operator (and the assistant driving on their behalf):

- **Single discoverable entrypoint.** A Makefile or justfile with named targets (`setup`, `up`, `logs`, `backup`, `restore`, `doctor`) so the management surface is discoverable via `make help` rather than requiring knowledge of every script in the scripts directory. Machine-readable, named commands are also far easier for an AI assistant to drive than free-form invocations.
- **`manage.py doctor` preflight.** The boot guards in [epicurrents/apps.py](epicurrents/apps.py) refuse to start on a few placeholder secrets; a friendly preflight that checks every required `.env` value, the Docker version, free ports, and DB / Redis reachability — printing plain-language fixes — catches misconfiguration before cryptic boot failures.

### Testing and CI

The end-target is a CI pipeline that builds the image, runs the setup scripts and init, and then runs unit + integration tests against the real stack. Current CI is host-only (SQLite / test settings) and never builds or runs the Docker stack, so a class of real-deploy breakage (e.g. an image that omits an app) is invisible. A staged path:

1. **Stack smoke job** — *(shipped: the `stack-smoke` and `bootstrap-fixture-smoke` jobs in [.github/workflows/ci.yml](.github/workflows/ci.yml))* `docker compose build` → `up -d` → wait for the health endpoint → `migrate` + `createadmin` → assert health → tear down. Validates the image build and init path; smallest step, catches the most.
2. **Compose integration suite** — exercise cross-service flows against real Postgres + Redis + Celery (upload → `process_recording` → download, an annotation round-trip, a two-instance federation handshake). The compose `test` profile service is the hook. Builds on the existing *Testing — integration-test layer for cross-app contracts* and *Testing — federation integration test harness* entries.
3. **Frontend in CI** — run `vitest` and `vue-tsc` / `vite build`. *(vitest runs in the `frontend` job; the typecheck and build still do not, so a broken frontend build still merges green.)*
4. **End-to-end and Tier 3** — a Playwright suite against the running stack, and a real-container run of [scripts/bootstrap.sh](scripts/bootstrap.sh) (the existing *container-based integration tests for the bootstrap pipeline (Tier 3)* entry).

Sequence the heavy Docker jobs (PRs to main + nightly) separately from the fast unit / lint jobs (every push) to keep CI time and cost bounded.

### Other

- **Restore drill in CI.** [scripts/restore-drill.sh](scripts/restore-drill.sh) validates the restore path end-to-end and runs on demand; wiring it into the integration-CI tier would catch a regression in the backup or restore path without anyone remembering to run it.
