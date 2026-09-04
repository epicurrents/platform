<script setup lang="ts">
import { reactive, ref, computed, shallowRef, onMounted, onUnmounted } from 'vue'
import { onBeforeRouteLeave, useRouter } from 'vue-router'
import { t } from '#i18n'
import { useRecordingsStore } from '#stores/recordings'
import { showToast } from '#lib/toast'
import {
    treeFromInputEvent,
    treeFromDropEvent,
    filterByExtension,
    pruneEmptyFolders,
    countFiles,
    hasFolders,
    isEmpty,
    type UploadTree,
} from '#composables/useFileTree'
import CollectionPickerDialog, { type PickerSelection, type CollectionSelection } from '#components/CollectionPickerDialog.vue'
import FileTreeItem from '#components/FileTreeItem.vue'
import type { Collection } from '#api/library'

const SCOPE = 'UploadView'
const ACCEPTED_EXTENSIONS = ['.edf', '.bdf', '.csv']

const router = useRouter()
const store = useRecordingsStore()

// ---------------------------------------------------------------------------
// Stage management
// ---------------------------------------------------------------------------

type Stage = 'select' | 'preview' | 'uploading' | 'done'
const stage = ref<Stage>('select')

// ---------------------------------------------------------------------------
// File tree state
// ---------------------------------------------------------------------------

const tree = shallowRef<UploadTree>({ rootFiles: [], rootFolders: [] })
const rejectedCount = ref(0)
const input = reactive({
    modality: 'eeg',
    // Grantee-visible label for a single-file upload; blank falls back to an
    // anonymous hash-prefix ID, keeping a PHI-bearing filename off the server.
    displayName: '',
    preserveAnnotations: false,
    // Opt-in: mirror the on-disk folder structure as nested Collections.
    // Default off because folder names on the uploading user's disk frequently
    // contain patient identifiers (site IDs, study codes, initials, dates of
    // birth). See frontend/README.md → Folder uploads + PHI.
    preserveFolderHierarchy: false,
})

// Hidden file input refs
const filesInputRef = ref<HTMLInputElement | null>(null)
const folderInputRef = ref<HTMLInputElement | null>(null)

// Drop zone state
const isDragOver = ref(false)

// ---------------------------------------------------------------------------
// Target collection (upload destination)
// ---------------------------------------------------------------------------

/** The collection to upload into. null = library root. */
const targetCollection = ref<Collection | null>(null)
const showPicker = ref(false)

function onPickerSelect (selection: PickerSelection) {
    if (selection.type === 'collection') {
        targetCollection.value = (selection as CollectionSelection).collection
    }
    showPicker.value = false
}

const totalFiles = computed(() => countFiles(tree.value))
const willCreateCollections = computed(() => hasFolders(tree.value))

// A display name is offered only for a lone recording; a batch names each file
// by its own hash-prefix fallback instead.
const isSingleFile = computed(() => totalFiles.value === 1 && tree.value.rootFolders.length === 0)

// ---------------------------------------------------------------------------
// Root-file pagination (preview stage only)
// ---------------------------------------------------------------------------

const ROOT_PAGE_SIZE = 50
const rootFilePage = ref(0)
const totalRootFilePages = computed(() => Math.ceil(tree.value.rootFiles.length / ROOT_PAGE_SIZE))
const showRootFilePagination = computed(() => tree.value.rootFiles.length > ROOT_PAGE_SIZE)
const visibleRootFiles = computed(() => {
    if (!showRootFilePagination.value) {
        return tree.value.rootFiles
    }
    const start = rootFilePage.value * ROOT_PAGE_SIZE
    return tree.value.rootFiles.slice(start, start + ROOT_PAGE_SIZE)
})

// ---------------------------------------------------------------------------
// Progress state (uploading / done stages)
// ---------------------------------------------------------------------------

