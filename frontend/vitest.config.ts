import { existsSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { defineConfig, configDefaults } from 'vitest/config'
import vue from '@vitejs/plugin-vue'
import tsconfigPaths from 'vite-tsconfig-paths'
import { buildAliases } from './build-aliases'

/*
 * Some specs import viewer code (`scoped-event-log`, `#epicurrents/*`), which the
 * `#`/bare aliases resolve into `frontend/viewer/**`. Those files are BUILD OUTPUT:
 * the viewer submodule is the Epicurrents *builder*, whose packages and `dist/`
 * directories are git-ignored and only appear after `npm run setup` inside it. So a
 * plain checkout — notably CI — has the submodule but none of the built artifacts,
 * and those specs cannot even be loaded there.
 *
 * Rather than fail (or silently pass) depending on the machine, detect the built
 * viewer and skip only the specs that need it, loudly. A full local tree runs the
 * whole suite; CI runs everything that does not require the viewer build.
 */
const builtViewer = fileURLToPath(new URL('./viewer/util/scoped-event-log/dist/index.js', import.meta.url))
const hasBuiltViewer = existsSync(builtViewer)

// Specs that import viewer build output, directly or transitively. None in the
// platform today; a project whose specs do belongs on this list.
const viewerDependentTests: string[] = []

if (!hasBuiltViewer) {
    console.warn(
        '\n[vitest] Built viewer not found at frontend/viewer/util/scoped-event-log/dist/.\n' +
            `[vitest] SKIPPING ${viewerDependentTests.length} viewer-dependent spec file(s): ` +
            `${viewerDependentTests.join(', ')}.\n` +
            '[vitest] Run `npm run setup` in frontend/viewer to build it and include them.\n'
    )
}

export default defineConfig({
    plugins: [
        // Resolve #-prefixed path aliases from tsconfig.app.json in tests.
        tsconfigPaths({ projects: ['./tsconfig.app.json'] }),
        vue(),
    ],
    // The active project's specs live above this directory, so vitest has to be
    // allowed to serve from there. vite.config.ts sets the same thing for the
    // app build; this is a separate config and does not inherit it.
    server: { fs: { allow: ['..'] } },
    // Shared with vite.config.ts. tsconfigPaths covers the `#` specifiers that
    // tsconfig maps, but not a bare one like `scoped-event-log` reached from a
    // file outside the tsconfig project — which every project spec now is.
    resolve: { alias: buildAliases(process.env.VITE_PROJECT) },
    test: {
        // jsdom, not node: this is a browser SPA and app code reaches for browser
        // globals — e.g. stores/auth.ts calls `window.__EPICURRENTS__?…`, which
        // throws ReferenceError under the node environment even though the
        // optional chaining makes it safe in a real browser.
        environment: 'jsdom',
        // Run each test file in its own module scope so vi.mock() calls do not
        // bleed between files.
        isolate: true,
        // Glob for test files. The second pattern reaches the active project,
        // whose frontend lives outside src/ because the project is its own
        // repository checked out at projects/<name>/. Without it a project's
        // specs are silently not collected — they do not fail, they vanish.
        include: ['src/**/*.{test,spec}.ts', '../projects/*/frontend/**/*.{test,spec}.ts'],
        // configDefaults.exclude carries '**/node_modules/**', but those
        // patterns are resolved against the vitest root and so do not reach
        // the project trees above it. The projects' node_modules are symlinks
        // into the platform's, so without this the glob walks the whole
        // dependency tree and collects third-party specs.
        exclude: [
            ...configDefaults.exclude,
            '../projects/*/frontend/node_modules/**',
            ...(hasBuiltViewer ? [] : viewerDependentTests),
        ],
    },
})
