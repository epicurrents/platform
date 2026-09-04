<script setup lang="ts">
import { reactive, ref, computed, onMounted, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useRecordingSelection } from '#composables/useRecordingSelection'
import { t } from '#i18n'
import { setPageTitle } from '#router'
import AccessRightsPanel from '#components/AccessRightsPanel.vue'
import MediaListRow from '#components/MediaListRow.vue'
import EditRecordingDialog from '#components/EditRecordingDialog.vue'
import RecordingListRow from '#components/RecordingListRow.vue'
import RecordingMediaDialog from '#components/RecordingMediaDialog.vue'
import {
    getDataset,
    updateDataset,
    deleteDataset,
    listDatasetItems,
    addDatasetItem,
    removeDatasetItem,
    listDatasetFolders,
    createDatasetFolder,
    updateDatasetFolder,
    deleteDatasetFolder,
    moveDatasetItem,
    listDatasetAccess,
    grantDatasetAccess,
    revokeDatasetAccess,
    getRecordingContentTypeId,
    type Collection,
    type CollectionItem,
    type AccessRight,
    type DatasetFolder,
} from '#api/library'
import { getMediaContentTypeId } from '#api/media'
import { getRecordingDetail, recordingName, type Recording } from '#api/recordings'
import MediaPickerDialog from '#components/MediaPickerDialog.vue'
import { useRecordingsStore } from '#stores/recordings'
import { useLibraryStore } from '#stores/library'
import { useAuthStore } from '#stores/auth'
import { showToast } from '#lib/toast'
import ViewerConfigEditor from '#components/ViewerConfigEditor.vue'
import type { ViewerSettingsOverrides } from '#lib/viewerConfig'

const SCOPE = 'DatasetView'
const route = useRoute()
const router = useRouter()
const recordingsStore = useRecordingsStore()
const libraryStore = useLibraryStore()

// The route param is the dataset's opaque object_hash; the backend also
// resolves the integer PK form, so either value addresses the same routes.
const datasetId = computed(() => String(route.params.id))

// ── Data ─────────────────────────────────────────────────────────────────

const dataset = ref<Collection | null>(null)
const items = ref<CollectionItem[]>([])
const folders = ref<DatasetFolder[]>([])
const accessRights = ref<AccessRight[]>([])
const loading = ref(true)
const error = ref<string | null>(null)

onMounted(async () => {
    try {
        const [ds, its, tree, rights] = await Promise.all([
            getDataset(datasetId.value),
            listDatasetItems(datasetId.value),
            listDatasetFolders(datasetId.value),
            listDatasetAccess(datasetId.value),
        ])
        dataset.value = ds
        items.value = its
        folders.value = tree
        accessRights.value = rights

        if (!recordingsStore.recordings.length) {
            recordingsStore.load()
        }
    } catch {
        error.value = t('Failed to load dataset.', SCOPE)
    } finally {
        loading.value = false
    }
})

// Swap the static "Dataset - Epicurrents" tab title for the actual dataset
// name once it's known. Vue auto-stops the watcher on unmount, so a late-
// arriving fetch on an already-navigated-away view can't write over the
// next route's title.
watch(
    () => dataset.value?.name,
    (name) => {
        if (name) {
            setPageTitle(name)
        }
    },
)

// ── Viewer settings (dataset-level overrides) ─────────────────────────────

const authStore = useAuthStore()
const savingConfig = ref(false)

// Only the dataset owner (or a superuser) edits its viewer overrides; the
// backend enforces the same via can_write_object on the PATCH.
const canEditConfig = computed(() =>
    !!dataset.value
    && (dataset.value.author_id === authStore.user?.id || authStore.isSuperuser),
)

// Folder management shares the same gate: the owner gets the CRUD UI while
// grantees see the tree read-only. The backend enforces write access on every
// mutation regardless.
const canManageFolders = canEditConfig

async function onSaveViewerConfig (overrides: ViewerSettingsOverrides) {
    if (!dataset.value) {
        return
    }
    savingConfig.value = true
    try {
        dataset.value = await updateDataset(datasetId.value, { viewer_config: overrides })
        showToast(t('Viewer settings saved.', SCOPE), 'neutral')
    } catch (err) {
        const detail = (err as { response?: { data?: { detail?: string } } }).response?.data?.detail
        showToast(detail ?? t('Failed to save viewer settings.', SCOPE), 'danger')
    } finally {
        savingConfig.value = false
    }
}

// ── Header actions ────────────────────────────────────────────────────────

const accessPanel = ref<InstanceType<typeof AccessRightsPanel> | null>(null)

function handleHeaderAction(event: Event) {
    const value = (event as CustomEvent<{ item: { value: string } }>).detail.item.value
    if (value === 'edit') {
        openEdit()
    } else if (value === 'open') {
        openDatasetInViewer()
    } else if (value === 'share') {
        accessPanel.value?.openGrantAccess()
    } else if (value === 'delete') {
        showDelete.value = true
    }
}

