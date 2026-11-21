/**
 * The dicom plugin's API client: the study link it hands to OHIF, which must name a mode route and the dicomjson
 * data source or OHIF opens its worklist instead of the study, and the upload's burned-in confirmation flag.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('#lib/http', () => ({
    http: {
        post: vi.fn(),
    },
}))

import { dicomApi } from './api'
import { http } from '#lib/http'

const mockPost = vi.mocked(http.post)

beforeEach(() => {
    mockPost.mockReset()
    mockPost.mockResolvedValue({ data: { studies: [], files: [], accepted: 0, rejected: 0 } })
})

describe('ohifViewerUrl', () => {
    it('opens the longitudinal mode on the dicomjson source with the study JSON as url', () => {
        const url = new URL(dicomApi.ohifViewerUrl('abc123'), window.location.origin)
        expect(url.pathname).toBe('/plugin/dicom/ohif/viewer/dicomjson')
        expect(url.searchParams.get('url')).toBe(
            `${window.location.origin}/plugin/dicom/api/v1/dicom/studies/abc123/ohif-json/`
        )
    })
})

describe('uploadFiles', () => {
    const files = [new File(['a'], 'a.dcm'), new File(['b'], 'b.dcm')]

    it('sends no confirmation unless asked to', async () => {
        await dicomApi.uploadFiles(files)
        const [url, form, config] = mockPost.mock.calls[0]
        expect(url).toBe('/plugin/dicom/api/v1/dicom/upload/')
        expect((form as FormData).getAll('files')).toHaveLength(2)
        expect(config?.params).toBeUndefined()
    })

    it('sends the burned-in confirmation as a query parameter', async () => {
        await dicomApi.uploadFiles(files, true)
        expect(mockPost.mock.calls[0][2]?.params).toEqual({ confirm_burned_in: true })
    })
})
