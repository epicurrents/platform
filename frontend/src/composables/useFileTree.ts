/**
 * useFileTree — normalise all four file/folder input methods into a unified
 * UploadTree structure.
 *
 * Supported input methods:
 *   A. <input multiple>              → flat FileList, no webkitRelativePath
 *   B. <input webkitdirectory>       → FileList with webkitRelativePath set
 *   C. drag-and-drop (files)         → DataTransfer via webkitGetAsEntry
 *   D. drag-and-drop (folders)       → DataTransfer via webkitGetAsEntry, recursive
 *
 * Firefox does not support webkitGetAsEntry for folders; in that case the
 * composable falls back to treating all dragged items as flat files.
 */

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

/** A single uploadable file with its path relative to the tree root. */
export interface FileLeaf {
    /** The native File object ready to pass to the upload API. */
    file: File
    /**
     * Path relative to the nearest containing folder, using "/" separators.
     * Empty string for files at the root of an UploadTree.
     * Example: "artifacts/ecg.edf" for a file two levels deep.
     */
    relativePath: string
}

/** A folder node in the upload tree. */
export interface FolderNode {
    /** Display name of this folder. */
    name: string
    /** Direct children files (not in sub-folders). */
    files: FileLeaf[]
    /** Immediate child folders. */
    subfolders: FolderNode[]
}

/**
 * The top-level tree produced by any of the four input methods.
 *
 * `rootFiles`   — files that were selected/dropped without a containing folder.
 * `rootFolders` — top-level folders (each may contain files and sub-folders).
 */
export interface UploadTree {
    rootFiles: FileLeaf[]
    rootFolders: FolderNode[]
}

// ---------------------------------------------------------------------------
// Internal helpers
// ---------------------------------------------------------------------------

/**
 * Insert a FileLeaf into the correct position in a FolderNode tree, creating
 * intermediate FolderNode entries as needed.
 *
 * @param root      The FolderNode that corresponds to `segments[0]`.
 * @param segments  Remaining path segments *after* the root folder name.
 *                  e.g. ["sub", "file.edf"] means root/sub/file.edf
 * @param leaf      The FileLeaf to insert.
 */
function insertIntoFolder(root: FolderNode, segments: string[], leaf: FileLeaf): void {
    if (segments.length === 1) {
        // Last segment is the file name — place it directly in root.
        root.files.push({ ...leaf, relativePath: segments[0]! })
        return
    }

    // Navigate/create the next folder level.
    const childName = segments[0]!
    let child = root.subfolders.find(f => f.name === childName)
    if (!child) {
        child = { name: childName, files: [], subfolders: [] }
        root.subfolders.push(child)
    }
    insertIntoFolder(child, segments.slice(1), leaf)
}

/**
 * Build an UploadTree from a FileList whose entries have `webkitRelativePath`
 * set (produced by `<input webkitdirectory>`).
 *
 * webkitRelativePath format: "TopFolderName/optional/sub/file.ext"
 * The first segment is stripped so that `rootFolders[0].name` == "TopFolderName".
 */
function treeFromRelativePaths(files: FileList): UploadTree {
    const rootFiles: FileLeaf[] = []
    const folderMap = new Map<string, FolderNode>()

    for (const file of Array.from(files)) {
        const rel = file.webkitRelativePath
        if (!rel) {
            // No relative path — treat as root file.
            rootFiles.push({ file, relativePath: file.name })
            continue
        }

        const segments = rel.split('/')
        if (segments.length === 1) {
            // Shouldn't happen with webkitdirectory, but handle gracefully.
            rootFiles.push({ file, relativePath: file.name })
            continue
        }

        const topName = segments[0]!
        if (!folderMap.has(topName)) {
            folderMap.set(topName, { name: topName, files: [], subfolders: [] })
        }
        const root = folderMap.get(topName)!
        // segments[1..] are the sub-path within the top folder.
        insertIntoFolder(root, segments.slice(1), { file, relativePath: '' })
    }

    return {
        rootFiles,
        rootFolders: Array.from(folderMap.values()),
    }
}

