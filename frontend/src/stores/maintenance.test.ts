/**
 * Tests for the maintenance store: what the SPA does with the maintenance lock.
 *
 * The lock arrives three ways — a 503 the HTTP interceptor records in
 * `lib/maintenanceLock`, a status read, and the public lock probe — and only
 * the probe decides that it is gone. The ones worth pinning: one toast per
 * phase rather than per request, dismissed when the phase moves on; a reload
 * exactly when the platform comes back on new code (leaving `updating` or
 * `rolling_back` for `verifying` or for no lock) and never otherwise; release
 * detected by the probe alone, since a superuser's reads pass during a lock;
 * the probe paused while the tab is hidden; and the feature probe reading a
 * 404 as "off" and nothing else as "off".
 */

import { vi, describe, it, expect, beforeEach, afterEach } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'
import { nextTick } from 'vue'

vi.mock('#api/maintenance', async (importOriginal) => {
    const actual = await importOriginal<typeof import('#api/maintenance')>()
    return { ...actual, fetchMaintenanceStatus: vi.fn(), fetchLock: vi.fn() }
})
vi.mock('#lib/toast', () => ({
    showToast: vi.fn(() => 7),
    dismissToast: vi.fn(),
}))
vi.mock('#i18n', () => ({
    t: (key: string) => key,
}))

import { fetchLock, fetchMaintenanceStatus, type LockProbe, type MaintenanceStatus } from '#api/maintenance'
import { clearMaintenanceNotice, recordMaintenanceNotice, type MaintenanceNotice } from '#lib/maintenanceLock'
import { dismissToast, showToast } from '#lib/toast'
import {
    codeChanged,
    RELEASE_POLL_MS,
    reloadPage,
    RETRY_AFTER_MAX_MS,
    useMaintenanceStore,
    VERIFYING_POLL_MS,
} from '#stores/maintenance'

const mockStatus = vi.mocked(fetchMaintenanceStatus)
const mockLock = vi.mocked(fetchLock)
const mockToast = vi.mocked(showToast)
const mockDismiss = vi.mocked(dismissToast)

function notice (phase: string, message = 'The platform is being updated.'): MaintenanceNotice {
    return { detail: 'maintenance', phase, since: '2026-09-20T10:00:00Z', expected_until: null, message }
}

function probe (phase: string | null): LockProbe {
    return phase === null
        ? { locked: false, phase: null, since: null, expected_until: null, message: null }
        : { locked: true, phase, since: '2026-09-20T10:00:00Z', expected_until: null, message: `Locked: ${phase}` }
}

function statusWithLock (phase: string | null): MaintenanceStatus {
    return {
        installed_version: '0.1.2',
        server_now: '2026-09-20T10:00:00Z',
        lock: phase === null
            ? null
            : { phase, since: null, expected_until: null, message: `Locked: ${phase}`, job_id: null },
    } as unknown as MaintenanceStatus
}

/** An axios-shaped rejection carrying a response status. */
function httpError (status: number) {
    return Object.assign(new Error(`HTTP ${status}`), { response: { status } })
}

function setHidden (hidden: boolean) {
    Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => hidden ? 'hidden' : 'visible' })
    document.dispatchEvent(new Event('visibilitychange'))
}

describe('codeChanged', () => {
    it('is true only when leaving a code-changing phase for verifying or for the release', () => {
        expect(codeChanged('updating', 'verifying')).toBe(true)
        expect(codeChanged('updating', null)).toBe(true)
        expect(codeChanged('rolling_back', null)).toBe(true)
        expect(codeChanged('rolling_back', 'verifying')).toBe(true)
        expect(codeChanged('updating', 'rolling_back')).toBe(false)
        expect(codeChanged('verifying', null)).toBe(false)
        expect(codeChanged('verifying', 'rolling_back')).toBe(false)
        expect(codeChanged(null, 'updating')).toBe(false)
        expect(codeChanged(null, 'verifying')).toBe(false)
    })
})

