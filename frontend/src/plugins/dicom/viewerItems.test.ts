/**
 * The dicom plugin's viewer items: a `?dicom=` link names studies in order, and each is listed through the radiology
 * importer with its OHIF page as the address, named by its description and never by its patient fields.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('#lib/http', () => ({
    http: {
        get: vi.fn(),
    },
}))

import { http } from '#lib/http'
import type { DatasetItem } from '#api/library'
import { studyStoredAsUploaded, type DicomStudyOut } from './api'
import {
    describeAttachedStudy,
    describeStudy,
    describeStudyMember,
    dicomViewerItems,
    studyHashesFromQuery,
    STUDY_IMPORTER,
} from './viewerItems'

const mockGet = vi.mocked(http.get)

/** Three study hashes, the second in upper case. */
const A = 'a'.repeat(32)
const B = 'B'.repeat(32)
const C = '0123456789abcdef'.repeat(2)

const study = (fields: Partial<DicomStudyOut>) => ({
    hash: 'abc',
    modalities: 'US',
    patient_name: 'Doe^Jane',
    study_description: '',
    ...fields,
}) as DicomStudyOut

beforeEach(() => {
    mockGet.mockReset()
})

describe('studyHashesFromQuery', () => {
    it('reads every dicom parameter as a comma-separated list, in order', () => {
        expect(studyHashesFromQuery({ dicom: [`${A}, ${B}`, C] })).toEqual([A, B, C])
        expect(studyHashesFromQuery({ dicom: A })).toEqual([A])
    })

    it('leaves out, with a warning, a value that is not a study hash', () => {
        const warn = vi.spyOn(console, 'warn').mockImplementation(() => undefined)
        const hostile = ['../../../../api/v1/user/me', 'abc', `${A}0`, `${A.slice(1)}g`, `${A}/x`]
        expect(studyHashesFromQuery({ dicom: [...hostile, A].join(',') })).toEqual([A])
        expect(warn).toHaveBeenCalledTimes(hostile.length)
        warn.mockRestore()
    })

    it('names nothing without the parameter', () => {
        expect(studyHashesFromQuery({ files: 'X' })).toEqual([])
        expect(studyHashesFromQuery({ dicom: '' })).toEqual([])
    })
})

describe('describeStudy', () => {
    it('lists the study through the radiology importer at its absolute OHIF page', () => {
        const described = describeStudy(study({ study_description: 'Median nerve US' }))
        expect(described.loader).toBe(STUDY_IMPORTER)
        expect(described.name).toBe('Median nerve US')
        const source = new URL(described.source)
        expect(source.origin).toBe(window.location.origin)
        expect(source.pathname).toBe('/plugin/dicom/ohif/viewer/dicomjson')
        expect(source.searchParams.get('url')).toContain('/studies/abc/ohif-json/')
        expect(described.properties).toBeNull()
    })

    it('falls back to the modality, never to the patient name', () => {
        const described = describeStudy(study({}))
        expect(described.name).toBe('US study')
        expect(described.name).not.toContain('Doe')
    })
})

describe('dicomViewerItems', () => {
    it('reads each named study and describes it', async () => {
        mockGet.mockImplementation(url => Promise.resolve({
            data: study({ hash: String(url).split('/').at(-2), study_description: String(url).split('/').at(-2) }),
        }))
        const items = await dicomViewerItems.fromQuery({ dicom: `${A},${B}` })
        expect(items.map(i => i.name)).toEqual([A, B])
        expect(mockGet).toHaveBeenCalledWith(`/plugin/dicom/api/v1/dicom/studies/${A}/`)
    })

    it('requests nothing for a value that is not a study hash', async () => {
        const warn = vi.spyOn(console, 'warn').mockImplementation(() => undefined)
        expect(await dicomViewerItems.fromQuery({ dicom: '../../../../api/v1/user/me' })).toEqual([])
        expect(mockGet).not.toHaveBeenCalled()
        warn.mockRestore()
    })

    it('answers no items, and reads nothing, for a link naming none', async () => {
        expect(await dicomViewerItems.fromQuery({})).toEqual([])
        expect(mockGet).not.toHaveBeenCalled()
    })
})

describe('describeAttachedStudy', () => {
    it('lists an attached study through the radiology importer, and ignores other types', () => {
        const attachment = {
            details: { modalities: 'US' }, hash: 'abc', name: '', parent: 'P', platform_pass: 'dicom-header',
            type: 'dicomstudy',
        }
        const described = describeAttachedStudy(attachment)
        expect(described?.loader).toBe(STUDY_IMPORTER)
        expect(described?.name).toBe('US study')
        expect(new URL(described!.source).searchParams.get('url')).toContain('/studies/abc/ohif-json/')
        expect(describeAttachedStudy({ ...attachment, type: 'mediafile' })).toBeNull()
    })
})

describe('describeStudyMember', () => {
    it('lists a study member by its listed name through the radiology importer, and ignores other types', () => {
        const item = { object_hash: 'abc', object_name: 'CT volume', object_type: 'dicomstudy' } as DatasetItem
        const described = describeStudyMember(item)
        expect(described?.loader).toBe(STUDY_IMPORTER)
        expect(described?.name).toBe('CT volume')
        expect(describeStudyMember({ ...item, object_type: 'recording' })).toBeNull()
        expect(describeStudyMember({ ...item, object_hash: null })).toBeNull()
    })
})

describe('a study stored as uploaded', () => {
    const MARK = ' (stored as uploaded)'

    it('is tested on the study row by its missing pass version', () => {
        expect(studyStoredAsUploaded({ deidentification_version: null })).toBe(true)
        expect(studyStoredAsUploaded({ deidentification_version: 1 })).toBe(false)
    })

    it('says so in its listed name when a link names it', () => {
        const raw = describeStudy(study({ deidentification_version: null, study_description: 'Median nerve US' }))
        expect(raw.name).toBe(`Median nerve US${MARK}`)
        expect(describeStudy(study({ deidentification_version: null })).name).toBe(`US study${MARK}`)
        expect(describeStudy(study({ deidentification_version: 2, study_description: 'Median nerve US' })).name)
            .toBe('Median nerve US')
    })

    it('says so when attached to a recording, by the pass the attachments listing records', () => {
        const attachment = {
            details: { modalities: 'US' }, hash: 'abc', name: 'Wrist', parent: 'P', platform_pass: 'none',
            type: 'dicomstudy',
        }
        expect(describeAttachedStudy(attachment)?.name).toBe(`Wrist${MARK}`)
        expect(describeAttachedStudy({ ...attachment, platform_pass: 'dicom-header' })?.name).toBe('Wrist')
    })

    it('says so as a dataset member, by the pass the item listing records', () => {
        const item = { object_hash: 'abc', object_name: 'CT volume', object_type: 'dicomstudy' } as DatasetItem
        expect(describeStudyMember({ ...item, platform_pass: 'none' })?.name).toBe(`CT volume${MARK}`)
        expect(describeStudyMember({ ...item, platform_pass: 'dicom-header' })?.name).toBe('CT volume')
        expect(describeStudyMember({ ...item, platform_pass: null })?.name).toBe('CT volume')
    })

    it('stays listed without properties, so it is still loaded up front', () => {
        expect(describeStudy(study({ deidentification_version: null })).properties).toBeNull()
    })
})