// Read from `store.currentBatch` (the batch this view kicked off) rather than
// `activeBatches[0]` (the oldest), so a prior batch lingering from a previous
// upload-then-navigate-away session does not bleed its totals + file map into
// this session's progress display.
const doneFiles = computed(() => store.currentBatch?.doneFiles ?? 0)
const totalBatchFiles = computed(() => store.currentBatch?.totalFiles ?? totalFiles.value)
const overallPercent = computed(() =>
    totalBatchFiles.value > 0 ? Math.round((doneFiles.value / totalBatchFiles.value) * 100) : 0,
)
const collectionsCreated = computed(() => store.currentBatch?.collectionsCreated ?? 0)

/** Map from file key → phase, used to show per-row status icons. */
const filePhases = computed<Map<string, string>>(() => {
    const batch = store.currentBatch
    if (!batch) {
        return new Map()
    }
    const m = new Map<string, string>()
    for (const [key, state] of batch.files) {
        m.set(key, state.phase)
    }
    return m
})

// ---------------------------------------------------------------------------
// Input / drag-drop handlers
// ---------------------------------------------------------------------------

function applyTree (raw: UploadTree) {
    const { filtered, rejectedCount: n } = filterByExtension(raw, ACCEPTED_EXTENSIONS)
    const pruned = pruneEmptyFolders(filtered)
    tree.value = pruned
    rejectedCount.value = n
    if (!isEmpty(pruned)) {
        stage.value = 'preview'
    }
}

function onFilesChange (event: Event) {
    applyTree(treeFromInputEvent(event))
    if (filesInputRef.value) {
        filesInputRef.value.value = ''
    }
}

function onFolderChange (event: Event) {
    applyTree(treeFromInputEvent(event))
    if (folderInputRef.value) {
        folderInputRef.value.value = ''
    }
}

function onDragOver (event: DragEvent) {
    event.preventDefault()
    isDragOver.value = true
}

function onDragLeave () {
    isDragOver.value = false
}

async function onDrop (event: DragEvent) {
    event.preventDefault()
    isDragOver.value = false
    // Must call synchronously in handler before DataTransfer is invalidated;
    // the returned Promise resolves after async folder traversal completes.
    const raw = await treeFromDropEvent(event)
    applyTree(raw)
}

function clearSelection () {
    tree.value = { rootFiles: [], rootFolders: [] }
    rejectedCount.value = 0
    stage.value = 'select'
    // Drop the finished batch (if any) so the next upload starts from a clean
    // slate — see `currentBatch` doc on the store for the carryover failure
    // mode this prevents.
    store.clearCompletedBatches()
}

function goHome () {
    router.push({ name: 'home' })
}

function triggerFileSelect () {
    filesInputRef.value?.click()
}

function triggerFolderSelect () {
    folderInputRef.value?.click()
}

function openPicker () {
    showPicker.value = true
}

function closePicker () {
    showPicker.value = false
}

function prevRootPage () {
    if (rootFilePage.value > 0) {
        rootFilePage.value--
    }
}

function nextRootPage () {
    if (rootFilePage.value < totalRootFilePages.value - 1) {
        rootFilePage.value++
    }
}

// ---------------------------------------------------------------------------
// Upload orchestration
// ---------------------------------------------------------------------------

async function startUpload () {
    // Guarantee a clean store state before the new batch is pushed, so the
    // `currentBatch` getter resolves to this session's batch even if a prior
    // run left a finished batch behind.
    store.clearCompletedBatches()
    stage.value = 'uploading'
    try {
        const parentCollectionId = targetCollection.value?.id ?? null
        const modality = input.modality
        const preserveAnnotations = input.preserveAnnotations
        const preserveFolderHierarchy = input.preserveFolderHierarchy
        if (willCreateCollections.value) {
            await store.uploadTree(tree.value, {
                parentCollectionId,
                modality,
                preserveAnnotations,
                preserveFolderHierarchy,
            })
        } else {
            await store.uploadBatch(tree.value.rootFiles, {
                parentCollectionId,
                modality,
                preserveAnnotations,
                displayName: input.displayName.trim() || undefined,
            })
        }
    } catch {
        showToast(t('Upload failed. Please try again.', SCOPE), 'danger')
        stage.value = 'preview'
        return
    }
    const batch = store.currentBatch
    if (batch?.phase === 'partial') {
        showToast(
            t('Some files could not be uploaded or processed. Check the results below.', SCOPE),
            'warning',
            8000,
        )
    } else {
        showToast(t('All files uploaded — processing in the background.', SCOPE), 'brand')
    }
    stage.value = 'done'
}

