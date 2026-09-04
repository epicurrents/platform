import { http } from '#lib/http'
import type { ViewerSettingsOverrides } from '#lib/viewerConfig'

// ---------------------------------------------------------------------------
// Shared types
// ---------------------------------------------------------------------------

/**
 * A Collection (hierarchical folder) or Dataset.
 * Both use the same response schema from the backend.
 */
export interface Collection {
    id: number
    name: string
    description: string
    parent_id: number | null
    author_id: number
    created_at: string
    modified_at: string
    deleted_at: string | null
    /** Per-dataset viewer-config overrides; always empty for collections. */
    viewer_config: ViewerSettingsOverrides
    /** Datasets only — opaque public identifier used for URLs; null for collections. */
    object_hash: string | null
    /** Datasets only — SPDX licence id from DatasetMeta; null for collections and undeclared datasets. */
    license_spdx: string | null
    /** Datasets only — licence text URL from DatasetMeta; null for collections and undeclared datasets. */
    license_url: string | null
}

export interface CollectionItem {
    id: number
    content_type_id: number
    object_id: string
    added_at: string
    /** Dataset items only — the containing folder, or null for the dataset root. */
    folder_id: number | null
    /** Resolved display name for known types (e.g. Recording.display_name). Null for unknown types. */
    object_name: string | null
    /** Stable public hash for known types (recording content hash, media content hash). Null for unknown types. */
    object_hash: string | null
    /** Discriminator: lowercased model name. "recording" | "mediafile" today; null for unresolved types. */
    object_type: string | null
    /** Media-only: MediaFile.MediaType value ("document" today). */
    media_type: string | null
    /** Media-only: lowercase, dot-prefixed file extension. */
    file_extension: string | null
    /**
     * Media-only: false when the file's extension is no longer in the project's live
     * MEDIA_ALLOWED_UPLOAD_EXTENSIONS. The row stays listed so users see what's there;
     * the frontend should render it greyed with a lock icon and a tooltip explaining
     * the current project can't open this type.
     */
    is_supported: boolean | null
}

/** A presentation-only folder in a dataset's tree. */
export interface DatasetFolder {
    id: number
    dataset_id: number
    parent_id: number | null
    name: string
    position: number
    created_at: string
    modified_at: string
}

export interface AccessRight {
    id: number
    access_target_id: number | null
    access_target_username: string | null
    access_target_group_id: number | null
    access_target_group_name: string | null
    public_share_token: string | null
    can_read: boolean
    can_write: boolean
    can_share: boolean
    apply_middleware: boolean
    expires_at: string | null
}

export interface GrantAccessPayload {
    access_target_id?: number | null
    access_target_group_id?: number | null
    public_share_token?: string | null
    can_read?: boolean
    can_write?: boolean
    can_share?: boolean
    apply_middleware?: boolean
    expires_at?: string | null
}

export interface ContentTypeInfo {
    id: number
    app_label: string
    model: string
    natural_key: string
}

// ---------------------------------------------------------------------------
// Content type lookup (cached per page load)
// ---------------------------------------------------------------------------

let _recordingContentTypeId: number | null = null

/**
 * Returns the Django ContentType primary key for the Recording model.
 * Result is cached for the lifetime of the page to avoid redundant requests.
 */
export async function getRecordingContentTypeId(): Promise<number> {
    if (_recordingContentTypeId !== null) return _recordingContentTypeId
    const response = await http.get<ContentTypeInfo[]>('/annotations/api/v1/content-types', {
        params: { app_label: 'recordings', model: 'recording' },
    })
    const found = response.data.find(t => t.natural_key === 'recordings.recording')
    if (!found) throw new Error('Recording content type not found')
    _recordingContentTypeId = found.id
    return found.id
}

// ---------------------------------------------------------------------------
// Collections
// ---------------------------------------------------------------------------

export async function listCollections(params?: { parent_id?: number; limit?: number; offset?: number }): Promise<Collection[]> {
    const response = await http.get<Collection[]>('/api/v1/library/collections/', { params })
    return response.data
}

