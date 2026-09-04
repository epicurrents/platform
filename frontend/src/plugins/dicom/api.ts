/**
 * API client for the *dicom* plugin endpoints at /plugin/dicom/api/v1/dicom/.
 */

import { http } from '#lib/http'

export interface DicomSeriesOut {
    series_instance_uid: string
    series_description: string
    series_number: string
    series_date: string
    modality: string
    slice_thickness: string
    instance_count: number
}

export interface DicomStudyOut {
    hash: string
    study_instance_uid: string
    study_date: string
    study_time: string
    study_description: string
    patient_name: string
    patient_id: string
    patient_sex: string
    patient_age: string
    accession_number: string
    num_instances: number
    modalities: string
    created_at: string
    is_author: boolean
}

export interface DicomStudyDetailOut extends DicomStudyOut {
    series: DicomSeriesOut[]
}

export interface DicomUploadStudyOut {
    hash: string
    study_instance_uid: string
    instances_added: number
}

export interface DicomUploadFileOut {
    filename: string
    accepted: boolean
    study_hash: string | null
    error: string | null
}

export interface DicomUploadOut {
    studies: DicomUploadStudyOut[]
    files: DicomUploadFileOut[]
    accepted: number
    rejected: number
}

const BASE = '/plugin/dicom/api/v1/dicom'

export const dicomApi = {
    listStudies(): Promise<DicomStudyOut[]> {
        return http.get<DicomStudyOut[]>(`${BASE}/studies/`).then(r => r.data)
    },

    getStudy(hash: string): Promise<DicomStudyDetailOut> {
        return http.get<DicomStudyDetailOut>(`${BASE}/studies/${hash}/`).then(r => r.data)
    },

    deleteStudy(hash: string): Promise<void> {
        return http.delete(`${BASE}/studies/${hash}/`)
    },

    shareStudy(hash: string, username: string): Promise<void> {
        return http.post(`${BASE}/studies/${hash}/share/`, { username })
    },

    revokeStudyAccess(hash: string, username: string): Promise<void> {
        return http.delete(`${BASE}/studies/${hash}/share/${username}/`)
    },

    uploadFiles(files: File[]): Promise<DicomUploadOut> {
        const form = new FormData()
        for (const file of files) {
            form.append('files', file)
        }
        return http.post<DicomUploadOut>(`${BASE}/upload/`, form, {
            headers: { 'Content-Type': 'multipart/form-data' },
        }).then(r => r.data)
    },

    /** URL of the OHIF viewer pre-loaded with a specific study. */
    ohifViewerUrl(studyHash: string): string {
        // OHIF resolves the encoded URL with its dicomjson datasource; the
        // backend serves the JSON at /plugin/dicom/api/v1/dicom/studies/{hash}/ohif-json/.
        const jsonUrl = encodeURIComponent(
            `${window.location.origin}${BASE}/studies/${studyHash}/ohif-json/`
        )
        return `/plugin/dicom/viewer/?url=${jsonUrl}`
    },
}
