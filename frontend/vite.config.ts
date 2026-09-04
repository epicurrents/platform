import { defineConfig, loadEnv, type PluginOption } from 'vite'
import { buildAliases } from './build-aliases'
import vue from '@vitejs/plugin-vue'
import crossOriginIsolation from 'vite-plugin-cross-origin-isolation'
import tsconfigPaths from 'vite-tsconfig-paths'
import { VitePWA } from 'vite-plugin-pwa'

// https://vite.dev/config/
/**
 * Vite configuration.
 * - VITE_BACKEND_URL=mock   (in-memory mock API, no real backend required)
 * - VITE_BACKEND_URL=http://localhost:8000   (proxy to a local Django dev server)
 *
 * The service worker (precache + web push, one worker) is built for every real
 * bundle and disabled on the dev server via devOptions; see the VitePWA block.
 */
export default defineConfig(({ mode }) => {
    const env = loadEnv(mode, process.cwd(), '')
    const useMock = env.VITE_BACKEND_URL === 'mock'
    const plugins = [
        tsconfigPaths({ projects: ['./tsconfig.app.json'] }),
        vue({
            template: {
                compilerOptions: {
                    isCustomElement: ((tag) => {
                        return tag === 'log-inspector' || tag.startsWith('wa-')
                    })
                }
            }
        }),
    ] as PluginOption[]
    // Use cross-origin isolation in development to enable SharedArrayBuffer support.
    if (env.NODE_ENV === 'development') {
        plugins.push(
            crossOriginIsolation()
        )
    }
    // Single service worker for the whole app: the generated worker precaches the
    // built shell AND pulls in the web-push handlers via importScripts, so there is
    // exactly one worker owning the root scope (no push-sw.js vs sw.js race). It is
    // built for every real bundle; `devOptions.enabled: false` keeps it off the Vite
    // dev server so HMR is untouched. Registration is manual (injectRegister: false)
    // in src/main.ts. Singlefile builds are retired, so the worker no longer needs to be
    // opt-in.
    //
    // A consequence worth knowing before diagnosing anything else: because the update
    // strategy below is `prompt` and not `autoUpdate`, a client that already has the app
    // keeps serving the previous shell out of the precache until the reload is accepted.
    // Redeploying does not move it and neither does a plain refresh, because the request
    // never reaches the server — the access log then shows API calls with no matching
    // fetch of `/` or `/assets/*`, and the symptom is an old bundle talking to a new
    // backend. Clearing site data is the way out.
    plugins.push(
        VitePWA({
            // 'prompt', not 'autoUpdate': a clinical viewer must not reload out from under
            // an analysis in progress. A new SW installs and WAITS; the app surfaces a
            // persistent "new version — reload" toast (main.ts onNeedRefresh → showToast with
            // a Reload action) and only activates + reloads when the user accepts. So NO
            // skipWaiting/clientsClaim here — those would take over immediately, defeating the prompt.
            registerType: 'prompt',
            injectRegister: false,
            includeAssets: ['favicon.svg', 'favicon.ico', 'robots.txt', 'apple-touch-icon.png'],
            devOptions: { enabled: false },
            workbox: {
                cleanupOutdatedCaches: true,
                // Merge the web-push logic into the generated worker (public/push-handlers.js).
                importScripts: ['push-handlers.js'],
                // Precache the immutable app shell/assets only. Server-owned and data
                // routes must always hit the network — PHI responses are no-store and
                // must never be served the SPA shell from cache.
                navigateFallbackDenylist: [
                    /^\/api\//, /^\/recordings\//, /^\/annotations\//, /^\/media\//,
                    /^\/compute\//, /^\/static\//, /^\/viewer\//,
                    /^\/project\//, /^\/plugin\//, /^\/\.well-known\//,
                ],
                // Runtime-cache the viewer lib and self-hosted Pyodide so the installed
                // app works fully offline (open a local file + compute). These live
                // outside the precached dist (Django serves them from viewer-dist /
                // vendor), so they populate on first ONLINE use, then serve from cache.
                // Cached responses keep their CORP header, so they still load under the
                // viewer's COEP isolation. Order matters — first match wins.
                runtimeCaching: [
                    {
                        // Pyodide lockfile is mutable (mne/pooch merge): revalidate when
                        // online but keep an offline copy. Must precede the CacheFirst rule.
                        urlPattern: /\/vendor\/pyodide\/.*\.json$/,
                        handler: 'StaleWhileRevalidate',
                        options: {
                            cacheName: 'pyodide-lock',
                            cacheableResponse: { statuses: [200] },
                        },
                    },
                    {
                        // Version-pinned Pyodide runtime + wheels/wasm — immutable.
                        urlPattern: /\/vendor\/pyodide\//,
                        handler: 'CacheFirst',
                        options: {
                            cacheName: 'pyodide-assets',
                            expiration: { maxEntries: 600, maxAgeSeconds: 60 * 60 * 24 * 365 },
                            cacheableResponse: { statuses: [200] },
                        },
                    },
                    {
                        // The viewer lib itself, which has a FIXED filename and so carries no
                        // cache-busting hash — Django serves it `Cache-Control: no-cache` for
                        // exactly that reason. StaleWhileRevalidate defeats that: it answers from
                        // the cache first and refreshes in the background, so every deploy is one
                        // reload behind and no error ever surfaces. NetworkFirst keeps the offline
                        // copy while making a reachable server authoritative. Must precede the
                        // catch-all /viewer/ rule below.
                        urlPattern: /\/viewer\/(.*\/)?epicurrents-lib\./,
                        handler: 'NetworkFirst',
                        options: {
                            cacheName: 'viewer-lib',
                            networkTimeoutSeconds: 10,
                            expiration: { maxEntries: 20, maxAgeSeconds: 60 * 60 * 24 * 90 },
                            cacheableResponse: { statuses: [200] },
                        },
                    },
                    {
                        // The lib's worker bundles and other viewer assets. These are
                        // content-hashed, so a changed build is a changed URL and a stale hit is
                        // impossible.
                        urlPattern: /\/viewer\//,
                        handler: 'StaleWhileRevalidate',
                        options: {
                            cacheName: 'viewer-assets',
                            expiration: { maxEntries: 100, maxAgeSeconds: 60 * 60 * 24 * 90 },
                            cacheableResponse: { statuses: [200] },
                        },
                    },
                    {
                        // Lead-field manifest is regenerated when the fields are rebuilt:
                        // revalidate online, keep an offline copy. Must precede the .npz
                        // CacheFirst rule. Matched on the /leadfields/ path segment so it
                        // works regardless of the (relative) STATIC_URL prefix.
                        urlPattern: /\/leadfields\/manifest\.json$/,
                        handler: 'StaleWhileRevalidate',
                        options: {
                            cacheName: 'leadfield-manifest',
                            cacheableResponse: { statuses: [200] },
                        },
                    },
                    {
                        // Content-addressed lead-field blobs (raw float64) — immutable
                        // (the hash in the filename changes when the computation changes),
                        // so cache forever and let a new hash fetch fresh. Same
                        // offline-on-first-use pattern as the Pyodide assets above.
                        urlPattern: /\/leadfields\/.*\.bin$/,
                        handler: 'CacheFirst',
                        options: {
                            cacheName: 'leadfield-assets',
                            expiration: { maxEntries: 50, maxAgeSeconds: 60 * 60 * 24 * 365 },
                            cacheableResponse: { statuses: [200] },
                        },
                    },
                ],
            },
            manifest: {
                name: 'Epicurrents',
                short_name: 'Epicurrents',
                description: 'A viewer for neurophysiological signal studies',
                theme_color: '#ffffff',
                icons: [
                    {
                        src: 'pwa-192x192.png',
                        sizes: '192x192',
                        type: 'image/png'
                    },
                    {
                        src: 'pwa-512x512.png',
                        sizes: '512x512',
                        type: 'image/png'
                    },
                    {
                        src: 'pwa-512x512.png',
                        sizes: '512x512',
                        type: 'image/png',
                        purpose: 'any maskable'
                    },
                ]
            }
        })
    )
    // In-memory mock API — active when VITE_BACKEND_URL=mock.
    // State resets on every full page navigation; see mocks.ts.
    if (useMock) {
        // Strip VITE_API_BASE_URL prefix so handleMock always sees /api/…, /recordings/…, etc.
        // e.g. if VITE_API_BASE_URL=/preview/ then /preview/api/v1/… → /api/v1/…
        const apiBase = (env.VITE_API_BASE_URL ?? '/').replace(/\/$/, '')
        plugins.push({
            name: 'mock-api',
            configureServer(server) {
                server.middlewares.use(async (req, res, next) => {
                    const url = req.url ?? ''
                    const rawPath = url.replace(/\?.*$/, '')
                    const path = apiBase && rawPath.startsWith(apiBase)
                        ? rawPath.slice(apiBase.length)
                        : rawPath

                    // Reset seed state on every full browser navigation.
                    if (
                        req.headers['sec-fetch-mode'] === 'navigate' &&
                        req.headers['sec-fetch-dest'] === 'document'
                    ) {
                        const { resetState, SESSION_COOKIE_CLEAR } = await import('./mocks')
                        res.setHeader('Set-Cookie', SESSION_COOKIE_CLEAR)
                        resetState()
                        return next()
                    }

                    // Only intercept known API path prefixes.
                    if (
                        !path.startsWith('/api/') &&
                        !path.startsWith('/recordings/') &&
                        !path.startsWith('/annotations/')
                    ) return next()

                    const { handleMock } = await import('./mocks')
                    const handled = await handleMock(
                        (req.method ?? 'GET').toUpperCase(),
                        path,
                        req,
                        res,
                    ).catch((err: unknown) => { next(err); return true })
                    if (!handled) next()
                })
            },
        } satisfies import('vite').Plugin)
    }
    const backend = env.VITE_BACKEND_URL
    return {
        base: env.VITE_BASE_URL ?? '/',
        server: {
            // Hostnames the dev server may be reached by when it is not localhost — a
            // remote workstation, a tunnel, a container name. Kept out of the repo
            // because the list is per-developer: set VITE_DEV_ALLOWED_HOSTS in
            // frontend/.env as a comma-separated list.
            allowedHosts: (env.VITE_DEV_ALLOWED_HOSTS ?? '')
                .split(',')
                .map((h) => h.trim())
                .filter(Boolean),
            host: '0.0.0.0',
            port: 5173,
            fs: { allow: ['..'] },
            // Pre-transform entry files at startup to avoid cold-start delay on first page load.
            warmup: {
                clientFiles: [
                    './src/main.ts',
                    './src/App.vue',
                    './src/views/*.vue',
                    './src/router/index.ts',
                    './src/stores/*.ts',
                    './src/lib/http.ts',
                ],
            },
            // Proxy API requests to a real backend when VITE_BACKEND_URL is set.
            ...(!useMock && backend ? {
                proxy: {
                    '/api/': { target: backend, changeOrigin: true },
                    '/project/': { target: backend, changeOrigin: true },
                    '/plugin/': { target: backend, changeOrigin: true },
                    '/recordings/': { target: backend, changeOrigin: true },
                    '/annotations/': { target: backend, changeOrigin: true },
                },
            } : {}),
        },
        resolve: {
            // Shared with vitest.config.ts — see build-aliases.ts for why these
            // two cannot go through the package.json imports map.
            alias: buildAliases(env.VITE_PROJECT),
        },
        define: {
            __INTLIFY_JIT_COMPILATION__: true,
            'process.env.NODE_ENV': JSON.stringify(env.NODE_ENV),
            // Per-plugin build-time flags. Each replaces to a boolean literal so
            // Rollup constant-folds the `if (__PLUGIN_X__)` guard in
            // src/plugins/active.ts and tree-shakes disabled plugins out of the
            // bundle entirely (a runtime list check would not fold). Add one
            // flag per registered frontend plugin.
            __PLUGIN_DICOM__: JSON.stringify(
                (env.VITE_PLUGINS ?? '').split(',').map(s => s.trim()).includes('dicom')
            ),
        },
        build: {
            rollupOptions: {
                output: {
                    // The Epicurrents viewer (DefaultInterface) strips all page stylesheets
                    // whose filename does not contain "epicurrents" when it mounts. Naming
                    // the app CSS bundle with "epicurrents" in the filename prevents that.
                    assetFileNames: (assetInfo) => {
                        if (assetInfo.names?.some(n => n.endsWith('.css'))) {
                            return 'assets/epicurrents-platform-[hash][extname]'
                        }
                        return 'assets/[name]-[hash][extname]'
                    },
                },
            },
        },
        mode: env.NODE_ENV,
        plugins,
    }
})
