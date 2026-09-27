<script setup lang="ts">
/**
 * The acknowledgement a database restore asks for when accounts were erased after its snapshot.
 *
 * Restoring the database brings those accounts' data back until the
 * re-erasure that runs automatically afterwards, so the server refuses the
 * request (409, `erasures_since_snapshot`) until it is resent with
 * `acknowledge_erasures`. This says how many accounts, and holds the explicit
 * tick the resend waits for; the parent owns the reactive state.
 *
 * @package    epicurrents-platform
 */
import { t } from '#i18n'

const SCOPE = 'ErasureAcknowledgement'

defineProps<{
    /** How many erased accounts the restore brings back. */
    count: number
    /** The reactive object the checkbox writes into; key `acknowledged`. */
    state: { acknowledged: boolean }
}>()
</script>

<template>
    <wa-callout variant="danger">
        <strong>{{ t('{count} erased account(s) come back with this restore.', SCOPE, { count }) }}</strong>
        {{ t('Accounts were erased after the snapshot was taken. Restoring the database restores their personal data too, until the erasure is repeated automatically once the restore finishes.', SCOPE) }}
    </wa-callout>
    <wa-checkbox v-wa="[state, 'acknowledged']">
        {{ t('I understand that erased personal data is restored until it is erased again.', SCOPE) }}
    </wa-checkbox>
</template>
