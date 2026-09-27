/**
 * The maintenance lock as the HTTP layer sees it.
 *
 * A module of its own, below both the HTTP wrapper and the stores, so that
 * the response interceptor in `lib/http.ts` can record a 503 without
 * importing a store that imports the wrapper back. The maintenance store
 * watches this state and owns everything that follows from it — polling,
 * toasts, the reload when the platform comes back.
 *
 * @package    epicurrents-platform
 */

import { reactive, readonly } from 'vue'

/** The body every maintenance 503 carries. */
export interface MaintenanceNotice {
    detail: 'maintenance'
    phase: string
    since: string | null
    expected_until: string | null
    message: string
}

/**
 * `sequence` counts notices rather than timestamping them: two refusals in the
 * same millisecond must still both be seen by a watcher, and a timestamp
 * would not change between them. `retryAfter` is the refusal's `Retry-After`
 * in seconds, when it carried one.
 */
const state = reactive<{ notice: MaintenanceNotice | null, sequence: number, retryAfter: number | null }>({
    notice: null,
    sequence: 0,
    retryAfter: null,
})

/** Read-only view of the last maintenance answer, or `null` when none has been seen since the last clear. */
export const maintenanceLock = readonly(state)

/** True for a response body the middleware wrote: `detail` is the literal `"maintenance"`. */
export function isMaintenanceNotice(data: unknown): data is MaintenanceNotice {
    return typeof data === 'object' && data !== null && (data as { detail?: unknown }).detail === 'maintenance'
}

/**
 * Read a `Retry-After` header given in seconds; the HTTP-date form and anything unparseable read as `null`.
 *
 * @param value - the raw header value, of whatever type the HTTP client handed over.
 */
export function parseRetryAfter(value: unknown): number | null {
    if (typeof value !== 'string' && typeof value !== 'number') {
        return null
    }
    const seconds = Number(value)
    return Number.isFinite(seconds) && seconds >= 0 ? seconds : null
}

/**
 * Record a maintenance answer; the store reacts to the change.
 *
 * @param notice - the 503 body.
 * @param retryAfter - the response's `Retry-After` in seconds, or `null` when it had none.
 */
export function recordMaintenanceNotice(notice: MaintenanceNotice, retryAfter: number | null = null): void {
    state.notice = notice
    state.retryAfter = retryAfter
    state.sequence += 1
}

/** Forget the lock, once the probe says it is gone. */
export function clearMaintenanceNotice(): void {
    state.notice = null
    state.retryAfter = null
}
