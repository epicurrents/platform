/**
 * Specs for how the platform describes a dataset's items to the viewer before any of them is loaded.
 *
 * A recording is listed from the metadata the platform recorded at ingest, so its properties have to match what the
 * viewer would show once it loads: the duration, and the channel count without the annotation channel. One without
 * metadata is listed by name and loaded up front, so the null return is what routes it there.
 */

import { describe, expect, it } from 'vitest'
import type { MediaFileDetail } from '#api/media'
import type { Recording, RecordingAttachment } from '#api/recordings'
import type { DatasetBrowseRow } from '#api/library'
import {
    describeMedia,
    describeMediaAttachment,
    describeRecording,
    eagerStudyLoadLimit,
    listedFromBrowseRow,
    listedFromRecording,
    loadsUpFront,
    mergeAttachments,
    recordingProperties,
    videoAttachments,
    type BundleItem,
    type StudyDescription,
} from './datasetStudies'
import type { EpicurrentsApp } from '@epicurrents/core/dist/types'

const recording = (overrides: Partial<Recording> = {}): Recording => ({
    content_hash: '',
    created_at: '2026-11-01T00:00:00Z',
    deleted_at: null,
    display_name: 'upload-20261119T082015Z.edf',
    events: [],
    file_extension: '.EDF',
    file_size: 0,
    has_custom_name: false,
    hash: 'a'.repeat(32),
    interruptions: [],
    labels: [],
    meta: {
        data_record_count: 600,
        data_record_duration: 1,
        discontinuous: false,
        duration: 600,
        format: 'edf+',
        signal_count: 3,
        signals: [
            { is_annotation_channel: false, label: 'Fp1' },
            { is_annotation_channel: false, label: 'Fp2' },
            { is_annotation_channel: true, label: 'EDF Annotations' },
        ],
    },
    modality: 'eeg',
    original_name: null,
    processing_error: null,
    public_source: '',
    status: 'ready',
    stored_hash: null,
    trashed_collection: null,
    ...overrides,
})

describe('recording properties', () => {
    it('lists the duration and the signal channels without the annotation channel', () => {
        expect(recordingProperties(listedFromRecording(recording()))).toEqual([['duration', 600], ['signals', 2]])
    })
    it('leaves the channel count out when the descriptors are absent', () => {
        const meta = { ...recording().meta!, signals: undefined }
        expect(recordingProperties(listedFromRecording(recording({ meta })))).toEqual([['duration', 600]])
    })
    it('holds none before processing has written the metadata', () => {
        expect(recordingProperties(listedFromRecording(recording({ meta: null })))).toBeNull()
    })
})

describe('a browser row as listed', () => {
    const row = (overrides: Partial<DatasetBrowseRow> = {}): DatasetBrowseRow => ({
        channel_count: 19,
        channel_layout: null,
        created_at: '2026-11-01T00:00:00Z',
        deidentification_version: 5,
        display_name: 'subject-007.edf',
        duration: 900,
        file_extension: '.edf',
        hash: 'b'.repeat(32),
        modality: 'eeg',
        public_source: '',
        sampling_rate_max: 256,
        sampling_rate_min: 256,
        subject: {},
        tag_count: 0,
        tags: [],
        ...overrides,
    })
    it('lists the same properties as the detail, from the ingest metadata the row carries', () => {
        expect(recordingProperties(listedFromBrowseRow(row()))).toEqual([['duration', 900], ['signals', 19]])
        expect(recordingProperties(listedFromBrowseRow(row({ channel_count: null })))).toEqual([['duration', 900]])
        expect(recordingProperties(listedFromBrowseRow(row({ duration: null, channel_count: null })))).toBeNull()
    })
    it('keeps a dot that is part of the name', () => {
        expect(describeRecording(listedFromBrowseRow(row({ display_name: 'Sleep v1.5' }))).name).toBe('Sleep v1.5')
        expect(describeRecording(listedFromBrowseRow(row({ display_name: 'Sleep v1.5.EDF' }))).name).toBe('Sleep v1.5')
    })
    it('names it by the grantee-visible name and loads it by its hash', () => {
        const description = describeRecording(listedFromBrowseRow(row()))
        expect(description.name).toBe('subject-007')
        expect(description.loader).toBe('eeg/edf-file')
        expect(new URL(description.source).pathname).toBe(`/recordings/api/v1/${'b'.repeat(32)}/file`)
    })
})

