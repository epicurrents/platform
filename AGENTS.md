# Epicurrents — Instructions for AI coding assistants

This file is the entry point for AI coding assistants working in this repository. It captures the conventions, gotchas, and process rules that don't change between sessions. App-specific architecture lives in per-app READMEs (linked from the "Repository orientation" section below).

The content is **tool-agnostic**: the conventions apply to any AI coding assistant (Claude, GPT-class, Cursor, Copilot, etc.). A few subsections under "Session hooks" reference Claude Code-specific features (`/skill` commands, `.claude/skills/` files); assistants using other tools should follow the underlying intent rather than the literal command.

## Helping the user get started

When the user's first message in a session is conversational or asks for help getting started — rather than describing a specific task — do **not** dump a list of options or proceed silently on a guess. Instead:

1. Briefly acknowledge what the repo is, in one sentence.
2. Ask one open-ended clarifying question to understand what they're trying to do.
3. Based on their answer, walk them through the relevant guide one decision at a time. Don't paste the whole guide — read it, restate the next step in plain language, wait for their input or confirmation, then proceed.

### When to apply this behaviour

**Apply** when the first message is:
- A bare greeting (`hi`, `hello`, `hey`).
- An explicit help request (`help me get started`, `I'm new here`, `what can I do here`, `how do I begin`).
- Otherwise conversational with no actionable task.

**Skip** when the first message contains a specific task or question:
- `audit X`, `find the bug in Y`, `add a feature to Z`.
- A specific question about code (`how does the permission system work`).
- A directive to inspect (`tell me about the federation app`).

When in doubt, lean toward skipping. False positives (showing the greeting when the user wanted a task done) are annoying; false negatives (just doing the task) are harmless.

### Suggested opening

Use this shape where possible to keep behaviour consistent across sessions:

> Welcome — this is the Epicurrents platform, a Django + Vue neurophysiological signal viewer with REST API, background workers, and optional inter-instance federation. Before I show you around, what are you trying to do? Common starting points are setting up a fresh deployment, joining an existing one, starting your own project plugin, or just exploring without a specific goal yet — but if you have a different intent, just describe it.

### Routing the user's intent

Once their intent is clear, locate the relevant guide and walk them through it step-by-step:

| Intent | Where to look |
|---|---|
| Fresh deployment | `docs/getting-started.md` (deployment section), `scripts/bootstrap.sh` |
| Joining or installing existing | `docs/getting-started.md` (install / clone section) |
| Starting a new project plugin | `docs/getting-started.md` (project plugin section) and `projects/example/` as the scaffolded template |
| Packaging a demo or a project distribution to share | `docs/getting-started.md` (packaging section); `scripts/make-bootstrap-fixture.sh --help` for `--demo` / `--dist` |
| Operator restoring a crashed/degraded system | `docs/operator-runbook.md` (black-box; escalates to developer docs) |
| Debugging / operating (developer) | `docs/troubleshooting.md`, `docs/debugging.md`, and `docs/operations.md` |
| Just exploring | This file's "Repository orientation" section; per-app READMEs for depth |

The `docs/` guides land in subsequent commits of this restructure; if they don't exist yet in the current tree, fall back to the relevant app README, `projects/example/`, or `docs/epicurrents/` (the vendored external docs).

## Documentation workflow

Documentation lives in three tiers, each with a defined audience:

| Tier | File(s) | Audience | Contains |
|---|---|---|---|
| `AGENTS.md` | This file | AI assistant in a session | Process rules, style conventions, repo orientation, cross-cutting rules, gotchas, recipes |
| App READMEs | `<app>/README.md` | In-repo developer | Substantive architecture: models, endpoints, signals, extension points |
| External docs | `docs/epicurrents/` (git submodule) and `docs.epicurrents.io` (rendered) | Operator / external user | Deployment, configuration, project-development tutorials |

Always add relevant README entries and code documentation without asking for separate permission.

### When to update what

After any non-trivial change that affects documented behaviour:

1. **App README first.** This is the canonical human-facing source and must not lag behind code. If the affected app has no README yet, write one. Same commit as the code change.
2. **AGENTS.md only when the change affects a rule.** New endpoints, models, or semantic detail do **not** belong here — they go to the app README. AGENTS.md changes when a *rule* changes: a new style convention, a new cross-cutting invariant, a new gotcha that must be applied across future sessions, or a new entry in the "Backend apps" table.
3. **External docs deferred.** Update `docs.epicurrents.io` (in `docs/epicurrents/`) when the feature is referenced from a release note or user-facing changelog. Do not block code changes on it.

### Split between AGENTS.md and app READMEs

- AGENTS.md is **lean and instructional**. If a section grows past "when X, do Y", it belongs in an app README.
- App READMEs are **the canonical narrative**. Architecture, models, endpoints, signals, extension points, and app-specific gotchas live here.
- The AGENTS.md "Backend apps" table is one line per app + a link to its README. That link is what keeps AGENTS.md from growing without bound.
- When AGENTS.md and an app README appear to overlap, the README is authoritative. AGENTS.md may carry a one-sentence summary plus the link, never the full detail.

### Minimum contents of an app README

- Purpose (one paragraph)
- Models (each: what it represents, key fields, relationships)
- API endpoints (table; link to source for full request/response detail)
- Signals / Celery tasks
- Settings / env vars consumed by the app
- Extension points (how a project plugin hooks in through this app)
- Gotchas / common pitfalls specific to the app

### In-repo README style

App READMEs target developers working in this codebase, not external users. Tone is matter-of-fact; trust the reader.

- **No restating.** Don't paraphrase a sentence you just wrote.
- **No "draw a picture" codas.** "X — just Y" / "X, so Y can be inferred" / "(i.e. X)" patterns that spell out implications a developer reader will draw on their own.
- **No glossing of standard terms.** A parenthetical that clarifies a domain term ("atomically (all succeed or none do)") is fine when the term may be ambiguous in context. A parenthetical that re-explains a referenced concept ("`CommandError` (non-zero exit)") is noise.
- **Justification over explanation.** Em-dash and parenthetical asides carry weight when they explain *why* a choice was made. They become noise when they explain what a referenced term already means.
- **Some emphasis is fine.** Bold leads in lists give the eye an anchor. Prose bold for emphasis ("**never**", "**absolutely**") should be reserved for genuine warnings.
- **Single asides for colour are fine.** A one-off observation that makes a section memorable without being load-bearing can stay — but don't repeat the same colour twice.

When prose in a README refers to a concept documented under a level-2 heading elsewhere in the same file, link the natural phrase to that heading using a fragment link (e.g. `[rolling back](#rollback)`). Only link to level-2 headings (`##`), not deeper. Do not add links inside code blocks or table cells that contain only a path or command.

## Session hooks

> The first three subsections below reference `.claude/skills/` files and Claude Code's `/skill` command system. They are Claude Code-specific; assistants using other tools (Cursor, Copilot, GPT-class, etc.) should follow the underlying intent — run the matching documentation update after the matching kind of change — even though they cannot execute the `/skill` literally. The `npm run build` instruction at the end of "Frontend sessions" is tool-agnostic.

### Viewer library documentation

After any change to `frontend/viewer/epicurrents/` that affects the public API, user-visible behaviour, or a concept described in the viewer docs, run `/update-viewer-docs`. The skill is at `.claude/skills/update-viewer-docs.md` and contains the full page-to-concept mapping and update checklist. Do not skip this for new features, changed method signatures, or modified semantics — only skip it for pure internal refactors, test changes, and bug fixes that restore already-documented behaviour.

### Platform documentation

After any platform change (Django apps, API endpoints, env vars, settings, CI jobs, project plugin system, deployment) that affects documented behaviour, run `/update-platform-docs`. The skill is at `.claude/skills/update-platform-docs.md`. Skip for internal refactors, test-only changes, and bug fixes that restore existing behaviour.

### Frontend sessions

At the start of any session that involves frontend work, open and read `.claude/skills/check-frontend-imports.md` before making changes to Vue files. This is a Markdown instructions file, not an executable script.

After making major changes to frontend files (new components, exports, significant refactors), run `npm run build` from the `frontend/` directory and confirm it succeeds before reporting the work as done.

### Handover checklist

**No piece of work is finished, staged, or presented as ready until the handover checklist has run and its report is given — every time, including when every step comes back clean.** The checklist is the `/handover` skill ([.claude/skills/handover.md](.claude/skills/handover.md)); assistants without skill support follow that file directly. Standing permission — running it is a step in the workflow, not a favour to request.

Its four steps, in order: the review agents matched to what the change touches (the trigger table lives in the skill), the clean-slate pass over the finished code, the ROADMAP and documentation refresh, and the handover report naming what each step returned. A green suite substitutes for none of them, and silence cannot be distinguished from a pass that never ran — the report says "nothing found" deliberately.

## Style and convention rules

### Line length

- **Python and TypeScript source wrap at a 120-column soft cap** — code, docstrings, and comments alike. Break lines that would exceed it; the editor ruler sits at column 120. This is the `line-length` set in [ruff.toml](ruff.toml) for Python. (Commit-message wrapping is a separate, tighter rule — see below.)
- **Exception — TypeScript `@param` (method parameter) docstrings stay on one line regardless of length.** Wrapping a `@param` onto a second line renders poorly in the VS Code hover / signature popup, so let these lines run past 120. This is the only sanctioned overrun of the cap.
- **Do not hard-wrap Markdown prose.** Write one line per paragraph (and one per list item / table row) and let the viewer wrap it. Documentation is read as rendered output — on GitHub and often side-by-side at a varying width — where every hard linefeed prints, so manual wrapping produces ragged output. The exception is fenced code blocks inside Markdown, which follow the 120-column source rule.

### Commit messages

