<script setup lang="ts">
/**
 * The recording details drawer: the file and recording facts of one recording, shared by the home page and the
 * dataset browser.
 *
 * The host owns which recording is shown and when. It passes `recording` as null while the details are still
 * loading, which shows a spinner under the label rather than an empty list. Anything a host adds about the
 * recording goes in the default slot below the facts, and its actions in the `footer` slot.
 *
 * @package    epicurrents-platform
 */
import { t } from '#i18n'
import { formatDate } from '#lib/datetime'
import { formatDuration, formatSize } from '#lib/format'
import type { Recording } from '#api/recordings'

const SCOPE = 'RecordingDetailsDrawer'

defineProps<{
    /** The drawer heading, normally the recording's name. */
    label: string
    /** Whether the drawer is open. */
    open: boolean
    /** The recording shown, or null while its details load. */
    recording: Recording | null
}>()

const emit = defineEmits<{ close: [] }>()

function onHide () {
    emit('close')
}
</script>

<template>
    <wa-drawer
        :label="label"
        light-dismiss
        :open="open"
        @wa-hide.self="onHide"
    >
        <div class="wa-stack">
            <p class="wa-caption-s details-text">{{ t('File and recording details', SCOPE) }}</p>
            <wa-divider></wa-divider>
            <wa-spinner v-if="!recording" class="details-loading"></wa-spinner>
            <dl v-else class="wa-stack details-text">
                <div v-if="recording.modality" class="wa-flank details-flank">
                    <dt>{{ t('Modality', SCOPE) }}</dt>
                    <dd>{{ recording.modality.toUpperCase() }}</dd>
                </div>
                <div v-if="recording.meta" class="wa-flank details-flank">
                    <dt>{{ t('Format', SCOPE) }}</dt>
                    <dd>{{ recording.meta.format.toUpperCase() }}</dd>
                </div>
                <div v-if="recording.meta" class="wa-flank details-flank">
                    <dt>{{ t('Duration', SCOPE) }}</dt>
                    <dd>{{ formatDuration(recording.meta.duration) }}</dd>
                </div>
                <div v-if="recording.meta" class="wa-flank details-flank">
                    <dt>{{ t('Channels', SCOPE) }}</dt>
                    <dd>{{ recording.meta.signal_count }}</dd>
                </div>
                <div v-if="recording.meta?.discontinuous" class="wa-flank details-flank">
                    <dt>{{ t('Discontinuous', SCOPE) }}</dt>
                    <dd>{{ t('Yes', SCOPE) }}</dd>
                </div>
                <div class="wa-flank details-flank">
                    <dt>{{ t('File size', SCOPE) }}</dt>
                    <dd>{{ formatSize(recording.file_size) }}</dd>
                </div>
                <div class="wa-flank details-flank">
                    <dt>{{ t('Added', SCOPE) }}</dt>
                    <dd>{{ formatDate(recording.created_at) }}</dd>
                </div>
                <div v-if="recording.stored_hash" class="wa-flank details-flank">
                    <dt>{{ t('File hash', SCOPE) }}</dt>
                    <dd class="details-hash">{{ recording.stored_hash }}</dd>
                </div>
                <div v-if="recording.public_source" class="wa-flank details-flank">
                    <dt>{{ t('Published source', SCOPE) }}</dt>
                    <dd class="details-hash">{{ recording.public_source }}</dd>
                </div>
            </dl>
            <slot></slot>
        </div>
        <div v-if="$slots.footer" slot="footer" class="form-actions">
            <slot name="footer"></slot>
        </div>
    </wa-drawer>
</template>

<style scoped>
.details-text {
    margin: 0;
}

.details-flank {
    --flank-size: 14ch;
}

.details-loading {
    align-self: center;
}
</style>
