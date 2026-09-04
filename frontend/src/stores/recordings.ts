import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import {
    listRecordings,
    uploadRecording,
    getRecordingStatus,
    updateRecording,
    type Recording,
    type RecordingUpload,
} from '#api/recordings'
import {
    createCollection,
    addCollectionItem,
    getRecordingContentTypeId,
} from '#api/library'
import { computeDedupedNames } from '#composables/useFileTree'
import type { UploadTree, FolderNode, FileLeaf } from '#composables/useFileTree'

const POLL_INTERVAL_MS = 2000
const POLL_MAX_ATTEMPTS = 60  // 2 minutes
const HOME_POLL_INTERVAL_MS = 5000

/** Maximum number of files uploaded concurrently within a batch or folder. */
const UPLOAD_CONCURRENCY = 3

export type UploadPhase = 'queued' | 'uploading' | 'processing' | 'ready' | 'failed' | 'timeout'

export type UploadState = {
    hash: string
    name: string
    progress: number
    /** 'uploading' | 'processing' | 'ready' | 'failed' | 'timeout' */
    phase: string
}

// ---------------------------------------------------------------------------
// Batch upload types
// ---------------------------------------------------------------------------

/** Per-file status within a batch or tree upload. */
export interface BatchFileState {
    /** Stable key: relative path within the batch (or just the file name). */
    key: string
    name: string
    phase: UploadPhase
    /** 0–100 during the HTTP upload phase; 100 once the file is queued for processing. */
    progress: number
    /** Set after the upload API responds; used to link the item into a collection. */
    recordingObjectId: string | null
    /** Set when the recording reaches ready/failed status. */
    recordingHash: string | null
}

/** A batch upload job (flat files or full folder tree). */
export interface BatchUploadState {
    /** Unique ID for this batch, generated at start time. */
    id: string
    totalFiles: number
    doneFiles: number
    /** Number of Collection rows created so far. */
    collectionsCreated: number
    /** Per-file tracking, keyed by BatchFileState.key. */
    files: Map<string, BatchFileState>
    /** Overall status. */
    phase: 'running' | 'done' | 'partial'
}