function finish () {
    if (store.currentBatch) {
        store.removeBatch(store.currentBatch.id)
    }
    if (targetCollection.value) {
        router.push({ name: 'collection', params: { id: targetCollection.value.id } })
    } else {
        router.push({ name: 'library' })
    }
}

// ---------------------------------------------------------------------------
// Navigation guard (only fires while an upload is in flight)
// ---------------------------------------------------------------------------

const showLeavePrompt = ref(false)
const pendingLeaveResolver = ref<((value: boolean) => void) | null>(null)

function isUploading () {
    return stage.value === 'uploading'
}

function confirmLeave () {
    pendingLeaveResolver.value?.(true)
    pendingLeaveResolver.value = null
    showLeavePrompt.value = false
}

function cancelLeave () {
    pendingLeaveResolver.value?.(false)
    pendingLeaveResolver.value = null
    showLeavePrompt.value = false
}

// Browser navigation / tab close. Modern browsers ignore custom messages
// and show their own generic warning. `returnValue` is formally deprecated
// but still required for Safari / older Chrome — `preventDefault()` alone
// is enough on current Chrome, both together is the cross-browser pattern.
function onBeforeUnload (event: BeforeUnloadEvent) {
    if (isUploading()) {
        event.preventDefault()
        event.returnValue = ''
    }
}

// Vue Router in-app navigation. When uploading, show a custom dialog and
// defer resolution until the user picks Continue / Leave. Other stages
// resolve to `true` immediately.
onBeforeRouteLeave(() => {
    if (!isUploading()) {
        return true
    }
    return new Promise<boolean>(resolve => {
        pendingLeaveResolver.value = resolve
        showLeavePrompt.value = true
    })
})

onMounted(() => {
    window.addEventListener('beforeunload', onBeforeUnload)
})

onUnmounted(() => {
    window.removeEventListener('beforeunload', onBeforeUnload)
    // Best-effort store cleanup. If the user navigates away mid-upload we
    // leave the running batch in place — it's still resolving on the
    // network and may yet complete.
    store.clearCompletedBatches()
})

// ---------------------------------------------------------------------------
// Per-row icon helpers
// ---------------------------------------------------------------------------

function phaseIcon (phase: string | undefined) {
    switch (phase) {
        case 'ready':
            return 'circle-check'
        case 'failed':
        case 'timeout':
            return 'triangle-exclamation'
        case 'queued':
            return 'clock'
        default:
            return 'spinner'
    }
}

function isSpinning (phase: string | undefined) {
    return phase === 'uploading' || phase === 'processing'
}

function phaseIconClass (phase: string | undefined) {
    if (phase === 'ready') {
        return 'phase-icon--ready'
    }
    if (phase === 'failed' || phase === 'timeout') {
        return 'phase-icon--error'
    }
    if (phase === 'queued') {
        return 'phase-icon--queued'
    }
    if (isSpinning(phase)) {
        return 'phase-icon--active phase-icon--spinning'
    }
    return 'phase-icon--active'
}

</script>

