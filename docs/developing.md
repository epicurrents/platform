# Developing on Epicurrents

For contributors working on the platform itself or building a project plugin.

This guide is the human-facing narrative. The canonical rules — style conventions, cross-cutting invariants, behaviour gotchas — live in [AGENTS.md](../AGENTS.md), which AI coding assistants also load automatically. Where this guide is brief, AGENTS.md has the full text; the link in each section takes you there.

If you're setting up a development environment for the first time, see [docs/getting-started.md](getting-started.md).

## Repository structure

```
.                                # repository root
├── README.md                    # landing card
├── AGENTS.md                    # assistant instructions + canonical rules
├── CLAUDE.md                    # pointer at AGENTS.md (Claude Code auto-discovery)
├── ROADMAP.md                   # known limitations + deferred improvements
├── docs/                        # documentation
│   ├── epicurrents/             # vendored docs site (git submodule)
│   ├── getting-started.md
│   ├── operations.md
│   ├── troubleshooting.md
│   ├── management-commands.md
│   └── developing.md            # this file
├── <app>/                       # one directory per Django app
│   ├── README.md                # canonical app docs
│   ├── apps.py
│   ├── models.py
│   ├── api/v1/ninja.py
│   ├── management/commands/
│   ├── migrations/
│   └── tests/
├── projects/                    # project plugins
│   └── example/                 # scaffolded template
├── frontend/                    # Vue 3 + Vite app
│   └── viewer/                  # Epicurrents viewer (git submodule)
├── scripts/                     # operational scripts
├── docker-compose.yml
└── ...
```