export const useRecordingsStore = defineStore('recordings', () => {
    const recordings = ref<Recording[]>([])
    const loading = ref(false)
    const error = ref<string | null>(null)
    const activeUploads = ref<UploadState[]>([])

    /** All active batch (multi-file / folder) upload jobs. */
    const activeBatches = ref<BatchUploadState[]>([])

    /** Id of the batch the active UploadView session is observing. Set by
     *  uploadBatch / uploadTree at start time; consumers should read from
     *  the `currentBatch` getter rather than indexing into activeBatches. */
    const currentBatchId = ref<string | null>(null)

    const currentBatch = computed<BatchUploadState | null>(() => {
        if (currentBatchId.value === null) {
            return null
        }
        return activeBatches.value.find(b => b.id === currentBatchId.value) ?? null
    })

    async function load() {
        loading.value = true
        error.value = null
        try {
            recordings.value = await listRecordings(100)
        } catch (e) {
            error.value = e instanceof Error ? e.message : 'Failed to load recordings'
        } finally {
            loading.value = false
        }
    }

    /** Silently refresh the recordings list without showing the loading spinner. */
    async function refresh() {
        try {
            recordings.value = await listRecordings(100)
        } catch {
            // Ignore transient poll errors
        }
    }

    let _homePollTimer: ReturnType<typeof setInterval> | null = null

    function startPolling(intervalMs = HOME_POLL_INTERVAL_MS) {
        stopPolling()
        _homePollTimer = setInterval(refresh, intervalMs)
    }

    function stopPolling() {
        if (_homePollTimer !== null) {
            clearInterval(_homePollTimer)
            _homePollTimer = null
        }
    }

    /**
     * Upload a single file and poll until processing completes or fails.
     *
     * Calls onProgress(percent) during the HTTP upload phase (0-100).
     * Calls onDone(phase) when the background worker finishes; phase is
     * 'ready', 'failed', or 'timeout'.
     *
     * Returns the upload response (containing stored_name / hash) so the
     * caller can show immediate feedback before polling finishes.
     */
    async function upload(
        file: File,
        onProgress?: (percent: number) => void,
        onDone?: (phase: 'ready' | 'failed' | 'timeout') => void,
    ): Promise<RecordingUpload> {
        const result = await uploadRecording(file, onProgress)
        const hash = result.stored_name.split('.')[0] ?? result.stored_name

        const state: UploadState = { hash, name: file.name, progress: 100, phase: 'processing' }
        activeUploads.value.push(state)

        // Poll in the background — do not await so the caller returns immediately.
        _pollStatus(hash, state, onDone)

        return result
    }

    // ---------------------------------------------------------------------------
    // Batch upload — flat list of files (no collections created)
    // ---------------------------------------------------------------------------

    /**
     * Upload multiple individual files concurrently (no collection structure).
     *
     * Returns a BatchUploadState ref that updates reactively as files progress.
     * The batch is automatically removed from `activeBatches` once all files
     * have reached a terminal phase.
     */
    async function uploadBatch(
        files: FileLeaf[],
        options?: {
            parentCollectionId?: number | null
            modality?: string
            preserveAnnotations?: boolean
            /** Grantee-visible label for a single-file upload; ignored when uploading many. */
            displayName?: string
        },
    ): Promise<BatchUploadState> {
        const batch = _makeBatch(files.map(f => f.relativePath || f.file.name))
        activeBatches.value.push(batch)
        currentBatchId.value = batch.id
        // Use the reactive proxy from the array — mutations on the plain `batch`
        // object won't trigger Vue reactivity, so the UI won't update.
        const reactiveBatch = activeBatches.value[activeBatches.value.length - 1] as BatchUploadState

        let recordingCtId: number | null = null
        const collId = options?.parentCollectionId ?? null
        if (collId !== null) {
            try {
                recordingCtId = await getRecordingContentTypeId()
            } catch {
                // Collection linking will silently skip.
            }
        }

        // A display name only makes sense for a single-file upload; with many files
        // it would collide, so it is applied only when exactly one file is uploaded.
        const displayName = files.length === 1 ? options?.displayName : undefined

        await _runConcurrent(files, UPLOAD_CONCURRENCY, async (leaf) => {
            const key = leaf.relativePath || leaf.file.name
            await _uploadLeaf(leaf, key, reactiveBatch, collId, recordingCtId, options?.modality, options?.preserveAnnotations, displayName)
        })

        _finaliseBatch(reactiveBatch)
        return reactiveBatch
    }

    // ---------------------------------------------------------------------------
    // Tree upload — creates Collections matching folder structure
    // ---------------------------------------------------------------------------

    /**
     * Upload an UploadTree, creating a Collection for each folder and adding
     * each uploaded recording as a CollectionItem to its folder's collection.
     *
     * Root-level files are uploaded without any collection assignment.
     *
     * Returns a BatchUploadState that updates reactively.
     */
    async function uploadTree(
        tree: UploadTree,
        options?: {
            /**
             * ID of an existing Collection to nest all root-level folders and
             * files under.  null / omitted = library root.
             */
            parentCollectionId?: number | null
            /** Modality to assign to each recording once processing completes. */
            modality?: string
            preserveAnnotations?: boolean
            /**
             * When false (default) every file is uploaded directly into the
             * parent collection; the on-disk folder names never reach the
             * server.  When true the folder structure is mirrored as nested
             * Collection rows — used only when the user opts in via the UI
             * and accepts the PHI risk that the folder names will be visible.
             * See frontend/README.md (Folder uploads + PHI) for the policy.
             */
            preserveFolderHierarchy?: boolean
        },
    ): Promise<BatchUploadState> {
        const parentId = options?.parentCollectionId ?? null
        const preserveHierarchy = options?.preserveFolderHierarchy ?? false

        // Flat default: collect every leaf, dedupe colliding names, and upload
        // each into the parent collection. No new Collections are created.
        if (!preserveHierarchy) {
            const allLeaves = _collectLeaves(tree)
            const newNames = computeDedupedNames(allLeaves.map(l => l.file.name))
            const renamed: FileLeaf[] = allLeaves.map((leaf, idx) => {
                const newName = newNames[idx]!
                if (newName === leaf.file.name) {
                    return leaf
                }
                const renamedFile = new File([leaf.file], newName, {
                    type: leaf.file.type,
                    lastModified: leaf.file.lastModified,
                })
                return { file: renamedFile, relativePath: newName }
            })
            const batch = _makeBatch(renamed.map(f => f.relativePath || f.file.name))
            activeBatches.value.push(batch)
            currentBatchId.value = batch.id
            const reactiveBatch = activeBatches.value[activeBatches.value.length - 1] as BatchUploadState

            let recordingCtId: number | null = null
            try {
                recordingCtId = await getRecordingContentTypeId()
            } catch {
                // Collection linking will silently skip.
            }

            await _runConcurrent(renamed, UPLOAD_CONCURRENCY, async (leaf) => {
                const key = leaf.relativePath || leaf.file.name
                await _uploadLeaf(leaf, key, reactiveBatch, parentId, recordingCtId, options?.modality, options?.preserveAnnotations)
            })
            _finaliseBatch(reactiveBatch)
            return reactiveBatch
        }

        // Hierarchy-preserving path — caller has explicitly opted in to having
        // the on-disk folder names land in the database as Collection rows.
        const allLeaves = _collectLeaves(tree)
        const batch = _makeBatch(allLeaves.map(f => f.relativePath || f.file.name))
        activeBatches.value.push(batch)
        currentBatchId.value = batch.id
        // Use the reactive proxy from the array — mutations on the plain `batch`
        // object won't trigger Vue reactivity, so the UI won't update.
        const reactiveBatch = activeBatches.value[activeBatches.value.length - 1] as BatchUploadState

        // Resolve Recording content type ID once before starting uploads.
        let recordingCtId: number | null = null
        try {
            recordingCtId = await getRecordingContentTypeId()
        } catch {
            // If the lookup fails we still upload; collection linking will silently skip.
        }

        // Root files: add to the parent collection if one was supplied.
        const rootFileUploads = tree.rootFiles.map(leaf =>
            _uploadLeaf(leaf, leaf.relativePath || leaf.file.name, reactiveBatch, parentId, recordingCtId, options?.modality, options?.preserveAnnotations),
        )

        // Root folders each become a child Collection of parentId.
        const rootFolderUploads = tree.rootFolders.map(folder =>
            _uploadFolder(folder, parentId, reactiveBatch, recordingCtId, options?.modality, options?.preserveAnnotations),
        )

        await Promise.all([...rootFileUploads, ...rootFolderUploads])
        _finaliseBatch(reactiveBatch)
        return reactiveBatch
    }

    // ---------------------------------------------------------------------------
    // Internal helpers
    // ---------------------------------------------------------------------------

    /** Collect every FileLeaf in the tree in depth-first order. */
    function _collectLeaves(tree: UploadTree): FileLeaf[] {
        const out: FileLeaf[] = [...tree.rootFiles]
        for (const folder of tree.rootFolders) _collectFolderLeaves(folder, out)
        return out
    }

    function _collectFolderLeaves(folder: FolderNode, out: FileLeaf[]): void {
        out.push(...folder.files)
        for (const sub of folder.subfolders) _collectFolderLeaves(sub, out)
    }

    /**
     * Create a Collection for `folder`, upload its files into it, then recurse
     * into sub-folders (each gets its own child Collection).
     */
    async function _uploadFolder(
        folder: FolderNode,
        parentCollectionId: number | null,
        batch: BatchUploadState,
        recordingCtId: number | null,
        modality?: string,
        preserveAnnotations?: boolean,
    ): Promise<void> {
        // Create the collection before uploading its files.
        let collectionId: number | null = null
        try {
            const collection = await createCollection({
                name: folder.name,
                parent_id: parentCollectionId,
            })
            batch.collectionsCreated++
            collectionId = collection.id
        } catch {
            // Collection creation failed; uploads still proceed, just unlinked.
        }

        // Upload files in this folder concurrently (bounded).
        await _runConcurrent(folder.files, UPLOAD_CONCURRENCY, async (leaf) => {
            const key = leaf.relativePath || leaf.file.name
            await _uploadLeaf(leaf, key, batch, collectionId, recordingCtId, modality, preserveAnnotations)
        })

        // Recurse into sub-folders sequentially to preserve parent→child ordering.
        for (const sub of folder.subfolders) {
            await _uploadFolder(sub, collectionId, batch, recordingCtId, modality, preserveAnnotations)
        }
    }

    /**
     * Upload one file, poll for processing completion, and optionally link it
     * into a collection once the server has assigned it an object ID.
     */
    async function _uploadLeaf(
        leaf: FileLeaf,
        key: string,
        batch: BatchUploadState,
        collectionId: number | null,
        recordingCtId?: number | null,
        modality?: string,
        preserveAnnotations?: boolean,
        displayName?: string,
    ): Promise<void> {
        const fileState = batch.files.get(key)!
        fileState.phase = 'uploading'

        let result: RecordingUpload
        try {
            result = await uploadRecording(leaf.file, (pct) => {
                fileState.progress = pct
            }, { preserveAnnotations, displayName })
        } catch {
            fileState.phase = 'failed'
            batch.doneFiles++
            return
        }

        fileState.progress = 100
        fileState.phase = 'processing'
        // The upload response uses stored_name (e.g. "ABC123.edf") as the opaque
        // identifier; the recordings API never exposes the integer PK directly.
        // _resolve_recording_object_id on the backend accepts the 32-char hex prefix.
        const storedHash = result.stored_name.split('.')[0] ?? result.stored_name
        fileState.recordingObjectId = storedHash

        // Link to collection immediately — the row exists before processing finishes.
        if (collectionId !== null && recordingCtId != null) {
            try {
                await addCollectionItem(collectionId, {
                    content_type_id: recordingCtId,
                    object_id: storedHash,
                })
            } catch {
                // Non-fatal: the recording is uploaded even if collection linking fails.
            }
        }

        // Track in the legacy activeUploads list so existing UI components still work.
        const hash = result.stored_name.split('.')[0] ?? result.stored_name
        const legacyState: UploadState = { hash, name: leaf.file.name, progress: 100, phase: 'processing' }
        activeUploads.value.push(legacyState)

        // Poll for processing result.
        await _pollStatusBatch(hash, legacyState, fileState, batch, modality)
    }

    async function _pollStatusBatch(
        hash: string,
        legacyState: UploadState,
        fileState: BatchFileState,
        batch: BatchUploadState,
        modality?: string,
    ): Promise<void> {
        for (let attempt = 0; attempt < POLL_MAX_ATTEMPTS; attempt++) {
            await _sleep(POLL_INTERVAL_MS)
            try {
                const status = await getRecordingStatus(hash)
                if (status.status === 'ready' || status.status === 'failed') {
                    const phase = status.status as 'ready' | 'failed'
                    if (phase === 'ready' && modality) {
                        try {
                            await updateRecording(hash, { modality })
                        } catch {
                            // Non-fatal: recording is usable even if modality update fails.
                        }
                    }
                    fileState.phase = phase
                    fileState.recordingHash = hash
                    legacyState.phase = phase
                    batch.doneFiles++
                    _removeUpload(hash)
                    await load()
                    return
                }
            } catch {
                fileState.phase = 'failed'
                legacyState.phase = 'failed'
                batch.doneFiles++
                _removeUpload(hash)
                return
            }
        }
        fileState.phase = 'timeout'
        legacyState.phase = 'timeout'
        batch.doneFiles++
        _removeUpload(hash)
    }

    function _makeBatch(keys: string[]): BatchUploadState {
        const files = new Map<string, BatchFileState>()
        for (const key of keys) {
            files.set(key, {
                key,
                name: key.includes('/') ? key.slice(key.lastIndexOf('/') + 1) : key,
                // All files start as 'queued'; _uploadLeaf flips to 'uploading'
                // when the HTTP request actually fires (the pool is bounded by
                // UPLOAD_CONCURRENCY, so most files sit queued for a while).
                phase: 'queued',
                progress: 0,
                recordingObjectId: null,
                recordingHash: null,
            })
        }
        return {
            id: `batch-${Date.now()}-${Math.random().toString(36).slice(2)}`,
            totalFiles: keys.length,
            doneFiles: 0,
            collectionsCreated: 0,
            files,
            phase: 'running',
        }
    }

    function _finaliseBatch(batch: BatchUploadState): void {
        const anyFailed = [...batch.files.values()].some(
            f => f.phase === 'failed' || f.phase === 'timeout',
        )
        batch.phase = anyFailed ? 'partial' : 'done'
        // Keep the batch in activeBatches briefly so the UI can show the final state,
        // then let the view remove it when the user dismisses or navigates away.
    }

    /**
     * Run `fn` over `items` with at most `concurrency` tasks in flight at once.
     */
    async function _runConcurrent<T>(
        items: T[],
        concurrency: number,
        fn: (item: T) => Promise<void>,
    ): Promise<void> {
        const queue = [...items]
        const workers = Array.from({ length: Math.min(concurrency, items.length) }, async () => {
            while (queue.length > 0) {
                const item = queue.shift()!
                await fn(item)
            }
        })
        await Promise.all(workers)
    }

    async function _pollStatus(
        hash: string,
        state: UploadState,
        onDone?: (phase: 'ready' | 'failed' | 'timeout') => void,
    ) {
        for (let attempt = 0; attempt < POLL_MAX_ATTEMPTS; attempt++) {
            await _sleep(POLL_INTERVAL_MS)
            try {
                const status = await getRecordingStatus(hash)
                if (status.status === 'ready' || status.status === 'failed') {
                    state.phase = status.status
                    onDone?.(status.status as 'ready' | 'failed')
                    // Refresh the list so the new recording appears.
                    await load()
                    _removeUpload(hash)
                    return
                }
            } catch {
                // 404 means the row was deleted (processing infrastructure error).
                state.phase = 'failed'
                onDone?.('failed')
                _removeUpload(hash)
                return
            }
        }
        state.phase = 'timeout'
        onDone?.('timeout')
        _removeUpload(hash)
    }

    function _removeUpload(hash: string) {
        const idx = activeUploads.value.findIndex(u => u.hash === hash)
        if (idx !== -1) activeUploads.value.splice(idx, 1)
    }

    function _sleep(ms: number) {
        return new Promise<void>(resolve => setTimeout(resolve, ms))
    }

    function removeBatch(batchId: string) {
        const idx = activeBatches.value.findIndex(b => b.id === batchId)
        if (idx !== -1) activeBatches.value.splice(idx, 1)
        if (currentBatchId.value === batchId) {
            currentBatchId.value = null
        }
    }

    /** Drop every batch whose upload has reached a terminal state. Leaves
     *  any still-running batch in place. Used by UploadView on lifecycle
     *  events (new upload start, stage reset, view unmount) so the store
     *  never accumulates stale state past a single completed session. */
    function clearCompletedBatches() {
        activeBatches.value = activeBatches.value.filter(b => b.phase === 'running')
        if (currentBatchId.value !== null &&
            !activeBatches.value.find(b => b.id === currentBatchId.value)) {
            currentBatchId.value = null
        }
    }

    return {
        recordings,
        loading,
        error,
        activeUploads,
        activeBatches,
        currentBatch,
        load,
        refresh,
        startPolling,
        stopPolling,
        upload,
        uploadBatch,
        uploadTree,
        removeBatch,
        clearCompletedBatches,
    }
})
