<script setup lang="ts">
import { t } from '#i18n'
import { recordingName, type Recording } from '#api/recordings'

const SCOPE = 'NeedsAttentionRow'

defineProps<{
    recording: Recording
}>()

const emit = defineEmits<{
    delete: []
    reupload: []
}>()
</script>

<template>
    <div class="attention-row">
        <div class="attention-row__head">
            <wa-badge variant="danger">
                <wa-icon name="triangle-exclamation"></wa-icon>
                {{ t('Failed', SCOPE) }}
            </wa-badge>
            <span class="attention-row__name">{{ recordingName(recording) }}</span>
            <div class="attention-row__actions">
                <wa-button
                    appearance="plain"
                    size="s"
                    @click="emit('reupload')"
                >
                    <wa-icon name="cloud-arrow-up" slot="start"></wa-icon>
                    {{ t('Re-upload', SCOPE) }}
                </wa-button>
                <wa-button
                    appearance="plain"
                    size="s"
                    variant="danger"
                    @click="emit('delete')"
                >
                    <wa-icon name="trash" slot="start"></wa-icon>
                    {{ t('Delete', SCOPE) }}
                </wa-button>
            </div>
        </div>
        <p v-if="recording.processing_error" class="attention-row__error">
            {{ recording.processing_error }}
        </p>
    </div>
</template>

<style scoped>
.attention-row {
    border: var(--wa-border-width-s) solid var(--wa-color-danger-border-quiet);
    border-radius: var(--wa-border-radius-m);
    display: flex;
    flex-direction: column;
    gap: 0.35rem;
    padding: 0.6rem 0.75rem;
}

.attention-row__head {
    align-items: center;
    display: flex;
    gap: 0.6rem;
}

.attention-row__name {
    flex: 1;
    font-weight: 500;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}

.attention-row__actions {
    display: flex;
    flex-shrink: 0;
    gap: 0.25rem;
}

.attention-row__error {
    color: var(--wa-color-text-quiet);
    font-size: var(--wa-font-size-s);
    margin: 0;
}
</style>
