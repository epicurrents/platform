/**
 * Tests for the v-wa directive's number inputs: values cross in the input's own '.'-decimal form whatever the
 * locale, and a cleared field reads as NaN.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { nextTick, reactive, type ObjectDirective } from 'vue'

type Bound = Record<string, unknown>

/** A stand-in for a number-type wa-input: the directive reads only its tag name, type and value. */
function numberInput (): HTMLInputElement {
    const el = document.createElement('wa-input') as HTMLInputElement
    Object.defineProperty(el, 'type', { value: 'number' })
    Object.defineProperty(el, 'value', { value: '', writable: true })
    return el
}

function type (el: HTMLInputElement, value: string) {
    el.value = value
    el.dispatchEvent(new Event('input'))
}

describe('v-wa on a number input', () => {
    let directive: ObjectDirective<HTMLInputElement, [Bound, string]>

    beforeEach(async () => {
        // A comma-decimal locale with a thousands separator, in force before the module loads, so any locale read at
        // import time sees it too.
        vi.spyOn(Number.prototype, 'toLocaleString').mockImplementation(function (this: number) {
            return this.toFixed(this % 1 ? 1 : 0).replace('.', ',').replace(/\B(?=(\d{3})+(?!\d))/g, ' ')
        })
        vi.resetModules()
        directive = (await import('./wa')).default as typeof directive
    })

    afterEach(() => {
        vi.restoreAllMocks()
    })

    function bind (state: Bound) {
        const el = numberInput()
        directive.beforeMount!(el, { value: [state, 'n'] } as never, null as never, null as never)
        return el
    }

    it('reads a decimal without truncating it', () => {
        const state = reactive<Bound>({ n: '' })
        const el = bind(state)
        type(el, '12.5')
        expect(state.n).toBe(12.5)
    })

    it('reads a cleared field as NaN', () => {
        const state = reactive<Bound>({ n: 3 })
        const el = bind(state)
        type(el, '')
        expect(state.n).toBeNaN()
    })

    it('writes a number in the form the input accepts', async () => {
        const state = reactive<Bound>({ n: 12.5 })
        const el = bind(state)
        expect(el.value).toBe('12.5')
        state.n = 1234
        await nextTick()
        expect(el.value).toBe('1234')
    })

    it('writes NaN back as an empty field', async () => {
        const state = reactive<Bound>({ n: 1 })
        const el = bind(state)
        state.n = NaN
        await nextTick()
        expect(el.value).toBe('')
    })
})
