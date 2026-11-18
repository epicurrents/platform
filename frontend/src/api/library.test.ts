/**
 * Tests for the dataset browser's client calls: the item listing read to its end for the folder tree, the repeated
 * `hash` parameter of the tag lookup, and the subject write naming what to clear.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('#lib/http', () => ({
    http: {
        get: vi.fn(),
        patch: vi.fn(),
    },
}))

import { listAllDatasetItems, listRecordingTags, updateDatasetMemberSubject } from './library'
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
