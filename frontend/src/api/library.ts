import { http } from '#lib/http'
import type { NameWarning } from '#lib/nameWarnings'
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
    /** Datasets only — members stay hidden from readers until a release run publishes them. */
    release_gated?: boolean
    /** Datasets only — the viewer loads every member when the dataset is opened, rather than each when opened. */
    preload_members?: boolean
    /** Create and update responses only: free-text warnings for `name` and `description`. */
    warnings?: NameWarning[]
}

export interface CollectionItem {
    id: number
    content_type_id: number
    object_id: string
    added_at: string
    /** Dataset items only — the containing folder, or null for the dataset root. */
    folder_id: number | null
    /** Dataset items only — the month a release run published the item; null in ungated datasets and for unreleased members. */
    release_month?: string | null
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

/**
 * A row of a dataset's item listing. `id` and `object_id` are null where the row must not be addressable by the
 * caller: the members of a release-gated dataset as a reader who does not manage it sees them. Such a reader has no
 * item controls; key and match rows on `object_hash` instead.
 */
export interface DatasetItem extends Omit<CollectionItem, 'id' | 'object_id'> {
    id: number | null
    object_id: string | null
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
    /** Create and update responses only: free-text warnings for `name`. */
    warnings?: NameWarning[]
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
    /** The sharer's contextual assessment; null when none is recorded or the caller may not see it. */
    assessment_reference: string | null
    assessment_date: string | null
    /** Whether the caller may record the assessment on this row: its giver, the object's author or a superuser. */
    can_assess: boolean
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
    assessment_reference?: string
    assessment_date?: string | null
}

/** Record, update or clear a grant's contextual assessment: both fields, or an empty reference and a null date. */
export interface AssessmentPayload {
    assessment_reference: string
    assessment_date: string | null
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

/** The page size the `listAll*` readers ask for: the most a library listing serves at once. */
const PAGE_SIZE = 200

/**
 * Every row of a paged listing, read a page at a time until a short page.
 * @param fetchPage - Reads one page from `offset`, at most `PAGE_SIZE` rows.
 */
async function readAllPages<T> (fetchPage: (offset: number) => Promise<T[]>): Promise<T[]> {
    const rows: T[] = []
    for (;;) {
        const page = await fetchPage(rows.length)
        rows.push(...page)
        if (page.length < PAGE_SIZE) {
            return rows
        }
    }
}

/**
 * Every root collection, or every child of `parentId`, read to the end: a listing that stops at the server's page
 * would leave collections out of a tree with nothing to show it.
 * @param parentId - The parent collection; omitted for the roots.
 */
export async function listAllCollections(parentId?: number): Promise<Collection[]> {
    return readAllPages(offset => listCollections({ parent_id: parentId, limit: PAGE_SIZE, offset }))
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
): Promise<{ renamed: number; skipped: number; warnings: NameWarning[] }> {
    const response = await http.post<{ renamed: number; skipped: number; warnings: NameWarning[] }>(
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

/**
 * Every item of a collection, read to the end. A selection, a dataset made from one or a name check against one
 * covers the whole collection, which a single page does not.
 * @param collectionId - The collection.
 */
export async function listAllCollectionItems(collectionId: number): Promise<CollectionItem[]> {
    return readAllPages(offset => listCollectionItems(collectionId, { limit: PAGE_SIZE, offset }))
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

/** Every dataset the caller can read, read to the end. */
export async function listAllDatasets(): Promise<Collection[]> {
    return readAllPages(offset => listDatasets({ limit: PAGE_SIZE, offset }))
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
    payload: {
        name?: string
        description?: string
        viewer_config?: ViewerSettingsOverrides
        license_spdx?: string
        license_url?: string
        release_gated?: boolean
        preload_members?: boolean
    },
): Promise<Collection> {
    const response = await http.patch<Collection>(`/api/v1/library/datasets/${id}/`, payload)
    return response.data
}

export async function deleteDataset(id: number | string): Promise<void> {
    await http.delete(`/api/v1/library/datasets/${id}/`)
}

// ---------------------------------------------------------------------------
// Curator review of a release-gated dataset's members
// ---------------------------------------------------------------------------

/** One recording member's review state, as a curator sees it. */
export interface DatasetMemberReview {
    /** How many curators approved it; never who. */
    approvals: number
    approved_by_me: boolean
    /** The recording's 32-hex hash, as the item listing names it. */
    hash: string
    released: boolean
}

/** The review state of a release-gated dataset's recording members, answered to its managers only. */
export interface DatasetReview {
    /** Approvals from distinct curators the pool's profile asks for; null when the dataset is not a pool. */
    approvals_required: number | null
    members: DatasetMemberReview[]
}

/** The closed list of veto reasons; free text is never recorded. */
export const VETO_REASONS = ['rare_condition', 'skull_defect', 'device', 'unusual_protocol', 'other'] as const
export type VetoReason = typeof VETO_REASONS[number]

/** The review state; 403 for a caller who does not manage the dataset. */
export async function getDatasetReview(id: number | string): Promise<DatasetReview> {
    const response = await http.get<DatasetReview>(`/api/v1/library/datasets/${id}/reviews/`)
    return response.data
}

/** Approve an unreleased member for release. */
export async function approveDatasetMember(id: number | string, hash: string): Promise<DatasetMemberReview> {
    const response = await http.post<DatasetMemberReview>(`/api/v1/library/datasets/${id}/reviews/${hash}/approval`)
    return response.data
}

/** Withdraw the caller's approval of an unreleased member. */
export async function withdrawDatasetMemberApproval(id: number | string, hash: string): Promise<DatasetMemberReview> {
    const response = await http.delete<DatasetMemberReview>(`/api/v1/library/datasets/${id}/reviews/${hash}/approval`)
    return response.data
}

/** Veto a member: the recording is removed at once and for good. */
export async function vetoDatasetMember(id: number | string, hash: string, reason: VetoReason): Promise<void> {
    await http.post(`/api/v1/library/datasets/${id}/reviews/${hash}/veto`, { reason })
}

// ---------------------------------------------------------------------------
// Submission pools
// ---------------------------------------------------------------------------

/** A dataset's submission-pool state, as its author or a superuser sees it. */
export interface DatasetPool {
    /** Not a pool and empty, so it can become one. */
    configurable: boolean
    failed_count: number
    /** Active members of the contributor group. */
    contributor_count: number
    /** The profile's m: the contributor count intake needs before it opens; null when the profile sets none. */
    contributors_required: number | null
    /** A file has been accepted from a contributor: the profile, group and gate are fixed. */
    filling: boolean
    group_id: number | null
    group_name: string | null
    ingested_count: number
    /** Whether the pool accepts new submissions. */
    open: boolean
    pending_count: number
    /** Key of the registered ingest profile; null when the dataset is not a pool. */
    profile: string | null
}

export async function getDatasetPool(id: number | string): Promise<DatasetPool> {
    const response = await http.get<DatasetPool>(`/api/v1/library/datasets/${id}/pool/`)
    return response.data
}

/** Make an empty dataset a pool checked against `profile`: a new dedicated group, the gate on and intake closed. */
export async function configureDatasetPool(id: number | string, profile: string): Promise<DatasetPool> {
    const response = await http.post<DatasetPool>(`/api/v1/library/datasets/${id}/pool/`, { profile })
    return response.data
}

/** Open or close intake, or change the profile of a pool that has not filled. */
export async function updateDatasetPool(
    id: number | string,
    payload: { open?: boolean; profile?: string },
): Promise<DatasetPool> {
    const response = await http.patch<DatasetPool>(`/api/v1/library/datasets/${id}/pool/`, payload)
    return response.data
}

/** Dissolve a pool that has not filled; its group is deleted and the gate turns off. */
export async function dissolveDatasetPool(id: number | string): Promise<DatasetPool> {
    const response = await http.delete<DatasetPool>(`/api/v1/library/datasets/${id}/pool/`)
    return response.data
}

// ---------------------------------------------------------------------------
// Dataset items
// ---------------------------------------------------------------------------

export async function listDatasetItems(datasetId: number | string, params?: { content_type_id?: number; limit?: number; offset?: number }, shareToken?: string): Promise<DatasetItem[]> {
    const response = await http.get<DatasetItem[]>(`/api/v1/library/datasets/${datasetId}/items/`, {
        params: shareToken ? { ...params, share_token: shareToken } : params,
    })
    return response.data
}

/**
 * Every item of a dataset, read a page at a time until a short page. The folder tree groups the whole membership,
 * so it cannot be shown one page at a time; the browser is the paged view of a large dataset.
 * @param datasetId - The dataset's object hash or primary key.
 */
export async function listAllDatasetItems(datasetId: number | string, shareToken?: string): Promise<DatasetItem[]> {
    return readAllPages(offset => listDatasetItems(datasetId, { limit: PAGE_SIZE, offset }, shareToken))
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
    /** Free-text warnings for the new dataset's `name` and `description`. */
    warnings?: NameWarning[]
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

export async function assessDatasetAccess(
    datasetId: number | string,
    rightId: number,
    payload: AssessmentPayload,
): Promise<AccessRight> {
    const response = await http.patch<AccessRight>(`/api/v1/library/datasets/${datasetId}/access/${rightId}/`, payload)
    return response.data
}

// ---------------------------------------------------------------------------
// Dataset browser
// ---------------------------------------------------------------------------

/** A tag as a browser row shows it. */
export interface TagChip {
    id: number
    name: string
    curated: boolean
}

/**
 * One field of a dataset's subject schema: a field of `choices`, or a `number` field, which has none and holds a
 * number per member. `displayed` marks a field a reader who does not manage a release-gated dataset, or one that has
 * had a release, is shown; elsewhere it has no effect. A number field is never displayed.
 */
export interface SubjectField {
    key: string
    label: string
    type: 'choice' | 'number'
    choices: string[]
    displayed: boolean
}

/** A member's value for a subject field: one of its choices, or a number for a number field. */
export type SubjectValue = string | number

/**
 * One recording of a dataset as the browser shows it. `created_at` is the date the caller is served: exact for the
 * author, otherwise a release month or the first of the upload month. The metadata columns are null until ingest has
 * written them. `subject` holds a value for each shown field the member has one for; `tags` are at most three of the
 * recording's tags, and `tag_count` counts them all.
 */
export interface DatasetBrowseRow {
    hash: string
    display_name: string
    modality: string
    public_source: string
    created_at: string
    duration: number | null
    channel_count: number | null
    sampling_rate_max: number | null
    sampling_rate_min: number | null
    channel_layout: string | null
    deidentification_version: number | null
    subject: Record<string, SubjectValue>
    tags: TagChip[]
    tag_count: number
}

/** One value of a browser facet and how many members it would match. */
export interface FacetValue {
    value: string
    count: number
}

/** One tag of the browser's tag facet and how many matching members carry it. */
export interface TagFacet extends TagChip {
    count: number
}

/**
 * A number field's facet: the lowest and highest value among the members that would match under the other filters,
 * and how many of them carry a value. `min` and `max` are null when none does.
 */
export interface RangeFacet {
    min: number | null
    max: number | null
    count: number
}

/**
 * The browser's facets: the modalities present, each shown subject field's choices by key, each shown number field's
 * range by key, and the tags.
 */
export interface DatasetFacets {
    modality: FacetValue[]
    subject: Record<string, FacetValue[]>
    ranges: Record<string, RangeFacet>
    tags: TagFacet[]
}

/** A number field's range filter, both ends inclusive; a missing end is open. */
export interface SubjectRange {
    min?: number
    max?: number
}

/**
 * What a browser page is narrowed to: any of the modalities, any of the choices of each subject field, a range of
 * each number field, and every one of the tags.
 */
export interface DatasetBrowseFilters {
    modality: string[]
    subject: Record<string, string[]>
    ranges: Record<string, SubjectRange>
    tags: number[]
}

/**
 * A page of the dataset browser, with the matching row count across every page, the subject fields the caller is
 * shown and the facets.
 */
export interface DatasetBrowsePage {
    count: number
    subject_fields: SubjectField[]
    facets: DatasetFacets
    items: DatasetBrowseRow[]
}

/** The tags on one recording, in display order. */
export interface RecordingTags {
    hash: string
    tags: TagChip[]
}

/**
 * A browser query's filters as query parameters: repeated `modality`, `subject.<key>` and `tag` parameters, and a
 * range as `subject.<key>.min` and `subject.<key>.max`.
 * @param query - The parameters to append to.
 * @param filters - The filters; none appends nothing.
 */
function appendFilters (query: URLSearchParams, filters?: DatasetBrowseFilters) {
    for (const modality of filters?.modality ?? []) {
        query.append('modality', modality)
    }
    for (const [key, values] of Object.entries(filters?.subject ?? {})) {
        for (const value of values) {
            query.append(`subject.${key}`, value)
        }
    }
    for (const [key, range] of Object.entries(filters?.ranges ?? {})) {
        if (range.min !== undefined) {
            query.append(`subject.${key}.min`, String(range.min))
        }
        if (range.max !== undefined) {
            query.append(`subject.${key}.max`, String(range.max))
        }
    }
    for (const tag of filters?.tags ?? []) {
        query.append('tag', String(tag))
    }
}

/**
 * One page of a dataset's recordings for the browser.
 * @param datasetId - The dataset's object hash or primary key.
 * @param params - `sort` is a key from the server's allowlist, or `subject.<key>`, prefixed with `-` for descending.
 * @param filters - Sent as repeated `modality`, `subject.<key>` and `tag` parameters, and a range as `subject.<key>.min` and `subject.<key>.max`.
 * @param shareToken - Share token the dataset is read with, for a reader without an account.
 */
export async function browseDataset(
    datasetId: number | string,
    params: { sort?: string; limit?: number; offset?: number },
    filters?: DatasetBrowseFilters,
    shareToken?: string,
): Promise<DatasetBrowsePage> {
    const query = new URLSearchParams()
    for (const [name, value] of Object.entries(params)) {
        if (value !== undefined) {
            query.append(name, String(value))
        }
    }
    appendFilters(query, filters)
    if (shareToken) {
        query.append('share_token', shareToken)
    }
    const response = await http.get<DatasetBrowsePage>(`/api/v1/library/datasets/${datasetId}/browse/`, {
        params: query,
    })
    return response.data
}

/**
 * A browser query's filters as the parameters of a page URL, named as the browse endpoint names them, so a link can
 * carry the query to another page. Empty when nothing is filtered.
 * @param filters - The filters.
 */
export function browseFiltersToQuery (filters: DatasetBrowseFilters): Record<string, string[]> {
    const params = new URLSearchParams()
    appendFilters(params, filters)
    const query: Record<string, string[]> = {}
    for (const [name, value] of params) {
        (query[name] ??= []).push(value)
    }
    return query
}

/**
 * The browser filters a page URL carries, or null when it carries none. Parameters that are not filters are ignored,
 * and so is a range end that is not a number; the server refuses a filter the caller may not use.
 * @param query - The route's query.
 */
export function browseFiltersFromQuery (query: Record<string, unknown>): DatasetBrowseFilters | null {
    const filters: DatasetBrowseFilters = { modality: [], subject: {}, ranges: {}, tags: [] }
    let found = false
    for (const [name, raw] of Object.entries(query)) {
        const values = (Array.isArray(raw) ? raw : [raw]).filter(
            (value): value is string => typeof value === 'string' && value !== '',
        )
        if (!values.length) {
            continue
        }
        if (name === 'modality') {
            filters.modality.push(...values)
            found = true
        } else if (name === 'tag') {
            const ids = values.map(Number).filter(Number.isInteger)
            filters.tags.push(...ids)
            found = found || ids.length > 0
        } else {
            const match = name.match(/^subject\.([a-z][a-z0-9_]*)(?:\.(min|max))?$/)
            if (!match) {
                continue
            }
            const [, key, end] = match
            if (end === 'min' || end === 'max') {
                const bound = Number(values[0])
                if (Number.isFinite(bound)) {
                    (filters.ranges[key] ??= {})[end] = bound
                    found = true
                }
            } else {
                filters.subject[key] = values
                found = true
            }
        }
    }
    return found ? filters : null
}

/**
 * The hashes of every recording of the dataset the filters match, in the browser's order under *sort*, read a page
 * of 200 at a time.
 * @param datasetId - The dataset's object hash or primary key.
 * @param filters - The browser filters, or null for every recording.
 * @param sort - A browser sort key, `-` prefixed for descending.
 * @param shareToken - Share token the dataset is read with, if any.
 */
export async function listMatchingRecordingHashes (
    datasetId: number | string,
    filters: DatasetBrowseFilters | null,
    sort: string,
    shareToken?: string,
): Promise<string[]> {
    const limit = 200
    const hashes: string[] = []
    for (let offset = 0; ; offset += limit) {
        const page = await browseDataset(datasetId, { sort, limit, offset }, filters ?? undefined, shareToken)
        hashes.push(...page.items.map(row => row.hash))
        if (page.items.length < limit || hashes.length >= page.count) {
            return hashes
        }
    }
}

/**
 * One dataset of the catalogue: a release-gated dataset the caller can read with released recordings.
 * `recording_count` counts the released recordings the caller can see, `latest_release` is the month of the latest
 * run that released one, and `subject_fields` are the labels of the fields the browser shows the caller.
 */
export interface CatalogueEntry {
    object_hash: string
    name: string
    description: string
    license_spdx: string | null
    license_url: string | null
    recording_count: number
    modalities: string[]
    latest_release: string
    subject_fields: string[]
}

/**
 * A page of the catalogue, by name.
 * @param params - Paging; the server caps `limit` at 200.
 */
export async function listCatalogue(params: { limit?: number; offset?: number } = {}): Promise<CatalogueEntry[]> {
    const response = await http.get<CatalogueEntry[]>('/api/v1/library/catalogue/', { params })
    return response.data
}

/**
 * One dataset of the public listing, with the share token it is listed through. `recording_count` and `modalities`
 * describe the recordings the token reads, and `public_sources` the published datasets they name.
 */
export interface PublicDataset {
    object_hash: string
    name: string
    description: string
    license_spdx: string | null
    license_url: string | null
    recording_count: number
    modalities: string[]
    public_sources: string[]
    subject_fields: string[]
    share_token: string
}

/** The public dataset listing, which needs no account; a 404 means the deployment does not serve it. */
export async function listPublicDatasets(): Promise<PublicDataset[]> {
    const response = await http.get<PublicDataset[]>('/api/v1/library/public-datasets/')
    return response.data
}

/** One reason a dataset cannot be listed publicly, with the number of members it concerns where it is one. */
export interface ListingProblem {
    code: string
    detail: string
    count: number | null
}

/**
 * A dataset's public listing as its author sees it: the share token it is listed through, whether the listing names
 * it today, why not, and whether the deployment serves the listing at all.
 */
export interface PublicListing {
    enabled: boolean
    access_right_id: number | null
    listed: boolean
    problems: ListingProblem[]
}

/**
 * The dataset's public listing. Its author or a superuser only.
 * @param datasetId - The dataset's object hash or primary key.
 */
export async function getPublicListing(datasetId: number | string): Promise<PublicListing> {
    const response = await http.get<PublicListing>(`/api/v1/library/datasets/${datasetId}/public-listing/`)
    return response.data
}

/**
 * List the dataset publicly through one of its share tokens; a 409 carries every reason it does not qualify.
 * @param datasetId - The dataset's object hash or primary key.
 * @param accessRightId - The id of the dataset's share-token access right.
 */
export async function setPublicListing(datasetId: number | string, accessRightId: number): Promise<PublicListing> {
    const response = await http.put<PublicListing>(`/api/v1/library/datasets/${datasetId}/public-listing/`, {
        access_right_id: accessRightId,
    })
    return response.data
}

/**
 * Take the dataset off the public listing. The token stays.
 * @param datasetId - The dataset's object hash or primary key.
 */
export async function clearPublicListing(datasetId: number | string): Promise<PublicListing> {
    const response = await http.delete<PublicListing>(`/api/v1/library/datasets/${datasetId}/public-listing/`)
    return response.data
}

/**
 * The tags on each of *hashes* the caller can read. A recording the caller cannot read is left out, as is one
 * without tags.
 * @param hashes - Recording hashes, at most 200.
 */
export async function listRecordingTags(hashes: string[]): Promise<RecordingTags[]> {
    const params = new URLSearchParams()
    for (const hash of hashes) {
        params.append('hash', hash)
    }
    const response = await http.get<RecordingTags[]>('/api/v1/library/recording-tags/', { params })
    return response.data
}

/**
 * Set or clear a member's subject values; dataset managers only. A key set to `null` is cleared, a key left out is
 * kept.
 * @param datasetId - The dataset's object hash or primary key.
 * @param hash - The member recording's hash.
 * @param values - Each value one of its field's choices, a number for a number field, or null.
 */
export async function updateDatasetMemberSubject(
    datasetId: number | string,
    hash: string,
    values: Record<string, SubjectValue | null>,
): Promise<Record<string, SubjectValue>> {
    const response = await http.patch<{ values: Record<string, SubjectValue> }>(
        `/api/v1/library/datasets/${datasetId}/recordings/${hash}/subject/`,
        { values },
    )
    return response.data.values
}

// ---------------------------------------------------------------------------
// Tags
// ---------------------------------------------------------------------------

/**
 * A tag within the caller's reach. `curated` tags were created by staff and are listed to everyone; any other is
 * listed to its author and to those who can read an object it is on.
 */
export interface Tag {
    id: number
    name: string
    description: string
    parent_id: number | null
    author_id: number
    curated: boolean
    created_at: string
    modified_at: string
}

/** A tag create or update response, with warnings for a name or description that looks like an identifier. */
export interface TagWrite extends Tag {
    warnings: NameWarning[]
}

/**
 * A tag on one object. `id` and `object_id` are null for a pooled recording listed to anyone but a manager of its
 * pool; `object_hash` names a recording or a media file to whoever is listed it.
 */
export interface TaggedItem {
    id: number | null
    tag_id: number
    content_type_id: number
    object_id: string | null
    object_hash: string | null
    tagged_at: string
}

/**
 * The outcome of tagging or untagging the members a browser query matches: of the `matched` members, `changed`
 * were tagged or untagged, `unchanged` already were, and `skipped` the caller may not tag or untag.
 */
export interface DatasetTagBatch {
    matched: number
    changed: number
    unchanged: number
    skipped: number
}

/**
 * The tags within the caller's reach, by name: the roots, the children of `parentId`, or with `q` the tags whose
 * name contains it at every level of the tree.
 * @param params - `q` is matched case-insensitively and narrowed to the children of `parentId` when both are given; the server caps `limit` at 200.
 */
export async function listTags(
    params: { q?: string; parentId?: number; limit?: number; offset?: number } = {},
): Promise<Tag[]> {
    const response = await http.get<Tag[]>('/api/v1/library/tags/', {
        params: {
            q: params.q || undefined,
            parent_id: params.parentId,
            limit: params.limit,
            offset: params.offset,
        },
    })
    return response.data
}

/**
 * Create a tag; staff only unless the deployment opens creation to everyone. A tag created by staff is curated.
 * @param payload - `parent_id` must be a tag within the caller's reach.
 */
export async function createTag(
    payload: { name: string; description?: string; parent_id?: number | null },
): Promise<TagWrite> {
    const response = await http.post<TagWrite>('/api/v1/library/tags/', payload)
    return response.data
}

/**
 * Tag one object; the caller must be able to write it. A second tagging of the same object is a 409.
 * @param tagId - A tag within the caller's reach.
 * @param contentTypeId - The object's content type, from `getRecordingContentTypeId` or `getMediaContentTypeId`.
 * @param objectId - A recording's or media file's hash, or another object's primary key.
 */
export async function tagObject(tagId: number, contentTypeId: number, objectId: string): Promise<TaggedItem> {
    const response = await http.post<TaggedItem>(`/api/v1/library/tags/${tagId}/items/`, {
        content_type_id: contentTypeId,
        object_id: objectId,
    })
    return response.data
}

/**
 * Remove a tag from one object; the caller must be able to write it or have authored the tag.
 * @param tagId - A tag within the caller's reach.
 * @param contentTypeId - The object's content type.
 * @param objectId - A recording's or media file's hash, or another object's primary key.
 */
export async function untagObject(tagId: number, contentTypeId: number, objectId: string): Promise<void> {
    await http.delete(`/api/v1/library/tags/${tagId}/items/`, {
        params: { content_type_id: contentTypeId, object_id: objectId },
    })
}

/**
 * Tag every recording of a dataset that a browser query matches; a member the caller may not write is skipped and
 * counted. The server refuses a query matching more than 5,000 recordings.
 * @param datasetId - The dataset's object hash or primary key.
 * @param tagId - A tag within the caller's reach.
 * @param filters - The browser's filters; none tags every member the caller can read.
 */
export async function tagDatasetMembers(
    datasetId: number | string,
    tagId: number,
    filters?: DatasetBrowseFilters,
): Promise<DatasetTagBatch> {
    const query = new URLSearchParams()
    appendFilters(query, filters)
    const response = await http.post<DatasetTagBatch>(
        `/api/v1/library/datasets/${datasetId}/tags/${tagId}/`,
        null,
        { params: query },
    )
    return response.data
}

/**
 * Remove a tag from every recording of a dataset that a browser query matches; a member is untagged where the
 * caller may write it or authored the tag, and skipped otherwise.
 * @param datasetId - The dataset's object hash or primary key.
 * @param tagId - A tag within the caller's reach.
 * @param filters - The browser's filters; none untags every member the caller can read.
 */
export async function untagDatasetMembers(
    datasetId: number | string,
    tagId: number,
    filters?: DatasetBrowseFilters,
): Promise<DatasetTagBatch> {
    const query = new URLSearchParams()
    appendFilters(query, filters)
    const response = await http.delete<DatasetTagBatch>(`/api/v1/library/datasets/${datasetId}/tags/${tagId}/`, {
        params: query,
    })
    return response.data
}
