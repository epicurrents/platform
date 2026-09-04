import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import {
    fetchMe,
    login as apiLogin,
    logout as apiLogout,
    startLoginEnrolment as apiStartLoginEnrolment,
    submitTwoFactorCode as apiSubmitTwoFactorCode,
    type AuthUser,
} from '#api/user'
import { isPushSupported, subscribeToPush, unsubscribeFromPush } from '#lib/webpush'

/**
 * Tell an embedded viewer (if one is mounted) that the platform session is valid again, so any
 * signal loads it latched on a prior auth failure resume. A no-op when no viewer is open — the
 * callback is only present while a viewer app exists. One-way: the platform announces the fact,
 * the viewer decides what to re-run.
 */
function notifyViewerSessionRestored () {
    window.__EPICURRENTS__?.notifySessionRestored?.()
}

export const useAuthStore = defineStore('auth', () => {
    const user = ref<AuthUser | null>(null)
    const initialized = ref(false)

    const isAuthenticated = computed(() => user.value !== null)
    const isStaff = computed(() => user.value?.is_staff === true || user.value?.is_superuser === true)
    const isSuperuser = computed(() => user.value?.is_superuser === true)

    /**
     * Re-fetch the current auth state from the server and report whether a user
     * is now present. The single source of truth for auth state: the probe
     * answers "logged in" or "logged out" with HTTP 200, and either resolved
     * answer marks the store initialised. Only a transient failure (5xx,
     * network) rejects, leaving `initialized` false so the next navigation
     * retries rather than caching a false "logged out". Called by `init()`, by
     * the route guard before any login redirect, and after an out-of-band login
     * such as a project's session join.
     */
    async function refresh(): Promise<boolean> {
        const wasAuthenticated = user.value !== null
        try {
            user.value = await fetchMe()
            initialized.value = true
        } catch {
            user.value = null
        }
        if (!wasAuthenticated && user.value !== null) {
            // Session came back (a silent restore or an out-of-band login) — nudge an open viewer.
            notifyViewerSessionRestored()
        }
        return user.value !== null
    }

    async function init() {
        if (initialized.value) return
        await refresh()
        // Re-register silently on page load if the user already granted permission.
        // Avoids a stale/missing subscription after page refreshes without prompting.
        if (user.value !== null && isPushSupported() && Notification.permission === 'granted') {
            subscribeToPush()
        }
    }

    /**
     * Adopt a user the server has just opened a session for.
     *
     * Both halves of a two-step login end here, and neither may run before the
     * session actually exists — a correct password alone does not authenticate
     * an account with a second factor, so the push subscription and the viewer
     * notification wait for the step that does.
     */
    function adoptSession(signedIn: AuthUser) {
        user.value = signedIn
        notifyViewerSessionRestored()
        // Request push permission after login (called in response to a user gesture,
        // which satisfies browser permission prompt requirements).
        subscribeToPush()
    }

    /**
     * Sign in with a password. Resolves to `'two_factor_required'` when the
     * password was right but the account owes a code, in which case the caller
     * collects one and calls `completeTwoFactor`; the store holds no user until
     * then.
     */
    async function login (
        username: string,
        password: string,
    ): Promise<'signed-in' | 'two_factor_required' | 'two_factor_enrolment_required'> {
        const result = await apiLogin(username, password)
        if (result.two_factor_enrolment_required) {
            return 'two_factor_enrolment_required'
        }
        if (result.two_factor_required || !result.user) {
            return 'two_factor_required'
        }
        adoptSession(result.user)
        return 'signed-in'
    }

    /**
     * Begin enrolment for a login that cannot proceed without a second factor.
     * Returns the secret to display; `completeTwoFactor` then confirms it and
     * opens the session in one step.
     */
    async function startEnrolment () {
        return await apiStartLoginEnrolment()
    }

    /**
     * Finish a login that came back needing a second factor. Rejects on a bad code.
     *
     * Returns the account's recovery codes when this login also completed a
     * first-time enrolment; they are issued once and the caller must show them
     * before navigating away.
     */
    async function completeTwoFactor (code: string): Promise<string[] | null> {
        const result = await apiSubmitTwoFactorCode(code)
        if (!result.user) {
            throw new Error('Two-factor verification did not return a user')
        }
        adoptSession(result.user)
        return result.backup_codes ?? null
    }

    async function logout() {
        // Remove subscription before clearing the session so the DELETE request
        // is still authenticated.
        await unsubscribeFromPush()
        await apiLogout()
        user.value = null
    }

    return {
        user,
        initialized,
        isAuthenticated,
        isStaff,
        isSuperuser,
        init,
        refresh,
        login,
        completeTwoFactor,
        startEnrolment,
        logout,
    }
})
