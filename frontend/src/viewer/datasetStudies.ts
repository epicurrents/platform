/**
 * How the platform lists the items of a dataset in the viewer before any of them is loaded.
 *
 * Every item becomes a deferred study: a listing the viewer shows by name, with the importer and URL to load it from
 * when it is opened. A recording that has finished processing also carries what the viewer would otherwise read from
 * its header to list it, its duration and channel count, which the platform parsed at ingest and serves as trusted
 * metadata; listing from it costs no fetch and no reader. An item without such metadata is listed by name, and the
 * caller loads up to `app.eagerStudyLoadLimit` of those straight away so they list with their properties.
 *
 * A recording is listed from a `ListedRecording`, which a dataset's browser row and a recording's detail both give, so
 * a dataset of any size is listed from its browser pages rather than one detail request per member.
 */

import type { DatasetBrowseRow } from '#api/library'
import type { MediaFileDetail } from '#api/media'
import { recordingName, type Recording, type RecordingAttachment } from '#api/recordings'
import type { DataResource, EpicurrentsApp } from '@epicurrents/core/dist/types'

/** A property a study is listed with: its duration in seconds, or its number of signal channels. */
export type ListedProperty = [string, number]

/** What a recording is listed in the viewer by. */
export interface ListedRecording {
    /** The recording's hash, which names it in the recording routes. */
    hash: string
    /** The name to list it under, which may end with its file extension. */
    name: string
    modality: string
    file_extension: string
    /** Duration in seconds, null until processing has written the metadata. */
    duration: number | null
    /** Signal channels, the annotation channel of an EDF+ file not among them; null when the platform holds none. */
    channel_count: number | null
}

/**
 * A recording's detail as the viewer lists it, under the name the caller is shown.
 * @param recording - The recording's detail.
 */
export function listedFromRecording(recording: Recording): ListedRecording {
    const signals = recording.meta?.signals
    return {
        channel_count: signals?.length ? signals.filter(signal => !signal.is_annotation_channel).length : null,
        duration: recording.meta?.duration ?? null,
        file_extension: recording.file_extension,
        hash: recording.hash,
        modality: recording.modality,
        name: recordingName(recording),
    }
}

/**
 * A dataset browser row as the viewer lists it, under the grantee-visible name the browser shows every reader.
 * @param row - A row of the dataset browser.
 */
export function listedFromBrowseRow(row: DatasetBrowseRow): ListedRecording {
    return {
        channel_count: row.channel_count,
        duration: row.duration,
        file_extension: row.file_extension,
        hash: row.hash,
        modality: row.modality,
        name: row.display_name,
    }
}

/**
 * The part of the viewer's deferred-study API the platform calls. The application the platform runs is the viewer
 * edition built from source, which has it; the `@epicurrents/core` dev dependency the SPA takes its types from
 * predates it, so it is declared here until that dependency moves to a release that does.
 */
export type DeferredStudyApp = EpicurrentsApp & {
    addDeferredStudy (
        study: {
            loader: string
            name: string
            onLoaded?: (resource: DataResource) => void | Promise<void>
            options?: { isValidated?: boolean, name?: string }
            properties?: ListedProperty[]
            source: string
        },
        options?: { dataset?: unknown },
    ): DataResource | null
    openDeferredResource (resource: DataResource): Promise<DataResource | null>
}

/** The viewer's eager load limit, `app.eagerStudyLoadLimit`, or its default where the setting is absent. */
export function eagerStudyLoadLimit(app: EpicurrentsApp): number {
    const limit = (app.runtime.SETTINGS.app as { eagerStudyLoadLimit?: number }).eagerStudyLoadLimit
    return typeof limit === 'number' ? limit : 50
}

/** What the viewer needs to list an item and to load it when it is opened. */
export interface StudyDescription {
    /** Name of the viewer importer that opens the item. */
    loader: string
    /** Name to list the item under. */
    name: string
    /** Absolute URL to load the item from. */
    source: string
    /** Properties to list the item with until it loads, or null when the platform holds none for it. */
    properties: ListedProperty[] | null
}

/** Media types the viewer renders with the browser's own element, each through its `media/<type>-url` importer. */
const NATIVE_MEDIA_TYPES = new Set(['audio', 'image', 'video'])

/**
 * Map a media file to the viewer importer that opens it: a native media type by its type, which hands the URL to
 * the browser's own element, and a document by its extension. The platform always loads media by URL (the bytes live
 * behind /media/api/v1/<hash>/file), so this dispatches to the ``-url`` importer rather than the ``-file`` one,
 * which expects a File object from a picker and misreads a URL string as an asset-relative filename
 * (``/assets/undefined`` requests against the SPA host). Returns null for a format the viewer cannot open; the caller
 * skips the item rather than failing the whole dataset load.
 */
