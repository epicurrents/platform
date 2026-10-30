// @vitest-environment node
import { describe, expect, it } from 'vitest'
import type { UploadTree } from '#composables/useFileTree'
import { pairHypnograms, readHeaderFacts } from './hypnogramPairing'

const SUBJECT = 'X F X Female_33yr'
const START = ['24.04.89', '16.13.00'] as const

function header ({
    labels,
    start = START,
    subject = SUBJECT,
    version = '0',
}: {
    labels: string[]
    start?: readonly [string, string]
    subject?: string
    version?: string
}) {
    const fixed = version.padEnd(8) + subject.padEnd(80) + ''.padEnd(80) + start[0].padEnd(8) + start[1].padEnd(8)
        + '512'.padEnd(8) + 'EDF+C'.padEnd(44) + '1'.padEnd(8) + '0'.padEnd(8) + String(labels.length).padEnd(4)
    const signals = labels.map(label => label.padEnd(16)).join('').padEnd(labels.length*256)
    return fixed + signals
}

const study = (name: string, options: Partial<Parameters<typeof header>[0]> = {}) => {
    return new File([header({ labels: ['EEG C4-M1', 'EDF Annotations'], ...options })], name)
}
const scoring = (name: string, options: Partial<Parameters<typeof header>[0]> = {}) => {
    return new File([header({ labels: ['EDF Annotations'], ...options })], name)
}
const flat = (...files: File[]): UploadTree => ({
    rootFiles: files.map(file => ({ file, relativePath: file.name })),
    rootFolders: [],
})

describe('readHeaderFacts', () => {
    it('tells a hypnogram from a recording by its signals', async () => {
        expect((await readHeaderFacts(scoring('a.edf')))?.annotationOnly).toBe(true)
        expect((await readHeaderFacts(study('a.edf')))?.annotationOnly).toBe(false)
    })

    it('reads a BDF header', async () => {
        const bytes = new TextEncoder().encode(header({ labels: ['BDF Annotations'], version: 'xBIOSEMI' }))
        bytes[0] = 0xff
        expect((await readHeaderFacts(new File([bytes], 'a.bdf')))?.annotationOnly).toBe(true)
    })

    it.each([
        ['another format', new File(['time,value\n'], 'a.csv')],
        ['a short file', new File(['0'], 'a.edf')],
        ['another version', study('a.edf', { version: '9' })],
    ])('passes over %s', async (_, file) => {
        expect(await readHeaderFacts(file)).toBeNull()
    })
})

describe('pairHypnograms', () => {
    it('pairs by header though the names share no stem', async () => {
        const psg = study('SC4001E0-PSG.edf')
        const hypnogram = scoring('SC4001EC-Hypnogram.edf')
        const pairing = await pairHypnograms(flat(psg, hypnogram))
        expect(pairing.hypnograms.get(psg)).toBe(hypnogram)
        expect(pairing.tree.rootFiles.map(leaf => leaf.file)).toEqual([psg])
        expect(pairing.unpairedCount).toBe(0)
    })

    it('gives each night its own', async () => {
        const later = ['25.04.89', '16.13.00'] as const
        const [first, second] = [study('a.edf'), study('b.edf', { start: later })]
        const [firstScoring, secondScoring] = [scoring('c.edf'), scoring('d.edf', { start: later })]
        const { hypnograms } = await pairHypnograms(flat(first, second, firstScoring, secondScoring))
        expect(hypnograms.get(first)).toBe(firstScoring)
        expect(hypnograms.get(second)).toBe(secondScoring)
    })

    it('does not pair another subject, and leaves the file out', async () => {
        const psg = study('a.edf')
        const pairing = await pairHypnograms(flat(psg, scoring('b.edf', { subject: 'X M X Male_60yr' })))
        expect(pairing.hypnograms.size).toBe(0)
        expect(pairing.unpairedCount).toBe(1)
        expect(pairing.tree.rootFiles.map(leaf => leaf.file)).toEqual([psg])
    })

    it('does not guess between two matching studies', async () => {
        const pairing = await pairHypnograms(flat(study('a.edf'), study('b.edf'), scoring('c.edf')))
        expect(pairing.hypnograms.size).toBe(0)
        expect(pairing.unpairedCount).toBe(1)
    })

    it('does not guess between two scorers of one night', async () => {
        const pairing = await pairHypnograms(flat(study('a.edf'), scoring('b.edf'), scoring('c.edf')))
        expect(pairing.hypnograms.size).toBe(0)
        expect(pairing.unpairedCount).toBe(2)
    })

    it('counts a hypnogram with an unreadable start as unpaired', async () => {
        const pairing = await pairHypnograms(flat(study('a.edf'), scoring('b.edf', { start: ['xx', 'yy'] })))
        expect(pairing.unpairedCount).toBe(1)
        expect(pairing.tree.rootFiles).toHaveLength(1)
    })

    it('stays inside a folder, and drops a folder left empty', async () => {
        const [psg, match, stray] = [study('a.edf'), scoring('b.edf'), scoring('c.edf')]
        const pairing = await pairHypnograms({
            rootFiles: [],
            rootFolders: [
                {
                    name: 'night',
                    files: [{ file: psg, relativePath: 'a.edf' }, { file: match, relativePath: 'b.edf' }],
                    subfolders: [],
                },
                { name: 'other', files: [{ file: stray, relativePath: 'c.edf' }], subfolders: [] },
            ],
        })
        expect(pairing.hypnograms.get(psg)).toBe(match)
        expect(pairing.unpairedCount).toBe(1)
        expect(pairing.tree.rootFolders.map(folder => folder.name)).toEqual(['night'])
    })

    it('leaves other files where they were', async () => {
        const notes = new File(['time,value\n'], 'a.csv')
        const pairing = await pairHypnograms(flat(notes))
        expect(pairing.tree.rootFiles.map(leaf => leaf.file)).toEqual([notes])
    })
})
