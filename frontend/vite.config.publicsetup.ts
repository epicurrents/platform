import { defineConfig } from 'vite'
import { fileURLToPath, URL } from 'url'

/*
 * Build for the public viewer's platform-owned setup.
 *
 * `src/viewer/publicSetup.ts` is the one piece of platform JavaScript the public viewer page
 * loads. It is built separately rather than as part of the SPA bundle because the page references
 * it by a fixed URL written into `epicurrents.views._PUBLIC_SETUP_SCRIPT`, and the SPA build emits
 * content-hashed names the template cannot know.
 *
 * IIFE, not the library default: the page loads it with a plain `<script src>` (it must run before
 * `createEpicurrentsApp()`, and a module script defers past that point).
 *
 * `emptyOutDir: false` is load-bearing. The output directory holds the per-project viewer builds,
 * which this build must not clear — it runs after them, as the last step of `build:viewer`.
 */
const abs = (p: string) => fileURLToPath(new URL(p, import.meta.url))

export default defineConfig({
    mode: 'production',
    // The setup module is self-contained; none of the SPA's public/ assets belong beside it.
    publicDir: false,
    build: {
        lib: {
            entry: abs('./src/viewer/publicSetup.ts'),
            name: 'EpicurrentsPublicSetup',
            formats: ['iife'],
            fileName: () => 'epicurrents-public-setup.js',
        },
        minify: 'esbuild',
        outDir: abs('./viewer-dist'),
        emptyOutDir: false,
        target: 'esnext',
    },
})
