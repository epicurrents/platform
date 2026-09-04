/**
 * Epicurrents WebAwesome directive for property binding.
 *
 * Usage: v-wa="[reactiveObject, 'key']"
 *
 * The directive binds reactiveObject[key] to the element two-way: it reflects
 * changes to reactiveObject[key] onto the element and writes user input back to
 * it, handling WA-INPUT (including number types with locale-aware decimal
 * parsing), WA-TEXTAREA, WA-COMBOBOX, WA-SELECT (change event), WA-SWITCH, and
 * WA-CHECKBOX (checked property).
 *
 * @package    epicurrents/platform
 * @copyright  2025 Sampsa Lohi
 * @license    Apache-2.0
 */

import { watch, type Directive } from 'vue'

const decimalSep = 1.1.toLocaleString().substring(1, 2)

type WaBinding = [Record<string, unknown>, string]
type ElEntry = { handler: EventListener; eventName: string; stop: () => void }
const elMap = new WeakMap<HTMLInputElement, ElEntry>()

const setValue = (el: HTMLInputElement, value: unknown) => {
    if (el.tagName === 'WA-SWITCH' || el.tagName === 'WA-CHECKBOX') {
        el.checked = Boolean(value ?? false)
    } else if (el.tagName === 'WA-INPUT' && typeof value === 'number') {
        el.value = value.toLocaleString()
    } else {
        el.value = (value ?? '') as string
    }
}

const waDirective: Directive<HTMLInputElement, WaBinding> = {
    beforeMount (el, binding) {
        const [obj, key] = binding.value
        // WA-CHECKBOX dispatches only `change`; WA-SELECT also only `change`.
        // WA-SWITCH, WA-INPUT, WA-TEXTAREA, WA-COMBOBOX dispatch `input`.
        const eventName = (el.tagName === 'WA-SELECT' || el.tagName === 'WA-CHECKBOX')
            ? 'change'
            : 'input'
        const inputHandler = (event: Event) => {
            const target = event.target as HTMLInputElement
            if (!target) return
            if (el.tagName === 'WA-INPUT' && el.type === 'number') {
                obj[key] = target.value.includes(decimalSep)
                    ? parseFloat(target.value)
                    : parseInt(target.value)
            } else if (el.tagName === 'WA-SWITCH' || el.tagName === 'WA-CHECKBOX') {
                obj[key] = target.checked
            } else {
                obj[key] = target.value
            }
        }
        setValue(el, obj[key] ?? '')
        el.addEventListener(eventName, inputHandler)
        // Reflect external changes to the bound value onto the element. The template
        // references the value only through this directive, not reactively, so a change
        // would not re-render the component on its own; watch it explicitly.
        const stop = watch(() => obj[key], (value) => setValue(el, value))
        elMap.set(el, { handler: inputHandler, eventName, stop })
    },
    updated (el, binding) {
        const [obj, key] = binding.value
        // Wait for all pending updates before syncing the DOM value.
        requestAnimationFrame(() => {
            setValue(el, obj[key] ?? '')
        })
    },
    beforeUnmount (el) {
        const entry = elMap.get(el)
        if (entry) {
            el.removeEventListener(entry.eventName, entry.handler)
            entry.stop()
            elMap.delete(el)
        }
    },
}
export default waDirective