<template>
    <main class="upload-view">
        <!-- Chrome band — header and the stage-specific blocks. Wrapped so they keep
             the content-column width now that the host is full-bleed for the scroller. -->
        <div
            class="upload-view__band"
            :class="{ 'upload-view__band--fill': stage === 'select' }"
        >
            <header class="page-header">
                <h1>{{ t('Upload recordings', SCOPE) }}</h1>
                <wa-button appearance="plain" @click="goHome">
                    <wa-icon name="arrow-left" slot="start"></wa-icon>
                    Back
                </wa-button>
            </header>

            <!-- ----------------------------------------------------------------
                 Stage: select — drop zone fills the available content space.
                 ---------------------------------------------------------------- -->
            <template v-if="stage === 'select'">
                <div
                    class="drop-zone"
                    :class="{ 'drop-zone--over': isDragOver }"
                    @dragover="onDragOver"
                    @dragleave="onDragLeave"
                    @drop="onDrop"
                >
                    <wa-icon name="cloud-arrow-up" class="drop-zone__icon"></wa-icon>
                    <p class="drop-zone__label">{{ t('Drop files or folders here', SCOPE) }}</p>
                    <p class="drop-zone__hint">{{ t('Supported formats: EDF, EDF+, BDF, BDF+, CSV', SCOPE) }}</p>
                    <div class="drop-zone__buttons">
                        <wa-button appearance="filled-outlined" variant="brand" @click="triggerFileSelect">
                            <wa-icon name="files" slot="start"></wa-icon>
                            {{ t('Select files', SCOPE) }}
                        </wa-button>
                        <wa-button appearance="filled-outlined" variant="brand" @click="triggerFolderSelect">
                            <wa-icon name="folders" slot="start"></wa-icon>
                            {{ t('Select folders', SCOPE) }}
                        </wa-button>
                    </div>
                </div>

                <input
                    ref="filesInputRef"
                    type="file"
                    multiple
                    accept=".edf,.bdf,.csv"
                    class="hidden-input"
                    @change="onFilesChange"
                />
                <input
                    ref="folderInputRef"
                    type="file"
                    webkitdirectory
                    class="hidden-input"
                    @change="onFolderChange"
                />
            </template>

            <!-- ----------------------------------------------------------------
                 Stage: preview — chrome above the scrollable tree.
                 ---------------------------------------------------------------- -->
            <template v-else-if="stage === 'preview'">
                <wa-callout v-if="rejectedCount > 0" variant="warning" class="gap">
                    <wa-icon name="triangle-exclamation" slot="icon"></wa-icon>
                    {{ t('{count} file(s) skipped — only EDF, BDF, and CSV files are supported.', SCOPE, { count: rejectedCount }) }}
                </wa-callout>

                <wa-input
                    v-if="isSingleFile"
                    class="gap"
                    :hint="t('Shown to anyone you share this recording with. Leave blank to use an anonymous ID (like ABCD1234) instead of the file name.', SCOPE)"
                    :label="t('Display name', SCOPE)"
                    size="s"
                    type="text"
                    v-wa="[input, 'displayName']"
                ></wa-input>

                <wa-select
                    class="gap"
                    :label="t('Recording modality', SCOPE)"
                    size="s"
                    v-wa="[input, 'modality']"
                >
                    <wa-option value="eeg" selected>{{ t('EEG', SCOPE) }}</wa-option>
                    <wa-option value="acc">{{ t('Accelerometry', SCOPE) }}</wa-option>
                </wa-select>

                <wa-checkbox class="gap" v-wa="[input, 'preserveAnnotations']">
                    {{ t('Preserve original annotations in file', SCOPE) }}
                </wa-checkbox>

                <!-- Hierarchy opt-in — folder uploads are flattened by default to
                     keep on-disk folder names off the server (they routinely
                     contain patient identifiers). -->
                <template v-if="willCreateCollections">
                    <wa-checkbox class="gap" v-wa="[input, 'preserveFolderHierarchy']">
                        {{ t('Preserve folder hierarchy as subcollections', SCOPE) }}
                    </wa-checkbox>
                    <wa-callout v-if="input.preserveFolderHierarchy" variant="warning" class="gap">
                        <wa-icon name="triangle-exclamation" slot="icon"></wa-icon>
                        {{
                            t(
                                'Folder names from your disk will become collection names visible to anyone with access. Make sure they do not contain patient identifiers (names, initials, dates of birth, site / study codes).',
                                SCOPE,
                            )
                        }}
                    </wa-callout>
                </template>

                <!-- Upload location -->
                <div class="location-row gap">
                    <wa-icon name="folder" class="location-row__icon"></wa-icon>
                    <span class="location-row__label">
                        <span class="location-row__prefix">{{ t('Upload to:', SCOPE) }}</span>
                        <span v-if="targetCollection" class="location-row__name">{{ targetCollection.name }}</span>
                        <span v-else class="location-row__root">{{ t('Library root', SCOPE) }}</span>
                    </span>
                    <wa-button appearance="filled-outlined" size="s" variant="brand" @click="openPicker">
                        {{ t('Change', SCOPE) }}
                    </wa-button>
                </div>

                <div class="tree-panel__header">
                    <span>{{ t('{count} file(s) selected', SCOPE, { count: totalFiles }) }}</span>
                    <span v-if="willCreateCollections" class="tree-panel__badge">
                        {{ t('Folders → Collections', SCOPE) }}
                    </span>
                </div>
            </template>

            <!-- ----------------------------------------------------------------
                 Stage: uploading — chrome above the scrollable tree.
                 ---------------------------------------------------------------- -->
            <template v-else-if="stage === 'uploading'">
                <wa-callout variant="brand" class="gap">
                    <wa-icon class="phase-icon--spinning" name="spinner" slot="icon"></wa-icon>
                    {{ t('Uploading and processing — do not close this page.', SCOPE) }}
                </wa-callout>

                <div class="progress-block gap">
                    <div class="progress-block__label">
                        <span>
                            {{ t('{done} / {total} files done', SCOPE, { done: doneFiles, total: totalBatchFiles }) }}
                        </span>
                        <span v-if="collectionsCreated > 0" class="progress-block__collections">
                            {{ t('{count} collection(s) created', SCOPE, { count: collectionsCreated }) }}
                        </span>
                    </div>
                    <wa-progress-bar :value="overallPercent"></wa-progress-bar>
                </div>
            </template>

        </div>

        <!-- ----------------------------------------------------------------
             Scrollable tree — the only element inside the scroller. Chrome
             above and action buttons below stay visible regardless of scroll.
             ---------------------------------------------------------------- -->
        <wa-scroller v-if="stage !== 'select'" orientation="vertical">
            <div class="upload-view__scroll-wrap">
                <!-- Preview-stage tree: static file icons + root-file pagination. -->
                <wa-tree v-if="stage === 'preview'"
                    class="upload-tree upload-tree--guided"
                    :class="{ 'upload-tree--flat': !willCreateCollections }"
                >
                    <wa-tree-item v-for="leaf in visibleRootFiles" :key="leaf.relativePath || leaf.file.name"
                        class="item"
                    >
                        <div class="file-row">
                            <wa-icon class="icon-file" name="file-music"></wa-icon>
                            <span class="file-row__name">{{ leaf.file.name }}</span>
                            <wa-format-bytes class="file-row__size" :value="leaf.file.size"></wa-format-bytes>
                        </div>
                    </wa-tree-item>
                    <wa-tree-item v-if="showRootFilePagination" class="pagination-row">
                        <span class="pagination-controls">
                            <button
                                class="page-btn"
                                :disabled="rootFilePage === 0"
                                @click.stop="prevRootPage"
                            >‹</button>
                            <span class="page-label">
                                {{ rootFilePage + 1 }}&thinsp;/&thinsp;{{ totalRootFilePages }}
                            </span>
                            <button
                                class="page-btn"
                                :disabled="rootFilePage >= totalRootFilePages - 1"
                                @click.stop="nextRootPage"
                            >›</button>
                        </span>
                    </wa-tree-item>
                    <file-tree-item
                        v-for="folder in tree.rootFolders"
                        :key="folder.name"
                        :folder="folder"
                    />
                </wa-tree>

                <!-- Uploading / done tree: per-row phase icons. -->
                <wa-tree v-else
                    class="upload-tree upload-tree--guided"
                    :class="{ 'upload-tree--flat': !willCreateCollections }"
                >
                    <wa-tree-item
                        v-for="leaf in tree.rootFiles"
                        :key="leaf.relativePath || leaf.file.name"
                        class="item"
                    >
                        <div class="file-row">
                            <wa-icon
                                :class="phaseIconClass(filePhases.get(leaf.relativePath || leaf.file.name))"
                                :name="phaseIcon(filePhases.get(leaf.relativePath || leaf.file.name))"
                            ></wa-icon>
                            <span class="file-row__name">{{ leaf.file.name }}</span>
                            <wa-format-bytes class="file-row__size" :value="leaf.file.size"></wa-format-bytes>
                        </div>
                    </wa-tree-item>
                    <file-tree-item
                        v-for="folder in tree.rootFolders"
                        :key="folder.name"
                        :folder="folder"
                        :file-phases="filePhases"
                    />
                </wa-tree>
            </div>
        </wa-scroller>

        <!-- Sticky action bar — outside the scroller so the primary actions
             stay visible regardless of how far the user has scrolled through
             the file list. -->
        <footer v-if="stage === 'preview' || stage === 'done'" class="upload-view__action-bar">
            <template v-if="stage === 'preview'">
                <wa-button appearance="filled-outlined" variant="neutral" @click="clearSelection">
                    <wa-icon name="xmark" slot="start"></wa-icon>
                    {{ t('Clear', SCOPE) }}
                </wa-button>
                <wa-button variant="brand" @click="startUpload">
                    <wa-icon name="cloud-arrow-up" slot="start"></wa-icon>
                    {{ t('Start upload', SCOPE) }}
                </wa-button>
            </template>
            <template v-else>
                <wa-button appearance="filled-outlined" variant="brand" @click="clearSelection">
                    {{ t('Upload more', SCOPE) }}
                </wa-button>
                <wa-button appearance="filled-outlined" variant="brand" @click="finish">
                    {{ t('Go to library', SCOPE) }}
                </wa-button>
            </template>
        </footer>
    </main>

    <collection-picker-dialog
        :open="showPicker"
        mode="collection"
        @select="onPickerSelect"
        @close="closePicker"
    />

    <!-- Leave-while-uploading guard. Fires from onBeforeRouteLeave when the
         user attempts in-app navigation mid-upload; hard-navigate / tab close
         is caught separately by the beforeunload listener. -->
    <wa-dialog
        :label="t('Upload in progress', SCOPE)"
        :open="showLeavePrompt"
        @wa-hide.self="cancelLeave"
    >
        <p>
            {{
                t(
                    'An upload is in progress. Leaving this page will lose track of any files that have not finished yet — they may continue in the background and may not all complete. Leave anyway?',
                    SCOPE,
                )
            }}
        </p>
        <div slot="footer" class="form-actions">
            <wa-button appearance="filled-outlined" variant="neutral" @click="cancelLeave">
                {{ t('Continue uploading', SCOPE) }}
            </wa-button>
            <wa-button appearance="filled-outlined" variant="danger" @click="confirmLeave">
                {{ t('Leave anyway', SCOPE) }}
            </wa-button>
        </div>
    </wa-dialog>
