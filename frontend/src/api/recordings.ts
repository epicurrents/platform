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

// ── Validating submissions to a submission pool ──────────────────────────────

/**
 * The public shape of an ingest profile: every value the gate checks a submission against. Null where the
 * profile leaves a value unchecked.
 */
export interface SubmissionProfile {
    key: string
    /** Channel labels, in the order the file must carry them. */
    channels: string[]
    sampling_rate: number | null
    physical_unit: string | null
    physical_min: number | null
    physical_max: number | null
    digital_min: number | null
    digital_max: number | null
    /** The lengths a file may have, in seconds. Empty when any length passes. */
    durations_seconds: number[]
    /** Keys the sidecar must carry, `recording_sha256` first. */
    required_sidecar_keys: string[]
    /** Keys refused anywhere in the sidecar, at any depth. */
    forbidden_sidecar_keys: string[]
}

/** An open pool the caller may submit to, with the profile a file is prepared against. */
export interface SubmissionPool {
    dataset_hash: string
    name: string
    profile: SubmissionProfile
}

/** One reason the gate refused a submitted file. */
export interface SubmissionViolation {
    code: string
    message: string
}

export type SubmissionResult =
    | { accepted: true }
    | { accepted: false; violations: SubmissionViolation[] }

/** The open pools the signed-in person may submit to, each with its published profile. */
export async function listSubmissionPools(): Promise<SubmissionPool[]> {
    const response = await http.get<SubmissionPool[]>('/recordings/api/v1/submissions/pools')
    return response.data
}

/** Every ingest profile the deployment registers, for choosing one when configuring a pool. */
export async function listSubmissionProfiles(): Promise<SubmissionProfile[]> {
    const response = await http.get<SubmissionProfile[]>('/recordings/api/v1/submissions/profiles')
    return response.data
}

/**
 * Submit one prepared recording with its sidecar to a pool. A refused file resolves (not rejects) with
 * `accepted: false` and the violations, since a 422 is the gate's ordinary answer; nothing was written. A pool the
 * caller may no longer submit to answers 404.
 * @param datasetHash - The pool's dataset hash.
 * @param file - The prepared EDF or BDF excerpt.
 * @param sidecar - The JSON sidecar as a Blob or File.
 * @param onProgress - Upload progress callback in whole percent.
 */
export async function submitFile(
    datasetHash: string,
    file: File,
    sidecar: Blob,
    onProgress?: (percent: number) => void,
): Promise<SubmissionResult> {
    const formData = new FormData()
    formData.append('file', file)
    formData.append('sidecar', sidecar, 'sidecar.json')
    const response = await http.post<SubmissionResult>(
        `/recordings/api/v1/submissions/pools/${datasetHash}/files`,
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
