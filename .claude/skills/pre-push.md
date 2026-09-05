# pre-push

Reproduce the blocking CI jobs locally, cheapest first, before pushing to `main`. The point is to find the failure here rather than in a rejected push, so run the sections in order and **stop at the first failure** — report it with its output rather than continuing or working around it.

This is a Markdown instruction set, not an executable. Assistants without skill support follow it directly.

## Ground rules

- **Never run `cp .env.example .env`.** The `stack-smoke` CI job does that on a throwaway runner. On a developer machine it destroys the working configuration, including generated secrets. Section 5 covers the same ground without touching `.env`.
- **Never weaken a check to make it pass.** A failing gate means the change is not ready; editing the assertion, lowering the coverage floor, or adding an ignore is not a fix.
- **Use `.venv/bin/python`**, never a bare `python` — macOS may not have one, and the venv is where the pinned tools live.
- **Node may not be on `PATH`** in a non-interactive shell, which does not source the profile nvm installs itself into. When `node` does not resolve, prefix `~/.nvm/versions/node/<version>/bin` rather than concluding it is absent.
- **Docker Desktop must be running** for section 5, and only for section 5.
- Report each section as ran / passed / failed. "Skipped" is a legitimate result when the scope table says the section does not apply, but say so explicitly — a silent skip and a pass look identical afterwards.

## Which sections apply

Run every section whose trigger the change touches. When in doubt, run it.

| The change touches | Run |
|---|---|
| any Python file | 1, 2, 3 |
| any file under `scripts/` | 1, 5 |
| a model, or any file under `migrations/` | 1, 2, 3 |
| anything under `frontend/src/` | 4 |
| `Dockerfile`, `entrypoint.sh`, any `docker-compose*.yml` | 5 |
| `requirements*.txt`, `requirements.lock` | 1, 3, 6 |
| Markdown or comments only | 1 |

## Section 1 — Format, lint, shell (seconds)

```
.venv/bin/ruff format --check .
.venv/bin/ruff check .
bash scripts/lint-shell.sh
```

CI jobs `format`, `lint`, `shell`. The first is the one that most often rejects a push over something trivial: `ruff format` is deterministic, so `.venv/bin/ruff format .` fixes any complaint outright — run it, then re-stage the files it rewrote. [scripts/lint-shell.sh](../../scripts/lint-shell.sh) uses a host shellcheck when there is one and otherwise pulls the `koalaman/shellcheck:stable` image, so it needs either shellcheck or Docker.

## Section 2 — Migrations (under a minute)

```
DJANGO_SETTINGS_MODULE=epicurrents.settings.test_platform .venv/bin/python manage.py makemigrations --check
DJANGO_SETTINGS_MODULE=epicurrents.settings.test_platform .venv/bin/python manage.py migrate
```

CI job `migrations`. These settings use an in-memory SQLite database, so neither command writes to any file or touches the compose stack. A `makemigrations --check` failure means a model changed without its migration: generate it, do not push past it.

## Section 3 — Python test suites (several minutes)

```
.venv/bin/python -m pytest --cov --cov-fail-under=70 -q
DJANGO_SETTINGS_MODULE=projects.example.settings_test .venv/bin/python manage.py check
DJANGO_SETTINGS_MODULE=projects.example.settings_test .venv/bin/python -m pytest projects/example/tests/ -q
DJANGO_SETTINGS_MODULE=plugins.dicom.settings_test .venv/bin/python manage.py check
DJANGO_SETTINGS_MODULE=plugins.dicom.settings_test .venv/bin/python -m pytest plugins/dicom/tests/ -q
```

CI jobs `test`, `test-example`, `test-dicom`. The first command needs no settings variable — [pytest.ini](../../pytest.ini) supplies it, and [conftest.py](../../conftest.py) prunes the project and plugin trees, which is why the other two suites run separately under their own settings.

The two `manage.py check` calls are not redundant with the suites beside them. They validate each configuration against its real schema — the subject-export classification, the erasure PII registry, the `requires_platform` pin — and a model that no registry covers fails there while its own tests still pass.

## Section 4 — Frontend (a few minutes)

```
cd frontend && npm run test
```

CI job `frontend`. Vitest only. CI reaches it through `npm ci`, which deletes `node_modules` and reinstalls from the lockfile — correct on a fresh runner, wasteful and disruptive on a working machine. Run `npm ci` here only when [frontend/package-lock.json](../../frontend/package-lock.json) is part of the change, since that is the case where an already-installed tree can disagree with what CI will resolve.

