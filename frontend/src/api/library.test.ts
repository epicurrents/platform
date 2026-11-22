/**
 * Tests for the library's client calls: the collection, collection item and dataset item listings read to their end,
 * the repeated `hash` parameter of the tag lookup, the subject write naming what to clear, the filters as repeated
 * parameters, the share token a browser page is read with, the tag search, tagging by hash, the batch tagging sending
 * the browser's filters, the browser query carried in a viewer link and read back in its order, the public
 * listing naming the token to list through, and the order action of a dataset item.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('#lib/http', () => ({
    http: {
        delete: vi.fn(),
        get: vi.fn(),
        patch: vi.fn(),
        post: vi.fn(),
        put: vi.fn(),
    },
}))

import {
    browseDataset,
    browseFiltersFromQuery,
    browseFiltersToQuery,
    clearPublicListing,
    listAllCollectionItems,
    listAllCollections,
    listAllDatasetItems,
    listMatchingRecordings,
    listRecordingTags,
    listTags,
    orderDatasetItem,
    setPublicListing,
    tagDatasetMembers,
    tagObject,
    untagDatasetMembers,
    untagObject,
    updateDatasetMemberSubject,
    type DatasetBrowseFilters,
} from './library'
import { http } from '#lib/http'

const mockDelete = vi.mocked(http.delete)
const mockGet = vi.mocked(http.get)
const mockPatch = vi.mocked(http.patch)
const mockPost = vi.mocked(http.post)
const mockPut = vi.mocked(http.put)

/** A page of *count* placeholder items; only the length matters to the paging. */
function page (count: number, from = 0) {
    return { data: Array.from({ length: count }, (_, index) => ({ id: from + index })) }
}

