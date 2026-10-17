import type { ViewerPlugin } from '#projects/types'

const isPlainObject = (value: unknown): value is Record<string, unknown> => {
    if (typeof value !== 'object' || value === null || Array.isArray(value)) {
        return false
    }
    const proto = Object.getPrototypeOf(value)
    return proto === null || proto === Object.prototype
}

/**
 * Merge viewer setup contributions into `target` in place and return it, recursing into plain objects and replacing
 * everything else, arrays and functions included.
 *
 * Contributions nest: a plugin and a project can both configure `modules.eeg`, and a shallow merge would let the later
 * one drop the earlier one's keys without a trace. Later sources win on a conflicting value; a key whose value is
 * `undefined` is skipped.
 *
 * @param target - Object to merge into. Mutated in place.
 * @param sources - Contributions, in ascending order of precedence.
 */
export function mergeSetup<T extends object> (target: T, ...sources: object[]): T {
    const merged = target as Record<string, unknown>
    for (const source of sources) {
        for (const [key, value] of Object.entries(source)) {
            if (value === undefined) {
                continue
            }
            if (isPlainObject(value) && isPlainObject(merged[key])) {
                merged[key] = mergeSetup({ ...merged[key] }, value)
            } else if (isPlainObject(value)) {
                merged[key] = mergeSetup({}, value)
            } else {
                merged[key] = value
            }
        }
    }
    return target
}

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
 * - `extraSetup` — merged with `mergeSetup` in plugin order; a later plugin
 *   overrides an earlier one on a conflicting value.
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

    const contributions = plugins.map(p => p.extraSetup).filter(setup => !!setup)
    const extraSetup = contributions.length ? mergeSetup({}, ...contributions) : undefined

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
