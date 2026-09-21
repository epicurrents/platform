# The remote-maintenance host agent

The root-owned half of the platform's remote-maintenance feature: a small bash script under a one-minute systemd timer that reads the requests the web tier wrote into the deployment's `update/` spool and drives the deployment's `update.sh` to carry them out. Three operations: `platform.update` applies a package it has verified, `platform.backup` takes a snapshot, `platform.rollback` restores one. It exists because the web application must never hold the capability to execute anything on the host; the design and the reasons are in [docs/engineering-notes/remote-maintenance-design.md](../../docs/engineering-notes/remote-maintenance-design.md), the platform side in [maintenance/README.md](../../maintenance/README.md). A distribution package ships this directory as `updater/`.

## Install

On the deployment host, as root, from the unpacked package:

```bash
sudo ./updater/install-updater.sh
```

It installs the agent and its own copy of `update.sh` under `/usr/local/lib/epicurrents-updater/`, the configuration and the release public key under `/etc/epicurrents-updater/` (root-only), creates the spool directories owned by the deployment account, and enables `epicurrents-updater.timer`. The key is `RELEASE_KEY.pub` from the package root unless `--key PATH` names another; the installer prints its id, which is the same one `release_sign.py key-id` prints for the publisher, so the two can be compared out of band. The agent starts disabled: it writes a heartbeat the Maintenance tab shows and refuses every request until `ENABLED=1` is set in the config (or `--enable` was passed). Running the installer again refreshes the agent and leaves the config alone; `--self-update` sets `SELF_UPDATE=1`, described below.

Two settings in the platform's `.env` are the other half: `REMOTE_MAINTENANCE_ENABLED=true` mounts the Maintenance tab, `REMOTE_UPDATE_ENABLED=true` lets a superuser request an update from it.

## What a tick does

1. Loads the config, refusing one that is not root-owned or is writable by anyone else, since a line in it becomes code.
2. Writes `update/agent.json`: version, enabled, runtime, the time, what it can do, the ids of the keys it trusts, and the snapshots under the deployment's `backups/` (name, when, the version the code carries, whether it holds a code archive, and whether the update after it migrated). The Maintenance tab reads it; two minutes without one and the tab reports the agent as not running.
3. If `update/lock` names a process that is gone or a boot that ended, the previous tick died mid-way: an update that had taken its snapshot is rolled back to it, one that had not is marked failed, an interrupted rollback is retried once, and an interrupted backup is marked failed.
4. Takes the oldest request without a status file, unless another job is in flight. Every check runs before anything changes. For every operation: the agent is enabled, the request carries protocol 1 and an operation this agent carries out, and the deployment is not a git checkout (unless allowed). For an update: the request names a package hash; the package exists in the spool; a copy of it under `/var/lib/epicurrents-updater/` hashes to what the request says; the disk has room for twice the package plus a gigabyte; and the agent's own `update.sh --check-archive` accepts the copy with `--require-signature` against the root-owned keys and `--require-newer` against the installed version. For a backup: the disk has a gigabyte free. For a rollback: the request names a snapshot of the shape `update.sh` writes that exists under `backups/` with a database dump and an `.env`, with a code archive too when the database is to be kept, and the disk has a gigabyte free for the safety snapshot. A refusal is a `failed` status naming the reason and nothing else changes.
5. An accepted update: raises the maintenance flag (`updating`), stops `celery-beat`, waits for the worker to finish its tasks, and runs `update.sh --archive <copy> --require-signature --require-newer --skip-beat --keep-lock --yes`, streaming the output into `jobs/<id>.log` and following its `::` lines into the status file's `step`; the `::migrations=` line says whether the release applied a migration, and the status file carries it as `migrations_applied`. Then the gate: the readiness probe answers 200 and the web container reports the package's version. The job is then `awaiting_verification` with a deadline, the flag says `verifying`, the agent's `update.sh` is replaced with the package's copy, and with `SELF_UPDATE=1` so is the agent.
6. In the window: a `.verify` marker makes the job `succeeded`, lifts the flag and starts `celery-beat`; a `.rollback` marker or the deadline rolls back. A rollback takes a `post-update` snapshot first, so what the database gained in the window is recoverable by someone with a shell, then `update.sh --rollback --snapshot <the update's own>` and the gate again; the newest two `post-update` snapshots are kept, since `update.sh` prunes only its own. When the update applied no migration the restore is `--code-only`: the code comes back and the database, with everything written since the update, is kept; `update.sh` checks the same fact against the database and may decline, and the database is then restored after all. After the job succeeded, a `.rollback` marker still rolls back for as long as the snapshot exists.
7. An accepted backup: `update.sh --snapshot backup`, without a flag, a stop or a drain, since the dump is one transaction and the platform keeps serving. The snapshot is `backups/backup-<stamp>`; the newest three are kept.
8. An accepted rollback: the flag (`rolling_back`), `celery-beat` stopped, the worker drained, a `pre-rollback` safety snapshot (a failure here fails the job with nothing changed), then `update.sh --rollback --snapshot <name>`, with `--code-only` when the request keeps the database, then the gate against the version the restored tree carries. A declined `--code-only` fails the job as `refused_code_only` with nothing changed but the safety snapshot already taken; the request asked for the database to be kept, so the agent does not restore it on its own. The newest two `pre-rollback` snapshots are kept.

