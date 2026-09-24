import { http } from '#lib/http'
import type { NameWarning } from '#lib/nameWarnings'

export interface RecordingMeta {
    format: string
    duration: number
    data_record_count: number
    data_record_duration: number
    signal_count: number
    discontinuous: boolean
}

export interface AnnotationRef {
    object_hash: string
}

export interface InterruptionRef extends AnnotationRef {
    /** Gap onset in seconds on the data-position timeline (gap-exclusive). */
    start: number
    /** Gap length in seconds. */
    duration: number
}

export interface Recording {
    hash: string
    /** Author-private uploaded filename; null for grantees, share-token holders, and federated peers. */
    original_name: string | null
    /** Grantee-visible label; always populated (falls back to a stored_name hash prefix). */
    display_name: string
    /** True when the author set an explicit display_name (vs the hash-prefix fallback). */
    has_custom_name: boolean
    /** Author-only failure detail when status is 'failed'; null for grantees and successful recordings. */
    processing_error: string | null
    file_extension: string
    file_size: number
    /** SHA-256 of the file as stored, after de-identification; empty until processing completes. */
    stored_hash: string
    content_hash: string
    status: 'pending' | 'processing' | 'ready' | 'failed'
    modality: string
    /** DOI or URL of the published dataset the data was taken from; empty for data acquired here. */
    public_source: string
    /** Exact for the author and superusers; truncated to the first of its month for every other reader. */
    created_at: string
    deleted_at: string | null
    meta: RecordingMeta | null
    events: AnnotationRef[]
    interruptions: InterruptionRef[]
    labels: AnnotationRef[]
    /**
     * Set when this recording appears at the library root only because its sole
     * collection is in the trash — the collection it will drop back into if that
     * collection is restored. Null for genuinely uncollected recordings.
     */
    trashed_collection: { id: number; name: string } | null
    /** PATCH responses only: free-text warnings for `display_name`. */
    warnings?: NameWarning[]
}

export interface RecordingUpload {
    id: number
    original_name: string
    stored_name: string
    file_extension: string
    file_size: number
    status: string
    /** Free-text warnings for the `display_name` sent with the upload. */
    warnings?: NameWarning[]
}

export interface RecordingStatus {
    id: number
    status: string
}

export async function listRecordings(
    limit = 50,
    offset = 0,
    opts: { uncollected?: boolean; status?: Recording['status'] } = {},
): Promise<Recording[]> {
    const response = await http.get<Recording[]>('/recordings/api/v1/', {
        params: {
            limit,
            offset,
            ...(opts.uncollected ? { uncollected: true } : {}),
            ...(opts.status ? { status: opts.status } : {}),
        },
    })
    return response.data
}

export async function getRecordingDetail(hash: string, shareToken?: string): Promise<Recording> {
    const response = await http.get<Recording>(`/recordings/api/v1/${hash}`, {
        params: shareToken ? { share_token: shareToken } : undefined,
    })
    return response.data
}

export async function getRecordingStatus(hash: string): Promise<RecordingStatus> {
    const response = await http.get<RecordingStatus>(`/recordings/api/v1/status/${hash}`)
    return response.data
}

export async function uploadRecording(
    file: File,
    onProgress?: (percent: number) => void,
    options?: { preserveAnnotations?: boolean; displayName?: string },
): Promise<RecordingUpload> {
    const formData = new FormData()
    formData.append('file', file)
    if (options?.preserveAnnotations) {
        formData.append('preserve_annotations', 'true')
    }
    if (options?.displayName) {
        formData.append('display_name', options.displayName)
    }
    const response = await http.post<RecordingUpload>('/recordings/api/v1/upload', formData, {
        headers: { 'Content-Type': 'multipart/form-data' },
        onUploadProgress(event) {
            if (onProgress && event.total) {
                onProgress(Math.round((event.loaded / event.total) * 100))
            }
        },
    })
    return response.data
}

export async function deleteRecording(hash: string): Promise<void> {
    await http.delete(`/recordings/api/v1/${hash}`)
}

export interface RecordingPatch {
    /** Grantee-visible label. Send an empty string to clear it and fall back to the hash prefix. */
    display_name?: string
    modality?: string
    /** DOI or http(s) URL of the published dataset the data was taken from; empty string clears it. */
    public_source?: string
}

/**
 * Effective recording name for display.
 *
 * Authors keep seeing their private `original_name` until they set an explicit
 * label; once a label is set — or for grantees, who never receive
 * `original_name` — the grantee-safe `display_name` is used.
 */
export function recordingName(
    rec: Pick<Recording, 'has_custom_name' | 'display_name' | 'original_name'>,
): string {
    if (rec.has_custom_name) {
        return rec.display_name
    }
    return rec.original_name ?? rec.display_name
}

export async function updateRecording(hash: string, payload: RecordingPatch): Promise<Recording> {
    const response = await http.patch<Recording>(`/recordings/api/v1/${hash}`, payload)
    return response.data
}

// ── Validating submissions to a release-gated dataset ────────────────────────

/** A contributor's batch of prepared recordings: identifiers and counts, never a file. */
export interface SubmissionBatch {
    hash: string
    dataset_hash: string
    profile: string
    pending_count: number
    failed_count: number
    ingested_count: number
    created_at: string
}

/** One reason the gate refused a submitted file. */
export interface SubmissionViolation {
    code: string
    message: string
}

export type SubmissionResult =
    | { accepted: true; pending_count: number }
    | { accepted: false; violations: SubmissionViolation[] }

/** Open a batch against a dataset the caller may submit to, checked under a registered ingest profile. */
export async function createSubmissionBatch(payload: { dataset: string; profile: string }): Promise<SubmissionBatch> {
    const response = await http.post<SubmissionBatch>('/recordings/api/v1/submissions/batches', payload)
    return response.data
}

export async function listSubmissionBatches(datasetHash?: string): Promise<SubmissionBatch[]> {
    const response = await http.get<SubmissionBatch[]>('/recordings/api/v1/submissions/batches', {
        params: datasetHash ? { dataset: datasetHash } : undefined,
    })
    return response.data
}

export async function getSubmissionBatch(batchHash: string): Promise<SubmissionBatch> {
    const response = await http.get<SubmissionBatch>(`/recordings/api/v1/submissions/batches/${batchHash}`)
    return response.data
}

/**
 * Submit one prepared recording with its sidecar. A refused file resolves (not rejects) with
 * `accepted: false` and the violations, since a 422 is the gate's ordinary answer; nothing was written.
 * @param batchHash - The batch to add the file to.
 * @param file - The prepared EDF or BDF excerpt.
 * @param sidecar - The JSON sidecar as a Blob or File.
 * @param onProgress - Upload progress callback in whole percent.
 */
export async function submitFile(
    batchHash: string,
    file: File,
    sidecar: Blob,
    onProgress?: (percent: number) => void,
): Promise<SubmissionResult> {
    const formData = new FormData()
    formData.append('file', file)
    formData.append('sidecar', sidecar, 'sidecar.json')
    const response = await http.post<SubmissionResult>(
        `/recordings/api/v1/submissions/batches/${batchHash}/files`,
        formData,
        {
            headers: { 'Content-Type': 'multipart/form-data' },
            validateStatus: (status) => status === 202 || status === 422,
            onUploadProgress(event) {
                if (onProgress && event.total) {
                    onProgress(Math.round((event.loaded / event.total) * 100))
                }
            },
        },
    )
    return response.data
}
