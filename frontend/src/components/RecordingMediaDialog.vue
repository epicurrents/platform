<script setup lang="ts">
/**
 * Manage the media files attached to a single recording.
 *
 * Lists the recording's current attachments with their timeline offsets and
 * lets the user attach more — either by uploading a new file or picking an
 * existing unattached one — set or edit each file's offset, or detach it. The
 * offset is the recording-time position where the media sits: the playback
 * start for video, or an event / annotation marker for images and documents.
 * It is optional; an empty offset leaves the file unplaced on the timeline.
 */
import { computed, reactive, ref, watch } from 'vue'
import { t } from '#i18n'
import {
    listMediaFiles,
    mediaTypeForFilename,
    patchMedia,
    uploadMedia,
    type MediaFileSummary,
} from '#api/media'
import { showToast } from '#lib/toast'

const SCOPE = 'RecordingMediaDialog'

const props = defineProps<{
    /** When true, the dialog is open. The parent owns this state. */
    open: boolean
    /** Public hash of the recording the media attaches to. */
    recordingHash: string
    /** Recording name, shown in the dialog header. */
    recordingName: string
}>()

const emit = defineEmits<{
    /** User dismissed the dialog. */
    'close': []
    /** An attachment was added, detached, or had its offset changed. */
    'changed': []
}>()

const attachments = ref<MediaFileSummary[]>([])
const existingChoices = ref<MediaFileSummary[]>([])
const loading = ref(false)
const errorText = ref<string | null>(null)

// Add-media sub-form. A pending upload (a chosen File) and a pending existing
// selection are mutually exclusive — picking one clears the other.
const pendingFile = ref<File | null>(null)
const existing = reactive({ hash: '' })
const addOffset = reactive({ value: '' })
const busy = ref(false)
const uploadProgress = ref(0)
const fileInput = ref<HTMLInputElement | null>(null)

// Per-attachment offset editing.
const editingHash = ref<string | null>(null)
const editOffset = reactive({ value: '' })
const savingOffset = ref(false)

const offsetHint = t(
    'Recording time in seconds where this media sits — the playback start for '
    + 'video, or an event marker for images and documents. Leave empty to leave '
    + 'it unplaced; use a negative value if it began before the recording.',
    SCOPE,
)

const addLabel = computed(() => {
    if (pendingFile.value) {
        return pendingFile.value.name
    }
    const picked = existingChoices.value.find(m => m.content_hash === existing.hash)
    return picked ? `${picked.display_name}${picked.file_extension}` : ''
})

const addDisabled = computed(() => busy.value || (!pendingFile.value && !existing.hash))

/** Icon for a media row: a film reel for video, a generic file otherwise. */
function iconFor (media: MediaFileSummary): string {
    return media.media_type === 'video' ? 'film' : 'file'
}

/** Human-readable offset for display, or an "unplaced" label when null. */
function offsetLabel (offset: number | null): string {
    if (offset === null) {
        return t('Unplaced', SCOPE)
    }
    return t('{seconds} s', SCOPE, { seconds: offset })
}

/**
 * Parse the offset input into a number, or null when empty / cleared
 * (unplaced). The v-wa directive writes back a real number for a number input,
 * so a number passes straight through (NaN — a cleared field — counts as
 * unplaced); a string is trimmed and converted, throwing a RangeError on
 * genuinely non-numeric text so the caller can surface it.
 */
function parseOffset (raw: string | number): number | null {
    if (typeof raw === 'number') {
        return Number.isNaN(raw) ? null : raw
    }
    const trimmed = raw.trim()
    if (!trimmed) {
        return null
    }
    const value = Number(trimmed)
    if (Number.isNaN(value)) {
        throw new RangeError('not a number')
    }
    return value
}

async function refresh () {
    loading.value = true
    errorText.value = null
    try {
        attachments.value = await listMediaFiles({
            attachedToType: 'recording',
            attachedToId: props.recordingHash,
            limit: 200,
        })
        // Offer only unattached readable media for the "choose existing" path;
        // reassigning a file already tied to another recording would silently
        // move it, which is surprising from here.
        const all = await listMediaFiles({ limit: 200 })
        existingChoices.value = all.filter(m => m.attached_to === null)
    } catch {
        errorText.value = t('Could not load media for this recording.', SCOPE)
    } finally {
        loading.value = false
    }
}

watch(() => props.open, async (next) => {
    if (!next) {
        return
    }
    pendingFile.value = null
    existing.hash = ''
    addOffset.value = ''
    editingHash.value = null
    uploadProgress.value = 0
    await refresh()
})

// Keep the two add paths mutually exclusive.
watch(() => existing.hash, (hash) => {
    if (hash) {
        pendingFile.value = null
    }
})

function pickFile () {
    fileInput.value?.click()
}

