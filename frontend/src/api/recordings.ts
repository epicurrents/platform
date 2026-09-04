import { http } from '#lib/http'

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
    file_hash: string
    content_hash: string
    status: 'pending' | 'processing' | 'ready' | 'failed'
    modality: string
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
}

export interface RecordingUpload {
    id: number
    original_name: string
    stored_name: string
    file_extension: string
    file_size: number
    file_hash: string
    status: string
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