</template>

<style scoped>
.hidden-input {
    display: none;
}

.upload-view {
    /* Width of the centred content column. The band, the scroller's inner wrap and
     * the action bar all have to agree on it, so it is named once here. */
    --content-width: 640px;
    /* Fill the remaining vertical space within .route-view-wrapper so the
     * internal wa-scroller resolves its height against a bounded box. The
     * padding stays on the host so it shows as a visible inset around the
     * scroller — content scrolls within the inset rather than sliding
     * flush with the viewport edges. */
    flex: 1;
    display: flex;
    flex-direction: column;
    min-height: 0;
    overflow: hidden;
    width: 100%;
    padding: 2rem 1rem;
}

.upload-view__scroll-wrap {
    /* Centring lives here, inside the scroller, rather than on the host: the
     * scroller has to span the full width so its scrollbar rides the viewport
     * edge instead of the content column. Flex-1 wrap for wa-scroller — see
     * AGENTS.md → WebAwesome shadow-DOM layout gotchas. */
    flex: 1;
    display: flex;
    flex-direction: column;
    margin: 0 auto;
    max-width: var(--content-width);
    min-height: 0;
    overflow: hidden;
    width: 100%;
}

.upload-view__band {
    /* The non-scrolling chrome keeps the content-column width the host used to
     * impose. Flex column so the wrapped blocks stack exactly as they did when
     * they were direct children of the host. */
    display: flex;
    flex-direction: column;
    margin: 0 auto;
    max-width: var(--content-width);
    width: 100%;
}

