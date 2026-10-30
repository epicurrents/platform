/**
 * Pairing hypnogram files with their studies in a browser batch.
 *
 * Each upload is its own request, so a hypnogram has to be matched to its study before either is sent. The rule is the
 * one `pair_hypnograms` applies in `recordings/hypnogram.py` for an import: a file holding only annotations pairs with
 * the one study in its folder that has the same subject field and start time, and anything less than one-to-one is left
 * unpaired rather than guessed at, since a study scored by the wrong file reads as a finding about the wrong night.
 * The server re-reads and re-aligns every pair, so this decides what is sent and nothing more. The headers are read
 * here in the browser and only the matched files leave it.
 */

import { pruneEmptyFolders, type FolderNode, type UploadTree } from '#composables/useFileTree'

const FIXED_HEADER_BYTES = 256
const SIGNAL_LABEL_BYTES = 16
const SIGNAL_HEADER_BYTES = 256
const EDF_EXTENSIONS = /\.(bdf|edf)$/i
const HEADER_DATE = /^\d\d\.\d\d\.\d\d$/
const ANNOTATION_LABELS = new Set(['bdf annotations', 'edf annotations'])

/** A batch sorted for upload. */
export interface HypnogramPairing {
    /** The batch without its hypnograms, paired or not: a hypnogram is never a recording of its own. */
    tree: UploadTree
    /** The hypnogram of each paired study, keyed by the study's file. */
    hypnograms: Map<File, File>
    /** Hypnograms that could not be matched to exactly one study; these are not uploaded. */
    unpairedCount: number
}

interface HeaderFacts {
    annotationOnly: boolean
    /** The subject field and start time, or null when the start time cannot be read. */
    identity: string | null
}

const text = (bytes: Uint8Array, start: number, length: number) => {
    return String.fromCharCode(...bytes.subarray(start, start + length)).trim()
}

/** Read what pairing needs from the header of an EDF or BDF file, or null when it is not one. */
export async function readHeaderFacts (file: File): Promise<HeaderFacts | null> {
    if (!EDF_EXTENSIONS.test(file.name)) {
        return null
    }
    const head = new Uint8Array(await file.slice(0, FIXED_HEADER_BYTES).arrayBuffer())
    if (head.length < FIXED_HEADER_BYTES) {
        return null
    }
    const isBdf = head[0] === 0xff && text(head, 1, 7) === 'BIOSEMI'
    if (!isBdf && text(head, 0, 8) !== '0') {
        return null
    }
    const signalCount = Number.parseInt(text(head, 252, 4), 10)
    if (!(signalCount > 0)) {
        return null
    }
    const labels = new Uint8Array(await file.slice(
        FIXED_HEADER_BYTES, FIXED_HEADER_BYTES + signalCount*SIGNAL_HEADER_BYTES
    ).arrayBuffer())
    let annotationOnly = true
    for (let signal = 0; signal < signalCount; signal++) {
        const label = text(labels, signal*SIGNAL_LABEL_BYTES, SIGNAL_LABEL_BYTES)
        if (!ANNOTATION_LABELS.has(label.toLowerCase())) {
            annotationOnly = false
            break
        }
    }
    const date = text(head, 168, 8)
    const time = text(head, 176, 8)
    const dated = HEADER_DATE.test(date) && HEADER_DATE.test(time)
    return { annotationOnly, identity: dated ? `${text(head, 8, 80)}|${date}|${time}` : null }
}

/** Every file of `tree` with the path of its folder, which is what confines a pair to one folder. */
function listFiles (tree: UploadTree) {
    const out: { file: File; folder: string }[] = tree.rootFiles.map(leaf => ({ file: leaf.file, folder: '' }))
    const walk = (node: FolderNode, parent: string) => {
        const folder = `${parent}/${node.name}`
        out.push(...node.files.map(leaf => ({ file: leaf.file, folder })))
        node.subfolders.forEach(sub => walk(sub, folder))
    }
    tree.rootFolders.forEach(node => walk(node, ''))
    return out
}

function withoutFiles (node: FolderNode, drop: Set<File>): FolderNode {
    return {
        ...node,
        files: node.files.filter(leaf => !drop.has(leaf.file)),
        subfolders: node.subfolders.map(sub => withoutFiles(sub, drop)),
    }
}

/**
 * Sort a batch into studies and their hypnograms.
 * @param tree - The batch as selected.
 * @returns The batch to upload, the pairs, and how many hypnograms were left unpaired.
 */
export async function pairHypnograms (tree: UploadTree): Promise<HypnogramPairing> {
    const files = listFiles(tree)
    const facts = await Promise.all(files.map(entry => readHeaderFacts(entry.file).catch(() => null)))

    const studies = new Map<string, File[]>()
    const claims = new Map<string, File[]>()
    const drop = new Set<File>()
    let unpairedCount = 0
    files.forEach(({ file, folder }, index) => {
        const fact = facts[index]
        if (fact?.annotationOnly) {
            drop.add(file)
        }
        if (!fact?.identity) {
            unpairedCount += fact?.annotationOnly ? 1 : 0
            return
        }
        const target = fact.annotationOnly ? claims : studies
        const key = `${folder}|${fact.identity}`
        target.set(key, [...(target.get(key) ?? []), file])
    })

    const hypnograms = new Map<File, File>()
    for (const [key, claimants] of claims) {
        const matches = studies.get(key) ?? []
        if (claimants.length === 1 && matches.length === 1) {
            hypnograms.set(matches[0]!, claimants[0]!)
        } else {
            unpairedCount += claimants.length
        }
    }
    return {
        hypnograms,
        tree: pruneEmptyFolders({
            rootFiles: tree.rootFiles.filter(leaf => !drop.has(leaf.file)),
            rootFolders: tree.rootFolders.map(node => withoutFiles(node, drop)),
        }),
        unpairedCount,
    }
}
