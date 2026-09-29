import axios from 'axios'
import { isMaintenanceNotice, parseRetryAfter, recordMaintenanceNotice } from '#lib/maintenanceLock'

/**
 * API base URL for all frontend HTTP calls.
 */
const baseURL = import.meta.env.VITE_API_BASE_URL ?? '/'

/**
 * Shared Axios instance for backend requests.
 *
 * The XSRF names are set to Django's defaults (axios otherwise looks for
 * `XSRF-TOKEN` / `X-XSRF-TOKEN`). On a same-origin write axios reads the
 * `csrftoken` cookie seeded on the SPA document and echoes it back in the
 * `X-CSRFToken` header, which the backend's session-CSRF chokepoint checks.
 */
export const http = axios.create({
    baseURL,
    xsrfCookieName: 'csrftoken',
    xsrfHeaderName: 'X-CSRFToken',
})

/**
 * Notice the platform being locked for maintenance.
 *
 * The lock middleware answers `503 {"detail": "maintenance", …}` to whatever
 * it refuses; recording that here, below the stores, is what lets every view
 * react through one store instead of each request handler checking for it.
 * The error still rejects, so a caller that was writing sees its write fail.
 */
http.interceptors.response.use(
    (response) => response,
    (error: unknown) => {
        type Rejected = { response?: { status?: number, data?: unknown, headers?: Record<string, unknown> } }
        const response = (error as Rejected)?.response
        if (response?.status === 503 && isMaintenanceNotice(response.data)) {
            recordMaintenanceNotice(response.data, parseRetryAfter(response.headers?.['retry-after']))
        }
        return Promise.reject(error)
    },
)

/**
 * Pull the server's own explanation out of a failed request.
 *
 * The administration API refuses things the client cannot check for itself —
 * the last-active-superuser guard, the grant count blocking a group deletion,
 * the password validators' joined messages — and each refusal arrives as a
 * `detail` string that is already the right thing to show. Falls back to
 * `fallback` for a network error, which has no response to read. A
 * maintenance refusal's `detail` is the token `"maintenance"`, so its
 * `message` is shown instead.
 *
 * @param error - the rejected value from an `http` call, of whatever shape axios produced.
 * @param fallback - message to use when the failure carries no `detail` string.
 */
export function errorDetail(error: unknown, fallback: string): string {
    const data = (error as { response?: { data?: unknown } })?.response?.data
    if (isMaintenanceNotice(data)) {
        return typeof data.message === 'string' && data.message.length > 0 ? data.message : fallback
    }
    const detail = (data as { detail?: unknown } | undefined)?.detail
    return typeof detail === 'string' && detail.length > 0 ? detail : fallback
}

/**
 * Summarise the failures of a batch of requests settled with `Promise.allSettled`.
 *
 * Returns null when every request succeeded. Otherwise `count` is the number that failed and `reason` the first
 * server explanation among them, or an empty string when none carried one, so a batch refused for one reason (a
 * release-gated dataset turning away recordings its author did not make, say) says why instead of only how many.
 *
 * @param results - the settled outcomes, in request order.
 */
export function settledFailure(results: PromiseSettledResult<unknown>[]): { count: number, reason: string } | null {
    const rejected = results.filter((result): result is PromiseRejectedResult => result.status === 'rejected')
    if (!rejected.length) {
        return null
    }
    const reason = rejected.map(result => errorDetail(result.reason, '')).find(detail => detail.length > 0) ?? ''
    return { count: rejected.length, reason }
}
