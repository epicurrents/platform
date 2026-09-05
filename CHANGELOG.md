# Changelog

Notable changes to the Epicurrents platform. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the versions are [semantic](https://semver.org).

Below 1.0 the *minor* is the breaking bump, which is semver's rule for initial development and what the platform is in — a project pins `>=0.1,<0.2`, not `<1`. What a version promises at all is narrower than the whole codebase, and is written out in [epicurrents/README.md → Versioning and the platform pin](epicurrents/README.md#versioning-and-the-platform-pin). A change that breaks something outside that surface is not a major bump, and a project pinning `>=0.1,<0.2` is not protected from it.

Entries are written for the person deciding whether to upgrade, so the ones that matter are removals, renames and changed behaviour. A project reading this needs to know what it has to change, not what was added for someone else.

## [Unreleased]

### Added

- [make-bootstrap-fixture.sh](scripts/make-bootstrap-fixture.sh) gained `--tarball`, which packs the assembled package into `<dest>.tar.gz` in one step, stamped as owned by uid/gid 1000 — the account every container runs as. Packing by hand records the *builder's* uid instead, and since an update applies the archive as root, that uid travels onto the deployment and locks its own account out of the tree. The failure surfaces later as a permission error from a container, far from its cause.

- Federation can run over a private overlay network without turning the SSRF guard off. `FEDERATION_ALLOWED_PEER_CIDRS` lists the networks a peer URL may resolve to despite not being globally routable — `100.64.0.0/10` and `fd7a:115c:a1e0::/48` for a Tailscale tailnet — and every other non-public address stays refused. The alternative was `FEDERATION_ALLOW_PRIVATE_PEER_URLS`, which disables the guard for all of them and is documented as never for production, so a deployment whose peers are reachable only over its own overlay had to choose between federating and keeping the guard.

  Empty by default, so a deployment that does not set it is unchanged. Entries must be network addresses, and a default route is refused: listing one disables the guard rather than narrowing it. NAT64 translation prefixes stay refused whatever is listed. The guard is shared, so the carve-out reaches every caller of `check_url_is_safe`, not only peer fetches.

- The viewer's source-localisation tool works in the platform SPA. It never could: the lead-field provider is what tells the viewer where lead fields live, and only the per-project viewer build supplied one, so the SPA — which loads the builder's full edition — reported every montage as unavailable before looking one up. `ViewerView` now passes the provider in the setup it hands to the viewer.

  That needed the interface package to publish declarations, which it also never did: its `exports` map named `dist/index.d.ts` and no such file was emitted. It now emits them for every subpath, so a host can name `SetupContext`, `LeadFieldProvider` and the rest instead of reaching into a file path or casting. Details, and the two traps in how they are produced, are in the interface package's README.

- The source-localisation tool works in the public viewer at `/viewer/<mode>` too, which the change above did not reach. That page builds its SETUP with `json.dumps` from `PUBLIC_VIEWER_MODES`, and a provider is a function, so no value in that setting could ever have carried one. It now loads the provider as a small standalone script built beside the viewer bundles, between its SETUP declaration and the lib.

  The provider also stops treating an unauthenticated compute API as a failure. The public page is auth-free by design and can never reach those endpoints, so a montage missing from the static bundle used to offer its users a retry that could not succeed; 401 and 403 now read as "not available on this server", the same as a 404.

- `manage.py vendor_pyodide` populates the self-hosted Pyodide runtime the viewer's Python analysis tools load from `/vendor/pyodide/<version>/`. The tree had no producer: it was described in the settings and the operations guide as vendored at deploy, but nothing in the repository created it, so a deployment that had not had it placed there by hand failed at the first request for the interpreter — visibly only in a browser console.

  It vendors the runtime core plus the closure of what the viewer loads (26 of the distribution's 354 packages, ~47 MiB) and writes a pruned `pyodide-lock.json` matching what is on disk. mne comes from PyPI, since Pyodide un-bundled it after 0.28. `--check` verifies an existing tree without downloading. `bootstrap.sh` runs it; `update.sh` runs `--check` and re-vendors only on failure, which also repairs a restored snapshot — the tree is excluded from the update rsync and, unlike `static/`, nothing else regenerates it.

  Both generators run in a `vendor` compose service rather than in `web`, which mounts the tree read-only: web serves the interpreter every viewer session executes, so it is deliberately the one container that cannot rewrite it.

- Both deploy scripts now produce the whole vendored asset tree, not half of it: `generate_compute_static` runs alongside the Pyodide vendoring, so the pre-computed lead fields the viewer's source-localisation tool prefers are present on a deployment rather than only on a machine where someone had run the generator by hand. It needs a migrated database, so `bootstrap.sh` runs it after the stack is up; `update.sh` runs it after migrations. A deployment without it still worked — the tool falls back to the compute API — but computed each montage per request and lost offline use.

- Art. 15 subject access export: `manage.py export_user --username <name>` produces a plain-text document listing everything the platform holds about one person, as the mirror of `erase_user`. `--format json` gives the machine-readable form for an Art. 20 portability request. Every relation to the user model is classified, and `manage.py check` reports one that is not — a project or plugin registers its own with `user.export.register_export_relation` from `AppConfig.ready()`.

  Credentials are excluded by reusing the masked-field registry, so `register_masked_fields` now covers the export too. `activity.audit` gained `registered_masked_fields()` as the public accessor for that.

- A deployment can be put on a tailnet as a **host**, not only as a container: scripts/tailscale-join.sh, `bootstrap.sh --tailscale-authkey` (which now defaults to it), and the same flags on a distribution's `start.sh`. This is the only arrangement in which containers can reach the tailnet, so it is what the evidence-host log shipper needs; the previous container-only mode publishes the web UI inbound and routes for nothing else.

  A distribution now also bundles both tailnet scripts, tailscale/serve.json (whose absence made the container mode start with an empty directory where its serve config should be), and the application half of examples/evidence-host/ — so a tarball deployment can ship its security stream off-host without a checkout.

- `TS_OUTBOUND_HTTP_PROXY_LISTEN` and `TAILNET_PROXY_URL`, both empty by default. Together they let the compose tailscale container give a sibling container outbound tailnet access, so a host where packages cannot be installed can still ship its security log. Verified end to end against a container on a network with no egress at all.

  It covers HTTP only, so remote Borg over `ssh://` still needs Tailscale on the host, and each client must name the proxy in its own configuration — `HTTP_PROXY` in the environment is not enough for promtail, which ignores it.

- `BORG_SSH_KNOWN_HOSTS_PATH`, mounted into the backup container as /root/.ssh/known_hosts. Remote backup could not previously work: the container had no known_hosts, so `StrictHostKeyChecking=yes` failed every connection, and the ssh-keyscan step in .env.example wrote to a file nothing read.

- `BORG_ARCHIVE_PREFIX`, default "epicurrents". Archive names no longer use borgmatic's `{hostname}` default, which in a container is its ID.

- `BACKUP_LOCAL_ENABLED`, default true. Setting it false gives a remote-only deployment, for a host where disk is tight and an append-only remote is already in place. `BORG_REMOTE_REPO` must then be set: with both tiers off the backup container refuses to start rather than emit a configuration that writes nothing while reporting success, and the web container logs the same at boot. A value outside the boolean vocabulary is refused rather than treated as the default.

  `bootstrap.sh` and a distribution's `start.sh` now also initialise the *remote* repository when one is configured, not just the local one, and `scripts/backup.sh` / `scripts/restore.sh` resolve which repository to act on instead of assuming `/backup`.

- Distribution and demo packages can be assembled for a domain: `--proxy-domain` with `--acme-email` fills `PROXY_DOMAIN`, the certificate contact, `FRONTEND_URL`, and appends the host to `ALLOWED_HOSTS` rather than replacing it. The allowlist matters more than it looks: the web container health-checks itself over loopback, so a list that lost `127.0.0.1` would report unhealthy while serving traffic normally.

  The two flags are required together, because a package carrying a domain and no certificate contact cannot start at all — better refused at assembly than discovered on the server it was carried to.

  `--federation` additionally sets `FEDERATION_INSTANCE_URL` from the same domain, and is off by default. `init_env` already generates the keypair, so the instance URL is the piece that completes the trio and leaves the instance ready to federate; that is a posture worth choosing rather than inheriting from having named a domain.

- Distribution and demo packages carry a prepare-host.sh for the case they did not previously cover: a bare Linux server with no Docker, where you are root and nothing else exists yet. Run once as root, it installs Docker Engine, creates the account the deployment runs as, puts it in the docker group, copies root's SSH keys across and hands the package over.

  The account is resolved by uid rather than by name, because every service runs as uid 1000 against a bind mount of the deployment: an image that already has a uid-1000 account is used as-is, since creating a second one would give it 1001 and produce a tree the containers cannot write.

  It installs Docker from scripts/lib/install-docker.sh, the same file bootstrap.sh sources, so a packaged deployment and a cloned one cannot drift onto different engines or a different version floor.

### Changed

- A distribution or demo package joins a Docker network named after its own directory instead of the shared `epicurrents` one, and `--network-name` overrides that when joining an existing network is the point.

  The compose file names its network rather than letting compose scope it per project, so that an externally-managed container can join a predictable one. The cost is that the name is host-wide: two stacks sharing it also share the alias `db`, so a package reaches whichever database answers first — on a machine running a development checkout, that can be the live one. Nothing fails loudly, because each deployment generates its own password; where two share credentials, migrations apply to the wrong database and report success.

  Deriving the default from the destination makes a package isolated because it was built that way, rather than because whoever ran it remembered to ask. An already-assembled package keeps whatever its own `.env` names — rebuild it, or set `EPICURRENTS_NETWORK_NAME` there, if it shares a host with another stack.

- **`AccessRight` now enforces one row per `(object, target)` grant** — three partial unique constraints (user target, group target, `(federated_peer, remote_user_id)` pair, declared on `AccessRight.Meta`). A project or plugin that bare-creates a grant a matching row already covers gets an `IntegrityError` where it previously got a silent duplicate; switch to `get_or_create` or answer 409 the way the core grant endpoints now do. The read resolvers also order multi-target matches deterministically (direct user row over group rows, exact federated user over the peer wildcard, de-identifying row among equals), so which grant's `apply_middleware` wins no longer depends on database row order.

- The default `BORG_RSH` now passes `-i /root/.ssh/id_borg`. Without it the mounted key was never offered, since ssh does not try that filename on its own, and remote backup failed authentication however correctly it was configured. It also passes `UpdateHostKeys=no` to silence a per-connection warning caused by known_hosts being a read-only bind mount.

- **Retention now applies to archives written by earlier container instances.** Borgmatic derives its prune match from `archive_name_format`, whose default embeds `{hostname}`; in a container that is the container ID, which changes on every recreate. `borg prune --glob-archives {hostname}-*` therefore matched only what the current container had written, and every older archive was kept for ever while retention appeared configured and reported success.

  A deployment that has been backing up for a while has archives under old container IDs that the new `epicurrents-*` match will not select either. They are not deleted by this change and will not be pruned; remove them once with `borg delete --glob-archives "????????????-*"`, then `borg compact` on the repository host if it is append-only.

- A distribution's `start.sh` initialises the local Borg repository before starting the stack, as `bootstrap.sh` always has. Without it borgmatic ran against a repository that did not exist and failed every cycle, so a tarball deployment had no backups from the day it was installed.

- scripts/tailscale-register.sh is now scripts/tailscale-serve.sh, and `bootstrap.sh --tailscale-authkey` runs the new host join rather than that container. Pass `--tailscale-mode serve` for the previous behaviour. The rename is the point: the two arrangements are not substitutes, and a name that did not say which one it was is what led to hand-installing Tailscale next to a deployment that appeared to already have it.

- Content-Security-Policy is now **enforced** in production rather than report-only (`CSP_REPORT_ONLY` defaults to `False`), and the baseline no longer permits any third-party origin — `https://cdn.jsdelivr.net` came out of `script-src` and `connect-src`, left there from before Pyodide was vendored same-origin.

  A deployment whose project views reach an external origin, or one running the dicom plugin, should set `CSP_REPORT_ONLY=True` for a cycle and extend `CONTENT_SECURITY_POLICY` before trusting the new default: neither configuration was covered by the tuning pass. Procedure in docs/operations.md → Security headers.

### Fixed

- Sharing a Dataset with a federated peer now reaches that peer. It did not: the federated resolver read direct `AccessRight` rows only and consulted no read extension, so `can_read_via_dataset` — the mechanism local dataset sharing runs on — was structurally unreachable for a peer. The grant was accepted and reported as created, the peer's listing came back empty, and a request for a recording in the dataset was refused. Since collections are author-private, datasets are the only sharing unit, so this was the whole container-sharing surface for federation.

  `register_federated_read_extension` is the path that was missing: a per-object check and a `visible_terms` batch answer registered as a pair, because a listing that disagrees with the object endpoint is its own defect. Terms are inherited from the dataset's grant row, `apply_middleware` included, so a sharer's choice to de-identify is not lost on the way through. Where several grants reach one recording — the same recording in two shared datasets, or a peer-wide grant alongside one naming a user — both halves rank them the same way: the grant naming a user wins, and among grants of equal specificity the de-identifying one does. Item placement inside a dataset is still not carried across; a peer sees a flat list.

- `federation_grant --recording` accepts the hash a recording's URL carries, not only its `content_hash`. Those are different strings — the URL and the API address a recording by the 32-character `stored_name` prefix, while `content_hash` fingerprints the bytes — so an operator pasting the hash they had in hand got "Recording not found for content_hash", which reads as "no such recording". Both forms now resolve, and the error names both.

- [update.sh](scripts/update.sh) refuses a deployment tree the container user cannot write, instead of applying the update and leaving the stack to fail on its first write. The usual cause is an archive built elsewhere: tar records the builder's uid, and an update applied as root preserves it. Fatal in archive mode; a warning in repo mode, where a checkout on a developer's own uid is legitimate and indistinguishable from the broken case.

- A distribution or demo package assembled without a project could not build its own image. The Dockerfile copies projects/ whether or not one is active — the stage that reads a project's requirements resolves an absent lock at build time, which is what keeps "no project" a supported configuration — and BuildKit fails a COPY whose source is missing from the context, reporting it as a checksum error naming an internal ref. Packages now always ship the projects/__init__.py package marker.

  Rebuild any package assembled before this. An existing one is repairable in place: `mkdir -p projects` and copy the platform's own projects/__init__.py beside it.

- The start.sh in a distribution or demo package checks the host before it builds — Docker present, Compose v2 rather than the v1 binary, Engine 25 or newer, the package's own projects/ directory, and the deployment directory writable by uid 1000, which is the user every container runs as. Each of these previously surfaced minutes into a build as a message about something else, and the ownership one not until the stack was up and failing its first write.

  On Linux it also refuses to run as root, which the ownership check alone does not catch: root can write a tree owned by anyone, and the .env generated under it then belongs to root inside a directory the deployment account and the containers both need to write.

## [0.1.0] — 2026-08-28

First versioned release. The platform ran unversioned until here, so this entry records the state at which the number starts rather than a set of changes from a previous one; the history before it is in the commit log.

Started at 0.x rather than 1.0.0 deliberately. The code is not early — it is feature-complete and about to carry data in a production test — but the surface this number governs is the one a project builds on, and that surface is not stable yet. Major version zero is semver's provision for exactly that, and it costs nothing to take: the cap moves to 1.0 when the extension points stop moving.

### Added

- `epicurrents.__version__`, and the `requires_platform` declaration a project or plugin sets on its `AppConfig`. A system check verifies it at `manage.py check`, which Django runs before `runserver` and `migrate`, so a project checked out beside a platform it does not support stops the stack rather than misbehaving.
- Per-project Python dependencies: `projects/<name>/requirements.txt` with a lock generated by `scripts/lock-requirements.sh --project <name>`, installed by the image after the platform's own closure.
- `recordings.testing.make_edf_bytes`, a shipped fixture builder for project test suites that must not import from the platform's test modules.

### Changed

- `recordings.processors.edf._build_header` is now `build_header`. It was private with sixteen call sites across the federation middleware, the FUSE filesystem and a project. Its de-identifying counterpart `_build_clean_header` stays private on purpose: its blanking values are a PHI contract, not parameters.
- `pandas` and `PyWavelets` left the platform's dependency closure for the one project that used them. A deployment needing them gets them from that project.

[Unreleased]: https://github.com/epicurrents/platform/compare/v0.1.0...HEAD [0.1.0]: https://github.com/epicurrents/platform/releases/tag/v0.1.0
