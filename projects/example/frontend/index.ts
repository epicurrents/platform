/**
 * Viewer plugin for the *example* project — the frontend half of the scaffolded template.
 *
 * A project's frontend lives at `projects/<name>/frontend/index.ts` and exports a `plugin`
 * implementing `ViewerPlugin` (see `frontend/src/projects/types.ts` for every hook). The
 * `#project` build alias resolves to exactly one such entry — the `VITE_PROJECT` project's — so
 * nothing from any other project reaches the bundle. A backend-only project simply omits this
 * directory and the alias falls back to the base no-op plugin.
 *
 * This template registers the three most common fields: a route, a navigation link, and an icon.
 * All hooks are optional; delete what your project does not need.
 */
import type { ViewerPlugin } from '#projects/types'
import ExampleNotesView from './ExampleNotesView.vue'
import icons from './icons'

export const plugin: ViewerPlugin = {
    icons,
    navLinks: [
        {
            icon: 'example-note',
            id: 'example-notes',
            label: 'Project notes',
            order: 30,
            section: 'example-notes',
            to: '/project-notes',
        },
    ],
    routes: [
        {
            path: '/project-notes',
            name: 'example-notes',
            component: ExampleNotesView,
            meta: { navSection: 'example-notes', requiresAuth: true, title: 'Project notes' },
        },
    ],
}
