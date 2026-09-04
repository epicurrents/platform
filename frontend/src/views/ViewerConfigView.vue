<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { t } from '#i18n'
import { getViewerConfig, updateViewerConfig } from '#api/viewerConfig'
import type { ViewerSettingsOverrides } from '#lib/viewerConfig'
import { showToast } from '#lib/toast'
import { useAuthStore } from '#stores/auth'
import ViewerConfigEditor from '#components/ViewerConfigEditor.vue'

const SCOPE = 'ViewerConfigView'
const authStore = useAuthStore()

const canSubmit = computed(() => authStore.isStaff)

const overrides = ref<ViewerSettingsOverrides>({})
const seedJson = ref('{}')
const effectiveJson = ref('{}')
const loading = ref(true)
const saving = ref(false)

function pretty (value: ViewerSettingsOverrides): string {
    return JSON.stringify(value, null, 2)
}

onMounted(async () => {
    try {
        const config = await getViewerConfig()
        seedJson.value = pretty(config.seed)
        effectiveJson.value = pretty(config.effective)
        overrides.value = config.overrides
    } catch {
        showToast(t('Failed to load viewer settings.', SCOPE), 'danger')
    } finally {
        loading.value = false
    }
})

async function onSave (next: ViewerSettingsOverrides) {
    saving.value = true
    try {
        const result = await updateViewerConfig(next)
        overrides.value = result.overrides
        effectiveJson.value = pretty(result.effective)
        showToast(t('Viewer settings saved.', SCOPE), 'neutral')
    } catch (err) {
        const detail = (err as { response?: { data?: { detail?: string } } }).response?.data?.detail
        showToast(detail ?? t('Failed to save viewer settings.', SCOPE), 'danger')
    } finally {
        saving.value = false
    }
}
</script>

<template>
    <main class="viewer-config-view">
        <wa-scroller orientation="vertical">
            <div class="viewer-config-view__scroll-wrap">
                <h1>{{ t('Viewer settings', SCOPE) }}</h1>
                <p class="viewer-config-view__intro">
                    {{ t('Override viewer defaults for this deployment without rebuilding the viewer. Each entry maps a dotted-path setting to a value, for example "eeg.defaultMontage": "lon". Overrides are layered on top of the project defaults and apply to every viewer session.', SCOPE) }}
                </p>

                <section class="viewer-config-section">
                    <h2>{{ t('Overrides', SCOPE) }}</h2>
                    <p class="viewer-config-section__hint">
                        {{ t('Editable overrides stored for this deployment. Leave empty to fall back to the project defaults.', SCOPE) }}
                    </p>
                    <ViewerConfigEditor
                        v-if="canSubmit"
                        :disabled="loading"
                        :overrides="overrides"
                        :saving="saving"
                        @save="onSave"
                    ></ViewerConfigEditor>
                </section>

                <section class="viewer-config-section">
                    <h2>{{ t('Project defaults', SCOPE) }}</h2>
                    <p class="viewer-config-section__hint">
                        {{ t('Read-only seed shipped with the active project. Edit the seed file in the project source to change these.', SCOPE) }}
                    </p>
                    <pre class="viewer-config-view__readonly">{{ seedJson }}</pre>
                </section>

                <section class="viewer-config-section">
                    <h2>{{ t('Effective configuration', SCOPE) }}</h2>
                    <p class="viewer-config-section__hint">
                        {{ t('Project defaults merged with the overrides — what the viewer applies. Updates when you save.', SCOPE) }}
                    </p>
                    <pre class="viewer-config-view__readonly">{{ effectiveJson }}</pre>
                </section>
            </div>
        </wa-scroller>
    </main>
</template>

<style scoped>
.viewer-config-view {
    /* Fill the remaining vertical space within .route-view-wrapper so the
     * internal wa-scroller resolves its height against a bounded box. */
    flex: 1;
    display: flex;
    flex-direction: column;
    min-height: 0;
    overflow: hidden;
    width: 100%;
    padding: 2rem 1rem;
}

.viewer-config-view__scroll-wrap {
    /* Centring lives here, inside the scroller, rather than on the host: the
     * scroller has to span the full width so its scrollbar rides the viewport
     * edge instead of the content column. Flex-1 wrap for wa-scroller — see
     * AGENTS.md → WebAwesome shadow-DOM layout gotchas. */
    flex: 1;
    display: flex;
    flex-direction: column;
    margin: 0 auto;
    max-width: 720px;
    min-height: 0;
    overflow: hidden;
    width: 100%;
}

.viewer-config-view h1 {
    margin: 0 0 0.5rem;
}

.viewer-config-view__intro {
    color: var(--wa-color-text-quiet);
    margin: 0 0 2rem;
}

.viewer-config-section {
    display: flex;
    flex-direction: column;
    gap: 0.75rem;
    margin-bottom: 3rem;
}

.viewer-config-section h2 {
    margin: 0;
}

.viewer-config-section__hint {
    color: var(--wa-color-text-quiet);
    font-size: 0.9rem;
    margin: 0;
}

.viewer-config-view__readonly {
    background: var(--wa-color-surface-lowered);
    border: 1px solid var(--wa-color-surface-border);
    border-radius: var(--wa-border-radius-m, 0.25rem);
    color: var(--wa-color-text-normal);
    font-family: var(--wa-font-family-code, monospace);
    font-size: 0.85rem;
    margin: 0;
    overflow-x: auto;
    padding: 0.75rem 1rem;
    white-space: pre;
}
</style>
