import type { ViewerPlugin } from '#projects/types'
import { mergePlugins } from './base'
import { plugin as dicomPlugin } from './dicom/index'

/**
 * The merged viewer plugin for every enabled frontend plugin, selected at build
 * time from the comma-separated `VITE_PLUGINS` environment variable (mirror of
 * the backend's `EPICURRENTS_PLUGINS`; set it in `frontend/.env` or the Docker
 * build args, leave unset for no plugins).
 *
 * Each plugin is guarded by a per-plugin build-time flag (`__PLUGIN_DICOM__`,
 * defined in vite.config.ts from `VITE_PLUGINS`). The flag replaces to a
 * boolean literal, so Rollup constant-folds the guard and tree-shakes disabled
 * plugins out of the bundle entirely — the same dead-branch elimination the
 * projects layer gets from its `VITE_PROJECT` ternary. A plain runtime lookup
 * keyed on the env string would bundle every registered plugin regardless.
 *
 * Unlike a project — of which exactly one is active — zero or more plugins may
 * be enabled, so their contributions are merged (see `mergePlugins`) rather
 * than one being picked. Order follows the guard order below, which must match
 * the documented `VITE_PLUGINS` merge order.
 *
 * Registering a new plugin: add its `import` above, a `__PLUGIN_<NAME>__`
 * define in vite.config.ts (+ declaration in `src/vite-plugins.d.ts`), a
 * guarded `push` below, and its name in `KNOWN_PLUGINS`.
 */
const KNOWN_PLUGINS = ['dicom']

const active: ViewerPlugin[] = []
if (__PLUGIN_DICOM__) {
    active.push(dicomPlugin)
}

// Surface typos early: a VITE_PLUGINS entry that matches no registered plugin
// would otherwise silently produce a pluginless build (the backend validates
// its list at boot; this is the frontend analogue, dev-only).
if (import.meta.env.DEV) {
    const requested = ((import.meta.env.VITE_PLUGINS as string | undefined) ?? '')
        .split(',')
        .map(name => name.trim())
        .filter(Boolean)
    for (const name of requested) {
        if (!KNOWN_PLUGINS.includes(name)) {
            console.warn(
                `VITE_PLUGINS names unknown plugin '${name}' — known plugins: ${KNOWN_PLUGINS.join(', ')}`
            )
        }
    }
}

export const plugin: ViewerPlugin = mergePlugins(active)
