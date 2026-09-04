import type { ViewerPlugin } from './types'
import { plugin as activePlugin } from '#project'

/**
 * The viewer plugin for the currently active project, resolved at build time
 * from the `VITE_PROJECT` environment variable.
 *
 * `#project` is an alias, defined in vite.config.ts, that points at
 * `projects/<VITE_PROJECT>/frontend/index.ts` — or at `./base` when no project
 * is set. Only the active project is ever imported, so the others contribute
 * nothing to the bundle whether or not anything shakes them out.
 *
 * That distinction is the reason for the alias rather than a static import of
 * each project behind a runtime check. Tree-shaking removes an unused project's
 * JavaScript but not its CSS: Vue `<style scoped>` blocks are emitted for any
 * component in the module graph, so any arrangement that puts every project in
 * that graph ships every project's component styles, class names and all. Only
 * one module is ever imported here, so nothing else is in the graph to emit.
 *
 * DICOM is not a project — it is a plugin. Enable it via `VITE_PLUGINS=dicom`
 * (see `#plugins/active`) instead.
 */
export const plugin: ViewerPlugin = activePlugin
