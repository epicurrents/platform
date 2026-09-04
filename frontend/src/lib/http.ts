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
