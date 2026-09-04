<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import { t } from '#i18n'
import { listMediaFiles, uploadMedia, type MediaFileSummary } from '#api/media'
import { showToast } from '#lib/toast'

const SCOPE = 'MediaPickerDialog'

const props = defineProps<{
    /** When true, the dialog is open. The parent owns this state. */
    open: boolean
    /**
     * Content hashes already in the parent container (collection / dataset).
     * Used to exclude rows the user can't add again from the picker; the
     * comparison is order-independent. Empty array = show every readable
     * media file.
     */
    excludeHashes: string[]
}>()

const emit = defineEmits<{
    /** User dismissed the dialog without confirming a selection. */
    'close': []
    /** User clicked Add — parent posts these hashes to the items endpoint. */
    'add': [hashes: string[]]
}>()

const available = ref<MediaFileSummary[]>([])
const selected = ref<string[]>([])
const loading = ref(false)
const errorText = ref<string | null>(null)

const uploading = ref(false)
const uploadProgress = ref(0)
const fileInput = ref<HTMLInputElement | null>(null)
// A chosen file awaiting label confirmation before upload. Staging it lets the
// author review (and redact) the grantee-visible label before it is sent — a
// filename can carry PHI, so it is never auto-applied as the public label.
const pendingFile = ref<File | null>(null)
const labelForm = reactive({ label: '' })

/** Drop the trailing file extension to seed a human-readable default label. */
function stripExtension (name: string): string {
    return name.replace(/\.[^.]+$/, '')
}

/** Re-fetch the picker list and drop rows that already sit in the parent. */
async function refreshAvailable () {
    try {
        const rows = await listMediaFiles({ limit: 200 })
        const seen = new Set(props.excludeHashes)
        available.value = rows.filter(m => !seen.has(m.content_hash))
    } catch {
        errorText.value = t('Could not load media files. Please try again.', SCOPE)
    }
}

/** Reload + reset on every open so the picker reflects the latest server state. */
watch(() => props.open, async (next) => {
    if (!next) return
    selected.value = []
    errorText.value = null
    cancelUpload()
    await refreshAvailable()
})

function pickUpload () {
    fileInput.value?.click()
}

function onFileChosen (event: Event) {
    const target = event.target as HTMLInputElement
    const file = target.files?.[0]
    // Reset the input so picking the same file twice in a row still triggers
    // a change event; otherwise a retry after a failed upload silently misses.
    target.value = ''
    if (!file) return
    // Stage the file and seed the label with the filename stem; the author
    // confirms (and can edit) it before the upload sends a grantee-visible label.
    pendingFile.value = file
    labelForm.label = stripExtension(file.name)
    errorText.value = null
}

async function confirmUpload () {
    const file = pendingFile.value
    if (!file) return
    uploading.value = true
    uploadProgress.value = 0
    errorText.value = null
    try {
        const created = await uploadMedia(file, {
            displayName: labelForm.label.trim() || undefined,
            onProgress: (f) => { uploadProgress.value = f },
        })
        // Newly uploaded row jumps to the top and is auto-checked so the
        // user can confirm with one click. listMediaFiles isn't re-run —
        // the response already carries the full row.
        available.value = [
            {
                content_hash: created.content_hash,
                media_type: created.media_type,
                display_name: created.display_name,
                file_extension: created.file_extension,
                file_size: created.file_size,
                is_supported: created.is_supported,
                attached_to: created.attached_to,
                time_offset: created.time_offset,
                created_at: created.created_at,
                modified_at: created.modified_at,
            },
            ...available.value,
        ]
        selected.value = [created.content_hash, ...selected.value]
        showToast(t('{name} uploaded.', SCOPE, { name: file.name }), 'success')
        pendingFile.value = null
        labelForm.label = ''
    } catch (e: unknown) {
        const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail
        errorText.value = detail ?? t('Upload failed. Please try again.', SCOPE)
    } finally {
        uploading.value = false
    }
}

/** Discard the staged file and its label without uploading. */
function cancelUpload () {
    pendingFile.value = null
    labelForm.label = ''
    uploadProgress.value = 0
}

const addDisabled = computed(
    () => !selected.value.length || uploading.value || pendingFile.value !== null,
)

function onAddClick () {
    emit('add', [...selected.value])
}

function onCloseClick () {
    emit('close')
}
</script>