/**
 * Recursively read all entries from a FileSystemDirectoryReader.
 * `readEntries` returns at most 100 entries per call; we loop until we get [].
 */
async function readAllEntries(reader: FileSystemDirectoryReader): Promise<FileSystemEntry[]> {
    const results: FileSystemEntry[] = []
    while (true) {
        const batch = await new Promise<FileSystemEntry[]>((resolve, reject) =>
            reader.readEntries(resolve, reject),
        )
        if (batch.length === 0) break
        results.push(...batch)
    }
    return results
}

/**
 * Materialise a FileSystemFileEntry into a native File object.
 */
function entryToFile(entry: FileSystemFileEntry): Promise<File> {
    return new Promise((resolve, reject) => entry.file(resolve, reject))
}

/**
 * Recursively traverse a FileSystemDirectoryEntry and build a FolderNode.
 */
async function traverseDirectory(entry: FileSystemDirectoryEntry): Promise<FolderNode> {
    const node: FolderNode = { name: entry.name, files: [], subfolders: [] }
    const reader = entry.createReader()
    const children = await readAllEntries(reader)

    await Promise.all(
        children.map(async child => {
            if (child.isFile) {
                const file = await entryToFile(child as FileSystemFileEntry)
                node.files.push({ file, relativePath: child.name })
            } else if (child.isDirectory) {
                const sub = await traverseDirectory(child as FileSystemDirectoryEntry)
                node.subfolders.push(sub)
            }
        }),
    )

    return node
}

/**
 * Build an UploadTree from a DataTransfer (drag-and-drop event).
 *
 * Uses `webkitGetAsEntry` when available (Chrome, Edge, Safari, Firefox 50+).
 * Falls back to `dataTransfer.files` when entries API is absent.
 */