describe('describing a recording', () => {
    it('names the importer by modality and a lower-case extension', () => {
        expect(describeRecording(listedFromRecording(recording())).loader).toBe('eeg/edf-file')
    })
    it('lists it under its name without the extension', () => {
        expect(describeRecording(listedFromRecording(recording())).name).toBe('upload-20261119T082015Z')
    })
    it('loads the default version from an absolute URL, with the share token when given', () => {
        const source = new URL(describeRecording(listedFromRecording(recording()), 'token-1').source)
        expect(source.pathname).toBe(`/recordings/api/v1/${'a'.repeat(32)}/file`)
        expect(source.searchParams.get('version')).toBe('default')
        expect(source.searchParams.get('share_token')).toBe('token-1')
        const tokenless = new URL(describeRecording(listedFromRecording(recording())).source)
        expect(tokenless.searchParams.has('share_token')).toBe(false)
    })
})

describe('describing a media file', () => {
    const media = (overrides: Partial<MediaFileDetail>) => ({
        content_hash: 'b'.repeat(32),
        display_name: 'Clip',
        file_extension: '.mp4',
        media_type: 'video',
        ...overrides,
    }) as MediaFileDetail

    it('opens a native media type by its type and a document by its extension', () => {
        expect(describeMedia(media({}))?.loader).toBe('media/video-url')
        expect(describeMedia(media({ file_extension: '.PDF', media_type: 'document' }))?.loader).toBe('doc/pdf-url')
    })
    it('lists it by name with no properties, so it loads up front', () => {
        expect(describeMedia(media({}))?.properties).toBeNull()
    })
    it('returns null for a format the viewer cannot open', () => {
        expect(describeMedia(media({ file_extension: '.zip', media_type: 'document' }))).toBeNull()
    })
    it('loads it from its file, carrying the share token when given, as an attachment does', () => {
        const source = new URL(describeMedia(media({}), 'tok')!.source)
        expect(source.pathname).toBe(`/media/api/v1/${'b'.repeat(32)}/file`)
        expect(source.searchParams.get('share_token')).toBe('tok')
        expect(new URL(describeMedia(media({}))!.source).searchParams.has('share_token')).toBe(false)
    })
})

describe('what loads up front', () => {
    const listedWith = (properties: StudyDescription['properties']): StudyDescription => ({
        loader: 'eeg/edf-file', name: 'A', properties, source: 'https://example.test/a',
    })

    it('loads a member the platform holds no metadata for, and lists one it does', () => {
        expect(loadsUpFront(false, listedWith(null), false)).toBe(true)
        expect(loadsUpFront(false, listedWith([['duration', 60]]), false)).toBe(false)
    })
    it('loads every member of a bundle flagged to preload', () => {
        expect(loadsUpFront(true, listedWith([['duration', 60]]), false)).toBe(true)
    })
    it('never loads a child, which has no metadata by construction, in a preloading bundle either', () => {
        expect(loadsUpFront(false, listedWith(null), true)).toBe(false)
        expect(loadsUpFront(true, listedWith(null), true)).toBe(false)
    })
})

