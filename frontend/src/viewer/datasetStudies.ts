/**
 * How the platform lists the items of a dataset in the viewer before any of them is loaded.
 *
 * Every item is listed as a resource of its module that has read nothing: the viewer shows it by name, and opening it
 * loads it in place with the importer and URL it was listed with. A recording that has finished processing also
 * carries what the viewer would otherwise read from its header to list it, its duration and channel count, which the
 * platform parsed at ingest and serves as trusted metadata; listing from it costs no fetch and no reader. A dataset
 * member without such metadata is listed by name, and the caller loads up to `app.eagerStudyLoadLimit` of those
 * straight away so they list with their properties. An attachment, listed under its recording, always waits to be
 * opened.
 *
 * A recording is listed from a `ListedRecording`, which a dataset's browser row and a recording's detail both give, so
 * a dataset of any size is listed from its browser pages rather than one detail request per member.
 */

import type { DatasetBrowseRow } from '#api/library'
import type { MediaFileDetail } from '#api/media'
import { recordingName, type Recording, type RecordingAttachment } from '#api/recordings'
import type { DataResource, EpicurrentsApp, VideoAttachment } from '@epicurrents/core/dist/types'

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

/** A resource listed in the viewer, which loads in place, on itself, when it is opened or when `load` is called. */
export type ListedResource = DataResource & {
    /** Read the study and finish the setup, resolving false when either fails, which leaves the resource in error. */
    load (): Promise<boolean>
}

/**
 * The part of the viewer's listing API the platform calls. The application the platform runs is the viewer edition
 * built from source, which has it; the `@epicurrents/core` dev dependency the SPA takes its types from predates it, so
 * it is declared here until that dependency moves to a release that does.
 */