Do **not** add `npm run build` to match CI, because CI does not run it: `vue-tsc` needs viewer types that are not a declared dependency yet. Build locally when a change warrants it, but a build failure there is not a CI rejection.

## Section 5 — Packaged bring-up (15 minutes or more, needs Docker)

This is the section that catches what the host-only jobs cannot, and the one worth running whenever `scripts/` or the Docker configuration changed. Both packages are assembled into a scratch directory, so the working tree and its `.env` are untouched, and compose scopes each package's volumes by directory name.

A package must run on the settings a recipient gets — default port, default network — or the smoke is exercising a configuration nobody ships. So nothing is overridden here. What that requires is that **the dev stack keeps out of the way**, which is a one-time setup rather than a per-run step: see the prerequisite below.

```
SMOKE="$(mktemp -d)"
scripts/make-bootstrap-fixture.sh "$SMOKE/fixture"
cd "$SMOKE/fixture"
./bootstrap-smoke.sh > "$SMOKE/fixture.log" 2>&1
echo "fixture smoke exit: $?"
```

Then the demo package, which is the runner a recipient actually uses:

```
scripts/make-bootstrap-fixture.sh "$SMOKE/demo" --demo
cd "$SMOKE/demo"
./start.sh > "$SMOKE/demo.log" 2>&1
echo "demo start exit: $?"
docker compose down -v
```

It needs `frontend/dist` to exist — build it with `npm run build` in `frontend/`, or reuse an existing one, since the smoke does not care what the bundle contains. `start.sh` leaves the stack running on success, so its teardown is not optional; the fixture smoke tears itself down through its own trap. Remove the scratch directory when both have passed.

**Capture to a file and read the exit status, as above. Never pipe these through `head` or `tail`** — the pipeline then reports the exit status of the pager, and a failed bring-up reads as a pass.

### Prerequisite: the dev stack does not sit on the package's port

The network is handled for you: a package is assembled onto one named after its own directory, so it cannot reach another stack on the same host whatever else is running.

The port is not, and it is the quieter of the two hazards. A package publishes `HOST_PORT`, which defaults to what a checkout uses. Both runners build their health-check URL by grepping `.env`, so a package that cannot bind the port polls it anyway — and a dev stack listening there answers `"status": "ok"`. The smoke then reports a healthy stack it never started.

Set the port in the deployment's own `.env`, which is gitignored, and leave `.env.example` alone:

```
HOST_PORT=8001
```

For the record of why the network default exists, since a package assembled before it will not have one: that name is host-wide rather than project-scoped, so two stacks sharing it share the alias `db`, and a package reaches whichever database answers first. It is refused only because each deployment generates its own password; where two share credentials, `manage.py migrate` applies migrations to the wrong database and reports success. Setting `EPICURRENTS_NETWORK_NAME` in an old package's `.env`, or rebuilding it, closes that.

**One check cannot be reproduced on macOS.** The preflight step that refuses a tree uid 1000 cannot write is guarded on `getent`, which macOS does not have, so it is skipped locally and silently. It runs on Linux and in CI. Do not read a local pass as covering it.

## Section 6 — Dependency audit (only when requirements changed)

```
./scripts/lock-requirements.sh --check
.venv/bin/pip-audit -r requirements.lock
.venv/bin/pip-audit -r requirements-test.txt -r requirements-dev.txt
```

CI job `security`. The lock check pulls a Python image and re-resolves the whole closure, so it is slow; it is also the one that fails when `requirements.txt` moved and the lock did not. The two audits are separate invocations on purpose — pip-audit resolves every `-r` in one pass, and a single hash line puts that resolution into hash-checking mode, which then rejects the range-pinned developer files.

## What this does not cover

- `stack-smoke`: the full-repo image build and stack bring-up. Section 5 exercises the same image build and migrate path from an assembled package, which is the reproducible part; the job's own `.env` handling is not safe to imitate locally.
- The uid-1000 ownership refusal, per the note in section 5.
- Anything requiring a Linux host: FUSE tests skip themselves on macOS, as does any test whose marker cannot be satisfied.

## Relationship to the other hooks

The `pre-commit` hook blocks on unresolved review-agent findings in `.review/findings/`, which is a different gate and runs at commit time. This skill is about CI rejection at push time. Neither substitutes for the [handover](handover.md) checklist, which is about whether the work is ready to present at all.
