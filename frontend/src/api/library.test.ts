/**
 * Tests for the dataset browser's client calls: the item listing read to its end for the folder tree, the repeated
 * `hash` parameter of the tag lookup, the subject write naming what to clear, and the filters as repeated parameters.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('#lib/http', () => ({
    http: {
        get: vi.fn(),
        patch: vi.fn(),
    },
}))

import { browseDataset, listAllDatasetItems, listRecordingTags, updateDatasetMemberSubject } from './library'
import { http } from '#lib/http'

const mockGet = vi.mocked(http.get)
const mockPatch = vi.mocked(http.patch)

/** A page of *count* placeholder items; only the length matters to the paging. */
function page (count: number, from = 0) {
    return { data: Array.from({ length: count }, (_, index) => ({ id: from + index })) }
}

beforeEach(() => {
    mockGet.mockReset()
    mockPatch.mockReset()
})

describe('listAllDatasetItems', () => {
    it('reads pages until a short one and keeps every item in order', async () => {
        mockGet.mockResolvedValueOnce(page(200)).mockResolvedValueOnce(page(200, 200)).mockResolvedValueOnce(page(3, 400))
        const items = await listAllDatasetItems('abc')
        expect(items).toHaveLength(403)
        expect(items.map(item => item.id)).toEqual(Array.from({ length: 403 }, (_, index) => index))
        expect(mockGet.mock.calls.map(call => call[1]?.params)).toEqual([
            { limit: 200, offset: 0 },
            { limit: 200, offset: 200 },
            { limit: 200, offset: 400 },
        ])
    })

    it('stops after an empty page when the membership is a whole number of pages', async () => {
        mockGet.mockResolvedValueOnce(page(200)).mockResolvedValueOnce(page(0))
        expect(await listAllDatasetItems('abc')).toHaveLength(200)
        expect(mockGet).toHaveBeenCalledTimes(2)
    })

    it('passes a share token on every page', async () => {
        mockGet.mockResolvedValueOnce(page(1))
        await listAllDatasetItems('abc', 'token')
        expect(mockGet.mock.calls[0][1]?.params).toEqual({ limit: 200, offset: 0, share_token: 'token' })
    })
})

describe('listRecordingTags', () => {
    it('sends each hash as its own parameter', async () => {
        mockGet.mockResolvedValueOnce({ data: [] })
        await listRecordingTags(['a'.repeat(32), 'b'.repeat(32)])
        const params = mockGet.mock.calls[0][1]?.params as URLSearchParams
        expect(params.getAll('hash')).toEqual(['a'.repeat(32), 'b'.repeat(32)])
    })
})

describe('updateDatasetMemberSubject', () => {
    it('sends a cleared field as null and answers the stored values', async () => {
        mockPatch.mockResolvedValueOnce({ data: { values: { age_band: '18-39' } } })
        const values = await updateDatasetMemberSubject('abc', 'f'.repeat(32), { age_band: '18-39', indication: null })
        expect(mockPatch).toHaveBeenCalledWith(
            `/api/v1/library/datasets/abc/recordings/${'f'.repeat(32)}/subject/`,
            { values: { age_band: '18-39', indication: null } },
        )
        expect(values).toEqual({ age_band: '18-39' })
    })
})

describe('browseDataset', () => {
    it('sends paging and each filter value as its own parameter', async () => {
        mockGet.mockResolvedValueOnce({ data: {} })
        await browseDataset('abc', { sort: '-name', limit: 50, offset: 0 }, {
            modality: ['eeg', 'ecg'],
            subject: { age_band: ['0-17', '65+'], indication: [] },
            ranges: {},
            tags: [3, 7],
        })
        const params = mockGet.mock.calls[0][1]?.params as URLSearchParams
        expect(mockGet.mock.calls[0][0]).toBe('/api/v1/library/datasets/abc/browse/')
        expect(params.get('sort')).toBe('-name')
        expect(params.get('offset')).toBe('0')
        expect(params.getAll('modality')).toEqual(['eeg', 'ecg'])
        expect(params.getAll('subject.age_band')).toEqual(['0-17', '65+'])
        expect(params.has('subject.indication')).toBe(false)
        expect(params.getAll('tag')).toEqual(['3', '7'])
    })

    it('sends each end of a range that is set, and an open end not at all', async () => {
        mockGet.mockResolvedValueOnce({ data: {} })
        await browseDataset('abc', { sort: 'name' }, {
            modality: [],
            subject: {},
            ranges: { age_years: { min: 0.5 }, weight: { min: 2, max: 10.25 } },
            tags: [],
        })
        const params = mockGet.mock.calls[0][1]?.params as URLSearchParams
        expect(params.get('subject.age_years.min')).toBe('0.5')
        expect(params.has('subject.age_years.max')).toBe(false)
        expect(params.get('subject.weight.min')).toBe('2')
        expect(params.get('subject.weight.max')).toBe('10.25')
    })

    it('sends no filter parameters without filters', async () => {
        mockGet.mockResolvedValueOnce({ data: {} })
        await browseDataset('abc', { sort: 'name' })
        const params = mockGet.mock.calls[0][1]?.params as URLSearchParams
        expect([...params.keys()]).toEqual(['sort'])
    })
})