.upload-view__band--fill {
    /* Select stage renders no scroller, so the band is what claims the leftover
     * height that .drop-zone (flex: 1) expands into. */
    flex: 1;
    min-height: 0;
}

.upload-header {
    display: flex;
    align-items: center;
    gap: 0.75rem;
    margin-bottom: 1.5rem;
}

.upload-header h1 {
    margin: 0;
    font-size: 1.5rem;
}

.gap {
    margin-bottom: 1.25rem;
}

/* Drop zone ---------------------------------------------------------------- */
.drop-zone {
    align-items: center;
    border: 2px dashed var(--wa-color-neutral-300);
    border-radius: var(--wa-border-radius-l);
    cursor: default;
    display: flex;
    flex: 1;
    flex-direction: column;
    justify-content: center;
    min-height: 0;
    padding: 2.5rem 1.5rem;
    text-align: center;
    transition: border-color 0.15s, background 0.15s;
}

.drop-zone--over {
    border-color: var(--wa-color-brand-500);
    background: var(--wa-color-brand-50);
}

.drop-zone__icon {
    font-size: 2.5rem;
    color: var(--wa-color-neutral-400);
    margin-bottom: 0.75rem;
}

.drop-zone__label {
    margin: 0 0 0.25rem;
    font-size: 1rem;
    font-weight: 600;
}

