/** Tests for the assessment reference prefix helpers. */

import { describe, expect, it } from 'vitest'
import { joinAssessmentReference, splitAssessmentReference } from './assessment'

describe('assessment reference prefixes', () => {
    it('round-trips a kind and an identifier', () => {
        const stored = joinAssessmentReference('dpia', ' DPIA-2026-04 ')
        expect(stored).toBe('DPIA: DPIA-2026-04')
        expect(splitAssessmentReference(stored)).toEqual({ kind: 'dpia', identifier: 'DPIA-2026-04' })
    })

    it('stores a bare identifier when no kind is chosen', () => {
        expect(joinAssessmentReference('', 'https://dms.example/7')).toBe('https://dms.example/7')
        expect(splitAssessmentReference('https://dms.example/7')).toEqual({ kind: '', identifier: 'https://dms.example/7' })
    })

    it('reads a reference typed elsewhere as having no kind', () => {
        expect(splitAssessmentReference('Contextual assessment 2026')).toEqual({
            kind: '',
            identifier: 'Contextual assessment 2026',
        })
    })

    it('a published dataset is a kind', () => {
        const stored = joinAssessmentReference('published', 'doi:10.1000/example')
        expect(stored).toBe('Published dataset: doi:10.1000/example')
        expect(splitAssessmentReference(stored).kind).toBe('published')
    })

    it('an empty identifier clears the reference whatever the kind', () => {
        expect(joinAssessmentReference('agreement', '   ')).toBe('')
    })
})