// ── Navigation ────────────────────────────────────────────────────────────

function goBack() {
    router.push({ name: 'datasets' })
}

function openDatasetInViewer() {
    const { href } = router.resolve({ name: 'viewer', query: { dataset: dataset.value?.object_hash ?? datasetId.value } })
    window.open(href, '_blank')
}

function openRecordings(hashes: string[]) {
    const { href } = router.resolve({ name: 'viewer', query: { files: hashes.join(',') } })
    window.open(href, '_blank')
}

function openMedia(hash: string) {
    const { href } = router.resolve({ name: 'viewer', query: { media: hash } })
    window.open(href, '_blank')
}

// ── Selection ─────────────────────────────────────────────────────────────

// Derive from the rendered rows so shift-click ranges follow visual order
// once folders regroup the list.
const selectableHashes = computed(() =>
    displayRows.value
        .map(row => row.item?.object_hash)
        .filter((h): h is string => !!h),
)
const listRef = ref<HTMLElement | null>(null)
const { selected, focusedHash, onRowClick, onCheckboxClick, clearSelection } = useRecordingSelection(
    () => selectableHashes.value,
    listRef,
)

function openSelectionInViewer() {
    openRecordings([...selected.value])
    clearSelection()
}

// ── Folder tree ───────────────────────────────────────────────────────────

/** One rendered line of the items section: a folder header or an item, with its tree depth. */
interface DisplayRow {
    kind: 'folder' | 'item'
    folder?: DatasetFolder
    item?: CollectionItem
    depth: number
}

/**
 * Flatten the folder tree and the items into display order: root items first,
 * then each folder (depth-first, in the backend's sibling order) followed by
 * its items. Items pointing at a folder that no longer exists fall back to
 * the root group.
 */
const displayRows = computed<DisplayRow[]>(() => {
    const byParent = new Map<number | null, DatasetFolder[]>()
    for (const folder of folders.value) {
        const list = byParent.get(folder.parent_id) ?? []
        list.push(folder)
        byParent.set(folder.parent_id, list)
    }
    const folderIds = new Set(folders.value.map(f => f.id))
    const itemsByFolder = new Map<number | null, CollectionItem[]>()
    for (const item of items.value) {
        const key = item.folder_id !== null && folderIds.has(item.folder_id) ? item.folder_id : null
        const list = itemsByFolder.get(key) ?? []
        list.push(item)
        itemsByFolder.set(key, list)
    }
    const rows: DisplayRow[] = []
    for (const item of itemsByFolder.get(null) ?? []) {
        rows.push({ kind: 'item', item, depth: 0 })
    }
    const walk = (parentId: number | null, depth: number) => {
        for (const folder of byParent.get(parentId) ?? []) {
            rows.push({ kind: 'folder', folder, depth })
            for (const item of itemsByFolder.get(folder.id) ?? []) {
                rows.push({ kind: 'item', item, depth: depth + 1 })
            }
            walk(folder.id, depth + 1)
        }
    }
    walk(null, 0)
    return rows
})

/** Ids of *folderId* and every folder beneath it — used to exclude a folder's own subtree from its parent options. */
function folderSubtreeIds(folderId: number): Set<number> {
    const ids = new Set([folderId])
    let grew = true
    while (grew) {
        grew = false
        for (const folder of folders.value) {
            if (folder.parent_id !== null && ids.has(folder.parent_id) && !ids.has(folder.id)) {
                ids.add(folder.id)
                grew = true
            }
        }
    }
    return ids
}

/** Indent a folder select option to its tree depth with em-spaces. */
function folderOptionLabel(option: { folder: DatasetFolder; depth: number }) {
    return `${'\u2003'.repeat(option.depth)}${option.folder.name}`
}

/** Folders in display order with their depth, for indented select options. */
const folderOptions = computed(() => {
    return displayRows.value
        .filter(row => row.kind === 'folder' && row.folder)
        .map(row => ({ folder: row.folder as DatasetFolder, depth: row.depth }))
})

// ── Create / edit folder ─────────────────────────────────────────────────

const showFolderDialog = ref(false)
const folderDialogLoading = ref(false)
const folderDialogError = ref<string | null>(null)
const editingFolder = ref<DatasetFolder | null>(null)
const folderInput = reactive({ name: '', parentId: '' })

/** Parent options for the dialog: everything except the edited folder's own subtree. */
const folderParentOptions = computed(() => {
    if (!editingFolder.value) {
        return folderOptions.value
    }
    const excluded = folderSubtreeIds(editingFolder.value.id)
    return folderOptions.value.filter(option => !excluded.has(option.folder.id))
})

function openCreateFolder() {
    editingFolder.value = null
    folderInput.name = ''
    folderInput.parentId = ''
    folderDialogError.value = null
    showFolderDialog.value = true
}