describe('mergeAttachments', () => {
    const P1 = 'a'.repeat(32)
    const P2 = 'b'.repeat(32)
    const recordingItem = (hash: string): BundleItem => ({
        kind: 'recording',
        listed: { channel_count: null, duration: null, file_extension: '.edf', hash, modality: 'eeg', name: hash },
        recording: null,
        videos: null,
    })
    const attached = (
        parent: string, hash: string, details: RecordingAttachment['details'] = { media_type: 'document' },
        type = 'mediafile',
    ): RecordingAttachment => ({ details, hash, name: hash, parent, platform_pass: 'none', type })
    /** Describes everything, a video included, by its hash, as a careless source might. */
    const describeAll = (attachment: RecordingAttachment): StudyDescription => ({
        loader: 'doc/pdf-url',
        name: attachment.name,
        properties: null,
        source: `https://example.test/${attachment.hash}`,
    })
    const kinds = (items: BundleItem[]) => items.map(item => {
        if (item.kind === 'recording') {
            return `recording:${item.listed.hash}`
        }
        return item.kind === 'described'
            ? `child:${item.description.name}@${item.parent}`
            : `media:${item.media.content_hash}`
    })

    it('lists each attachment after its own recording, keyed by the upper-cased parent hash', () => {
        const merged = mergeAttachments(
            [recordingItem(P1), recordingItem(P2)],
            [attached(P2.toUpperCase(), 'x'), attached(P1, 'y'), attached(P2, 'z')],
            describeAll,
        )
        expect(kinds(merged)).toEqual([
            `recording:${P1}`, `child:y@${P1.toUpperCase()}`,
            `recording:${P2}`, `child:x@${P2.toUpperCase()}`, `child:z@${P2.toUpperCase()}`,
        ])
    })

    it('gives a video to its recording and never lists it as a child, whatever would describe it', () => {
        const merged = mergeAttachments(
            [recordingItem(P1)],
            [
                attached(P1, 'v1', { is_supported: true, media_type: 'video', time_offset: 12.5 }),
                attached(P1, 'v2', { is_supported: true, media_type: 'video', time_offset: null }),
                attached(P1, 'v3', { is_supported: false, media_type: 'video', time_offset: 3 }),
            ],
            describeAll,
        )
        expect(merged).toHaveLength(1)
        expect(merged[0]).toMatchObject({
            videos: [{ hash: 'v1', time_offset: 12.5 }, { hash: 'v2', time_offset: null }],
        })
    })

    it('answers an empty video list for a listed recording without one, and null for one the listing missed', () => {
        const merged = mergeAttachments([recordingItem(P1), recordingItem(P2)], [], describeAll, [P2.toUpperCase()])
        expect(merged[0]).toMatchObject({ videos: [] })
        expect(merged[1]).toMatchObject({ videos: null })
    })

    it('lists once what the bundle already holds, what is attached twice, and nothing that is not described', () => {
        const member: BundleItem = { kind: 'media', media: { content_hash: 'HELD' } as MediaFileDetail }
        const study: BundleItem = { description: describeAll(attached(P1, 'study')), kind: 'described' }
        const merged = mergeAttachments(
            [recordingItem(P1), member, study],
            [
                attached(P1, 'held'),
                attached(P1, 'study', {}, 'dicomstudy'),
                attached(P1, 'twice'),
                attached(P1, 'twice'),
                attached(P1, 'unknown', {}, 'other'),
            ],
            attachment => attachment.type === 'other' ? null : describeAll(attachment),
        )
        expect(kinds(merged)).toEqual([
            `recording:${P1}`, `child:twice@${P1.toUpperCase()}`, 'media:HELD', 'child:study@undefined',
        ])
    })

    it('leaves the other items where they were', () => {
        const member: BundleItem = { kind: 'media', media: { content_hash: 'M' } as MediaFileDetail }
        expect(mergeAttachments([member], [], describeAll)).toEqual([member])
    })
})

describe('videoAttachments', () => {
    it('places each video at its offset, at its absolute file address', () => {
        const [first, second] = videoAttachments([{ hash: 'v1', time_offset: 12.5 }, { hash: 'v2', time_offset: null }])
        expect(first.startTime).toBe(12.5)
        expect(second.startTime).toBe(0)
        const url = new URL(first.url)
        expect(url.origin).toBe(window.location.origin)
        expect(url.pathname).toBe('/media/api/v1/v1/file')
        expect(url.searchParams.has('share_token')).toBe(false)
    })
    it('carries the share token, which the video element can send no other way', () => {
        const [video] = videoAttachments([{ hash: 'v1', time_offset: 0 }], 'tok')
        expect(new URL(video.url).searchParams.get('share_token')).toBe('tok')
    })
})

describe('the eager load limit', () => {
    const app = (settings: object) => ({ runtime: { SETTINGS: { app: settings } } }) as unknown as EpicurrentsApp

    it('reads the viewer setting', () => {
        expect(eagerStudyLoadLimit(app({ eagerStudyLoadLimit: 7 }))).toBe(7)
    })
    it('falls back to the default where the setting is absent', () => {
        expect(eagerStudyLoadLimit(app({}))).toBe(50)
    })
})

describe('describeMediaAttachment', () => {
    const attachment = (details: RecordingAttachment['details'], type = 'mediafile'): RecordingAttachment => ({
        details,
        hash: 'abc',
        name: 'Report',
        parent: 'P',
        platform_pass: 'none',
        type,
    })

    it('opens a document through its importer at its file, carrying the share token', () => {
        const described = describeMediaAttachment(
            attachment({ file_extension: '.pdf', is_supported: true, media_type: 'document' }), 'tok',
        )
        expect(described?.loader).toBe('doc/pdf-url')
        expect(described?.name).toBe('Report')
        const source = new URL(described!.source)
        expect(source.pathname).toBe('/media/api/v1/abc/file')
        expect(source.searchParams.get('share_token')).toBe('tok')
    })

    it('leaves a video to its recording, and skips an unsupported file or another type', () => {
        expect(describeMediaAttachment(attachment({ media_type: 'video', file_extension: '.mp4' }))).toBeNull()
        expect(describeMediaAttachment(attachment({ media_type: 'document', file_extension: '.pdf',
            is_supported: false }))).toBeNull()
        expect(describeMediaAttachment(attachment({}, 'dicomstudy'))).toBeNull()
    })
})
