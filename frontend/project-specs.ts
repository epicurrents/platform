/**
 * Spec files a project declares as needing the built viewer.
 *
 * Some specs import viewer code, which the `#`/bare aliases resolve into
 * `frontend/viewer/**`. Those files are build output that only exists after
 * `npm run setup` inside the submodule, so on a plain checkout they cannot be
 * loaded at all — the failure is at import time, before any `skipIf` in the
 * spec could run. The test runner therefore has to know which files to leave
 * out of collection, and it has to know before it collects them.
 *
 * The declaration belongs to the project rather than to this repository. A
 * project is its own git repository checked out at `projects/<name>/`, and the
 * platform does not track it; a list of project spec paths in the platform's
 * own config would name deployments that the platform has no other reason to
 * know about, and would need a commit here whenever a project renamed a file.
 *
 * So each project states its own, in the `package.json` its frontend already
 * carries for the `imports` map:
 *
 * ```json
 * "epicurrents": { "viewerDependentTests": ["__tests__/annotations.test.ts"] }
 * ```
 *
 * Paths are relative to that project's `frontend/` directory, and name files
 * rather than globs so that a stale entry is reported instead of matching
 * nothing.
 */

import { existsSync, readdirSync, readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = fileURLToPath(new URL('.', import.meta.url))

/**
 * Every viewer-dependent spec declared by a project on disk, as paths relative
 * to this directory — the form vitest's `exclude` patterns take.
 *
 * Empty when no project is checked out, which is the CI case and the case for
 * a deployment that runs none.
 */
export function viewerDependentProjectSpecs(): string[] {
    const projectsDirectory = resolve(here, '../projects')
    if (!existsSync(projectsDirectory)) {
        return []
    }
    const collected: string[] = []
    for (const project of readdirSync(projectsDirectory)) {
        const manifest = resolve(projectsDirectory, project, 'frontend/package.json')
        if (!existsSync(manifest)) {
            continue
        }
        for (const entry of readDeclaration(manifest, project)) {
            if (!existsSync(resolve(projectsDirectory, project, 'frontend', entry))) {
                throw new Error(
                    `projects/${project}/frontend/package.json declares viewerDependentTests ` +
                        `entry "${entry}", which is not a file in that project's frontend/. ` +
                        'Correct the path or drop the entry: an entry that matches nothing ' +
                        'silently stops excluding the spec it was written for.'
                )
            }
            collected.push(`../projects/${project}/frontend/${entry}`)
        }
    }
    return collected
}

/**
 * Read one project's declaration, rejecting a shape that would be ignored.
 *
 * A malformed value is thrown on rather than skipped because the symptom of
 * skipping is the very failure the declaration exists to prevent, appearing
 * only on machines without a built viewer.
 */
function readDeclaration(manifest: string, project: string): string[] {
    let parsed: unknown
    try {
        parsed = JSON.parse(readFileSync(manifest, 'utf8'))
    } catch (error) {
        throw new Error(`projects/${project}/frontend/package.json is not valid JSON: ${String(error)}`)
    }
    const section = (parsed as { epicurrents?: unknown })?.epicurrents
    if (section === undefined) {
        return []
    }
    // Optional chaining past a string or an array would read undefined here and
    // declare nothing, which is the silent outcome this whole check exists to
    // avoid — so the container's shape is rejected as loudly as its contents.
    if (typeof section !== 'object' || section === null || Array.isArray(section)) {
        throw new Error(
            `projects/${project}/frontend/package.json has an "epicurrents" key that is not an object. ` +
                'Expected { "viewerDependentTests": [...] }.'
        )
    }
    const declared = (section as { viewerDependentTests?: unknown }).viewerDependentTests
    if (declared === undefined) {
        return []
    }
    if (!Array.isArray(declared) || declared.some(entry => typeof entry !== 'string')) {
        throw new Error(
            `projects/${project}/frontend/package.json has epicurrents.viewerDependentTests, ` +
                'but it is not an array of strings. Expected paths relative to that ' +
                'project\'s frontend/, as in ["__tests__/annotations.test.ts"].'
        )
    }
    return declared as string[]
}
