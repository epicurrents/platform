/**
 * Module aliases shared by the app build and the test runner.
 *
 * These two cannot be resolved by the `imports` map in package.json, which is
 * how every other `#`-prefixed specifier reaches the platform: Node subpath
 * imports only handle specifiers that begin with `#`, and only relative to the
 * importing file's nearest package.json. `scoped-event-log` is a bare
 * specifier, and `#project` points at a target chosen from an environment
 * variable. Both therefore have to be resolver-level aliases.
 *
 * Defined once here because vite.config.ts and vitest.config.ts are separate
 * configs that do not inherit from each other, and an alias present in one but
 * not the other fails asymmetrically: the app builds and the specs do not, or
 * the reverse, with an error that names a module rather than a missing alias.
 */

import { existsSync } from 'node:fs'
import { resolve } from 'path'
import { fileURLToPath } from 'node:url'

const here = fileURLToPath(new URL('.', import.meta.url))

/**
 * Build the alias map for a given active project.
 *
 * @param project - The `VITE_PROJECT` value. Empty or undefined selects the base no-op plugin, which is what a deployment with no project runs.
 */
export function buildAliases(project?: string): Record<string, string> {
    return {
        // The viewer's scoped-event-log, so project code firing user-facing
        // announcements goes through the same Log pipeline the viewer's
        // App.vue listens on.
        'scoped-event-log': resolve(here, './viewer/util/scoped-event-log/dist/index.js'),
        // The active project's Vue plugin, which lives in the project's own
        // repository at projects/<name>/frontend/. Resolving exactly one path
        // is what keeps every other project out of the bundle — see
        // src/projects/active.ts for why tree-shaking was not enough.
        '#project': resolveProjectPlugin(project),
    }
}

/**
 * Pick the module `#project` resolves to, distinguishing a project that has no
 * frontend from one that is not there at all.
 *
 * A project may legitimately be backend-only — extra models, endpoints and EDF
 * middleware, with no UI of its own — and the scaffolded starting point must
 * build for one. Pointing at a missing `frontend/index.ts` regardless fails
 * the build with an unresolved import naming a path the developer never wrote.
 *
 * The absent directory is the opposite case and must not share the fallback. A
 * mistyped `VITE_PROJECT`, or a project bootstrap failed to clone, would
 * otherwise build the base UI and succeed — shipping a deployment with none of
 * its project's interface and nothing anywhere saying so.
 */
function resolveProjectPlugin(project?: string): string {
    const base = resolve(here, './src/projects/base.ts')
    if (!project) {
        return base
    }
    const directory = resolve(here, `../projects/${project}`)
    const entry = resolve(directory, 'frontend/index.ts')
    if (existsSync(entry)) {
        return entry
    }
    if (existsSync(directory)) {
        return base
    }
    throw new Error(
        `VITE_PROJECT is "${project}" but projects/${project}/ does not exist. ` +
            'Check the spelling, or clone the project into that directory ' +
            '(bootstrap.sh does this from EPICURRENTS_PROJECT_REPO).'
    )
}
