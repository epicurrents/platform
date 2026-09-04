# check-frontend-imports

Audit the frontend for missing icon registrations and missing WebAwesome component imports, then fix any gaps found.

## Context

The frontend (`frontend/src/`) uses two manually-maintained registries that must be kept in sync with template usage:

1. **`frontend/src/icons.ts`** — every `<wa-icon name="foo">` used in any `.vue` file must have a corresponding `import fooIcon from '@fa/svgs/regular/foo.svg?raw'` and an entry in the `icons` export map. Icon name → file name is 1:1 for regular icons (the icon name IS the filename, e.g. `arrow-left` → `arrow-left.svg`).

2. **`frontend/src/main.ts`** — every `<wa-*>` custom element used in any `.vue` file must be imported here. The import path pattern is:
   `import '@awesome.me/webawesome/dist/components/<component-name>/<component-name>.js'`
   where `<component-name>` is the tag name minus the `wa-` prefix, e.g. `<wa-badge>` → `badge/badge.js`.

## Steps

1. **Collect all icon names used** — grep all `.vue` files under `frontend/src/` for `<wa-icon` with a `name=` attribute (both static `name="foo"` and bound `:name="expr"` forms). For bound forms note the possible string values from the surrounding context (e.g. a `phaseIcon()` switch statement).

2. **Collect registered icon names** — read `frontend/src/icons.ts` and list the keys in the `icons` export map.

3. **Diff** — report any icon names from step 1 that are absent from step 2.

4. **Collect all wa-* tags used** — grep all `.vue` files under `frontend/src/` for `<wa-` and collect the unique tag names (e.g. `wa-badge`, `wa-tree-item`).

5. **Collect registered WA components** — read `frontend/src/main.ts` and list the imported component paths.

6. **Diff** — report any `wa-*` tags from step 4 whose corresponding `<component-name>/<component-name>.js` path is absent from the imports in step 5.

7. **Fix all gaps** — for missing icons: add the import and the map entry to `icons.ts` in alphabetical order. For missing WA components: add the import to `main.ts` in alphabetical order alongside the existing component imports.

Report what was added (or confirm everything is already in sync if no gaps are found).