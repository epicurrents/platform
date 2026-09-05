# Changelog

Notable changes to the Epicurrents platform. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the versions are [semantic](https://semver.org).

Below 1.0 the *minor* is the breaking bump, which is semver's rule for initial development and what the platform is in — a project pins `>=0.1,<0.2`, not `<1`. What a version promises at all is narrower than the whole codebase, and is written out in [epicurrents/README.md → Versioning and the platform pin](epicurrents/README.md#versioning-and-the-platform-pin). A change that breaks something outside that surface is not a major bump, and a project pinning `>=0.1,<0.2` is not protected from it.

Entries are written for the person deciding whether to upgrade, so the ones that matter are removals, renames and changed behaviour. A project reading this needs to know what it has to change, not what was added for someone else.

## [Unreleased]

### Added

- Art. 15 subject access export: `manage.py export_user --username <name>`
  produces a plain-text document listing everything the platform holds about one
  person, as the mirror of `erase_user`. `--format json` gives the machine-readable
  form for an Art. 20 portability request. Every relation to the user model is classified, and
  `manage.py check` reports one that is not — a project or plugin registers its
  own with `user.export.register_export_relation` from `AppConfig.ready()`.

  Credentials are excluded by reusing the masked-field registry, so
  `register_masked_fields` now covers the export too. `activity.audit` gained
  `registered_masked_fields()` as the public accessor for that.

- A deployment can be put on a tailnet as a **host**, not only as a container:
  scripts/tailscale-join.sh, `bootstrap.sh --tailscale-authkey` (which now
  defaults to it), and the same flags on a distribution's `start.sh`. This is
  the only arrangement in which containers can reach the tailnet, so it is what
  the evidence-host log shipper needs; the previous container-only mode
  publishes the web UI inbound and routes for nothing else.

  A distribution now also bundles both tailnet scripts, tailscale/serve.json
  (whose absence made the container mode start with an empty directory where its
  serve config should be), and the application half of
  examples/evidence-host/ — so a tarball deployment can ship its security
  stream off-host without a checkout.

- `TS_OUTBOUND_HTTP_PROXY_LISTEN` and `TAILNET_PROXY_URL`, both empty by
  default. Together they let the compose tailscale container give a sibling
  container outbound tailnet access, so a host where packages cannot be
  installed can still ship its security log. Verified end to end against a
  container on a network with no egress at all.

  It covers HTTP only, so remote Borg over `ssh://` still needs Tailscale on the
  host, and each client must name the proxy in its own configuration —
  `HTTP_PROXY` in the environment is not enough for promtail, which ignores it.

- `BORG_SSH_KNOWN_HOSTS_PATH`, mounted into the backup container as
  /root/.ssh/known_hosts. Remote backup could not previously work: the container
  had no known_hosts, so `StrictHostKeyChecking=yes` failed every connection,
  and the ssh-keyscan step in .env.example wrote to a file nothing read.

- `BORG_ARCHIVE_PREFIX`, default "epicurrents". Archive names no longer use
  borgmatic's `{hostname}` default, which in a container is its ID.

- `BACKUP_LOCAL_ENABLED`, default true. Setting it false gives a remote-only
  deployment, for a host where disk is tight and an append-only remote is
  already in place. `BORG_REMOTE_REPO` must then be set: with both tiers off
  the backup container refuses to start rather than emit a configuration that
  writes nothing while reporting success, and the web container logs the same
  at boot. A value outside the boolean vocabulary is refused rather than
  treated as the default.

  `bootstrap.sh` and a distribution's `start.sh` now also initialise the
  *remote* repository when one is configured, not just the local one, and
  `scripts/backup.sh` / `scripts/restore.sh` resolve which repository to act on
  instead of assuming `/backup`.

- Distribution and demo packages carry a prepare-host.sh for the case they did
  not previously cover: a bare Linux server with no Docker, where you are root
  and nothing else exists yet. Run once as root, it installs Docker Engine,
  creates the account the deployment runs as, puts it in the docker group,
  copies root's SSH keys across and hands the package over.

  The account is resolved by uid rather than by name, because every service runs
  as uid 1000 against a bind mount of the deployment: an image that already has
  a uid-1000 account is used as-is, since creating a second one would give it
  1001 and produce a tree the containers cannot write.

  It installs Docker from scripts/lib/install-docker.sh, the same file
  bootstrap.sh sources, so a packaged deployment and a cloned one cannot drift onto
  different engines or a different version floor.

### Changed

- **`AccessRight` now enforces one row per `(object, target)` grant** — three partial unique constraints (user target, group target, `(federated_peer, remote_user_id)` pair, declared on `AccessRight.Meta`). A project or plugin that bare-creates a grant a matching row already covers gets an `IntegrityError` where it previously got a silent duplicate; switch to `get_or_create` or answer 409 the way the core grant endpoints now do. The read resolvers also order multi-target matches deterministically (direct user row over group rows, exact federated user over the peer wildcard, de-identifying row among equals), so which grant's `apply_middleware` wins no longer depends on database row order.

- The default `BORG_RSH` now passes `-i /root/.ssh/id_borg`. Without it the
  mounted key was never offered, since ssh does not try that filename on its
  own, and remote backup failed authentication however correctly it was
  configured. It also passes `UpdateHostKeys=no` to silence a per-connection
  warning caused by known_hosts being a read-only bind mount.

- **Retention now applies to archives written by earlier container instances.**
  Borgmatic derives its prune match from `archive_name_format`, whose default
  embeds `{hostname}`; in a container that is the container ID, which changes on
  every recreate. `borg prune --glob-archives {hostname}-*` therefore matched
  only what the current container had written, and every older archive was kept
  for ever while retention appeared configured and reported success.

  A deployment that has been backing up for a while has archives under old
  container IDs that the new `epicurrents-*` match will not select either. They
  are not deleted by this change and will not be pruned; remove them once with
  `borg delete --glob-archives "????????????-*"`, then `borg compact` on the
  repository host if it is append-only.

- A distribution's `start.sh` initialises the local Borg repository before
  starting the stack, as `bootstrap.sh` always has. Without it borgmatic ran
  against a repository that did not exist and failed every cycle, so a
  tarball deployment had no backups from the day it was installed.

- scripts/tailscale-register.sh is now scripts/tailscale-serve.sh, and
  `bootstrap.sh --tailscale-authkey` runs the new host join rather than that
  container. Pass `--tailscale-mode serve` for the previous behaviour. The
  rename is the point: the two arrangements are not substitutes, and a name
  that did not say which one it was is what led to hand-installing Tailscale
  next to a deployment that appeared to already have it.

- Content-Security-Policy is now **enforced** in production rather than
  report-only (`CSP_REPORT_ONLY` defaults to `False`), and the baseline no
  longer permits any third-party origin — `https://cdn.jsdelivr.net` came out
  of `script-src` and `connect-src`, left there from before Pyodide was
  vendored same-origin.

  A deployment whose project views reach an external origin, or one running the
  dicom plugin, should set `CSP_REPORT_ONLY=True` for a cycle and extend
  `CONTENT_SECURITY_POLICY` before trusting the new default: neither
  configuration was covered by the tuning pass. Procedure in
  docs/operations.md → Security headers.

### Fixed

- A distribution or demo package assembled without a project could not build its
  own image. The Dockerfile copies projects/ whether or not one is active — the
  stage that reads a project's requirements resolves an absent lock at build
  time, which is what keeps "no project" a supported configuration — and
  BuildKit fails a COPY whose source is missing from the context, reporting it
  as a checksum error naming an internal ref. Packages now always ship the
  projects/__init__.py package marker.

  Rebuild any package assembled before this. An existing one is repairable in
  place: `mkdir -p projects` and copy the platform's own projects/__init__.py
  beside it.

- The start.sh in a distribution or demo package checks the host before it
  builds — Docker present, Compose v2 rather than the v1 binary, Engine 25 or
  newer, the package's own projects/ directory, and the deployment directory
  writable by uid 1000, which is the user every container runs as. Each of these
  previously surfaced minutes into a build as a message about something else,
  and the ownership one not until the stack was up and failing its first write.

  On Linux it also refuses to run as root, which the ownership check alone does
  not catch: root can write a tree owned by anyone, and the .env generated under
  it then belongs to root inside a directory the deployment account and the
  containers both need to write.

## [0.1.0] — 2026-08-28

First versioned release. The platform ran unversioned until here, so this entry
records the state at which the number starts rather than a set of changes from a
previous one; the history before it is in the commit log.

Started at 0.x rather than 1.0.0 deliberately. The code is not early — it is
feature-complete and about to carry data in a production test — but the surface
this number governs is the one a project builds on, and that surface is not
stable yet. Major version zero is semver's provision for exactly that, and it
costs nothing to take: the cap moves to 1.0 when the extension points stop
moving.

### Added

- `epicurrents.__version__`, and the `requires_platform` declaration a project or
  plugin sets on its `AppConfig`. A system check verifies it at `manage.py check`,
  which Django runs before `runserver` and `migrate`, so a project checked out
  beside a platform it does not support stops the stack rather than misbehaving.
- Per-project Python dependencies: `projects/<name>/requirements.txt` with a lock
  generated by `scripts/lock-requirements.sh --project <name>`, installed by the
  image after the platform's own closure.
- `recordings.testing.make_edf_bytes`, a shipped fixture builder for project test
  suites that must not import from the platform's test modules.

### Changed

- `recordings.processors.edf._build_header` is now `build_header`. It was private
  with sixteen call sites across the federation middleware, the FUSE filesystem
  and a project. Its de-identifying counterpart `_build_clean_header` stays
  private on purpose: its blanking values are a PHI contract, not parameters.
- `pandas` and `PyWavelets` left the platform's dependency closure for the one
  project that used them. A deployment needing them gets them from that project.

[Unreleased]: https://github.com/epicurrents/platform/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/epicurrents/platform/releases/tag/v0.1.0
