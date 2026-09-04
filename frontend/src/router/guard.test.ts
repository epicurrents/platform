/**
 * Tests for the navigation guard, focused on the re-validation that keeps a
 * just-authenticated participant (or one recovering from a transient /me error)
 * out of the login redirect.
 */

import { vi, describe, it, expect, beforeEach } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'
import type { RouteLocationNormalized } from 'vue-router'

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

import { authGuard } from './guard'
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
}

function route (meta: Record<string, unknown> = {}, query: Record<string, string> = {}): RouteLocationNormalized {
    return {
        meta,
        query,
        fullPath: '/target',
        path: '/target',
        name: 'target',
        hash: '',
        params: {},
        matched: [],
        redirectedFrom: undefined,
    } as unknown as RouteLocationNormalized
}

describe('authGuard', () => {
    beforeEach(() => {
        setActivePinia(createPinia())
        vi.clearAllMocks()
    })

    it('allows an unguarded route without touching the server', async () => {
        const result = await authGuard(route())
        expect(result).toBe(true)
        expect(mockFetchMe).not.toHaveBeenCalled()
    })

    it('redirects a genuinely logged-out caller to login without double-fetching', async () => {
        mockFetchMe.mockResolvedValueOnce(null)
        const result = await authGuard(route({ requiresAuth: true }))
        expect(result).toEqual({ name: 'login', query: { redirect: '/target' } })
        // init() fetched once; no redundant re-validate on a fresh fetch.
        expect(mockFetchMe).toHaveBeenCalledTimes(1)
    })

    it('re-validates a stale unauthenticated store and allows the just-joined user', async () => {
        // Earlier visit cached "logged out" (the participant had not joined yet).
        mockFetchMe.mockResolvedValueOnce(null)
        const store = useAuthStore()
        await store.init()
        expect(store.isAuthenticated).toBe(false)

        // The join set a cookie; the next /me now succeeds. The guard must
        // re-check rather than trust the memoised null.
        mockFetchMe.mockResolvedValueOnce(STUDENT)
        const result = await authGuard(route({ requiresAuthUnlessToken: true }, { session: 'abc' }))
        expect(result).toBe(true)
        expect(store.isAuthenticated).toBe(true)
        expect(mockFetchMe).toHaveBeenCalledTimes(2)
    })

    it('sends an authenticated non-staff user away from a staff-only route', async () => {
        mockFetchMe.mockResolvedValueOnce(STUDENT)
        const result = await authGuard(route({ requiresAuth: true, requiresStaff: true }))
        expect(result).toEqual({ name: 'home' })
    })

    it('exempts a share-token URL from the auth check', async () => {
        const result = await authGuard(route({ requiresAuthUnlessToken: true }, { token: 'share' }))
        expect(result).toBe(true)
        expect(mockFetchMe).not.toHaveBeenCalled()
    })
})
