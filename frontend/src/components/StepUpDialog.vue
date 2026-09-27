<script setup lang="ts">
/**
 * The shared step-up prompt, mounted once in `App.vue`.
 *
 * Renders whatever `requestStepUp` / `withStepUp` asked for, with the
 * credential inputs `StepUpFields` chooses for the signed-in account. The
 * confirm button stays disabled for an account that has nothing to confirm
 * with, where `StepUpFields` says why instead.
 *
 * @package    epicurrents-platform
 */
import StepUpFields from '#components/StepUpFields.vue'
import { useStepUpPrompt } from '#composables/useStepUpPrompt'
import { t } from '#i18n'
import { useAuthStore } from '#stores/auth'

const SCOPE = 'StepUpDialog'

const authStore = useAuthStore()
const { state, confirm, cancel, onHide } = useStepUpPrompt()
</script>

<template>
    <wa-dialog :label="state.title" :open="state.open" @wa-hide.self="onHide">
        <form class="stepup-form" @submit.prevent="confirm">
            <wa-callout v-if="state.error" variant="danger">
                {{ state.error }}
            </wa-callout>
            <p v-if="state.message" class="stepup-message">
                {{ state.message }}
            </p>
            <StepUpFields :credentials="state.credentials" :step-up="authStore.stepUp" />
        </form>
        <div slot="footer" class="form-actions">
            <wa-button
                appearance="filled-outlined"
                :disabled="state.busy"
                variant="neutral"
                @click="cancel"
            >
                {{ t('Cancel', SCOPE) }}
            </wa-button>
            <wa-button
                appearance="filled-outlined"
                :disabled="!authStore.stepUp.available"
                :loading="state.busy"
                variant="brand"
                @click="confirm"
            >
                {{ t('Confirm', SCOPE) }}
            </wa-button>
        </div>
    </wa-dialog>
</template>

<style scoped>
.stepup-form {
    display: flex;
    flex-direction: column;
    gap: var(--wa-space-s);
}

.stepup-message {
    margin: 0;
}
</style>
