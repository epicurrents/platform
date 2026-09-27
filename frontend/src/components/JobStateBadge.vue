<script setup lang="ts">
/**
 * A maintenance job's state as a badge, coloured by what it asks of the reader.
 *
 * @package    epicurrents-platform
 */
import { computed } from 'vue'
import type { JobState } from '#api/maintenance'
import { jobStateLabel } from '#lib/maintenanceLabels'

const props = defineProps<{
    state: JobState
}>()

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

const label = computed(() => jobStateLabel(props.state))
</script>

<template>
    <wa-badge appearance="filled" :variant="variant">{{ label }}</wa-badge>
</template>