function openEditFolder(folder: DatasetFolder) {
    editingFolder.value = folder
    folderInput.name = folder.name
    folderInput.parentId = folder.parent_id === null ? '' : String(folder.parent_id)
    folderDialogError.value = null
    showFolderDialog.value = true
}

function closeFolderDialog() {
    showFolderDialog.value = false
}

async function submitFolder() {
    const name = folderInput.name.trim()
    if (!name) {
        folderDialogError.value = t('Name is required.', SCOPE)
        return
    }
    const parentId = folderInput.parentId ? Number(folderInput.parentId) : null
    folderDialogLoading.value = true
    folderDialogError.value = null
    try {
        if (editingFolder.value) {
            const updated = await updateDatasetFolder(datasetId.value, editingFolder.value.id, {
                name,
                parent_id: parentId,
            })
            folders.value = folders.value.map(f => (f.id === updated.id ? updated : f))
        } else {
            folders.value.push(await createDatasetFolder(datasetId.value, { name, parent_id: parentId }))
        }
        showFolderDialog.value = false
    } catch {
        folderDialogError.value = t('Failed to save folder.', SCOPE)
    } finally {
        folderDialogLoading.value = false
    }
}

// ── Delete folder ────────────────────────────────────────────────────────

const deletingFolder = ref<DatasetFolder | null>(null)
const deleteFolderLoading = ref(false)

function closeDeleteFolder() {
    deletingFolder.value = null
}

async function confirmDeleteFolder() {
    if (!deletingFolder.value) {
        return
    }
    deleteFolderLoading.value = true
    try {
        const removed = folderSubtreeIds(deletingFolder.value.id)
        await deleteDatasetFolder(datasetId.value, deletingFolder.value.id)
        folders.value = folders.value.filter(f => !removed.has(f.id))
        for (const item of items.value) {
            if (item.folder_id !== null && removed.has(item.folder_id)) {
                item.folder_id = null
            }
        }
        deletingFolder.value = null
    } catch {
        showToast(t('Failed to delete folder.', SCOPE), 'danger')
    } finally {
        deleteFolderLoading.value = false
    }
}

function onFolderAction(event: Event, folder: DatasetFolder) {
    const value = (event as CustomEvent<{ item: { value: string } }>).detail.item.value
    if (value === 'edit') {
        openEditFolder(folder)
    } else if (value === 'delete') {
        deletingFolder.value = folder
    }
}

// ── Move item to folder ──────────────────────────────────────────────────

const movingItem = ref<CollectionItem | null>(null)
const moveItemLoading = ref(false)
const moveInput = reactive({ folderId: '' })

function openMoveItem(item: CollectionItem) {
    movingItem.value = item
    moveInput.folderId = item.folder_id === null ? '' : String(item.folder_id)
}

function closeMoveItem() {
    movingItem.value = null
}

async function submitMoveItem() {
    if (!movingItem.value) {
        return
    }
    moveItemLoading.value = true
    try {
        const folderId = moveInput.folderId ? Number(moveInput.folderId) : null
        const updated = await moveDatasetItem(datasetId.value, movingItem.value.id, folderId)
        const row = items.value.find(i => i.id === updated.id)
        if (row) {
            row.folder_id = updated.folder_id
        }
        movingItem.value = null
    } catch {
        showToast(t('Failed to move item.', SCOPE), 'danger')
    } finally {
        moveItemLoading.value = false
    }
}

// ── Edit dataset ──────────────────────────────────────────────────────────

const showEdit = ref(false)
const editLoading = ref(false)
const editError = ref<string | null>(null)
const input = reactive({ editName: '', editDescription: '' })

function openEdit() {
    if (!dataset.value) {
        return
    }
    input.editName = dataset.value.name
    input.editDescription = dataset.value.description
    editError.value = null
    showEdit.value = true
}

function closeEdit() {
    showEdit.value = false
}

async function submitEdit() {
    if (!input.editName.trim()) {
        editError.value = t('Name is required.', SCOPE)
        return
    }
    editLoading.value = true
    editError.value = null
    try {
        dataset.value = await updateDataset(datasetId.value, {
            name: input.editName.trim(),
            description: input.editDescription.trim(),
        })
        showEdit.value = false
        showToast(t('Dataset updated.', SCOPE), 'success')
    } catch {
        editError.value = t('Failed to update dataset.', SCOPE)
    } finally {
        editLoading.value = false
    }
}

// ── Delete dataset ────────────────────────────────────────────────────────

const showDelete = ref(false)
const deleteLoading = ref(false)

function closeDelete() {
    showDelete.value = false
}

async function confirmDelete() {
    deleteLoading.value = true
    try {
        await deleteDataset(datasetId.value)
        if (dataset.value) {
            libraryStore.removeDataset(dataset.value.id)
        }
        showToast(t('"{name}" moved to trash.', SCOPE, { name: dataset.value?.name ?? '' }), 'neutral')
        router.push({ name: 'datasets' })
    } catch {
        showToast(t('Failed to delete dataset.', SCOPE), 'danger')
        deleteLoading.value = false
    }
}