export async function getCollection(id: number): Promise<Collection> {
    const response = await http.get<Collection>(`/api/v1/library/collections/${id}/`)
    return response.data
}

export async function createCollection(payload: { name: string; description?: string; parent_id?: number | null }): Promise<Collection> {
    const response = await http.post<Collection>('/api/v1/library/collections/', payload)
    return response.data
}

export async function updateCollection(id: number, payload: { name?: string; description?: string; parent_id?: number | null }): Promise<Collection> {
    const response = await http.patch<Collection>(`/api/v1/library/collections/${id}/`, payload)
    return response.data
}

export async function deleteCollection(id: number): Promise<void> {
    await http.delete(`/api/v1/library/collections/${id}/`)
}

/**
 * Give the collection's writable recordings sequential display names
 * (`<prefix> 1`, `<prefix> 2`, … in added order), replacing any existing name.
 * Recordings the caller cannot write are left untouched and counted in `skipped`.
 */
export async function bulkRenameCollectionRecordings(
    collectionId: number,
    prefix: string,
): Promise<{ renamed: number; skipped: number }> {
    const response = await http.post<{ renamed: number; skipped: number }>(
        `/api/v1/library/collections/${collectionId}/recordings/bulk-rename`,
        { prefix },
    )
    return response.data
}

// ---------------------------------------------------------------------------
// Collection items
// ---------------------------------------------------------------------------

export async function listCollectionItems(collectionId: number, params?: { content_type_id?: number; limit?: number; offset?: number }): Promise<CollectionItem[]> {
    const response = await http.get<CollectionItem[]>(`/api/v1/library/collections/${collectionId}/items/`, { params })
    return response.data
}

export async function addCollectionItem(collectionId: number, payload: { content_type_id: number; object_id: string }): Promise<CollectionItem> {
    const response = await http.post<CollectionItem>(`/api/v1/library/collections/${collectionId}/items/`, payload)
    return response.data
}

export async function removeCollectionItem(collectionId: number, itemId: number): Promise<void> {
    await http.delete(`/api/v1/library/collections/${collectionId}/items/${itemId}/`)
}

export async function moveCollectionItem(
    sourceCollectionId: number,
    itemId: number,
    targetCollectionId: number,
): Promise<CollectionItem> {
    const response = await http.post<CollectionItem>(
        `/api/v1/library/collections/${sourceCollectionId}/items/${itemId}/move`,
        { target_collection_id: targetCollectionId },
    )
    return response.data
}

// ---------------------------------------------------------------------------
// Datasets
// ---------------------------------------------------------------------------

export async function listDatasets(params?: { limit?: number; offset?: number }): Promise<Collection[]> {
    const response = await http.get<Collection[]>('/api/v1/library/datasets/', { params })
    return response.data
}

export async function getDataset(id: number | string, shareToken?: string): Promise<Collection> {
    const response = await http.get<Collection>(`/api/v1/library/datasets/${id}/`, {
        params: shareToken ? { share_token: shareToken } : undefined,
    })
    return response.data
}

export async function createDataset(payload: { name: string; description?: string; recording_hashes?: string[] }): Promise<Collection> {
    const response = await http.post<Collection>('/api/v1/library/datasets/', payload)
    return response.data
}

export async function updateDataset(
    id: number | string,
    payload: { name?: string; description?: string; viewer_config?: ViewerSettingsOverrides; license_spdx?: string; license_url?: string },
): Promise<Collection> {
    const response = await http.patch<Collection>(`/api/v1/library/datasets/${id}/`, payload)
    return response.data
}

export async function deleteDataset(id: number | string): Promise<void> {
    await http.delete(`/api/v1/library/datasets/${id}/`)
}

// ---------------------------------------------------------------------------
// Dataset items
// ---------------------------------------------------------------------------

