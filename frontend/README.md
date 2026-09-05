# Epicurrents Frontend

Vue 3 + TypeScript frontend scaffolded with Vite.

## Scripts

- `npm run dev` starts the Vite development server.
- `npm run build` runs type-checking then creates a production build.
- `npm run build:viewer` builds the viewer's full edition from the `viewer/` submodule into `viewer-dist/`, then the platform-side base bundle ([vite.config.base.ts](vite.config.base.ts)); it needs the submodule's packages built first (`npm run setup` inside `viewer/`, or `scripts/rebuild-frontend.sh --viewer`).
- `npm run test` runs the vitest suite; `npm run test:watch` keeps it running.
- `npm run preview` serves the built app locally.
- `npm run link:projects` symlinks `node_modules` into each project frontend; `postinstall` runs it for you.

## Environment Variables

Copy `.env.example` to `.env` (or provide equivalent server-side env values).

- `VITE_API_BASE_URL`: Axios base URL used by `src/lib/http.ts`.
- `VITE_BACKEND_URL`: Controls API routing in the Vite dev server. Set to `mock` for the in-memory mock API (no backend needed), or to a URL like `http://localhost:8000` to proxy requests to a real Django server. Leave unset when the backend shares the same origin (e.g. behind a reverse proxy).
- `VITE_ENABLE_SINGLEFILE`: Enables `vite-plugin-singlefile` when set to `true`.

See [.env.example](.env.example) for defaults.

## Mock dev server

The Vite dev server includes an in-memory mock API that covers all currently implemented endpoints. It lets you work on UI styling and interactions without running Django, Celery, PostgreSQL, or Redis.

**Enable:**
```
# frontend/.env
VITE_BACKEND_URL=mock
```
Then run `npm run dev` as normal. Log in with any credentials — the mock accepts everything and returns a fixed `mockuser` session.

**Seed data** (resets on every full page reload):
- 15 recordings — 12 ready EEG (resting-state, P300, ERP, N-back, SSVEP), 1 EMG, 1 ECoG, 1 pending EEG
- 2 collections: *Sleep Studies* (2 items, 1 share token), *Epilepsy Cases* (1 item)
- 2 datasets: *Public EEG Dataset* (2 items, 1 share token), *Research Cohort A* (1 item)

**Behaviour notes:**
- The pending recording flips to `ready` on its second status poll, exercising the upload-progress UI.
- Renaming a recording propagates immediately to any collection/dataset items that reference it.
- All CRUD is fully in-memory — nothing is persisted between page reloads.

The mock handler lives in [mocks.ts](mocks.ts) (project root, compiled by Vite at dev-server startup). The Vite plugin wiring is in [vite.config.ts](vite.config.ts).

## App Wiring

- Router setup: [src/router/index.ts](src/router/index.ts)
- State setup (Pinia): [src/stores/index.ts](src/stores/index.ts)
- i18n setup: [src/i18n/index.ts](src/i18n/index.ts)
- HTTP client: [src/lib/http.ts](src/lib/http.ts)
- Vite plugins/config: [vite.config.ts](vite.config.ts)

## Project and Plugin Extensions

Project-specific frontend modules live under `src/projects/<name>/` and are
selected at build time via `VITE_PROJECT` (see [src/projects/active.ts](src/projects/active.ts)).
Exactly one project is active per build.

Plugin frontend modules live under `src/plugins/<name>/` and are selected at
build time via the comma-separated `VITE_PLUGINS` (see [src/plugins/active.ts](src/plugins/active.ts)).
Zero or more plugins may be enabled alongside the active project; their
contributions are merged by `mergePlugins` in [src/plugins/base.ts](src/plugins/base.ts). Plugins use
the same contract as projects, re-exported from [src/plugins/types.ts](src/plugins/types.ts). DICOM is
a plugin (`VITE_PLUGINS=dicom`).

The plugin contract in `src/projects/types.ts` supports:

- `routes`: additional Vue Router records injected at startup.
- `navLinks`: additional top-navigation links injected into `App.vue`.
- `icons`: additional icon name → raw SVG entries for project-only views/nav.
- `iconLibraries`: optional named icon libraries for variant icons (for
    example regular + solid with the same `name`).

