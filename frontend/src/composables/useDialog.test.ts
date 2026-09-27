/**
 * Tests for the dialog state: it closes on success, ignores a second submit while the first is out, keeps itself
 * open against Escape while busy, and shows the server's refusal.
 */

import { describe, expect, it, vi } from 'vitest'

vi.mock('#i18n', () => ({ t: (key: string) => key }))

import { useDialog } from '#composables/useDialog'

function deferred<T> () {
    let resolve!: (value: T) => void
    let reject!: (reason: unknown) => void
    const promise = new Promise<T>((res, rej) => {
        resolve = res
        reject = rej
    })
    return { promise, resolve, reject }
}

describe('useDialog', () => {
    it('closes when the action succeeds', async () => {
        const dialog = useDialog()
        dialog.show()
        const result = await dialog.run(async () => 'done', { fallback: 'failed' })
        expect(result).toBe('done')
        expect(dialog.open.value).toBe(false)
        expect(dialog.busy.value).toBe(false)
    })

    it('ignores a second submit while the first is out', async () => {
        const dialog = useDialog()
        dialog.show()
        const pending = deferred<string>()
        const perform = vi.fn(() => pending.promise)
        const first = dialog.run(perform, { fallback: 'failed' })
        const second = await dialog.run(perform, { fallback: 'failed' })
        expect(second).toBeUndefined()
        expect(perform).toHaveBeenCalledTimes(1)
        pending.resolve('ok')
        await first
        expect(dialog.open.value).toBe(false)
    })

    it('refuses to hide while busy, and hides otherwise', async () => {
        const dialog = useDialog()
        dialog.show()
        const pending = deferred<void>()
        const running = dialog.run(() => pending.promise, { fallback: 'failed' })
        const busyHide = new Event('wa-hide', { cancelable: true })
        dialog.onHide(busyHide)
        expect(busyHide.defaultPrevented).toBe(true)
        expect(dialog.open.value).toBe(true)
        dialog.close()
        expect(dialog.open.value).toBe(true)
        pending.resolve()
        await running
        dialog.show()
        const idleHide = new Event('wa-hide', { cancelable: true })
        dialog.onHide(idleHide)
        expect(idleHide.defaultPrevented).toBe(false)
        expect(dialog.open.value).toBe(false)
    })

    it('stays open with the server detail on failure', async () => {
        const dialog = useDialog()
        dialog.show()
        const refusal = Object.assign(new Error('409'), {
            response: { status: 409, data: { detail: 'Already confirmed.' } },
        })
        const result = await dialog.run(() => Promise.reject(refusal), { fallback: 'failed' })
        expect(result).toBeUndefined()
        expect(dialog.open.value).toBe(true)
        expect(dialog.error.value).toBe('Already confirmed.')
    })

    it('leaves the error to a handler that claims the failure', async () => {
        const dialog = useDialog()
        dialog.show()
        await dialog.run(() => Promise.reject(new Error('x')), { fallback: 'failed', onError: () => true })
        expect(dialog.error.value).toBe('')
        expect(dialog.open.value).toBe(true)
    })
})
