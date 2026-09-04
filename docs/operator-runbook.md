# Operator runbook

**Audience: operators / system administrators.** You keep the deployment
running. You are comfortable with Linux, Docker, and networking, but you do
**not** need to know the platform's code or internals. Everything here is
black-box: container state, the health endpoint, logs, restart, restore. When
a problem turns out to be code-level, [Escalate to a developer](#escalate-to-a-developer)
tells you what to capture and hand over.

Run every command from the deployment directory (where `docker-compose.yml`
lives). Nothing here edits code or the database directly.

## First response

Two commands tell you most of what you need:

```bash
docker compose ps                                 # which services are up / healthy?
curl -s localhost:${HOST_PORT:-8000}/api/v1/ready # can the app reach its dependencies?
```

A healthy system shows every service `Up`/`running`, with `db`, `redis`, `web`
and `celery` reporting `healthy`, and the readiness probe returns
`{"status": "ready", "checks": {"database": "ok", "cache": "ok"}}`.

A 503 from the probe names the dependency that did not answer in `checks` —
go straight to that service's log. `/api/v1/health` is the liveness counterpart:
it answers `{"status": "ok", "mode": "production", ...}` from a web container
whose database is unreachable, so use it to confirm the web tier is alive, never
to conclude the system is working.

From there, work the triage flow:

1. **Is a service missing or not `Up`?** → [The services](#the-services) names what each one does and how to bring it back.
2. **Is a service up but `unhealthy`, or constantly restarting?** → read its log: `scripts/logs.sh <service> 200`. Then match what you see in [Reading the logs](#reading-the-logs).
3. **Are all services `Up` and healthy, but a feature is broken or a user reports an error?** → that is an application-level problem, not an infrastructure one. Go to [Escalate to a developer](#escalate-to-a-developer).

## The services

| Service | What it does | If it's down |
|---|---|---|
| `web` | Serves the API and the app to users. | Users get connection errors. Check its log, then restart. |
| `celery` | Background processing — recording ingest, notifications. | Uploads never finish (stay "processing"); the app itself still loads. Restart it. |
| `celery-beat` | Scheduler that triggers periodic jobs (purges, integrity checks). | Scheduled maintenance stops; no immediate user impact. Restart it. |
| `db` (PostgreSQL) | The database. | Almost everything fails. This is usually the root cause when multiple services are unhealthy. |
| `redis` | Message broker + cache. | `celery` won't start; rate-limiting and some caching degrade. |
| `borg` | Scheduled backups. | Backups stop — no user impact, but fix before you need a restore. |

Bring a service back:

```bash
docker compose restart <service>      # restart one service
docker compose up -d                  # (re)start anything that's stopped
```

If `db` or `redis` is down, start it first, then restart the services that
depend on it (`web`, `celery`, `celery-beat`).

## Reading the logs

```bash
scripts/logs.sh <service> 200         # last 200 lines from one service
scripts/logs.sh                       # follow the whole stack
```

You are looking for the **first** error, near where the service started or
crashed. Match the message to the cause:

| What the log says | Likely cause | Operator action |
|---|---|---|
| `connection refused` / `could not connect to server` (in `web`/`celery`) | `db` or `redis` isn't up yet | Confirm `db`/`redis` are `healthy`, then restart the complaining service. |
| `bind for 0.0.0.0:8000 failed: port is already allocated` | Another process owns the host port | Set `HOST_PORT` in `.env` to a free port, then `docker compose up -d`. |
| `no space left on device` | Disk full | See [Disk space](#disk-space). |
| `permission denied` on a data path | Volume ownership | `docker compose run --rm init-volumes`, then restart. |
| `ImproperlyConfigured: ... must be set` / `REDIS_PASSWORD is not set` | A required value is missing from `.env` | Set the named value in `.env` (see [getting-started.md](getting-started.md) / [.env.example](../.env.example)), then `docker compose up -d`. |
| `401 ... JWT 'iat' is too old` / `JWT has expired` between federated instances | Server clock drift | Fix time sync (NTP) on the host; no platform change needed. |
| A `Traceback (most recent call last):` followed by `File ".../*.py"` | An application (code) error | **Do not try to fix.** [Escalate to a developer](#escalate-to-a-developer). |
| A line containing `epicurrents.security` and an `audit.` event type | Audit-integrity alarm — possible tampering or corruption | Escalate immediately to a developer / your security contact. Do not restart or "clean up" first. |

The symptom-keyed [troubleshooting.md](troubleshooting.md) has more entries; the
ones above are the conditions an operator can resolve without code knowledge.

## Disk space

```bash
df -h                  # host disk usage
docker system df       # space used by Docker images / volumes
```

Recordings and backups grow over time. If a data volume is full, **do not
delete files inside it by hand** — that can orphan database rows. Free space
by pruning unused Docker images (`docker image prune`) or expanding the disk,
and escalate if recordings/backups themselves need trimming.

## Recovering from data loss

If the database or recording files are lost or corrupted, restore from the most
recent Borg backup. The procedure (stop app services → restore DB → restore
files → restart) is in [operations.md → Restore from a backup](operations.md#restore-from-a-backup).
Practise it once on a scratch host before you need it for real — a restore you
have never run is not a backup you can rely on.

## A recording is stuck "processing"

If uploads never finish, the `celery` worker likely died mid-job:

```bash
docker compose restart celery
```

New uploads should process again. Clearing the *already-stuck* recordings
requires a developer (it touches the database); if recordings keep getting
stuck after a restart, that is a code-level problem — escalate.

## Escalate to a developer

Hand off when the problem is **not** "a service is down":

- A service keeps exiting or restarting even though `db`, `redis`, and disk are fine.
- The log shows a Python `Traceback` rather than an infrastructure message (connection refused, no space, permission denied, port in use).
- Every service is `Up` and `healthy`, but a specific feature or user action fails.
- Any `epicurrents.security` log line with an `audit.` event type (escalate immediately).

Capture these and include them in the handoff:

```bash
docker compose ps > state.txt
scripts/logs.sh <implicated-service> 500 > issue.log
curl -s localhost:${HOST_PORT:-8000}/api/v1/ready > ready.json
```

Plus: the time the problem started, what the user was doing, and which project
plugin is active (the `EPICURRENTS_PROJECT` value in `.env`). The developer's
starting point is [debugging.md](debugging.md).

## What's out of scope here

These need a developer (and are intentionally **not** in this runbook): editing
code or configuration logic, database/Django-shell recoveries, debugging why a
feature behaves wrong, federation key rotation, and anything that reads or
changes data inside the database. Those live in [troubleshooting.md](troubleshooting.md),
[debugging.md](debugging.md), and [operations.md](operations.md).