For nav highlighting, routes should set `meta.navSection`, and each nav link
should set the matching `section` key. `App.vue` marks a link as active when
`route.meta.navSection === link.section`.

A project's frontend lives at `projects/<name>/frontend/`, outside this
directory, because the project is its own repository checked out alongside the
platform. `src/projects/` keeps only what the platform owns: the plugin type,
the base no-op plugin, the event bus, and
[active.ts](src/projects/active.ts), which resolves the one active project
through the `#project` alias.

Example, for a project supplying a session list:

- `projects/<name>/frontend/index.ts` adds `/sessions` to `routes`.
- `projects/<name>/frontend/index.ts` adds a `Sessions` entry to `navLinks`.
- `projects/<name>/frontend/SessionsView.vue` provides the route component.
- `projects/<name>/frontend/icons.ts` registers project-only icons (merged at startup).

Base and project icon registries are merged in `src/main.ts`. If a project icon
name already exists in `src/icons.ts`, the base icon wins and the project
entry is ignored to prevent accidental overrides.

When multiple variants of the same icon are needed, keep the default registry
for one variant and register others under `iconLibraries` (project plugin).
Then choose variants in templates with the `library` attribute:

```vue
<wa-icon name="users"></wa-icon>
<wa-icon library="fa-solid" name="users"></wa-icon>
```

This is backwards-compatible: existing icons that omit `library` continue to
resolve through the default icon library.

## Routes

| Path | View | Description |
|---|---|---|
| `/` | `HomeView` | Recording list with upload, rename, delete |
| `/library` | `LibraryView` | Top-level collections list |
| `/library/collections/:id` | `CollectionView` | Collection detail — items, access rights |
| `/datasets` | `DatasetsView` | Dataset list |
| `/datasets/:id` | `DatasetView` | Dataset detail — items, access rights |
| `/profile` | `ProfileView` | User profile / password change |
| `/login` | `LoginView` | Login form |

## API Modules (`src/api/`)

| File | Backend prefix | Covers |
|---|---|---|
| `recordings.ts` | `/recordings/api/v1/` | List, upload, status, patch (rename/modality), delete |
| `library.ts` | `/api/v1/library/` | Collections, datasets, their items and access rights |
| `annotations.ts` | `/annotations/api/v1/` | Content-type lookup, annotation CRUD |
| `user.ts` | `/api/v1/user/` | Login, logout, me, password change |
| `notifications.ts` | `/api/v1/notifications/` | VAPID key, push subscription |

## Composables (`src/composables/`)

| File | Description |
|---|---|
| `useFileTree.ts` | Normalises all four file/folder input methods (multi-file `<input>`, `webkitdirectory`, drag-and-drop files, drag-and-drop folders) into a unified `UploadTree` structure. Exports `treeFromInputEvent`, `treeFromDropEvent`, `filterByExtension`, `countFiles`, `hasFolders`, `isEmpty`. |

## Reusable Components (`src/components/`)

| Component | Description |
|---|---|
| `AppLogo.vue` | The Epicurrents mark as inline SVG, rendered in the nav brand link. Geometry is a verbatim port of the logo component in the epicurrents.github.io repository; the colouring is not, so the two can be re-synced by copying the paths across. Size is set by the consumer through the `--logo-size` custom property, outline weight through the `strokeWidth` prop. |
| `CollectionPickerDialog.vue` | Dialog for browsing the Collection hierarchy, creating new collections inline, and selecting a target. Controlled via `:open` prop; emits `select` (with a `PickerSelection`) and `close`. Use when any feature needs the user to pick a collection destination or browse library items. Two modes: `collection` (pick a folder) and `item` (pick a recording inside a folder). Breadcrumb navigation via `wa-breadcrumb`. |

`CollectionPickerDialog` usage pattern:

```vue
<collection-picker-dialog
    :open="showPicker"
    mode="collection"
    @select="onPickerSelect"
    @close="showPicker = false"
/>
```

