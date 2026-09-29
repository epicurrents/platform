/**
 * Tests for the error-reading helpers: a refusal the server explains must reach the user in the server's words, and
 * a body that is not a sentence (a validation array, a network error) must fall back rather than print as an object.
 */

import { describe, expect, it } from 'vitest'

import { errorDetail, settledFailure } from '#lib/http'

function refusal (data: unknown) {
    return { response: { status: 409, data } }
}

describe('errorDetail', () => {
    it('returns the server detail string', () => {
        expect(errorDetail(refusal({ detail: 'Dissolve the submission pool first.' }), 'fallback'))
            .toBe('Dissolve the submission pool first.')
    })

    it('falls back for a validation body whose detail is an array', () => {
        expect(errorDetail(refusal({ detail: [{ loc: ['body'], msg: 'bad' }] }), 'fallback')).toBe('fallback')
    })

    it('falls back for a network error with no response', () => {
        expect(errorDetail(new Error('Network Error'), 'fallback')).toBe('fallback')
    })

    it('falls back for an empty detail', () => {
        expect(errorDetail(refusal({ detail: '' }), 'fallback')).toBe('fallback')
    })
})

describe('settledFailure', () => {
    it('is null when every request succeeded', () => {
        expect(settledFailure([{ status: 'fulfilled', value: 1 }])).toBeNull()
    })

    it('counts the failures and carries the first explained reason', () => {
        const result = settledFailure([
            { status: 'fulfilled', value: 1 },
            { status: 'rejected', reason: new Error('Network Error') },
            { status: 'rejected', reason: refusal({ detail: 'Not authored by the dataset author.' }) },
            { status: 'rejected', reason: refusal({ detail: 'Another reason.' }) },
        ])
        expect(result).toEqual({ count: 3, reason: 'Not authored by the dataset author.' })
    })

    it('leaves the reason empty when no failure explains itself', () => {
        expect(settledFailure([{ status: 'rejected', reason: new Error('boom') }])).toEqual({ count: 1, reason: '' })
    })
})
