# update-viewer-docs

Review recent changes to the Epicurrents viewer library and update the documentation site at `docs/epicurrents/src/docs/latest/` to reflect them.

## When this skill applies

Invoke this skill after any viewer library change that touches:
- **Public API** — new or renamed exports, changed method signatures, new static properties
- **User-visible behaviour** — how annotations, events, montages, or settings work in the UI
- **Core concepts** — anything described in the conceptual documentation (signal data flow, module/reader/service patterns, locking semantics, etc.)
- **New features** — anything a developer embedding the library or a user of the viewer would need to know about

Do **not** invoke for internal refactors, test changes, or bug fixes that restore already-documented behaviour.

## Docs site location

`docs/epicurrents/src/docs/latest/`, relative to the platform repository root.

The docs site is a submodule of this repository (`docs/epicurrents` → `epicurrents/epicurrents.github.io`). **Edit that checkout, not a standalone clone of the same repository elsewhere on the machine** — the submodule is the copy that is kept current, and it is the one the platform pins. A sibling clone may exist and be stale; writing to it produces changes that look applied but are not part of what this repository builds against.

Because it is a submodule, changes are committed *inside* `docs/epicurrents/` first; the platform repository then records the new commit pointer as a change to `docs/epicurrents` and needs its own commit.

The dev server runs from `docs/epicurrents/` at `http://localhost:5174/` (or the next available port). Navigation is declared in `src/router.ts` — new pages must be added there to appear in the sidebar.

## Page → concept mapping

Use this table to identify which pages are affected by a given change.

| Changed area | Pages to check |
|---|---|
| `GenericAnnotation`, `GenericBiosignalEvent`, `ResourceLabel` | `annotations.md` |
| `GenericBiosignalResource` (events, labels, locking) | `annotations.md` |
| `EegEvent.CODED_EVENTS`, `EegEvent.PRIORITY` | `annotations.md` (coded annotations section) |
| `GenericBiosignalEvent.PRIORITY` | `annotations.md` (event classes section), `library-structure.md` (Annotations cross-ref) |
| `GenericSignalReader`, `EdfReader`, `EdfDecoder` | `edf-reader.md` |
| `DicomReader` | `dicom-reader.md` |
| `EegRecording`, `EegMontage`, `EegSetup`, `EegStudyLoader` | `eeg-module.md`, `eeg-module/supported-file-types.md` |
| `GenericBiosignalMontage`, montage switching, filter API | `eeg-module/eeg-viewer.md` |
| Analysis tools (FFT, PSD, topomap, examination) | `eeg-module/analysis-tools.md` |
| `Epicurrents` app class, `registerModule`, `registerService`, lifecycle | `implementation.md` |
| `ViewerPlugin`, `waitForEventBus`, event bus API | `getting-started/platform-integration.md` |
| Package additions/removals, `GenericAsset`/`GenericResource` base classes | `library-structure.md` |
| Signal data flow (SAB path, BiosignalMutex, BiosignalCache) | `library-structure.md` (Signal data flow section) |
| Reader/module/service patterns | `library-structure.md` (Reader/Module/Service pattern sections) |
| Build system, `tsconfig-replace-paths`, monorepo structure | `development.md` |
| Vitest setup, `package.json imports`, test configuration | `development.md` (Testing section) |
| `GenericAsset.configure`, known bugs | `development.md` (Known issues section) |
| Settings (`SETTINGS`, `INTERFACE` singleton) | `implementation.md`, `library-structure.md` |
| User interface components (`EegViewer`, `EegNavigator`, `EegControls`) | `eeg-module/eeg-viewer.md` |
| New file format support | `edf-reader.md` or `dicom-reader.md` or new page |
| Platform-specific viewer integration (ViewerView, project plugins) | `getting-started/platform-integration.md`, `platform/` section |

## Steps

1. **Identify changed files** — review the diff (or conversation context) to list every source file that changed.

2. **Map to docs pages** — use the table above to produce a list of pages that may need updating. If a changed concept spans multiple pages, check all of them.

3. **Read affected pages** — read each candidate page in full. For each section, ask: does this still accurately describe the current implementation?

4. **Check for omissions** — ask whether the change introduces something new that is not yet documented anywhere. New public API, new configuration options, new behaviour, and new limitations all require at minimum a mention.

5. **Update** — edit the affected pages. Keep the same tone and heading style as the surrounding text. Follow the conventions in the existing file (e.g. `[[toc]]` at the top, `##` as the primary heading level, links use `docs/` prefix).

6. **Check the router** — if a new page was added, verify it has an entry in `docs/epicurrents/src/router.ts`. New sub-pages must also appear as `subitems` under their parent.

7. **Roadmap items** — if the change is a partial implementation or leaves known gaps, add a `> **Planned:** ...` blockquote callout in the relevant section so users know what is coming.

## Conventions

- **Tone** — practical and direct. Explain what something does and when to use it. Avoid marketing language.
- **Code examples** — use TypeScript. Keep them minimal but self-contained.
- **Cross-references** — link to related pages with `[text](docs/path)` (the router prefixes the hash automatically).
- **Properties tables** — use the `| Property | Type | Description |` format used in `annotations.md`.
- **"Planned" callouts** — for unimplemented features that are documented by design: `> **Planned:** description.`
- Do not add emojis.
