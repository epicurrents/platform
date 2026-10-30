import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { uploadRecording } from '#api/recordings'
import { showToast } from '#lib/toast'
import { useRecordingsStore } from './recordings'

vi.mock('#api/recordings', () => ({
    getRecordingStatus: vi.fn(),
    listRecordings: vi.fn(),
    uploadRecording: vi.fn(),
}))
vi.mock('#api/library', () => ({
    addCollectionItem: vi.fn(),
    createCollection: vi.fn(),
    getRecordingContentTypeId: vi.fn().mockResolvedValue(1),
}))
vi.mock('#lib/toast', () => ({ showToast: vi.fn() }))

const mockUpload = vi.mocked(uploadRecording)

/** The hypnogram each uploaded file was sent with, by the name it was uploaded under. */
const sent = () => new Map(mockUpload.mock.calls.map(([file, , options]) => [file.name, options?.hypnogram]))

describe('uploading studies with their hypnograms', () => {
    beforeEach(() => {
        setActivePinia(createPinia())
        vi.clearAllMocks()
        // A refusal ends the leaf before it starts polling, which is all these tests need of the server.
        mockUpload.mockRejectedValue({ response: { data: { detail: 'Refused.' } } })
    })

    it('sends each study its own in a flat batch', async () => {
        const [study, other, scoring] = [new File(['a'], 'a.edf'), new File(['b'], 'b.edf'), new File(['c'], 'c.edf')]
        await useRecordingsStore().uploadBatch(
            [{ file: study, relativePath: 'a.edf' }, { file: other, relativePath: 'b.edf' }],
            { hypnograms: new Map([[study, scoring]]) },
        )
        expect(sent().get('a.edf')).toBe(scoring)
        expect(sent().get('b.edf')).toBeUndefined()
    })

    it('keeps a hypnogram with its study through a name collision', async () => {
        // Flattening a folder renames one of two same-named files, which makes it a new File.
        const [first, second, scoring] = [new File(['a'], 'a.edf'), new File(['b'], 'a.edf'), new File(['c'], 'c.edf')]
        await useRecordingsStore().uploadTree(
            {
                rootFiles: [],
                rootFolders: [
                    { name: 'one', files: [{ file: first, relativePath: 'a.edf' }], subfolders: [] },
                    { name: 'two', files: [{ file: second, relativePath: 'a.edf' }], subfolders: [] },
                ],
            },
            { hypnograms: new Map([[second, scoring]]) },
        )
        const scored = [...sent()].filter(([, hypnogram]) => hypnogram === scoring)
        expect(scored).toHaveLength(1)
        expect(scored[0]![0]).not.toBe('a.edf')
    })

    it('reaches a study in a preserved folder', async () => {
        const [study, scoring] = [new File(['a'], 'a.edf'), new File(['c'], 'c.edf')]
        await useRecordingsStore().uploadTree(
            {
                rootFiles: [],
                rootFolders: [{ name: 'night', files: [{ file: study, relativePath: 'a.edf' }], subfolders: [] }],
            },
            { hypnograms: new Map([[study, scoring]]), preserveFolderHierarchy: true },
        )
        expect(sent().get('a.edf')).toBe(scoring)
    })

    it('shows the reason a file was refused', async () => {
        await useRecordingsStore().uploadBatch([{ file: new File(['a'], 'a.edf'), relativePath: 'a.edf' }])
        expect(showToast).toHaveBeenCalledWith('Refused.', 'danger')
    })
})
