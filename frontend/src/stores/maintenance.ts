/**
 * Maintenance store: the lock the platform is under, and whether the feature exists here.
 *
 * Two things live here. The lock, with what follows from it: one toast per
 * phase, dismissed when the phase moves on; a poll of the public lock probe
 * (`GET /api/v1/maintenance/lock`) while a lock is known; and a page reload
 * when the platform comes back on new code. And the feature gate: whether
 * `/api/v1/maintenance/status` answers at all, which decides whether the
 * Maintenance tab renders.
 *
 * The lock is learnt three ways — a maintenance 503 the HTTP interceptor
 * records in `lib/maintenanceLock`, the `lock` of a status read, and the probe
 * itself, asked once at start-up — but only the probe decides that it is
 * gone. Any other request is the wrong witness: a superuser's reads pass
 * while an update runs, so a 200 from them says nothing about the lock.
 *
 * The reload is deliberate. Leaving `updating` or `rolling_back` — for
 * `verifying` or for no lock at all — means the server is running a different
 * release from this bundle; a superuser testing the new API through stale
 * code would verify the wrong thing. A lifted `verifying` lock, and the step
 * from `verifying` to `rolling_back`, change nothing yet and only announce.
 * `window.location.reload()` is wrapped so tests can stub it.
 *
 * @package    epicurrents-platform
 */

import { defineStore } from 'pinia'
import { computed, onScopeDispose, ref, watch } from 'vue'
import {
    fetchLock,
    fetchMaintenanceStatus,
    isFeatureDisabled,
    type LockProbe,
    type MaintenanceStatus,
} from '#api/maintenance'
import { t } from '#i18n'
import { clearMaintenanceNotice, maintenanceLock, type MaintenanceNotice } from '#lib/maintenanceLock'
import { dismissToast, showToast } from '#lib/toast'

const SCOPE = 'MaintenanceStore'

/** How often the probe is asked while the platform is suspended, in milliseconds. */
export const RELEASE_POLL_MS = 10_000
/** How often it is asked while an update awaits confirmation, which can last half an hour and reloads nothing. */
export const VERIFYING_POLL_MS = 30_000
/** Bounds on a delay taken from a refusal's `Retry-After`, which derives from the expected end and may be far off. */
export const RETRY_AFTER_MIN_MS = 5_000
export const RETRY_AFTER_MAX_MS = 60_000

/** The phases during which the server's code is not the code this bundle was built against, or is about to change. */
const CODE_CHANGING_PHASES = new Set(['updating', 'rolling_back'])

/** Reload the document; a seam for tests, which cannot navigate jsdom. */
export function reloadPage(): void {
    // The viewer guards the document against an unload that would end a review session. An update
    // reload is not the accident that guard exists for, and a prompt here would leave the SPA
    // running code the server has already replaced.
    window.__EPICURRENTS__?.APP?.allowUnload?.()
    window.location.reload()
}

/**
 * Whether moving from `previous` to `next` (null: no lock) puts new code behind this bundle. Only leaving a
 * code-changing phase for `verifying` or for the release does; `updating` to `rolling_back` has not served yet.
 */
export function codeChanged(previous: string | null, next: string | null): boolean {
    if (previous === null || !CODE_CHANGING_PHASES.has(previous)) {
        return false
    }
    return next === null || next === 'verifying'
}

function noticeFromProbe(probe: LockProbe): MaintenanceNotice {
    return {
        detail: 'maintenance',
        phase: probe.phase ?? 'updating',
        since: probe.since,
        expected_until: probe.expected_until,
        message: probe.message ?? '',
    }
}

