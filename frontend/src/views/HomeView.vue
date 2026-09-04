<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { RouterLink, useRouter } from 'vue-router'
import { t } from '#i18n'
import { useRecordingsStore } from '#stores/recordings'
import { deleteRecording, recordingName, type Recording } from '#api/recordings'
import { showToast } from '#lib/toast'
import { useRecordingSelection } from '#composables/useRecordingSelection'
import EditRecordingDialog from '#components/EditRecordingDialog.vue'
import NeedsAttentionRow from '#components/NeedsAttentionRow.vue'
import RecordingListRow from '#components/RecordingListRow.vue'
import RecordingMediaDialog from '#components/RecordingMediaDialog.vue'

const SCOPE = 'HomeView'
const PROCESSING_DETAIL_THRESHOLD = 5
const RECENT_LIMIT = 10
const NEEDS_ATTENTION_LIMIT = 5

const router = useRouter()
const store = useRecordingsStore()

onMounted(() => {
    store.load()
    store.startPolling()
})
onUnmounted(() => store.stopPolling())

// ── Derived lists ─────────────────────────────────────────────────────────

const processingRecs = computed(() =>
    store.recordings.filter(r => r.status === 'pending' || r.status === 'processing'),
)

const recentRecs = computed(() =>
    store.recordings.filter(r => r.status === 'ready').slice(0, RECENT_LIMIT),
)

// Recordings that failed processing. Derived from the loaded set (like the other
// buckets); the paginated needs-attention view is the authoritative full list.
const failedRecs = computed(() => store.recordings.filter(r => r.status === 'failed'))
const attentionRecs = computed(() => failedRecs.value.slice(0, NEEDS_ATTENTION_LIMIT))

const showProcessingDetail = computed(() =>
    processingRecs.value.length <= PROCESSING_DETAIL_THRESHOLD,
)

// ── Formatters ───────────────────────────────────────────────────────────

function formatDuration (secs: number) {
    if (secs < 60) {
        return `${secs.toFixed(0)} s`
    }
    if (secs < 3600) {
        return `${Math.floor(secs / 60)} m ${Math.floor(secs % 60)} s`
    }
    return `${Math.floor(secs / 3600)} h ${Math.floor((secs % 3600) / 60)} m`
}

