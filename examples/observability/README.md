# Observability example — shipping the security log stream

A starting template for getting the platform's structured logs — and the
`epicurrents.security` event stream in particular — off the host and onto a
queryable, alertable backend (Loki + Promtail + Grafana). These files are
**templates to adapt**, not a turnkey production stack: review storage,
retention, authentication, and alert delivery before relying on them.

The operator-facing reference for the security stream — logger name, the
`security_event_type` taxonomy, structured fields, and why shipping it
off-host matters — is in [docs/operations.md → Security log stream](../../docs/operations.md#security-log-stream).

## What's here

| File | Purpose |
|---|---|
| `loki-config.yaml` | Single-binary Loki: filesystem storage, 31-day retention, ruler enabled. |
| `promtail-config.yaml` | Discovers the compose containers via the Docker socket and parses the production JSON log lines, promoting `level` and `security_event_type` to labels. |
| `rules.yaml` | LogQL alert rules keyed on `security_event_type` (audit tampering, federation auth failures, login brute force, permission-denial spikes). |
| `docker-compose.observability.yml` | Wires Loki + Promtail + Grafana on the `observability` profile. |

## Run it

```bash
docker compose -f docker-compose.yml \
  -f examples/observability/docker-compose.observability.yml \
  --profile observability up -d
```

Open Grafana at `http://127.0.0.1:3000`, add a Loki data source at
`http://loki:3100`, and explore:

```logql
# all security events
{container=~"epicurrents.*"} | json | logger="epicurrents.security"

# one event type
{container=~"epicurrents.*"} | json | security_event_type="federation.auth_failed"
```

Because the production formatter ([epicurrents/log_formatters.py](../../epicurrents/log_formatters.py))
emits `security_event_type` and the structured fields (`actor_id`, `ip`,
`reason`, …) as discrete JSON keys, you can filter and alert on them directly
rather than regexing the message string.

## Prerequisites and caveats

- **Production JSON logs.** Parsing assumes `DJANGO_MODE=production`, where the
  stack emits one JSON object per line. In development the format is plain
  text and the JSON pipeline stage simply keeps the raw line.
- **Alert delivery.** `rules.yaml` only fires if the ruler's `alertmanager_url`
  (in `loki-config.yaml`) points at a running Alertmanager. Without one, rules
  still evaluate and are visible via the ruler API but nothing is delivered.
- **Docker socket mount.** Promtail reads `/var/run/docker.sock` (read-only) to
  discover containers — a privileged mount that grants visibility into every
  container on the host.
- **Promtail vs Alloy.** Grafana has succeeded Promtail with Grafana Alloy.
  Promtail still works and is the simplest illustration; for a long-lived
  deployment, migrate to Alloy — the JSON-parse and label stages translate
  directly.
- **No PII in the stream.** Per the security-logging rule, callers hash
  usernames and emails before logging; `actor_id` is an integer FK. Keep that
  invariant when adding new events, since these logs now leave the host.
