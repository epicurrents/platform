/**
 * The dicom plugin's API client: the study link it hands to OHIF, which must name a mode route and the dicomjson
 * data source or OHIF opens its worklist instead of the study, and the upload's burned-in confirmation flag.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('#lib/http', () => ({
    http: {
        delete: vi.fn(),
        get: vi.fn(),
        post: vi.fn(),
    },
}))

import type { GrantAccessPayload } from '#api/library'
import { dicomApi } from './api'
import { http } from '#lib/http'

const mockDelete = vi.mocked(http.delete)
const mockGet = vi.mocked(http.get)
const mockPost = vi.mocked(http.post)

beforeEach(() => {
    mockDelete.mockReset()
    mockGet.mockReset()
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

describe('study paths', () => {
    const hostile = '../../../../api/v1/user/me'

    it('keep a hash that is not one inside its path segment', async () => {
        mockGet.mockResolvedValue({ data: {} })
        await dicomApi.getStudy(hostile)
        await dicomApi.listAccess(hostile)
        await dicomApi.deleteStudy(hostile)
        await dicomApi.revokeAccess(hostile, 7)
        await dicomApi.grantAccess(hostile, {} as GrantAccessPayload)
        const escaped = encodeURIComponent(hostile)
        expect(mockGet.mock.calls.map(call => call[0])).toEqual([
            `/plugin/dicom/api/v1/dicom/studies/${escaped}/`,
            `/plugin/dicom/api/v1/dicom/studies/${escaped}/access/`,
        ])
        expect(mockDelete.mock.calls.map(call => call[0])).toEqual([
            `/plugin/dicom/api/v1/dicom/studies/${escaped}/`,
            `/plugin/dicom/api/v1/dicom/studies/${escaped}/access/7/`,
        ])
        expect(mockPost.mock.calls[0][0]).toBe(`/plugin/dicom/api/v1/dicom/studies/${escaped}/access/`)
    })

    it('hand OHIF a study JSON address that stays under the studies route', () => {
        const url = new URL(dicomApi.ohifViewerUrl(hostile), window.location.origin)
        const json = new URL(url.searchParams.get('url')!)
        expect(json.pathname).toBe(`/plugin/dicom/api/v1/dicom/studies/${encodeURIComponent(hostile)}/ohif-json/`)
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
