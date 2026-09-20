<script setup lang="ts">
/**
 * A maintenance job's state as a badge, coloured by what it asks of the reader.
 *
 * @package    epicurrents-platform
 */
import { computed } from 'vue'
import { t } from '#i18n'
import type { JobState } from '#api/maintenance'

const SCOPE = 'JobStateBadge'

const props = defineProps<{
    state: JobState
}>()

const LABELS: Record<JobState, string> = {
    requested: 'Requested',
    accepted: 'Accepted',
    running: 'Running',
    awaiting_verification: 'Awaiting confirmation',
    succeeded: 'Succeeded',
    failed: 'Failed',
    cancelled: 'Cancelled',
    rolling_back: 'Rolling back',
    rolled_back: 'Rolled back',
    rollback_failed: 'Rollback failed',
}

const variant = computed(() => {
    switch (props.state) {
        case 'succeeded':
            return 'success'
        case 'failed':
        case 'rollback_failed':
            return 'danger'
        case 'awaiting_verification':
        case 'rolling_back':
            return 'warning'
        case 'running':
        case 'accepted':
            return 'brand'
        default:
            return 'neutral'
    }
})

const label = computed(() => t(LABELS[props.state] ?? props.state, SCOPE))
</script>

<template>
    <wa-badge appearance="filled" :variant="variant">{{ label }}</wa-badge>
</template>
