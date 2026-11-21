/**
 * API client for the *dicom* plugin endpoints at /plugin/dicom/api/v1/dicom/.
 */

import { http } from '#lib/http'
import type { AccessRight, GrantAccessPayload } from '#api/library'

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
    /** Whether the caller may manage the study's access rights: author, superuser or a `can_share` grant. */
    can_share: boolean
    /** The de-identification pass every file was rewritten by, or null when any file is stored as uploaded. */
    deidentification_version: number | null
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
    /** The file's position in the request; the report is not in request order. */
    index: number
    filename: string
    accepted: boolean
    study_hash: string | null
    error: string | null
    /** A refusal the client can act on: `BURNED_IN_CONFIRMATION_REQUIRED`. */
    code: string | null
}

/** A study or series description that looks like it holds an identifier; it is stored as written. */
export interface DicomNameWarningOut {
    field: string
    kind: string
    message: string
    study_hash: string
}

export interface DicomUploadOut {
    studies: DicomUploadStudyOut[]
    files: DicomUploadFileOut[]
    accepted: number
    rejected: number
    warnings: DicomNameWarningOut[]
}

const BASE = '/plugin/dicom/api/v1/dicom'

/**
 * The refusal code for an image that may show identifying text in its pixels (ultrasound, secondary capture, or a
 * flagged burned-in annotation). The server decides which files it applies to; resubmitting them with
 * `confirmBurnedIn` records that the uploader checked them.
 */
export const BURNED_IN_CONFIRMATION_REQUIRED = 'burned_in_confirmation_required'

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

    /** The study's access rights other than its author's own. */
    listAccess(hash: string): Promise<AccessRight[]> {
        return http.get<AccessRight[]>(`${BASE}/studies/${hash}/access/`).then(r => r.data)
    },

    /**
     * Grant a user an access right on a study. Only user targets are accepted for now. A study is de-identified, or
     * not, when it is stored, and every grantee receives the stored files, so the payload's `apply_middleware` is
     * ignored.
     * @param hash - The study's `hash` from the study listing.
     * @param payload - The grant, as the shared access-rights panel builds it.
     */
    grantAccess(hash: string, payload: GrantAccessPayload): Promise<AccessRight> {
        return http.post<AccessRight>(`${BASE}/studies/${hash}/access/`, payload).then(r => r.data)
    },

    revokeAccess(hash: string, rightId: number): Promise<void> {
        return http.delete(`${BASE}/studies/${hash}/access/${rightId}/`)
    },

    /**
     * Upload a batch of DICOM files. The report's `index` names each file's position in *files*.
     * @param files - The files to upload, in the order the report's `index` refers to.
     * @param confirmBurnedIn - The uploader has checked these images and they show no identifying text.
     */
    uploadFiles(files: File[], confirmBurnedIn = false): Promise<DicomUploadOut> {
        const form = new FormData()
        for (const file of files) {
            form.append('files', file)
        }
        return http.post<DicomUploadOut>(`${BASE}/upload/`, form, {
            headers: { 'Content-Type': 'multipart/form-data' },
            params: confirmBurnedIn ? { confirm_burned_in: true } : undefined,
        }).then(r => r.data)
    },

    /**
     * URL of the OHIF viewer pre-loaded with a specific study.
     * @remarks
     * `viewer` is OHIF's longitudinal mode route and `dicomjson` the data source configured in
     * plugins/dicom/ohif-app-config.js, which fetches the study from the `url` parameter. The bare
     * mount would open OHIF's worklist instead of the study.
     * @param studyHash - The study's `hash` from the study listing.
     */
    ohifViewerUrl(studyHash: string): string {
        const jsonUrl = encodeURIComponent(
            `${window.location.origin}${BASE}/studies/${studyHash}/ohif-json/`
        )
        return `/plugin/dicom/ohif/viewer/dicomjson?url=${jsonUrl}`
    },
}