// ── Add recording ─────────────────────────────────────────────────────────

const showAddItem = ref(false)
const addItemSelectedHashes = ref<string[]>([])
const addItemLoading = ref(false)
const addItemError = ref<string | null>(null)

const addableRecordings = computed(() => {
    const existingHashes = new Set(items.value.map(i => i.object_hash).filter(Boolean))
    return recordingsStore.recordings.filter(r => !existingHashes.has(r.hash))
})

function openAddItem() {
    showAddItem.value = true
}

function closeAddItem() {
    showAddItem.value = false
    addItemSelectedHashes.value = []
    addItemError.value = null
}

async function submitAddItem() {
    if (!addItemSelectedHashes.value.length) {
        addItemError.value = t('Select at least one recording.', SCOPE)
        return
    }
    addItemLoading.value = true
    addItemError.value = null
    try {
        const ctId = await getRecordingContentTypeId()
        const results = await Promise.allSettled(
            addItemSelectedHashes.value.map(hash =>
                addDatasetItem(datasetId.value, { content_type_id: ctId, object_id: hash })
            )
        )
        results.forEach(r => {
            if (r.status === 'fulfilled') {
                items.value.push(r.value)
            }
        })
        const failed = results.filter(r => r.status === 'rejected')
        if (failed.length) {
            addItemError.value = t('{count} recording(s) could not be added.', SCOPE, { count: failed.length })
            return
        }
        showAddItem.value = false
        addItemSelectedHashes.value = []
    } finally {
        addItemLoading.value = false
    }
}

// ── Add-new menu dispatcher ──────────────────────────────────────────────

function handleAddNew (event: Event) {
    const value = (event as CustomEvent<{ item: { value: string } }>).detail.item.value
    if (value === 'recording') {
        openAddItem()
    } else if (value === 'media') {
        openAddMedia()
    } else if (value === 'folder') {
        openCreateFolder()
    }
}

// ── Add media file ─────────────────────────────────────────────────────────

const showAddMedia = ref(false)

/** Hashes already on this dataset; the picker excludes them from its list. */
const datasetMediaHashes = computed(
    () => items.value.map(i => i.object_hash).filter((h): h is string => !!h),
)

function openAddMedia () {
    showAddMedia.value = true
}

function closeAddMedia () {
    showAddMedia.value = false
}

async function onAddMediaSubmit (hashes: string[]) {
    try {
        const ctId = await getMediaContentTypeId()
        const results = await Promise.allSettled(
            hashes.map(hash =>
                addDatasetItem(datasetId.value, { content_type_id: ctId, object_id: hash })
            )
        )
        results.forEach(r => {
            if (r.status === 'fulfilled') {
                items.value.push(r.value)
            }
        })
        const failed = results.filter(r => r.status === 'rejected')
        if (failed.length) {
            showToast(
                t('{count} media file(s) could not be added.', SCOPE, { count: failed.length }),
                'warning',
            )
        }
        showAddMedia.value = false
    } catch {
        showToast(t('Failed to add media files.', SCOPE), 'danger')
    }
}

async function removeItem(item: CollectionItem) {
    try {
        await removeDatasetItem(datasetId.value, item.id)
        items.value = items.value.filter(i => i.id !== item.id)
    } catch {
        showToast(t('Failed to remove item.', SCOPE), 'danger')
    }
}

const attachMediaItem = ref<CollectionItem | null>(null)

/** Route the recording row menu: attach media opens the dialog, anything else removes. */
function onRecordingAction(value: string, item: CollectionItem) {
    if (value === 'attach-media') {
        attachMediaItem.value = item
    } else if (value === 'edit') {
        openEditRecording(item)
    } else if (value === 'move-folder') {
        openMoveItem(item)
    } else {
        removeItem(item)
    }
}

/** Route the media row menu: move to folder opens the dialog, anything else removes. */
function onMediaAction(value: string, item: CollectionItem) {
    if (value === 'move-folder') {
        openMoveItem(item)
    } else {
        removeItem(item)
    }
}

// ── Edit recording ─────────────────────────────────────────────────────────

const editingRec = ref<Recording | null>(null)

// The dataset list only carries the item's hash and name, so fetch the full
// recording before opening the shared edit dialog.
async function openEditRecording(item: CollectionItem) {
    if (!item.object_hash) {
        return
    }
    try {
        editingRec.value = await getRecordingDetail(item.object_hash)
    } catch {
        showToast(t('Failed to load recording.', SCOPE), 'danger')
    }
}

function onRecordingEdited(updated: Recording) {
    const item = items.value.find(i => i.object_hash === updated.hash)
    if (item) {
        item.object_name = recordingName(updated)
    }
}

// ── Access rights ─────────────────────────────────────────────────────────

