import axios from 'axios'

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
 * Pull the server's own explanation out of a failed request.
 *
 * The administration API refuses things the client cannot check for itself —
 * the last-active-superuser guard, the grant count blocking a group deletion,
 * the password validators' joined messages — and each refusal arrives as a
 * `detail` string that is already the right thing to show. Falls back to
 * `fallback` for a network error, which has no response to read.
 *
 * @param error - the rejected value from an `http` call, of whatever shape axios produced.
 * @param fallback - message to use when the failure carries no `detail` string.
 */
export function errorDetail(error: unknown, fallback: string): string {
    const detail = (error as { response?: { data?: { detail?: string } } })?.response?.data?.detail
    return typeof detail === 'string' && detail.length > 0 ? detail : fallback
}
