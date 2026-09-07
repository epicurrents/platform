/**
 * Specs for the public viewer's platform-owned setup module.
 *
 * The module does its work as an import side effect against `window.__EPICURRENTS__`, so each case
 * arranges the global first and then imports a fresh copy of it.
 *
 * The property under test throughout is the precedence rule: platform defaults fill in what the
 * page left unsaid, and anything the page declared survives. Getting that backwards would not
 * fail loudly — it would quietly override a deployment's `PUBLIC_VIEWER_MODES` from a bundle.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

type ViewerGlobal = {
    __EPICURRENTS__?: { SETUP?: unknown }
}

const asGlobal = () => window as unknown as ViewerGlobal

/** Import the module after the global is arranged, so its side effect sees the intended state. */
const run = async () => {
    vi.resetModules()
    await import('./publicSetup')
}

const eegOf = (setup: unknown) => (setup as { modules: { eeg: Record<string, unknown> } }).modules.eeg

beforeEach(() => {
    vi.spyOn(console, 'warn').mockImplementation(() => undefined)
})

afterEach(() => {
    delete asGlobal().__EPICURRENTS__
    vi.restoreAllMocks()
})

describe('publicSetup', () => {
    describe('lead-field provider', () => {
        it('installs the provider into a setup that declares no modules', async () => {
            const setup = { containerId: 'viewer' }
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

        // JSON can carry `modules.eeg` as a URL string but cannot carry a provider, so the object
        // form has to win or the page keeps the string and gets no provider.
        it('replaces a non-object eeg configuration', async () => {
            const setup = { modules: { eeg: '/some/config.json' } }
            asGlobal().__EPICURRENTS__ = { SETUP: setup }
            await run()
            expect(typeof eegOf(setup).leadFieldProvider).toBe('function')
        })

        // Writing a property onto a primitive is a silent no-op in a non-strict bundle, which
        // would present as the script having run with source localisation still unavailable.
        it('substitutes an object when modules is not one', async () => {
            const setup = { modules: 'nonsense' }
            asGlobal().__EPICURRENTS__ = { SETUP: setup }
            await run()
            expect(typeof eegOf(setup).leadFieldProvider).toBe('function')
        })
    })

    describe('trend configuration', () => {
        it('gives the page a trend setup it did not declare', async () => {
            const setup = {}
            asGlobal().__EPICURRENTS__ = { SETUP: setup }
            await run()
            expect(eegOf(setup).trends).toEqual({ defaultType: 'aeeg', showStrip: false })
        })

        // A default that overwrote a mode's own trend setup would take the deployment's ability to
        // configure trends away from PUBLIC_VIEWER_MODES entirely.
        it('lets a mode override the trend setup', async () => {
            const setup = { modules: { eeg: { trends: { defaultType: 'spectrogram', showStrip: true } } } }
            asGlobal().__EPICURRENTS__ = { SETUP: setup }
            await run()
            expect(eegOf(setup).trends).toEqual({ defaultType: 'spectrogram', showStrip: true })
            expect(typeof eegOf(setup).leadFieldProvider).toBe('function')
        })
    })

    describe('setup defaults', () => {
        it('asks for shared memory when the page leaves it unsaid', async () => {
            const setup: Record<string, unknown> = {}
            asGlobal().__EPICURRENTS__ = { SETUP: setup }
            await run()
            expect(setup.useSAB).toBe(true)
        })

        // The override case and the falsy case are the same case here, and it is the one a `||` or
        // `??` fill-in would silently undo — useSAB is exactly the setting an operator turns off
        // to work around a browser whose SharedArrayBuffer misbehaves.
        it('leaves a deliberately disabled useSAB alone', async () => {
            const setup: Record<string, unknown> = { useSAB: false }
            asGlobal().__EPICURRENTS__ = { SETUP: setup }
            await run()
            expect(setup.useSAB).toBe(false)
        })

        // Three of these stay in PUBLIC_VIEWER_MODES (assetPath is derived by the view, and
        // vendor_pyodide reads the version back out of pyodideAssetPath). The other two are
        // resolved by the viewer whatever the page says, so writing them here would be inert
        // config that reads as if it worked.
        it('invents no value for keys it does not own', async () => {
            const setup: Record<string, unknown> = {}
            asGlobal().__EPICURRENTS__ = { SETUP: setup }
            await run()
            for (const key of [
                'activeModules', 'assetPath', 'containerId', 'logThreshold', 'pyodideAssetPath',
            ]) {
                expect(setup).not.toHaveProperty(key)
            }
        })
    })

    describe('missing global', () => {
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
})