async function treeFromDataTransfer(dt: DataTransfer): Promise<UploadTree> {
    const rootFiles: FileLeaf[] = []
    const rootFolders: FolderNode[] = []

    const items = Array.from(dt.items)
    const hasEntryApi = items.length > 0 && typeof items[0]!.webkitGetAsEntry === 'function'

    if (!hasEntryApi) {
        // Fallback: flat file list (Firefox < 50 or unusual browsers).
        for (const file of Array.from(dt.files)) {
            rootFiles.push({ file, relativePath: file.name })
        }
        return { rootFiles, rootFolders }
    }

    await Promise.all(
        items.map(async item => {
            const entry = item.webkitGetAsEntry()
            if (!entry) return

            if (entry.isFile) {
                const file = await entryToFile(entry as FileSystemFileEntry)
                rootFiles.push({ file, relativePath: file.name })
            } else if (entry.isDirectory) {
                const folder = await traverseDirectory(entry as FileSystemDirectoryEntry)
                rootFolders.push(folder)
            }
        }),
    )

    return { rootFiles, rootFolders }
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

/**
 * Build an UploadTree from a file `<input>` change event.
 *
 * Handles both:
 *   - `<input multiple>` → rootFiles only, no folders
 *   - `<input webkitdirectory>` → rootFolders built from webkitRelativePath
 */
export function treeFromInputEvent(event: Event): UploadTree {
    const input = event.target as HTMLInputElement
    const files = input.files
    if (!files || files.length === 0) {
        return { rootFiles: [], rootFolders: [] }
    }

    // If any file has a webkitRelativePath the user picked a directory.
    const hasRelativePaths = Array.from(files).some(f => f.webkitRelativePath)
    if (hasRelativePaths) {
        return treeFromRelativePaths(files)
    }

    // Plain multi-file pick — all go to rootFiles.
    return {
        rootFiles: Array.from(files).map(f => ({ file: f, relativePath: f.name })),
        rootFolders: [],
    }
}

/**
 * Build an UploadTree from a drag-and-drop event.
 *
 * Must be called synchronously inside the `drop` event handler
 * (DataTransfer is invalidated after the handler returns), but the
 * returned Promise resolves asynchronously after all folder traversal
 * is complete.
 */
export function treeFromDropEvent(event: DragEvent): Promise<UploadTree> {
    const dt = event.dataTransfer
    if (!dt) return Promise.resolve({ rootFiles: [], rootFolders: [] })
    return treeFromDataTransfer(dt)
}

// ---------------------------------------------------------------------------
// Utility helpers used by the upload UI
// ---------------------------------------------------------------------------

/** Total number of files across the entire tree (recursive). */
export function countFiles(tree: UploadTree): number {
    return tree.rootFiles.length + tree.rootFolders.reduce((n, f) => n + countFolderFiles(f), 0)
}

function countFolderFiles(folder: FolderNode): number {
    return folder.files.length + folder.subfolders.reduce((n, f) => n + countFolderFiles(f), 0)
}

/** Whether the tree contains any folder nodes (i.e. collections will be created). */
export function hasFolders(tree: UploadTree): boolean {
    return tree.rootFolders.length > 0
}

/** Whether the tree is empty (no files anywhere). */
export function isEmpty(tree: UploadTree): boolean {
    return countFiles(tree) === 0
}

/**
 * Filter an UploadTree, keeping only files whose name matches the given
 * extension list.  Returns a new tree; the original is not modified.
 *
 * @param tree       Source tree.
 * @param extensions Lower-cased extensions including the dot, e.g. ['.edf', '.bdf'].
 * @returns `{ filtered, rejectedCount }` — the pruned tree and how many files
 *          were dropped so the caller can show a warning.
 */
/**
 * Append ` (2)`, ` (3)`, … before the file extension when names collide,
 * skipping any candidate already present in the list so existing
 * "foo (2).edf" entries are not overwritten. Pure function operating on
 * strings — the caller is responsible for wrapping the matching `File`
 * objects in a renamed `new File(...)` before uploading.
 */
export function computeDedupedNames(names: string[]): string[] {
    const used = new Set<string>()
    return names.map(original => {
        if (!used.has(original)) {
            used.add(original)
            return original
        }
        const dotIdx = original.lastIndexOf('.')
        const stem = dotIdx === -1 ? original : original.slice(0, dotIdx)
        const ext = dotIdx === -1 ? '' : original.slice(dotIdx)
        let i = 2
        let candidate = `${stem} (${i})${ext}`
        while (used.has(candidate)) {
            i++
            candidate = `${stem} (${i})${ext}`
        }
        used.add(candidate)
        return candidate
    })
}

/**
 * Drop folders that have no files (recursively) — used after `filterByExtension`
 * so a folder whose every entry was rejected by the extension filter does not
 * stay in the tree and trigger an empty `Collection` creation downstream.
 */
export function pruneEmptyFolders(tree: UploadTree): UploadTree {
    function pruneFolder(folder: FolderNode): FolderNode | null {
        const subfolders = folder.subfolders
            .map(pruneFolder)
            .filter((f): f is FolderNode => f !== null)
        if (folder.files.length === 0 && subfolders.length === 0) {
            return null
        }
        return { name: folder.name, files: folder.files, subfolders }
    }
    return {
        rootFiles: tree.rootFiles,
        rootFolders: tree.rootFolders
            .map(pruneFolder)
            .filter((f): f is FolderNode => f !== null),
    }
}

export function filterByExtension(
    tree: UploadTree,
    extensions: string[],
): { filtered: UploadTree; rejectedCount: number } {
    let rejectedCount = 0

    function filterLeaves(leaves: FileLeaf[]): FileLeaf[] {
        return leaves.filter(leaf => {
            const dot = leaf.file.name.lastIndexOf('.')
            const ext = dot === -1 ? '' : leaf.file.name.slice(dot).toLowerCase()
            if (extensions.includes(ext)) return true
            rejectedCount++
            return false
        })
    }

    function filterFolder(folder: FolderNode): FolderNode {
        return {
            name: folder.name,
            files: filterLeaves(folder.files),
            subfolders: folder.subfolders.map(filterFolder),
        }
    }

    return {
        filtered: {
            rootFiles: filterLeaves(tree.rootFiles),
            rootFolders: tree.rootFolders.map(filterFolder),
        },
        rejectedCount,
    }
}
