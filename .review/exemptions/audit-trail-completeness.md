# Audit-trail-completeness exemptions

Exemption registry for the [`audit-trail-completeness` agent](../agents/audit-trail-completeness.md).
Sibling registries for other agents live next to this file in
[.review/exemptions/](.); see that directory's [README](README.md)
for the convention.

The Activity audit table captures **interactions with data the platform
stores** — patient recordings and their derivatives, user records, access
rights, federation peer registrations, the audit trail itself. Endpoints
that operate on something else (health-monitor responses, static API-shape
lookups, the publicly-served VAPID key, computed artifacts whose inputs
are anatomical assumptions rather than a specific user's data) are
exempt.

This file is consulted by the agent before it flags a missing
annotation. Listing an endpoint here means the agent treats its
absence-of-annotation as intentional rather than as a finding.

## How to add an entry

1. Add a row to the table below with the endpoint's HTTP method + path
   exactly as written in the `@api.*` decorator.
2. Pick a treatment:
   - **`skip_row_creation`** — the middleware does not create an
     `Activity` row at all. Reserved for high-volume operational
     endpoints (health checks, etc.) and for endpoints whose existence
     is genuinely public (`vapid-public-key`). The skip list is
     enforced by `ApiActivityLoggingMiddleware`; adding the entry here
     is not enough — the middleware setting must be updated too.
   - **`skip_annotation`** — the middleware still creates an `Activity`
     row (with the default `"get"` / `"post"` verb), but the endpoint
     does not need to annotate it. Use for endpoints whose audit-trail
     value is low but where row volume is also low (`content-types`,
     and for now the four compute leadfield endpoints). Where
     debugging value exists, route to `logger.info` instead.
3. Add a one-line reason. The reason should make it obvious to a
   future reviewer why the endpoint is *not* a data-interaction event.

If the answer to "is this endpoint exempt?" is anything other than an
unambiguous yes, add the annotation instead.

## Current exemptions

| Method + path | Treatment | Reason |
|---|---|---|
| `GET /annotations/api/v1/health` | `skip_row_creation` | Health-monitor poll; no data interaction. Skipped at middleware level to bound audit-table volume. |
| `GET /api/v1/health` | `skip_row_creation` | Same — `epicurrents.health`. |
| `GET /api/v1/ready` | `skip_row_creation` | Container-healthcheck poll (every 15 s per web container); reports only whether the database and cache answered. No data interaction. |
| `GET /api/v1/notifications/vapid-public-key` | `skip_row_creation` | Publicly-served push-encryption public key; possessing it grants no abuse capability. Anyone subscribing to push needs it. |
| `GET /api/v1/user/auth-config` | `skip_row_creation` | Public list of enabled external-login providers, polled on every login-screen render. Identical response for every caller; no data interaction. The OIDC callback that does the actual login is **not** exempt and annotates `user.login`. |
| `GET /annotations/api/v1/content-types` | `skip_annotation` | Static lookup of `ContentType` PKs the generic-FK API surface needs. No data interaction. Row volume is low enough that middleware row creation is fine. |
| `GET /compute/api/v1/eeg/leadfield/` | `skip_annotation` | Computed artifact derived from anatomical assumptions, not from a specific user's recordings. Use `logger.info` for technical debugging. |
| `POST /compute/api/v1/eeg/leadfield/` | `skip_annotation` | Fetch-or-compute on a montage. Same reasoning. |
| `GET /compute/api/v1/eeg/leadfield/{montage_name}/` | `skip_annotation` | Same. |
| `GET /compute/api/v1/eeg/leadfield/{montage_name}/data/` | `skip_annotation` | Bytes of the computed leadfield. Same reasoning. |

## Re-scoping when behaviour changes

If a future `compute` endpoint processes a specific patient recording
(e.g. a server-side feature-extraction run keyed on a Recording PK),
that endpoint comes back into scope and gets annotated as
`compute.<feature>.run` or similar — the leadfield exemption above is
about the current four endpoints, not the `compute` app as a whole.

Same logic applies anywhere: an exemption is per-endpoint, never
per-app.

## Related

- Agent that consults this file:
  [.review/agents/audit-trail-completeness.md](../agents/audit-trail-completeness.md).
- Middleware that the `skip_row_creation` entries depend on:
  [epicurrents/middleware.py](../../epicurrents/middleware.py). The list
  itself is the `ACTIVITY_PATH_SKIP_LIST` setting in
  [epicurrents/settings/common.py](../../epicurrents/settings/common.py);
  matching is on the exact path, so a new operational endpoint needs its
  own entry there as well as a row above.
