/**
 * A viewer annotation's codes as a write sends them inline: registered standards only, each with the `meta` its
 * owner provides.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('#lib/http', () => ({
    http: {
        get: vi.fn(),
    },
}))

import { inlineCodes, registerCodeMetaProviders, resetAnnotationCodes } from './annotationCodes'
import { http } from '#lib/http'

const mockGet = vi.mocked(http.get)
const PIN = { schema: { library: 'score', version: '2.1.0', with_standard: '8.4.0' } }

function listing (...standards: string[]) {
    mockGet.mockResolvedValue({ data: standards.map(standard => ({ standard })) } as never)
}

function confinedListing (confined: string[], open: string[]) {
    mockGet.mockResolvedValue({
        data: [
            ...confined.map(standard => ({ confined: true, standard })),
            ...open.map(standard => ({ confined: false, standard })),
        ],
    } as never)
}

beforeEach(() => {
    mockGet.mockReset()
    resetAnnotationCodes()
})

describe('inlineCodes', () => {
    it('stamps each code with its provider and sends null meta without one', async () => {
        listing('hed', 'epicurrents.eeg')
        registerCodeMetaProviders(undefined, { hed: async () => PIN })
        expect(await inlineCodes({ 'epicurrents.eeg': 'EEG_ACT_EC', hed: '(Spike, ID/104)' })).toEqual({
            'epicurrents.eeg': { meta: null, value: 'EEG_ACT_EC' },
            hed: { meta: PIN, value: '(Spike, ID/104)' },
        })
    })

    it('leaves out a standard the deployment does not register, and an empty value', async () => {
        listing('hed')
        expect(await inlineCodes({ hed: '', icd10: 'G40.9' })).toEqual({})
    })

    it('keeps only confined vocabularies when asked, and every registered one otherwise', async () => {
        confinedListing(['hed'], ['epicurrents.eeg'])
        const codes = { 'epicurrents.eeg': 'EEG_ACT_EC', hed: 'Spike' }
        expect(Object.keys(await inlineCodes(codes, { confinedOnly: true }))).toEqual(['hed'])
        expect(Object.keys(await inlineCodes(codes)).sort()).toEqual(['epicurrents.eeg', 'hed'])
    })

    it('asks nothing for an annotation without codes', async () => {
        expect(await inlineCodes(null)).toEqual({})
        expect(await inlineCodes({})).toEqual({})
        expect(mockGet).not.toHaveBeenCalled()
    })

    it('reads the listing once, and again after a failed read', async () => {
        mockGet.mockRejectedValueOnce(new Error('offline'))
        await expect(inlineCodes({ hed: 'Spike' })).rejects.toThrow('offline')
        listing('hed')
        await inlineCodes({ hed: 'Spike' })
        await inlineCodes({ hed: 'Spike' })
        expect(mockGet).toHaveBeenCalledTimes(2)
    })

    it('keeps the first provider registered for a standard', async () => {
        listing('hed')
        registerCodeMetaProviders({ hed: () => 'project' }, { hed: () => 'plugin' })
        expect((await inlineCodes({ hed: 'Spike' })).hed.meta).toBe('project')
    })

    it('rejects when a provider fails, so no event is saved without its codes', async () => {
        listing('hed')
        registerCodeMetaProviders({ hed: () => Promise.reject(new Error('index')) })
        await expect(inlineCodes({ hed: 'Spike' })).rejects.toThrow('index')
    })
})
