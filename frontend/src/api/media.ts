/**
 * Frontend client for the media app's REST API at /media/api/v1/.
 *
 * Surfaces the operations the platform UI needs: listing the
 * user-readable media files for the pickers, resolving the MediaFile
 * ContentType id for posting to the generic library /items/ endpoint,
 * fetching a single file's detail, uploading new media, and patching a
 * file's metadata, recording attachment, and timeline offset. A dedicated
 * media-library page (with delete) lands in a future iteration.
 */

import { http } from '#lib/http'

interface ContentTypeInfo {
    id: number
    app_label: string
    model: string
    natural_key: string
}

export interface MediaAttachment {
    type: string
    id: string
}

export interface MediaFileSummary {
    content_hash: string
    media_type: string
    display_name: string
    file_extension: string
    file_size: number
    is_supported: boolean
    attached_to: MediaAttachment | null
    /** Position in seconds of the media on the parent's timeline (video/audio start, image pin point). */
    time_offset: number | null
    created_at: string
    modified_at: string
}

export interface MediaFileDetail extends MediaFileSummary {
    /** Visible only to the file's author / a superuser; null otherwise. */
    original_name: string | null
}

/** Internal pk of the MediaFile ContentType, memoised after the first lookup. */
let _mediaContentTypeId: number | null = null

/**
 * Resolve the integer ContentType id for media.MediaFile. Used as
 * ``content_type_id`` when adding a media row to a collection or dataset
 * through the generic library /items/ endpoint.
 */
export async function getMediaContentTypeId(): Promise<number> {
    if (_mediaContentTypeId !== null) {
        return _mediaContentTypeId
    }
    const response = await http.get<ContentTypeInfo[]>('/annotations/api/v1/content-types', {
        params: { app_label: 'media', model: 'mediafile' },
    })
    const found = response.data.find(t => t.natural_key === 'media.mediafile')
    if (!found) throw new Error('media.MediaFile ContentType not found on this server')
    _mediaContentTypeId = found.id
    return found.id
}

/**
 * List media files visible to the caller. Optional filters mirror the
 * server endpoint and are passed straight through as query params.
 */
export async function listMediaFiles(opts: {
    mediaType?: string
    attachedToType?: string
    attachedToId?: string
    limit?: number
    offset?: number
} = {}): Promise<MediaFileSummary[]> {
    const params: Record<string, unknown> = {}
    if (opts.mediaType) params.media_type = opts.mediaType
    if (opts.attachedToType) params.attached_to_type = opts.attachedToType
    if (opts.attachedToId) params.attached_to_id = opts.attachedToId
    if (opts.limit !== undefined) params.limit = opts.limit
    if (opts.offset !== undefined) params.offset = opts.offset
    const res = await http.get<MediaFileSummary[]>('/media/api/v1/', { params })
    return res.data
}

/**
 * Internal primary key of a media file by its public ``content_hash``.
 * The library generic-items endpoint uses internal PKs for object_id;
 * this helper resolves the hash so add-media flows can pass the right
 * value without bumping the API contract.
 *
 * Returns the integer PK as a string (matches the library endpoint's
 * ``object_id`` shape) — the detail endpoint exposes ``content_hash``
 * publicly but the integer PK is exposed only through this lookup, used
 * exclusively for membership writes.
 */
export async function getMediaDetail(contentHash: string): Promise<MediaFileDetail> {
    const res = await http.get<MediaFileDetail>(`/media/api/v1/${contentHash}`)
    return res.data
}

/**
 * Upload a media file and return its created row. The display_name and
 * attachment fields are optional and map to query params on the server
 * endpoint (multipart bodies in Ninja only carry the file part itself).
 */
export interface MediaUploadOptions {
    displayName?: string
    mediaType?: string
    attachedToType?: string
    attachedToId?: string
    /**
     * Recording-time position (seconds) to pin the media on the parent's
     * timeline. Omitted or null leaves it unplaced (the server stores null).
     */
    timeOffset?: number | null
    /** Optional fetch progress hook; called with values in [0, 1]. */
    onProgress?: (fraction: number) => void
}

export async function uploadMedia(
    file: File,
    opts: MediaUploadOptions = {},
): Promise<MediaFileDetail> {
    const form = new FormData()
    form.append('file', file)
    const params: Record<string, string> = {}
    if (opts.displayName) params.display_name = opts.displayName
    if (opts.mediaType) params.media_type = opts.mediaType
    if (opts.attachedToType) params.attached_to_type = opts.attachedToType
    if (opts.attachedToId) params.attached_to_id = opts.attachedToId
    if (opts.timeOffset !== undefined && opts.timeOffset !== null) {
        params.time_offset = String(opts.timeOffset)
    }
    const res = await http.post<MediaFileDetail>('/media/api/v1/upload', form, {
        params,
        onUploadProgress: (e) => {
            if (!opts.onProgress || !e.total) return
            opts.onProgress(e.loaded / e.total)
        },
    })
    return res.data
}

/** Lower-case, dot-prefixed extensions the platform serves as inline video. */
const VIDEO_EXTENSIONS = new Set(['.mp4', '.webm', '.mov', '.m4v', '.ogv', '.mkv'])

/**
 * Map a filename to the ``media_type`` the upload endpoint expects. Only the
 * media types the backend currently accepts are produced — video for known
 * video containers, document for everything else. Image / audio extensions
 * fall through to document until those MediaType seats are enabled server-side.
 */
export function mediaTypeForFilename(name: string): string {
    const dot = name.lastIndexOf('.')
    const ext = dot >= 0 ? name.slice(dot).toLowerCase() : ''
    return VIDEO_EXTENSIONS.has(ext) ? 'video' : 'document'
}

export interface MediaPatch {
    displayName?: string
    mediaType?: string
    /**
     * ``{type, id}`` to attach to a parent, or ``{type: '', id: ''}`` to
     * detach. Omit to leave the attachment unchanged.
     */
    attachedTo?: { type: string; id: string }
    /**
     * Timeline offset in seconds; null clears it. The key's presence decides
     * whether the field is sent, so ``null`` clears and omission leaves it
     * unchanged — mirroring the server's ``model_fields_set`` handling.
     */
    timeOffset?: number | null
}

/**
 * Update a media file's metadata, recording attachment, or timeline offset.
 * Only the fields present on ``patch`` are sent; everything else is left
 * untouched server-side. Returns the refreshed detail row.
 */
export async function patchMedia(contentHash: string, patch: MediaPatch): Promise<MediaFileDetail> {
    const body: Record<string, unknown> = {}
    if (patch.displayName !== undefined) {
        body.display_name = patch.displayName
    }
    if (patch.mediaType !== undefined) {
        body.media_type = patch.mediaType
    }
    if (patch.attachedTo !== undefined) {
        body.attached_to = patch.attachedTo
    }
    if ('timeOffset' in patch) {
        body.time_offset = patch.timeOffset
    }
    const res = await http.patch<MediaFileDetail>(`/media/api/v1/${contentHash}`, body)
    return res.data
}
