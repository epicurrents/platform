/**
 * Tests for the maintenance store: what the SPA does with a maintenance 503.
 *
 * The lock arrives through the HTTP interceptor, which records it in
 * `lib/maintenanceLock`; the store watches that and owns the consequences.
 * The ones worth pinning: one toast per phase rather than per request, a
 * reload when the platform comes back on new code (any lifted lock but
 * `verifying`, and the step from `updating` to `verifying`), a toast rather
 * than a reload when a confirmed update's window closes, the release poll,
 * and the feature probe reading a 404 as "off" and nothing else as "off".
 */

import { vi, describe, it, expect, beforeEach, afterEach } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'
import { nextTick } from 'vue'

vi.mock('#api/user', () => ({
    fetchMe: vi.fn(),
}))
vi.mock('#api/maintenance', async (importOriginal) => {
    const actual = await importOriginal<typeof import('#api/maintenance')>()
    return { ...actual, fetchMaintenanceStatus: vi.fn() }
})
vi.mock('#lib/toast', () => ({
    showToast: vi.fn(),
}))
vi.mock('#i18n', () => ({
    t: (key: string) => key,
}))

import { fetchMe } from '#api/user'
import { fetchMaintenanceStatus } from '#api/maintenance'
import { clearMaintenanceNotice, recordMaintenanceNotice, type MaintenanceNotice } from '#lib/maintenanceLock'
import { showToast } from '#lib/toast'
import { RELEASE_POLL_MS, useMaintenanceStore } from '#stores/maintenance'

const mockFetchMe = vi.mocked(fetchMe)
const mockStatus = vi.mocked(fetchMaintenanceStatus)
const mockToast = vi.mocked(showToast)

function notice (phase: string, message = 'The platform is being updated.'): MaintenanceNotice {
    return { detail: 'maintenance', phase, since: '2026-09-20T10:00:00Z', expected_until: null, message }
}

/** An axios-shaped rejection carrying a response status. */
function httpError (status: number) {
    return Object.assign(new Error(`HTTP ${status}`), { response: { status } })
}

describe('maintenance store', () => {
    let reloads = 0
    let store: ReturnType<typeof useMaintenanceStore>

    beforeEach(() => {
        setActivePinia(createPinia())
        vi.clearAllMocks()
        vi.useFakeTimers()
        clearMaintenanceNotice()
        reloads = 0
        store = useMaintenanceStore()
        store.setReloadHandler(() => {
            reloads += 1
        })
    })

    afterEach(() => {
        // The store watches a module-level signal; an undisposed one from an
        // earlier test would keep reacting to the next test's notices.
        store.$dispose()
        vi.useRealTimers()
    })

    it('starts unlocked and reads the lock the interceptor records', async () => {
        expect(store.locked).toBe(false)
        recordMaintenanceNotice(notice('updating'))
        await nextTick()
        expect(store.locked).toBe(true)
        expect(store.phase).toBe('updating')
        expect(store.suspended).toBe(true)
    })

    it('announces a phase once, however many refusals repeat it', async () => {
        recordMaintenanceNotice(notice('updating'))
        await nextTick()
        recordMaintenanceNotice(notice('updating'))
        await nextTick()
        recordMaintenanceNotice(notice('updating'))
        await nextTick()
        expect(mockToast).toHaveBeenCalledTimes(1)
        expect(mockToast.mock.calls[0]?.[1]).toBe('brand')
    })

    it('verifying is a lock that does not suspend', async () => {
        recordMaintenanceNotice(notice('verifying'))
        await nextTick()
        expect(store.locked).toBe(true)
        expect(store.suspended).toBe(false)
    })

    it('reloads when the lock moves from updating to verifying: the new release is serving', async () => {
        recordMaintenanceNotice(notice('updating'))
        await nextTick()
        recordMaintenanceNotice(notice('verifying'))
        await nextTick()
        expect(reloads).toBe(1)
    })

    it('polls /me while locked and reloads once the platform answers after an update', async () => {
        recordMaintenanceNotice(notice('rolling_back'))
        await nextTick()
        mockFetchMe.mockRejectedValueOnce(httpError(503))
        await vi.advanceTimersByTimeAsync(RELEASE_POLL_MS)
        expect(mockFetchMe).toHaveBeenCalledTimes(1)
        expect(store.locked).toBe(true)
        mockFetchMe.mockResolvedValueOnce(null)
        await vi.advanceTimersByTimeAsync(RELEASE_POLL_MS)
        expect(mockFetchMe).toHaveBeenCalledTimes(2)
        expect(store.locked).toBe(false)
        expect(reloads).toBe(1)
    })

    it('a lifted verifying lock is announced, not reloaded: the page already runs the confirmed release', async () => {
        recordMaintenanceNotice(notice('verifying'))
        await nextTick()
        mockToast.mockClear()
        mockFetchMe.mockResolvedValueOnce(null)
        await vi.advanceTimersByTimeAsync(RELEASE_POLL_MS)
        expect(store.locked).toBe(false)
        expect(reloads).toBe(0)
        expect(mockToast).toHaveBeenCalledTimes(1)
    })

    it('stops polling once released', async () => {
        recordMaintenanceNotice(notice('verifying'))
        await nextTick()
        mockFetchMe.mockResolvedValueOnce(null)
        await vi.advanceTimersByTimeAsync(RELEASE_POLL_MS)
        await vi.advanceTimersByTimeAsync(RELEASE_POLL_MS * 3)
        expect(mockFetchMe).toHaveBeenCalledTimes(1)
    })

    it('the feature probe reads a 404 as off, a success as on, and anything else as unknown', async () => {
        mockStatus.mockRejectedValueOnce(httpError(404))
        expect(await store.probeFeature()).toBe(false)
        expect(store.featureEnabled).toBe(false)

        setActivePinia(createPinia())
        const fresh = useMaintenanceStore()
        mockStatus.mockRejectedValueOnce(httpError(500))
        expect(await fresh.probeFeature()).toBe(false)
        expect(fresh.featureEnabled).toBeNull()
        mockStatus.mockResolvedValueOnce({ installed_version: '0.1.2' } as never)
        expect(await fresh.probeFeature()).toBe(true)
        expect(fresh.status?.installed_version).toBe('0.1.2')
        expect(mockStatus).toHaveBeenCalledTimes(3)
        fresh.$dispose()
    })

    it('the probe is memoised once answered', async () => {
        mockStatus.mockRejectedValueOnce(httpError(404))
        await store.probeFeature()
        await store.probeFeature()
        expect(mockStatus).toHaveBeenCalledTimes(1)
    })
})