<template>
    <wa-dialog
        :label="t('Add media', SCOPE)"
        :open="open"
        @wa-hide.self="onCloseClick"
    >
        <div class="dialog-form">
            <wa-callout v-if="errorText" variant="danger">{{ errorText }}</wa-callout>

            <!-- Upload row -->
            <div class="upload-row">
                <wa-button
                    v-if="!pendingFile"
                    appearance="filled-outlined"
                    :disabled="loading"
                    size="s"
                    variant="brand"
                    @click="pickUpload"
                >
                    <wa-icon name="arrow-up-from-bracket" slot="start"></wa-icon>
                    {{ t('Upload new file', SCOPE) }}
                </wa-button>
                <div v-else class="pending-upload">
                    <wa-input
                        :disabled="uploading"
                        :hint="t('Shown to anyone you share this file with — avoid names or identifiers.', SCOPE)"
                        :label="t('Label', SCOPE)"
                        size="s"
                        v-wa="[labelForm, 'label']"
                    >
                    </wa-input>
                    <div class="pending-actions">
                        <wa-button
                            appearance="plain"
                            :disabled="uploading"
                            size="s"
                            @click="cancelUpload"
                        >
                            {{ t('Cancel', SCOPE) }}
                        </wa-button>
                        <wa-button
                            appearance="filled-outlined"
                            :loading="uploading"
                            size="s"
                            variant="brand"
                            @click="confirmUpload"
                        >
                            {{ uploading
                                ? t('Uploading… {pct}%', SCOPE, { pct: Math.round(uploadProgress * 100) })
                                : t('Upload', SCOPE)
                            }}
                        </wa-button>
                    </div>
                </div>
                <input
                    accept=".md,.pdf,.htm,.html"
                    class="file-picker-input"
                    hidden
                    ref="fileInput"
                    type="file"
                    @change="onFileChosen"
                />
            </div>

            <wa-divider></wa-divider>

            <p v-if="!available.length" class="empty-note">
                {{ t('No media files available. Upload one to get started.', SCOPE) }}
            </p>
            <template v-else>
                <label class="select-label">{{ t('Media files', SCOPE) }}</label>
                <select
                    class="media-select"
                    :disabled="uploading"
                    multiple
                    v-model="selected"
                >
                    <option v-for="m in available" :key="m.content_hash" :value="m.content_hash">
                        {{ m.display_name }}{{ m.file_extension }}
                    </option>
                </select>
            </template>
        </div>
        <div slot="footer" class="form-actions">
            <wa-button
                appearance="filled-outlined"
                :disabled="uploading"
                variant="neutral"
                @click="onCloseClick"
            >
                {{ t('Cancel', SCOPE) }}
            </wa-button>
            <wa-button
                appearance="filled-outlined"
                :disabled="addDisabled"
                variant="brand"
                @click="onAddClick"
            >
                {{ t('Add', SCOPE) }}
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

/* The form's 1rem flex gap already spaces the divider from its neighbours;
   wa-divider's default 1rem block margin (--wa-space-m) just double-spaced it.
   Zero it so the divider keeps the form's uniform rhythm. */
wa-divider {
    margin: 0;
}

.upload-row {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
}

.pending-upload {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
}

.pending-actions {
    display: flex;
    gap: 0.5rem;
    justify-content: flex-end;
}

.empty-note {
    color: var(--wa-color-text-quiet);
    font-size: var(--wa-font-size-s);
    margin: 0;
}

.select-label {
    color: var(--wa-color-text-quiet);
    font-size: var(--wa-font-size-xs);
    font-weight: 600;
    letter-spacing: 0.04em;
    text-transform: uppercase;
}

.media-select {
    background: var(--wa-color-surface-default);
    border: var(--wa-border-width-s) solid var(--wa-color-surface-border);
    border-radius: var(--wa-border-radius-m);
    color: var(--wa-color-text-normal);
    font-family: inherit;
    font-size: var(--wa-font-size-s);
    margin-block-start: 0;
    min-height: 12rem;
    padding: 0.5rem;
    width: 100%;
}

.form-actions {
    display: flex;
    gap: 0.5rem;
    justify-content: flex-end;
}

/* Triggered programmatically by the "Upload file" button; never shown.
   A scoped rule beats WebAwesome's low-specificity native-input reset, which
   the bare [hidden] attribute alone does not. */
.file-picker-input {
    display: none;
}
</style>
