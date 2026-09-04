<script setup lang="ts">
import { reactive, ref, watch } from 'vue'
import { t } from '#i18n'
import { showToast } from '#lib/toast'
import { updateRecording, type Recording } from '#api/recordings'

const SCOPE = 'EditRecordingDialog'

// The dialog is open whenever a recording is supplied; the parent clears it
// (via `close`) to dismiss. Reusing one dialog across views keeps the editable
// field set in a single place as more recording properties become editable.
const props = defineProps<{
    recording: Recording | null
}>()

const emit = defineEmits<{
    (e: 'updated', recording: Recording): void
    (e: 'close'): void
}>()

const input = reactive({ name: '', modality: '' })
const loading = ref(false)
const error = ref<string | null>(null)

watch(
    () => props.recording,
    (rec) => {
        if (!rec) {
            return
        }
        // Pre-fill with the current custom label only — never the author-private
        // original_name, so saving unchanged can't promote a PHI-bearing filename
        // into the grantee-visible display name.
        input.name = rec.has_custom_name ? rec.display_name : ''
        input.modality = rec.modality
        error.value = null
    },
)

function close () {
    emit('close')
}

async function submit () {
    if (!props.recording) {
        return
    }
    loading.value = true
    error.value = null
    try {
        const updated = await updateRecording(props.recording.hash, {
            display_name: input.name,
            modality: input.modality,
        })
        emit('updated', updated)
        showToast(t('Recording updated.', SCOPE), 'success')
        emit('close')
    } catch {
        error.value = t('Failed to update recording. Please try again.', SCOPE)
    } finally {
        loading.value = false
    }
}
</script>

<template>
    <wa-dialog
        :label="t('Edit recording', SCOPE)"
        :open="!!recording"
        @wa-hide.self="close"
    >
        <div class="dialog-form">
            <wa-callout v-if="error" variant="danger">{{ error }}</wa-callout>
            <wa-input
                :disabled="loading"
                :hint="t('Shown to anyone you share this recording with. Leave blank to keep it private.', SCOPE)"
                :label="t('Display name', SCOPE)"
                :placeholder="recording?.original_name ?? ''"
                size="s"
                type="text"
                v-wa="[input, 'name']"
            ></wa-input>
            <wa-select
                :disabled="loading"
                :label="t('Modality', SCOPE)"
                size="s"
                v-wa="[input, 'modality']"
            >
                <wa-option value="eeg">{{ t('EEG', SCOPE) }}</wa-option>
                <wa-option value="emg">{{ t('EMG', SCOPE) }}</wa-option>
                <wa-option value="ecg">{{ t('ECG', SCOPE) }}</wa-option>
                <wa-option value="eog">{{ t('EOG', SCOPE) }}</wa-option>
                <wa-option value="meg">{{ t('MEG', SCOPE) }}</wa-option>
                <wa-option value="ecog">{{ t('ECoG', SCOPE) }}</wa-option>
                <wa-option value="seeg">{{ t('sEEG', SCOPE) }}</wa-option>
                <wa-option value="acc">{{ t('Accelerometry', SCOPE) }}</wa-option>
            </wa-select>
        </div>
        <div slot="footer" class="form-actions">
            <wa-button
                appearance="filled-outlined"
                :disabled="loading"
                variant="neutral"
                @click="close"
            >
                {{ t('Cancel', SCOPE) }}
            </wa-button>
            <wa-button
                appearance="filled-outlined"
                :loading="loading"
                variant="brand"
                @click="submit"
            >
                {{ t('Save', SCOPE) }}
            </wa-button>
        </div>
    </wa-dialog>
</template>

<style scoped>
.dialog-form {
    display: flex;
    flex-direction: column;
    gap: 1rem;
}

.form-actions {
    display: flex;
    gap: 0.5rem;
    justify-content: flex-end;
}
</style>
