/**
 * Tests for the shared step-up prompt: it opens only when the request needs a confirmation (or the server turns
 * out to ask for one), sends what was typed, stays open on a refusal, and settles on success or cancel.
 */

import { describe, expect, it, vi } from 'vitest'

vi.mock('#i18n', () => ({ t: (key: string) => key }))

import { requestStepUp, useStepUpPrompt, withStepUp } from '#composables/useStepUpPrompt'

const refusal = Object.assign(new Error('400'), { response: { status: 400, data: { detail: 'Confirmation failed.' } } })

describe('withStepUp', () => {
    it('sends at once, without a prompt, when no confirmation is needed', async () => {
        const { state } = useStepUpPrompt()
        const submit = vi.fn().mockResolvedValue(undefined)
        expect(await withStepUp(false, { title: 'Save' }, submit)).toBe(true)
        expect(submit).toHaveBeenCalledWith({})
        expect(state.open).toBe(false)
    })

    it('passes any other failure of an unconfirmed request to the caller', async () => {
        const { state } = useStepUpPrompt()
        const failure = Object.assign(new Error('409'), {
            response: { status: 409, data: { detail: 'Last superuser.' } },
        })
        await expect(withStepUp(false, { title: 'Save' }, vi.fn().mockRejectedValue(failure))).rejects.toBe(failure)
        expect(state.open).toBe(false)
    })

    it('prompts when needed and sends the typed credentials', async () => {
        const { state, confirm } = useStepUpPrompt()
        const submit = vi.fn().mockResolvedValue(undefined)
        const outcome = withStepUp(true, { title: 'Make superuser' }, submit)
        await Promise.resolve()
        expect(state.open).toBe(true)
        expect(submit).not.toHaveBeenCalled()
        state.credentials.password = 'pw'
        state.credentials.totp_code = '123456'
        await confirm()
        expect(submit).toHaveBeenCalledWith({ password: 'pw', totp_code: '123456' })
        expect(await outcome).toBe(true)
        expect(state.open).toBe(false)
        expect(state.credentials.password).toBe('')
    })

    it('prompts when the server asks for a confirmation the rule did not expect', async () => {
        const { state, confirm } = useStepUpPrompt()
        const submit = vi.fn().mockRejectedValueOnce(refusal).mockResolvedValueOnce(undefined)
        const outcome = withStepUp(false, { title: 'Save' }, submit)
        await vi.waitFor(() => expect(state.open).toBe(true))
        state.credentials.password = 'pw'
        await confirm()
        expect(await outcome).toBe(true)
        expect(submit).toHaveBeenLastCalledWith({ password: 'pw' })
    })

    it('stays open with the refusal and resolves false on cancel', async () => {
        const { state, confirm, cancel } = useStepUpPrompt()
        const submit = vi.fn().mockRejectedValue(refusal)
        const outcome = requestStepUp({ title: 'Add to group', submit })
        state.credentials.password = 'wrong'
        await confirm()
        expect(state.open).toBe(true)
        expect(state.error).toBe('Confirmation failed.')
        cancel()
        expect(await outcome).toBe(false)
        expect(state.open).toBe(false)
    })

    it('refuses to close while the request is out', async () => {
        const { state, confirm, onHide } = useStepUpPrompt()
        let release!: () => void
        const submit = vi.fn(() => new Promise<void>(resolve => {
            release = resolve
        }))
        const outcome = requestStepUp({ title: 'Add to group', submit })
        const running = confirm()
        const hide = new Event('wa-hide', { cancelable: true })
        onHide(hide)
        expect(hide.defaultPrevented).toBe(true)
        expect(state.open).toBe(true)
        release()
        await running
        expect(await outcome).toBe(true)
    })
})
