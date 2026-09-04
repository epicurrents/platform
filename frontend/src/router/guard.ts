/**
 * Navigation guard for auth- and staff-restricted routes.
 *
 * Extracted from the router so it can be unit-tested without instantiating the
 * full route table. Registered via `router.beforeEach(authGuard)`.
 *
 * @package    epicurrents-platform
 */

import type { RouteLocationNormalized } from 'vue-router'
import { useAuthStore } from '#stores/auth'

/**
 * Resolve whether navigation to `to` is allowed.
 *
 * Returns `true` to proceed, or a route location to redirect to. Routes opt in
 * via `meta.requiresAuth`, `meta.requiresAuthUnlessToken` (a `?token=` share
 * credential exempts the auth check), and `meta.requiresStaff`.
 *
 * When the memoised auth store reads unauthenticated, the guard re-validates
 * against the server before redirecting to login. The store can be stale in two
 * ways its cached flag cannot distinguish: an out-of-band login (a project's
 * session join logs the browser in server-side) or a transient `/me` failure.
 * Re-checking lets the cookie — the real source of truth — decide, and is
 * skipped when the store fetched fresh during this navigation (`init()` was not
 * a memoised no-op) so a genuinely logged-out caller is not double-fetched.
 */
export async function authGuard (to: RouteLocationNormalized): Promise<true | { name: string, query?: Record<string, string> }> {
    const requiresAuth = to.meta.requiresAuth
    const requiresAuthUnlessToken = to.meta.requiresAuthUnlessToken && !to.query.token
    if (!requiresAuth && !requiresAuthUnlessToken) {
        return true
    }

    const authStore = useAuthStore()
    const wasInitialized = authStore.initialized
    await authStore.init()

    if (!authStore.isAuthenticated && wasInitialized) {
        await authStore.refresh()
    }

    if (!authStore.isAuthenticated) {
        return { name: 'login', query: { redirect: to.fullPath } }
    }

    if (to.meta.requiresStaff && !authStore.isStaff) {
        return { name: 'home' }
    }

    return true
}