```ts
import CollectionPickerDialog, { type PickerSelection } from '#components/CollectionPickerDialog.vue'

const showPicker = ref(false)

function onPickerSelect(sel: PickerSelection) {
    if (sel.type === 'collection') {
        // sel.collection is Collection | null (null = library root)
    }
    showPicker.value = false
}
```

## Stores (`src/stores/`)

| Store | File | State |
|---|---|---|
| `useAuthStore` | `auth.ts` | Authenticated user, login/logout |
| `useRecordingsStore` | `recordings.ts` | Recording list, upload queue |
| `useLibraryStore` | `library.ts` | Top-level collections and datasets lists |

## Styling

Global layout classes live in [src/style.css](src/style.css). Scoped styles inside components are kept minimal — only component-specific overrides that cannot be shared.

### CSS bundle filename must contain `"epicurrents"`

The Epicurrents viewer (`DefaultInterface`) strips all `<link rel="stylesheet">` tags from the page whose filename does not contain the string `"epicurrents"` when it mounts in non-embedded mode. This is intentional — it was introduced to avoid style conflicts when Epicurrents is hosted inside third-party platforms such as Nextcloud. On this platform the viewer mounts non-embedded, so without intervention the main app CSS bundle would be silently removed the moment the viewer loads.

The Vite config (`vite.config.ts`) works around this via `build.rollupOptions.output.assetFileNames`, which names every CSS output file `epicurrents-platform-[hash].css`. Do not change this naming — if the CSS file no longer matches the `"epicurrents"` substring check the viewer will strip it and all platform styles will disappear on the viewer page.

### WebAwesome version must stay in sync with the viewer UMD

The platform vendors WebAwesome Pro under `node_external/webawesome-<version>/`. The viewer's interface (`frontend/viewer/interface/`) has its own pinned WA dependency in `package.json` and `package-lock.json`. **Both must be kept at the same version.**

When the viewer UMD is rebuilt, the interface build resolves WA from its own `node_modules` (not the workspace root). If the interface's WA drifts ahead of the platform's vendored copy, the `customElements.define` guard in `index.html` will make the platform's older version win — and the viewer's components will silently run against the wrong implementation, causing subtle bugs (e.g. `wa-select` not displaying the selected value).

**To update WebAwesome:**
1. Obtain the new WebAwesome Pro zip and extract it to `node_external/webawesome-<new-version>/`.
2. Update the alias in `vite.config.ts` to point at the new directory.
3. Update `"@awesome.me/webawesome"` in `frontend/viewer/interface/package.json` to the exact new version.
4. Run `npm install --package-lock-only` from `frontend/viewer/interface/` to update its lock file.
5. Rebuild the viewer UMD and the platform frontend (`npm run build`).

The alternative is to set `embedded: true` in the viewer setup in `index.html`. In embedded mode the viewer creates a shadow DOM instead of stripping page styles, which is the cleaner long-term approach but requires auditing platform styles to ensure they still apply correctly inside the shadow root.

Key global classes:

- **Layout**: `.app-nav`, `.page-view`, `.page-view--narrow`, `.page-header`
- **Lists**: `.list-rows`, `.list-row`, `.list-row-main`, `.list-row-name`, `.list-row-meta`, `.list-row-actions`, `.row-badges`
- **Sections**: `.section-header`, `.form-panel`, `.form-actions`
- **Access rights**: `.access-row`, `.access-target`, `.access-perms`, `.perm-row`
- **States**: `.loading-center`, `.empty-state`

## UI Components

