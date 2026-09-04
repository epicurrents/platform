/**
 * Link each project's frontend to the platform's node_modules.
 *
 * A project's Vue plugin lives at `projects/<name>/frontend/`, outside this
 * directory, because the project is its own repository checked out alongside
 * the platform. Node resolves a bare import by walking up from the importing
 * file, and that walk never reaches `frontend/node_modules` — so without a link
 * the project cannot import the platform's dependencies at all, and the build
 * fails naming the package rather than the cause.
 *
 * This is what npm workspaces would create; the platform does not use them
 * because the workspace root would have to sit above `frontend/`, which would
 * move node_modules for the Docker build and CI caching too.
 *
 * Idempotent, and silent when there are no projects — a base deployment has
 * none, and that is not an error.
 */

import { existsSync, lstatSync, mkdirSync, readdirSync, rmSync, symlinkSync } from 'node:fs'
import { dirname, join, relative, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const frontend = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const projectsDir = resolve(frontend, '..', 'projects')
const target = join(frontend, 'node_modules')

if (!existsSync(projectsDir)) {
    process.exit(0)
}

let linked = 0
for (const name of readdirSync(projectsDir)) {
    const projectFrontend = join(projectsDir, name, 'frontend')
    if (!existsSync(join(projectFrontend, 'package.json'))) {
        continue
    }
    const link = join(projectFrontend, 'node_modules')
    // Replace only a symlink. A real directory means someone installed
    // dependencies there deliberately, and removing it would be destructive.
    if (existsSync(link) || lstatSync(link, { throwIfNoEntry: false })) {
        if (!lstatSync(link).isSymbolicLink()) {
            console.warn(`[link:projects] ${name}: node_modules is a real directory, leaving it alone`)
            continue
        }
        rmSync(link)
    }
    mkdirSync(dirname(link), { recursive: true })
    symlinkSync(relative(projectFrontend, target), link, 'dir')
    linked += 1
}
if (linked) {
    console.log(`[link:projects] linked node_modules for ${linked} project frontend(s)`)
}
