/**
 * The cue for content stored as uploaded is decided from the listed row alone.
 */

import { describe, expect, it } from 'vitest'
import { PASS_NONE, storedAsUploaded } from './platformPass'

describe('storedAsUploaded', () => {
    it('marks a row whose content no pass was applied to', () => {
        expect(storedAsUploaded({ platform_pass: PASS_NONE })).toBe(true)
        expect(storedAsUploaded({ platform_pass: 'none' })).toBe(true)
    })

    it('leaves a row a pass was applied to unmarked', () => {
        expect(storedAsUploaded({ platform_pass: 'dicom-header' })).toBe(false)
    })

    it('leaves a row that names no pass unmarked', () => {
        expect(storedAsUploaded({ platform_pass: null })).toBe(false)
        expect(storedAsUploaded({})).toBe(false)
        expect(storedAsUploaded({ platform_pass: '' })).toBe(false)
    })
})
