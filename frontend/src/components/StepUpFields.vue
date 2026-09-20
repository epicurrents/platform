<script setup lang="ts">
/**
 * The credential inputs a step-up confirmation asks for.
 *
 * Which inputs appear follows the method the status endpoint reports for
 * the caller: the password for an account that has one, the second-factor
 * code when one is enrolled, and for an externally authenticated account
 * the code alone. Rendered inside the dialog of whatever action needs the
 * confirmation; the parent owns the reactive credentials object and sends
 * it with the request.
 *
 * @package    epicurrents-platform
 */
import { computed } from 'vue'
import { t } from '#i18n'
import type { StepUpInfo } from '#api/maintenance'

const SCOPE = 'StepUpFields'

const props = defineProps<{
    /** The reactive object the inputs write into; keys `password` and `totp_code`. */
    credentials: { password: string, totp_code: string }
    /** How the caller confirms, from the maintenance status. */
    stepUp: StepUpInfo
    /** Ask for the password only, for an action the server confirms without the second factor. */
    passwordOnly?: boolean
}>()

const asksPassword = computed(() => props.stepUp.method !== null && props.stepUp.method.includes('password'))
/** The code is waived with `passwordOnly` unless it is the account's only credential, which the server also insists on. */
const asksCode = computed(() => {
    const method = props.stepUp.method
    if (method === null || !method.includes('totp')) {
        return false
    }
    return !props.passwordOnly || method === 'totp'
})
</script>

<template>
    <wa-callout v-if="!stepUp.available" variant="warning">
        {{ stepUp.reason ?? t('This account cannot confirm sensitive actions.', SCOPE) }}
    </wa-callout>
    <template v-else>
        <p class="stepup-hint">
            {{ t('Confirm it is you before continuing.', SCOPE) }}
        </p>
        <wa-input v-if="asksPassword"
            autocomplete="current-password"
            :label="t('Password', SCOPE)"
            password-toggle
            required
            type="password"
            v-wa="[credentials, 'password']"
        ></wa-input>
        <wa-input v-if="asksCode"
            autocomplete="one-time-code"
            inputmode="numeric"
            :label="t('Authenticator or recovery code', SCOPE)"
            required
            v-wa="[credentials, 'totp_code']"
        ></wa-input>
    </template>
</template>

<style scoped>
.stepup-hint {
    color: var(--wa-color-text-quiet);
    font-size: var(--wa-font-size-s);
    margin: 0;
}
</style>
