/**
 * Specs for how the platform describes a dataset's items to the viewer before any of them is loaded.
 *
 * A recording is listed from the metadata the platform recorded at ingest, so its properties have to match what the
 * viewer would show once it loads: the duration, and the channel count without the annotation channel. One without
 * metadata is listed by name and loaded up front, so the null return is what routes it there.
 */

import { describe, expect, it } from 'vitest'
import type { MediaFileDetail } from '#api/media'
import type { Recording } from '#api/recordings'
import { describeMedia, describeRecording, eagerStudyLoadLimit, recordingProperties } from './datasetStudies'
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
        expect(recordingProperties(recording())).toEqual([['duration', 600], ['signals', 2]])
    })
    it('leaves the channel count out when the descriptors are absent', () => {
        const meta = { ...recording().meta!, signals: undefined }
        expect(recordingProperties(recording({ meta }))).toEqual([['duration', 600]])
    })
    it('holds none before processing has written the metadata', () => {
        expect(recordingProperties(recording({ meta: null }))).toBeNull()
    })
})

describe('describing a recording', () => {
    it('names the importer by modality and a lower-case extension', () => {
        expect(describeRecording(recording()).loader).toBe('eeg/edf-file')
    })
    it('lists it under its name without the extension', () => {
        expect(describeRecording(recording()).name).toBe('upload-20261119T082015Z')
    })
    it('loads the default version from an absolute URL, with the share token when given', () => {
        const source = new URL(describeRecording(recording(), 'token-1').source)
        expect(source.pathname).toBe(`/recordings/api/v1/${'a'.repeat(32)}/file`)
        expect(source.searchParams.get('version')).toBe('default')
        expect(source.searchParams.get('share_token')).toBe('token-1')
        expect(new URL(describeRecording(recording()).source).searchParams.has('share_token')).toBe(false)
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
