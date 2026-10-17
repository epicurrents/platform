/**
 * The policy the finding picker is handed: none when the server registers no `hed` vocabulary, otherwise the pinned
 * release and the accepted branches as the viewer reads them.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('#lib/http', () => ({
    http: {
        get: vi.fn(),
    },
}))

import { getHedScorePolicy } from './api'
import { http } from '#lib/http'

const mockGet = vi.mocked(http.get)

const POLICY = {
    library: 'score',
    version: '2.1.0',
    with_standard: '8.4.0',
    sha256: 'a'.repeat(64),
    allowed_branches: ['Episode', 'Property'],
}

beforeEach(() => {
    mockGet.mockReset()
})

describe('getHedScorePolicy', () => {
    it('is null when the server takes no HED codes, without asking the plugin', async () => {
        mockGet.mockResolvedValueOnce({
            data: [{ standard: 'epicurrents.eeg', label: 'EEG', version: '2.0', closed: true }],
        })
        expect(await getHedScorePolicy()).toBeNull()
        expect(mockGet).toHaveBeenCalledTimes(1)
    })

    it('reads the pin and the branches', async () => {
        mockGet
            .mockResolvedValueOnce({ data: [{ standard: 'hed', label: 'HED', version: '2.1.0', closed: false }] })
            .mockResolvedValueOnce({ data: POLICY })
        expect(await getHedScorePolicy()).toEqual({
            allowedBranches: ['Episode', 'Property'],
            schema: { library: 'score', sha256: POLICY.sha256, version: '2.1.0', withStandard: '8.4.0' },
        })
        expect(mockGet).toHaveBeenLastCalledWith('/plugin/hedscore/api/v1/policy')
    })

    it('rejects when a read fails', async () => {
        mockGet.mockRejectedValueOnce(new Error('offline'))
        await expect(getHedScorePolicy()).rejects.toThrow('offline')
    })
})
