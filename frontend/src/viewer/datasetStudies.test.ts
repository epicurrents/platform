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
import type { DatasetBrowseRow } from '#api/library'
import {
    describeMedia,
    describeRecording,
    eagerStudyLoadLimit,
    listedFromBrowseRow,
    listedFromRecording,
    recordingProperties,
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
        expect(new URL(describeRecording(listedFromRecording(recording())).source).searchParams.has('share_token')).toBe(false)
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
