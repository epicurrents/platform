/**
 * The one step-up prompt: a dialog asking for the caller's own credentials before a request that needs them.
 *
 * A singleton, rendered once by `StepUpDialog` in `App.vue`, so any view can
 * ask for confirmation without owning a dialog of its own. `withStepUp` is the
 * entry point: it sends the request straight away when the client-side rule
 * says no confirmation is needed, and otherwise opens the prompt and sends it
 * with what the caller types. A failure while prompting — a wrong code, a
 * lockout, the server's own refusal — is shown in the prompt, which stays open
 * for another try; the request's promise settles only on success or cancel.
 *
 * If the rule and the server disagree and the server asks for a confirmation
 * the rule did not expect, the prompt opens then instead of the request
 * failing.
 *
 * @package    epicurrents-platform
 */

import { reactive } from 'vue'
import type { StepUpCredentials } from '#api/maintenance'
import { t } from '#i18n'
import { errorDetail } from '#lib/http'
import { isStepUpRefusal, stepUpBody } from '#lib/stepUp'

const SCOPE = 'StepUpPrompt'

export interface StepUpRequest {
    /** Dialog heading, naming the action. */
    title: string
    /** Optional sentence saying why this action asks for confirmation. */
    message?: string
    /** Send the request with the credentials; throw to keep the prompt open with the error. */
    submit: (credentials: StepUpCredentials) => Promise<void>
}

const state = reactive({
    open: false,
    busy: false,
    error: '',
    title: '',
    message: '',
    credentials: { password: '', totp_code: '' },
})

let pending: { submit: StepUpRequest['submit'], resolve: (confirmed: boolean) => void } | null = null

function settle(confirmed: boolean) {
    const current = pending
    pending = null
    state.open = false
    state.busy = false
    state.credentials.password = ''
    state.credentials.totp_code = ''
    current?.resolve(confirmed)
}

/** Open the prompt. Resolves `true` once `submit` succeeded, `false` when the caller cancelled. */
export function requestStepUp(request: StepUpRequest): Promise<boolean> {
    if (pending !== null) {
        settle(false)
    }
    state.title = request.title
    state.message = request.message ?? ''
    state.error = ''
    state.credentials.password = ''
    state.credentials.totp_code = ''
    state.open = true
    return new Promise(resolve => {
        pending = { submit: request.submit, resolve }
    })
}

/**
 * Send a request, confirming first when `required`.
 *
 * Resolves `true` when the request succeeded and `false` when the prompt was
 * cancelled. A request that needed no confirmation rejects with its own error,
 * for the caller to show as it would any other.
 *
 * @param required - whether the client-side rule says this request needs confirming.
 * @param prompt - title and message for the prompt.
 * @param submit - sends the request; receives `{}` when no confirmation was asked for.
 */
export async function withStepUp(
    required: boolean,
    prompt: { title: string, message?: string },
    submit: (credentials: StepUpCredentials) => Promise<void>,
): Promise<boolean> {
    if (!required) {
        try {
            await submit({})
            return true
        } catch (err) {
            if (!isStepUpRefusal(err)) {
                throw err
            }
        }
    }
    return requestStepUp({ ...prompt, submit })
}

/** The prompt's state and controls, for `StepUpDialog`. */
export function useStepUpPrompt() {
    async function confirm() {
        if (pending === null || state.busy) {
            return
        }
        state.busy = true
        state.error = ''
        try {
            await pending.submit(stepUpBody(state.credentials))
        } catch (err) {
            state.busy = false
            state.credentials.totp_code = ''
            state.error = errorDetail(err, t('The change could not be made.', SCOPE))
            return
        }
        settle(true)
    }

    function cancel() {
        if (state.busy) {
            return
        }
        settle(false)
    }

    /** The dialog's `wa-hide` handler: refused while the request is out, a cancel otherwise. */
    function onHide(event: Event) {
        if (state.busy) {
            event.preventDefault()
            return
        }
        settle(false)
    }

    return { state, confirm, cancel, onHide }
}
