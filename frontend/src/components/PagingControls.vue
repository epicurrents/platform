<script setup lang="ts">
/**
 * Previous and next controls for an offset-paged listing whose endpoint answers no total.
 *
 * The platform's list endpoints answer a bare page and no count, so paging can say "there is more" but never
 * "N of M": the host enables next on a full page and previous above offset zero, which is the whole signal
 * available. The controls render nothing while neither applies, which is every listing that fits in one page.
 *
 * @package    epicurrents-platform
 */
import { t } from '#i18n'

const SCOPE = 'PagingControls'

defineProps<{
    /** Whether a page may follow the current one; a full page is the usual evidence. */
    hasNext: boolean
    /** Whether a page precedes the current one. */
    hasPrev: boolean
}>()

const emit = defineEmits<{ next: [], prev: [] }>()

function next () {
    emit('next')
}

function prev () {
    emit('prev')
}
</script>

<template>
    <div v-if="hasPrev || hasNext" class="paging">
        <wa-button
            appearance="plain"
            :disabled="!hasPrev"
            size="s"
            @click="prev"
        >
            <wa-icon name="arrow-left" slot="start"></wa-icon>
            {{ t('Previous', SCOPE) }}
        </wa-button>
        <wa-button
            appearance="plain"
            :disabled="!hasNext"
            size="s"
            @click="next"
        >
            {{ t('Next', SCOPE) }}
            <wa-icon name="arrow-right" slot="end"></wa-icon>
        </wa-button>
    </div>
</template>

<style scoped>
.paging {
    display: flex;
    justify-content: space-between;
    margin-top: var(--wa-space-m);
}
</style>
