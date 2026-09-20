# The remote-maintenance host agent

The root-owned half of the platform's remote-maintenance feature: a small bash script under a one-minute systemd timer that reads update requests the web tier wrote into the deployment's `update/` spool, verifies the package each one names, and drives the deployment's `update.sh`. It exists because the web application must never hold the capability to execute anything on the host; the design and the reasons are in [docs/engineering-notes/remote-maintenance-design.md](../../docs/engineering-notes/remote-maintenance-design.md), the platform side in [maintenance/README.md](../../maintenance/README.md). A distribution package ships this directory as `updater/`.

## Install

On the deployment host, as root, from the unpacked package:

```bash
sudo ./updater/install-updater.sh
```

It installs the agent and its own copy of `update.sh` under `/usr/local/lib/epicurrents-updater/`, the configuration and the release public key under `/etc/epicurrents-updater/` (root-only), creates the spool directories owned by the deployment account, and enables `epicurrents-updater.timer`. The key is `RELEASE_KEY.pub` from the package root unless `--key PATH` names another; the installer prints its id, which is the same one `release_sign.py key-id` prints for the publisher, so the two can be compared out of band. The agent starts disabled: it writes a heartbeat the Maintenance tab shows and refuses every request until `ENABLED=1` is set in the config (or `--enable` was passed). Running the installer again refreshes the agent and leaves the config alone.

Two settings in the platform's `.env` are the other half: `REMOTE_MAINTENANCE_ENABLED=true` mounts the Maintenance tab, `REMOTE_UPDATE_ENABLED=true` lets a superuser request an update from it.

## What a tick does

1. Loads the config, refusing one that is not root-owned or is writable by anyone else, since a line in it becomes code.
2. Writes `update/agent.json`: version, enabled, runtime, the time, what it can do. The Maintenance tab reads it; two minutes without one and the tab reports the agent as not running.
3. If `update/lock` names a process that is gone or a boot that ended, the previous tick died mid-way: an update that had taken its snapshot is rolled back to it, one that had not is marked failed, an interrupted rollback is retried once.
4. Takes the oldest request without a status file, unless another job is in flight. Every check runs before anything changes: the agent is enabled; the request carries protocol 1, the one operation this agent carries out (`platform.update`) and a package hash; the deployment is not a git checkout (unless allowed); the package exists in the spool; a copy of it under `/var/lib/epicurrents-updater/` hashes to what the request says; the disk has room for twice the package plus a gigabyte; and the agent's own `update.sh --check-archive` accepts the copy with `--require-signature` against the root-owned key and `--require-newer` against the installed version. A refusal is a `failed` status naming the reason and nothing else changes.
5. On acceptance: raises the maintenance flag (`updating`), stops `celery-beat`, waits for the worker to finish its tasks, and runs `update.sh --archive <copy> --require-signature --require-newer --skip-beat --keep-lock --yes`, streaming the output into `jobs/<id>.log` and following its `::` lines into the status file's `step`. Then the gate: the readiness probe answers 200 and the web container reports the package's version. The job is then `awaiting_verification` with a deadline, the flag says `verifying`, and the agent's `update.sh` is replaced with the package's copy.
6. In the window: a `.verify` marker makes the job `succeeded`, lifts the flag and starts `celery-beat`; a `.rollback` marker or the deadline rolls back. A rollback takes a `post-update` snapshot first, so what the database gained in the window is recoverable by someone with a shell, then `update.sh --rollback --snapshot <the update's own>` and the gate again; the newest two `post-update` snapshots are kept, since `update.sh` prunes only its own. After the job succeeded, a `.rollback` marker still rolls back for as long as the snapshot exists.

Every state is written to the status file and to the journal (`journalctl -t epicurrents-updater`), which survives the database restore a rollback performs.

## States and reasons

`requested` → `accepted` → `running` → `awaiting_verification` → `succeeded`, with `failed` and `rolling_back` → `rolled_back` | `rollback_failed` off the path. A `failed` request carries `refused_<reason>`: `disabled`, `protocol`, `operation`, `hash`, `checkout`, `disk`, `signature`, `manifest`, `updater_too_old`, `incompatible`, `version_not_newer`, or `check` when `update.sh` refused for a reason it did not name. A failed run carries `update_failed_before_snapshot` (nothing changed), and a rollback carries what caused it: `update_failed`, `health_failed`, `requested`, `deadline`, `late`, `stale`.

`rollback_failed` and `stale` without a snapshot are the states that need a shell. The maintenance flag is left in place in the first case, so the platform keeps saying why it is not serving; `update.sh --rollback --snapshot <name>` from the deployment root is the manual continuation, and `rm update/maintenance.json` lifts the flag once the deployment is repaired.

## What the agent trusts

The deployment tree belongs to uid 1000, the account every container runs as, so nothing the agent executes or interprets comes from there unverified. The package is copied out and verified as a copy; `update.sh` runs from the agent's own copy, refreshed from a package only after that package verified and applied; the release key is root's own copy. The compose files are read from the tree because `update.sh` has to, the same trust the manual path places in them. Request files are read as JSON through `python3` and nothing in them is evaluated: the operation must equal the one key, the hash must match `[0-9a-f]{64}`, the window is clamped, and the job id must be a UUID.

## Files

| Path | Owner | What |
|---|---|---|
| `/etc/epicurrents-updater/config` | root, 0600 | `DEPLOY_ROOT`, `ENABLED`, `ALLOW_CHECKOUT`, `MIN_FREE_BYTES`, `HEALTH_TIMEOUT`, `DRAIN_TIMEOUT`, `VERIFY_WINDOW_MINUTES` |
| `/etc/epicurrents-updater/release.pub` | root, 0600 | the release public key |
| `/usr/local/lib/epicurrents-updater/` | root | `epicurrents-updater.sh`, `update.sh`, this file |
| `/var/lib/epicurrents-updater/work/<job>/` | root | the verified package copy while a job runs |
| `/run/lock/epicurrents-updater.lock` | root | the run lock (`flock`) |
| `<root>/update/agent.json` | deployment account | the heartbeat |
| `<root>/update/lock` | deployment account | which process is executing which job, present only during an update or a rollback |
| `<root>/update/maintenance.json` | deployment account | the maintenance flag the platform honours |
| `<root>/update/jobs/<id>.status.json`, `.log` | deployment account | the job's state and the agent's log of it |

The agent runs `chown` on every spool file it writes, so the web tier, which runs as the deployment account, can read them; the installer creates the spool directories with that owner for the same reason.

## Testing

[scripts/tests/test_updater_agent.py](../tests/test_updater_agent.py) drives the agent with the fakebin harness against a staged deployment and a stub `update.sh` that speaks the `::` protocol: each refusal reason, the path to the window, confirm, the three routes into a rollback, the two failure shapes of an update, a failed rollback, a stale lock in each state, the late rollback, the log cap, the file ownership and the runtime detection. Nothing there runs a container.
