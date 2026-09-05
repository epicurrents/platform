import { defineConfig } from 'vite'
import { fileURLToPath, URL } from 'url'

/*
 * Build for the public viewer's lead-field provider.
 *
 * `src/viewer/publicLeadFields.ts` is the one piece of platform JavaScript the public viewer page
 * loads. It is built separately rather than as part of the SPA bundle because the page references
 * it by a fixed URL written into `epicurrents.views._PUBLIC_VIEWER_TEMPLATE`, and the SPA build
 * emits content-hashed names the template cannot know.
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
    // The provider is self-contained; none of the SPA's public/ assets belong beside it.
    publicDir: false,
    build: {
        lib: {
            entry: abs('./src/viewer/publicLeadFields.ts'),
            name: 'EpicurrentsLeadFields',
            formats: ['iife'],
            fileName: () => 'epicurrents-leadfields.js',
        },
        minify: 'esbuild',
        outDir: abs('./viewer-dist'),
        emptyOutDir: false,
        target: 'esnext',
    },
})
