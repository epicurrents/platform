/**
 * The open / busy / error state of one confirmation dialog, and the submit that closes it.
 *
 * Every confirmation dialog in the admin views has the same three rules, and
 * each broke at least once when written by hand: the dialog closes when its
 * action succeeds, a second click while the first is out is ignored rather
 * than sent again, and neither Escape nor the overlay closes it while the
 * request is out — a closed dialog would hide the answer. `onHide` is the
 * `@wa-hide.self` handler: it cancels the hide while busy, which is what
 * `preventDefault` on that event does.
 *
 * @package    epicurrents-platform
 */

import { ref } from 'vue'
import { errorDetail } from '#lib/http'

export interface DialogRunOptions {
    /** Message shown when the failure carries no `detail` of its own. */
    fallback: string
    /** Handle a failure specially; return true to keep the generic error message from being set. */
    onError?: (error: unknown) => boolean
}

/** One dialog's state. `run` resolves to the action's result, or `undefined` when it failed or was ignored. */
export function useDialog() {
    const open = ref(false)
    const busy = ref(false)
    const error = ref('')

    function show() {
        error.value = ''
        open.value = true
    }

    /** Close unless an action is out. */
    function close() {
        if (busy.value) {
            return
        }
        open.value = false
    }

    /** The dialog's `wa-hide` handler: refuse to close while busy, otherwise follow the component. */
    function onHide(event: Event) {
        if (busy.value) {
            event.preventDefault()
            return
        }
        open.value = false
    }

    async function run<T>(perform: () => Promise<T>, options: DialogRunOptions): Promise<T | undefined> {
        if (busy.value) {
            return undefined
        }
        busy.value = true
        error.value = ''
        let result: T
        try {
            result = await perform()
        } catch (err) {
            busy.value = false
            if (!options.onError?.(err)) {
                error.value = errorDetail(err, options.fallback)
            }
            return undefined
        }
        // Not busy first: the dialog may announce the close as a hide, which must not be refused.
        busy.value = false
        open.value = false
        return result
    }

    return { open, busy, error, show, close, onHide, run }
}
