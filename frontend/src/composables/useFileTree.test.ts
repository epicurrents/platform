/**
 * Unit tests for the pure-function helpers in useFileTree.ts.
 *
 * Run:  npm test  (from frontend/)
 */

import { describe, it, expect } from 'vitest'
import { computeDedupedNames, pruneEmptyFolders } from './useFileTree'
import type { UploadTree, FolderNode } from './useFileTree'

describe('computeDedupedNames', () => {
    it('leaves unique names untouched', () => {
        expect(computeDedupedNames(['a.edf', 'b.edf', 'c.edf'])).toEqual([
            'a.edf', 'b.edf', 'c.edf',
        ])
    })

    it('appends " (2)" on the first collision', () => {
        expect(computeDedupedNames(['eeg.edf', 'eeg.edf'])).toEqual([
            'eeg.edf', 'eeg (2).edf',
        ])
    })

    it('numbers each subsequent collision', () => {
        expect(computeDedupedNames(['x.edf', 'x.edf', 'x.edf', 'x.edf'])).toEqual([
            'x.edf', 'x (2).edf', 'x (3).edf', 'x (4).edf',
        ])
    })

    it('skips a candidate already present in the input', () => {
        // The second "rec.edf" should not collide with the explicit "rec (2).edf"
        // already in the list — it gets bumped to "rec (3).edf".
        expect(computeDedupedNames(['rec.edf', 'rec (2).edf', 'rec.edf'])).toEqual([
            'rec.edf', 'rec (2).edf', 'rec (3).edf',
        ])
    })

    it('handles files with no extension', () => {
        expect(computeDedupedNames(['notes', 'notes'])).toEqual([
            'notes', 'notes (2)',
        ])
    })

    it('treats only the last dot as the extension separator', () => {
        expect(computeDedupedNames(['archive.tar.gz', 'archive.tar.gz'])).toEqual([
            'archive.tar.gz', 'archive.tar (2).gz',
        ])
    })

    it('is order-sensitive — the first occurrence keeps the bare name', () => {
        expect(computeDedupedNames(['a.edf', 'a (2).edf', 'a.edf', 'a.edf'])).toEqual([
            'a.edf', 'a (2).edf', 'a (3).edf', 'a (4).edf',
        ])
    })

    it('returns an empty array for empty input', () => {
        expect(computeDedupedNames([])).toEqual([])
    })
})

describe('pruneEmptyFolders', () => {
    function leaf(name: string) {
        // Test stub — File is not needed for the tree-shape tests, only the
        // path metadata matters to pruneEmptyFolders.
        return { file: { name } as File, relativePath: name }
    }

    function folder(name: string, files: string[], subfolders: FolderNode[] = []): FolderNode {
        return { name, files: files.map(leaf), subfolders }
    }

    it('keeps folders that have at least one file', () => {
        const tree: UploadTree = {
            rootFiles: [],
            rootFolders: [folder('keep', ['a.edf'])],
        }
        expect(pruneEmptyFolders(tree).rootFolders).toHaveLength(1)
    })

    it('drops a top-level folder that has no files and no subfolders', () => {
        const tree: UploadTree = {
            rootFiles: [],
            rootFolders: [folder('empty', [])],
        }
        expect(pruneEmptyFolders(tree).rootFolders).toHaveLength(0)
    })

    it('drops an empty subfolder but preserves the parent if it has its own files', () => {
        const tree: UploadTree = {
            rootFiles: [],
            rootFolders: [folder('parent', ['root.edf'], [folder('inner', [])])],
        }
        const result = pruneEmptyFolders(tree)
        expect(result.rootFolders).toHaveLength(1)
        expect(result.rootFolders[0]!.subfolders).toHaveLength(0)
    })

    it('drops a parent whose only child is an empty subfolder', () => {
        const tree: UploadTree = {
            rootFiles: [],
            rootFolders: [folder('outer', [], [folder('inner', [])])],
        }
        expect(pruneEmptyFolders(tree).rootFolders).toHaveLength(0)
    })

    it('leaves rootFiles untouched', () => {
        const tree: UploadTree = {
            rootFiles: [leaf('top.edf')],
            rootFolders: [folder('empty', [])],
        }
        const result = pruneEmptyFolders(tree)
        expect(result.rootFiles).toHaveLength(1)
        expect(result.rootFolders).toHaveLength(0)
    })
})
