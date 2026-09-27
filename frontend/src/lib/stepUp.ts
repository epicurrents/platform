/**
 * Step-up confirmation on the client: which credentials the caller has, and which requests need them.
 *
 * The server decides, and answers 400 "Confirmation failed." to a request that
 * needed confirming and was not. The rules here mirror its triggers so that
 * the prompt appears only when a request will be asked for it — a rename stays
 * one click — and they compare against the values the page loaded, as the
 * server compares against the stored ones: setting a field to the value it
 * already has is not a change.
 *
 * @package    epicurrents-platform
 */

import type { StepUpCredentials, StepUpInfo } from '#api/maintenance'
import type { AuthUser } from '#api/user'
import { t } from '#i18n'

const SCOPE = 'StepUp'

/** The answer the server gives a request whose confirmation was missing or wrong. */
export const CONFIRMATION_FAILED = 'Confirmation failed.'

/**
 * How `user` confirms: the password when the account has one of its own, the second factor when one is confirmed,
 * and for an account signing in through a provider without a factor, nothing — which the prompt explains.
 */
export function stepUpInfoFor(user: AuthUser | null): StepUpInfo {
    if (user === null) {
        return { method: null, available: false, reason: null }
    }
    const hasPassword = !user.external_provider
    const hasFactor = user.is_2fa_enabled
    if (hasPassword && hasFactor) {
        return { method: 'password+totp', available: true, reason: null }
    }
    if (hasPassword) {
        return { method: 'password', available: true, reason: null }
    }
    if (hasFactor) {
        return { method: 'totp', available: true, reason: null }
    }
    return {
        method: null,
        available: false,
        reason: t('This account signs in through an external provider and has no second factor, so it cannot confirm sensitive changes. Set up two-step verification on your profile first.', SCOPE),
    }
}

/** The credentials a form collected, with blanks left out so the server sees them as absent. */
export function stepUpBody(credentials: { password?: string, totp_code?: string }): StepUpCredentials {
    const body: StepUpCredentials = {}
    if (credentials.password) {
        body.password = credentials.password
    }
    const code = credentials.totp_code?.trim()
    if (code) {
        body.totp_code = code
    }
    return body
}

/** Whether `error` is the server refusing a request for want of (the right) confirmation. */
export function isStepUpRefusal(error: unknown): boolean {
    const response = (error as { response?: { status?: number, data?: { detail?: unknown } } })?.response
    return response?.status === 400 && response.data?.detail === CONFIRMATION_FAILED
}

/** The fields of an account edit the step-up rule reads. */
interface AccountFlags {
    email: string
    is_active: boolean
    is_staff: boolean
    is_superuser: boolean
}

/**
 * `PATCH /admin/accounts/{id}` asks for confirmation when the edit changes `is_staff`, `is_superuser` or the
 * email address, or activates the account.
 */
export function accountUpdateNeedsStepUp(current: AccountFlags, next: Partial<AccountFlags>): boolean {
    if (next.email !== undefined && next.email.trim() !== current.email) {
        return true
    }
    if (next.is_staff !== undefined && next.is_staff !== current.is_staff) {
        return true
    }
    if (next.is_superuser !== undefined && next.is_superuser !== current.is_superuser) {
        return true
    }
    return next.is_active === true && !current.is_active
}

/** `POST /admin/accounts` asks for confirmation when a password is supplied or either staff tier is set. */
export function accountCreateNeedsStepUp(next: { password?: string, is_staff?: boolean, is_superuser?: boolean }) {
    return Boolean(next.password) || next.is_staff === true || next.is_superuser === true
}

/** A membership replacement asks for confirmation when it adds anyone; removals need nothing. */
export function membershipAddsAny(before: Iterable<number>, after: Iterable<number>): boolean {
    const held = new Set(before)
    for (const id of after) {
        if (!held.has(id)) {
            return true
        }
    }
    return false
}

/** `PATCH /admin/groups/{id}` asks for confirmation when any role in the payload is set to a value. */
export function rolesNeedStepUp(roles: Record<string, string | null> | undefined): boolean {
    return Object.values(roles ?? {}).some(value => value !== null)
}

/**
 * Keep only the roles whose value differs from the group's current one.
 *
 * The server asks for confirmation whenever a role in the payload carries a
 * value, unchanged or not, so sending the whole rendered set would make every
 * rename of a group holding a role a confirmed action. An absent key is left
 * untouched, which is exactly what an unchanged role should be; the result is
 * a subset of `payload`, so it can never pad the map.
 *
 * @param payload - the rendered roles, from `rolesPayload`.
 * @param current - the group's roles as last loaded.
 */
export function changedRoles(
    payload: Record<string, string | null>,
    current: Record<string, string | null>,
): Record<string, string | null> {
    const changed: Record<string, string | null> = {}
    for (const [key, value] of Object.entries(payload)) {
        if ((current[key] ?? null) !== value) {
            changed[key] = value
        }
    }
    return changed
}

/** `PATCH /me` asks for confirmation when the address changes. */
export function profileNeedsStepUp(currentEmail: string, nextEmail: string): boolean {
    return nextEmail.trim() !== currentEmail
}
