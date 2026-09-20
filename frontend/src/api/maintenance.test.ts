/**
 * The package file classifier: which of a selection's files is the archive, the manifest and the signature.
 *
 * The packager writes `<name>.tar.gz`, `<name>.tar.gz.manifest.json` and
 * `<name>.tar.gz.manifest.sig`, and the upload form lets all three be picked
 * from one file input, so the sort by suffix is what stands between a user
 * and a form that silently sends the manifest as the archive.
 */

import { describe, expect, it, vi } from 'vitest'

vi.mock('#lib/http', () => ({ http: {} }))

import { classifyPackageFiles } from '#api/maintenance'

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