export const useMaintenanceStore = defineStore('maintenance', () => {
    /** The lock as last reported by any of the three sources, or `null` when none holds. */
    const lock = ref<MaintenanceNotice | null>(null)
    const notice = computed<MaintenanceNotice | null>(() => lock.value)
    const locked = computed(() => lock.value !== null)
    const phase = computed(() => lock.value?.phase ?? null)
    /** Whether the lock forbids every write for a non-superuser: `updating` and `rolling_back`. */
    const suspended = computed(() => phase.value !== null && phase.value !== 'verifying')
    /** What the banner shows. */
    const banner = computed<MaintenanceNotice | null>(() => lock.value)

    /** `null` until asked; the tab renders only on `true`. */
    const featureEnabled = ref<boolean | null>(null)
    const status = ref<MaintenanceStatus | null>(null)

    let pollTimer: ReturnType<typeof setTimeout> | undefined
    /** Set when a poll fell due while the tab was hidden; the poll runs when it is shown again. */
    let deferred = false
    /** Guards against an older probe answer landing after a newer one. */
    let probeSeq = 0
    let lastPhase: string | null = null
    let phaseToast: number | null = null
    let reload: () => void = reloadPage

    /** Replace the reload behaviour; tests use this to observe it. */
    function setReloadHandler(handler: () => void) {
        reload = handler
    }

    function dismissPhaseToast() {
        if (phaseToast !== null) {
            dismissToast(phaseToast)
            phaseToast = null
        }
    }

    function stopPolling() {
        clearTimeout(pollTimer)
        pollTimer = undefined
        deferred = false
    }

    function nextDelay(): number {
        const retryAfter = maintenanceLock.retryAfter
        if (retryAfter !== null && lastPhase !== null && CODE_CHANGING_PHASES.has(lastPhase)) {
            return Math.min(RETRY_AFTER_MAX_MS, Math.max(RETRY_AFTER_MIN_MS, retryAfter * 1000))
        }
        return lastPhase === 'verifying' ? VERIFYING_POLL_MS : RELEASE_POLL_MS
    }

    function isHidden() {
        return typeof document !== 'undefined' && document.visibilityState === 'hidden'
    }

    /** Schedule the next probe unless one is already due, so a stream of refusals cannot keep pushing it back. */
    function ensurePolling() {
        if (pollTimer !== undefined || deferred) {
            return
        }
        schedulePoll()
    }

    function schedulePoll() {
        clearTimeout(pollTimer)
        pollTimer = setTimeout(() => {
            pollTimer = undefined
            if (isHidden()) {
                deferred = true
                return
            }
            void pollLock()
        }, nextDelay())
    }

    /** Ask the probe now. A failure — the containers restarting — asks again later. */
    async function pollLock() {
        const seq = ++probeSeq
        let probe: LockProbe
        try {
            probe = await fetchLock()
        } catch {
            if (seq === probeSeq && lastPhase !== null) {
                schedulePoll()
            }
            return
        }
        if (seq !== probeSeq) {
            return
        }
        applyProbe(probe)
    }

    function applyProbe(probe: LockProbe) {
        if (!probe.locked) {
            if (lastPhase !== null || lock.value !== null) {
                released()
            }
            return
        }
        observe(noticeFromProbe(probe))
        schedulePoll()
    }

    /**
     * Adopt what a source says the lock is. One toast per phase; the previous
     * phase's toast goes when the phase changes. Returns false when the change
     * reloaded the page.
     */
    function observe(current: MaintenanceNotice): boolean {
        lock.value = current
        if (current.phase === lastPhase) {
            return true
        }
        const previous = lastPhase
        lastPhase = current.phase
        dismissPhaseToast()
        if (codeChanged(previous, current.phase)) {
            stopPolling()
            reload()
            return false
        }
        phaseToast = showToast([t('Maintenance', SCOPE), current.message], 'brand', 0)
        return true
    }

    /**
     * The probe says the lock is gone. A lock that was changing code means
     * this bundle is stale, so reload; a lifted `verifying` lock is a
     * confirmed update the page already runs against.
     */
    function released() {
        const wasPhase = lastPhase
        stopPolling()
        clearMaintenanceNotice()
        dismissPhaseToast()
        lock.value = null
        lastPhase = null
        if (codeChanged(wasPhase, null)) {
            reload()
            return
        }
        if (wasPhase !== null) {
            showToast(t('The platform is back.', SCOPE), 'brand')
        }
    }

    watch(
        () => maintenanceLock.sequence,
        () => {
            const current = maintenanceLock.notice
            if (current === null) {
                return
            }
            if (observe(current)) {
                ensurePolling()
            }
        },
    )

    /** A status read carries the lock too; a superuser never receives a 503 while an update awaits confirmation. */
    function observeStatus(fresh: MaintenanceStatus | null) {
        const statusLock = fresh?.lock ?? null
        if (statusLock !== null) {
            const current: MaintenanceNotice = {
                detail: 'maintenance',
                phase: statusLock.phase,
                since: statusLock.since,
                expected_until: statusLock.expected_until,
                message: statusLock.message,
            }
            if (observe(current)) {
                ensurePolling()
            }
        } else if (fresh !== null && lastPhase !== null) {
            // The flag is gone as far as the status saw; let the probe confirm it.
            void pollLock()
        }
    }

    function onVisibilityChange() {
        if (!isHidden() && deferred) {
            deferred = false
            void pollLock()
        }
    }

    if (typeof document !== 'undefined') {
        document.addEventListener('visibilitychange', onVisibilityChange)
    }

    onScopeDispose(() => {
        stopPolling()
        probeSeq += 1
        if (typeof document !== 'undefined') {
            document.removeEventListener('visibilitychange', onVisibilityChange)
        }
    })

    /** Ask the probe once, so a page opened during a lock shows it before any request is refused. */
    async function init() {
        await pollLock()
    }

    /**
     * Find out whether the deployment has the feature switched on, once.
     * A 404 is the gate, not an error; anything else leaves the answer
     * unknown so the next caller asks again.
     */
    async function probeFeature(): Promise<boolean> {
        if (featureEnabled.value !== null) {
            return featureEnabled.value
        }
        try {
            status.value = await fetchMaintenanceStatus()
            featureEnabled.value = true
            observeStatus(status.value)
        } catch (err) {
            if (isFeatureDisabled(err)) {
                featureEnabled.value = false
            }
        }
        return featureEnabled.value === true
    }

    /** Re-read the status; the tab calls this on load and after each job it touches. */
    async function refreshStatus(): Promise<MaintenanceStatus | null> {
        try {
            status.value = await fetchMaintenanceStatus()
            featureEnabled.value = true
            observeStatus(status.value)
        } catch (err) {
            if (isFeatureDisabled(err)) {
                featureEnabled.value = false
                status.value = null
            }
        }
        return status.value
    }

    return {
        notice,
        banner,
        locked,
        phase,
        suspended,
        featureEnabled,
        status,
        init,
        pollLock,
        probeFeature,
        refreshStatus,
        released,
        setReloadHandler,
    }
})
