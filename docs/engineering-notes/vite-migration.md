# Viewer — webpack → Vite migration plan

`core` migrated to Vite on 2026-08-20; the other 16 packages under `frontend/viewer/epicurrents/`
still build with webpack + ts-loader.
The webpack setup works but carries several liabilities: declaration files leaked into `dist/`
during the UMD pass (fixed with `transpileOnly: true` but still inelegant), `tsconfig-replace-paths`
is a fragile post-processing step that creates a corruption window between `tsc` and path
replacement, and `libraryTarget: 'umd'` is the legacy module format — modern bundlers prefer
ESM with named exports.

Vite's library mode (powered by Rollup) eliminates all three issues: esbuild handles
transpilation without declaration emit, the `#`-aliases are resolved at bundle time via
`resolve.alias`, and Rollup natively emits dual `dist/index.js` (ESM) + `dist/index.cjs` (CJS)
output. The `build:tsc` step (`tsc --noEmit` for type-checking + declaration emit only) remains
unchanged; the webpack bundle is simply replaced by `vite build`.

## Shared config design

Create `frontend/viewer/epicurrents/vite.base.ts` (imported by each package, not extended):

```ts
// epicurrents/vite.base.ts
import { defineConfig, type UserConfig } from 'vite'
import dts from 'vite-plugin-dts'
import path from 'path'

export function baseLibConfig(
    packageDir: string,
    libraryName: string,
    extraAlias: Record<string, string> = {}
): UserConfig {
    return defineConfig({
        build: {
            lib: {
                entry: path.resolve(packageDir, 'src/index.ts'),
                name: libraryName,
                formats: ['es', 'cjs'],
                fileName: (format) => format === 'es' ? 'index.js' : 'index.cjs',
            },
            outDir: 'umd',            // keep same output dir so copy.mjs needs no change
            sourcemap: true,
            rollupOptions: { external: [] },
        },
        resolve: {
            alias: {
                '#root': packageDir,
                '#': path.resolve(packageDir, 'src'),
                ...extraAlias,
            },
            preserveSymlinks: true,
        },
    })
}
```

Each package's `vite.config.ts` then becomes minimal:

```ts
// epicurrents/wav-reader/vite.config.ts
import { baseLibConfig } from '../vite.base'
export default baseLibConfig(__dirname, 'EpicWavReader')
```

Packages with workers or non-standard aliases pass extra options:

```ts
// epicurrents/edf-reader/vite.config.ts
import { baseLibConfig } from '../vite.base'
export default baseLibConfig(__dirname, 'EpiCEdfReader', {
    '#edf': path.resolve(__dirname, 'src/edf'),
    '#util': path.resolve(__dirname, 'src/util'),
})
```

## Worker bundles

Vite's `?worker` syntax cannot be used in library mode (it targets browser app builds).
Workers must remain as separate Rollup entry points:

```ts
rollupOptions: {
    input: {
        'edf.worker': path.resolve(__dirname, 'src/workers/edf.worker.ts'),
    },
}
```

The output workers land in `umd/` alongside the main bundle and are picked up by
`scripts/copy.mjs` without any change.

For `pdf-reader` (pdfjs.worker) and `pyodide-service` (pyodide.worker + Python `?raw` imports),
add Rollup plugin `@rollup/plugin-url` for binary/raw-text assets that currently use webpack's
`asset/source` loader.

## `dts` plugin

`vite-plugin-dts` generates declaration files into `dist/` from the same TypeScript source,
replacing the `tsc` declaration-emit step. The `tsconfig-replace-paths` post-processing step
is no longer needed because Rollup resolves `#`-aliases at bundle time; the emitted `.d.ts`
files will use relative paths natively.

Update `build:tsc` in each migrated package to:
```
"build:tsc": "tsc --noEmit"   # type-check only; dts plugin handles declarations
```

## Migration order (easiest → most complex)

**Batch 1 — no workers, no special assets** (start here, establish the pattern)

| Package | Notes |
|---|---|
| `wav-reader` | Minimal: one entry, no workers |
| `htm-reader` | Minimal; uses `marked` for Markdown |
| `doc-module` | No workers; has JSON copy step |
| `tab-module` | No workers; has JSON copy step |
| `emg-module` | No workers; has JSON copy step |
| `ncs-module` | No workers; has JSON copy step |
| `acc-module` | No workers; added after this plan was written |

**Batch 2 — single worker**

| Package | Notes |
|---|---|
| `api-reader` | One worker; straightforward |
| `dicom-reader` | One worker; depends on `cornerstone` |
| `eeg-module` | One worker; has Python scripts via `?raw`-equivalent |
| `csv-reader` | One worker; added after this plan was written |
| `nic-reader` | One worker; added after this plan was written |

**Batch 3 — more complex assets or core**

| Package | Notes |
|---|---|
| `edf-reader` | One worker; the worker is the critical path — test thoroughly |
| `onnx-service` | One worker; ONNX runtime is a large binary |
| ~~`core`~~ | **Done 2026-08-20.** Config is `core/vite.shared.mjs`, not the workspace-level `vite.base.ts` this plan proposed, and `vite-plugin-dts` was not adopted — `build:types` still runs `tsc --emitDeclarationOnly` plus `tsconfig-replace-paths`, so the section below claiming that step becomes unnecessary is unrealised. |
| `pdf-reader` | Two entry points (lib + pdfjs.worker); pdfjs binary assets |
| `pyodide-service` | Most complex: Python `?raw` imports, Pyodide CDN bootstrap, two worker entry points |

## Considerations per conversion

- **`module` field in `package.json`**: add `"module": "dist/index.js"` (ESM entry) alongside
  `"main": "dist/index.cjs"` (CJS entry). Update `"exports"` to expose both.
- **`moduleResolution`**: switch `tsconfig.base.json` from `"node"` to `"bundler"` once all
  packages are on Vite. `"bundler"` is the correct setting for Vite/esbuild and eliminates the
  TypeScript 5.x deprecation warning about `module: esnext` + `moduleResolution: node`.
- **Python `?raw` imports in `eeg-module` / `pyodide-service`**: webpack uses `asset/source`;
  Vite uses `?raw` query suffix. The `?raw` approach is cleaner; update the import sites.
- **`isomorphic-fetch` in `pyodide-service`**: webpack needed an alias because the bundler
  ran in Node context. Vite library builds target browsers by default so this alias can be
  dropped; add `build.target: 'esnext'` and let the browser's native `fetch` handle it.
- **JSON imports in doc/tab/emg/ncs-module**: webpack copied JSON config files with a custom
  script; Rollup handles `import config from './config.json'` natively, so the `copy:json`
  build script can be removed.
- **`scripts/copy.mjs`**: update `workerPaths` to read from `umd/` (Vite output) OR change
  Vite's `outDir` to `umd/` (already shown above) so the script needs no change.
- **Sourcemaps**: with Vite, enable `build.sourcemap: true` in the base config. The worker
  scripts should also emit sourcemaps. Update `tsconfig.base.json` to `"sourceMap": true`
  at the same time (currently false everywhere).

## Cleanup after migration

Remove from devDependencies across all packages:
- `webpack`, `webpack-cli`, `webpack-bundle-analyzer`, `webpack-dev-server`, `webpack-merge`
- `ts-loader`, `dotenv-webpack`
- `tsconfig-replace-paths`

Add to devDependencies:
- `vite` (workspace root, single version)
- `vite-plugin-dts` (workspace root)
- `@rollup/plugin-url` (packages with binary/raw assets)