export type ListingStudyApp = EpicurrentsApp & {
    listStudy (
        listing: {
            loader: string
            name: string
            onLoad?: (resource: DataResource) => void | Promise<void>
            options?: { isValidated?: boolean, name?: string }
            properties?: ListedProperty[]
            source: string
        },
        options?: { dataset?: unknown, parent?: DataResource },
    ): ListedResource | null
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
    if (ext === '.pdf') {
        return 'doc/pdf-url'
    }
    if (ext === '.md') {
        return 'doc/htm-url'
    }
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

/**
 * Describe a recording for the viewer. The URL is absolute because the reader fetches ranges from a worker, which has
 * no document base URL to resolve a relative one against. It names the default version, the one the recording's
 * reconstruction stages built and the stored file when there is none.
 * @param recording - The recording as listed.
 * @param shareToken - Share token the dataset was opened with, if any.
 */
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
 * Describe a media file for the viewer, or return null when the viewer has no importer for its format. The address
 * carries the share token the dataset was opened with, which the media file endpoint accepts.
 * @param media - The media file.
 * @param shareToken - Share token the dataset was opened with, if any.
 */
export function describeMedia(media: MediaFileDetail, shareToken?: string): StudyDescription | null {
    const loader = importerForMedia(media)
    if (!loader) {
        return null
    }
    // The relative path is bound to a local before ``new URL(...)`` because Vite's minifier mis-handles an inline
    // template literal there and emits ``new URL(Object.assign({})[`/media/...`], ...)``, which resolves to
    // ``/assets/undefined``.
    const relativePath = `/media/api/v1/${media.content_hash}/file`
    const source = new URL(relativePath, import.meta.url)
    if (shareToken) {
        source.searchParams.set('share_token', shareToken)
    }
    return {
        loader,
        name: media.display_name,
        properties: null,
        source: source.toString(),
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
export function describeMediaAttachment(
    attachment: RecordingAttachment,
    shareToken?: string,
): StudyDescription | null {
    const details = attachment.details
    if (attachment.type !== 'mediafile' || details.is_supported === false || isAttachedVideo(attachment)) {
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

/** True for an attached media file that is a video, which plays on its recording's timeline. */
export function isAttachedVideo(attachment: RecordingAttachment): boolean {
    return attachment.type === 'mediafile' && attachment.details.media_type === 'video'
}

/** A video attached to a recording, as the attachments listing gives it. */
export interface AttachedVideo {
    /** The media file's content hash. */
    hash: string
    /** Position in seconds of the video's start on the recording's timeline, or null when unplaced. */
    time_offset: number | null
}

/**
 * A dataset bundle's item after the platform-side fetch, discriminated so the viewer page can pick the importer and
 * URL without re-querying. A recording is listed from what its dataset's browser rows give; its detail, which carries
 * the interruption table, is read when it is loaded, unless the link named it and it was read up front. Its `videos`
 * are the playable videos attached to it, or null when no attachments listing covers it. A described item is a
 * plugin's, or an attachment, which carries the hash of its parent recording.
 */
export type BundleItem =
    | { kind: 'recording'; listed: ListedRecording; recording: Recording | null; videos: AttachedVideo[] | null }
    | { kind: 'media'; media: MediaFileDetail }
    | { kind: 'described'; description: StudyDescription; parent?: string }

/**
 * Join the attachments listing into a bundle's items. Each recording the listing covered is given its playable
 * videos, and is followed by its other attachments as items carrying its upper-cased hash as `parent`, under which
 * the viewer lists them. A video is never listed as an item, whatever would describe it, since it plays on its
 * recording's timeline; nor is a media file the bundle already holds as an item of its own, an attachment whose
 * address the bundle already lists (a plugin's item that is both a member and attached, or a row listed twice), or
 * one nothing describes. A recording named in *unlisted* keeps `videos: null`, which tells its loader the listing
 * knows nothing of it.
 * @param items - The bundle's items, in order.
 * @param attachments - The attachments listing of the bundle's recordings.
 * @param describe - Describes an attachment that is not a video, or answers null for one nothing opens.
 * @param unlisted - Hashes of the recordings the listing could not be read for.
 */
export function mergeAttachments(
    items: BundleItem[],
    attachments: RecordingAttachment[],
    describe: (attachment: RecordingAttachment) => StudyDescription | null,
    unlisted: string[] = [],
): BundleItem[] {
    const held = new Set(items.flatMap(item => item.kind === 'media' ? [item.media.content_hash.toUpperCase()] : []))
    const heldSources = new Set(items.flatMap(item => item.kind === 'described' ? [item.description.source] : []))
    const unlistedParents = new Set(unlisted.map(hash => hash.toUpperCase()))
    const children = new Map<string, BundleItem[]>()
    const videos = new Map<string, AttachedVideo[]>()
    for (const attachment of attachments) {
        const parent = attachment.parent.toUpperCase()
        if (isAttachedVideo(attachment)) {
            if (attachment.details.is_supported !== false) {
                const offset = attachment.details.time_offset
                const listed = videos.get(parent) ?? []
                listed.push({ hash: attachment.hash, time_offset: typeof offset === 'number' ? offset : null })
                videos.set(parent, listed)
            }
            continue
        }
        if (held.has(attachment.hash.toUpperCase())) {
            continue
        }
        const description = describe(attachment)
        if (!description || heldSources.has(description.source)) {
            continue
        }
        heldSources.add(description.source)
        const listed = children.get(parent) ?? []
        listed.push({ kind: 'described', description, parent })
        children.set(parent, listed)
    }
    return items.flatMap((item): BundleItem[] => {
        if (item.kind !== 'recording') {
            return [item]
        }
        const hash = item.listed.hash.toUpperCase()
        const recording = unlistedParents.has(hash) ? item : { ...item, videos: videos.get(hash) ?? [] }
        return [recording, ...(children.get(hash) ?? [])]
    })
}

/**
 * The videos attached to a recording as the viewer's video window takes them, each at its absolute file address. The
 * address carries the share token the recording was read with, which the media file endpoint accepts, since the
 * browser's video element fetches it directly and can send nothing else.
 * @param videos - The recording's attached videos.
 * @param shareToken - Share token the recording was read with, if any.
 */
export function videoAttachments(videos: AttachedVideo[], shareToken?: string): VideoAttachment[] {
    return videos.map(video => {
        const url = new URL(`/media/api/v1/${video.hash}/file`, window.location.origin)
        if (shareToken) {
            url.searchParams.set('share_token', shareToken)
        }
        return {
            // The platform stores no duration, so the end point is left open. The video window plays the first video
            // directly and does not depend on it.
            endTime: Number.MAX_SAFE_INTEGER,
            group: 0,
            startTime: video.time_offset ?? 0,
            syncPoints: [],
            url: url.toString(),
        }
    })
}

/**
 * Whether an item loads when the viewer opens rather than when the item is opened: a member of a bundle flagged to
 * preload, or one the platform holds no metadata to list. Never a child, an attachment listed under its recording,
 * which loads in place when it is opened: loading children up front would read a document per recording before the
 * first recording opens, and take the eager slots of the recordings a preloading workflow waits for.
 * @param preload - The bundle loads its members up front.
 * @param description - What the item is listed by.
 * @param isChild - The item is listed under a parent recording.
 */
export function loadsUpFront(preload: boolean, description: StudyDescription, isChild: boolean): boolean {
    return !isChild && (preload || !description.properties)
}
