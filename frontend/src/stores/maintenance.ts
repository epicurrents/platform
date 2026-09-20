/**
 * Maintenance store: the lock the platform is under, and whether the feature exists here.
 *
 * Two things live here. The lock — fed by the HTTP interceptor through
 * `lib/maintenanceLock` — with what follows from it: one toast per phase
 * change, a poll of `/api/v1/user/me` every ten seconds to notice release,
 * and a page reload when the platform comes back on new code. And the
 * feature gate: whether `/api/v1/maintenance/status` answers at all, which
 * decides whether the Maintenance tab renders.
 *
 * The reload is deliberate. Once a lock lifts, or moves from `updating` to
 * `verifying`, the server is running the new release and this bundle is the
 * old one; a superuser testing the new API through stale code would verify
 * the wrong thing. `window.location.reload()` is wrapped so tests can stub it.
 *
 * @package    epicurrents-platform
 */

import { defineStore } from 'pinia'
import { computed, ref, watch } from 'vue'
import { fetchMaintenanceStatus, isFeatureDisabled, type MaintenanceStatus } from '#api/maintenance'
import { fetchMe } from '#api/user'
import { t } from '#i18n'
import { clearMaintenanceNotice, maintenanceLock, type MaintenanceNotice } from '#lib/maintenanceLock'
import { showToast } from '#lib/toast'

const SCOPE = 'MaintenanceStore'

/** How often a locked SPA asks whether the platform is back, in milliseconds. */
export const RELEASE_POLL_MS = 10_000

/** Reload the document; a seam for tests, which cannot navigate jsdom. */
export function reloadPage(): void {
    window.location.reload()
}

export const useMaintenanceStore = defineStore('maintenance', () => {
    const notice = computed<MaintenanceNotice | null>(() => maintenanceLock.notice)
    const locked = computed(() => notice.value !== null)
    const phase = computed(() => notice.value?.phase ?? null)
    /** Whether the lock forbids every write for a non-superuser: `updating` and `rolling_back`. */
    const suspended = computed(() => phase.value !== null && phase.value !== 'verifying')

    /** `null` until asked; the tab renders only on `true`. */
    const featureEnabled = ref<boolean | null>(null)
    const status = ref<MaintenanceStatus | null>(null)

    /**
     * What the banner shows: the lock a refusal reported, or failing that the
     * one the status endpoint reports. A superuser is exempt from the lock
     * while an update awaits confirmation and so never receives a 503; the
     * status is how their banner learns of it.
     */
    const banner = computed<MaintenanceNotice | null>(() => {
        if (notice.value !== null) {
            return notice.value
        }
        const lock = status.value?.lock
        if (!lock) {
            return null
        }
        return { detail: 'maintenance', phase: lock.phase, since: lock.since, expected_until: lock.expected_until, message: lock.message }
    })

    let pollTimer: ReturnType<typeof setTimeout> | undefined
    let lastAnnouncedPhase: string | null = null
    let reload: () => void = reloadPage

    /** Replace the reload behaviour; tests use this to observe it. */
    function setReloadHandler(handler: () => void) {
        reload = handler
    }

    function stopPolling() {
        clearTimeout(pollTimer)
        pollTimer = undefined
    }

    /**
     * Ask the server whether the lock still holds. `/me` answers 200 to anyone
     * once the platform serves again; while locked it is one more 503 the
     * interceptor records, so the notice refreshes itself.
     */
    async function pollForRelease() {
        try {
            await fetchMe()
            released()
        } catch {
            // Still locked, or briefly unreachable while the containers restart;
            // either way, ask again.
            schedulePoll()
        }
    }

    function schedulePoll() {
        stopPolling()
        pollTimer = setTimeout(pollForRelease, RELEASE_POLL_MS)
    }

    /**
     * The platform serves again. A lock that was anything but `verifying`
     * means the code changed underneath this bundle, so reload; a lifted
     * `verifying` lock is a confirmed update the page already runs against.
     */
    function released() {
        const wasPhase = lastAnnouncedPhase
        stopPolling()
        clearMaintenanceNotice()
        lastAnnouncedPhase = null
        if (wasPhase !== null && wasPhase !== 'verifying') {
            reload()
            return
        }
        showToast(t('The platform is back.', SCOPE), 'brand')
    }

    function announce(current: MaintenanceNotice) {
        if (current.phase === lastAnnouncedPhase) {
            return
        }
        const previous = lastAnnouncedPhase
        lastAnnouncedPhase = current.phase
        // From updating to verifying: the new release is serving. Reload so a
        // superuser confirms the new code with the new bundle, not through this one.
        if (previous !== null && previous !== 'verifying' && current.phase === 'verifying') {
            reload()
            return
        }
        showToast([t('Maintenance', SCOPE), current.message], 'brand', 0)
    }

    watch(
        () => maintenanceLock.sequence,
        () => {
            const current = maintenanceLock.notice
            if (current === null) {
                return
            }
            announce(current)
            schedulePoll()
        },
    )

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
        probeFeature,
        refreshStatus,
        released,
        setReloadHandler,
    }
})
