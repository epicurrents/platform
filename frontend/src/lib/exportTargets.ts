/**
 * Templates for the viewer's export targets: the platform destinations a recording opened from a local file can be
 * sent to from the viewer's file menu.
 *
 * The viewer knows nothing of the platform. It keeps a registry of targets, each a label, the format it takes,
 * optional constraints and a function receiving the finished bytes, and the host fills it. This module builds the
 * two kinds the platform has: the plain upload to the person's own recordings, and a submission to a batch, checked
 * against the batch's ingest profile. Which targets a viewer offers is the host's configuration — the viewer page
 * registers the upload, and a project registers its batches, with the labels and adjustments its profiles need.
 *
 * @package    epicurrents-platform
 */

import type {
    SignalExportConstraints,
    SignalExportFile,
    SignalExportTarget,
    SignalExportTargetResult,
} from '@epicurrents/core/dist/types'
import {
    submitFile,
    uploadRecording,
    type SubmissionBatch,
    type SubmissionProfile,
} from '#api/recordings'
import { t } from '#i18n'
import { errorDetail } from '#lib/http'

const SCOPE = 'exportTargets'

/** The digital range the viewer's EDF encoder writes every channel in. A profile asking for another cannot be met. */
export const EXPORT_DIGITAL_RANGE: readonly [number, number] = [-32768, 32767]

/** The sidecar key declaring the SHA-256 of the file it accompanies, which the submission gate checks. */
const DECLARED_HASH_KEY = 'recording_sha256'

/** Adjustments a host makes to a submission target built from a profile. */
export interface SubmissionTargetOptions {
    /** Constraints replacing the profile's own, key by key. */
    constraints?: Partial<SignalExportConstraints>
    /**
     * Add the keys a profile requires beyond the declared hash, such as coded subject facts. Receives the parsed
     * sidecar and returns the one to send; the hash is declared after it runs.
     */
    extendSidecar?: (sidecar: Record<string, unknown>) => Record<string, unknown> | Promise<Record<string, unknown>>
    /** User-facing label of the target. */
    label?: string
}

/** Hex SHA-256 of `data`. */
async function sha256Hex(data: ArrayBuffer): Promise<string> {
    const digest = new Uint8Array(await crypto.subtle.digest('SHA-256', data))
    return Array.from(digest, (byte) => byte.toString(16).padStart(2, '0')).join('')
}

/**
 * Translate a profile's public shape into export constraints, or null when the viewer cannot produce a file the
 * profile accepts. The one such case today is a digital range other than {@link EXPORT_DIGITAL_RANGE}, which the
 * encoder does not let an export choose.
 * @param profile - The batch's profile as the platform serves it.
 */
export function profileConstraints(profile: SubmissionProfile): SignalExportConstraints | null {
    const digital = [profile.digital_min, profile.digital_max]
    if (digital.some((value, i) => value !== null && value !== EXPORT_DIGITAL_RANGE[i])) {
        return null
    }
    const constraints: SignalExportConstraints = {}
    if (profile.channels.length) {
        constraints.channels = [...profile.channels]
    }
    if (profile.sampling_rate !== null) {
        constraints.samplingRate = profile.sampling_rate
    }
    if (profile.durations_seconds.length) {
        constraints.durations = [...profile.durations_seconds]
    }
    if (profile.physical_min !== null && profile.physical_max !== null) {
        constraints.amplitudeRange = [profile.physical_min, profile.physical_max]
    }
    if (profile.physical_unit) {
        constraints.unit = profile.physical_unit
    }
    if (profile.forbidden_sidecar_keys.length) {
        constraints.forbiddenMetadataKeys = [...profile.forbidden_sidecar_keys]
    }
    return constraints
}

/**
 * The upload to the person's own recordings. The file travels as the container the platform ingests, the
 * de-identified EDF with its sidecar as a footer, so the codes of its events arrive with it.
 * @param label - User-facing label; "My recordings" when omitted.
 */
export function createUploadTarget(label?: string): SignalExportTarget {
    return {
        format: 'edf',
        label: label ?? t('My recordings', SCOPE),
        options: { deidentify: true, embedFooter: true },
        async submit(file: SignalExportFile): Promise<SignalExportTargetResult> {
            try {
                await uploadRecording(new File([file.data], 'recording.edf', { type: 'application/octet-stream' }))
                return {
                    message: t('The recording was added to your recordings and is being processed.', SCOPE),
                    success: true,
                }
            } catch (error) {
                return { message: errorDetail(error, t('The upload failed.', SCOPE)), success: false }
            }
        },
    }
}

/**
 * A submission to `batch`, checked against its profile: the profile's constraints restrict the export dialog, the
 * file is a plain EDF, and the de-identified sidecar travels beside it with the file's hash declared. Null when the
 * viewer cannot meet the profile (see {@link profileConstraints}).
 * @param batch - The batch the submissions go to.
 * @param profile - The batch's profile as the platform serves it.
 * @param options - The host's adjustments: a label, constraints and the sidecar keys the profile requires.
 */
export function createSubmissionTarget(
    batch: SubmissionBatch,
    profile: SubmissionProfile,
    options: SubmissionTargetOptions = {},
): SignalExportTarget | null {
    const constraints = profileConstraints(profile)
    if (!constraints) {
        return null
    }
    return {
        constraints: { ...constraints, ...(options.constraints ?? {}) },
        format: 'edf',
        label: options.label ?? t('Submission batch {batch}', SCOPE, { batch: batch.hash.slice(0, 8) }),
        options: { deidentify: true, deidentifySidecar: true, embedFooter: false },
        sidecar: true,
        async submit(file: SignalExportFile): Promise<SignalExportTargetResult> {
            let sidecar: Record<string, unknown> = {}
            try {
                sidecar = file.sidecar ? JSON.parse(file.sidecar) : {}
                if (options.extendSidecar) {
                    sidecar = await options.extendSidecar(sidecar)
                }
                // Inside the guard: `crypto.subtle` exists only in a secure context, so on a
                // deployment served over plain HTTP the hash cannot be declared.
                sidecar[DECLARED_HASH_KEY] = await sha256Hex(file.data)
            } catch (error) {
                return {
                    message: t('The sidecar could not be prepared: {reason}', SCOPE, { reason: String(error) }),
                    success: false,
                }
            }
            const missing = profile.required_sidecar_keys.filter((key) => !(key in sidecar))
            if (missing.length) {
                return {
                    message: t('The sidecar lacks the required keys {keys}.', SCOPE, { keys: missing.join(', ') }),
                    success: false,
                }
            }
            try {
                const result = await submitFile(
                    batch.hash,
                    new File([file.data], 'recording.edf', { type: 'application/octet-stream' }),
                    new Blob([JSON.stringify(sidecar)], { type: 'application/json' }),
                )
                if (result.accepted) {
                    return {
                        message: t('Accepted. Recordings waiting in the batch: {count}.', SCOPE, {
                            count: result.pending_count,
                        }),
                        success: true,
                    }
                }
                return {
                    message: t('Refused: {reasons}', SCOPE, {
                        reasons: result.violations.map((violation) => violation.message).join(' '),
                    }),
                    success: false,
                }
            } catch (error) {
                return { message: errorDetail(error, t('The submission failed.', SCOPE)), success: false }
            }
        },
    }
}
