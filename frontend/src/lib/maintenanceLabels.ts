/**
 * Human labels for the tokens the maintenance API reports: job states, job reasons and package states.
 *
 * Every label is a literal `t()` call so the key is a static string a translator can find; a token the map does
 * not know is shown as it came, since a new reason from a newer agent is better read raw than hidden.
 *
 * @package    epicurrents-platform
 */

import type { JobState, PackageState } from '#api/maintenance'
import { t } from '#i18n'

const SCOPE = 'MaintenanceLabels'

/** The label of a job state. */
export function jobStateLabel (state: JobState | string): string {
    switch (state) {
        case 'requested':
            return t('Requested', SCOPE)
        case 'accepted':
            return t('Accepted', SCOPE)
        case 'running':
            return t('Running', SCOPE)
        case 'awaiting_verification':
            return t('Awaiting confirmation', SCOPE)
        case 'succeeded':
            return t('Succeeded', SCOPE)
        case 'failed':
            return t('Failed', SCOPE)
        case 'cancelled':
            return t('Cancelled', SCOPE)
        case 'rolling_back':
            return t('Rolling back', SCOPE)
        case 'rolled_back':
            return t('Rolled back', SCOPE)
        case 'rollback_failed':
            return t('Rollback failed', SCOPE)
        default:
            return state
    }
}

/** Why a job ended the way it did, from the `reason` token the platform or the host agent recorded. */
export function jobReasonLabel (reason: string): string {
    switch (reason) {
        case 'orphaned':
            return t('The agent left no record of the job', SCOPE)
        case 'stale':
            return t('The executor stopped reporting', SCOPE)
        case 'abandoned':
            return t('Marked as failed by a superuser', SCOPE)
        case 'interrupted':
            return t('Interrupted before it finished', SCOPE)
        case 'deadline':
            return t('Not confirmed before the window closed', SCOPE)
        case 'requested':
            return t('A rollback was requested', SCOPE)
        case 'maintenance_lock':
            return t('The platform was locked for maintenance', SCOPE)
        case 'spool_write_failed':
            return t('The request could not be written into the maintenance spool', SCOPE)
        case 'spool_unwritable':
            return t('The maintenance spool is not writable', SCOPE)
        case 'dispatch_failed':
            return t('The request could not be handed to the worker', SCOPE)
        case 'refused_request':
            return t('The host agent refused the request', SCOPE)
        case 'refused_contents':
            return t('The host agent refused the package contents', SCOPE)
        case 'refused_locked':
            return t('The host agent refused: the platform was already locked', SCOPE)
        case 'refused_unfinished':
            return t('The host agent refused: an earlier operation is unfinished', SCOPE)
        case 'refused_disk':
            return t('The host agent refused: not enough disk space', SCOPE)
        case 'refused_operation':
            return t('The host agent does not carry out this operation', SCOPE)
        case 'refused_signature':
            return t('The host agent could not verify the package signature', SCOPE)
        case 'refused_hash':
            return t('The package does not match its manifest', SCOPE)
        case 'refused_code_only':
            return t('The database cannot be kept: a migration was applied since the snapshot', SCOPE)
        default:
            return reason
    }
}

/** The label of an uploaded package's state; an available package that is not newer says so. */
export function packageStateLabel (state: PackageState | string, applicable: boolean): string {
    switch (state) {
        case 'applied':
            return t('Applied', SCOPE)
        case 'pruned':
            return t('Removed', SCOPE)
        case 'unverified':
            return t('Being verified', SCOPE)
        case 'invalid':
            return t('Invalid', SCOPE)
        case 'available':
            return applicable ? t('Available', SCOPE) : t('Not newer than the installed version', SCOPE)
        default:
            return state
    }
}

/** What a package state that cannot be applied means for the reader, or an empty string for the others. */
export function packageStateHint (state: PackageState | string): string {
    switch (state) {
        case 'unverified':
            return t('The archive is being hashed against its manifest; it can be applied once that holds.', SCOPE)
        case 'invalid':
            return t('The signature or the archive hash did not hold. The package cannot be applied; remove it.', SCOPE)
        default:
            return ''
    }
}
