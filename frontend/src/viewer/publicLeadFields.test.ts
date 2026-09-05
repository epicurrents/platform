/**
 * Specs for the public viewer's lead-field entry point.
 *
 * The module does its work as an import side effect against `window.__EPICURRENTS__`, so each case
 * arranges the global first and then imports a fresh copy of it.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

type ViewerGlobal = {
    __EPICURRENTS__?: { SETUP?: unknown }
}

const asGlobal = () => window as unknown as ViewerGlobal

/** Import the module after the global is arranged, so its side effect sees the intended state. */
const run = async () => {
    vi.resetModules()
    await import('./publicLeadFields')
}

const eegOf = (setup: unknown) => (setup as { modules: { eeg: Record<string, unknown> } }).modules.eeg

beforeEach(() => {
    vi.spyOn(console, 'warn').mockImplementation(() => undefined)
})

afterEach(() => {
    delete asGlobal().__EPICURRENTS__
    vi.restoreAllMocks()
})

describe('publicLeadFields', () => {
    it('installs the provider into a setup that declares no modules', async () => {
        const setup = { activeModules: ['eeg'] }
        asGlobal().__EPICURRENTS__ = { SETUP: setup }
        await run()
        expect(typeof eegOf(setup).leadFieldProvider).toBe('function')
    })

    it('keeps the rest of an existing eeg configuration', async () => {
        const setup = { modules: { eeg: { defaultMontage: 'lon' } } }
        asGlobal().__EPICURRENTS__ = { SETUP: setup }
        await run()
        expect(eegOf(setup).defaultMontage).toBe('lon')
        expect(typeof eegOf(setup).leadFieldProvider).toBe('function')
    })

    it('keeps other modules untouched', async () => {
        const setup = { modules: { acc: { some: 'config' } } }
        asGlobal().__EPICURRENTS__ = { SETUP: setup }
        await run()
        const modules = (setup as { modules: Record<string, unknown> }).modules
        expect(modules.acc).toEqual({ some: 'config' })
        expect(typeof eegOf(setup).leadFieldProvider).toBe('function')
    })

    // JSON can carry `modules.eeg` as a URL string but cannot carry a provider, so the object form
    // has to win or the page keeps the string and gets no provider.
    it('replaces a non-object eeg configuration', async () => {
        const setup = { modules: { eeg: '/some/config.json' } }
        asGlobal().__EPICURRENTS__ = { SETUP: setup }
        await run()
        expect(typeof eegOf(setup).leadFieldProvider).toBe('function')
    })

    // Writing a property onto a primitive is a silent no-op in a non-strict bundle, which would
    // present as the script having run with source localisation still unavailable.
    it('substitutes an object when modules is not one', async () => {
        const setup = { modules: 'nonsense' }
        asGlobal().__EPICURRENTS__ = { SETUP: setup }
        await run()
        expect(typeof eegOf(setup).leadFieldProvider).toBe('function')
    })

    it('warns instead of throwing when there is no setup to write into', async () => {
        asGlobal().__EPICURRENTS__ = {}
        await expect(run()).resolves.toBeUndefined()
        expect(console.warn).toHaveBeenCalled()
    })

    it('warns instead of throwing when the viewer global is absent entirely', async () => {
        await expect(run()).resolves.toBeUndefined()
        expect(console.warn).toHaveBeenCalled()
    })
})