const grantFn = (payload: Parameters<typeof grantDatasetAccess>[1]) =>
    grantDatasetAccess(datasetId.value, payload)

const revokeFn = (right: AccessRight) =>
    revokeDatasetAccess(datasetId.value, right.id)
</script>

<template>
    <main class="dataset-view">
        <wa-spinner v-if="loading" class="loading-center"></wa-spinner>
        <wa-callout v-else-if="error" class="dataset-view__error" variant="danger">{{ error }}</wa-callout>

        <wa-scroller v-else-if="dataset" orientation="vertical">
            <div class="dataset-view__scroll-wrap">
                <header class="page-header">
                <div class="page-header-start">
                    <h1>{{ dataset.name }}</h1>
                </div>
                <wa-button
                    appearance="plain"
                    class="go-back-button"
                    size="s"
                    variant="text"
                    @click="goBack"
                >
                    <wa-icon name="arrow-left" slot="start"></wa-icon>
                    {{ t('Back', SCOPE) }}
                </wa-button>
                <wa-dropdown
                    placement="bottom-end"
                    @click.stop
                    @wa-select="handleHeaderAction"
                >
                    <wa-button appearance="plain" slot="trigger" variant="neutral">
                        <wa-icon name="ellipsis"></wa-icon>
                    </wa-button>
                    <wa-dropdown-item :disabled="!items.length" value="open">
                        <wa-icon name="arrow-up-right-from-square" slot="icon" variant="brand"></wa-icon>
                        {{ t('Open in viewer', SCOPE) }}
                    </wa-dropdown-item>
                    <wa-dropdown-item value="edit">
                        <wa-icon name="pencil" slot="icon"></wa-icon>
                        {{ t('Edit', SCOPE) }}
                    </wa-dropdown-item>
                    <wa-dropdown-item value="share">
                        <wa-icon name="share" slot="icon"></wa-icon>
                        {{ t('Grant access', SCOPE) }}
                    </wa-dropdown-item>
                    <wa-dropdown-item value="delete" variant="danger">
                        <wa-icon name="trash" slot="icon"></wa-icon>
                        {{ t('Move to trash', SCOPE) }}
                    </wa-dropdown-item>
                </wa-dropdown>
            </header>

            <p v-if="dataset.description" class="dataset-description">{{ dataset.description }}</p>

            <!-- ── Items section ──────────────────────────────────────── -->
            <div class="section-header">
                <h2>{{ t('Items', SCOPE) }}</h2>
                <wa-dropdown
                    placement="bottom-end"
                    @wa-select="handleAddNew"
                >
                    <wa-button
                        appearance="plain"
                        size="s"
                        slot="trigger"
                        variant="brand"
                    >
                        <wa-icon name="plus" slot="start"></wa-icon>
                        {{ t('Add new', SCOPE) }}
                    </wa-button>
                    <wa-dropdown-item value="recording">
                        <wa-icon name="file-music" slot="icon"></wa-icon>
                        {{ t('Recording', SCOPE) }}
                    </wa-dropdown-item>
                    <wa-dropdown-item value="media">
                        <wa-icon name="file" slot="icon"></wa-icon>
                        {{ t('Media file', SCOPE) }}
                    </wa-dropdown-item>
                    <wa-dropdown-item v-if="canManageFolders" value="folder">
                        <wa-icon name="folder-plus" slot="icon"></wa-icon>
                        {{ t('Folder', SCOPE) }}
                    </wa-dropdown-item>
                </wa-dropdown>
            </div>

            <p v-if="!items.length" class="empty-state recordings-empty">
                {{ t('No items in this dataset yet.', SCOPE) }}
            </p>

            <div v-else class="list-rows" ref="listRef">
                <template v-for="row in displayRows" :key="row.kind === 'folder' ? `f${row.folder!.id}` : `i${row.item!.id}`">
                    <div v-if="row.kind === 'folder' && row.folder"
                        class="folder-row"
                        :style="{ paddingLeft: `${row.depth * 1.5}rem` }"
                    >
                        <div class="folder-row__main">
                            <wa-icon class="folder-row__icon" name="folder"></wa-icon>
                            <span class="folder-row__name">{{ row.folder.name }}</span>
                        </div>
                        <wa-dropdown v-if="canManageFolders"
                            placement="bottom-end"
                            @click.stop
                            @wa-select="onFolderAction($event, row.folder)"
                        >
                            <wa-button appearance="plain" size="s" slot="trigger" variant="neutral">
                                <wa-icon name="ellipsis"></wa-icon>
                            </wa-button>
                            <wa-dropdown-item value="edit">
                                <wa-icon name="pencil" slot="icon"></wa-icon>
                                {{ t('Rename or move', SCOPE) }}
                            </wa-dropdown-item>
                            <wa-dropdown-item value="delete" variant="danger">
                                <wa-icon name="trash" slot="icon"></wa-icon>
                                {{ t('Delete folder', SCOPE) }}
                            </wa-dropdown-item>
                        </wa-dropdown>
                    </div>
                    <template v-else-if="row.item">
                        <div v-for="item in [row.item]"
                            class="tree-row"
                            :key="item.id"
                            :style="{ paddingLeft: `${row.depth * 1.5}rem` }"
                        >
                            <RecordingListRow v-if="item.object_type === 'recording' && item.object_hash"
                                :hash="item.object_hash"
                                :is-focused="focusedHash === item.object_hash"
                                :is-selected="selected.has(item.object_hash)"
                                :name="item.object_name ?? t('Item #{id}', SCOPE, { id: item.id })"
                                :selection-active="selected.size > 0"
                                @checkbox-click="onCheckboxClick(item.object_hash)"
                                @dropdown-action="onRecordingAction($event, item)"
                                @open="openRecordings([item.object_hash])"
                                @row-click="onRowClick(item.object_hash, $event)"
                                @row-dblclick="openRecordings([item.object_hash])"
                            >
                                <template #actions>
                                    <wa-dropdown-item value="edit">
                                        <wa-icon name="pencil" slot="icon"></wa-icon>
                                        {{ t('Edit', SCOPE) }}
                                    </wa-dropdown-item>
                                    <wa-dropdown-item value="attach-media">
                                        <wa-icon name="paperclip" slot="icon"></wa-icon>
                                        {{ t('Attach media', SCOPE) }}
                                    </wa-dropdown-item>
                                    <wa-dropdown-item v-if="canManageFolders && folders.length" value="move-folder">
                                        <wa-icon name="folder-open" slot="icon"></wa-icon>
                                        {{ t('Move to folder', SCOPE) }}
                                    </wa-dropdown-item>
                                    <wa-dropdown-item value="remove" variant="danger">
                                        <wa-icon name="xmark" slot="icon"></wa-icon>
                                        {{ t('Remove from dataset', SCOPE) }}
                                    </wa-dropdown-item>
                                </template>
                            </RecordingListRow>
                            <MediaListRow v-else-if="item.object_type === 'mediafile' && item.object_hash"
                                :file-extension="item.file_extension"
                                :hash="item.object_hash"
                                :is-focused="false"
                                :is-selected="false"
                                :is-supported="item.is_supported ?? false"
                                :name="item.object_name ?? t('Item #{id}', SCOPE, { id: item.id })"
                                @dropdown-action="onMediaAction($event, item)"
                                @open="openMedia(item.object_hash)"
                                @row-dblclick="openMedia(item.object_hash)"
                            >
                                <template #actions>
                                    <wa-dropdown-item v-if="canManageFolders && folders.length" value="move-folder">
                                        <wa-icon name="folder-open" slot="icon"></wa-icon>
                                        {{ t('Move to folder', SCOPE) }}
                                    </wa-dropdown-item>
                                    <wa-dropdown-item value="remove" variant="danger">
                                        <wa-icon name="xmark" slot="icon"></wa-icon>
                                        {{ t('Remove from dataset', SCOPE) }}
                                    </wa-dropdown-item>
                                </template>
                            </MediaListRow>
                            <div v-else class="list-row">
                                <div class="list-row-main">
                                    <wa-icon class="icon-muted" name="file"></wa-icon>
                                    <span class="list-row-name">
                                        {{ item.object_name ?? t('Item #{id}', SCOPE, { id: item.id }) }}
                                    </span>
                                </div>
                            </div>
                        </div>
                    </template>
                </template>
                <!-- Selection action bar -->
                <div v-if="selected.size > 0" class="selection-bar">
                    <span class="selection-bar__count">
                        {{ t('{count} selected', SCOPE, { count: selected.size }) }}
                    </span>
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

            <wa-divider class="section-divider"></wa-divider>

            <!-- ── Access rights section ──────────────────────────────── -->
            <AccessRightsPanel
                ref="accessPanel"
                v-model:accessRights="accessRights"
                :grantFn="grantFn"
                :infoMessage="t('Anyone with read access to this dataset can read all recordings in it, including recordings added in the future.', SCOPE)"
                :readPermLabel="t('Read (grants access to all recordings)', SCOPE)"
                :revokeFn="revokeFn"
                :tokenHint="t('Provide this token to users so they can access the dataset.', SCOPE)"
                :tokenPlaceholder="t('e.g. team-neuro-2025', SCOPE)"
            ></AccessRightsPanel>

            <template v-if="canEditConfig">
                <wa-divider class="section-divider"></wa-divider>
                <wa-details :summary="t('Viewer settings', SCOPE)">
                    <p class="dataset-viewer-config__hint">
                        {{ t('Override viewer defaults for this dataset. These apply on top of the deployment defaults whenever the dataset is opened in the viewer. Leave empty to use the deployment defaults.', SCOPE) }}
                    </p>
                    <ViewerConfigEditor
                        :overrides="dataset.viewer_config"
                        :saving="savingConfig"
                        @save="onSaveViewerConfig"
                    ></ViewerConfigEditor>
                </wa-details>
            </template>
            </div>
        </wa-scroller>
    </main>

    <!-- Edit dialog -->
    <wa-dialog
        :label="t('Edit dataset', SCOPE)"
        :open="showEdit"
        @wa-hide.self="closeEdit"
    >
        <div class="dialog-form">
            <wa-callout v-if="editError" variant="danger">{{ editError }}</wa-callout>
            <wa-input
                :disabled="editLoading"
                :label="t('Name', SCOPE)"
                size="s"
                type="text"
                v-wa="[input, 'editName']"
            ></wa-input>
            <wa-textarea
                :disabled="editLoading"
                :label="t('Description', SCOPE)"
                rows="3"
                size="s"
                v-wa="[input, 'editDescription']"
            ></wa-textarea>
        </div>
        <div slot="footer" class="form-actions">
            <wa-button
                appearance="filled-outlined"
                :disabled="editLoading"
                variant="neutral"
                @click="closeEdit"
            >
                {{ t('Cancel', SCOPE) }}
            </wa-button>
            <wa-button
                appearance="filled-outlined"
                :loading="editLoading"
                variant="brand"
                @click="submitEdit"
            >
                {{ t('Save', SCOPE) }}
            </wa-button>
        </div>
    </wa-dialog>

    <!-- Delete dialog -->
    <wa-dialog
        :label="t('Move to trash', SCOPE)"
        :open="showDelete"
        @wa-hide.self="closeDelete"
    >
        <i18n-t class="dialog-text" keypath="DatasetView.move_to_trash_confirm" tag="p">
            <template #name>
                <strong>{{ dataset?.name }}</strong>
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

    <!-- Add recording dialog -->
    <wa-dialog
        :label="t('Add recording', SCOPE)"
        :open="showAddItem"
        @wa-hide.self="closeAddItem"
    >
        <div class="dialog-form">
            <wa-callout v-if="addItemError" variant="danger">{{ addItemError }}</wa-callout>
            <p v-if="!addableRecordings.length" class="empty-note">
                {{ t('All available recordings are already in this dataset.', SCOPE) }}
            </p>
            <template v-else>
                <label class="select-label">{{ t('Recordings', SCOPE) }}</label>
                <select
                    class="recording-select"
                    :disabled="addItemLoading"
                    multiple
                    v-model="addItemSelectedHashes"
                >
                    <option v-for="rec in addableRecordings" :key="rec.hash" :value="rec.hash">
                        {{ recordingName(rec) }}
                    </option>
                </select>
            </template>
        </div>
        <div slot="footer" class="form-actions">
            <wa-button
                appearance="filled-outlined"
                :disabled="addItemLoading"
                variant="neutral"
                @click="closeAddItem"
            >
                {{ t('Cancel', SCOPE) }}
            </wa-button>
            <wa-button
                appearance="filled-outlined"
                :disabled="!addItemSelectedHashes.length || !addableRecordings.length"
                :loading="addItemLoading"
                variant="brand"
                @click="submitAddItem"
            >
                {{ t('Add', SCOPE) }}
            </wa-button>
        </div>
    </wa-dialog>

    <!-- Add media dialog -->
    <MediaPickerDialog
        :exclude-hashes="datasetMediaHashes"
        :open="showAddMedia"
        @add="onAddMediaSubmit"
        @close="closeAddMedia"
    ></MediaPickerDialog>

    <!-- Attach media to recording dialog -->
    <RecordingMediaDialog
        :open="!!attachMediaItem"
        :recording-hash="attachMediaItem?.object_hash ?? ''"
        :recording-name="attachMediaItem?.object_name ?? ''"
        @close="attachMediaItem = null"
    ></RecordingMediaDialog>

    <!-- Edit recording dialog -->
    <EditRecordingDialog
        :recording="editingRec"
        @close="editingRec = null"
        @updated="onRecordingEdited"
    />

    <!-- Create / edit folder dialog -->
    <wa-dialog
        :label="editingFolder ? t('Edit folder', SCOPE) : t('New folder', SCOPE)"
        :open="showFolderDialog"
        @wa-hide.self="closeFolderDialog"
    >
        <div class="dialog-form">
            <wa-callout v-if="folderDialogError" variant="danger">{{ folderDialogError }}</wa-callout>
            <wa-input
                :disabled="folderDialogLoading"
                :label="t('Name', SCOPE)"
                size="s"
                type="text"
                v-wa="[folderInput, 'name']"
            ></wa-input>
            <wa-select
                :disabled="folderDialogLoading"
                :label="t('Parent folder', SCOPE)"
                size="s"
                v-wa="[folderInput, 'parentId']"
            >
                <wa-option value="">{{ t('Dataset root', SCOPE) }}</wa-option>
                <wa-option v-for="option in folderParentOptions" :key="option.folder.id" :value="String(option.folder.id)">
                    {{ folderOptionLabel(option) }}
                </wa-option>
            </wa-select>
        </div>
        <div slot="footer" class="form-actions">
            <wa-button
                appearance="filled-outlined"
                :disabled="folderDialogLoading"
                variant="neutral"
                @click="closeFolderDialog"
            >
                {{ t('Cancel', SCOPE) }}
            </wa-button>
            <wa-button
                appearance="filled-outlined"
                :loading="folderDialogLoading"
                variant="brand"
                @click="submitFolder"
            >
                {{ editingFolder ? t('Save', SCOPE) : t('Create', SCOPE) }}
            </wa-button>
        </div>
    </wa-dialog>

    <!-- Delete folder dialog -->
    <wa-dialog
        :label="t('Delete folder', SCOPE)"
        :open="!!deletingFolder"
        @wa-hide.self="closeDeleteFolder"
    >
        <p>
            {{ t('The folder and its sub-folders are deleted. The items inside are not removed from the dataset — they move back to the dataset root.', SCOPE) }}
        </p>
        <div slot="footer" class="form-actions">
            <wa-button
                appearance="filled-outlined"
                :disabled="deleteFolderLoading"
                variant="neutral"
                @click="closeDeleteFolder"
            >
                {{ t('Cancel', SCOPE) }}
            </wa-button>
            <wa-button
                appearance="filled-outlined"
                :loading="deleteFolderLoading"
                variant="danger"
                @click="confirmDeleteFolder"
            >
                {{ t('Delete folder', SCOPE) }}
            </wa-button>
        </div>
    </wa-dialog>

    <!-- Move item to folder dialog -->
    <wa-dialog
        :label="t('Move to folder', SCOPE)"
        :open="!!movingItem"
        @wa-hide.self="closeMoveItem"
    >
        <div class="dialog-form">
            <wa-select
                :disabled="moveItemLoading"
                :label="t('Folder', SCOPE)"
                size="s"
                v-wa="[moveInput, 'folderId']"
            >
                <wa-option value="">{{ t('Dataset root', SCOPE) }}</wa-option>
                <wa-option v-for="option in folderOptions" :key="option.folder.id" :value="String(option.folder.id)">
                    {{ folderOptionLabel(option) }}
                </wa-option>
            </wa-select>
        </div>
        <div slot="footer" class="form-actions">
            <wa-button
                appearance="filled-outlined"
                :disabled="moveItemLoading"
                variant="neutral"
                @click="closeMoveItem"
            >
                {{ t('Cancel', SCOPE) }}
            </wa-button>
            <wa-button
                appearance="filled-outlined"
                :loading="moveItemLoading"
                variant="brand"
                @click="submitMoveItem"
            >
                {{ t('Move', SCOPE) }}
            </wa-button>
        </div>
    </wa-dialog>