beforeEach(() => {
    mockDelete.mockReset()
    mockGet.mockReset()
    mockPatch.mockReset()
    mockPost.mockReset()
    mockPut.mockReset()
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

describe('listAllCollectionItems', () => {
    it('reads past the first page, which a dataset made from the collection needs whole', async () => {
        mockGet.mockResolvedValueOnce(page(200)).mockResolvedValueOnce(page(200, 200)).mockResolvedValueOnce(page(0, 400))
        const items = await listAllCollectionItems(7)
        expect(items).toHaveLength(400)
        expect(mockGet.mock.calls.map(call => [call[0], call[1]?.params])).toEqual([
            ['/api/v1/library/collections/7/items/', { limit: 200, offset: 0 }],
            ['/api/v1/library/collections/7/items/', { limit: 200, offset: 200 }],
            ['/api/v1/library/collections/7/items/', { limit: 200, offset: 400 }],
        ])
    })
})

describe('listAllCollections', () => {
    it('reads the children of a parent to the end', async () => {
        mockGet.mockResolvedValueOnce(page(200)).mockResolvedValueOnce(page(12, 200))
        expect(await listAllCollections(3)).toHaveLength(212)
        expect(mockGet.mock.calls.map(call => call[1]?.params)).toEqual([
            { parent_id: 3, limit: 200, offset: 0 },
            { parent_id: 3, limit: 200, offset: 200 },
        ])
    })

    it('reads the roots with no parent', async () => {
        mockGet.mockResolvedValueOnce(page(5))
        await listAllCollections()
        expect(mockGet.mock.calls[0][1]?.params).toEqual({ parent_id: undefined, limit: 200, offset: 0 })
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
    it('sends the share token a page is read with, and none without one', async () => {
        mockGet.mockResolvedValue({ data: {} })
        await browseDataset('abc', { sort: 'name' }, undefined, 'shared-token')
        await browseDataset('abc', { sort: 'name' })
        expect((mockGet.mock.calls[0][1]?.params as URLSearchParams).get('share_token')).toBe('shared-token')
        expect((mockGet.mock.calls[1][1]?.params as URLSearchParams).has('share_token')).toBe(false)
    })

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

describe('listTags', () => {
    it('sends the search and leaves out an empty one', async () => {
        mockGet.mockResolvedValue({ data: [] })
        await listTags({ q: 'norm', limit: 20 })
        await listTags({ q: '' })
        expect(mockGet.mock.calls[0]).toEqual([
            '/api/v1/library/tags/',
            { params: { q: 'norm', parent_id: undefined, limit: 20, offset: undefined } },
        ])
        expect(mockGet.mock.calls[1][1]?.params).toEqual({
            q: undefined,
            parent_id: undefined,
            limit: undefined,
            offset: undefined,
        })
    })
})

describe('tagging one object', () => {
    it('names the recording by its hash in the body when tagging', async () => {
        mockPost.mockResolvedValueOnce({ data: {} })
        await tagObject(4, 42, 'ab'.repeat(16))
        expect(mockPost).toHaveBeenCalledWith('/api/v1/library/tags/4/items/', {
            content_type_id: 42,
            object_id: 'ab'.repeat(16),
        })
    })

    it('names the recording by its hash in the query when untagging', async () => {
        mockDelete.mockResolvedValueOnce({ data: { status: 'ok' } })
        await untagObject(4, 42, 'ab'.repeat(16))
        expect(mockDelete).toHaveBeenCalledWith('/api/v1/library/tags/4/items/', {
            params: { content_type_id: 42, object_id: 'ab'.repeat(16) },
        })
    })
})

describe('batch tagging', () => {
    const FILTERS: DatasetBrowseFilters = {
        modality: ['eeg'],
        subject: { sex: ['female'] },
        ranges: { age_years: { max: 12 } },
        tags: [3],
    }

    /** The parameters *call* sent, as name and value pairs in order. */
    function sent (call: unknown[]) {
        return [...((call[call.length - 1] as { params: URLSearchParams }).params)]
    }

    it('sends the filters the browser sends', async () => {
        mockGet.mockResolvedValueOnce({ data: {} })
        mockPost.mockResolvedValueOnce({ data: {} })
        mockDelete.mockResolvedValueOnce({ data: {} })
        await browseDataset('abc', {}, FILTERS)
        await tagDatasetMembers('abc', 9, FILTERS)
        await untagDatasetMembers('abc', 9, FILTERS)
        const browsed = sent(mockGet.mock.calls[0])
        expect(browsed).toEqual([
            ['modality', 'eeg'],
            ['subject.sex', 'female'],
            ['subject.age_years.max', '12'],
            ['tag', '3'],
        ])
        expect(mockPost.mock.calls[0][0]).toBe('/api/v1/library/datasets/abc/tags/9/')
        expect(mockPost.mock.calls[0][1]).toBeNull()
        expect(sent(mockPost.mock.calls[0])).toEqual(browsed)
        expect(mockDelete.mock.calls[0][0]).toBe('/api/v1/library/datasets/abc/tags/9/')
        expect(sent(mockDelete.mock.calls[0])).toEqual(browsed)
    })

    it('sends no parameters without filters, which covers every member', async () => {
        mockPost.mockResolvedValueOnce({ data: {} })
        await tagDatasetMembers('abc', 9)
        expect(sent(mockPost.mock.calls[0])).toEqual([])
    })
})

describe('public listing', () => {
    it('lists through the access right named, and clears with a delete', async () => {
        mockPut.mockResolvedValueOnce({ data: {} })
        mockDelete.mockResolvedValueOnce({ data: {} })
        await setPublicListing('abc', 12)
        await clearPublicListing('abc')
        expect(mockPut).toHaveBeenCalledWith('/api/v1/library/datasets/abc/public-listing/', { access_right_id: 12 })
        expect(mockDelete).toHaveBeenCalledWith('/api/v1/library/datasets/abc/public-listing/')
    })
})

describe('orderDatasetItem', () => {
    it('names the item by its id and sends the action alone', async () => {
        const answer = { changed: 2, folder_id: null, items: [{ id: 7, position: 1 }, { id: 4, position: 2 }] }
        mockPost.mockResolvedValueOnce({ data: answer })
        expect(await orderDatasetItem('abc', 7, 'up')).toEqual(answer)
        expect(mockPost).toHaveBeenCalledWith('/api/v1/library/datasets/abc/items/7/order', { action: 'up' })
    })
})

describe('browser query in a link', () => {
    const filters: DatasetBrowseFilters = {
        modality: ['eeg'],
        subject: { age_band: ['0-17', '65+'] },
        ranges: { age_years: { min: 2, max: 10.5 } },
        tags: [4],
    }

    it('round-trips the filters through the query parameters', () => {
        const query = browseFiltersToQuery(filters)
        expect(query).toEqual({
            'modality': ['eeg'],
            'subject.age_band': ['0-17', '65+'],
            'subject.age_years.min': ['2'],
            'subject.age_years.max': ['10.5'],
            'tag': ['4'],
        })
        expect(browseFiltersFromQuery({ ...query, dataset: 'abc', sort: '-duration', token: 't' })).toEqual(filters)
    })

    it('reads a query carrying no filters as none', () => {
        expect(browseFiltersToQuery({ modality: [], subject: {}, ranges: {}, tags: [] })).toEqual({})
        expect(browseFiltersFromQuery({ dataset: 'abc', sort: 'name', 'subject.age_years.min': 'x' })).toBeNull()
    })

    it('reads every page under the sort, in the order the server lists them', async () => {
        const rows = (from: number, count: number) => Array.from({ length: count }, (_, i) => ({ hash: `h${from + i}` }))
        const pages: Record<string, unknown> = {
            0: { count: 403, items: rows(0, 200) },
            200: { count: 403, items: rows(200, 200) },
            400: { count: 403, items: rows(400, 3) },
        }
        mockGet.mockImplementation((_url: string, config?: { params?: URLSearchParams }) => {
            return Promise.resolve({ data: pages[config?.params?.get('offset') ?? ''] })
        })
        const listed = await listMatchingRecordings('abc', null, '-duration', 'tok')
        expect(listed.map(row => row.hash)).toEqual(Array.from({ length: 403 }, (_, i) => `h${i}`))
        expect(mockGet).toHaveBeenCalledTimes(3)
        for (const [, config] of mockGet.mock.calls) {
            const params = config?.params as URLSearchParams
            expect(params.get('sort')).toBe('-duration')
            expect(params.get('share_token')).toBe('tok')
        }
    })

    it('reads the later pages a few at a time and keeps a recording that moved between them once', async () => {
        let open = 0
        let most = 0
        mockGet.mockImplementation(async (_url: string, config?: { params?: URLSearchParams }) => {
            const offset = Number(config?.params?.get('offset'))
            open += 1
            most = Math.max(most, open)
            await new Promise(resolve => setTimeout(resolve, 0))
            open -= 1
            // Every page repeats the first row of the one before it, as a member moving down between reads would.
            const items = Array.from({ length: 200 }, (_, i) => ({ hash: `h${Math.max(offset + i - 1, 0)}` }))
            return { data: { count: 2000, items } }
        })
        const listed = await listMatchingRecordings('abc', null, 'name')
        expect(mockGet).toHaveBeenCalledTimes(10)
        expect(most).toBeLessThanOrEqual(4)
        expect(new Set(listed.map(row => row.hash)).size).toBe(listed.length)
    })

    it('reads one page when the first holds every match', async () => {
        mockGet.mockResolvedValueOnce({ data: { count: 2, items: [{ hash: 'h0' }, { hash: 'h1' }] } })
        expect((await listMatchingRecordings('abc', null, 'name')).map(row => row.hash)).toEqual(['h0', 'h1'])
        expect(mockGet).toHaveBeenCalledTimes(1)
    })
})