The repo uses [Conventional Commits](https://www.conventionalcommits.org/) with a scope. Canonical spec in [docs/developing.md → Commit messages](docs/developing.md#commit-messages); summary here so an agent doesn't have to follow the link before drafting:

```
<type>(<scope>): <imperative subject>
```

**Types:** `feat`, `fix`, `docs`, `refactor`, `test`, `chore`, `perf`, `style`, `build`, `ci`, `revert`.

**Scopes:** `activity`, `annotations`, `compute`, `epicurrents`, `federation`, `library`, `notifications`, `recordings`, `user`, `frontend`, `viewer`, `docs`, `infra`, `deps`, `tests` — plus the project name for project-plugin work (e.g. `feat(example): ...`).

Rules:

- Imperative subject, lower-case after the colon, no trailing period, wrap at ~72 chars including the prefix.
- **Multi-scope commits drop the scope** rather than picking one (`chore: bump dependencies and rebuild lockfiles`). If a change genuinely spans many scopes, that's usually a signal to split it.
- Body optional; when present, explains *why* not what, wraps at 72 chars, references the related ROADMAP / README / issue.
- Enforcement is **at the PR-title level**, not per-commit — default merge is squash, so the PR title becomes `main`'s history.

When the user asks for a commit-message suggestion, invoke the `/commit-message` skill — it inspects the staged diff and generates conforming suggestions.

### Python docstring conventions

- **Every Python file gets a module docstring**, including `__init__.py`. One sentence is enough when the file is trivial; longer prose for files with real behaviour. The IDE surfaces this on hover-over imports, which makes even one-line docstrings worth writing.
- **Public functions, methods, and classes** (anything not `_`-prefixed) get a docstring.
- **Private helpers** get a docstring when the name doesn't fully convey intent. Skip it when the name does.
- **Test methods** don't need docstrings — make the test name descriptive enough to stand alone.
- **Dunder methods** (`__str__`, `__repr__`, trivial `__init__`) don't need docstrings.
- **Parameter semantics** go in the prose docstring, mentioning the parameter by name where its behaviour isn't obvious from name + type hint. Do not add `:param x:` lines for every parameter — Python's type hints already convey shape, and ritual per-parameter lines are noise.

The module-docstring rule is enforced via `ruff` (D100 + D104) in `pyproject.toml`. The method-level rules are convention; reviewers and follow-up audits enforce them.

### TypeScript type imports

When accessing viewer objects (resources, annotations, montages, events, labels) from platform code, always import and use the appropriate type from the viewer packages rather than writing inline structural types or `as unknown as { ... }` casts.

Available entry points:
- `#epicurrents/core/dist/types` — `DataResource`, `BiosignalResource`, `BiosignalMontage`, `BiosignalAnnotationEvent`, `AnnotationLabel`, `StudyContext`, `StateManager`, …
- `scoped-event-bus/dist/types` — `ScopedEventBus`
- `#epicurrents/eeg-module/dist/` — `EegEvent`, `EegRecording`, … (prefer the core biosignal abstractions unless you genuinely need EEG-specific API)

The only acceptable `as unknown as` or `as any` casts are for accessing private backing fields (prefixed `_`) that have no public API equivalent — document each one with a comment and file a ROADMAP item for the missing API.

### Vue template style

- Place each tag on its own line; do not place nested tags on the same line.
- Indent nested tags by one level (4 spaces).
- Opening and closing tags with short text content may be written on one line, but the line must stay within 120 characters.
- At most 3 attributes may appear on one line with the opening/closing tags when the line stays short; otherwise, put attributes on separate lines.
- If attributes are on separate lines, place the opening tag closing `>` on its own line at the same indent level as the opening `<tag` line.
- Exception: empty tags (no content) may use `></tag>` on the same line.
- Keep `v-if`, `v-else-if`, `v-else`, `v-for`, and `:key` on the same line as the opening tag.
- Sort attributes alphabetically; treat `v-` attributes (except the directive exceptions above) after regular attributes, and place `@` event attributes last.
- Avoid inline styles; use dedicated CSS classes.
- Do not place inline JavaScript expressions in templates for behaviour. Template event handlers should call named methods/functions.

### JavaScript conventions

- Never use `var`. Use `const` by default and use `let` only when reassignment is required.
- Do not use single-line control flow blocks. Always write `if`/`else`, `for`, `while`, `try`/`catch`/`finally`, etc. with braces and place the body on separate lines.
- Keep short ternary assignments on one line. For longer ternary assignments, use multiline formatting with `?` and `:` on their own indented lines.
- Do not add semicolons unless they are required for correctness (for example, when a line starts with `[` and could otherwise be parsed as part of the previous statement).
- Omit explicit return types on function declarations when the return type is obvious from the implementation (for example: `void`, `string`, `boolean`, and straightforward async `Promise<void>` handlers).

### TypeScript class member ordering

Arrange class members in this order, and sort alphabetically by name within each group:

1. Protected properties
2. Constructor
3. Getters & setters (paired together by property name; getter before setter within each pair)
4. Protected methods
5. Public methods

Type-only files (files that export only `type` / `interface` declarations) sort all top-level declarations alphabetically by name — no grouping by `type` vs `interface`.

### TypeScript docstring conventions

The Python docstring rules above apply by analogy. The differences:

- **Every TypeScript file gets a module docstring.** One sentence is enough when the file is trivial; longer prose for files with real behaviour. The IDE surfaces this on hover-over imports, which makes even one-line docstrings worth writing. The `@package` / `@copyright` / `@license` tags belong here too.
- **Public API docstrings belong on the interface, not the class.** When an interface or `type` alias declares the public surface (`BiosignalResource`, `SignalDataReader`, etc.), the class implementation does not redocument those members. Reading the type is the authoritative description; redundant class-side docstrings drift from the interface and rot.
- **Class-side docstrings document protected and private members.** Internal mechanism, override notes, race-condition rationale, and any "why" that wouldn't survive a refactor go on the class.
- **Constructor docstrings are encouraged whenever they serve a purpose.** Substantive setup beyond what the signature conveys, a non-obvious initialisation order, or a reference to the activation lifecycle each earns a docstring. Thin wrappers that just forward to the super-class constructor let the super-class docstring pass through; no re-document is needed.
- **Parameter docs (`@param`) are encouraged.** VSCode and other TS-aware editors surface `@param` lines on per-parameter hover and in completion popups, so they earn their keep at every call site even when the prose already covers the same ground. Use `@param name - description` on interface declarations and on class methods whose parameters carry information the type signature does not: purpose, units, sentinel values, optional-but-required-together conditions, etc.
- **Module-level functions** follow the Python rule: public exports get a docstring; private helpers get one when the name doesn't fully convey intent.
- **Static members** come first in the class body, above `protected` properties. The class member ordering rule above starts at protected properties; statics are conventionally a separate block sorted alphabetically within their kind (properties first, then methods).

### Vue i18n

Always wrap user-facing text in `t(key, SCOPE, params?)`. The key string doubles as the English fallback — no translation file entry is required for plain strings.

- **Plain variables**: pass as named params — `t('{count} selected', SCOPE, { count: n })`. The fallback key is the key string itself, interpolated with the same params.
- **Styled text without variables**: strip the HTML element and use plain `t()` — translators cannot reliably preserve inline HTML, so `<em>all</em>` becomes just "all" in the key string.
- **Styled text with variables** (e.g. `<strong>{{ name }}</strong>` inside a sentence): use the `<i18n-t>` component with an explicit `keypath="SCOPE.key_name"` and a named `<template #slot>` for the styled fragment. A translation entry **is required** even for English because the fallback mechanism of the custom `t` wrapper does not apply to `<i18n-t>`. Add the entry under the component scope key in `frontend/src/i18n/index.ts`.

### Colour conventions

The frontend supports light, dark, and system-auto colour modes (see `frontend/src/stores/theme.ts`). The active mode is applied by toggling `wa-dark` / `wa-light` on `document.documentElement`; WebAwesome's CSS custom properties adapt automatically.

**Always use WA semantic colour tokens** — never hardcode hex values, `rgb()`, or `rgba()` in component styles or inline bindings. Hardcoded colours do not adapt to mode switches and will look broken in dark mode.

| Context | Use |
|---|---|
| Backgrounds / surfaces | `--wa-color-surface-default`, `--wa-color-surface-raised`, `--wa-color-surface-lowered` |
| Borders | `--wa-color-surface-border` |
| Text | `--wa-color-text-normal`, `--wa-color-text-quiet` |
| Semantic fills (SVG, bars) | `--wa-color-success-fill-loud`, `--wa-color-brand-fill-loud`, `--wa-color-warning-fill-loud`, `--wa-color-danger-fill-loud` |
| Neutral fills | `--wa-color-neutral-fill-quiet`, `--wa-color-neutral-fill-subtle` |

The only exceptions are colours that are intentionally invariant regardless of mode — for example, white text on a strongly-coloured phase-band background in an SVG.

CSS custom properties work in **CSS rules and `style=""` attribute values** but **not** in SVG presentation attributes (`fill="…"`, `stroke="…"`). When colouring SVG elements dynamically, use Vue's `:style="{ fill: colorVar }"` binding rather than `:fill="colorVar"`.

### Button design conventions

WebAwesome (`wa-button`) buttons follow a three-tier hierarchy based on where they appear and how important their action is:

| Tier | Location | `appearance` | `variant` | Example |
|---|---|---|---|---|
| **Page primary** | Page-header row, rightmost | `filled-outlined` | `brand` | "New session", "Start upload" |
| **Section primary** | Section-header row, primary action for that section | `plain` | `brand` | "Add item", "Run computation" |
| **Section secondary / generic** | Section-header row (secondary), back buttons, ellipsis menus | `plain` | (omitted / neutral) | "Back", "⋯" menu trigger |

Rules of thumb:
- A page has at most one primary action button; all others are secondary or generic.
- "Generic" actions (navigation, menus, close) are always `appearance="plain"` and carry no `variant`.
- Never combine `appearance="filled"` with `variant="neutral"` — that looks identical to the default and signals nothing.

### Toast variant usage

The `showToast(message, variant)` helper from [`#lib/toast`](frontend/src/lib/toast.ts) accepts five variants. Pick by the *semantic intent* of the message, not just by tone or emphasis:

| Variant | When to use | Example |
|---|---|---|
| `success` | An *awaited* completion landed positively — the user kicked off a longer task and was waiting for the result. Think of `success` as `brand` specialised for an awaited, positive outcome. | "EOG reprocessing complete." |
| `neutral` | Quick reactive confirmation of a routine state change the user just made — no waiting involved, just acknowledging the action took effect. | "Access revoked.", "Recording moved to trash." |
| `warning` | Recoverable issues or cautionary state changes. Also wired to the global `Log.WARN` stream (see `App.vue`). | "Auto-save failed; retrying." |
| `danger` | Operation failures and error conditions. Also wired to the global `Log.ERROR` stream. | "Failed to load library.", "Upload failed. Please try again." |
| `brand` | **Unprompted** notifications (something the user didn't directly cause), or notifications that **prepare the user to change what they're doing next**. Reserved — most messages do not fit this. | "All files uploaded — processing in the background." (prepares the user to view the processed files soon) |

The three informational variants (`brand`, `success`, `neutral`) all carry positive-or-neutral messages; they differ by the user's state when the toast arrives:

- `neutral` — they just did something instantaneous; here's the confirmation.
- `success` — they kicked off a longer task and were waiting for it; the result is positive.
- `brand` — the toast isn't a direct response to a recent action, or it signals a phase change affecting what the user can do next.

`brand` is not a "more emphatic neutral". For confirmations of an action the user just took, reach for `success` if there was waiting involved, `neutral` if it was instant.

### Icon registration

When adding icons for project-specific frontend features, register them in `frontend/src/projects/<project>/icons.ts` and expose them through that project's plugin (`icons` field in `frontend/src/projects/types.ts`), not in the global `frontend/src/icons.ts` unless the icon is shared by base UI. Base and project icon maps are merged in `frontend/src/main.ts`; if a project icon name already exists in base icons, the base icon wins and the project entry is ignored.

If multiple variants of the same icon name are needed (e.g. regular and solid `users`), use the project's `iconLibraries` plugin field to register named icon libraries and select them with `<wa-icon library="<library-name>" name="...">`. Keep one variant in the default library for backwards compatibility; existing `<wa-icon name="...">` calls (without a `library` attribute) continue to resolve from default.

### Frontend access control

`UserOut` (backend, `user/api/v1/ninja.py`) and `AuthUser` (frontend, `frontend/src/api/user.ts`) both expose `is_staff: bool` and `is_superuser: bool`. The auth store (`frontend/src/stores/auth.ts`) provides two computed properties derived from these:

- `isStaff` — `true` when `is_staff` **or** `is_superuser` (use for read-only admin views).
- `isSuperuser` — `true` only when `is_superuser` (use for write / destructive operations).

Always use these computeds in Vue components rather than reading `authStore.user?.is_staff` directly.

**Nav links.** Set `requiresStaff: true` or `requiresSuperuser: true` on any `ProjectNavLink` entry whose target view requires elevated access. `App.vue` filters the merged nav link list using `authStore.isStaff` / `authStore.isSuperuser` before rendering, so the link is invisible to users who lack the required role. The route itself should additionally guard the page (via the view's own check or a router guard) so that direct URL access is also blocked.

**View gating.** Inside a view component, gate write actions with `canSubmit = computed(() => authStore.isSuperuser)` (or `isStaff` for read-only admin sections) and wrap the relevant template block in `v-if="canSubmit"`. Staff users who land on the page without superuser rights should see progress / status information but no submission controls.

### WebAwesome shadow-DOM layout gotchas

Several WA components ship default styles where the **host element** has a definite size but the **inner ::part wrapper** is content-driven (`display: block`, no `height: 100%`). When you put a `flex: 1` child inside such a component expecting it to claim the remaining space, the flex child has no bounded parent to grow into and instead inflates to fit its own content — silently overflowing the host's `overflow: hidden` clip on browsers that let absolute-positioned scrollers escape.

Canonical example: `wa-tab-panel`. The host gets `display: block` (or `display: none` when inactive); the inner `.tab-panel` is just `display: block; padding: var(--padding)`. If you add a `flex: 1; min-height: 0` child for a scroller, you also need:

```css
wa-tab-panel::part(base) {
    display: flex;
    flex-direction: column;
    height: 100%;
    overflow: hidden;
}
```

The `height: 100%` is the load-bearing line — without it, the flex child can't size correctly even with `min-height: 0`. The same trap shows up on any WA component that wraps its content in an inner `::part(base)` without forwarding a definite height.

The same trap in a second costume, worth knowing because the symptom does not look like a layout bug. `wa-qr-code`'s host is `display: inline-flex; aspect-ratio: 1` with no intrinsic width, its canvas is `width: 100%`, and the component sets only `max-width` — never a width. The percentage resolves against a content-sized parent and collapses. Because the canvas is rendered at twice the `size` attribute and then downscaled into whatever the layout gave it, the result is not merely small: downsampling a 1-bit pattern greys it out, worst on the largest solid blocks, and a QR code stops scanning. Give the host an explicit `width` and `height` rather than trusting `size`.

Colour on that component has its own trap. `fill` and `background` are deprecated attributes, and the fallback is the host's computed `color` with a transparent background — so in dark mode a code with no explicit colour is drawn light-on-transparent and no camera reads it. A canvas cannot be recoloured from outside (CSS `fill` does not touch raster pixels, and neither does styling its `::part`), so the only levers are those attributes and the host's `color`. The durable fix is a `wa-light` wrapper: every token inside resolves to the light palette, so the fallback lands on a dark colour by construction. See [ProfileView.vue](frontend/src/views/ProfileView.vue).

Related: `wa-scroller` with `orientation="vertical"` has internal `height: 100%`, so it must be wrapped in a flex-1 div with `min-height: 0; overflow: hidden` when a sibling shares the same flex parent — otherwise it tries to be 100% of the *whole* parent instead of the leftover space.

### Scroller nesting in a centred content column

When a view scrolls internally *and* holds its content to a centred `max-width` column, the scroller must be the full-width element and the centring must happen **inside** it:

```
<main class="x-view">                    <- flex column, min-height: 0, overflow: hidden, full width
    <wa-scroller orientation="vertical"> <- full width, so its scrollbar rides the viewport edge
        <div class="x-view__scroll-wrap"> <- margin: 0 auto, max-width, width: 100%
```

Nesting them the other way — `max-width` on the host with the scroller inside — puts the scrollbar hard against the content column, where it overlaps the content and steals width from it. The canonical shape is [AnnotationExportView.vue](frontend/src/views/AnnotationExportView.vue).

The consequence to plan for is that the host stops centring **all** of its children, not just the scroller. Any non-scrolling sibling — page header, error callout, sticky action bar — needs the same `margin: 0 auto; max-width; width: 100%` treatment, or it stretches to the full viewport. Where several rules have to agree on the column width, name it once as a custom property on the host (`--content-width`) rather than repeating the literal; [UploadView.vue](frontend/src/views/UploadView.vue) does this across three rules, and wraps its non-scrolling chrome in a band element that carries the centring. A sibling that fills leftover height with `flex: 1` needs that band to grow too, in the stage where no scroller is present.

## Cross-cutting rules

### Staff vs superuser tier

Django's built-in `is_staff` flag is the access tier for admin-level features — dashboards, batch operations, anything that requires visibility across all users' data. `is_superuser` is reserved for destructive or irreversible actions (epoch generation with `--clear`, future data-deletion flows, etc.). Treat superuser as a strict subset of staff: anything a superuser can do, a staff user should also be able to do or see in read-only form.

Use `_require_staff` / `_require_superuser` guards at the top of project API endpoints (see [`projects/example/urls.py`](projects/example/urls.py) for the canonical implementation, [user/README.md](user/README.md) for the principle). Do **not** invent project-specific role models for access tiers that map cleanly onto staff/superuser — use the Django flags directly. Project-specific roles are only appropriate when the distinction cannot be expressed as staff vs. superuser (e.g. a per-session student identity).

### Session-authenticated write CSRF

The Ninja API mounts run `csrf_exempt` (the codebase uses `auth=None` plus manual `request.user` reads, so Django's `CsrfViewMiddleware` never enforces a token on them). The only CSRF protection on the session-authenticated write surface is the explicit `enforce_session_csrf(request)` call in [epicurrents/auth.py](epicurrents/auth.py), invoked from each app's `_require_auth` helper and the session branch of `_require_auth_or_federated`. It fires only for unsafe methods (POST/PUT/PATCH/DELETE) and only for session-cookie callers; FederatedBearer JWT and `?share_token=` callers authenticate outside the chokepoint and are never a CSRF vector.

When adding a new unsafe-method endpoint, obtain the caller through the app's `_require_auth*` helper rather than reading `request.user` directly — that is what routes the request through the chokepoint. A write endpoint that resolves the user some other way must call `enforce_session_csrf(request)` itself. The `csrf-coverage` review agent flags any session write that bypasses the chokepoint. Enforcement is gated by `SESSION_CSRF_ENFORCED` (on in production, off in development so the cross-origin Vite SPA and host tooling work without a token); never disable it in a production deployment.

### Multi-step write atomicity

Any endpoint that performs two or more database write operations must wrap them all in `transaction.atomic()`. This prevents partial writes — if any step raises an exception (including `HttpError`), Django rolls back the entire block and no half-created state is left in the database. Use `transaction.on_commit()` inside the block for side effects that must only run after a successful commit (e.g. dispatching Celery tasks). Apply whenever creating an object and its associated `AccessRight` together, or whenever adding child rows to a newly created parent. Canonical example: the upload endpoint in `recordings/api/v1/ninja.py`.

### Audited scope for non-request callers

The audit signals in [activity/signals.py](activity/signals.py) gate on `is_audited_context()` — they fire only inside a scope that an entry-point layer has opened. `ApiActivityLoggingMiddleware` opens the scope for every HTTP request; `activity.system_activity.with_system_activity` is the explicit opt-in for Celery tasks and management commands. Pass `interface=Activity.Interface.CELERY` or `Activity.Interface.COMMAND` to tag the row so audit views can filter by caller-type:

```python
from activity.models import Activity
from activity.system_activity import with_system_activity

with with_system_activity(
    "recordings.process",
    interface=Activity.Interface.CELERY,
    target=recording,
    metadata={"recording_id": recording_id},
):
    # ORM writes inside this block auto-attribute via signals.
    ...
```

Verbs follow the same `<app>.<resource>.<action>` taxonomy as HTTP endpoints. Add a verb for any task / command that mutates user-owned data; skip the scope only when the operation makes no model writes (pure file-system housekeeping, log shipping, etc.). Apply to all new Celery tasks and management commands; retrofit existing ones when you touch them.

### Activity metadata carries identifiers, never names

`metadata=` on `log_activity` / `with_system_activity` takes primary keys, hashes, counts, enums and booleans. It must not take a username, an email address, a person's name, or a filesystem path supplied by a user or operator. Use `"owner_id": user.pk`, never `"owner_username": user.get_username()`.

**Why it is stricter than it looks.** `erase_subject` reaches `Activity` rows only where `target_content_type` is the user model, and it scrubs only the keys in `ACTIVITY_METADATA_PII_KEYS`. So personal data in the metadata of a row targeting anything else — a job, a recording, a dataset — is reachable by no erasure path the platform has: not scrubbed, not tombstoned, not removed with the account. A primary key is safe precisely because it stops resolving to a person once the row it points at is deleted; a name never stops being one.

A path is the same hazard wearing different clothes, because directories holding clinical exports are routinely named after what was exported. Where the operator needs the path, keep it on the live row and let the `target` FK carry the reference — a live row can be deleted, an audit row cannot. That is the split `recordings.import` uses: `ImportJob.source_path` on the row, `target=job` in the audit, nothing personal in the metadata.

This was implicit until the 2026-08-26 GDPR audit found the one call site that broke it, so it is now written down. Regression coverage in [recordings/tests/test_import_audit_hygiene.py](recordings/tests/test_import_audit_hygiene.py). The related-but-separate rule for the security log stream (hash usernames and emails rather than omitting them) is under "Log security-related activity" below; the two streams differ because a hash is useful to a SIEM correlating attempts and useless in an audit row that already has a `target`.

### Bulk ORM operations bypass the audit signal

`QuerySet.update()`, `bulk_create()`, and `QuerySet.delete()` (in particular `Model.objects.filter(...).delete()` inside non-API contexts) do **not** fire the `post_save` / `pre_delete` signals the audit trail listens to. A `with_system_activity` scope alone is not enough — the explicit recorders in [activity/audit.py](activity/audit.py) (`record_create_change`, `record_modify_change`, `record_delete_change`) must additionally be called for every affected row, inside the scope:

```python
with with_system_activity("recordings.purge", interface=Activity.Interface.CELERY):
    with transaction.atomic():
        targets = list(Recording.objects.filter(...))
        for r in targets:
            record_delete_change(actor=None, obj=r, before_state=serialize_instance(r))
        Recording.objects.filter(pk__in=[r.pk for r in targets]).delete()
```

Canonical pattern: [activity/erasure.py](activity/erasure.py); a project's `--clear`-style regeneration command follows the same shape. The chain (`activity.audit.create_chained_change_log`) advances atomically per row.

### Models linking to a user must be classified for subject export

A model with a foreign key to the user model has two obligations, not one. It must be registered for [erasure](#personal-data-in-audited-models-must-be-registered-for-erasure), and it must be classified for the Art. 15 subject access export — `user.export.register_export_relation(model_label, field_name, fields=(...))` from the owning `AppConfig.ready()`, or the same call with `omit_reason=...` when the rows are not the subject's personal data.

The two are mirrors: erasure answers "remove what you hold about me", export answers "show me", and a model covered by one but not the other makes the pair inconsistent. Core relations are declared in [user/export.py](user/export.py); projects and plugins register their own, because the set of things pointing at a user depends on what is installed.

**Why it needs a rule rather than review.** An export fails silently — it returns a plausible document missing something, and the only person positioned to notice is the subject, who cannot see what was left out. [user/checks.py](user/checks.py) turns that into a `manage.py check` error: an unclassified relation, a field name the model does not have, and a registration for a model that is not installed are each reported at deploy rather than while a legal deadline runs.

Credentials need no separate exclusion — the export reads `activity.audit`'s masked-field registry, so `register_masked_fields` covers both surfaces.

### Personal data in audited models must be registered for erasure

The audit trail is permanent, so any model whose serialized state carries a user's personal data (names, emails, external identifiers, device endpoints) must be registered with the GDPR Art. 17 erasure engine, or "delete my account" silently becomes unfulfillable for that data. Two registries, both called from the owning app's `AppConfig.ready()`:

- `activity.erasure.register_subject_pii(model_label, owner_field=..., pii_fields=...)` — which audited fields are scrubbed when the linked user is erased. `owner_field` is the serialized FK attname linking a row to the subject (`"user_id"`), or `None` for the user model itself. Note *attname*: a foreign key is named with its `_id` suffix, because that is the key `serialize_instance` writes into the payload. A Django system check ([activity/checks.py](activity/checks.py)) validates every registration against the real model at `manage.py check`, so a wrong label or field name stops a deployment instead of silently scrubbing nothing. When a registered model is later dropped from the schema, keep the registration and add `historical=True` — the audit rows outlive the model and still need scrubbing (the scrub needs only the surviving ContentType row), and the flag flips the check to expect the model's absence.
- `activity.audit.register_masked_fields(model_label, fields)` — credential fields (password hashes, encryption keys, tokens) that must never reach the audit trail in the clear; they are masked at write time.

Core registrations: [user/apps.py](user/apps.py), [notifications/apps.py](notifications/apps.py). Apply when adding any model with user-linked personal data, in core or in a project plugin. Never mutate `ObjectChangeLog` rows directly to remove data — `erase_subject` is the only sanctioned mutation (it tombstones and re-seals so chain verification survives; see [activity/README.md → Subject erasure](activity/README.md#subject-erasure-gdpr-art-17)). The operator flow is the `erase_user` management command ([user/README.md](user/README.md#account-erasure-gdpr-art-17)).

**High-fanout derived rows**: when a single operation bulk-creates many derived rows (e.g. `SignalInfo` for a Recording, computed features for a parent object), per-row audit inflates `ObjectChangeLog` for no analytical benefit — the derived state is fully reconstructable from the parent. The closure strategy is to audit only the parent transition and embed a digest of the derived rows in the parent's audit entry, keeping derived-row tampering detectable without per-row chain entries. The supporting helper extension to `create_chained_change_log` lands alongside the first call site that needs it; document each digest-only exception inline on the writing site so the next reader sees the choice is deliberate.

### De-identification

- Prefer opaque hashes over integer PKs in URLs and public-facing identifiers — sequential integer PKs leak information about when and how many objects have been created.
- Recording responses omit `id` and `author_id`; the URL `hash` parameter is the 32-hex-char `stored_name` prefix (`content_hash` is a content fingerprint, not the identifier).
- Annotation-type responses omit `id`, `created_at`, `modified_at`; `author_id` is kept; CRUD endpoints use `/{object_hash}`.
- File timestamps are normalised to UNIX epoch 0 by `os.utime` in `process_recording` after the file is moved to permanent storage.
- **`Recording.original_name` and `Recording.processing_error` are author-private** — both fields can carry PHI or operator-only context (filename subject identifiers, filesystem paths in stack traces). Grantees, share-token holders, and federated peers see `null` for both. The `_can_see_original_name` helper in [recordings/api/v1/ninja.py](recordings/api/v1/ninja.py) is the single check. Any new author-private field on `Recording` should be gated by the same helper rather than introducing a parallel one. The `SignalInfo.source_*` fields (pre-de-identification channel labels, transducer strings, prefiltering strings, original channel positions) are author-private under the same gate.
- **Channel-block de-identification at ingest** — `deidentify_signal_infos` in [recordings/processors/edf.py](recordings/processors/edf.py) rewrites channel labels (canonical or `MISC_<n>`, fail-closed), blanks transducer strings, and reconstructs prefiltering in the stored file, and `reorder_edf_channels` then permutes the channels into the canonical homologous-pair order (`CANONICAL_EEG_ORDER`, versioned via `RecordingMeta.channel_order_version`), so no serving path — raw-file grants, proxy offload, federation — carries acquisition-site naming conventions or the acquisition-template channel order. Threat model and phase plan in [docs/engineering-notes/channel-deidentification-plan.md](docs/engineering-notes/channel-deidentification-plan.md); mechanics in [recordings/README.md → RecordingMeta and SignalInfo](recordings/README.md#recordingmeta-and-signalinfo).
- **`Recording.display_name` is the grantee-visible label**, with a `stored_name` hash-prefix fallback. The `Content-Disposition` filename on every download is built from `display_name + file_extension`, never from `original_name`. See [recordings/README.md → Display name vs. original filename](recordings/README.md#gotchas).
- Datasets are hash-addressed like everything else: `Dataset.object_hash` (32-hex, random) is the identifier in dataset routes and viewer `?dataset=` links. The integer PK stays accepted on the same routes for internal callers and old links — the dual resolution `_get_active_dataset` in [library/api/v1/ninja.py](library/api/v1/ninja.py) mirrors the recording resolver.

### FAILED recording hiding

Recordings with `status=FAILED` are visible only to the author and to superusers. Every grantee-facing surface — recording listings, per-recording endpoints, federation `inbound_check_object`, library collection / dataset / tag item listings — filters them out and returns 404 on direct hash lookups. A FAILED-recording response that distinguishes itself from "no such object" would leak the existence of a failed upload along with its PHI-bearing original filename. Enforcement is two-layer. The read-visibility gate in [recordings/permissions.py](recordings/permissions.py), registered with `register_read_visibility_gate` in [epicurrents/permissions.py](epicurrents/permissions.py), denies read resolution for FAILED (non-author) and trashed recordings before any `AccessRight` row or extension is consulted — so a surface that resolves recordings through `can_read_object` is safe without knowing the rule. `_failed_hidden_for_caller` in [recordings/api/v1/ninja.py](recordings/api/v1/ninja.py) is the endpoint-side layer: checked before the permission check on every recording surface, it produces the 404 response shape (the resolver's denial reads as 403) and stands as defence-in-depth. Apply the endpoint-side check to any new endpoint that surfaces a Recording. See [recordings/README.md → FAILED-hidden rule](recordings/README.md#failed-hidden-rule).

### Originals preservation volume is strictly write-only

The host-controlled originals volume (`RECORDINGS_ORIGINALS_PATH`) is the operator's regulatory backstop, not platform-managed storage. The platform writes to it through `recordings.preservation.write_original` and **never reads back** — no download endpoint, no recovery management command, no programmatic content read. The `validate_originals` command reads filesystem metadata (`stat()`, directory listings, `manifest.json` parsing) only. Recovery of original bytes is out-of-band: the operator mounts the volume directly. Do not add a read path here even when convenience tempts you to — every read API on this volume re-introduces the PHI-leak risk the preservation tier exists to bound. See [recordings/README.md → Preservation tiers](recordings/README.md#preservation-tiers).

### PHI no-store caching

`SecurityHeadersMiddleware` sets `Cache-Control: no-store` on every response via `setdefault` — on unless `DISABLE_NO_STORE_HEADERS=True` (the flag defaults off, so no-store is the standing rule and disabling it is a deliberate act). This keeps PHI-bearing responses — API JSON, recording / media byte serving — out of every browser and intermediary-proxy cache without a per-endpoint opt-in, so a new data or byte-serving endpoint inherits it automatically and needs no change. The opt-out is the other direction: views serving **non-PHI** static assets set their own `Cache-Control` (the content-hashed SPA bundles → `immutable`; the fixed-name viewer lib → `no-cache`), which the `setdefault` default preserves. **Never set a cacheable `Cache-Control` on a response that can carry PHI** — that exempts it from the no-store default. Contract test: [epicurrents/tests/test_no_store_headers.py](epicurrents/tests/test_no_store_headers.py).

### Raw byte serving may be offloaded to the proxy — never for a middleware grant

When the proxy overlay is deployed, `epicurrents/offload.py` lets a byte-serving endpoint answer with an empty 200 plus `X-Serve-Path` and have Caddy read the file off a read-only mount, instead of holding a gunicorn thread for the whole transfer. Django still handles, authorises and audits every request — including every Range request — so the `log_activity` contract is unaffected; only the bytes move.

**`offload_file_response` must never be reached with `apply_middleware=True`.** Those bytes are computed per request — an anonymised header, and under a signal pipeline every data record transformed individually — so no file on disk holds them. Handing the proxy a path for such a caller serves the original recording, patient identification and clinical annotation text included, to exactly the caller the flag exists to protect against, with a 200 and no error anywhere. The interlock lives inside the helper and `apply_middleware` is a required keyword-only argument so that a new endpoint cannot acquire the offload by not thinking about it. Contract tests: [epicurrents/tests/test_offload.py](epicurrents/tests/test_offload.py), including a source scan for call sites that omit the argument.

The corollary is a performance one worth knowing before changing a grant: turning `apply_middleware` on for a population also turns their offload off, because the bytes stop existing on disk.

### Asset cache headers are defined in two places

With the TLS proxy overlay enabled, Caddy serves `/static/`, `/assets/`, `/vendor/` and the viewer bundles from disk — those responses never reach Django, so they never pick up the `Cache-Control` and `Cross-Origin-Resource-Policy` headers set in [epicurrents/views.py](epicurrents/views.py). The rules therefore exist twice: once in the views, once in [caddy/Caddyfile](caddy/Caddyfile). **Change one and you must change the other.** Drift is invisible on the server — it surfaces as a bundle that stays stale after a deploy, or as a subresource the public viewer's `COEP: require-corp` blocks, both only reproducible in a browser against a real deployment. Contract test: [epicurrents/tests/test_proxy_asset_headers.py](epicurrents/tests/test_proxy_asset_headers.py), which reads the expected values out of the running views rather than restating them.

Two traps the Caddyfile comments already flag, repeated here because they cost a debugging session each. Caddy's path globs do not cross a path separator, so `/viewer/*epicurrents-lib.*` silently misses the per-project builds under `viewer-dist/<project>/` — the viewer matchers use `path_regexp` for that reason. And `index.html` must never be served from disk: `frontend_view` seeds the `csrftoken` cookie on the document via `get_token`, and `SESSION_CSRF_ENFORCED` then rejects every session write that arrives without it, so a `file_server` reaching index.html breaks writes across the whole SPA while reads keep working.

### Initial migrations

FK fields are inlined into `CreateModel` calls in every app's `0001_initial.py` — no separate `AddField` operations for FKs in the initial migration. Follow this when adding a new app or squashing existing migrations.

**Generation order is what produces that shape**, not hand-editing. `makemigrations` defers an FK into a second `0002_initial` whenever the target app's migration does not yet exist in the same run — which happens with the swappable user model, and with any app generated before something it depends on. Generate `user` first, then apps whose only dependencies are `contenttypes` and the user model, then the ones that depend on those (`epicurrents` needs `federation`; `compute` needs `recordings` and `annotations`). A deferred `AddField` in an initial migration means the order was wrong, not that the graph has a cycle.

**Regenerating beats hand-merging when the deployment has no data.** Deleting an app's migrations and regenerating gives final field shapes and inlined FKs by construction, where a hand-written squash asks a reviewer to pick the last value off an `AlterField` chain. Two things to check afterwards: data migrations are lost (`RunPython` / `RunSQL` — grep for them first and decide each one deliberately), and dependent apps pinning to intermediate migrations of a squashed app need repointing at its `0001_initial`. Verify by dumping the schema from a fresh `migrate` before and after and comparing definition parts as sets — column *order* legitimately differs, since the old schema appends columns in migration order and the new one uses declaration order.

### GenericFK target cascade pattern

When you write a model that can be the **target** of a `GenericForeignKey` (e.g. annotations attach to it, AccessRight grants on it, library items contain it, tags decorate it), declare matching reverse `GenericRelation` fields on the target model. Without them, Django's ORM cannot enforce referential integrity across generic FKs, and hard-deleting the target leaves orphan rows pointing at a stale `object_id`.

The reference-row types in core that need a reverse relation on every target are:

| Reference row | Field on target | Defined in |
|---|---|---|
| `epicurrents.AccessRight` | `access_rights = GenericRelation("epicurrents.AccessRight")` | [epicurrents/models.py](epicurrents/models.py) |
| `library.CollectionItem` | `collection_memberships = GenericRelation("library.CollectionItem")` | [library/models.py](library/models.py) |
| `library.DatasetItem` | `dataset_memberships = GenericRelation("library.DatasetItem")` | [library/models.py](library/models.py) |
| `library.TaggedItem` | `tagged_items = GenericRelation("library.TaggedItem")` | [library/models.py](library/models.py) |
| `annotations.Annotation` / `Event` / `Interruption` / `Label` | `annotations` / `events` / `interruptions` / `labels = GenericRelation(..., object_id_field="target_object_id", content_type_field="target_content_type")` | [annotations/models.py](annotations/models.py) |

Core models that already declare the full set: `Recording` ([recordings/models.py](recordings/models.py)) and `Dataset` ([library/models.py](library/models.py)). `Collection` declares everything except the `AccessRight` relation — deliberately, since collections are author-private and no row may target them.

Audit-trail rows (`activity.ObjectChangeLog`, `activity.Activity` target, `federation.FederationAuditLog` target) intentionally have **no** reverse `GenericRelation` on their targets — they must outlive the rows they reference.

**Exception — derived models with user-content descendants.** A model whose rows are *deterministically regeneratable* from settings (slice geometry, computed features, etc.) and which carries user-generated content as a GenericFK descendant should intentionally **not** declare the reverse `GenericRelation` for that user content. The asymmetry matters: the derived rows are cheap to recreate, the user content is not, and cascading from "clear the derived rows" to "destroy the user content" is the precise harm the user-protection design avoids. The canonical shape is a project's epoch model: epochs are regeneratable from slice settings, but `annotations.Label` rows on them are irreplaceable rater output, so the epoch model deliberately omits a `labels = GenericRelation(...)` and `--clear` on the epoch set preserves the labels. The trade-off is that orphan labels (whose `target_object_id` points at a deleted epoch PK) accumulate; the project's workflow accepts this and offers an explicit `--clear-labels` switch for the case where the operator does want them gone with an audit trail.

When applying this exception, document the reasoning inline on the target model and in the project's README, so the absence of the reverse relation is visibly deliberate rather than looking like an oversight.

**When this rule applies:**

- Adding a new model in core that should be accessible / annotatable / collectable / taggable.
- Adding a new model in a project plugin (`projects/<name>/`) that should participate in any of the above.
- **Skip** the rule when the new model is a derived / regeneratable artifact and the descendant is user-generated content the operator should not lose on cascade.

**Why:** Django's `Collector` walks `GenericRelation` fields during cascade, so hard-delete fires per-row `pre_delete` signals in the same transaction as the target delete. This (a) cleans up reference rows atomically without a race window, (b) lets the activity audit trail capture each cascaded row as `ACTION_DELETE` since the signal handlers fire individually, and (c) keeps the design uniform with how regular FK cascade works.

**Soft-delete is unaffected.** The cascade only runs when Django actually removes the target row. Setting `deleted_at` on a soft-deleted target leaves the row in place, so reference rows are also untouched.

Cascade tests live next to the affected app's other tests: see [recordings/tests/test_cascade.py](recordings/tests/test_cascade.py) and [library/tests/test_cascade.py](library/tests/test_cascade.py) for the canonical patterns. Add one per (target, reference-row-type) pair when introducing a new target model.

### Log security-related activity

Authentication failures, permission denials, rate-limit hits, and federation auth failures must be logged through `epicurrents.security_log.log_security_event` — not via ad-hoc `logger.warning(...)`. The helper emits WARNING entries to the `epicurrents.security` logger with a stable `event_type` token plus structured fields, so an external SIEM / log shipper can build rules against a predictable format.

Apply whenever a request is rejected for a security reason: 401, 403, 429, signature/replay failures, or any centralised `ensure_*` permission helper. The existing event-type taxonomy is documented in `epicurrents/security_log.py`; extend that docstring when adding new event types so the rule set stays maintainable. Never write raw usernames or email addresses to the log stream — hash them (the user-login and password-reset endpoints are the canonical pattern). The audit trail in the `activity` app is a complementary mechanism; both should be present, neither replaces the other.

### Project system

One project is active per deployment, controlled by `EPICURRENTS_PROJECT=<name>` in `.env`. The project layer is designed for one active project per deployment; switching projects exists to support development and onboarding new deployments and is not intended as a runtime operation in production. Structure, settings merge rules, lifecycle commands (`activate_project` / `deactivate_project` / `remove_project_data`), and the recommended switch workflow (`scripts/switch_project.sh`) are documented in [epicurrents/README.md](epicurrents/README.md#project-loader); the scaffolded template lives at [projects/example/](projects/example/).

**Two URL slots** are available to the active project:

- `urls.py` — Django Ninja API patterns, mounted at `/project/api/v1/` (existing).
- `public_urls.py` — Plain Django URL patterns, mounted at `/project/<name>/`. Use this when the project needs to serve non-API content — an SPA, a viewer, or responses with custom HTTP headers (e.g. `Cross-Origin-Opener-Policy` / `Cross-Origin-Embedder-Policy` for WASM). Both files are optional.

> **Audit-trail note for `public_urls.py`.** Paths mounted under `/project/<name>/` do **not** match the API path matcher in [epicurrents/middleware.py](epicurrents/middleware.py) — by design, since the slot is intended for static-ish content. Views mounted there receive no `Activity` row and their ORM writes do **not** appear in `ObjectChangeLog`. If a `public_urls.py` view needs to write to the database, it must record changes manually via `record_create_change` / `record_modify_change` / `record_delete_change` from [activity/audit.py](activity/audit.py). The cleaner pattern is usually to keep the write on a Ninja endpoint under `urls.py` and have the public view fetch only.

**Every project and plugin declares `requires_platform` on its `AppConfig`** — a semver range such as `">=0.1,<0.2"` naming the platform versions it supports. A system check verifies it at `manage.py check`, which runs before `runserver` and `migrate`; an unsatisfied pin blocks the boot, an absent one warns. When a change breaks something inside the compatibility surface listed in [epicurrents/README.md → Versioning and the platform pin](epicurrents/README.md#versioning-and-the-platform-pin), bump `__version__` in [epicurrents/version.py](epicurrents/version.py) and add a [CHANGELOG.md](CHANGELOG.md) entry in the same commit. **While the platform is on `0.x`, the breaking bump is the minor** (`0.1 → 0.2`) and additions go in the patch — semver's rule for initial development, and the reason a cap is `<0.2` rather than `<1`. From 1.0 onwards it is the usual major-for-breaking, minor-for-addition. Never hand-compute a cap: `compatible_range()` in [epicurrents/version.py](epicurrents/version.py) encodes which of the two applies, and "next major" looks right while admitting every breaking release of a 0.x platform. Changes outside the surface bump neither. Never resolve a failing pin by editing the project's range to match: the range says what the project was tested against, so widening it without testing is an assertion nobody checked.

**Python dependencies a project needs and the platform does not** go in `projects/<name>/requirements.txt` with a lock generated by `scripts/lock-requirements.sh --project <name>` — never appended to the platform's `requirements.txt`. Never generate that lock with a bare `uv pip compile` or `pip freeze`: the two closures overlap (`numpy` is in both), and pip does not treat an overlap as a conflict, so an independently-resolved project lock installs cleanly over the platform's version and reports nothing. The `--project` mode resolves against the platform lock's exact versions as constraints, which is the only thing making the overlap agree. Regenerating `requirements.lock` invalidates every project lock; re-run `--project` for each. Full contract in [epicurrents/README.md → Project dependencies](epicurrents/README.md#project-dependencies); invariants asserted in [epicurrents/tests/test_project_requirements.py](epicurrents/tests/test_project_requirements.py).

**Owner-specific submodules** (heavy dependencies that only one project or plugin needs — e.g. an embedded viewer source tree) should be registered in `.gitmodules` with `update = none` so the default `git submodule update --init --recursive` skips them. [scripts/bootstrap.sh](scripts/bootstrap.sh) then runs an explicit `git submodule update --init --checkout <path>` when the owner is active. Canonical example: `plugins/dicom/ohif-viewer`, gated on the dicom plugin being enabled (`EPICURRENTS_PLUGINS` contains `dicom`), fetched by [scripts/bootstrap.sh](scripts/bootstrap.sh) and [scripts/enable_plugin.sh](scripts/enable_plugin.sh). This keeps the clean-clone deploy fast for deployments that don't need the dep.

> **Critical — always run lifecycle commands via `docker compose run`, never plain `python manage.py` on the host.** The host uses a local SQLite development database; the Docker stack uses PostgreSQL. Running `activate_project` or `deactivate_project` outside a container applies migrations to the wrong database and leaves PostgreSQL tables broken (archived tables Django thinks are live, or vice-versa), which shows up as `relation "..." does not exist` errors at runtime.

### Plugin system

Plugins are the composable sibling of projects: zero or more may be enabled per deployment via `EPICURRENTS_PLUGINS=<comma,separated>` in `.env` (mirror the same list into `VITE_PLUGINS` in `frontend/.env`). Unlike a project, a plugin never owns the landing page or primary UX — it composes with whatever project is active. A `plugins/<name>/` app subclasses [`epicurrents.plugins.PluginConfig`](epicurrents/plugins.py) — setting `default = True` on the concrete config, without which Django's config auto-detection silently instantiates the bare `AppConfig` and `ready()` never runs — and may provide the same extension points a project can. [`epicurrents.plugin_loader`](epicurrents/plugin_loader.py) merges plugin settings between `common` and the active project (precedence `common < plugins < project < .env`) and validates `requires` dependencies + URL-namespace uniqueness at boot via `EpicurrentsConfig.ready`. URL slots mount at `/plugin/<name>/api/v1/` (Ninja `urls.py`) and `/plugin/<name>/` (`public_urls.py`); the `public_urls.py` audit-trail note above applies identically. Lifecycle: [scripts/enable_plugin.sh](scripts/enable_plugin.sh) / [scripts/disable_plugin.sh](scripts/disable_plugin.sh). The test of "project or plugin?" is whether the same deployment would plausibly want a different one alongside it. Full contract in [plugins/README.md](plugins/README.md) and [docs/plugins.md](docs/plugins.md); `dicom` ([plugins/dicom/README.md](plugins/dicom/README.md)) is the reference plugin. When a plugin migration changes only the app's module path but keeps its Django `label`, no table or migration-history rename is needed — the label is what keys `django_migrations` and every table name (as with the dicom migration, which kept `label = "dicom"`).

## Load-bearing files

A small set of files are *load-bearing* for security-critical platform features. Modifying one of them with anything but a behaviour-preserving refactor risks silently disabling the feature without any visible test failure or runtime error. Every such file carries a `⚠️ LOAD-BEARING` block at the top of its module docstring naming the contract it upholds and pointing at the contract test that backstops it.

### Files currently marked

| File | Feature at stake | Contract test |
|---|---|---|
| [epicurrents/middleware.py](epicurrents/middleware.py) | Audit-trail coverage — `_API_PATH_RE` decides which HTTP requests create `Activity` rows and trigger the `ObjectChangeLog` signals. Mis-classifying a path silently strips the audit trail off every endpoint mounted under it. | [epicurrents/tests/test_middleware_path_recognition.py](epicurrents/tests/test_middleware_path_recognition.py), [epicurrents/tests/test_middleware_audit_trail.py](epicurrents/tests/test_middleware_audit_trail.py) |
| [epicurrents/permissions.py](epicurrents/permissions.py) | Object-level access control — `can_*_object` and `ensure_*_object` are called from every endpoint that touches a user-owned object. Changing the resolution order, return-shape, extension-registry, or visibility-gate-registry semantics changes security behaviour repo-wide; a dropped gate consultation silently re-surfaces FAILED and trashed recordings to grant holders on every generic surface. The federated extension registry is part of the same surface: dropping its consultation, or letting `get_federated_visible_ids` disagree with the per-object check, silently reduces a peer's access to whatever it holds direct rows on — which is how dataset sharing conveyed nothing across federation before the registry existed. | [epicurrents/tests/test_permissions.py](epicurrents/tests/test_permissions.py) |
| [epicurrents/auth.py](epicurrents/auth.py) | Session-CSRF chokepoint — `enforce_session_csrf` is the only CSRF protection on the session-authenticated write surface (the Ninja mounts are `csrf_exempt`). Each app's `_require_auth` helper and the session branch of `_require_auth_or_federated` call it after confirming the session user. Weakening the unsafe-method set, the `SESSION_CSRF_ENFORCED` gate, or the `check_csrf` delegation silently strips CSRF from every session write; the FederatedBearer / share-token exemption (they never reach the call) is part of the contract. | [epicurrents/tests/test_session_csrf.py](epicurrents/tests/test_session_csrf.py) — safe-method no-op, kill-switch no-op, unsafe-method-without-token 403, test-client exemption (the property that keeps the suite green). |
| [activity/signals.py](activity/signals.py) | Audit-trail auto-attribution — the `_track_sender` guard decides which model writes generate `ObjectChangeLog` rows. Same hazard as the middleware: any narrowing here silently strips coverage. | [activity/tests/test_audit.py](activity/tests/test_audit.py) (see also the middleware integration test above for the request-context side) |
| [activity/audit.py](activity/audit.py) | Audit-trail integrity hash — `compute_audit_hash` is the versioned dispatcher. `_compute_audit_hash_v1` is the legacy SHA-256 fingerprint kept for already-written rows; `_compute_audit_hash_v2` is HMAC-SHA256 against a server-side key from `settings.ACTIVITY_HASH_KEYS[hash_key_version]`; `_compute_audit_hash_v3` adds a per-content_type chain pointer (`prev_hash`) into the HMAC payload. `create_chained_change_log` is the single write entry point — every signal handler and explicit recorder calls it, acquiring the per-shard advisory lock and reading the chain tail under `transaction.atomic()`. v3 provides forgery resistance (HMAC) and ordering resistance (link check between adjacent rows). `verify_chain(content_type)` walks the shard and reports content-hash breaks + link breaks + gaps + genesis-sentinel match. `compute_erased_hash` seals subject-erased rows (see the [activity/erasure.py](activity/erasure.py) entry) and `register_masked_fields` masks credential fields out of every payload at write time. Changing any algorithm, the chain link payload shape, the erased-hash payload shape, or the genesis-sentinel format invalidates every row written under it. | [activity/tests/test_audit.py](activity/tests/test_audit.py) — `TestComputeAuditHash` (dispatch + v1/v2/v3 paths), `TestHashTamperDetection` (naive-tamper round-trip), `TestVerifyChangeHash` (read-side gate per stored algorithm), `TestChainWrites` (genesis sentinel, monotonic sequence_no, shard isolation, v1 fallback), `TestChainVerification` (naive content tamper at target row, covered-track tamper at next row's link, gap detection, lifted-genesis detection, pre-chain row exclusion). |
| [epicurrents/offload.py](epicurrents/offload.py) | Reverse-proxy byte-serving handoff. The `apply_middleware` interlock is the only barrier between a de-identified grant and the raw PHI on disk: with middleware applied the caller's bytes are computed per request and exist in no file, so naming a path serves the original recording — patient identification and clinical annotation text included — with a 200 and nothing in any log to notice. Every refusal returns `None` and the caller streams as before, so the failure direction is safe by construction; the hazard is a new call site that omits the flag, not the helper misbehaving. The path mapping is the second contract: `X-Serve-Path` is resolved by Caddy against `/srv/protected`, and Django, the Caddyfile and the compose mount must agree or every download 404s. | [epicurrents/tests/test_offload.py](epicurrents/tests/test_offload.py) — `TestRefusals` (middleware interlock, traversal, symlink escape, outside-root, missing file), `TestCapabilityGate` (off by default, declines with no proxy in front), `TestDeploymentPairing` (Caddyfile root, compose mount, `no-store` on the offloaded response), `TestCallSiteDiscipline` (keyword-only flag plus a source scan for call sites that omit it). |
| [activity/system_activity.py](activity/system_activity.py) | Audited-scope entry point for Celery tasks and management commands, and the only one — no HTTP request exists, so nothing else opens the scope. Two silent-failure classes. Narrowing what the context manager sets strips audit coverage from every background write at once, the same hazard as the middleware and `_track_sender` on the request side. And `target_identifier` must stay a locator: it once held `str(target)`, which published `Recording.original_name` into a permanent column on every processed recording — past the field mask, past the author-private API gate, and past every erasure path, none of which touch that column. Anything rendered rather than referenced here is unrecallable. | [activity/tests/test_system_activity_identifier.py](activity/tests/test_system_activity_identifier.py) — locator shape, no `__str__` content, target columns still resolve, empty without a target, and the model's own `__str__` left intact so relocating the fix into it is deliberate. Scope-opening behaviour in [activity/tests/test_system_activity.py](activity/tests/test_system_activity.py). |
| [activity/request_context.py](activity/request_context.py) | ContextVar bridge between the audited-scope entry points (HTTP middleware, `with_system_activity`) and the audit signals. The flag is `current_is_audited_context`; changing the ContextVar contract (defaults, set/reset shape) decouples entry points from signals — same end result as breaking either side individually. | Covered indirectly by the middleware integration test and [activity/tests/test_system_activity.py](activity/tests/test_system_activity.py) |
| [activity/erasure.py](activity/erasure.py) | GDPR Art. 17 subject erasure across the audit trail — `erase_subject` scrubs registered PII from `ObjectChangeLog` payloads and `Activity` metadata, tombstoning each row (`erased_at` + re-sealed `erased_hash`) while leaving `after_hash` untouched so chain links keep verifying. Two silent-failure classes: scrub narrowing (a lost registration or filter change leaves personal data in the permanent trail — the unfulfillable-erasure gap this module closes), and seal weakening (post-erasure tampering becomes undetectable, or the chain breaks). The `register_subject_pii` registry is part of the surface; core registrations live in [user/apps.py](user/apps.py) and [notifications/apps.py](notifications/apps.py). The registry takes plain strings and validates nothing at call time, so [activity/checks.py](activity/checks.py) validates it at `manage.py check` — a wrong model label, `owner_field` or `pii_fields` entry otherwise scrubs nothing while reporting zero rows, which is indistinguishable from a clean run. | [activity/tests/test_erasure.py](activity/tests/test_erasure.py) — scrub completeness per registered model, erased-row verification via `erased_hash`, chain intactness after erasure, sealed `erase` record, idempotency, post-erasure tamper detection, rollback refusal. Registry validation in [activity/tests/test_checks.py](activity/tests/test_checks.py), including a source assertion that `ActivityConfig.ready` still imports the checks — the runtime one cannot see that, since the test's own import registers them. |
| [user/management/commands/erase_user.py](user/management/commands/erase_user.py) | GDPR Art. 17 account-erasure fulfilment path — the only deletion route that unlinks owned recording / media files (FK cascade never touches the filesystem), flushes the subject's sessions, deletes the account, and scrubs the audit trail via `erase_subject`. Silent regression of any step leaves erasure requests unfulfillable (stranded PHI files, residual identifiers) with no visible signal. | [user/tests/test_erase_user.py](user/tests/test_erase_user.py) — full-flow no-residual-identifiers sweep, file unlinks, session flush, unlink-failure abort-before-DB-changes, scrub-only path for already-deleted accounts. |
| [epicurrents/security_log.py](epicurrents/security_log.py) | SIEM rule surface — the logger name `"epicurrents.security"`, the structured-log key `"security_event_type"`, and the well-known event-type set are all operator-visible API that downstream alert rules pivot on. Silent renames leave the application logging happily while every rule stops matching. The production JSON formatter [epicurrents/log_formatters.py](epicurrents/log_formatters.py) is part of the same surface: it must emit `extra=` fields (notably `security_event_type`) as discrete JSON keys, or the pivot field silently disappears from shipped logs even though the emission side is intact. | [epicurrents/tests/test_security_log_taxonomy.py](epicurrents/tests/test_security_log_taxonomy.py) (event-type set), [epicurrents/tests/test_security_log_emission.py](epicurrents/tests/test_security_log_emission.py) (logger name + extra-key shape + WARNING level), [epicurrents/tests/test_log_formatter.py](epicurrents/tests/test_log_formatter.py) (formatter emits `security_event_type` as a top-level JSON key) |
| [epicurrents/urls.py](epicurrents/urls.py) | Fallback ordering. The three entries appended at the bottom — `api_not_found`, then the two SPA routes — must stay below every real mount, and nothing may be appended after them. The SPA catch-all matches every path, so anything registered after it is unreachable; `api_not_found` matches every API-shaped path, so hoisting it above the mounts 404s the API and turns "not signed in" into "no such endpoint". Its presence is what stops a mistyped API write from answering 200 with index.html, which reads as success to every caller. | [epicurrents/tests/test_api_path_not_found.py](epicurrents/tests/test_api_path_not_found.py) — resolution target per mount style, SPA deep links unaffected, JSON 404 shape, every unsafe method, real routes still win, authenticated routes still 401, nothing registered after the catch-all. |
| [epicurrents/settings/common.py](epicurrents/settings/common.py) | `MIDDLEWARE` ordering — `AuthenticationMiddleware` must precede `ApiActivityLoggingMiddleware` so `request.user` is populated when the audit row is built. Removing or reordering the audit middleware silently degrades every Activity row's actor to `None`. | [epicurrents/tests/test_middleware_failure_modes.py](epicurrents/tests/test_middleware_failure_modes.py) — `test_middleware_is_registered` + `test_middleware_runs_after_authentication`. The contract test lives with the middleware (its consumer), not with the settings. |
| [federation/auth.py](federation/auth.py) | Federated authentication surface — JWT signature/alg/exp/iat/aud verification, replay (`jti`) cache, SSRF guard, strict TLS context, `is_trusted` peer gate, local-key consistency. Silent weakening of any layer opens cross-instance auth bypass. | [federation/tests/test_auth.py](federation/tests/test_auth.py) — ~40 cases covering every check (verify failures, iat edge cases, replay, leeway, SSRF guard across 8 IP categories, TLS context strictness, key consistency, issuer normalisation). |
| [federation/middleware.py](federation/middleware.py) | PHI sanitization on the wire — every EDF/BDF byte served to a federated peer or to a caller with `apply_middleware=True` passes through this pipeline. Three silent-failure classes: sanitization regression (`AnonymizeEDFHeader` ↔ `_build_clean_header` / `deidentify_signal_infos` drift — the class applies both the subject and the channel-block transforms), pipeline ordering / scope-filter regression, fail-open-on-parse-error (deliberate but worth coordinating). | [federation/tests/test_middleware.py](federation/tests/test_middleware.py) — 70 cases covering pipeline shape, scope filtering, size-preserving properties, the actual PHI removal (`"X X X X" in anon_hdr.patient_id`), the channel-block cleaning (canonical / `MISC_<n>` labels, blanked transducers, reconstructed prefiltering, annotation-label preservation, isometry), and the fail-open behaviour. |
| [library/permissions.py](library/permissions.py) | Access grants from Dataset sharing, and the author-only gate on Collections. `can_read_via_dataset` is registered as a `can_read_object` extension in `library.apps.LibraryConfig.ready()`; three silent-failure classes: extension regression silently breaking dataset sharing, soft-delete filter bypass, `apply_middleware` propagation drop (flips EDF serving from anonymised to raw). The collection helpers must stay author-only — collection sharing was removed, and a collection-targeted `AccessRight` row granting anything would silently reopen it. | [library/tests/test_permissions.py](library/tests/test_permissions.py) — dataset extension cases including `apply_middleware` propagation for both values, plus `TestCollectionRowsGrantNothing` (stale collection-targeted rows are inert). Author gate additionally covered in [library/tests/test_models.py](library/tests/test_models.py). |
| [media/permissions.py](media/permissions.py) | Attached-media access inheritance — `can_read_via_attachment` is registered as a `can_read_object` extension in `media.apps.MediaConfig.ready()`. It is the only path by which a grantee reaches media attached to a recording (no separate sharing surface exists for attached media), so a regression silently hides every attached clip / document from everyone but the author. It delegates to `can_read_object` on the parent, so the media inherits access from any source (direct grant, dataset share, share token). | [media/tests/test_permissions.py](media/tests/test_permissions.py) — `TestMediaInheritsAttachmentAccess`: grantee-of-parent reads attached media (direct grant + dataset inheritance), no-parent-access denies, unattached media not granted. |
| [recordings/processors/edf.py](recordings/processors/edf.py) | PHI removal in EDF / BDF headers — `_build_clean_header` is the canonical de-identification function the platform delegates to. Note the deliberate split with its public sibling `build_header`, which serializes a header and copies identification fields verbatim: the serializer is API a project may call, the de-identifier is not, and promoting `_build_clean_header` to match would turn a contract into a parameter. Hardcoded byte values (`"X X X X"`, `"Startdate X X X X"`, `b"01.01.85"`, `b"00.00.00"`) look like they could be "parameterised for testability" or replaced with real values — don't. Each silent change leaks PHI on every uploaded recording (the stored file uses this) AND on every federated download with `apply_middleware=True` (where the federation middleware fails open on parse error, so this function producing the correct bytes is the last line of defense). `deidentify_signal_infos` is the channel-block counterpart (site de-identification): canonical-or-`MISC_<n>` labels fail-closed, blanked transducers, reconstructed prefiltering, `source_*` capture, and a uniqueness guarantee for EEG labels — silently keeping a raw label on the "unresolved" path re-leaks the site fingerprint the pass exists to remove. `reorder_edf_channels` permutes data records into the canonical channel order and is the one ingest pass that moves *sample bytes*: a subtle offset or width error (mixed rates, BDF 3-byte samples) corrupts every recording silently, and its short-read guard is what keeps a truncated file from being permuted into garbage. | [recordings/tests/test_edf_processor.py](recordings/tests/test_edf_processor.py) — `TestRewriteEdfHeader` asserts each PHI-removal byte explicitly (`test_patient_field_blanked`, `test_recording_field_blanked`, `test_start_date_anonymised`, `test_start_time_zeroed`), plus EDF+C/EDF+D marker preservation, BDF binary version byte, ASCII cleaning, and data-records-untouched. [recordings/tests/test_channel_deidentification.py](recordings/tests/test_channel_deidentification.py) — cleaned bytes on disk, fail-closed `MISC_<n>`, collision fallback, source capture through persistence, refresh preservation, API gating. [recordings/tests/test_channel_order.py](recordings/tests/test_channel_order.py) — bit-exact per-channel sample movement (EDF and BDF widths, mixed rates), annotation channel last with TALs intact, `source_index` permutation record, ordered-file no-op. |
| [recordings/api/v1/ninja.py](recordings/api/v1/ninja.py) — `_build_serve_pipeline` | Serving-pipeline parity — the single construction site for the sanitization pipeline (`AnonymizeEDFHeader` + `StripAnnotationTextMiddleware`) applied on every byte-serving path: full download, range request, time-range slice, and the peer download-size computation. The hazard is divergence, not absence: a serving path that hand-rolls its own `MiddlewarePipeline` can anonymise the header while leaking clinical annotation text, with every locally-written test for that path still green. Two rules keep paths in sync: byte-serving code with `apply_middleware=True` calls `_build_serve_pipeline()` (never a hand-rolled non-empty pipeline), and a new byte-serving endpoint must be added to the request-shape list in the contract test. | [recordings/tests/test_serve_pipeline_parity.py](recordings/tests/test_serve_pipeline_parity.py) — `TestServePipelineParity` (per-shape sanitization parity for middleware callers, raw-bytes parity for authors), `TestSinglePipelineSource` (source scan rejecting `MiddlewarePipeline` construction outside `_build_serve_pipeline`). |
| [recordings/tasks.py](recordings/tasks.py) — `purge_deleted_recordings` | GDPR Art. 17 erasure pipeline — actually removes the soft-deleted row + file pair after the retention window. Two filters define the contract: the soft-delete filter (`deleted_at__isnull=False, deleted_at__lt=cutoff, status__in=[READY, FAILED]`) and the orphan reaper filter (`status__in=[PENDING, PROCESSING], created_at__lt=cutoff`). Silent narrowing keeps PHI past the window; silent widening reaps live data; a regression in the file-unlink-vs-DB-delete ordering produces half-states (file gone, row stays, or row gone, file stays — both bad). Originals-volume non-interaction is part of the contract: the task must never read or write `RECORDINGS_ORIGINALS_PATH`, the operator's regulatory backstop. | [recordings/tests/test_tasks.py](recordings/tests/test_tasks.py) — `TestPurgeDeletedRecordingsContract` pins each invariant: active recordings untouched regardless of age, cutoff-boundary semantics (off-by-one in either direction), file-unlink failure preserves the DB row on both soft-delete and orphan paths, FAILED status not orphan-reaped, originals volume untouched. |
| [media/tasks.py](media/tasks.py) — `purge_deleted_media` | GDPR Art. 17 erasure pipeline for non-signal media (the video / document analog of the recordings purge). One filter defines the contract: `deleted_at__isnull=False, deleted_at__lt=cutoff`. Silent narrowing keeps PHI-bearing media (a face in a video, a subject identifier in a document) past the window; widening reaps live files; a regression in the file-unlink-vs-DB-delete ordering leaves half-states. No orphan-reaper branch exists — media has no processing step, so an active row (`deleted_at` null) must survive any age. | [media/tests/test_tasks.py](media/tests/test_tasks.py) — `TestPurgeDeletedMediaContract`: active rows untouched regardless of age, cutoff-boundary semantics, file-unlink failure preserves the row, missing-file still purges, DELETE audit row per purged file under the `media.purge` activity. |
| [user/oidc.py](user/oidc.py) | External-login authorization boundary. Three checks decide whether a browser holding a provider token becomes a platform user, and which one: `_check_claims` (issuer / audience / **tenant `tid`** / nonce on the ID token — the single-directory lock), `email_domain_allowed` (the email-domain allowlist, PHI-containment control #1, fail-closed), and `resolve_identity` (find-or-create on the pairwise `sub`, applying the auto-create / verified-email linking policy). `validate_id_token` is the cryptographic root of trust (JWKS signature). Dropping the `tid` check, defaulting the domain gate open, or linking on an unverified email each silently opens cross-tenant / cross-domain account access. Inert while `OIDC_ENABLED` is off, but the contract is what must not break when it is on. | [user/tests/test_oidc.py](user/tests/test_oidc.py) — `TestCheckClaims` (issuer / audience / tenant / nonce gate), `TestDomainAllowlist` (allow / reject / fail-closed / guest rejection), `TestResolveIdentity` (auto-create, returning-login reuse, domain reject-before-create, auto-create-disabled, verified-email link, unverified no-link), and the `/oidc` endpoint tests (disabled→404, state mismatch, domain reject, successful login). |

### Conventions for working with load-bearing files

- **Read the contract before editing.** The header block names the feature at stake and points at the contract test. If the change is anything more than a behaviour-preserving refactor, run the contract test first to confirm it's currently green, then again after the change.
- **When tightening a check, enumerate the inputs that previously matched.** This is the failure mode that produced the 2026-05-25 middleware regression (`"/api/" in path` → `path.startswith("/api/")`). The tightener evaluated the new form against *one* concern (a hypothetical future false positive) but did not enumerate the current API mounts that the *old* form happened to catch by substring. Whenever narrowing a path matcher, URL pattern, exception list, or any other input filter on a load-bearing file, list the inputs that previously matched and confirm each still matches after the change. The discharge recipe:
    1. Describe the old match set in plain English.
    2. Describe the new match set in plain English.
    3. List the concrete inputs the old set caught — file:function:URL-pattern triples, or whatever the relevant unit is.
    4. For each input, mark the new outcome as *same*, *intentionally different*, or *regression*. A regression stops the change.
    5. Write the conclusion in the commit message body (or the PR description) so the next reviewer finds it via `git log`.
    6. Empty `.review/findings/load-bearing-diff-reviewer.md` to signal discharge for the current commit. The agent re-flags on the next run if the tightening is still in the diff window — that's correct; it's a prompt to re-enumerate, not a permanent verdict.
- **Don't "fix" a failing contract test by editing the test.** A red contract test on a load-bearing file means the feature it guards is broken. Fix the production code, not the assertion.
- **Adding a file to the table is a Cross-cutting Rule change.** It needs the same care as adding to any other table in this document — the registry only works if it stays accurate.

## Repository orientation

Neurophysiological signal viewer — Django 6 + Django Ninja REST API, Celery workers, Vue 3 + TypeScript frontend, PostgreSQL, Redis. Deployed via Docker Compose; see `scripts/` for host tooling.

### Backend apps

| App | Responsibility |
|---|---|
| `activity` | Per-request audit log + field-level change tracking + rollback API at `/api/v1/activity/`. See [activity/README.md](activity/README.md). |
| `annotations` | Generic annotations attached to any model via content types. Four concrete types (`Annotation`, `Event`, `Interruption`, `Label`) plus `Code` for standardised classification. API at `/annotations/api/v1/`. See [annotations/README.md](annotations/README.md). |
| `compute` | Server-side scientific Python (MNE, LAPACK/BLAS) that can't run in Pyodide. Lead-field caching for source localisation. API at `/compute/api/v1/`. See [compute/README.md](compute/README.md). |
| `epicurrents` | Core app: `AccessRight`, permission functions, request middleware, settings layers, project loader, lifecycle management commands, health endpoint at `/api/v1/`. See [epicurrents/README.md](epicurrents/README.md). |
| `federation` | Inter-instance federated data sharing — `FederatedPeer`, Ed25519 JWT auth, well-known endpoint, peer/grant management API at `/api/v1/federation/`, optional FUSE filesystem for mounting remote recordings as local files. See [federation/README.md](federation/README.md). |
| `library` | Collections (folder tree), Datasets (flat sets with downward read inheritance), Tags (hierarchical labels). Endpoints at `/api/v1/library/`. See [library/README.md](library/README.md). |
| `media` | Non-signal media files (documents today; image / audio / video later) with per-project extension allowlist and optional GenericFK attachment to a parent object. API at `/media/api/v1/`. See [media/README.md](media/README.md). |
| `notifications` | `PushSubscription` model, VAPID subscribe/unsubscribe API, `send_push_to_user` Celery task. See [notifications/README.md](notifications/README.md). |
| `recordings` | EDF/BDF upload, processing, storage, delivery. Owns the ingest pipeline + converter registry, soft-delete / purge, bulk import. API at `/recordings/api/v1/`. See [recordings/README.md](recordings/README.md). |
| `user` | Custom `AbstractUser` model (`AUTH_USER_MODEL`), login/logout/profile/password-reset endpoints at `/api/v1/user/`. See [user/README.md](user/README.md). |

### API layout

```
/api/v1/                epicurrents (health)
/api/v1/user/           login, logout, me, change-password, reset-password, search
/api/v1/activity/       changelog, rollback, bulk-rollback
/api/v1/notifications/  vapid-public-key, subscribe, unsubscribe
/api/v1/library/        collections + datasets CRUD, items (generic), access rights, tags
/api/v1/federation/     peers, grants, inbound object check
/.well-known/epicurrents-federation.json   public key document
/recordings/api/v1/     upload, list, status, download, delete (soft)
/media/api/v1/          upload, list, detail, file download, patch, delete (soft)
/annotations/api/v1/    bundles, events, interruptions, labels, codes
/compute/api/v1/        eeg/leadfield/ — list, trigger, metadata, binary download
```

### Frontend (`frontend/src/`)

Vue 3 + Vite + Pinia + Vue Router. Path aliases: `#api/`, `#composables/`, `#stores/`, `#views/`, `#lib/`, `#i18n`. UI components are WebAwesome (`wa-*`) — initialised with `setBasePath` in `main.ts`. API calls go through `#lib/http` (axios wrapper). Auth state lives in `#stores/auth`.

**Mock dev server**: set `VITE_BACKEND_URL=mock` in `frontend/.env` to run against an in-memory API (no backend stack required). State resets on every page reload. Handler: `frontend/mocks.ts`. See `frontend/README.md` for seed data details.

**`v-wa` directive** (`frontend/src/directives/wa.ts`, registered globally): two-way binding for WebAwesome form controls (`wa-input`, `wa-textarea`, `wa-combobox`, `wa-select`, `wa-switch`, `wa-checkbox`). Standard `v-model` does not work reliably on these custom elements — always use `v-wa` instead. Syntax: `v-wa="[reactiveObject, 'key']"` where `reactiveObject` is a `reactive({})` object and `'key'` is the property name. Never bind to a standalone `ref` — wrap the value in a reactive object first.

### Infrastructure

- `docker-compose.yml`: `web`, `celery`, `celery-beat`, `db` (Postgres), `redis`, `borg` (borgmatic backup). All app containers share `.:/code` bind mount.
- Runtime state lives in per-domain named volumes (`postgres-data`, `recordings-data`, `staging-data`, `media-data`, `celery-data`, `borg-data`). The earlier single-volume + `subpath:` layout was reverted because Podman's Docker-API socket drops the `subpath:` mount option; see the volume-layout note at the top of that file.
- Compose files layer: `docker-compose.yml` (dev) + `docker-compose.prod.yml` (gunicorn, production mode, public binding) + optional `docker-compose.proxy.yml` (Caddy TLS termination and static offload, config in `caddy/Caddyfile`). `bootstrap.sh` and `update.sh` add the proxy overlay when `.env` carries a `PROXY_DOMAIN` value — both scripts must agree, or an update orphans the running proxy.
- `scripts/bootstrap.sh` installs Docker on a fresh Ubuntu VM.
- `.env` is generated by `python manage.py init_env` (auto-fills `SECRET_KEY`, `BORG_PASSPHRASE`, `ADMIN_PASSWORD`, VAPID keys, and federation Ed25519 keys).

## Subsystem cheat sheet

One-paragraph behaviour summaries with pointers to the relevant README for full detail. Use these when you need orientation without reading the whole app.

- **Permissions**: four functions in `epicurrents.permissions` (`can_read_object`, `can_write_object`, `can_modify_object`, `can_annotate_object`) plus `ensure_*` raisers and `get_read_access_result` for metadata-carrying reads. Extension protocol via `register_read_permission_extension`. Share-token rows are always `can_write=False`, `can_share=False`. See [epicurrents/README.md](epicurrents/README.md#permissions).
- **`ReadAccessTerms` resolution**: superuser fast-path → read-visibility gates (restrictive; a gate that hides the object denies before any grant is read) → direct `AccessRight` query → registered extensions. If a direct row matches, its `apply_middleware` flag is returned and extensions are **not** consulted (early return). Per-target uniqueness (one `AccessRight` row per `(object, target)`, three partial constraints) plus deterministic ordering — direct user row over group rows, exact federated user over the peer wildcard, de-identifying row among equals — make the winning row defined rather than database row order; grant endpoints answer a duplicate target with 409. An extension grant carries the terms of the row it inherited from — dataset membership returns the dataset right's `apply_middleware` — so a sharer's de-identification choice survives inheritance. `can_read_object` is a bool wrapper for `get_read_access_result(...).granted`.
- **Federated resolution** is the same shape with a different caller: `get_federated_read_access_result` consults read-visibility gates, then direct `AccessRight` rows for the peer, then extensions registered with `register_federated_read_extension`. Local extensions cannot serve a peer — they take a user, groups and a share token, none of which a peer has — so a federated path needs its own registration or the peer reaches nothing it lacks a per-object grant on. The registration takes a pair: a per-object check and a `visible_terms` batch answer for listing endpoints, together so the two cannot drift. The batch half returns terms rather than ids because a listing advertises a download size that depends on `apply_middleware`.
- **Audit trail + rollback**: signals in `activity/signals.py` auto-log create/modify/delete on tracked models; `ApiActivityLoggingMiddleware` wires the request-scoped `Activity` and user into ContextVars. Rollback endpoints at `/api/v1/activity/{rollback/{id},rollback/bulk,changes/}`. See [activity/README.md](activity/README.md).
- **Soft delete**: `Recording.deleted_at` and `Collection.deleted_at` — null means active; non-null means trashed. Recording purge timing, orphan reaping, and file removal in [recordings/README.md](recordings/README.md#soft-delete-and-purge).
- **Collection access**: collections are author-private — `library.permissions.can_read_collection` / `can_write_collection` grant only the author and superusers, and no `AccessRight` may target a collection. Sharing goes through datasets (the Collection → Dataset export carries a tree across). Dataset-inheritance semantics and `apply_middleware` propagation in [library/README.md](library/README.md#permission-extensions).
- **Tag hierarchy + per-item access N+1**: `Tag.parent` is an adjacency-list FK; `GET /tags/{id}/items/` defaults to including descendants via `_get_tag_subtree_ids`. Collection / tag item listings use per-item access checks (`_filter_readable`), which is N+1 — fine for bounded scopes with mostly-readable items, expensive otherwise. Batched-check migration path in [library/README.md](library/README.md#gotchas).
- **One-collection-per-recording**: `CollectionItem` carries two `UniqueConstraint`s; the second means recordings belong to at most one collection globally. Adding a recording already in another collection returns 409. Test both constraints separately. See [library/README.md](library/README.md#collectionitem).
- **Federation**: Ed25519 JWT auth, well-known public-key publication, peer + grant management at `/api/v1/federation/`, optional FUSE filesystem. Federation grants are `AccessRight` rows with `federated_peer` set. `remote_user_id=""` is a wildcard (any authenticated user from that peer). **Do not add federated group targets** — only the local data owner decides who gets access to local data. See [federation/README.md](federation/README.md).
- **EDF middleware pipeline**: `federation/middleware.py` defines three ABCs — `EDFHeaderMiddleware` (isometric), `EDFSignalMiddleware` (per-record, range-aware), `EDFFullFileMiddleware` (arbitrary size). Used on both the HTTP download path and the FUSE filesystem. Class hierarchy, scope targeting, size properties, `SignalPipelineContext`, and `build_signal_context_from_infos` in [federation/README.md](federation/README.md#middleware-pipeline).
- **`SignalInfoOut` schema** (`recordings/api/v1/ninja.py`): per-channel descriptor on `RecordingMetaOut.signals`. Used by federation peers to compute download sizes without re-parsing EDF bytes. Full field list in [recordings/README.md](recordings/README.md#recordingmeta-and-signalinfo).
- **Recording ingest pipelines + format converters**: `recordings/pipelines.py` exposes `get_pipeline(label)` and `get_converter(ext)`. Built-in pipelines `"web"` (upload Celery task) and `"import"` (bulk import); built-in converter `.csv` → EDF (via `recordings/converters/csv2edf.py`, which dispatches to a registry of per-format subconverters — projects inject their own with `register_csv_subconverter` from `AppConfig.ready()`); further converters (a vendor `.e` → EDF converter, for instance) install as separate packages and register through `RECORDING_CONVERTERS`. Float→int EDF writing is `recordings.processors.edf.write_edf`; EDF→EDF transforms keep copying integer samples verbatim. Override / extend via `RECORDING_PIPELINES` and `RECORDING_CONVERTERS` in settings. See [recordings/README.md](recordings/README.md#ingest-pipelines).
- **Annotation permission model + `annotator` field**: annotation creation uses `can_annotate_object` (lower bar than `can_write`), update / delete uses `can_modify_object(annotation)`. All four `*In` schemas carry an optional `annotator` string, required when authenticating via `share_token`. See [annotations/README.md](annotations/README.md#permission-model).
- **Annotation `Code` vocabularies**: external standards use their registry identifier as `standard` (`hed`, `icd10`); project-local codes use `epicurrents.<project>.<concept>`. Validators register via `register_vocabulary` in [annotations/vocabularies.py](annotations/vocabularies.py) from an owning `AppConfig.ready()` — core ships the mechanism with zero vocabularies. See [annotations/README.md](annotations/README.md#code--standardised-classification).
- **`object_hash` uniqueness**: `AnnotationBase` enforces uniqueness per `(target_content_type, target_object_id, object_hash)` per concrete model. Caller-supplied for client annotations; server-generated annotations use `_annotation_hash(recording.pk, suffix)` keyed on the recording PK so re-uploads produce fresh hashes. Suffix conventions in [annotations/README.md](annotations/README.md#object_hash).
- **Shared `recordings/tasks.py` helpers**: `_save_edf_results`, `_annotation_hash`, `_determine_modality` are imported by `import_recordings`. Don't rename without updating both call sites. The sidecar parser lives at [recordings/converters/sidecar.py](recordings/converters/sidecar.py) (split out so the post_convert hook can fire it automatically on the Celery path) — its `save_sidecar_events` function is also imported by `import_recordings`. See [recordings/README.md](recordings/README.md#bulk-import-via-import_recordings).
- **Upload atomicity**: the upload endpoint wraps `Recording` + `AccessRight` creation in `transaction.atomic()` and dispatches `process_recording` via `transaction.on_commit()`. See [recordings/README.md](recordings/README.md#upload-contract).
- **Auth: login rate limit, password validation, password-reset cooldown**: enforced in `user/api/v1/ninja.py`. Constants and cache-key shapes in [user/README.md](user/README.md#security-mechanisms). The password-reset cooldown duration is mirrored in `LoginView.vue` — change both together.
- **Push notifications**: `notifications.tasks.send_push_to_user.delay(user_id, title, body, data={"type": "..."})` from anywhere; add a `notificationclick` branch in `frontend/public/push-handlers.js` only if you need behaviour beyond opening `/`. See [notifications/README.md](notifications/README.md).
- **Settings modes**: `DJANGO_MODE=development` → SQLite + `DEBUG`; `DJANGO_MODE=production` → Postgres, `DEBUG=False`. Full settings layout in [epicurrents/README.md](epicurrents/README.md#settings-architecture).

## Testing conventions

- **Run**: `pytest` from the repo root (settings auto-selected via `pytest.ini`).
- **Fixtures**: `user`, `superuser`, `auth_client`, `superuser_client`, `make_user`, `make_superuser` are available globally from `conftest.py`.
- **API tests**: use `client.force_login(user)` + `client.post(url, json.dumps(data), content_type="application/json")`. Helper functions `post_json`, `patch_json`, `delete_json` live in `conftest.py`.
- **Celery tasks**: `CELERY_TASK_ALWAYS_EAGER=True` runs tasks synchronously; just call the task function directly in tests or use `.delay()` — both work.
- **`webpush` mock path**: patch at `pywebpush.webpush`, not `notifications.tasks.webpush` — the symbol is imported inside the task function. Worked test pattern in [notifications/README.md](notifications/README.md#mocking-webpush).
- **`auto_now_add` fields**: use `.update()` after creation to backdate `created_at` (e.g. `activity.tests.test_tasks._backdate`).
- **`make_user` / `make_superuser` unique names**: both fixtures use an `itertools.count` counter to generate unique `testuser_N` / `testsuperuser_N` usernames when none is passed. Pass an explicit `username=` when the test depends on a specific name.
- **Reset-password rate limit in tests**: rate-limited per email address (case-normalised). A second request for the same address within the window returns 429; different addresses are independent. Use distinct emails for parallel reset-flow tests.
- **`AccessRight` for listing annotations**: `list_annotations` calls `can_read_object` on the parent object — the recording author must have an explicit `AccessRight` row (not auto-granted by `baker.make`).
- **Adding a new test file**: create `<app>/tests/test_<topic>.py` (the directory and `__init__.py` already exist for every app).
- **Shell-script tests**: dry-run tests for `scripts/*.sh` live in [scripts/tests/](scripts/tests/) and use the `fakebin` fixture from [scripts/tests/conftest.py](scripts/tests/conftest.py). The fixture builds an isolated directory of stub binaries that log every invocation; the test runs the staged script with `PATH=<fakebin>:/usr/bin:/bin` and asserts on the call log. `sudo` is stripped to a passthrough so the underlying command lands cleanly. Use `fakebin.stub(name, body=...)` for branch-specific output (e.g. fake `--version` strings), `fakebin.remove(name)` to simulate a missing binary, and `fakebin.has_call("substring")` to assert. For new bootstrap-script branches, follow [scripts/tests/test_bootstrap_podman.py](scripts/tests/test_bootstrap_podman.py) — one test per observable, all stubbing in `conftest.py`. Real container-runtime behaviour (image pulls, volume permissions, init order) is out of scope for these tests; that's the Tier 3 container-integration harness, deliberately not built yet.
- **Linting shell scripts**: `scripts/lint-shell.sh` runs shellcheck + `bash -n` on every `scripts/*.sh`. Uses host shellcheck if present, falls back to `koalaman/shellcheck:stable` via Docker so contributors don't need a local install. CI installs shellcheck natively (`apt-get install -y -qq shellcheck`) and invokes the same wrapper.
