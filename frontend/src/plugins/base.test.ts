/**
 * Merging viewer setup contributions: a plugin and the project can configure the same module without either losing
 * the other's keys.
 */

import { describe, expect, it } from 'vitest'
import { mergePlugins, mergeSetup } from './base'

describe('mergeSetup', () => {
    it('recurses into plain objects, later sources winning on a conflict', () => {
        const policy = () => Promise.resolve(null)
        const merged = mergeSetup(
            {},
            { modules: { eeg: { hedScore: { policy } } } },
            { modules: { eeg: { hedScore: { palette: ['Spike'] }, trends: { enabled: ['aeeg'] } } } },
            { modules: { eeg: { trends: { enabled: ['spectrogram'] } } } },
        )
        expect(merged).toEqual({
            modules: { eeg: { hedScore: { palette: ['Spike'], policy }, trends: { enabled: ['spectrogram'] } } },
        })
    })

    it('replaces arrays and functions, skips undefined, and copies what it merges', () => {
        const first = () => 1
        const second = () => 2
        const source = { modules: { eeg: { list: [1, 2] } } }
        const merged = mergeSetup({ fn: first, keep: 1 }, source, {
            fn: second,
            keep: undefined,
            modules: { eeg: { list: [3] } },
        })
        expect(merged).toEqual({ fn: second, keep: 1, modules: { eeg: { list: [3] } } })
        expect(source.modules.eeg.list).toEqual([1, 2])
    })
})

describe('mergePlugins', () => {
    it('merges the extra setup of every plugin', () => {
        const merged = mergePlugins([
            { extraSetup: { modules: { eeg: { a: 1 } } } },
            {},
            { extraSetup: { modules: { eeg: { b: 2 } } } },
        ])
        expect(merged.extraSetup).toEqual({ modules: { eeg: { a: 1, b: 2 } } })
        expect(mergePlugins([{}]).extraSetup).toBeUndefined()
    })
})
