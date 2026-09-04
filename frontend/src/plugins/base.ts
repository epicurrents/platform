import type { ViewerPlugin } from '#projects/types'

/**
 * Merge a list of plugin `ViewerPlugin` objects into a single one.
 *
 * Plugins compose, so — unlike the single active project — the frontend may
 * have several plugin contributions to combine. The merge rules mirror how the
 * consuming sites already fold the project plugin in:
 *
 * - `routes` / `navLinks` — concatenated in plugin order.
 * - `icons` / `iconLibraries` — shallow-merged; the earliest plugin to declare
 *   a given icon name (or library/name pair) wins, matching the base-icons-win
 *   precedence used in `main.ts`.
 * - `viewerPanel` — the first plugin that defines one wins (a page hosts a
 *   single viewer overlay).
 * - `extraSetup` — object-merged in plugin order; a later plugin overrides an
 *   earlier one on key conflicts.
 * - `onAppReady` / `onStudiesReady` — chained: every plugin's hook runs, in
 *   order, each awaited before the next.
 *
 * @param plugins - Enabled plugins, in `VITE_PLUGINS` order.
 * @returns One `ViewerPlugin` combining all contributions.
 */
export function mergePlugins(plugins: ViewerPlugin[]): ViewerPlugin {
    const icons: Record<string, string> = {}
    for (const p of plugins) {
        for (const [name, svg] of Object.entries(p.icons ?? {})) {
            if (!(name in icons)) {
                icons[name] = svg
            }
        }
    }

    const iconLibraries: Record<string, Record<string, string>> = {}
    for (const p of plugins) {
        for (const [lib, entries] of Object.entries(p.iconLibraries ?? {})) {
            const target = (iconLibraries[lib] ??= {})
            for (const [name, svg] of Object.entries(entries)) {
                if (!(name in target)) {
                    target[name] = svg
                }
            }
        }
    }

    let extraSetup: Record<string, unknown> | undefined
    for (const p of plugins) {
        if (p.extraSetup) {
            extraSetup = { ...(extraSetup ?? {}), ...p.extraSetup }
        }
    }

    const viewerPanel = plugins.find(p => p.viewerPanel != null)?.viewerPanel ?? null

    return {
        routes: plugins.flatMap(p => p.routes ?? []),
        navLinks: plugins.flatMap(p => p.navLinks ?? []),
        icons,
        iconLibraries,
        viewerPanel,
        extraSetup,
        async onAppReady(epic, bus) {
            for (const p of plugins) {
                await p.onAppReady?.(epic, bus)
            }
        },
        async onStudiesReady(epic, studies) {
            for (const p of plugins) {
                await p.onStudiesReady?.(epic, studies)
            }
        },
    }
}