</template>

<style scoped>
.dataset-view {
    /* Width of the centred content column, shared by the scroller's inner wrap and
     * the error callout. */
    --content-width: 960px;
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
    padding: var(--wa-space-xl) var(--wa-space-m);
}

.dataset-view__scroll-wrap {
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

.dataset-view__error {
    /* Held to the content column now that the host no longer centres its children. */
    margin: 0 auto;
    max-width: var(--content-width);
    width: 100%;
}

.dataset-description {
    color: var(--wa-color-text-quiet);
    margin-bottom: 1.5rem;
    margin-top: -0.75rem;
}

.recordings-empty {
    padding: 1.5rem 0;
}

.folder-row {
    align-items: center;
    display: flex;
    gap: 0.5rem;
    justify-content: space-between;
    padding-bottom: 0.25rem;
    padding-top: 0.75rem;
}

.folder-row__main {
    align-items: center;
    display: flex;
    gap: 0.5rem;
    min-width: 0;
}

.folder-row__icon {
    color: var(--wa-color-text-quiet);
    flex-shrink: 0;
}

.folder-row__name {
    font-weight: var(--wa-font-weight-semibold);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}

.tree-row > * {
    width: 100%;
}

.icon-muted {
    color: var(--wa-color-text-quiet);
    flex-shrink: 0;
}

.dialog-form {
    display: flex;
    flex-direction: column;
    gap: 1rem;
}

.dialog-text {
    margin: 0;
}

.empty-note {
    color: var(--wa-color-text-quiet);
    margin: 0;
}

.select-label {
    font-size: 0.875rem;
    font-weight: 500;
}

.recording-select {
    border: 1px solid var(--wa-color-neutral-border-normal);
    border-radius: 6px;
    font-size: 0.875rem;
    min-height: 12rem;
    max-height: 24rem;
    overflow-y: auto;
    padding: 0.25rem 0;
    width: 100%;
}

.recording-select option {
    padding: 0.3rem 0.75rem;
}

.dataset-viewer-config__hint {
    color: var(--wa-color-text-quiet);
    font-size: 0.9rem;
    margin: 0 0 0.75rem;
}
</style>
