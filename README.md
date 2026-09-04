# epicurrents platform

Django 6 + Django Ninja REST API, Celery workers, Vue 3 + TypeScript frontend, PostgreSQL, Redis. Deployed via Docker Compose.

**Full documentation:** [docs.epicurrents.io](https://docs.epicurrents.io) (also vendored offline at [`docs/epicurrents/`](docs/epicurrents/)).

## AI-agent friendly

Epicurrents is aimed at researchers, clinicians, and technicians working with neurophysiological signal data — not only programmers. You don't need extensive programming experience to get started. The technical documentation is structured so an AI coding assistant can read it and walk you through setting up an instance, running common operations, or starting your own customisation, one step at a time.

Three practical AI-assistant arrangements work with this repository:

- **Agent with filesystem access** (Claude Code, Cursor, Aider, Copilot in your editor) — clone the repository locally; the assistant operates on your files directly.
- **Web-only agent** (ChatGPT, Claude.ai, Gemini in a browser) — point the assistant at [AGENTS.md on GitHub](https://github.com/epicurrents/platform/blob/main/AGENTS.md). It reads the docs remotely and dictates commands you run on your own machine.
- **Cloud dev environment** (GitHub Codespaces) — open the repository on GitHub, click "Open in Codespace"; you install nothing locally, and the assistant inside the Codespace has filesystem access from the start.

See [docs/getting-started.md](docs/getting-started.md) for the step-by-step walkthrough once you've picked a mode.

## Quick start

```bash
git clone https://github.com/epicurrents/platform epicurrents && cd epicurrents
./scripts/bootstrap.sh
# Edit .env — set ALLOWED_HOSTS, DB_*, ADMIN_*, EMAIL_*, and optionally EPICURRENTS_PROJECT
./scripts/bootstrap.sh
```

On a fresh Ubuntu/Debian host, `bootstrap.sh` installs git + Docker if missing, initialises submodules, generates `.env` with random secrets, builds the frontend bundles, initialises the local Borg backup repository, and starts the stack. The first invocation exits after writing `.env` so you can review it; the second invocation finishes the bootstrap.

For the full walkthrough including project plugin notes, see [docs/getting-started.md](docs/getting-started.md).

## Running tests

```bash
# Install runtime, test and lint dependencies
pip install -r requirements.txt -r requirements-test.txt -r requirements-dev.txt

# Platform tests (no project, no plugins)
pytest

# Project tests
DJANGO_SETTINGS_MODULE=projects.<project>.settings_test pytest projects/<project>/tests/

# Plugin tests
DJANGO_SETTINGS_MODULE=plugins.<plugin>.settings_test pytest plugins/<plugin>/tests/

# Platform tests against PostgreSQL (catches type bugs SQLite hides)
docker compose run --rm test-postgres
```

The bare `pytest` is deliberate: what is and is not part of the platform suite is
decided in [`conftest.py`](conftest.py) and applies to every invocation, rather
than living in flags one has to remember. It excludes each plugin/project test
tree whose own settings module is not the
active one — those fail while *importing*, during collection, so they take the
whole run down with them and no skip mark can intervene. Tests needing a usable
libfuse now skip themselves via `require_fuse` instead of being ignored by path,
so they run where the library is installed.

See [docs/developing.md](docs/developing.md#testing) for fixtures, helpers, and test conventions.

## Documentation

| Topic | Where |
|---|---|
| Set up a fresh deployment or development environment | [docs/getting-started.md](docs/getting-started.md) |
| Day-to-day operations on a running stack | [docs/operations.md](docs/operations.md) |
| **Operator/sysadmin:** a service crashed or the system is down | [docs/operator-runbook.md](docs/operator-runbook.md) |
| **Developer:** troubleshooting a specific symptom | [docs/troubleshooting.md](docs/troubleshooting.md) |
| **Developer:** debugging *why* a failure happened (logs, failure fields, tasks) | [docs/debugging.md](docs/debugging.md) |
| Index of all management commands | [docs/management-commands.md](docs/management-commands.md) |
| Contributing to the platform | [docs/developing.md](docs/developing.md) |
| Reporting a security vulnerability | [SECURITY.md](SECURITY.md) |
| GDPR posture: data inventory, retention, erasure paths, processor flows | [docs/gdpr-compliance.md](docs/gdpr-compliance.md) — update in the same commit as any change to personal-data models, retention windows, or outbound flows (the `gdpr-compliance` review agent enforces the inventories); full re-audit before each production release or six-monthly |
| Telling data subjects what you do with their data (Art. 13/14) | [docs/privacy-notice-template.md](docs/privacy-notice-template.md) — a drafting template, not a notice; the software-determined facts are filled in and the rest is marked for the operator, who is the controller |
| Known limitations + deferred improvements | [ROADMAP.md](ROADMAP.md) |
| AI assistant behaviour rules | [AGENTS.md](AGENTS.md) |
| Per-app architecture | each app has its own `<app>/README.md` (index in [AGENTS.md](AGENTS.md#backend-apps)) |
| Vendored external documentation | [docs/epicurrents/](docs/epicurrents/) |

## Project plugins

A *project plugin* is a self-contained customisation layer that lives under [`projects/<name>/`](projects/) and adds deployment-specific behaviour without modifying the platform itself. Only one project is active per deployment, selected via `EPICURRENTS_PROJECT` in `.env`.

| Extension point | How |
|---|---|
| New models, API endpoints, EDF/BDF middleware | Add files in `projects/<name>/` — `models.py`, `urls.py`, `middleware.py` |
| Override platform settings | `projects/<name>/settings.py` (list-append for `INSTALLED_APPS` etc., dict-merge for `CELERY_BEAT_SCHEDULE`, scalar-replace otherwise) |
| Custom read-permission rules | `register_read_permission_extension(callable)` from `apps.py::ready()` |
| Project-specific user fields | `OneToOneField` profile model with `<name>_profile` related name |
| Project-specific annotations / labels | `annotations.Code` rows with `standard = "epicurrents.<name>.<concept>"` |

See [docs/getting-started.md → Starting a new project plugin](docs/getting-started.md#starting-a-new-project-plugin) for the step-by-step walkthrough, and [`projects/example/`](projects/example/) for the heavily-commented scaffolded template.

## Plugins

A *plugin* is the composable sibling of a project: zero or more may be enabled per deployment via `EPICURRENTS_PLUGINS` in `.env`, each adding functionality (models, API, frontend routes) alongside whatever project is active without owning the landing page. Enable one with `scripts/enable_plugin.sh <name>`. See [docs/plugins.md](docs/plugins.md) for the operator walkthrough, [plugins/README.md](plugins/README.md) for the authoring contract, and [`plugins/dicom/`](plugins/dicom/) (DICOM studies + OHIF viewer) for the reference plugin.

## Design decisions worth knowing before you read the code

Three ceilings are decisions, not oversights. They are stated here so nobody re-derives them from the code and mistakes them for accidents.

- **EDF is the hub format.** Every ingested recording is converted to EDF, so signals are quantised to 16-bit integers and the canonical channel layer is EEG-shaped. The trade was accepted deliberately: the integrity chain, the de-identification middleware and the byte-level sanitisation are coupled to EDF's fixed byte layout, and that coupling is what lets the platform prove what left the server. Formats that need more than EDF can express lose that precision on ingest.
- **One instance per deployment.** The platform scales by running one stack per organisation, federated over a tailnet, not by sharding one stack across many. There is no multi-tenant isolation inside an instance and none is planned.
- **Benchmarking is roadmap, not a shipped feature.** The substrate exists — datasets are content-addressed, and immutable dataset snapshots pin membership so "model X on dataset Z" is reproducible — but the run/experiment model that records a tool, its configuration, a snapshot and its metrics as one audited object does not exist yet. See [ROADMAP.md](ROADMAP.md).

## History

This repository started from a reset history in September 2026: the platform was extracted from a private repository that also carried project plugins and unpublished research. That repository, `platform-archive`, remains the archive of record — pre-reset rationale for design decisions lives in its commit history and its engineering notes, and the short history here is not evidence that the reasoning was never done. Version numbering continues from the archive rather than restarting.

## Contributing

See [docs/developing.md](docs/developing.md) for repository structure, code conventions, testing patterns, and the contribution flow. After cloning, run `./scripts/install-dev-tools.sh` once (`bootstrap.sh` does it for you) to install the project's git hooks — the pre-commit hook refuses a commit while a review agent has unresolved findings under `.review/findings/`. AI-assisted contributions are a first-class workflow — see [AGENTS.md](AGENTS.md) for the rules assistants follow during development.
