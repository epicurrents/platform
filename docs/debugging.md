# Debugging failure states

**Audience: developers** (including AI-assisted project-plugin authors). How to
investigate *why* something failed — the diagnostic-logging tier, the failure
fields on the data model, and how to read background-task state. It assumes
familiarity with the platform's internals (models, Celery, the security
taxonomy) and uses the Django shell.

> **Operators / system administrators:** if you are restoring a crashed or
> degraded system, work the [operator runbook](operator-runbook.md) first — it
> is black-box and hands off here, with the artifacts to capture, when a
> problem turns out to be code-level.

This guide is process-oriented ("a job failed, where do I look?"). For
symptom-keyed entries ("I see error X, what's the fix?") see
[troubleshooting.md](troubleshooting.md); for intentional operations see
[operations.md](operations.md). The per-request **audit trail** in the
`activity` app answers *who changed what* and is a separate concern from the
technical logs described here — see [activity/README.md](../activity/README.md).

## The logging model

Every service logs to its container's stdout/stderr; nothing writes to a file
inside a container. Tail it with [logs.sh](../scripts/logs.sh):

```bash
scripts/logs.sh                 # follow the whole stack
scripts/logs.sh celery 200      # one service, last 200 lines
scripts/logs.sh web 500
```

Application log records carry the originating module as the logger name
(`recordings.tasks`, `federation.auth`, …), set per-module via
`logging.getLogger(__name__)`. The format differs by mode:

- **Production** ([settings/production.py](../epicurrents/settings/production.py)) — one JSON object per line (`time`, `level`, `logger`, `message`), so a log shipper can parse and index it.
- **Development** ([settings/development.py](../epicurrents/settings/development.py)) — human-readable `time level logger: message`. Both modes share the same handler shape and level knobs, so verbosity behaves identically.

Security-relevant events (auth failures, permission denials, rate-limit hits,
federation auth failures, audit-integrity alarms) go to a dedicated
`epicurrents.security` logger with a structured `security_event_type` — see
[epicurrents/security_log.py](../epicurrents/security_log.py). Shipping that
stream off-host and alerting on it is covered in
[operations.md → Security log stream](operations.md#security-log-stream), with
sample Loki/Promtail/Grafana configs under
[examples/observability/](../examples/observability/).

## Raising verbosity

Two environment variables control level, read in both modes:

| Variable | Controls | Default |
|---|---|---|
| `LOG_LEVEL` | Root logger — all application modules | `INFO` |
| `DJANGO_LOG_LEVEL` | The `django` logger (request handling, ORM, etc.) | `WARNING` (prod) / `INFO` (dev) |

Set the level in `.env` and restart the service whose logs you're reading:

```bash
# .env
LOG_LEVEL=DEBUG
```

```bash
docker compose restart web celery
scripts/logs.sh celery 200
```

Lower it again once you've captured what you need — `DEBUG` is noisy and, in
production, increases the volume your log shipper ingests.

## Correlating a report to a log line

When a user reports a failure at a given time, narrow by the record identifier
first, then the timestamp. Most failure log lines carry the relevant id (a
recording PK, a peer id). For example, to trace a recording:

```bash
scripts/logs.sh celery 2000 | grep "recording 4217"
```

In production the `time` field is on every JSON line, so a log shipper can be
queried by timestamp window directly; from raw `docker compose logs`, add
`--since` / `--until`:

```bash
docker compose logs celery --since 2026-06-13T15:40:00 --until 2026-06-13T15:50:00
```

## Recording processing failures

A recording moves `PENDING → PROCESSING → READY`, or to `FAILED`. There are two
failure paths and they leave different traces:

1. **Handled format error** (e.g. an unparseable EDF/BDF header). The recording
   is set to `FAILED` and the reason is written to `Recording.processing_error`.
2. **Unexpected error** (missing file, database error, converter crash). The
   recording is also set to `FAILED`, with `processing_error` prefixed
   `Unexpected processing error:` and the exception text. The full traceback is
   in the `celery` log (logged with `exc_info`).

Either way the row survives, so there is always something to inspect.
`processing_error` is author/superuser-only (it can carry PHI from a source
filename or a stack trace), and `FAILED` recordings 404 for every caller but
the author and superusers — local and federated alike — enforced by the
read-visibility gate registered via `register_read_visibility_gate` (see
[recordings/permissions.py](../recordings/permissions.py)).

Read the reason from a Django shell:

```bash
scripts/manage.sh shell -c \
  "from recordings.models import Recording; \
   r = Recording.objects.get(content_hash='<hash>'); \
   print(r.status, repr(r.processing_error))"
```

The author also sees `processing_error` on the recording's detail response, and
receives a "Recording failed" push notification. If a recording is stuck in
`PROCESSING` rather than `FAILED`, the worker died mid-task — see the stuck-task
entry in [troubleshooting.md](troubleshooting.md#recording-stuck-in-processing-indefinitely).

## Background tasks (Celery)

Task exceptions are logged (with traceback) and the task state is stored in the
Redis result backend; there is no dead-letter queue, so the **log line plus any
failure field on the affected model is the record**. To inspect a running
worker:

```bash
docker compose exec celery celery -A epicurrents inspect active     # tasks running now
docker compose exec celery celery -A epicurrents inspect reserved   # queued, not yet started
docker compose exec celery celery -A epicurrents inspect stats      # worker pool / counts
```

`inspect` talks to live workers; if it hangs or returns nothing, the worker is
down — check `docker compose ps` and the `celery` log for a startup error
(commonly Redis unreachable or `REDIS_PASSWORD` unset, covered in
[troubleshooting.md](troubleshooting.md)).

Tasks that retry (email send, some project compute tasks) log a `WARNING` on
each attempt with the retry counter; a final failure surfaces as an `ERROR`
with the traceback.

## Federation failures

Inbound auth failures are logged through the security logger as
`federation.auth_failed` *and* recorded in `FederationAuditLog` — query the
latter for per-peer patterns (worked example in
[operations.md](operations.md#query-the-federation-audit-log)). Outbound
failures (peer unreachable, signature mismatch, TLS) are logged by
`federation.auth` / `federation.fuse_fs` at `WARNING`; raise `LOG_LEVEL=DEBUG`
on `web`/`celery` to see the full request detail.

## Compute and project-plugin tasks

Core compute (lead-field generation) runs synchronously inside the request, so
a failure returns an HTTP error to the caller and logs to `web` — there is no
separate job record to consult. Project plugins that run their own Celery tasks
log via their own module loggers; find them by the plugin's logger-name prefix
in the `celery` log.

## Health endpoints

`GET /api/v1/health` is the liveness probe: `{"status": "ok", "mode": "<development|production|unset>", "debug": <bool>}`,
unauthenticated, touching no backing service. A response with `mode: "unset"` or
an unexpected `debug` value points at a misconfigured `DJANGO_MODE`.

`GET /api/v1/ready` is the readiness probe: it opens a database cursor and reads
from the cache, answering 200 `{"status": "ready", "checks": {...}}` only when
both respond and 503 with the failing dependency marked `"error"` otherwise. The
`web` container's healthcheck polls it. Per-dependency failure detail is written
to the `web` container log rather than returned, since the endpoint takes no
credentials — read the traceback there when a probe reports `"error"`.

A 400 on `/api/v1/ready` from inside the container means `ALLOWED_HOSTS` no
longer contains `127.0.0.1`; the probe requests it over loopback.