describe('maintenance store', () => {
    let reloads = 0
    let store: ReturnType<typeof useMaintenanceStore>

    beforeEach(() => {
        setActivePinia(createPinia())
        vi.clearAllMocks()
        vi.useFakeTimers()
        clearMaintenanceNotice()
        setHidden(false)
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

    it('asks the probe, never /me, and reloads once it reports updating moved to verifying', async () => {
        recordMaintenanceNotice(notice('updating'))
        await nextTick()
        mockLock.mockResolvedValueOnce(probe('updating'))
        await vi.advanceTimersByTimeAsync(RELEASE_POLL_MS)
        expect(mockLock).toHaveBeenCalledTimes(1)
        expect(reloads).toBe(0)
        mockLock.mockResolvedValueOnce(probe('verifying'))
        await vi.advanceTimersByTimeAsync(RELEASE_POLL_MS)
        expect(mockLock).toHaveBeenCalledTimes(2)
        expect(reloads).toBe(1)
    })

    it('reloads when the probe reports release after a rollback', async () => {
        recordMaintenanceNotice(notice('rolling_back'))
        await nextTick()
        mockLock.mockRejectedValueOnce(httpError(502))
        await vi.advanceTimersByTimeAsync(RELEASE_POLL_MS)
        expect(store.locked).toBe(true)
        mockLock.mockResolvedValueOnce(probe(null))
        await vi.advanceTimersByTimeAsync(RELEASE_POLL_MS)
        expect(store.locked).toBe(false)
        expect(reloads).toBe(1)
    })

    it('a superuser learns of an update from the status and reloads when the probe says verifying', async () => {
        // No 503 ever reaches a superuser's reads; the status is how the store hears of the lock.
        mockStatus.mockResolvedValueOnce(statusWithLock('updating'))
        await store.refreshStatus()
        expect(store.phase).toBe('updating')
        mockLock.mockResolvedValueOnce(probe('verifying'))
        await vi.advanceTimersByTimeAsync(RELEASE_POLL_MS)
        expect(reloads).toBe(1)
    })

    it('a lifted verifying lock is announced, not reloaded, and only once the probe says so', async () => {
        recordMaintenanceNotice(notice('verifying'))
        await nextTick()
        mockToast.mockClear()
        mockLock.mockResolvedValueOnce(probe('verifying'))
        await vi.advanceTimersByTimeAsync(VERIFYING_POLL_MS)
        expect(store.locked).toBe(true)
        expect(mockToast).not.toHaveBeenCalled()
        mockLock.mockResolvedValueOnce(probe(null))
        await vi.advanceTimersByTimeAsync(VERIFYING_POLL_MS)
        expect(store.locked).toBe(false)
        expect(reloads).toBe(0)
        expect(mockToast).toHaveBeenCalledTimes(1)
        expect(mockToast.mock.calls[0]?.[0]).toBe('The platform is back.')
    })

    it('verifying to rolling_back announces without reloading, and the release after it reloads', async () => {
        recordMaintenanceNotice(notice('verifying'))
        await nextTick()
        mockLock.mockResolvedValueOnce(probe('rolling_back'))
        await vi.advanceTimersByTimeAsync(VERIFYING_POLL_MS)
        expect(store.phase).toBe('rolling_back')
        expect(reloads).toBe(0)
        mockLock.mockResolvedValueOnce(probe(null))
        await vi.advanceTimersByTimeAsync(RELEASE_POLL_MS)
        expect(reloads).toBe(1)
    })

    it('a page opened during verifying does not reload on its first probe', async () => {
        mockLock.mockResolvedValueOnce(probe('verifying'))
        await store.init()
        expect(store.phase).toBe('verifying')
        expect(reloads).toBe(0)
    })

    it('an unlocked first probe changes nothing', async () => {
        mockLock.mockResolvedValueOnce(probe(null))
        await store.init()
        expect(store.locked).toBe(false)
        expect(mockToast).not.toHaveBeenCalled()
        await vi.advanceTimersByTimeAsync(RELEASE_POLL_MS * 3)
        expect(mockLock).toHaveBeenCalledTimes(1)
    })

    it('dismisses the phase toast when the phase changes and when the lock lifts', async () => {
        recordMaintenanceNotice(notice('verifying'))
        await nextTick()
        mockLock.mockResolvedValueOnce(probe('rolling_back'))
        await vi.advanceTimersByTimeAsync(VERIFYING_POLL_MS)
        expect(mockDismiss).toHaveBeenCalledTimes(1)
        mockLock.mockResolvedValueOnce(probe(null))
        await vi.advanceTimersByTimeAsync(RELEASE_POLL_MS)
        expect(mockDismiss).toHaveBeenCalledTimes(2)
    })

    it('stops polling once released', async () => {
        recordMaintenanceNotice(notice('verifying'))
        await nextTick()
        mockLock.mockResolvedValueOnce(probe(null))
        await vi.advanceTimersByTimeAsync(VERIFYING_POLL_MS)
        await vi.advanceTimersByTimeAsync(VERIFYING_POLL_MS * 3)
        expect(mockLock).toHaveBeenCalledTimes(1)
    })

    it('a stream of refusals does not keep pushing the probe back', async () => {
        recordMaintenanceNotice(notice('updating'))
        await nextTick()
        mockLock.mockResolvedValue(probe('updating'))
        for (let i = 0; i < 5; i += 1) {
            await vi.advanceTimersByTimeAsync(RELEASE_POLL_MS / 4)
            recordMaintenanceNotice(notice('updating'))
            await nextTick()
        }
        expect(mockLock).toHaveBeenCalled()
    })

    it('honours Retry-After, bounded', async () => {
        recordMaintenanceNotice(notice('updating'), 3600)
        await nextTick()
        mockLock.mockResolvedValue(probe('updating'))
        await vi.advanceTimersByTimeAsync(RELEASE_POLL_MS)
        expect(mockLock).not.toHaveBeenCalled()
        await vi.advanceTimersByTimeAsync(RETRY_AFTER_MAX_MS - RELEASE_POLL_MS)
        expect(mockLock).toHaveBeenCalledTimes(1)
    })

    it('pauses the probe while the tab is hidden and asks as soon as it is shown', async () => {
        recordMaintenanceNotice(notice('updating'))
        await nextTick()
        setHidden(true)
        mockLock.mockResolvedValue(probe('updating'))
        await vi.advanceTimersByTimeAsync(RELEASE_POLL_MS * 5)
        expect(mockLock).not.toHaveBeenCalled()
        setHidden(false)
        await vi.advanceTimersByTimeAsync(0)
        expect(mockLock).toHaveBeenCalledTimes(1)
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
        mockStatus.mockResolvedValueOnce({ installed_version: '0.1.2', lock: null } as never)
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

describe('reloadPage', () => {
    let order: string[]
    let originalLocation: Location
    beforeEach(() => {
        order = []
        originalLocation = window.location
        Object.defineProperty(window, 'location', {
            configurable: true,
            value: { reload: () => order.push('reload') },
        })
    })
    afterEach(() => {
        Object.defineProperty(window, 'location', { configurable: true, value: originalLocation })
        delete (window as unknown as { __EPICURRENTS__?: unknown }).__EPICURRENTS__
    })

    it('waives the viewer unload guard before reloading', () => {
        // Without the waiver an update reload raises the browser's unsaved-changes prompt in a
        // viewer tab, and a cancelled reload leaves the SPA running code the server has replaced.
        ;(window as unknown as { __EPICURRENTS__: unknown }).__EPICURRENTS__ = {
            APP: { allowUnload: () => order.push('allowUnload') },
        }
        reloadPage()
        expect(order).toStrictEqual(['allowUnload', 'reload'])
    })
    it('reloads when no viewer is loaded in the document', () => {
        reloadPage()
        expect(order).toStrictEqual(['reload'])
    })
})
