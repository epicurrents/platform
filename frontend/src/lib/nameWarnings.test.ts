/**
 * Tests for the name-warning toasts: the field a warning names reaches the user in their language, and a field this
 * client does not know still reads as words rather than an API token.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('#i18n', () => ({
    t: (key: string, _scope: string, params: Record<string, unknown> = {}) =>
        `[${key.replace(/\{(\w+)\}/g, (_match, name: string) => String(params[name]))}]`,
}))
vi.mock('#lib/toast', () => ({ showToast: vi.fn() }))

import { showToast } from '#lib/toast'
import { fieldLabel, toastNameWarnings } from '#lib/nameWarnings'

beforeEach(() => {
    vi.mocked(showToast).mockReset()
})

describe('fieldLabel', () => {
    it('translates the known field tokens', () => {
        expect(fieldLabel('display_name')).toBe('[display name]')
        expect(fieldLabel('public_source')).toBe('[published source]')
    })

    it('spaces an unknown token without translating it', () => {
        expect(fieldLabel('some_new_field')).toBe('some new field')
    })
})

describe('toastNameWarnings', () => {
    it('shows one warning toast per flagged field, with the translated field inside the sentence', () => {
        toastNameWarnings([{ field: 'name', kind: 'date', message: 'English.' }])
        expect(showToast).toHaveBeenCalledTimes(1)
        expect(vi.mocked(showToast).mock.calls[0]).toEqual([
            '[The [name] [contains something that reads as a date]. Everyone you share with sees it as typed.]',
            'warning',
        ])
    })

    it('is a no-op without warnings', () => {
        toastNameWarnings(undefined)
        expect(showToast).not.toHaveBeenCalled()
    })
})