The app uses [WebAwesome](https://webawesome.com/) (`wa-*`) web components throughout. The base path is set in `src/main.ts` via `setBasePath`.

Notable usage patterns:

- **Dialogs**: `wa-dialog` with `ref` + `.show()` / `.hide()` for confirmations and forms.
- **Modality picker**: `wa-combobox` on the recording edit dialog (EEG, EMG, ECG, EOG, MEG, ECoG, sEEG).
- **Permission checkboxes**: Native `<input type="checkbox">` inside `<label class="perm-row">` — `wa-checkbox` is avoided because its `checked` property does not bind reliably with `v-model`.

### Design conventions

- **Colors**: always use WebAwesome semantic color tokens — never static palette steps (`--wa-color-neutral-100` etc.). Semantic tokens adapt automatically to light/dark mode. Available families: `neutral`, `brand`, `danger`, `success`, `warning`. Roles: `fill-quiet/normal/loud` (backgrounds), `border-quiet/normal/loud` (borders), `on-quiet/normal/loud` (text on those fills), `text-normal/quiet` (body text), `surface-default/raised/lowered/border` (page surfaces).
- **Inline SVG**: colour it from a scoped `<style>` block, not from `fill=` / `stroke=` presentation attributes — those do not accept `var()`, so a token written there silently resolves to nothing. Put a class on the element (or on a `<g>`, since `fill` and `stroke` inherit) and set the property in CSS. [src/components/AppLogo.vue](src/components/AppLogo.vue) is the worked example: `currentColor` for the outlines, `--wa-color-brand-on-quiet` and `--wa-color-brand-border-loud` for the two blues, which sit on opposite sides of the brand scale and swap places between light and dark mode.
- **Buttons**: the default is `variant="brand" appearance="filled-outlined"`. Either attribute can be omitted to create visual distinction — e.g. drop `appearance` on the single primary CTA to make it stand out, or drop `variant` on a secondary/cancel button to de-emphasise it. Destructive actions use `variant="danger"`. Navigation / lowest-emphasis links use `variant="text"`. Do not use the standalone `outline` boolean attribute — `filled-outlined` already provides the border.
- **Dropdown menus** (`wa-dropdown` + `wa-dropdown-item`): use for row and page-level context actions to allow future extension. Trigger button uses `appearance="plain"`. Destructive items (delete, remove) use `variant="danger"`. Icon slot in dropdown items is `slot="icon"`; icon slot in buttons is `slot="start"`.
- **Overlay close events — common pitfall**: always use `@wa-hide.self` and `@wa-after-hide.self` (not `@wa-hide` / `@wa-after-hide`) on `wa-dialog` and `wa-drawer`. Child components inside the overlay (`wa-combobox`, `wa-dropdown`, `wa-tooltip`, etc.) fire their own `wa-hide` and `wa-after-hide` events when they close; these bubble up and trigger the overlay's handler, closing it prematurely. The `.self` modifier restricts the handler to events whose `target` is the overlay element itself. This applies to every handler on any overlay-type component — add `.self` as a matter of course.
- **Closing a `wa-dialog` from a button**: add `data-dialog="close"` to the button element — the dialog handles the click natively, plays the hide animation, and fires `wa-hide`. Do not call `.hide()` on the element ref (the method is internal and may not be accessible) and do not emit `close` directly from the button (bypasses the animation).
- **Form components** (`wa-input`, `wa-textarea`, `wa-combobox`, `wa-file-input`): always include `size="small"` to keep the UI compact.
- **Icons**: all icons are imported as raw SVGs in [`src/icons.ts`](src/icons.ts) and registered via `registerIconLibrary` in `src/main.ts`. To add a new icon, import it from `@fa/svgs/regular/<name>.svg?raw` and add it to the map. Icon names follow FontAwesome conventions (e.g. `xmark`, `plus`, `file-music`) — do not use Bootstrap icon names.

### Vue template formatting

- Place each tag on its own line; do not place nested tags on the same line.
- Indent nested tags by one level (4 spaces).
- Opening and closing tags with short text content may be written on one line, but the line must stay within 120 characters.
- At most 3 attributes may appear on one line with the opening/closing tags when the line stays short; otherwise, put attributes on separate lines.
- If attributes are on separate lines, place the opening tag closing `>` on its own line at the same indent level as the opening `<tag` line.
- Exception: empty tags (no content) may use `></tag>` on the same line.
- Keep `v-if`, `v-else-if`, `v-else`, `v-for`, and `:key` on the same line as the opening tag.
- Sort attributes alphabetically; treat `v-` attributes (except the directive exceptions above) after regular attributes, and place `@` event attributes last.
- Avoid inline styles; use dedicated CSS classes.
- Route all user-facing template text through `t(key, SCOPE, params?)`. Use the visible English string itself as the key/fallback text.

### JavaScript conventions

- Never use `var`. Use `const` by default and use `let` only when reassignment is required.
- Do not use single-line control flow blocks. Always write `if`/`else`, `for`, `while`, `try`/`catch`/`finally`, etc. with braces and place the body on separate lines.
- Keep short ternary assignments on one line. For longer ternary assignments, use multiline formatting with `?` and `:` on their own indented lines.
- Do not add semicolons unless they are required for correctness (for example, when a line starts with `[` and could otherwise be parsed as part of the previous statement).
- Do not place inline JavaScript expressions in templates for behaviour. Template event handlers should call named methods/functions.
- Omit explicit return types on function declarations when the return type is obvious from the implementation (for example: `void`, `string`, `boolean`, and straightforward async `Promise<void>` handlers).

## De-identification

Recording integer PKs are never returned by the API and must not appear in frontend code. All recording references use the 32-character hex hash (`Recording.hash` = first segment of `stored_name`). `CollectionItem.object_hash` and `object_name` are resolved server-side so the frontend never needs to resolve a PK to a display name.

## Folder uploads + PHI

Folder names on a clinician's workstation routinely embed patient identifiers — site IDs, study codes, subject initials, dates of birth. When the user picks a folder via the UploadView "Upload folder" affordance, the on-disk folder names are present in the browser's `FileList` via `webkitRelativePath`. Letting those strings flow through to the server and become persisted `Collection.name` rows is a PHI-leak vector with the same blast radius as `Recording.original_name`.

### Policy

**Flat by default.** `useRecordingsStore.uploadTree` collects every file in the selected tree and attaches each one directly to the target collection. No new `Collection` rows are created from folder uploads on this path. The folder names exist only in the browser's memory during the upload session and never leave it. Filename collisions are resolved with a ` (N)` suffix before the extension via `computeDedupedNames` in [src/composables/useFileTree.ts](src/composables/useFileTree.ts).

**Hierarchy is opt-in.** UploadView shows a "Preserve folder hierarchy as subcollections" checkbox in the preview stage *only when* the upload contains at least one folder. Default state is off. When the user checks it, a `wa-callout variant="warning"` appears immediately below with explicit instruction that the folder names will be visible as collection names to anyone with access. The store then takes the hierarchy-preserving code path (`_uploadFolder`), which creates a `Collection` row per folder using the on-disk name as the `Collection.name`.

### Why opt-in, not opt-out

The clinician's mental model of "I'm uploading this folder" includes the folder structure they see in their file browser. Removing it silently — even when safe — risks the user losing track of which file belongs to which study. Inverting the default to opt-in is the safer compromise: the user has to make an explicit choice to expose folder names, with a visible warning at the moment of decision. The cost is one extra click for the genuinely-non-PHI use cases (e.g. uploading public datasets); the benefit is that the most common clinical workflow defaults to safe.

### Audit pointers

- Policy enforcement: [src/stores/recordings.ts](src/stores/recordings.ts) `uploadTree` — the `preserveFolderHierarchy` branch is the only path that calls `_uploadFolder`. Any future change that lifts a `createCollection` call out of that branch needs to be flagged here.
- UI surface: [src/views/UploadView.vue](src/views/UploadView.vue) — the checkbox + warning callout are rendered behind a `v-if="willCreateCollections"` gate and `v-if="input.preserveFolderHierarchy"` for the warning. Both default-off; the warning is unconditional when checked.
- Dedup logic: [src/composables/useFileTree.ts](src/composables/useFileTree.ts) `computeDedupedNames`, covered by unit tests in [src/composables/useFileTree.test.ts](src/composables/useFileTree.test.ts).

The Collection-name PHI vector is the only known leak path tied to folder uploads; the file *contents* are separately sanitised by the EDF/BDF header-rewriting pipeline (see [recordings/processors/edf.py](../recordings/processors/edf.py) and `recordings/README.md`).
