# commit-message

Inspect the current diff and propose one or more commit messages that follow the repo's Conventional Commits + scope convention.

## When this skill applies

Invoke when the user asks for help formulating a commit message — phrasings like *"draft a commit message"*, *"what should the commit message be"*, *"help me commit this"*, or anything similar.

Do **not** invoke this skill to actually create the commit. The skill drafts; the user reviews and runs `git commit` themselves (or asks separately).

## Reference

Canonical spec: [docs/developing.md → Commit messages](../../docs/developing.md#commit-messages).
Compact summary: [AGENTS.md → Commit messages](../../AGENTS.md#commit-messages).

Format: `<type>(<scope>): <imperative subject>`.

**Types:** `feat`, `fix`, `docs`, `refactor`, `test`, `chore`, `perf`, `style`, `build`, `ci`, `revert`.

**Scopes:** `activity`, `annotations`, `compute`, `epicurrents`, `federation`, `library`, `notifications`, `recordings`, `user`, `frontend`, `viewer`, `docs`, `infra`, `deps`, `tests`. For project plugins: the project name (e.g. `example`).

## Procedure

### Step 1 — Inspect the change

Run these in parallel:

```bash
git status --short
git diff --cached --stat
git diff --stat
```

If nothing is staged AND there are unstaged changes, also run `git diff --stat` and treat the unstaged changes as the candidate change. If something is staged, prefer the staged set.

For the dominant-file paths, read enough of the diff to understand the *nature* of the change — don't just classify by directory.

```bash
git diff --cached  # or git diff if nothing is staged
```

For diffs over ~300 lines, sample the largest hunks rather than reading everything.

### Step 2 — Pick the type

Walk through the type table mentally, looking for the *primary* user-visible effect:

| Signal in the diff | Type |
|---|---|
| Net-new endpoint, new model field, new user-visible capability | `feat` |
| Diff restores documented behaviour (test was failing, README claim wasn't true) | `fix` |
| Only `.md` files, docstrings, or comments changed | `docs` |
| Code restructured but behaviour preserved (renames, splits, extractions) | `refactor` |
| Only `test_*.py` or `tests/` files changed | `test` |
| Lockfile bumps, generated files, CI config, tooling | `chore` |
| Strictly-faster change with no behaviour delta (caches, batched queries) | `perf` |
| Whitespace, formatter output, import reorder only | `style` |
| `pyproject.toml` deps, `package.json` deps, `docker-compose.yml`, `Dockerfile` | `build` |
| `.github/workflows/`, `.pre-commit-config.yaml`, `ruff.toml`, similar | `ci` |
| `git revert` output | `revert` |

When two types fit:
- `feat` beats `docs` if the docs explain the new feature.
- `fix` beats `refactor` if the restructure was triggered by the bug.
- `refactor` beats `chore` if behaviour-preserving code changed.
- Mention the secondary aspect in the body when it's load-bearing.

### Step 3 — Pick the scope

Look at the file paths in the diff:

- All files under `activity/` → `activity`.
- All files under `epicurrents/` → `epicurrents`.
- All files under `frontend/src/` → `frontend`.
- All files under `frontend/viewer/` → `viewer`.
- All `.md` files outside an app's README → `docs`.
- `docker-compose.yml`, `Dockerfile`, `scripts/`, `.env.example` → `infra`.
- Lockfiles, deps blocks → `deps`.
- Files under `projects/<name>/` → `<name>` (the project name).

**Multi-scope rule.** If the diff genuinely spans more than one scope and there's no clear primary, **drop the scope** entirely: `chore: bump dependencies and rebuild lockfiles`. Don't invent compound scopes like `chore(infra/deps): ...`.

Edge cases:
- A change to `epicurrents/` that includes its own tests is still scope `epicurrents` (the tests serve the app).
- A change to AGENTS.md plus one app's code: if the AGENTS.md change is *because of* the code change (e.g. a new rule), use the app's scope and mention the AGENTS.md update in the body. If the AGENTS.md change is independent (e.g. clarifying an existing rule), it's a separate `docs:` commit.
- A `tests/integration/` change that exercises multiple apps: scope `tests`.

### Step 4 — Compose the subject

- Imperative ("add", "fix", "tighten", "document"). Not "added"/"fixes"/"documenting".
- Lower-case after the colon.
- No trailing period.
- Aim for ~72 characters total including the prefix. Hard cap at ~100.
- Focus on *what changed at the user-visible level*, not the implementation detail.

Good:
- `fix(epicurrents): close audit-trail gap on app-prefixed API mounts`
- `feat(federation): add per-peer download rate limit`
- `docs(recordings): document the EDF de-identification contract`

Bad:
- `fix(epicurrents): change regex` — implementation detail, not user-visible effect
- `Add audit middleware fix` — missing type/scope, capitalised, vague
- `feat(federation): added peer revocation endpoint with all the routing wiring and tests and the documentation update too please review` — way too long

### Step 5 — Decide on a body

Include a body when:
- The *why* is non-obvious from the diff (a constraint, a prior incident, a downstream gotcha).
- The change references a ROADMAP entry, an issue, or a README section.
- A reviewer would need context to assess the change.

Skip the body when:
- The subject is genuinely self-explanatory.
- The change is mechanical (renames, formatter output, docstring additions).

Body conventions:
- Blank line between subject and body.
- Wrap at 72 chars.
- Explain why, not what. The diff shows what.
- Reference the trigger: `Closes #N`, `Per ROADMAP — *Annotations — wire share-token attribution*`, `See activity/README.md → "Known limitations"`.
- For load-bearing changes, point at the contract test.

### Step 6 — Present the suggestion

Show the user **one** suggestion when the change is unambiguous, **two** when there's a real type/scope choice to make.

Format the output as a fenced block the user can copy verbatim:

```
fix(epicurrents): close audit-trail gap on app-prefixed API mounts

The middleware's path matcher had been tightened from "in" to
"startswith" in the activity audit (commit 0cdc545), which silently
stopped matching /annotations/api/v1/..., /compute/api/v1/...,
/recordings/api/v1/..., and /project/api/v1/.... Every endpoint
mounted under those prefixes lost its audit trail without any visible
test failure.

Fix: regex matching /api/v<N>/... and /<app>/api/v<N>/...; contract
test in epicurrents/tests/test_middleware_path_recognition.py walks
the URL config and asserts every mounted api/v<N>/ path is recognised.

Per AGENTS.md → Load-bearing files (new section).
```

When showing two options, label them clearly and give a one-line rationale per option, e.g.:

> Option A — emphasises the bug fix:
> ```
> fix(epicurrents): close audit-trail gap on app-prefixed API mounts
> ```
> Option B — emphasises the new contract:
> ```
> feat(epicurrents): backstop audit-trail coverage with URL-enumeration test
> ```
> A reads better if the fix is the point of the commit; B reads better if the test is the point. Pick A here — the test exists *because* of the fix.

## Anti-patterns to avoid

- **Don't invent new types.** If nothing fits, use `chore` and explain in the body.
- **Don't invent new scopes.** If the change doesn't fit any listed scope, ask the user before adding one to the spec — scopes are part of the spec, not free-form.
- **Don't pluralise the type.** It's `chore`, not `chores`. `feat`, not `feats`.
- **Don't combine with branch-style prefixes.** `feat: federation/add-peer-revoke` is wrong; the scope goes in the parentheses, not the description.
- **Don't include the file path in the subject.** `fix: update activity/signals.py` is too low-level; say what changed conceptually.
- **Don't omit the Co-Authored-By trailer when the user instructs you to commit on their behalf** — but the skill itself doesn't add it; that's the `Bash` tool's `git commit` invocation's job (see CLAUDE.md / system instructions).

## When to push back

- If the staged diff actually contains two unrelated changes, **say so** and suggest splitting before drafting messages. Don't paper over a sloppy stage.
- If the change touches a LOAD-BEARING file (see [AGENTS.md → Load-bearing files](../../AGENTS.md#load-bearing-files)) and the contract test hasn't been run, **note this** before drafting — the message will need to mention the contract test, and the user should confirm it's green.
