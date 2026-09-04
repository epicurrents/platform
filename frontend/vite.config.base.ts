import { defineConfig, loadEnv } from 'vite'
import { existsSync } from 'node:fs'
import { fileURLToPath, URL } from 'url'
import vue from '@vitejs/plugin-vue'
import { viteSingleFile } from 'vite-plugin-singlefile'

/*
 * Platform-side base viewer build (PROTOTYPE).
 *
 * Bundles the platform-owned setup (`src/viewer/base.ts`) into a single
 * `viewer-dist/<project>/epicurrents-lib.{js,css}` — the same artifact the
 * viewer's own `build:lib:base` produces, but built from the PLATFORM so the
 * setup file lives here, not in the viewer repo. This is the "single build from
 * the frontend" prototype: it consumes the interface and its UI module from
 * SOURCE, so changing interface source needs only this build, not a separate
 * viewer build. It still consumes the @epicurrents/* MODULE packages from their
 * built `dist/` (same as the viewer's own base build does), so a change to core /
 * eeg-module / edf-reader / dicom-reader still needs that package rebuilt first.
 *
 * Per-project overlay: `VITE_PROJECT` (mirroring the SPA's build-time project
 * selection in `src/projects/active.ts`) picks the active project. When a project
 * ships `src/projects/<project>/viewer.ts`, `@viewer-overlay` resolves to it and
 * its modules are bundled in and registered after the base set; otherwise the
 * no-op `src/viewer/overlays/none.ts` is used. Keeping the overlay inside the
 * project folder means removing `projects/<project>/` extracts it wholesale — the
 * platform config here carries no project name. The output lands in
 * `viewer-dist/<project>/` (or `viewer-dist/base/` when no project is active), so
 * a project's extra readers ship only with that project's viewer and stay out of
 * the generic base build.
 *
 * The interface's internal `#*` / `@/` aliases are declared explicitly here
 * rather than via tsconfigPaths, because the platform's own tsconfig maps `#*`
 * to the platform's `src/*` — pulling that in would misresolve the bundled
 * interface source. This config is never merged into the SPA build (vite.config.ts).
 */
const abs = (p: string) => fileURLToPath(new URL(p, import.meta.url))

export default defineConfig(({ mode }) => {
    const env = loadEnv(mode, process.cwd(), '')
    // The active project (mirrors the SPA's VITE_PROJECT selection). A project
    // supplies its viewer overlay as `projects/<project>/frontend/viewer.ts`,
    // outside this directory because the project is its own repository checked
    // out alongside the platform; absent that file (or with no project set) the
    // base build uses the no-op stub.
    const project = env.VITE_PROJECT || ''
    const overlaySource = project ? abs(`../projects/${project}/frontend/viewer.ts`) : ''
    const overlayPath = overlaySource && existsSync(overlaySource)
        ? overlaySource
        : abs('./src/viewer/overlays/none.ts')
    // Output segment and public path: per-project so `viewer-dist/<project>/` and
    // `viewer-dist/base/` can coexist, served at `/viewer/<segment>/`.
    const segment = project || 'base'
    const publicPath = `/viewer/${segment}/`
    return {
        base: publicPath,
        mode: 'production',
        // The viewer lib is self-contained — don't copy the SPA's public/ assets
        // (favicon, manifest, push-sw.js, …) into the lib output directory.
        publicDir: false,
        build: {
            lib: {
                entry: abs('./src/viewer/base.ts'),
                name: 'Epicurrents',
                fileName: 'epicurrents-lib',
            },
            minify: 'esbuild',
            outDir: abs(`./viewer-dist/${segment}`),
            emptyOutDir: true,
            target: 'esnext',
        },
        esbuild: {
            supported: {
                'top-level-await': true,
            },
            keepNames: true,
        },
        optimizeDeps: {
            esbuildOptions: {
                target: 'esnext',
                keepNames: true,
            },
        },
        // Classic worker format: the reader packages' worker bundles are UMD and use
        // importScripts, which a module worker cannot call.
        worker: {
            format: 'iife',
        },
        plugins: [
            vue({
                template: {
                    compilerOptions: {
                        isCustomElement: ((tag) => {
                            return tag === 'log-inspector' || tag.startsWith('wa-')
                        }),
                    },
                },
            }),
            viteSingleFile(),
        ],
        define: {
            __INTLIFY_JIT_COMPILATION__: true,
            'process.env.ASSET_PATH': JSON.stringify(publicPath),
            'process.env.NODE_ENV': JSON.stringify('production'),
        },
        resolve: {
            // Order matters: the alias resolver matches in sequence, first hit wins.
            //
            // The interface framework + its eeg UI module resolve to viewer SOURCE.
            // The @epicurrents/* module packages are NOT dir-aliased — that would
            // shadow their `exports` subpaths (e.g. `@epicurrents/core/util`), which
            // the bundled interface source relies on. Instead:
            //   - interface source (under viewer/) resolves those packages naturally
            //     via viewer/node_modules, honouring each package's exports map;
            //   - the out-of-tree platform base.ts gets targeted aliases only for
            //     what it imports directly — the public `./workers/*` (→ each
            //     package's self-contained umd bundle, keeping any `?raw` query),
            //     `./util`, and the bare package roots.
            alias: [
                // The active project's overlay (or the no-op stub). Must precede the
                // interface `#*` / `@/` aliases so the exact specifier wins.
                { find: '@viewer-overlay', replacement: overlayPath },
                { find: '@epicurrents/interface/modules/eeg', replacement: abs('./viewer/interface/src/app/modules/eeg/index.ts') },
                { find: '@epicurrents/interface', replacement: abs('./viewer/interface/src/setups/index.ts') },
                { find: /^@epicurrents\/(core|edf-reader|dicom-reader|nic-reader)\/workers\/(.*)$/, replacement: abs('./viewer/epicurrents/') + '$1/umd/$2' },
                { find: /^@epicurrents\/core\/util$/, replacement: abs('./viewer/epicurrents/core/dist/util/index.js') },
                { find: /^@epicurrents\/eeg-module$/, replacement: abs('./viewer/epicurrents/eeg-module/dist/index.js') },
                { find: /^@epicurrents\/edf-reader$/, replacement: abs('./viewer/epicurrents/edf-reader/dist/index.js') },
                { find: /^@epicurrents\/nic-reader$/, replacement: abs('./viewer/epicurrents/nic-reader/dist/index.js') },
                { find: /^@epicurrents\/dicom-reader$/, replacement: abs('./viewer/epicurrents/dicom-reader/dist/index.js') },
                { find: /^scoped-event-log$/, replacement: abs('./viewer/util/scoped-event-log/dist/index.js') },
                // Interface-internal path aliases (it bundles from source here).
                { find: /^#root\/(.*)/, replacement: abs('./viewer/interface/$1') },
                { find: /^#workspace\/(.*)/, replacement: abs('./viewer/$1') },
                { find: /^#(.*)/, replacement: abs('./viewer/interface/src/$1') },
                { find: /^@\/(.*)/, replacement: abs('./viewer/interface/src/$1') },
                { find: 'node-fetch', replacement: 'isomorphic-fetch' },
                { find: 'stream', replacement: 'stream-browserify' },
            ],
            preserveSymlinks: true,
        },
    }
})