function onFileChosen (event: Event) {
    const target = event.target as HTMLInputElement
    const file = target.files?.[0] ?? null
    // Reset so re-picking the same file fires the change event again.
    target.value = ''
    if (!file) {
        return
    }
    pendingFile.value = file
    existing.hash = ''
}

async function submitAdd () {
    let offset: number | null
    try {
        offset = parseOffset(addOffset.value)
    } catch {
        errorText.value = t('Offset must be a number of seconds.', SCOPE)
        return
    }
    busy.value = true
    errorText.value = null
    uploadProgress.value = 0
    try {
        if (pendingFile.value) {
            const file = pendingFile.value
            await uploadMedia(file, {
                mediaType: mediaTypeForFilename(file.name),
                attachedToType: 'recording',
                attachedToId: props.recordingHash,
                timeOffset: offset ?? undefined,
                onProgress: (f) => { uploadProgress.value = f },
            })
            showToast(t('{name} attached.', SCOPE, { name: file.name }), 'success')
        } else {
            await patchMedia(existing.hash, {
                attachedTo: { type: 'recording', id: props.recordingHash },
                timeOffset: offset,
            })
            showToast(t('Media attached.', SCOPE), 'success')
        }
        pendingFile.value = null
        existing.hash = ''
        addOffset.value = ''
        await refresh()
        emit('changed')
    } catch (e: unknown) {
        const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail
        errorText.value = detail ?? t('Could not attach the media. Please try again.', SCOPE)
    } finally {
        busy.value = false
    }
}

function startEditOffset (media: MediaFileSummary) {
    editingHash.value = media.content_hash
    editOffset.value = media.time_offset === null ? '' : String(media.time_offset)
}

function cancelEditOffset () {
    editingHash.value = null
}

async function saveEditOffset (media: MediaFileSummary) {
    let offset: number | null
    try {
        offset = parseOffset(editOffset.value)
    } catch {
        errorText.value = t('Offset must be a number of seconds.', SCOPE)
        return
    }
    savingOffset.value = true
    errorText.value = null
    try {
        const updated = await patchMedia(media.content_hash, { timeOffset: offset })
        const idx = attachments.value.findIndex(m => m.content_hash === media.content_hash)
        if (idx !== -1) {
            attachments.value[idx] = updated
        }
        editingHash.value = null
        emit('changed')
    } catch {
        errorText.value = t('Could not update the offset. Please try again.', SCOPE)
    } finally {
        savingOffset.value = false
    }
}

async function detach (media: MediaFileSummary) {
    errorText.value = null
    try {
        await patchMedia(media.content_hash, { attachedTo: { type: '', id: '' } })
        showToast(t('Media detached.', SCOPE), 'neutral')
        await refresh()
        emit('changed')
    } catch {
        errorText.value = t('Could not detach the media. Please try again.', SCOPE)
    }
}

function onClose () {
    emit('close')
}
</script>

