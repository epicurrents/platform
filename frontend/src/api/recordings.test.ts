import { beforeEach, describe, expect, it, vi } from 'vitest'
import { http } from '#lib/http'
import { ATTACHMENT_LISTING_LIMIT, listRecordingAttachments, uploadRecording } from './recordings'

vi.mock('#lib/http', () => ({ http: { get: vi.fn(), post: vi.fn() } }))

const mockPost = vi.mocked(http.post)
const file = new File(['a'], 'a.edf')

/** The body and the config of the upload request. */
const request = () => {
    const [, body, config] = mockPost.mock.calls[0]!
    return { body: body as FormData, params: config?.params as Record<string, unknown> }
}

describe('uploadRecording', () => {
    beforeEach(() => {
        vi.clearAllMocks()
        mockPost.mockResolvedValue({ data: {} })
    })

    it('names the channel map in the query', async () => {
        await uploadRecording(new File(['x'], 'a.edf'), undefined, { channelMap: 'source' })
        expect(mockPost.mock.calls[0][2]?.params).toEqual({ channel_map: 'source' })
    })

    it('sends the flags where the endpoint binds them', async () => {
        await uploadRecording(file, undefined, { modality: 'psg', preserveAnnotations: true })
        expect(request().params).toEqual({ modality: 'psg', preserve_annotations: true })
        expect(request().body.has('preserve_annotations')).toBe(false)
    })

    it('keeps a free-text label out of the URL', async () => {
        await uploadRecording(file, undefined, { displayName: 'Cohort A' })
        expect(request().body.get('display_name')).toBe('Cohort A')
        expect(request().params).toEqual({})
    })

    it('sends a hypnogram beside the study', async () => {
        const hypnogram = new File(['b'], 'b.edf')
        await uploadRecording(file, undefined, { hypnogram })
        expect((request().body.get('hypnogram') as File).name).toBe('b.edf')
    })

    it('leaves out what was not asked for', async () => {
        await uploadRecording(file)
        expect(request().params).toEqual({})
        expect([...request().body.keys()]).toEqual(['file'])
    })
})

describe('listRecordingAttachments', () => {
    const mockGet = vi.mocked(http.get)

    beforeEach(() => {
        vi.clearAllMocks()
        mockGet.mockImplementation((_url, config) => Promise.resolve({
            data: [{ parent: String(config?.params?.recordings).split(',')[0] }],
        }))
    })

    it('names the recordings a page at a time and joins the pages in order', async () => {
        const hashes = Array.from({ length: ATTACHMENT_LISTING_LIMIT + 1 }, (_, i) => `H${i}`)
        const items = await listRecordingAttachments(hashes)
        expect(mockGet).toHaveBeenCalledTimes(2)
        expect(items.map(item => item.parent)).toEqual(['H0', `H${ATTACHMENT_LISTING_LIMIT}`])
    })

    it('passes the share token when there is one', async () => {
        await listRecordingAttachments(['A'], 'tok')
        expect(mockGet.mock.calls[0][1]?.params).toEqual({ recordings: 'A', share_token: 'tok' })
    })
})