export function importerForMedia(media: Pick<MediaFileDetail, 'file_extension' | 'media_type'>): string | null {
    if (NATIVE_MEDIA_TYPES.has(media.media_type)) {
        return `media/${media.media_type}-url`
    }
    const ext = (media.file_extension || '').toLowerCase()
    if (ext === '.pdf') return 'doc/pdf-url'
    if (ext === '.md') return 'doc/htm-url'
    return null
}

/**
 * The properties a recording is listed with, from the metadata the platform recorded at ingest: its duration and the
 * number of signal channels, the annotation channel of an EDF+ file not among them, as the viewer counts them once the
 * recording loads. Null until processing has written the metadata.
 * @param recording - The recording as listed.
 */
export function recordingProperties(recording: ListedRecording): ListedProperty[] | null {
    if (recording.duration === null) {
        return null
    }
    const properties: ListedProperty[] = [['duration', recording.duration]]
    if (recording.channel_count !== null) {
        properties.push(['signals', recording.channel_count])
    }
    return properties
}

/**
 * Describe a recording for the viewer. The URL is absolute because the reader fetches ranges from a worker, which has
 * no document base URL to resolve a relative one against. It names the default version, the one the recording's
 * reconstruction stages built and the stored file when there is none.
 * @param recording - The recording as listed.
 * @param shareToken - Share token the dataset was opened with, if any.
 */
/**
 * The recording's name without its file extension. Only that extension is taken off: a browser row's name is the
 * label its author gave it, which carries none and may hold a dot of its own.
 */
function nameWithoutExtension (recording: ListedRecording) {
    const extension = recording.file_extension.toLowerCase()
    if (extension && recording.name.toLowerCase().endsWith(extension)) {
        return recording.name.slice(0, -extension.length)
    }
    return recording.name
}

export function describeRecording(recording: ListedRecording, shareToken?: string): StudyDescription {
    const query = new URLSearchParams({ version: 'default' })
    if (shareToken) {
        query.set('share_token', shareToken)
    }
    const relativePath = `/recordings/api/v1/${recording.hash}/file?${query.toString()}`
    // Loader names are registered in lower case (``eeg/edf-file``), and older rows can carry an upper-case
    // extension such as ``.EDF``.
    const extension = recording.file_extension.replace(/^\./, '').toLowerCase()
    return {
        loader: `${recording.modality}/${extension}-file`,
        name: nameWithoutExtension(recording),
        properties: recordingProperties(recording),
        source: new URL(relativePath, import.meta.url).toString(),
    }
}

/**
 * Describe a media file for the viewer, or return null when the viewer has no importer for its format. The media
 * download endpoint is session-authenticated, so a dataset opened with a share token does not render its media.
 * @param media - The media file.
 */
export function describeMedia(media: MediaFileDetail): StudyDescription | null {
    const loader = importerForMedia(media)
    if (!loader) {
        return null
    }
    // The relative path is bound to a local before ``new URL(...)`` because Vite's minifier mis-handles an inline
    // template literal there and emits ``new URL(Object.assign({})[`/media/...`], ...)``, which resolves to
    // ``/assets/undefined``.
    const relativePath = `/media/api/v1/${media.content_hash}/file`
    return {
        loader,
        name: media.display_name,
        properties: null,
        source: new URL(relativePath, import.meta.url).toString(),
    }
}

/**
 * Describe a media file attached to a recording, or answer null for one listed some other way: an attachment of
 * another type, a format the live allowlist no longer admits, or a video, which plays on its recording's timeline
 * rather than as an item of its own. The address carries the share token the recording was read with, which the
 * media file endpoint accepts.
 * @param attachment - The attachment as the attachments listing gives it.
 * @param shareToken - Share token the recording was read with, if any.
 */
export function describeMediaAttachment(attachment: RecordingAttachment, shareToken?: string): StudyDescription | null {
    const details = attachment.details
    if (attachment.type !== 'mediafile' || details.is_supported === false || details.media_type === 'video') {
        return null
    }
    const loader = importerForMedia({
        file_extension: String(details.file_extension ?? ''),
        media_type: String(details.media_type ?? ''),
    })
    if (!loader) {
        return null
    }
    const source = new URL(`/media/api/v1/${attachment.hash}/file`, window.location.origin)
    if (shareToken) {
        source.searchParams.set('share_token', shareToken)
    }
    return {
        loader,
        name: attachment.name,
        properties: null,
        source: source.toString(),
    }
}
