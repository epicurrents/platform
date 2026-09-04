import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { fetchHealth, type DeploymentMode } from '#api/health'

/**
 * Deployment-posture store backed by the backend's `/api/v1/health`
 * response. Used by App.vue to render the dev-mode banner at the bottom
 * of the page when the backend reports mode=development.
 *
 * Init is idempotent; subsequent calls are no-ops. The fetch is
 * fire-and-forget — if the backend is unreachable at app boot, the
 * store falls back to mode='unset' and the banner stays hidden.
 */
export const useDeploymentStore = defineStore('deployment', () => {
    const mode = ref<DeploymentMode>('unset')
    const debug = ref<boolean>(false)
    const initialized = ref(false)

    const isDevelopmentMode = computed(() => mode.value === 'development')

    async function init() {
        if (initialized.value) {
            return
        }
        try {
            const info = await fetchHealth()
            mode.value = info.mode
            debug.value = info.debug
        } catch {
            mode.value = 'unset'
            debug.value = false
        } finally {
            initialized.value = true
        }
    }

    return { mode, debug, isDevelopmentMode, init }
})
