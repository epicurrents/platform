/**
 * Tests for the client-side step-up rules: the prompt must appear exactly when the server will ask for a
 * confirmation, so a plain rename stays one click and a privilege change never fails for want of one.
 */

import { describe, expect, it, vi } from 'vitest'

vi.mock('#i18n', () => ({ t: (key: string) => key }))

import type { AuthUser } from '#api/user'
import {
    accountCreateNeedsStepUp,
    accountUpdateNeedsStepUp,
    changedRoles,
    isStepUpRefusal,
    membershipAddsAny,
    profileNeedsStepUp,
    rolesNeedStepUp,
    stepUpBody,
    stepUpInfoFor,
} from '#lib/stepUp'

const account = { email: 'a@example.org', is_active: true, is_staff: false, is_superuser: false }

function user (overrides: Partial<AuthUser>): AuthUser {
    return {
        id: 1,
        username: 'u',
        email: '',
        first_name: '',
        last_name: '',
        is_staff: true,
        is_superuser: true,
        is_2fa_enabled: false,
        external_provider: null,
        ...overrides,
    }
}

describe('stepUpInfoFor', () => {
    it('asks for what the account has', () => {
        expect(stepUpInfoFor(user({})).method).toBe('password')
        expect(stepUpInfoFor(user({ is_2fa_enabled: true })).method).toBe('password+totp')
        expect(stepUpInfoFor(user({ external_provider: 'Entra', is_2fa_enabled: true })).method).toBe('totp')
        const none = stepUpInfoFor(user({ external_provider: 'Entra' }))
        expect(none.available).toBe(false)
        expect(none.reason).toBeTruthy()
    })
})

describe('stepUpBody', () => {
    it('leaves blanks out and trims the code', () => {
        expect(stepUpBody({ password: '', totp_code: '' })).toEqual({})
        expect(stepUpBody({ password: 'pw', totp_code: ' 123456 ' })).toEqual({ password: 'pw', totp_code: '123456' })
        expect(stepUpBody({ password: '', totp_code: '654321' })).toEqual({ totp_code: '654321' })
    })
})

describe('accountUpdateNeedsStepUp', () => {
    it('a rename needs nothing', () => {
        expect(accountUpdateNeedsStepUp(account, { ...account })).toBe(false)
        expect(accountUpdateNeedsStepUp(account, { email: ' a@example.org ' })).toBe(false)
    })

    it('a tier change, an address change or an activation needs a confirmation', () => {
        expect(accountUpdateNeedsStepUp(account, { ...account, is_staff: true })).toBe(true)
        expect(accountUpdateNeedsStepUp(account, { ...account, is_superuser: true })).toBe(true)
        expect(accountUpdateNeedsStepUp(account, { ...account, email: 'b@example.org' })).toBe(true)
        expect(accountUpdateNeedsStepUp({ ...account, is_active: false }, { ...account, is_active: true })).toBe(true)
    })

    it('a deactivation or a demotion back to what it was needs nothing beyond the tier rule', () => {
        expect(accountUpdateNeedsStepUp(account, { ...account, is_active: false })).toBe(false)
    })
})

describe('accountCreateNeedsStepUp', () => {
    it('an invited ordinary account needs nothing; a password or a tier does', () => {
        expect(accountCreateNeedsStepUp({})).toBe(false)
        expect(accountCreateNeedsStepUp({ password: 'long enough' })).toBe(true)
        expect(accountCreateNeedsStepUp({ is_staff: true })).toBe(true)
        expect(accountCreateNeedsStepUp({ is_superuser: true })).toBe(true)
    })
})

describe('membershipAddsAny', () => {
    it('only an addition needs a confirmation', () => {
        expect(membershipAddsAny([1, 2], [1])).toBe(false)
        expect(membershipAddsAny([1, 2], [2, 1])).toBe(false)
        expect(membershipAddsAny([1], [1, 3])).toBe(true)
    })
})

describe('group roles', () => {
    it('only a role set to a value needs a confirmation', () => {
        expect(rolesNeedStepUp(undefined)).toBe(false)
        expect(rolesNeedStepUp({ a: null })).toBe(false)
        expect(rolesNeedStepUp({ a: null, b: 'x' })).toBe(true)
    })

    it('changedRoles drops unchanged roles, so a rename of a group holding one stays unconfirmed', () => {
        const current = { a: 'x', b: null, c: null }
        expect(changedRoles({ a: 'x', b: null }, current)).toEqual({})
        expect(changedRoles({ a: null, b: null }, current)).toEqual({ a: null })
        expect(changedRoles({ a: 'x', b: 'y' }, current)).toEqual({ b: 'y' })
        // Never a key the payload did not carry: the padded-map clear stays impossible.
        expect(Object.keys(changedRoles({ a: null }, current))).toEqual(['a'])
    })
})

describe('profileNeedsStepUp', () => {
    it('only an address change needs a confirmation', () => {
        expect(profileNeedsStepUp('a@example.org', 'a@example.org ')).toBe(false)
        expect(profileNeedsStepUp('a@example.org', 'b@example.org')).toBe(true)
    })
})

describe('isStepUpRefusal', () => {
    it('recognises only the confirmation refusal', () => {
        const refusal = { response: { status: 400, data: { detail: 'Confirmation failed.' } } }
        expect(isStepUpRefusal(refusal)).toBe(true)
        const other = { response: { status: 400, data: { detail: 'Enter a valid email address.' } } }
        expect(isStepUpRefusal(other)).toBe(false)
        expect(isStepUpRefusal(new Error('offline'))).toBe(false)
    })
})