export async function listDatasetItems(datasetId: number | string, params?: { content_type_id?: number; limit?: number; offset?: number }, shareToken?: string): Promise<CollectionItem[]> {
    const response = await http.get<CollectionItem[]>(`/api/v1/library/datasets/${datasetId}/items/`, {
        params: shareToken ? { ...params, share_token: shareToken } : params,
    })
    return response.data
}

export async function addDatasetItem(datasetId: number | string, payload: { content_type_id: number; object_id: string }): Promise<CollectionItem> {
    const response = await http.post<CollectionItem>(`/api/v1/library/datasets/${datasetId}/items/`, payload)
    return response.data
}

export async function removeDatasetItem(datasetId: number | string, itemId: number): Promise<void> {
    await http.delete(`/api/v1/library/datasets/${datasetId}/items/${itemId}/`)
}

// ---------------------------------------------------------------------------
// Dataset folders and item placement
// ---------------------------------------------------------------------------

export async function listDatasetFolders(datasetId: number | string, shareToken?: string): Promise<DatasetFolder[]> {
    const response = await http.get<DatasetFolder[]>(`/api/v1/library/datasets/${datasetId}/folders/`, {
        params: shareToken ? { share_token: shareToken } : undefined,
    })
    return response.data
}

export async function createDatasetFolder(
    datasetId: number | string,
    payload: { name: string; parent_id?: number | null; position?: number },
): Promise<DatasetFolder> {
    const response = await http.post<DatasetFolder>(`/api/v1/library/datasets/${datasetId}/folders/`, payload)
    return response.data
}

export async function updateDatasetFolder(
    datasetId: number | string,
    folderId: number,
    payload: { name?: string; parent_id?: number | null; position?: number },
): Promise<DatasetFolder> {
    const response = await http.patch<DatasetFolder>(`/api/v1/library/datasets/${datasetId}/folders/${folderId}/`, payload)
    return response.data
}

export async function deleteDatasetFolder(datasetId: number | string, folderId: number): Promise<void> {
    await http.delete(`/api/v1/library/datasets/${datasetId}/folders/${folderId}/`)
}

/** Place an item in a folder, or back at the dataset root with `folderId: null`. */
export async function moveDatasetItem(
    datasetId: number | string,
    itemId: number,
    folderId: number | null,
): Promise<CollectionItem> {
    const response = await http.post<CollectionItem>(
        `/api/v1/library/datasets/${datasetId}/items/${itemId}/move`,
        { folder_id: folderId },
    )
    return response.data
}

// ---------------------------------------------------------------------------
// Collection export
// ---------------------------------------------------------------------------

export interface CollectionExportResult {
    dataset: Collection
    exported_count: number
    skipped_count: number
    folder_count: number
}

/**
 * Copy a collection's subtree into a new dataset owned by the caller. With
 * `materialise_hierarchy` (default true on the backend) sub-collections become
 * dataset folders. Unreadable, trashed, and failed items are skipped and counted.
 */
export async function exportCollectionToDataset(
    collectionId: number,
    payload: { name?: string; description?: string; materialise_hierarchy?: boolean },
): Promise<CollectionExportResult> {
    const response = await http.post<CollectionExportResult>(
        `/api/v1/library/collections/${collectionId}/export/`,
        payload,
    )
    return response.data
}

// ---------------------------------------------------------------------------
// Dataset access rights
// ---------------------------------------------------------------------------

export async function listDatasetAccess(datasetId: number | string): Promise<AccessRight[]> {
    const response = await http.get<AccessRight[]>(`/api/v1/library/datasets/${datasetId}/access/`)
    return response.data
}

export async function grantDatasetAccess(datasetId: number | string, payload: GrantAccessPayload): Promise<AccessRight> {
    const response = await http.post<AccessRight>(`/api/v1/library/datasets/${datasetId}/access/`, payload)
    return response.data
}

export async function revokeDatasetAccess(datasetId: number | string, rightId: number): Promise<void> {
    await http.delete(`/api/v1/library/datasets/${datasetId}/access/${rightId}/`)
}
