import { createApp } from 'vue'
import { registerSW } from 'virtual:pwa-register'
import { showToast } from '#lib/toast'
import waDirective from '#directives/wa'
import { setBasePath } from '@awesome.me/webawesome/dist/utilities/base-path.js'
import { registerIconLibrary } from '@awesome.me/webawesome/dist/components/icon/library.js'
import '@awesome.me/webawesome/dist/styles/webawesome.css'
import icons from './icons'
import { plugin } from '#projects/active'
import { plugin as pluginsPlugin } from '#plugins/active'
// WebAwesome components — imported centrally so Vite pre-bundles them all
// at startup rather than discovering them lazily per-view (which triggers
// dep re-optimisation on first load and causes a ~3 min hang).
// Keep this list in sync when adding new wa-* components to any view.
import '@awesome.me/webawesome/dist/components/badge/badge.js'
import '@awesome.me/webawesome/dist/components/breadcrumb/breadcrumb.js'
import '@awesome.me/webawesome/dist/components/card/card.js'
import '@awesome.me/webawesome/dist/components/breadcrumb-item/breadcrumb-item.js'
import '@awesome.me/webawesome/dist/components/button/button.js'
import '@awesome.me/webawesome/dist/components/color-picker/color-picker.js'
import '@awesome.me/webawesome/dist/components/copy-button/copy-button.js'
import '@awesome.me/webawesome/dist/components/callout/callout.js'
import '@awesome.me/webawesome/dist/components/checkbox/checkbox.js'
import '@awesome.me/webawesome/dist/components/details/details.js'
import '@awesome.me/webawesome/dist/components/dialog/dialog.js'
import '@awesome.me/webawesome/dist/components/drawer/drawer.js'
import '@awesome.me/webawesome/dist/components/relative-time/relative-time.js'
import '@awesome.me/webawesome/dist/components/divider/divider.js'
import '@awesome.me/webawesome/dist/components/dropdown/dropdown.js'
import '@awesome.me/webawesome/dist/components/dropdown-item/dropdown-item.js'
import '@awesome.me/webawesome/dist/components/format-bytes/format-bytes.js'
import '@awesome.me/webawesome/dist/components/page/page.js'
import '@awesome.me/webawesome/dist/components/scroller/scroller.js'
import '@awesome.me/webawesome/dist/components/progress-bar/progress-bar.js'
import '@awesome.me/webawesome/dist/components/split-panel/split-panel.js'
import '@awesome.me/webawesome/dist/components/qr-code/qr-code.js'
import '@awesome.me/webawesome/dist/components/icon/icon.js'
import '@awesome.me/webawesome/dist/components/input/input.js'
import '@awesome.me/webawesome/dist/components/option/option.js'
import '@awesome.me/webawesome/dist/components/progress-ring/progress-ring.js'
import '@awesome.me/webawesome/dist/components/radio/radio.js'
import '@awesome.me/webawesome/dist/components/radio-group/radio-group.js'
import '@awesome.me/webawesome/dist/components/select/select.js'
import '@awesome.me/webawesome/dist/components/spinner/spinner.js'
import '@awesome.me/webawesome/dist/components/switch/switch.js'
import '@awesome.me/webawesome/dist/components/tab/tab.js'
import '@awesome.me/webawesome/dist/components/tab-group/tab-group.js'
import '@awesome.me/webawesome/dist/components/tab-panel/tab-panel.js'
import '@awesome.me/webawesome/dist/components/textarea/textarea.js'
import '@awesome.me/webawesome/dist/components/tree/tree.js'
import '@awesome.me/webawesome/dist/components/tree-item/tree-item.js'
import '@awesome.me/webawesome/dist/components/tooltip/tooltip.js'
import './style.css'
import App from './App.vue'
import { router } from '#router'
import { pinia } from '#stores'
import { i18n } from '#i18n'

// setKitCode is only required for the CDN-hosted delivery of WebAwesome and
// must not be called here — it triggers an outbound fetch to the WebAwesome CDN
// which times out (~2 min) in offline / air-gapped environments.
setBasePath('/node_modules/@awesome.me/webawesome/dist')

// Register all FA Pro icons as data URIs so wa-icon never makes a network
// fetch for icon SVGs — every icon is bundled at build time via icons.ts.
const mergedIcons: Record<string, string> = { ...icons }

