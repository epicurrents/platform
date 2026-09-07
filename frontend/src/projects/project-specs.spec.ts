// @vitest-environment node

/**
 * Specs for the viewer-dependent declaration reader in `frontend/project-specs.ts`.
 *
 * Node rather than the suite's default jsdom, for the reason given in
 * build-aliases.spec.ts: this is build-time config that reads the filesystem,
 * and under jsdom `import.meta.url` is the dev server's http URL, which
 * `fileURLToPath` rejects before any assertion runs.
 *
 * What the reader decides is which specs vitest collects on a machine with no
 * built viewer. Both of its silent failure directions are covered here: a
 * declaration that reaches nothing (the spec is collected and dies at import,
 * which is what the mechanism exists to prevent) and a declaration in a shape
 * the reader would skip over.
 *
 * No project in the tree declares one — the platform has no viewer-dependent
 * spec of its own, and the projects that do are separate repositories that
 * need not be checked out — so each case materialises a scratch project
 * directory to stand in for one.
 *
 * Lives under `src/` because vitest's include globs cover `src/` and the active
 * project, not the config files beside them.
 */

import { mkdirSync, rmSync, writeFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { afterEach, describe, expect, it } from 'vitest'

import { viewerDependentProjectSpecs } from '../../project-specs'

const PROJECT = '.spec-viewer-declaration'
const projectDirectory = fileURLToPath(new URL(`../../../projects/${PROJECT}`, import.meta.url))

/** Write a scratch project frontend carrying *manifest* as its package.json. */
function scratchProject(manifest: Record<string, unknown>, specFiles: string[] = []) {
    mkdirSync(`${projectDirectory}/frontend/__tests__`, { recursive: true })
    writeFileSync(`${projectDirectory}/frontend/package.json`, JSON.stringify(manifest))
    for (const file of specFiles) {
        writeFileSync(`${projectDirectory}/frontend/${file}`, '')
    }
}

describe('viewer-dependent spec declarations', () => {
    afterEach(() => {
        rmSync(projectDirectory, { recursive: true, force: true })
    })

    it('collects a declared spec as a path relative to the frontend directory', () => {
        scratchProject(
            { epicurrents: { viewerDependentTests: ['__tests__/annotations.test.ts'] } },
            ['__tests__/annotations.test.ts']
        )
        expect(viewerDependentProjectSpecs()).toContain(
            `../projects/${PROJECT}/frontend/__tests__/annotations.test.ts`
        )
    })

    it('ignores a project that declares nothing', () => {
        scratchProject({ name: '@epicurrents-project/scratch' })
        expect(viewerDependentProjectSpecs().filter(path => path.includes(PROJECT))).toEqual([])
    })

    it('refuses a declared path that is not on disk instead of excluding nothing', () => {
        // The symptom of accepting it appears only where the viewer is
        // unbuilt: the spec is collected and dies at import.
        scratchProject({ epicurrents: { viewerDependentTests: ['__tests__/renamed.test.ts'] } })
        expect(viewerDependentProjectSpecs).toThrow(/is not a file/)
    })

    it('refuses an "epicurrents" key that is not an object', () => {
        // Optional chaining past this would read undefined and declare
        // nothing, which is indistinguishable from declaring nothing on
        // purpose.
        scratchProject({ epicurrents: ['__tests__/annotations.test.ts'] })
        expect(viewerDependentProjectSpecs).toThrow(/is not an object/)
    })

    it('refuses a declaration that is not an array of strings', () => {
        scratchProject({ epicurrents: { viewerDependentTests: '__tests__/annotations.test.ts' } })
        expect(viewerDependentProjectSpecs).toThrow(/not an array of strings/)
    })

    it('refuses a manifest that is not valid JSON', () => {
        mkdirSync(`${projectDirectory}/frontend`, { recursive: true })
        writeFileSync(`${projectDirectory}/frontend/package.json`, '{ not json')
        expect(viewerDependentProjectSpecs).toThrow(/not valid JSON/)
    })

    it('skips a project directory with no frontend package.json', () => {
        mkdirSync(projectDirectory, { recursive: true })
        expect(viewerDependentProjectSpecs().filter(path => path.includes(PROJECT))).toEqual([])
    })
})