Every state is written to the status file and to the journal (`journalctl -t epicurrents-updater`), which survives the database restore a rollback performs.

## States and reasons

`requested` → `accepted` → `running` → `awaiting_verification` → `succeeded`, with `failed` and `rolling_back` → `rolled_back` | `rollback_failed` off the path; a backup or a rollback job goes `running` → `succeeded` | `failed` | `rollback_failed`. A `failed` request carries `refused_<reason>`: `disabled`, `protocol`, `operation`, `hash`, `checkout`, `disk`, `snapshot` (a rollback naming no restorable snapshot), `signature`, `manifest`, `updater_too_old`, `incompatible`, `version_not_newer`, `code_only` (the database cannot be kept: a migration was applied since the snapshot, or the agent's `update.sh` predates the option), or `check` when `update.sh` refused for a reason it did not name. A failed run carries `update_failed_before_snapshot` (nothing changed) or `snapshot_failed` (a backup, or a rollback's safety snapshot, did not complete; nothing changed), and a rollback carries what caused it: `update_failed`, `health_failed`, `requested`, `deadline`, `late`, `stale`.

`rollback_failed` and `stale` without a snapshot are the states that need a shell. The maintenance flag is left in place in the first case, so the platform keeps saying why it is not serving; `update.sh --rollback --snapshot <name>` from the deployment root is the manual continuation, and `rm update/maintenance.json` lifts the flag once the deployment is repaired.

## Release keys and their rotation

The agent verifies packages against `/etc/epicurrents-updater/release.pub`, its own root-owned copy of the publisher's key. A release may announce the key that signs the next one: the packager's `--successor-key` puts that public key inside the signed manifest, and when a package with one verifies, the agent installs it as `release.pub.next`. From then on a package may be signed with either key; the first time one verifies with the successor, the successor becomes `release.pub` and the old key is gone. The heartbeat carries both ids, so the Maintenance tab shows which keys the agent trusts. A deployment that skipped the announcing release does not know the new key and refuses the next one with `refused_signature`; re-running the installer with `--key` is the way in.

## Self-update

The agent's own copy of `update.sh` is replaced with the one every verified package ships, so the next package finds the script it was built for. The agent itself is replaced only with `SELF_UPDATE=1` in the config: after a package verifies and applies, the agent installs the package's `updater/epicurrents-updater.sh` when it parses and carries a newer `AGENT_VERSION`, keeps the copy it replaced as `epicurrents-updater.sh.previous`, and runs the new one from the next tick. The systemd units are not touched. Off, a newer agent is installed by running the package's `updater/install-updater.sh` again, which is the route on a host where an unattended change to a root-owned script is not wanted.

## What the agent trusts

The deployment tree belongs to uid 1000, the account every container runs as, so nothing the agent executes or interprets comes from there unverified. The package is copied out and verified as a copy; `update.sh` and, when allowed, the agent run from root's own copies, refreshed from a package only after that package verified; the release keys are root's own copies, and a successor arrives inside a manifest the current key signed. The compose files are read from the tree because `update.sh` has to, the same trust the manual path places in them. Request files are read as JSON through `python3` and nothing in them is evaluated: the operation must be one of the three keys, a package hash must match `[0-9a-f]{64}`, a snapshot name must match `<label>-<date>-<time>` and exist, the window is clamped, and the job id must be a UUID.

## Files

| Path | Owner | What |
|---|---|---|
| `/etc/epicurrents-updater/config` | root, 0600 | `DEPLOY_ROOT`, `ENABLED`, `ALLOW_CHECKOUT`, `SELF_UPDATE`, `MIN_FREE_BYTES`, `HEALTH_TIMEOUT`, `DRAIN_TIMEOUT`, `VERIFY_WINDOW_MINUTES` |
| `/etc/epicurrents-updater/release.pub` | root, 0600 | the release public key |
| `/etc/epicurrents-updater/release.pub.next` | root, 0600 | a successor key a release announced, until a package signed with it verifies |
| `/usr/local/lib/epicurrents-updater/` | root | `epicurrents-updater.sh` (and `.previous` after a self-update), `update.sh`, this file |
| `/var/lib/epicurrents-updater/work/<job>/` | root | the verified package copy while a job runs |
| `/run/lock/epicurrents-updater.lock` | root | the run lock (`flock`) |
| `<root>/update/agent.json` | deployment account | the heartbeat |
| `<root>/update/lock` | deployment account | which process is executing which job, present only during an update or a rollback |
| `<root>/update/maintenance.json` | deployment account | the maintenance flag the platform honours |
| `<root>/update/jobs/<id>.status.json`, `.log` | deployment account | the job's state and the agent's log of it |

The agent runs `chown` on every spool file it writes, so the web tier, which runs as the deployment account, can read them; the installer creates the spool directories with that owner for the same reason.

## Testing

[scripts/tests/test_updater_agent.py](../tests/test_updater_agent.py) drives the agent with the fakebin harness against a staged deployment and a stub `update.sh` that speaks the `::` protocol: each refusal reason, the path to the window, confirm, the three routes into a rollback, the code-only rollback and its fallback, the two failure shapes of an update, a failed rollback, a stale lock in each state and for each operation, the late rollback, a backup and its pruning, a standalone rollback with and without the database, a successor key installed and promoted, the self-update on and off, the heartbeat's snapshot list, the log cap, the file ownership and the runtime detection. Nothing there runs a container.