// Base icons win over the active project's, which win over plugins'. First to
// register a given name keeps it, so iterate base → project → plugins.
for (const iconSet of [plugin.icons, pluginsPlugin.icons]) {
    for (const [name, svg] of Object.entries(iconSet ?? {})) {
        if (mergedIcons[name]) {
            continue
        }
        mergedIcons[name] = svg
    }
}

// Material Symbols SVGs ship with literal ``fill="#1f1f1f"`` (or similar);
// the mutator rewrites this to ``currentColor`` so the icon inherits the
// surrounding text colour the same way the previous FA Pro SVGs did.
const setFillCurrentColor = (svg: SVGElement) => svg.setAttribute('fill', 'currentColor')

registerIconLibrary('default', {
    resolver: (name: string) => {
        const svg = mergedIcons[name]
        if (!svg) return ''
        return `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`
    },
    mutator: setFillCurrentColor,
})

// Named icon libraries follow the same precedence as icons: project entries
// win over plugin entries within a shared library name. Merge before
// registering — registerIconLibrary replaces wholesale, so registering the
// project's and a plugin's same-named library in sequence would let the
// later (plugin) registration silently drop the project's icons.
const mergedIconLibraries: Record<string, Record<string, string>> = {}
for (const iconLibs of [plugin.iconLibraries, pluginsPlugin.iconLibraries]) {
    for (const [libraryName, libraryIcons] of Object.entries(iconLibs ?? {})) {
        const target = (mergedIconLibraries[libraryName] ??= {})
        for (const [name, svg] of Object.entries(libraryIcons)) {
            if (!(name in target)) {
                target[name] = svg
            }
        }
    }
}
for (const [libraryName, libraryIcons] of Object.entries(mergedIconLibraries)) {
    registerIconLibrary(libraryName, {
        resolver: (name: string) => {
            const svg = libraryIcons[name]
            if (!svg) return ''
            return `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`
        },
        mutator: setFillCurrentColor,
    })
}
// Expose registerIconLibrary on the global EPICURRENTS object so that the
// embedded viewer UMD bundle can register its own icon libraries into this
// same WebAwesome instance (the one that owns the wa-icon custom element).
window.__EPICURRENTS__ = window.__EPICURRENTS__ ?? {} as typeof window.__EPICURRENTS__
window.__EPICURRENTS__.registerIconLibrary = registerIconLibrary

createApp(App)
    .use(pinia)
    .use(router)
    .use(i18n)
    .directive('wa', waDirective)
    .mount('#app')

// Service worker: register the single precache+push worker. The SW uses the
// 'prompt' strategy (vite.config): a new build installs and WAITS rather than
// taking over, because a clinical viewer must never reload out from under an
// analysis in progress. When a new worker is waiting, `onNeedRefresh` fires and
// we surface a persistent toast with a "Reload" action; only when the user
// clicks it does `updateSW(true)` message the waiting worker to skip-wait,
// activate, and reload. So the user chooses the moment to update.
// Gate on serviceWorker support only — NOT import.meta.env.PROD. This project
// builds with NODE_ENV=development (vite.config sets `mode` from it), so PROD is
// always false and this whole block was being tree-shaken out of every build,
// meaning the SW was generated but never registered. registerSW is a no-op in the
// Vite dev server (VitePWA devOptions.enabled=false), so calling it unconditionally
// is safe there and registers the real worker in the Django-served build.

if ('serviceWorker' in navigator) {
    const updateSW = registerSW({
        immediate: true,
        onNeedRefresh() {
            // Persistent (duration: 0) — the prompt must not auto-dismiss, or a user
            // could miss it and stay on the old build. Reload only on their click.
            showToast(
                ['A new app version is available.', 'Save unfinished work before reloading the app.'],
                'brand',
                0,
                { label: 'Reload', run: () => { void updateSW(true) } },
            )
        },
        onRegisteredSW(_swUrl, registration) {
            // Poll for a new deployment so the prompt can surface MID-SESSION, not only
            // on the next reload — reloading is the very disruption the prompt defers, so
            // waiting for one to discover the update would defeat the point. The browser's
            // own update check is infrequent (~24h); an explicit hourly update() closes
            // that gap. No-op when offline (update() just rejects, caught by workbox).
            if (registration) {
                setInterval(() => { void registration.update() }, 60 * 60 * 1000)
            }
        },
    })
    // One-time migration: drop the legacy standalone push worker so it can no
    // longer race the precache worker for the root scope.
    navigator.serviceWorker.getRegistrations?.().then((regs) => {
        for (const reg of regs) {
            if (reg.active?.scriptURL.endsWith('/push-sw.js')) reg.unregister()
        }
    })
}
