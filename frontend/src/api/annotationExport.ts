/**
 * Bulk annotation export API — downloads events and labels as a JSON or CSV file.
 *
 * Exported files identify annotators by numeric user id only; the staff-only annotator roster is
 * the in-platform mapping from those ids to identities.
 *
 * @package    epicurrents-platform
 */

import { downloadFile } from '#lib/download'
import { http } from '#lib/http'

const EXPORT_URL = '/annotations/api/v1/export'

/** Annotation types the export understands. */
export type ExportType = 'events' | 'labels'

/** Output formats. CSV carries one type per file; JSON carries either or both. */
export type ExportFormat = 'json' | 'csv'

/** One entry of the staff-only annotator roster. */
export interface ExportAnnotator {
    /** User id — the value the export's `author_id` fields carry. */
    id: number
    username: string
    /** Full name, falling back to the username when none is set. */
    name: string
    /** Number of Event rows this user has authored. */
    events: number
    /** Number of Label rows this user has authored. */
    labels: number
}

/** Filters narrowing an export. Every field is optional; omitting all of them exports everything. */
export interface AnnotationExportFilters {
    /** Types to include. CSV requires exactly one. */
    types: ExportType[]
    format: ExportFormat
    /** Recording content hashes. Repeated as `?recording=` for each entry. */
    recordings?: string[]
    /** Restrict to the recordings in one dataset. */
    datasetId?: number | null
    /** Restrict to these annotators' rows, by user id. Staff only. */
    annotatorIds?: number[]
    /** Inclusive lower bound on `created_at`; a bare date covers the whole day. */
    since?: string | null
    /** Inclusive upper bound on `created_at`; a bare date covers the whole day. */
    until?: string | null
    /** Restrict to annotations bound to one signal version. */
    versionId?: string | null
}

/**
 * Fetch the annotator roster: every user who has authored events or labels, with their user id,
 * identity, and per-type counts. Staff only — the server answers 403 for anyone else.
 */
export async function listExportAnnotators(): Promise<ExportAnnotator[]> {
    const response = await http.get(`${EXPORT_URL}/annotators`)
    return response.data.annotators
}

/**
 * Download an annotation export with the given filters.
 *
 * Resolves once the file has been handed to the browser. Rejects with the axios error when the
 * server refuses the request, in which case the caller reads the message with `readBlobError`.
 *
 * @param filters - The filter set to apply; see {@link AnnotationExportFilters}.
 */
export async function downloadAnnotationExport(filters: AnnotationExportFilters): Promise<void> {
    const params: Record<string, unknown> = {
        types: filters.types.join(','),
        format: filters.format,
    }
    if (filters.recordings?.length) {
        params.recording = filters.recordings
    }
    if (filters.datasetId) {
        params.dataset_id = filters.datasetId
    }
    if (filters.annotatorIds?.length) {
        params.annotator_id = filters.annotatorIds
    }
    if (filters.since) {
        params.since = filters.since
    }
    if (filters.until) {
        params.until = filters.until
    }
    if (filters.versionId) {
        params.version_id = filters.versionId
    }
    const fallback = `annotations-${filters.types.join('-')}.${filters.format}`
    await downloadFile(EXPORT_URL, params, fallback)
}