.drop-zone__hint {
    margin: 0 0 1.25rem;
    font-size: 0.875rem;
    color: var(--wa-color-neutral-500);
}

.drop-zone__buttons {
    display: flex;
    gap: 0.75rem;
    justify-content: center;
    flex-wrap: wrap;
}

/* Tree panel (preview header) ---------------------------------------------- */
.tree-panel__header {
    align-items: center;
    background: var(--wa-color-neutral-fill-quiet);
    border-radius: var(--wa-border-radius-m);
    color: var(--wa-color-text-quiet);
    display: flex;
    font-size: 0.8125rem;
    justify-content: space-between;
    margin-bottom: 0.5rem;
    padding: 0.5rem 0.75rem;
}

.tree-panel__badge {
    font-size: 0.75rem;
    background: var(--wa-color-brand-100);
    color: var(--wa-color-brand-700);
    padding: 0.125rem 0.5rem;
    border-radius: var(--wa-border-radius-pill);
}

/* WA tree (shared by preview / uploading / done) -------------------------- */
.upload-tree {
    font-size: 1rem;
}
    .upload-tree wa-tree-item {
        --indent-size: 1em;
    }

/* File row layout — icon on the left, filename takes the remaining space,
   size pushed to the right edge. Used by both the root-file rows here and
   the recursive folder-content rows in FileTreeItem. */
.file-row {
    align-items: center;
    display: flex;
    flex: 1;
    gap: 0.5em;
    justify-content: space-evenly;
}

.file-row__name {
    flex: 1;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}

.file-row__size {
    color: var(--wa-color-text-quiet);
    flex-shrink: 0;
    font-size: 0.85em;
}

.upload-tree--guided {
    --indent-guide-width: 1px;
}

