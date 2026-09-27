/**
 * Tests for reading the reset link: the credential comes from the fragment, the fragment leaves the address bar at
 * once, and the query string is not read — the old query-string links are not supported.
 */

import { afterEach, describe, expect, it } from 'vitest'
import { consumeResetFragment } from '#lib/resetLink'

afterEach(() => {
    window.history.replaceState(null, '', '/')
})

describe('consumeResetFragment', () => {
    it('reads uid, token and welcome from the fragment and strips it', () => {
        window.history.replaceState({ router: 'state' }, '', '/reset-password?x=1#uid=MTI&token=abc-123&welcome=1')
        const link = consumeResetFragment()
        expect(link).toEqual({ uid: 'MTI', token: 'abc-123', welcome: true })
        expect(window.location.hash).toBe('')
        expect(window.location.pathname).toBe('/reset-password')
        expect(window.location.search).toBe('?x=1')
        expect(window.history.state).toEqual({ router: 'state' })
    })

    it('a reset link is not a welcome', () => {
        window.history.replaceState(null, '', '/reset-password#uid=MTI&token=abc')
        expect(consumeResetFragment()?.welcome).toBe(false)
    })

    it('an incomplete fragment is no link, and is stripped all the same', () => {
        window.history.replaceState(null, '', '/reset-password#uid=MTI')
        expect(consumeResetFragment()).toBeNull()
        expect(window.location.hash).toBe('')
    })

    it('ignores the query string', () => {
        window.history.replaceState(null, '', '/reset-password?uid=MTI&token=abc')
        expect(consumeResetFragment()).toBeNull()
        expect(window.location.search).toBe('?uid=MTI&token=abc')
    })
})
