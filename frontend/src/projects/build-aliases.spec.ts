// @vitest-environment node

/**
 * Specs for the `#project` alias resolution in `frontend/build-aliases.ts`.
 *
 * Node rather than the suite's default jsdom: this is build-time config that
 * reads the filesystem, and under jsdom `import.meta.url` is the dev server's
 * http URL, which `fileURLToPath` rejects before any assertion runs.
 *
 * The alias decides which project's Vue plugin is the only one in the module
 * graph, so what it resolves to is the whole of the cross-project isolation.
 * Two of its outcomes fail in ways a build does not report as a mistake: a
 * backend-only project pointed at a `frontend/index.ts` that was never written,
 * and a mistyped project name quietly building the base UI.
 *
 * The backend-only case has no permanent subject — every project in the tree
 * ships a frontend, `example` included — so the spec materialises a scratch
 * project directory (gitignored, removed afterwards) to stand in for one.
 *
 * Lives under `src/` because vitest's include globs cover `src/` and the active
 * project, not the config files beside them.
 */

import { mkdirSync, rmSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { afterAll, beforeAll, describe, expect, it } from 'vitest'

import { buildAliases } from '../../build-aliases'

const BACKEND_ONLY = '.spec-backend-only'
const backendOnlyDir = fileURLToPath(new URL(`../../../projects/${BACKEND_ONLY}`, import.meta.url))

describe('#project alias', () => {
    beforeAll(() => {
        mkdirSync(backendOnlyDir, { recursive: true })
    })

    afterAll(() => {
        rmSync(backendOnlyDir, { recursive: true, force: true })
    })

    it('resolves to the base plugin when no project is set', () => {
        expect(buildAliases()['#project']).toMatch(/src\/projects\/base\.ts$/)
        expect(buildAliases('')['#project']).toMatch(/src\/projects\/base\.ts$/)
    })

    it('resolves a backend-only project to the base plugin rather than a missing file', () => {
        // A project with no frontend/ is legitimate; before this fell back,
        // building one failed with "Could not load .../frontend/index.ts".
        expect(buildAliases(BACKEND_ONLY)['#project']).toMatch(/src\/projects\/base\.ts$/)
    })

    it('refuses a project that is not on disk instead of silently building the base UI', () => {
        expect(() => buildAliases('nosuchproject')).toThrow(/does not exist/)
    })

    it('resolves the scaffolded example project to its own frontend entry', () => {
        // example ships a frontend half precisely so this contract has a
        // subject in every tree that carries the template.
        expect(buildAliases('example')['#project']).toMatch(/projects\/example\/frontend\/index\.ts$/)
    })
})
