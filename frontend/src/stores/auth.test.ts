/**
 * Regression tests for the auth store's refresh()/init() interaction.
 *
 * A project's session join logs the browser in server-side, then navigates to the
 * viewer. If init() had cached an unauthenticated state, the viewer route guard
 * would read isAuthenticated as false and redirect to login. refresh() forces a
 * re-fetch after such an out-of-band auth change, and classifies failures so a
 * transient /me error does not strand the user as permanently logged out.
 */

import { vi, describe, it, expect, beforeEach } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'

// Mock the API and webpush layers before the store module is imported.
vi.mock('#api/user', () => ({
    fetchMe: vi.fn(),
    login: vi.fn(),
    logout: vi.fn(),
}))
vi.mock('#lib/webpush', () => ({
    isPushSupported: () => false,
    subscribeToPush: vi.fn(),
    unsubscribeFromPush: vi.fn(),
}))

import { useAuthStore } from '#stores/auth'
import { fetchMe, type AuthUser } from '#api/user'

const mockFetchMe = vi.mocked(fetchMe)

const STUDENT: AuthUser = {
    id: 1,
    username: 'student',
    email: '',
    first_name: 'Shared',
    last_name: 'Student',
    is_staff: false,
    is_superuser: false,
    is_2fa_enabled: false,
}

/** An axios-shaped HTTP error carrying a response status. */
function httpError (status: number) {
    return Object.assign(new Error(`HTTP ${status}`), { response: { status } })
}

describe('auth store', () => {
    beforeEach(() => {
        setActivePinia(createPinia())
        vi.clearAllMocks()
    })

    it('init() is memoised after a resolved logged-out state: a second call does not re-fetch', async () => {
        mockFetchMe.mockResolvedValueOnce(null)
        const store = useAuthStore()
        await store.init()
        await store.init()
        expect(mockFetchMe).toHaveBeenCalledTimes(1)
        expect(store.isAuthenticated).toBe(false)
        expect(store.initialized).toBe(true)
    })

    it('refresh() re-fetches after init() resolved an unauthenticated state', async () => {
        mockFetchMe.mockResolvedValueOnce(null)
        const store = useAuthStore()
        await store.init()
        expect(store.isAuthenticated).toBe(false)

        // The join logs the browser in server-side; refresh picks it up even
        // though initialized is already true.
        mockFetchMe.mockResolvedValueOnce(STUDENT)
        const ok = await store.refresh()
        expect(ok).toBe(true)
        expect(mockFetchMe).toHaveBeenCalledTimes(2)
        expect(store.isAuthenticated).toBe(true)
        expect(store.user).toEqual(STUDENT)
    })

    it('a transient failure does not strand the user: init() retries on the next call', async () => {
        mockFetchMe.mockRejectedValueOnce(httpError(503))
        const store = useAuthStore()
        await store.init()
        expect(store.isAuthenticated).toBe(false)
        // Not cached — a 5xx is not an authoritative "logged out".
        expect(store.initialized).toBe(false)

        mockFetchMe.mockResolvedValueOnce(STUDENT)
        await store.init()
        expect(mockFetchMe).toHaveBeenCalledTimes(2)
        expect(store.isAuthenticated).toBe(true)
    })

    it('refresh() reports false and clears the user on failure', async () => {
        mockFetchMe.mockResolvedValueOnce(STUDENT)
        const store = useAuthStore()
        expect(await store.refresh()).toBe(true)

        mockFetchMe.mockRejectedValueOnce(httpError(500))
        expect(await store.refresh()).toBe(false)
        expect(store.isAuthenticated).toBe(false)
    })
})