function formatSize (bytes: number) {
    if (bytes < 1024 * 1024) {
        return `${(bytes / 1024).toFixed(0)} KB`
    }
    return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

function formatDate (iso: string) {
    return new Date(iso).toLocaleDateString(undefined, {
        year: 'numeric', month: 'short', day: 'numeric',
    })
}

function openRecordings (hashes: string[]) {
    const { href } = router.resolve({ name: 'viewer', query: { files: hashes.join(',') } })
    window.open(href, '_blank')
}

function openUpload () {
    router.push({ name: 'upload' })
}

const listRef = ref<HTMLElement | null>(null)
const { selected, focusedHash, onRowClick, onCheckboxClick, clearSelection } = useRecordingSelection(
    () => recentRecs.value.map(r => r.hash),
    listRef,
)

function openSelectionInViewer () {
    openRecordings([...selected.value])
    clearSelection()
}

// ── Row actions ──────────────────────────────────────────────────────────

const attachingRec = ref<Recording | null>(null)

function handleDropdownAction (value: string, rec: Recording) {
    if (value === 'details') {
        openDetails(rec)
    } else if (value === 'edit') {
        openEdit(rec)
    } else if (value === 'attach-media') {
        attachingRec.value = rec
    } else if (value === 'delete') {
        openDelete(rec)
    }
}

// ── Details drawer ────────────────────────────────────────────────────────

const detailsRec = ref<Recording | null>(null)

function openDetails (rec: Recording) {
    detailsRec.value = rec
}

function closeDetails () {
    detailsRec.value = null
}

// ── Edit recording ───────────────────────────────────────────────────────

const editingRec = ref<Recording | null>(null)

function openEdit (rec: Recording) {
    editingRec.value = rec
}

function onEdited (updated: Recording) {
    const idx = store.recordings.findIndex(r => r.hash === updated.hash)
    if (idx !== -1) {
        store.recordings[idx] = updated
    }
}

// ── Delete recording ─────────────────────────────────────────────────────

const deletingRec = ref<Recording | null>(null)
const deleteLoading = ref(false)

function openDelete (rec: Recording) {
    deletingRec.value = rec
}

function closeDelete () {
    deletingRec.value = null
}

async function confirmDelete () {
    if (!deletingRec.value) {
        return
    }
    deleteLoading.value = true
    try {
        await deleteRecording(deletingRec.value.hash)
        store.recordings = store.recordings.filter(r => r.hash !== deletingRec.value!.hash)
        showToast(`"${recordingName(deletingRec.value)}" moved to trash.`, 'neutral')
        closeDelete()
    } catch {
        showToast('Failed to delete recording. Please try again.', 'danger')
    } finally {
        deleteLoading.value = false
    }
}
</script>

<template>
    <main class="page-view">
        <header class="page-header">
            <h1>{{ t('Home', SCOPE) }}</h1>
            <wa-button
                appearance="filled-outlined"
                size="s"
                variant="brand"
                @click="openUpload"
            >
                <wa-icon name="cloud-arrow-up" slot="start"></wa-icon>
                {{ t('Upload', SCOPE) }}
            </wa-button>
        </header>

        <wa-spinner v-if="store.loading" class="loading-center"></wa-spinner>

        <wa-callout v-else-if="store.error" variant="danger">
            {{ store.error }}
        </wa-callout>

        <template v-else>

            <!-- Needs attention -->
            <section v-if="failedRecs.length" class="home-section">
                <header class="section-heading-row">
                    <h2 class="section-heading">
                        <wa-icon class="attention-icon" name="triangle-exclamation"></wa-icon>
                        {{ t('Needs attention', SCOPE) }}
                        <wa-badge pill variant="danger">{{ failedRecs.length }}</wa-badge>
                    </h2>
                    <RouterLink
                        v-if="failedRecs.length > attentionRecs.length"
                        class="view-all-link"
                        :to="{ name: 'needs-attention' }"
                    >
                        {{ t('View all in library', SCOPE) }}
                        <wa-icon name="arrow-right" class="view-all-link__icon"></wa-icon>
                    </RouterLink>
                </header>
                <div class="attention-rows">
                    <NeedsAttentionRow
                        v-for="rec in attentionRecs"
                        :key="rec.hash"
                        :recording="rec"
                        @delete="openDelete(rec)"
                        @reupload="openUpload"
                    ></NeedsAttentionRow>
                </div>
            </section>

            <!-- In progress -->
            <section v-if="processingRecs.length" class="home-section">
                <h2 class="section-heading">
                    <wa-spinner class="section-spinner"></wa-spinner>
                    {{ t('In progress', SCOPE) }}
                    <wa-badge pill variant="primary">{{ processingRecs.length }}</wa-badge>
                </h2>

                <!-- Summary when many recordings are queued -->
                <wa-callout v-if="!showProcessingDetail" variant="primary">
                    {{ processingRecs.length }}
                    {{ t('recordings are being processed or waiting to start.', SCOPE) }}
                </wa-callout>

                <!-- Individual rows when only a few -->
                <div v-else class="list-rows">
                    <div
                        v-for="rec in processingRecs"
                        :key="rec.hash"
                        class="list-row"
                    >
                        <div class="list-row-main">
                            <span class="list-row-name">{{ recordingName(rec) }}</span>
                            <span class="list-row-meta">
                                {{ rec.status }} ·
                                <wa-relative-time :date="rec.created_at"></wa-relative-time>
                            </span>
                        </div>
                    </div>
                </div>
            </section>

            <!-- Recent recordings -->
            <section class="home-section">
                <header class="section-heading-row">
                    <h2 class="section-heading">{{ t('Recent recordings', SCOPE) }}</h2>
                    <RouterLink v-if="recentRecs.length" class="view-all-link" :to="{ name: 'library' }">
                        {{ t('View all in library', SCOPE) }}
                        <wa-icon name="arrow-right" class="view-all-link__icon"></wa-icon>
                    </RouterLink>
                </header>

                <p v-if="!recentRecs.length" class="empty-state">
                    {{ t('No recordings yet. Upload your first EDF or BDF file.', SCOPE) }}
                </p>

                <div v-else ref="listRef" class="list-rows">
                    <RecordingListRow v-for="rec in recentRecs" :key="rec.hash"
                        :hash="rec.hash"
                        :is-focused="focusedHash === rec.hash"
                        :is-selected="selected.has(rec.hash)"
                        :name="recordingName(rec)"
                        :selection-active="selected.size > 0"
                        @checkbox-click="onCheckboxClick(rec.hash)"
                        @dropdown-action="handleDropdownAction($event, rec)"
                        @open="openRecordings([rec.hash])"
                        @row-click="onRowClick(rec.hash, $event)"
                        @row-dblclick="openRecordings([rec.hash])"
                    >
                        <template #meta>
                            <span class="list-row-meta">
                                <template v-if="rec.meta">
                                    {{ formatDuration(rec.meta.duration) }} ·
                                </template>
                                <wa-relative-time :date="rec.created_at"></wa-relative-time>
                            </span>
                        </template>
                        <template #actions>
                            <wa-dropdown-item value="details">
                                <wa-icon name="circle-info" slot="icon"></wa-icon>
                                {{ t('Details', SCOPE) }}
                            </wa-dropdown-item>
                            <wa-dropdown-item value="edit">
                                <wa-icon name="pencil" slot="icon"></wa-icon>
                                {{ t('Edit', SCOPE) }}
                            </wa-dropdown-item>
                            <wa-dropdown-item value="attach-media">
                                <wa-icon name="paperclip" slot="icon"></wa-icon>
                                {{ t('Attach media', SCOPE) }}
                            </wa-dropdown-item>
                            <wa-dropdown-item value="delete" variant="danger">
                                <wa-icon name="trash" slot="icon"></wa-icon>
                                {{ t('Delete', SCOPE) }}
                            </wa-dropdown-item>
                        </template>
                    </RecordingListRow>
                    <!-- Selection action bar -->
                    <div v-if="selected.size > 0" class="selection-bar">
                        <span class="selection-bar__count">{{ selected.size }} {{ t('selected', SCOPE) }}</span>
                        <wa-button
                            appearance="plain"
                            size="s"
                            variant="neutral"
                            @click="clearSelection"
                        >
                            {{ t('Clear selection', SCOPE) }}
                        </wa-button>
                        <wa-button
                            appearance="filled-outlined"
                            size="s"
                            variant="brand"
                            @click="openSelectionInViewer"
                        >
                            <wa-icon name="arrow-up-right-from-square" slot="start"></wa-icon>
                            {{
                                selected.size === 1
                                ? t('Open recording', SCOPE)
                                : t('Open {count} recordings', SCOPE, { count: selected.size })
                            }}
                        </wa-button>
                    </div>
                </div>
            </section>

        </template>
    </main>

    <!-- Details drawer -->
    <wa-drawer
        :label="detailsRec ? recordingName(detailsRec) : ''"
        :open="!!detailsRec"
        @wa-hide="closeDetails"
    >
        <div v-if="detailsRec" class="wa-stack">
            <p class="wa-caption-s dialog-text">{{ t('File and recording details', SCOPE) }}</p>
            <wa-divider></wa-divider>
            <dl class="wa-stack dialog-text">
                <div v-if="detailsRec.modality" class="wa-flank details-flank">
                    <dt>{{ t('Modality', SCOPE) }}</dt>
                    <dd>{{ detailsRec.modality.toUpperCase() }}</dd>
                </div>
                <div v-if="detailsRec.meta" class="wa-flank details-flank">
                    <dt>{{ t('Format', SCOPE) }}</dt>
                    <dd>{{ detailsRec.meta.format.toUpperCase() }}</dd>
                </div>
                <div v-if="detailsRec.meta" class="wa-flank details-flank">
                    <dt>{{ t('Duration', SCOPE) }}</dt>
                    <dd>{{ formatDuration(detailsRec.meta.duration) }}</dd>
                </div>
                <div v-if="detailsRec.meta" class="wa-flank details-flank">
                    <dt>{{ t('Channels', SCOPE) }}</dt>
                    <dd>{{ detailsRec.meta.signal_count }}</dd>
                </div>
                <div v-if="detailsRec.meta?.discontinuous" class="wa-flank details-flank">
                    <dt>{{ t('Discontinuous', SCOPE) }}</dt>
                    <dd>{{ t('Yes', SCOPE) }}</dd>
                </div>
                <div class="wa-flank details-flank">
                    <dt>{{ t('File size', SCOPE) }}</dt>
                    <dd>{{ formatSize(detailsRec.file_size) }}</dd>
                </div>
                <div class="wa-flank details-flank">
                    <dt>{{ t('Added', SCOPE) }}</dt>
                    <dd>{{ formatDate(detailsRec.created_at) }}</dd>
                </div>
                <div class="wa-flank details-flank">
                    <dt>{{ t('File hash', SCOPE) }}</dt>
                    <dd class="details-hash">{{ detailsRec.file_hash }}</dd>
                </div>
            </dl>
        </div>
    </wa-drawer>

    <!-- Edit recording dialog -->
    <EditRecordingDialog
        :recording="editingRec"
        @close="editingRec = null"
        @updated="onEdited"
    />

    <!-- Delete recording dialog -->
    <wa-dialog
        :label="t('Move to trash', SCOPE)"
        :open="!!deletingRec"
        @wa-hide.self="closeDelete"
    >
        <i18n-t class="dialog-text" keypath="HomeView.move_to_trash_confirm" tag="p">
            <template #name>
                <strong>{{ deletingRec ? recordingName(deletingRec) : '' }}</strong>
            </template>
        </i18n-t>
        <div slot="footer" class="form-actions">
            <wa-button
                appearance="filled-outlined"
                :disabled="deleteLoading"
                variant="neutral"
                @click="closeDelete"
            >
                {{ t('Cancel', SCOPE) }}
            </wa-button>
            <wa-button
                appearance="filled-outlined"
                :loading="deleteLoading"
                variant="danger"
                @click="confirmDelete"
            >
                {{ t('Move to trash', SCOPE) }}
            </wa-button>
        </div>
    </wa-dialog>

    <!-- Attach media dialog -->
    <RecordingMediaDialog
        :open="!!attachingRec"
        :recording-hash="attachingRec?.hash ?? ''"
        :recording-name="attachingRec ? recordingName(attachingRec) : ''"
        @close="attachingRec = null"
    ></RecordingMediaDialog>
</template>

<style scoped>
.section-spinner {
    font-size: 0.9em;
}

.section-heading-row {
    align-items: baseline;
    display: flex;
    gap: 1rem;
    justify-content: space-between;
    margin-bottom: 0.5rem;
}

.attention-icon {
    color: var(--wa-color-danger-fill-loud);
    font-size: 0.9em;
}

.attention-rows {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
}

.section-heading-row .section-heading {
    margin-bottom: 0;
}

.view-all-link {
    align-items: center;
    color: var(--wa-color-brand-text-loud);
    display: inline-flex;
    font-size: 0.875rem;
    font-weight: 500;
    gap: 0.25rem;
    text-decoration: none;
}

.view-all-link:hover {
    text-decoration: underline;
}

.view-all-link__icon {
    font-size: 0.85em;
}

.dialog-form {
    display: flex;
    flex-direction: column;
    gap: 1rem;
}

.dialog-text {
    margin: 0;
}

.details-flank {
    --flank-size: 14ch;
}
</style>