/* A flat list has no expandable folders, so hide the per-item expand chevron
   slot — otherwise every file row carries a dead affordance and the indent
   that only belongs to folder trees. */
.upload-tree--flat :deep(wa-tree-item::part(expand-button)) {
    display: none;
}

:deep(wa-tree-item::part(item)) {
    background-color: transparent !important;
    border-inline-start-color: transparent !important;
}

/* wa-tree-item's label shadow part is content-sized by default, so the inner
   .file-row's flex layout has nothing to grow into and the size never reaches
   the right edge. Stretching the label part to fill the row gives the flex
   container a bounded width so margin-left semantics work as expected. */
.item::part(label) {
    flex: 1;
}

.icon-file {
    color: var(--wa-color-neutral-on-quiet);
}

.phase-icon--ready {
    color: var(--wa-color-success-fill-loud);
}

.phase-icon--error {
    color: var(--wa-color-danger-fill-loud);
}

/* Queued phase: gray static clock — file is in the pool but not yet in flight. */
.phase-icon--queued {
    color: var(--wa-color-text-quiet);
}

/* Unknown phase fallback: gray static glyph. Same colour as queued — they
   look visually similar because both mean "not actively uploading". */
.phase-icon--active {
    color: var(--wa-color-text-quiet);
}

/* Active phase (uploading / processing): blue spinning glyph. */
.phase-icon--active.phase-icon--spinning {
    color: var(--wa-color-brand-fill-loud);
}

.phase-icon--spinning {
    animation: spin 1s linear infinite;
}

/* Progress block ----------------------------------------------------------- */
.progress-block__label {
    display: flex;
    justify-content: space-between;
    font-size: 0.875rem;
    margin-bottom: 0.4rem;
}

.progress-block__collections {
    color: var(--wa-color-brand-600);
    font-size: 0.8125rem;
}

/* Location row ------------------------------------------------------------- */
.location-row {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    padding: 0.5rem 0.75rem;
    border: 1px solid var(--wa-color-neutral-200);
    border-radius: var(--wa-border-radius-m);
    font-size: 0.875rem;
    background: var(--wa-color-neutral-fill-quiet);
}

.location-row__icon {
    flex-shrink: 0;
    color: var(--wa-color-warning-500);
}

.location-row__label {
    flex: 1;
    display: flex;
    align-items: baseline;
    gap: 0.35rem;
    min-width: 0;
    overflow: hidden;
}

.location-row__prefix {
    color: var(--wa-color-neutral-500);
    flex-shrink: 0;
}

.location-row__name {
    font-weight: 600;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}

.location-row__root {
    color: var(--wa-color-neutral-500);
    font-style: italic;
}

/* Sticky action bar -------------------------------------------------------- */
.upload-view__action-bar {
    border-top: 1px solid var(--wa-color-surface-border);
    display: flex;
    flex-shrink: 0;
    gap: 0.75rem;
    justify-content: flex-end;
    padding-top: 1rem;
    /* Centred on the content column like the band, so the top border stops at the
     * content edge rather than running the full width of the viewport. */
    margin: 1rem auto 0;
    max-width: var(--content-width);
    width: 100%;
}

/* Pagination (root-file row in preview) ------------------------------------ */
.pagination-controls {
    display: inline-flex;
    align-items: center;
    gap: 0.35rem;
    color: var(--wa-color-neutral-500);
    font-size: 0.8em;
}

.page-btn {
    all: unset;
    cursor: pointer;
    padding: 0 0.3rem;
    border-radius: var(--wa-border-radius-s);
    line-height: 1.5;
    color: var(--wa-color-neutral-600);
}

.page-btn:hover:not(:disabled) {
    background: var(--wa-color-neutral-100);
    color: var(--wa-color-neutral-900);
}

.page-btn:disabled {
    opacity: 0.35;
    cursor: default;
}

.page-label {
    min-width: 3em;
    text-align: center;
}

@keyframes spin {
    to { transform: rotate(360deg); }
}
</style>