<template>
    <wa-dialog
        :label="t('Attach media to {name}', SCOPE, { name: recordingName })"
        :open="open"
        @wa-hide.self="onClose"
    >
        <div class="dialog-body">
            <wa-callout v-if="errorText" variant="danger">{{ errorText }}</wa-callout>

            <!-- Current attachments -->
            <section class="section">
                <label class="section-label">{{ t('Attached media', SCOPE) }}</label>
                <wa-spinner v-if="loading"></wa-spinner>
                <p v-else-if="!attachments.length" class="empty-note">
                    {{ t('Nothing attached yet.', SCOPE) }}
                </p>
                <ul v-else class="attach-list">
                    <li v-for="media in attachments" :key="media.content_hash" class="attach-row">
                        <wa-icon class="attach-icon" :name="iconFor(media)"></wa-icon>
                        <span class="attach-name">{{ media.display_name }}{{ media.file_extension }}</span>
                        <template v-if="editingHash === media.content_hash">
                            <wa-input
                                class="offset-input"
                                :disabled="savingOffset"
                                :placeholder="t('seconds', SCOPE)"
                                size="s"
                                step="0.1"
                                type="number"
                                v-wa="[editOffset, 'value']"
                            ></wa-input>
                            <wa-button
                                appearance="plain"
                                :loading="savingOffset"
                                size="s"
                                :title="t('Save offset', SCOPE)"
                                variant="brand"
                                @click="saveEditOffset(media)"
                            >
                                <wa-icon name="check"></wa-icon>
                            </wa-button>
                            <wa-button
                                appearance="plain"
                                :disabled="savingOffset"
                                size="s"
                                :title="t('Cancel', SCOPE)"
                                @click="cancelEditOffset"
                            >
                                <wa-icon name="xmark"></wa-icon>
                            </wa-button>
                        </template>
                        <template v-else>
                            <span class="attach-offset">{{ offsetLabel(media.time_offset) }}</span>
                            <wa-button
                                appearance="plain"
                                size="s"
                                :title="t('Edit offset', SCOPE)"
                                @click="startEditOffset(media)"
                            >
                                <wa-icon name="clock"></wa-icon>
                            </wa-button>
                            <wa-button
                                appearance="plain"
                                size="s"
                                :title="t('Detach', SCOPE)"
                                variant="danger"
                                @click="detach(media)"
                            >
                                <wa-icon name="xmark"></wa-icon>
                            </wa-button>
                        </template>
                    </li>
                </ul>
            </section>

            <wa-divider></wa-divider>

            <!-- Add media -->
            <section class="section">
                <label class="section-label">{{ t('Add media', SCOPE) }}</label>
                <div class="add-controls">
                    <wa-button
                        appearance="filled-outlined"
                        :disabled="busy"
                        size="s"
                        variant="brand"
                        @click="pickFile"
                    >
                        <wa-icon name="cloud-arrow-up" slot="start"></wa-icon>
                        {{ t('Upload file', SCOPE) }}
                    </wa-button>
                    <span class="add-or">{{ t('or', SCOPE) }}</span>
                    <wa-select
                        class="existing-select"
                        :disabled="busy || !existingChoices.length"
                        size="s"
                        v-wa="[existing, 'hash']"
                    >
                        <wa-option value="">{{ t('Choose an existing file', SCOPE) }}</wa-option>
                        <wa-option v-for="m in existingChoices" :key="m.content_hash" :value="m.content_hash">
                            {{ m.display_name }}{{ m.file_extension }}
                        </wa-option>
                    </wa-select>
                </div>
                <p v-if="addLabel" class="add-chosen">{{ t('Selected: {name}', SCOPE, { name: addLabel }) }}</p>
                <wa-input
                    :disabled="busy"
                    :hint="offsetHint"
                    :label="t('Timeline offset (seconds)', SCOPE)"
                    :placeholder="t('Leave empty to leave unplaced', SCOPE)"
                    size="s"
                    step="0.1"
                    type="number"
                    v-wa="[addOffset, 'value']"
                ></wa-input>
                <input
                    accept="video/*,.pdf,.md,.htm,.html,.txt"
                    class="file-picker-input"
                    hidden
                    ref="fileInput"
                    type="file"
                    @change="onFileChosen"
                />
            </section>
        </div>

        <div slot="footer" class="form-actions">
            <wa-button
                appearance="filled-outlined"
                :disabled="busy"
                variant="neutral"
                @click="onClose"
            >
                {{ t('Close', SCOPE) }}
            </wa-button>
            <wa-button
                appearance="filled-outlined"
                :disabled="addDisabled"
                :loading="busy"
                variant="brand"
                @click="submitAdd"
            >
                {{ busy && pendingFile
                    ? t('Uploading… {pct}%', SCOPE, { pct: Math.round(uploadProgress * 100) })
                    : t('Attach', SCOPE)
                }}
            </wa-button>
        </div>
    </wa-dialog>
</template>

<style scoped>
.dialog-body {
    display: flex;
    flex-direction: column;
    gap: 1rem;
}
    .dialog-body wa-divider {
        margin: 0.5rem 0;
    }

.section {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
}

.section-label {
    color: var(--wa-color-text-quiet);
    font-size: var(--wa-font-size-xs);
    font-weight: 600;
    letter-spacing: 0.04em;
    text-transform: uppercase;
}

.empty-note {
    color: var(--wa-color-text-quiet);
    font-size: var(--wa-font-size-s);
    margin: 0;
}

.attach-list {
    display: flex;
    flex-direction: column;
    gap: 0.25rem;
    list-style: none;
    margin: 0;
    padding: 0;
}

.attach-row {
    align-items: center;
    border: var(--wa-border-width-s) solid var(--wa-color-surface-border);
    border-radius: var(--wa-border-radius-m);
    display: flex;
    gap: 0.5rem;
    margin-inline-start: 0;
    padding: 0.375rem 0.5rem;
}

.attach-icon {
    color: var(--wa-color-text-quiet);
    flex-shrink: 0;
}

.attach-name {
    flex: 1;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}

.attach-offset {
    color: var(--wa-color-text-quiet);
    font-size: var(--wa-font-size-s);
    white-space: nowrap;
}

.offset-input {
    width: 7rem;
}

.add-controls {
    align-items: center;
    display: flex;
    flex-wrap: wrap;
    gap: 0.5rem;
}

.add-or {
    color: var(--wa-color-text-quiet);
    font-size: var(--wa-font-size-s);
}

.existing-select {
    flex: 1;
    min-width: 12rem;
}

.add-chosen {
    color: var(--wa-color-text-quiet);
    font-size: var(--wa-font-size-s);
    margin: 0;
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
