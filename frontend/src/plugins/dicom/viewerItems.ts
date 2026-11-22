/**
 * DICOM studies as viewer items: what the viewer lists a study by, opened in the viewer edition's radiology view.
 *
 * A study is listed through the `rad/viewer-url` importer with the address of its page on the plugin's OHIF mount,
 * which the radiology view frames; the viewer reads nothing of the study itself. A viewer URL names studies with
 * `?dicom=<hash>[,<hash>…]`, as `?files=` names recordings, and a study attached to a listed recording comes through
 * the recording attachments listing and a study in a dataset through the dataset item listing.
 */

import type { LocationQuery } from 'vue-router'
import type { DatasetItem } from '#api/library'
import type { RecordingAttachment } from '#api/recordings'
import { t } from '#i18n'
import { storedAsUploaded } from '#lib/platformPass'
import type { ViewerItemSource } from '#projects/types'
import type { StudyDescription } from '#viewer/datasetStudies'
import { dicomApi, studyStoredAsUploaded, type DicomStudyOut } from './api'

const SCOPE = 'DicomViewerItems'

/** The viewer importer that opens a study: the radiology module's, which frames the study's viewer page. */
export const STUDY_IMPORTER = 'rad/viewer-url'

/**
 * Describe a study for the viewer. It is listed by its description, which is the label every reader of the study is
 * shown; the patient fields are never used, since a study stored as uploaded still carries them. The study is listed
 * with no properties, so the viewer loads it up front, which costs nothing: loading keeps the address and fetches
 * nothing.
 * @param study - The study as the study endpoints serve it.
 */
export function describeStudy(study: DicomStudyOut): StudyDescription {
    return studyDescription(study.hash, study.study_description, study.modalities, studyStoredAsUploaded(study))
}

/**
 * Describe a study attached to a recording, or answer null for an attachment of another type. The study endpoints
 * authenticate by session only, so the listing never gives a share-token reader a study, and none is needed here.
 * @param attachment - The attachment as the recording attachments listing gives it.
 */
export function describeAttachedStudy(attachment: RecordingAttachment): StudyDescription | null {
    if (attachment.type !== 'dicomstudy') {
        return null
    }
    return studyDescription(
        attachment.hash, attachment.name, String(attachment.details.modalities ?? ''), storedAsUploaded(attachment),
    )
}

/**
 * Describe a study that is a dataset member, or answer null for a member of another type. The listing leaves studies
 * out for a share-token reader, whom the study endpoints would not serve.
 * @param item - The member as the dataset item listing gives it.
 */
export function describeStudyMember(item: DatasetItem): StudyDescription | null {
    if (item.object_type !== 'dicomstudy' || !item.object_hash) {
        return null
    }
    return studyDescription(item.object_hash, item.object_name ?? '', '', storedAsUploaded(item))
}

/**
 * The description of a study by its hash, its description as typed and its modalities. A study stored as uploaded
 * says so in its listed name, which is the one thing the viewer shows of an item listed without properties; a
 * property would show too, but an item with properties is no longer loaded up front.
 */
function studyDescription(
    hash: string,
    description: string,
    modalities: string,
    asUploaded: boolean,
): StudyDescription {
    const name = description?.trim() || t('{modality} study', SCOPE, { modality: modalities || 'DICOM' })
    return {
        loader: STUDY_IMPORTER,
        name: asUploaded ? t('{name} (stored as uploaded)', SCOPE, { name }) : name,
        properties: null,
        source: `${window.location.origin}${dicomApi.ohifViewerUrl(hash)}`,
    }
}

/** The shape of a study's public hash. */
const STUDY_HASH = /^[0-9a-f]{32}$/i

/**
 * The study hashes a viewer URL names: every `dicom` parameter, each a comma-separated list, in order. A value that
 * is not a study hash is left out with a warning and never reaches a request: the hash becomes a segment of the
 * study's request path, and a link is anyone's to write.
 * @param query - The viewer route's query.
 */
export function studyHashesFromQuery(query: LocationQuery): string[] {
    const raw = query.dicom
    const values = Array.isArray(raw) ? raw : [raw]
    return values
        .filter((value): value is string => typeof value === 'string')
        .flatMap(value => value.split(','))
        .map(hash => hash.trim())
        .filter(Boolean)
        .filter(hash => {
            const valid = STUDY_HASH.test(hash)
            if (!valid) {
                console.warn('[Epicurrents] Ignoring a ?dicom= value that is not a study hash.')
            }
            return valid
        })
}

/** The plugin's viewer item source: the studies `?dicom=` names, attached to a listed recording, or in a dataset. */
export const dicomViewerItems: ViewerItemSource = {
    describeAttachment: describeAttachedStudy,
    describeDatasetItem: describeStudyMember,
    params: ['dicom'],
    async fromQuery(query) {
        const studies = await Promise.all(studyHashesFromQuery(query).map(hash => dicomApi.getStudy(hash)))
        return studies.map(describeStudy)
    },
}
