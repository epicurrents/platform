# update-platform-docs

Review recent changes to the Epicurrents platform and update the documentation site at `docs/epicurrents/src/docs/latest/platform/` and `README.md` to reflect them.

## When this skill applies

Invoke after any platform change that touches:
- **API endpoints** — new routes, changed request/response shapes, removed endpoints, new query parameters
- **Models** — new fields, changed field types, new models, changed relationships
- **Settings / env vars** — new environment variables, changed defaults, new configuration sections
- **Project plugin system** — changes to how plugins are registered, activated, deactivated, or tested
- **CI/CD** — new jobs, changed test strategy, new tooling (ruff, vitest, pip-audit, etc.)
- **Deployment** — changes to Docker services, scripts, bootstrap process, volume layout
- **Permissions / access control** — new guards, changed semantics for `is_staff`/`is_superuser`, new `AccessRight` patterns
- **New platform features** — federation, annotations, library (collections/datasets/tags), recordings pipeline changes

Do **not** invoke for internal refactors, test-only changes, or bug fixes that restore already-documented behaviour.

## Docs locations

| Location | Contents |
|---|---|
| `docs/epicurrents/src/docs/latest/platform/` | Full platform documentation (deployment, config, project development) |
| `README.md` | Landing card — quick start, test commands, project plugin table. Keep this ≤ 50 lines. |

Both paths are relative to the platform repository root. The docs site is a submodule (`docs/epicurrents` → `epicurrents/epicurrents.github.io`). **Edit that checkout, not a standalone clone of the same repository elsewhere on the machine** — the submodule is the copy that is kept current, and it is the one the platform pins. Changes are committed inside `docs/epicurrents/` first; the platform repository then records the new commit pointer as its own change.

Navigation is declared in `docs/epicurrents/src/router.ts` under the `'Platform'` key. New pages must be added there.

## Page → concept mapping

| Changed area | Pages to check |
|---|---|
| Docker Compose services, `entrypoint.sh`, volume layout, `bootstrap.sh` | `platform/deployment.md` |
| `scripts/` (deploy, backup, restore, switch_project, etc.) | `platform/deployment.md` (Scripts table) |
| Environment variables (`DJANGO_MODE`, `DB_*`, `EMAIL_*`, `REDIS_*`, `WEBPUSH_*`, `FEDERATION_*`, `LOG_LEVEL`) | `platform/configuration.md` |
| `epicurrents/settings/` (`common.py`, `production.py`, `development.py`) | `platform/configuration.md` |
| HTTPS security headers, login rate limiting, upload size limit | `platform/configuration.md` (Security section) |
| `projects/<name>/` structure, `apps.py`, `settings.py`, lifecycle commands | `platform/project-development.md` |
| `activate_project`, `deactivate_project`, `remove_project_data`, `switch_project.sh` | `platform/project-development.md` (Lifecycle section) |
| `settings_test.py`, `tests/conftest.py`, `tests/urls.py` conventions | `platform/project-development/testing.md` |
| CI workflow (`.github/workflows/ci.yml`), pytest, ruff, pip-audit | `platform/project-development/testing.md`, `platform/deployment.md` |
| Fork vs submodule integration patterns | `platform/project-development/fork.md`, `platform/project-development/submodule.md` |
| `README.md` quick start, test commands, plugin table | `platform/README.md` |
| `recordings/` API, upload pipeline, format converters, bulk import | `README.md` (redirect to docs if detailed enough) |
| `library/` (collections, datasets, tags) | `README.md` (redirect; no dedicated platform docs page yet) |
| `federation/` (peers, grants, FUSE filesystem) | `README.md` (redirect; no dedicated platform docs page yet) |
| `user/` auth, login rate limiting, password validation | `platform/configuration.md` (Security section) |
| `activity/` audit trail, rollback | `README.md` |
| Annotation system (`annotations/`) | `README.md` |
| `notifications/` (VAPID, push) | `platform/configuration.md` (Web push section) |

> **Note:** Several backend subsystems (library, federation, annotations, activity/rollback, recordings pipeline) are documented in the `README.md` but do not yet have dedicated pages in the docs site. When a change is significant enough, consider promoting the relevant README section into a new `platform/` docs page rather than expanding the README further.

## Steps

1. **Identify changed files** — list every platform source file that changed.

2. **Map to docs pages** — use the table above to produce the list of pages to check.

3. **Read affected pages** — read each page in full. For each section ask: does this still accurately describe the current behaviour?

4. **Check README.md** — if the change affects the quick start, test commands, or the plugin table, update the platform `README.md`. Keep it ≤ 50 lines; anything detailed belongs in the docs site.

5. **Check for omissions** — new env vars, new management commands, new API endpoints, and new concepts not yet documented anywhere each need at minimum a mention.

6. **Update** — edit the affected pages. Follow the existing style:
   - Tables for env vars: `| Variable | Default | Description |`
   - Tables for commands: `| Command | Required env | Effect |`
   - Shell code blocks for CLI examples
   - `> **Important:**` callouts for destructive or irreversible operations

7. **Check the router** — if a new page was added, add it to `docs/epicurrents/src/router.ts` under `'Platform'`.

8. **Roadmap / planned items** — if a change is a partial implementation, add a `> **Planned:** ...` callout so readers know what is coming.

## Conventions

- **Tone** — operator-focused and direct. Assume the reader is deploying or configuring the stack, not reading the Django source.
- **Commands** — always show the Docker Compose form (`docker compose run --rm ...`), never bare `python manage.py`, to reinforce that management commands must run inside the container.
- **Env var tables** — list every new variable with its default and a one-line description. Mark variables with no safe default as `—` (em-dash).
- **Cross-references** — link to related platform pages with `[text](docs/platform/path)`.
- Do not add emojis.
