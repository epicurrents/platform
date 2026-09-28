/**
 * Templates for the viewer's export targets: the platform destinations a recording opened from a local file can be
 * sent to from the viewer's file menu.
 *
 * The viewer knows nothing of the platform. It keeps a registry of targets, each a label, the format it takes,
 * optional constraints and a function receiving the finished bytes, and the host fills it. This module builds the
 * two kinds the platform has: the plain upload to the person's own recordings, and a submission to a pool, checked
 * against the pool's ingest profile. Which targets a viewer offers is the host's configuration — the viewer page
 * registers the upload, and a project registers its pools, with the labels and adjustments its profiles need.
 *
 * @package    epicurrents-platform
 */

import type {
    SignalExportConstraints,
    SignalExportFile,
    SignalExportReceipt,
    SignalExportTarget,
    SignalExportTargetResult,
} from '@epicurrents/core/dist/types'
import {
    submitFile,
    uploadRecording,
    type SubmissionPool,
    type SubmissionProfile,
    type SubmissionViolation,
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

/**
 * A gate violation in the user's language. The gate's `message` is English and often names the offending value, so
 * a known `code` is shown as a translated sentence and the message only for a code this client does not know.
 * @param violation - One violation from the gate's refusal.
 */
export function violationText(violation: SubmissionViolation): string {
    switch (violation.code) {
        case 'annotations': {
            return t('The file carries an annotation channel; export it without annotations.', SCOPE)
        }
        case 'channels': {
            return t('The channels differ from those the pool asks for.', SCOPE)
        }
        case 'duration': {
            return t('The length of the excerpt is not one the pool accepts.', SCOPE)
        }
        case 'format': {
            return t('The file is not a readable EDF or BDF file.', SCOPE)
        }
        case 'identification': {
            return t('The file still carries identification in its header.', SCOPE)
        }
        case 'range': {
            return t('The signal range differs from the one the pool asks for.', SCOPE)
        }
        case 'sampling_rate': {
            return t('The sampling rate differs from the one the pool asks for.', SCOPE)
        }
        case 'sidecar_forbidden_key': {
            return t('The sidecar carries a key the pool forbids.', SCOPE)
        }
        case 'sidecar_hash': {
            return t('The declared hash does not match the file that arrived.', SCOPE)
        }
        case 'sidecar_missing_key': {
            return t('The sidecar lacks a key the pool requires.', SCOPE)
        }
        case 'sidecar_shape': {
            return t('The sidecar is not in the shape the viewer writes.', SCOPE)
        }
        case 'truncated': {
            return t('The file is shorter or longer than its header says.', SCOPE)
        }
        case 'unit': {
            return t('The physical unit differs from the one the pool asks for.', SCOPE)
        }
        default: {
            return violation.message
        }
    }
}

/** Hex SHA-256 of `data`. */
async function sha256Hex(data: ArrayBuffer): Promise<string> {
    const digest = new Uint8Array(await crypto.subtle.digest('SHA-256', data))
    return Array.from(digest, (byte) => byte.toString(16).padStart(2, '0')).join('')
}

/**
 * The receipt for a file a pool accepted. It names the file by the SHA-256 of the bytes sent, which is the one
 * reference withdrawal from a pool is keyed on: the platform keeps no record of who submitted a recording, and a
 * submission is dithered, so exporting the recording again does not reproduce the hash. The contributor is the only
 * holder of the reference, which is why it is handed to them rather than kept anywhere.
 * @param pool - The pool that accepted the file.
 * @param sha256 - Hex SHA-256 of the bytes sent.
 */
export function submissionReceipt(pool: SubmissionPool, sha256: string): SignalExportReceipt {
    const lines = [
        t('Submission receipt', SCOPE),
        '',
        t('Pool: {name}', SCOPE, { name: pool.name }),
        t('Pool reference: {hash}', SCOPE, { hash: pool.dataset_hash }),
        // The day only: the receipt may be forwarded whole, and an exact time could be matched to a ledger's.
        t('Submitted on: {date}', SCOPE, { date: new Date().toISOString().slice(0, 10) }),
        t('Recording reference (SHA-256 of the submitted file): {hash}', SCOPE, { hash: sha256 }),
        '',
        t(
            'Keep this receipt. To withdraw the recording from the pool, give the recording reference to the ' +
                'operator of this service. The reference is the only way to find the recording: the service does ' +
                'not record who submitted it, and exporting the recording again gives a different reference.',
            SCOPE,
        ),
        '',
    ]
    return {
        data: lines.join('\n'),
        fileName: `submission-receipt-${sha256.slice(0, 12)}.txt`,
        mimeType: 'text/plain',
    }
}

/**
 * Translate a profile's public shape into export constraints, or null when the viewer cannot produce a file the
 * profile accepts. The one such case today is a digital range other than {@link EXPORT_DIGITAL_RANGE}, which the
 * encoder does not let an export choose.
 * @param profile - The pool's profile as the platform serves it.
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
 * A submission to `pool`, checked against its profile: the profile's constraints restrict the export dialog, the
 * file is a plain EDF, and the de-identified sidecar travels beside it with the file's hash declared. Null when the
 * viewer cannot meet the profile (see {@link profileConstraints}).
 * @param pool - The pool the submissions go to, with its profile as the platform serves it.
 * @param options - The host's adjustments: a label, constraints and the sidecar keys the profile requires.
 */
export function createSubmissionTarget(
    pool: SubmissionPool,
    options: SubmissionTargetOptions = {},
): SignalExportTarget | null {
    const profile = pool.profile
    const constraints = profileConstraints(profile)
    if (!constraints) {
        return null
    }
    return {
        constraints: { ...constraints, ...(options.constraints ?? {}) },
        format: 'edf',
        label: options.label ?? t('Submission pool: {name}', SCOPE, { name: pool.name }),
        // Dithered, so the submitted bytes cannot be found by re-encoding a copy of the original; see the receipt.
        options: { deidentify: true, deidentifySidecar: true, dither: true, embedFooter: false },
        sidecar: true,
        async submit(file: SignalExportFile): Promise<SignalExportTargetResult> {
            let sidecar: Record<string, unknown> = {}
            let sha256 = ''
            try {
                sidecar = file.sidecar ? JSON.parse(file.sidecar) : {}
            } catch {
                return { message: t('The viewer produced a sidecar that could not be read.', SCOPE), success: false }
            }
            if (options.extendSidecar) {
                try {
                    sidecar = await options.extendSidecar(sidecar)
                } catch (error) {
                    // The host's own reason, which it writes for the user.
                    return {
                        message: t('The sidecar could not be prepared: {reason}', SCOPE, {
                            reason: error instanceof Error ? error.message : String(error),
                        }),
                        success: false,
                    }
                }
            }
            try {
                // `crypto.subtle` exists only in a secure context, so on a deployment served over plain HTTP the
                // hash cannot be declared.
                sha256 = await sha256Hex(file.data)
            } catch {
                return { message: t('Submitting needs a secure (https) connection.', SCOPE), success: false }
            }
            sidecar[DECLARED_HASH_KEY] = sha256
            const missing = profile.required_sidecar_keys.filter((key) => !(key in sidecar))
            if (missing.length) {
                return {
                    message: t('The sidecar lacks the required keys {keys}.', SCOPE, { keys: missing.join(', ') }),
                    success: false,
                }
            }
            try {
                const result = await submitFile(
                    pool.dataset_hash,
                    new File([file.data], 'recording.edf', { type: 'application/octet-stream' }),
                    new Blob([JSON.stringify(sidecar)], { type: 'application/json' }),
                )
                if (result.accepted) {
                    return {
                        message: t('Accepted. The recording joins the pool at the next pooled ingest.', SCOPE),
                        receipt: submissionReceipt(pool, sha256),
                        success: true,
                    }
                }
                return {
                    message: t('Refused: {reasons}', SCOPE, {
                        reasons: [...new Set(result.violations.map(violationText))].join(' '),
                    }),
                    success: false,
                }
            } catch (error) {
                return { message: errorDetail(error, t('The submission failed.', SCOPE)), success: false }
            }
        },
    }
}
