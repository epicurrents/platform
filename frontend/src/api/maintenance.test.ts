/**
 * The package file classifier: which of a selection's files is the archive, the manifest and the signature.
 *
 * The packager writes `<name>.tar.gz`, `<name>.tar.gz.manifest.json` and
 * `<name>.tar.gz.manifest.sig`, and the upload form lets all three be picked
 * from one file input, so the sort by suffix is what stands between a user
 * and a form that silently sends the manifest as the archive.
 */

import { describe, expect, it, vi } from 'vitest'

vi.mock('#lib/http', () => ({
    http: {
        get: vi.fn(() => Promise.resolve({ data: {} })),
        post: vi.fn(() => Promise.resolve({ data: {} })),
    },
}))
vi.mock('#i18n', () => ({ t: (key: string) => key }))

import {
    abandonJob,
    classifyPackageFiles,
    createJob,
    erasureConflict,
    fetchLock,
    rollbackJob,
    verifyJob,
} from '#api/maintenance'
import { http } from '#lib/http'
import { stepUpBody } from '#lib/stepUp'

const mockPost = vi.mocked(http.post)
const mockGet = vi.mocked(http.get)

function file(name: string): File {
    return new File(['x'], name)
}

describe('classifyPackageFiles', () => {
    it('sorts the three files the packager writes by suffix', () => {
        const parts = classifyPackageFiles([
            file('epicurrents-somecourse-0.1.2.tar.gz.manifest.sig'),
            file('epicurrents-somecourse-0.1.2.tar.gz'),
            file('epicurrents-somecourse-0.1.2.tar.gz.manifest.json'),
        ])
        expect(parts.package?.name).toBe('epicurrents-somecourse-0.1.2.tar.gz')
        expect(parts.manifest?.name).toBe('epicurrents-somecourse-0.1.2.tar.gz.manifest.json')
        expect(parts.signature?.name).toBe('epicurrents-somecourse-0.1.2.tar.gz.manifest.sig')
    })

    it('never mistakes the manifest for the archive, whatever the order', () => {
        const parts = classifyPackageFiles([file('a.tar.gz.manifest.json'), file('a.tar.gz')])
        expect(parts.package?.name).toBe('a.tar.gz')
        expect(parts.manifest?.name).toBe('a.tar.gz.manifest.json')
        expect(parts.signature).toBeUndefined()
    })

    it('leaves out what it cannot place and keeps the last of a kind', () => {
        const parts = classifyPackageFiles([file('notes.txt'), file('old.tgz'), file('new.tar.gz')])
        expect(parts.package?.name).toBe('new.tar.gz')
        expect(parts.manifest).toBeUndefined()
        expect(Object.keys(parts)).toEqual(['package'])
    })

    it('ignores case in the suffix', () => {
        const parts = classifyPackageFiles([file('A.TAR.GZ.MANIFEST.SIG')])
        expect(parts.signature?.name).toBe('A.TAR.GZ.MANIFEST.SIG')
    })
})

describe('confirmations', () => {
    it('verify sends the code an account without a password confirms with', async () => {
        mockPost.mockClear()
        await verifyJob('j1', stepUpBody({ password: '', totp_code: '123456' }))
        expect(mockPost).toHaveBeenCalledWith('/api/v1/maintenance/jobs/j1/verify', { totp_code: '123456' })
    })

    it('verify sends both when both were typed', async () => {
        mockPost.mockClear()
        await verifyJob('j1', stepUpBody({ password: 'pw', totp_code: '123456' }))
        expect(mockPost.mock.calls[0]?.[1]).toEqual({ password: 'pw', totp_code: '123456' })
    })

    it('rollback and job creation carry the erasure acknowledgement when given', async () => {
        mockPost.mockClear()
        await rollbackJob('j1', { password: 'pw', acknowledge_erasures: true })
        await createJob({ operation: 'platform.rollback', args: {}, password: 'pw', acknowledge_erasures: true })
        expect(mockPost.mock.calls[0]?.[1]).toEqual({ password: 'pw', acknowledge_erasures: true })
        expect(mockPost.mock.calls[1]?.[1]).toMatchObject({ acknowledge_erasures: true })
    })

    it('abandon posts the step-up credentials to its own route', async () => {
        mockPost.mockClear()
        await abandonJob('j1', { password: 'pw' })
        expect(mockPost).toHaveBeenCalledWith('/api/v1/maintenance/jobs/j1/abandon', { password: 'pw' })
    })

    it('the lock probe is the public lock route', async () => {
        mockGet.mockClear()
        await fetchLock()
        expect(mockGet).toHaveBeenCalledWith('/api/v1/maintenance/lock')
    })
})

describe('erasureConflict', () => {
    it('reads the count off the erasure 409 and nothing else', () => {
        const conflict = {
            response: { status: 409, data: { reason: 'erasures_since_snapshot', erasures: 3, detail: 'x' } },
        }
        expect(erasureConflict(conflict)).toBe(3)
        expect(erasureConflict({ response: { status: 409, data: { detail: 'Another job is in flight.' } } })).toBeNull()
        expect(erasureConflict({ response: { status: 400, data: { reason: 'erasures_since_snapshot' } } })).toBeNull()
        expect(erasureConflict(new Error('offline'))).toBeNull()
    })
})