Each of the ten Django apps (`activity`, `annotations`, `compute`, `epicurrents`, `federation`, `library`, `media`, `notifications`, `recordings`, `user`) has its own README — that's the canonical source for the app's architecture, models, endpoints, signals, and gotchas. The "Backend apps" table in [AGENTS.md](../AGENTS.md#backend-apps) is the index.

## Documentation workflow

Documentation lives in three tiers:

| Tier | File | Audience |
|---|---|---|
| `AGENTS.md` | repo root | AI assistant in a session |
| App READMEs | `<app>/README.md` | In-repo developer |
| External docs | `docs.epicurrents.io` (vendored at [`docs/epicurrents/`](epicurrents/)) | Operator / external user |

When you change code that affects documented behaviour:

1. **Update the app README first.** It's the canonical human-facing source and must not lag behind code. If the affected app has no README yet, write one. Same commit as the code change.
2. **Update AGENTS.md only when a *rule* changes** — a style convention, a cross-cutting invariant, a new gotcha that applies across sessions, a new entry in the "Backend apps" table. Specific endpoints, models, or semantic detail belong in the app README.
3. **Update external docs deferred.** Update the docs site when the feature is referenced from a release note or user-facing changelog. Don't block code changes on external doc updates.

Full rule set including the in-repo README style guide: [AGENTS.md → Documentation workflow](../AGENTS.md#documentation-workflow).

## Code style

The platform uses standard tooling per language:

- **Python** — [Ruff](https://docs.astral.sh/ruff/) for linting and formatting. Configuration in [`ruff.toml`](../ruff.toml); CI runs `ruff check .` and `ruff format --check .` on every push. Run them locally before pushing — or let your editor surface them inline (see below).
- **TypeScript / Vue** — Vite's built-in type-checking plus project conventions in [AGENTS.md → Style and convention rules](../AGENTS.md#style-and-convention-rules). This covers Vue template formatting, JavaScript conventions, Vue i18n usage, the WA semantic colour tokens, and the WebAwesome button design hierarchy.

The full style rules are documented in AGENTS.md so AI assistants load them automatically. This section is the human-facing pointer; for the actual rules, follow the link.

Line endings are LF repo-wide, enforced by [.gitattributes](../.gitattributes).

### VS Code setup

The repo ships [`.vscode/settings.json`](../.vscode/settings.json) and [`.vscode/extensions.json`](../.vscode/extensions.json) so every contributor's editor surfaces the same Python diagnostics as CI. On first open, VS Code will prompt to install the recommended extensions — primarily [`charliermarsh.ruff`](https://marketplace.visualstudio.com/items?itemName=charliermarsh.ruff), which the settings file wires up as the Python formatter and import-sorter. The legacy Python linting API is turned off so it doesn't run in parallel with Ruff and surface divergent warnings; Pyright's strict type checking is also off because the project doesn't currently enforce type coverage. Override either in your personal `.vscode/settings.json` (gitignored) if you want stronger local checks — but the committed file is the baseline that matches CI.

If you use a different editor, replicate the same intent: Ruff as the Python linter and formatter, no other Python linters running on the same files, no project-wide type-checking enforcement.

## Testing

### Running the platform suite

Two paths, pick whichever fits your machine.

**Host (fastest on Linux):**

```bash
pytest
```

Collection policy lives in the root [`conftest.py`](../conftest.py) rather than a remembered set of flags: each project/plugin's own test tree is excluded from the default sweep there, and `require_fuse`-marked tests (e.g. `federation/tests/test_fuse_fs.py`) skip automatically when `fuse` does not import — no manual `--ignore` needed.

**Container (recommended on macOS / Windows, or whenever you need libfuse2):**

```bash
docker compose run --rm test                     # full suite
docker compose run --rm test federation/tests/   # one app
docker compose run --rm test -v -k auth          # arbitrary pytest args
```

The `test` service is profile-gated (`profiles: ["test"]`), so `docker compose up` never starts it. It shares the `web` image — same Python deps, same `libfuse2`, same code mount — so the `require_fuse` tests run for real here instead of skipping. SYS_ADMIN + /dev/fuse are baked into the service for future integration tests that actually mount FUSE.

The platform tests use an in-memory SQLite database, MD5 password hashing, synchronous Celery (`CELERY_TASK_ALWAYS_EAGER=True`), and locmem email — see [`epicurrents/settings/test_platform.py`](../epicurrents/settings/test_platform.py). No `db` / `redis` containers required; the `test` service has no `depends_on`.

#### Against PostgreSQL

SQLite's dynamic typing masks a class of bug that only surfaces on the production database — the canonical one is a generic-FK `object_id` (a `CharField`) compared against a model's integer primary key, which SQLite tolerates and PostgreSQL rejects with `operator does not exist: bigint = character varying`. The `test-postgres` service runs the same suite against the live `db` service so those are caught:

```bash
docker compose run --rm test-postgres                          # platform suite on Postgres
docker compose run --rm test-postgres --ds=epicurrents.settings.test_postgres media/tests/   # a subset
```

It reuses [`test_platform`](../epicurrents/settings/test_platform.py)'s fast configuration (eager Celery, deterministic audit keys, fast hasher) with only the database swapped to PostgreSQL, via [`epicurrents/settings/test_postgres.py`](../epicurrents/settings/test_postgres.py). It `depends_on` a healthy `db`, and pytest-django creates a throwaway `test_<DB_NAME>` so the dev data is untouched. When overriding the command with a path subset, repeat the `--ds` flag — it is pytest-django's highest-precedence settings selector.

**Test-only deps** live in [`requirements-test.txt`](../requirements-test.txt) (pytest, pytest-django, pytest-cov, pytest-httpserver, model-bakery); production deps stay in [`requirements.txt`](../requirements.txt). The Dockerfile installs both in one layer for dev/test; a future lean-prod multi-stage build can install only `requirements.txt`. Lint / audit deps (ruff, pip-audit) live in [`requirements-dev.txt`](../requirements-dev.txt) and run on the host.

### Running project plugin tests

```bash
DJANGO_SETTINGS_MODULE=projects.<name>.settings_test pytest projects/<name>/tests/
```

The project's `settings_test.py` must extend the platform's test settings and add the project app to `INSTALLED_APPS`. See [`projects/example/`](../projects/example/) for the canonical scaffold (file lives at `projects/example/settings_test.py` in projects that include it).

### Fixtures and helpers

Global pytest fixtures live in the root [`conftest.py`](../conftest.py):

- `user`, `superuser` — pre-created users.
- `make_user`, `make_superuser` — factories that generate unique usernames per call.
- `auth_client`, `superuser_client` — Django test clients pre-authenticated.
- `post_json`, `patch_json`, `delete_json` — JSON API request helpers.

App-specific fixtures live in `<app>/tests/conftest.py` where they exist.

### Federation integration tests

The federation app has a Phase-1 integration test layer that stands up a real HTTP server playing the role of a remote peer (using [`pytest-httpserver`](https://pytest-httpserver.readthedocs.io/), declared in [`requirements-test.txt`](../requirements-test.txt)). The fixture is [`federation/tests/conftest.py::mock_federated_peer`](../federation/tests/conftest.py) and tests built on it live in [`federation/tests/test_integration.py`](../federation/tests/test_integration.py). They exercise the real outbound `urllib` stack and the inbound JWT pipeline through Django's request layer — covering paths that the existing `urlopen`-mocked unit tests do not. Combined runtime ~2 s; runs as part of the normal `pytest` invocation, no extra flags needed.

A Phase-2 two-instance smoke suite (Docker Compose with `mkcert`-issued TLS) is still future work; see the "Testing — federation integration test harness" entry in [ROADMAP.md](../ROADMAP.md) for the scope.

### Bootstrap-smoke fixture

[scripts/make-bootstrap-fixture.sh](../scripts/make-bootstrap-fixture.sh) assembles a minimal copy of the platform — Docker config plus the backend Django apps, no frontend or project plugins — into a destination directory, with a generated runner that builds the image, generates `.env` via `init_env`, brings up `web` + `celery`, and checks the health endpoint. `--with-frontend` and `--with-project <name>` opt those parts in. Its assembly logic is unit-tested in [scripts/tests/test_make_bootstrap_fixture.py](../scripts/tests/test_make_bootstrap_fixture.py) (a real run against a tmp directory, no Docker), which the `test` CI job picks up.

Two CI jobs exercise the bring-up. `stack-smoke` builds the full repo image and brings the stack up — proving the deploy artifact works. `bootstrap-fixture-smoke` assembles the fixture and runs its smoke — proving the backend is self-sufficient: a backend path that quietly grows a dependency on the frontend or a project plugin passes `stack-smoke` (the full tree is present) but fails the fixture job.

That second job then assembles a `--demo` package and runs its start script end to end, because the runner a recipient actually uses shares no code with the CI fixture's. Two things are faked and worth knowing before reading a green result as more than it is. The compiled UI is a stub file that exists only to satisfy the mode's guard — a real one needs `npm run build`, whose `vue-tsc` half is the same blocker that stops the frontend job at vitest — so the job proves the package's scripts work, not that anything renders. And the package is made world-writable rather than handed to uid 1000, since a runner is uid 1001 and cannot become the account a real deployment runs as; that is the other shape the start script's preflight accepts. The step before it asserts the preflight refuses the tree as the runner first receives it, which is the one place that check runs against a real filesystem instead of a stubbed `stat`.

The same script produces two human-facing packages instead of the CI fixture. `--demo` builds a browsable base-UI package: it bundles the prebuilt `frontend/dist` (build it first with `npm run build` in `frontend/`), omits `viewer-dist` and the frontend source, activates no project, and generates a human start script (brings the stack up and leaves it running) plus a getting-started README. A recipient with only Docker runs that start script and browses the base UI at `localhost:8000`; the embedded signal viewer is not included, so opening a recording does not work in a demo package. Both package modes also carry a prepare-host.sh for the case where the host has no Docker and no account to run as — it installs the engine from [scripts/lib/install-docker.sh](../scripts/lib/install-docker.sh), the same file [scripts/bootstrap.sh](../scripts/bootstrap.sh) sources, and is bundled rather than regenerated so the two paths cannot drift onto different engines.

`--dist` builds a runnable distribution: like `--demo` but it also bundles the compiled `viewer-dist` (build it with `npm run build:viewer`), so the signal viewer works, and with `--with-project <name>` it bundles and activates that project — the full experience for a given project. `--demo` and `--dist` are mutually exclusive, and both ship the prebuilt bundles rather than the source. Both also give the package its own Docker network, named after the destination directory, so it cannot reach another stack on the same host; `--network-name` overrides that where joining an existing network is deliberate. (For a project that needs a frontend submodule, such as dicom's OHIF viewer, the distribution needs that submodule built and bundled separately — a follow-up, not handled by the flag yet.)

### Test conventions and gotchas

The mock paths (`pywebpush.webpush` not `notifications.tasks.webpush`), `auto_now_add` field backdating, `AccessRight` setup requirements for annotation listing, and reset-password rate-limit handling in parallel tests are all documented in [AGENTS.md → Testing conventions](../AGENTS.md#testing-conventions). Worth a read before writing your first test against a new app.

## Commits and pull requests

### Commit messages

Subject lines follow [Conventional Commits](https://www.conventionalcommits.org/) with a scope:

```
<type>(<scope>): <imperative subject>
```

Examples:

```
feat(federation): add peer revocation endpoint
fix(activity): handle null actor in rollback path
docs(recordings): document the upload contract
refactor(epicurrents): split audit.py into focused submodules
test(activity): add bulk-rollback ordering test
chore(deps): bump psycopg to 3.2
```

#### Types

| Type | Use for |
|---|---|
| `feat` | A new user-visible feature or capability |
| `fix` | A bug fix restoring previously-documented behaviour |
| `docs` | Documentation only (READMEs, AGENTS.md, this file, the docs site) |
| `refactor` | Behaviour-preserving code change — restructuring, renaming, splitting |
| `test` | Adding or restructuring tests with no production-code change |
| `chore` | Tooling, lockfiles, version bumps, CI config, generated files |
| `perf` | Optimisation that's strictly faster without changing behaviour |
| `style` | Formatting only — whitespace, comments, no logic changes |
| `build` | Build-system or external dependency changes (`docker-compose.yml`, `Dockerfile`, `pyproject.toml` deps section) |
| `ci` | GitHub Actions config, pre-commit hooks |
| `revert` | Reverts a previous commit |

Reach for the type that best describes the *primary* user-visible effect. When two types fit (e.g. `feat` + `docs`), pick the dominant one and mention the other in the body.

#### Scopes

Scopes are Django app names plus a few non-app slots:

| Scope | Covers |
|---|---|
| `activity` | `activity/` |
| `annotations` | `annotations/` |
| `compute` | `compute/` |
| `epicurrents` | `epicurrents/` (core app — permissions, middleware, settings, project loader) |
| `federation` | `federation/` |
| `library` | `library/` |
| `media` | `media/` |
| `notifications` | `notifications/` |
| `recordings` | `recordings/` |
| `user` | `user/` |
| `frontend` | `frontend/src/` (the Vue SPA) |
| `viewer` | `frontend/viewer/` (the embedded viewer monorepo) |
| `docs` | `docs/`, app READMEs, AGENTS.md, this file, the external docs site |
| `infra` | `docker-compose.yml`, `Dockerfile`, `scripts/`, `.env.example`, deployment files |
| `deps` | Lockfiles, `pyproject.toml` deps, `package.json` deps |
| `tests` | When the change is exclusively test-infrastructure (conftest, fixtures, integration harness) and doesn't fit one app's `tests/` |

For project plugins, use the project name as the scope (e.g. `feat(example): add note-export endpoint`).

**Multi-scope commits drop the scope** rather than picking one. `chore: bump dependencies and rebuild lockfiles` is preferable to `chore(infra/deps): ...`. If a single commit genuinely spans many areas, that's often a signal to split it; if it has to land as one, the scope-less form is the honest version.

#### Subject line

- **Imperative.** "Add X" not "Added X" / "Adds X".
- **Lower-case after the colon.** `feat(federation): add peer revocation` not `feat(federation): Add peer revocation`.
- **No trailing period.**
- **Wrap at ~72 characters** for the whole subject including the prefix.

#### Body

Optional but encouraged when the change isn't self-evident from the diff.

- **Explain why, not what.** The diff shows what; the body explains the motivation, the constraint, the prior incident.
- **Reference the related issue, ROADMAP entry, or app README section** when one exists.
- **Wrap body lines at 72 characters.**

#### One coherent change per commit

Bundle small refactors with their motivating change; split unrelated work. When in doubt, smaller is better — a 50-line commit is easier to bisect, review, and revert than a 500-line one.

#### Enforcement

The format is **enforced at the PR-title level**, not on individual commits — because the default merge method is *squash and merge*, which collapses every commit on the branch into one whose message is the PR title. Local commits during development can be loose; the PR title is the one that ends up in `main`'s history.

A `commitlint` GitHub Action will enforce the format on PR titles before merge — adoption tracked in ROADMAP. Until that lands, the convention is enforced by reviewer judgment.

### Branches and pull requests

- Topic branch named `<area>/<short-description>` (e.g. `federation/jwt-leeway`, `docs/troubleshooting-update`).
- Tests accompany the code change in the same PR.
- Documentation-only PRs are fine without tests.

### Signing

Commits should be signed (GPG or SSH key) for release-bound work.

### Working with an AI assistant

AI assistants are a first-class part of the contributor workflow. Point your assistant at the repository (or the relevant files), describe the change you want to make, and let the assistant draft. The same review standards apply to AI-assisted commits as to hand-written ones — read the diff, run the tests, sign as your own work.

AGENTS.md instructs assistants to:

- Update the app README in the same commit as a code change.
- Follow the in-repo README style guide (no restating, no draw-a-picture codas, justification over explanation).
- Use the existing fixtures and helpers when writing tests.
- Match the existing commit message style.

If you spot an assistant violating one of these, the rule is in AGENTS.md and can be updated there.

## Adding new code

### Adding a new Django app

Rare. The platform's ten apps cover the existing domain; most extensions fit into an existing app or a project plugin. If a new app is genuinely needed:

1. `docker compose run --rm web python manage.py startapp <name>` from the repo root.
2. Add `"<name>.apps.<Name>Config"` to `INSTALLED_APPS` in [`epicurrents/settings/common.py`](../epicurrents/settings/common.py).
3. Create `<name>/README.md` covering the sections in [AGENTS.md → Minimum contents of an app README](../AGENTS.md#minimum-contents-of-an-app-readme).
4. Add a row to the "Backend apps" table in [AGENTS.md](../AGENTS.md#backend-apps).
5. Generate and apply the initial migration with FK fields inlined into `CreateModel` (see [AGENTS.md → Initial migrations](../AGENTS.md#initial-migrations)).

### Adding a project plugin

The full step-by-step walkthrough is at [docs/getting-started.md → Starting a new project plugin](getting-started.md#starting-a-new-project-plugin). The [`projects/example/`](../projects/example/) directory is heavily commented with worked examples of every extension point: models, settings, URL endpoints, EDF middleware.

The plugin system supports:

- Custom models with FKs to platform models.
- API endpoints mounted at `/project/api/v1/`.
- Settings overrides (list-append for `INSTALLED_APPS` etc., dict-merge for `CELERY_BEAT_SCHEDULE`, replace otherwise).
- Custom read-permission rules via `register_read_permission_extension(callable)` from `apps.py::ready()`.
- EDF/BDF middleware via subclassing `EDFHeaderMiddleware` / `EDFSignalMiddleware`.

For switching between projects during development use [`scripts/switch_project.sh`](../scripts/switch_project.sh) rather than running the lifecycle commands by hand.

## Where to ask questions

- **Architecture** — read the relevant app's README first. They answer "how does this app work?" before you ask.
- **Behaviour rules / style** — [AGENTS.md](../AGENTS.md).
- **Operations on a running stack** — [docs/operations.md](operations.md).
- **Diagnosing failures** — [docs/troubleshooting.md](troubleshooting.md).
- **Known issues and deferred improvements** — [ROADMAP.md](../ROADMAP.md).
- **Anything else** — open an issue on GitHub.
