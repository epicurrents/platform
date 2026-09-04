<script setup lang="ts">
import { reactive, ref, computed, onMounted, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useRecordingSelection } from '#composables/useRecordingSelection'
import { t } from '#i18n'
import { setPageTitle } from '#router'
import CollectionPickerDialog, { type PickerSelection, type CollectionSelection } from '#components/CollectionPickerDialog.vue'
import CreateDatasetDialog from '#components/CreateDatasetDialog.vue'
import MediaListRow from '#components/MediaListRow.vue'
import EditRecordingDialog from '#components/EditRecordingDialog.vue'
import RecordingListRow from '#components/RecordingListRow.vue'
import RecordingMediaDialog from '#components/RecordingMediaDialog.vue'
import {
    getCollection,
    updateCollection,
    deleteCollection,
    bulkRenameCollectionRecordings,
    listCollections,
    createCollection,
    listCollectionItems,
    addCollectionItem,
    removeCollectionItem,
    moveCollectionItem,
    exportCollectionToDataset,
    getRecordingContentTypeId,
    type Collection,
    type CollectionItem,
} from '#api/library'
import { getMediaContentTypeId } from '#api/media'
import { getRecordingDetail, recordingName, type Recording } from '#api/recordings'
import MediaPickerDialog from '#components/MediaPickerDialog.vue'
import { useRecordingsStore } from '#stores/recordings'
import { useLibraryStore } from '#stores/library'
import { showToast } from '#lib/toast'

const SCOPE = 'CollectionView'
const route = useRoute()
const router = useRouter()
const recordingsStore = useRecordingsStore()
const libraryStore = useLibraryStore()

const collectionId = computed(() => Number(route.params.id))

// ── Data ─────────────────────────────────────────────────────────────────

const collection = ref<Collection | null>(null)
const items = ref<CollectionItem[]>([])
const subCollections = ref<Collection[]>([])
const loading = ref(true)
const error = ref<string | null>(null)

async function loadCollection () {
    loading.value = true
    error.value = null
    try {
        const [col, its, subs] = await Promise.all([
            getCollection(collectionId.value),
            listCollectionItems(collectionId.value),
            listCollections({ parent_id: collectionId.value }),
        ])
        collection.value = col
        items.value = its
        subCollections.value = subs

        if (!recordingsStore.recordings.length) {
            recordingsStore.load()
        }
    } catch {
        error.value = t('Failed to load collection.', SCOPE)
    } finally {
        loading.value = false
    }
}

onMounted(loadCollection)

// The route component is reused across /collection/:id transitions (no
// :key on RouterView), so a watcher is needed to re-fetch when the
// caller navigates into a subcollection or back up the tree.
watch(collectionId, (id) => {
    if (Number.isFinite(id)) {
        clearAllSelection()
        loadCollection()
    }
})

// Swap the static "Collection - Epicurrents" tab title for the actual
// collection name once it's known. Vue auto-stops the watcher on unmount,
// so a late-arriving fetch on an already-navigated-away view can't write
// over the next route's title.
watch(
    () => collection.value?.name,
    (name) => {
        if (name) {
            setPageTitle(name)
        }
    },
)

// ── Header actions ────────────────────────────────────────────────────────

function handleHeaderAction (event: Event) {
    const value = (event as CustomEvent<{ item: { value: string } }>).detail.item.value
    if (value === 'edit') {
        openEdit()
    } else if (value === 'create-dataset') {
        openCreateDataset()
    } else if (value === 'export-dataset') {
        openExport()
    } else if (value === 'number-recordings') {
        openNumberDialog()
    } else if (value === 'delete') {
        showDelete.value = true
    }
}

// ── Number recordings (bulk rename) ────────────────────────────────────────

const showNumber = ref(false)
const numberInput = reactive({ prefix: 'Recording' })
const numberLoading = ref(false)
const numberError = ref<string | null>(null)

/** Live preview of the first two names the current prefix would produce. */
const numberExamples = computed(() => {
    const p = numberInput.prefix.trim() || 'Recording'
    return { example1: `${p} 1`, example2: `${p} 2` }
})

function openNumberDialog () {
    numberInput.prefix = 'Recording'
    numberError.value = null
    showNumber.value = true
}

async function submitNumber () {
    const prefix = numberInput.prefix.trim() || 'Recording'
    numberLoading.value = true
    numberError.value = null
    try {
        const { renamed, skipped } = await bulkRenameCollectionRecordings(collectionId.value, prefix)
        showNumber.value = false
        await loadCollection()
        if (skipped > 0) {
            showToast(
                t('Renamed {renamed}; skipped {skipped} you cannot edit.', SCOPE, { renamed, skipped }),
                'warning',
            )
        } else {
            showToast(t('Renamed {renamed} recordings.', SCOPE, { renamed }), 'success')
        }
    } catch {
        numberError.value = t('Failed to rename recordings. Please try again.', SCOPE)
    } finally {
        numberLoading.value = false
    }
}

function enterSubcollection (sub: Collection) {
    router.push({ name: 'collection', params: { id: sub.id } })
}

function goBack () {
    if (collection.value?.parent_id) {
        router.push({ name: 'collection', params: { id: collection.value.parent_id } })
    } else {
        router.push({ name: 'library' })
    }
}

// ── Edit collection ───────────────────────────────────────────────────────

const showEdit = ref(false)
const editLoading = ref(false)
const editError = ref<string | null>(null)
const input = reactive({
    editName: '',
    editDescription: '',
    addItemFilter: '',
    newCollectionName: '',
    newCollectionDescription: '',
})

function openRecordings (hashes: string[]) {
    const { href } = router.resolve({ name: 'viewer', query: { files: hashes.join(',') } })
    window.open(href, '_blank')
}

function openMedia (hash: string) {
    const { href } = router.resolve({ name: 'viewer', query: { media: hash } })
    window.open(href, '_blank')
}

const selectableHashes = computed(() =>
    items.value.map(i => i.object_hash).filter((h): h is string => !!h),
)
const listRef = ref<HTMLElement | null>(null)
const { selected, focusedHash, onRowClick, onCheckboxClick, clearSelection } = useRecordingSelection(
    () => selectableHashes.value,
    listRef,
)

// Subcollection multi-select runs in parallel with the recording-hash set.
// Both contribute to `totalSelected`; the selection-bar reads from there.
const selectedSubCollections = ref<Set<number>>(new Set())

const totalSelected = computed(() => selected.value.size + selectedSubCollections.value.size)

const selectedRecordingItems = computed(() =>
    items.value.filter(i => i.object_hash && selected.value.has(i.object_hash)),
)

const selectedSubcollectionList = computed(() =>
    subCollections.value.filter(s => selectedSubCollections.value.has(s.id)),
)

function toggleSubcollectionSelection (id: number) {
    const s = new Set(selectedSubCollections.value)
    if (s.has(id)) s.delete(id)
    else s.add(id)
    selectedSubCollections.value = s
}

function onSubcollectionRowClick (sub: Collection, event: MouseEvent) {
    if (event.ctrlKey || event.metaKey) {
        toggleSubcollectionSelection(sub.id)
        return
    }
    // Selection is active anywhere on the page → click toggles instead of
    // navigating, matching the recording-row behaviour.
    if (totalSelected.value > 0) {
        toggleSubcollectionSelection(sub.id)
        return
    }
    enterSubcollection(sub)
}

function clearAllSelection () {
    clearSelection()
    selectedSubCollections.value = new Set()
}

function openSelectionInViewer () {
    openRecordings([...selected.value])
    clearAllSelection()
}

// ── Move selection ────────────────────────────────────────────────────────

const showMovePicker = ref(false)
const moveExecuting = ref(false)

const moveConflict = reactive({
    show: false,
    target: null as Collection | null,
    recordings: [] as string[],
    subcollections: [] as string[],
})

function openMovePicker () {
    showMovePicker.value = true
}

function closeMovePicker () {
    showMovePicker.value = false
}

async function onMovePickerSelect (selection: PickerSelection) {
    if (selection.type !== 'collection') {
        return
    }
    const target = (selection as CollectionSelection).collection
    showMovePicker.value = false

    if (target && target.id === collectionId.value) {
        showToast(t('These items are already in this collection.', SCOPE), 'neutral')
        return
    }

    // Conflict-check the target.
    //   - target collection: compare against its existing items + children.
    //   - library root (target null): only subcollections form a sibling
    //     group (root-level Collections); uncollected recordings have no
    //     equivalent group, so no name-collision check applies to them.
    let targetItemNames = new Set<string>()
    let targetSubNames = new Set<string>()
    try {
        if (target) {
            const [targetItems, targetSubs] = await Promise.all([
                listCollectionItems(target.id),
                listCollections({ parent_id: target.id }),
            ])
            targetItemNames = new Set(
                targetItems.map(i => i.object_name).filter((n): n is string => !!n),
            )
            targetSubNames = new Set(targetSubs.map(s => s.name))
        } else {
            // Root: only check subcollection name collisions against root
            // collections.  listCollections() with no parent_id returns roots.
            const rootSubs = await listCollections()
            targetSubNames = new Set(rootSubs.map(s => s.name))
        }
    } catch {
        showToast(t('Failed to check target collection.', SCOPE), 'danger')
        return
    }

    const recConflicts = target
        ? selectedRecordingItems.value
            .map(i => i.object_name)
            .filter((n): n is string => !!n && targetItemNames.has(n))
        : []

    const subConflicts = selectedSubcollectionList.value
        .map(s => s.name)
        .filter(n => targetSubNames.has(n))

    if (recConflicts.length === 0 && subConflicts.length === 0) {
        await executeMove(target)
        return
    }

    moveConflict.target = target
    moveConflict.recordings = recConflicts
    moveConflict.subcollections = subConflicts
    moveConflict.show = true
}

function cancelMoveConflict () {
    moveConflict.show = false
    moveConflict.target = null
    moveConflict.recordings = []
    moveConflict.subcollections = []
}

async function confirmMoveConflict () {
    const target = moveConflict.target
    const hadTarget = target !== null
    moveConflict.show = false
    moveConflict.target = null
    moveConflict.recordings = []
    moveConflict.subcollections = []
    // The "library root" case is signalled by target=null AFTER a conflict
    // dialog where the user opted in; pass null through to executeMove.
    if (hadTarget || target === null) {
        await executeMove(target)
    }
}

async function executeMove (target: Collection | null) {
    moveExecuting.value = true
    try {
        // Recording → real target uses the atomic move endpoint (preserves
        // the one-collection-per-recording invariant).  Recording → library
        // root means leaving any collection: there is no "uncollected"
        // CollectionItem row, just an absent one, so the existing remove
        // endpoint is the right hammer.
        const recordingMoves: Promise<unknown>[] = target
            ? selectedRecordingItems.value.map(item =>
                moveCollectionItem(collectionId.value, item.id, target.id),
            )
            : selectedRecordingItems.value.map(item =>
                removeCollectionItem(collectionId.value, item.id),
            )
        const subcollectionMoves = selectedSubcollectionList.value.map(sub =>
            updateCollection(sub.id, { parent_id: target?.id ?? null }),
        )
        const results = await Promise.allSettled([...recordingMoves, ...subcollectionMoves])
        const failures = results.filter(r => r.status === 'rejected').length
        const successes = results.length - failures

        if (failures === 0) {
            showToast(t('{count} item(s) moved.', SCOPE, { count: successes }), 'success')
        } else if (successes === 0) {
            showToast(t('Move failed. Check permissions and try again.', SCOPE), 'danger')
        } else {
            showToast(
                t('{success} of {total} moved. {failed} failed.', SCOPE, {
                    success: successes, total: results.length, failed: failures,
                }),
                'warning',
            )
        }
    } finally {
        moveExecuting.value = false
        clearAllSelection()
        await loadCollection()
    }
}

function openEdit () {
    if (!collection.value) {
        return
    }
    input.editName = collection.value.name
    input.editDescription = collection.value.description
    editError.value = null
    showEdit.value = true
}

function closeEdit () {
    showEdit.value = false
}

async function submitEdit () {
    if (!input.editName.trim()) {
        editError.value = t('Name is required.', SCOPE)
        return
    }
    editLoading.value = true
    editError.value = null
    try {
        collection.value = await updateCollection(collectionId.value, {
            name: input.editName.trim(),
            description: input.editDescription.trim(),
        })
        showEdit.value = false
        showToast(t('Collection updated.', SCOPE), 'success')
    } catch {
        editError.value = t('Failed to update collection.', SCOPE)
    } finally {
        editLoading.value = false
    }
}

// ── Create subcollection ──────────────────────────────────────────────────

const showCreateCollection = ref(false)
const createCollectionLoading = ref(false)
const createCollectionError = ref<string | null>(null)

function openCreateCollection () {
    input.newCollectionName = ''
    input.newCollectionDescription = ''
    createCollectionError.value = null
    showCreateCollection.value = true
}

function closeCreateCollection () {
    showCreateCollection.value = false
}

async function submitCreateCollection () {
    const name = input.newCollectionName.trim()
    if (!name) {
        createCollectionError.value = t('Name is required.', SCOPE)
        return
    }
    createCollectionLoading.value = true
    createCollectionError.value = null
    try {
        const created = await createCollection({
            name,
            description: input.newCollectionDescription.trim(),
            parent_id: collectionId.value,
        })
        subCollections.value = [...subCollections.value, created].sort((a, b) =>
            a.name.localeCompare(b.name)
        )
        showCreateCollection.value = false
        showToast(t('Subcollection created.', SCOPE), 'success')
    } catch {
        createCollectionError.value = t('Failed to create subcollection.', SCOPE)
    } finally {
        createCollectionLoading.value = false
    }
}

// ── Delete collection ─────────────────────────────────────────────────────

const showDelete = ref(false)
const deleteLoading = ref(false)

function closeDelete () {
    showDelete.value = false
}

async function confirmDelete () {
    deleteLoading.value = true
    const parentId = collection.value?.parent_id ?? null
    try {
        await deleteCollection(collectionId.value)
        libraryStore.removeCollection(collectionId.value)
        showToast(t('"{name}" moved to trash.', SCOPE, { name: collection.value?.name ?? '' }), 'neutral')
        if (parentId) {
            router.push({ name: 'collection', params: { id: parentId } })
        } else {
            router.push({ name: 'library' })
        }
    } catch {
        showToast(t('Failed to delete collection.', SCOPE), 'danger')
        deleteLoading.value = false
    }
}

// ── Add-new menu dispatcher ──────────────────────────────────────────────

function handleAddNew (event: Event) {
    const value = (event as CustomEvent<{ item: { value: string } }>).detail.item.value
    if (value === 'collection') {
        openCreateCollection()
    } else if (value === 'recording') {
        openAddItem()
    } else if (value === 'media') {
        openAddMedia()
    }
}

// ── Add recording ─────────────────────────────────────────────────────────

const showAddItem = ref(false)
const addItemSelectedHashes = ref<string[]>([])
const addItemLoading = ref(false)
const addItemError = ref<string | null>(null)

/** Recordings not yet in this collection, for display in the picker. */
const addableRecordings = computed(() => {
    const existingHashes = new Set(items.value.map(i => i.object_hash).filter(Boolean))
    return recordingsStore.recordings.filter(r => !existingHashes.has(r.hash))
})

const filteredAddableRecordings = computed(() => {
    const q = input.addItemFilter.trim().toLowerCase()
    if (!q) return addableRecordings.value
    return addableRecordings.value.filter(r => recordingName(r).toLowerCase().includes(q))
})

function openAddItem () {
    addItemSelectedHashes.value = []
    input.addItemFilter = ''
    addItemError.value = null
    showAddItem.value = true
}

function closeAddItem () {
    showAddItem.value = false
    addItemSelectedHashes.value = []
    input.addItemFilter = ''
    addItemError.value = null
}

async function submitAddItem () {
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
                addCollectionItem(collectionId.value, { content_type_id: ctId, object_id: hash })
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
        closeAddItem()
    } finally {
        addItemLoading.value = false
    }
}

async function removeItem (item: CollectionItem) {
    try {
        await removeCollectionItem(collectionId.value, item.id)
        items.value = items.value.filter(i => i.id !== item.id)
    } catch {
        showToast(t('Failed to remove item.', SCOPE), 'danger')
    }
}

const attachMediaItem = ref<CollectionItem | null>(null)

/** Route the recording row menu: attach media opens the dialog, anything else removes. */
function onRecordingAction (value: string, item: CollectionItem) {
    if (value === 'attach-media') {
        attachMediaItem.value = item
    } else if (value === 'edit') {
        openEditRecording(item)
    } else {
        removeItem(item)
    }
}

// ── Edit recording ─────────────────────────────────────────────────────────

const editingRec = ref<Recording | null>(null)

// The collection list only carries the item's hash and name, so fetch the full
// recording before opening the shared edit dialog.
async function openEditRecording (item: CollectionItem) {
    if (!item.object_hash) {
        return
    }
    try {
        editingRec.value = await getRecordingDetail(item.object_hash)
    } catch {
        showToast(t('Failed to load recording.', SCOPE), 'danger')
    }
}

function onRecordingEdited (updated: Recording) {
    const item = items.value.find(i => i.object_hash === updated.hash)
    if (item) {
        item.object_name = recordingName(updated)
    }
}

// ── Add media file ────────────────────────────────────────────────────────

const showAddMedia = ref(false)

/** Hashes already on this collection; the picker excludes them from its list. */
const collectionMediaHashes = computed(
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
                addCollectionItem(collectionId.value, { content_type_id: ctId, object_id: hash })
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
        closeAddMedia()
    } catch {
        showToast(t('Failed to add media files.', SCOPE), 'danger')
    }
}

// ── Create dataset ────────────────────────────────────────────────────────

const showCreateDataset = ref(false)

function openCreateDataset () {
    showCreateDataset.value = true
}

function closeCreateDataset () {
    showCreateDataset.value = false
}

// ── Export as dataset ─────────────────────────────────────────────────────

const showExport = ref(false)
const exportLoading = ref(false)
const exportError = ref<string | null>(null)
const exportInput = reactive({ name: '', description: '', keepHierarchy: true })

function openExport () {
    if (!collection.value) {
        return
    }
    exportInput.name = collection.value.name
    exportInput.description = collection.value.description
    exportInput.keepHierarchy = true
    exportError.value = null
    showExport.value = true
}

function closeExport () {
    showExport.value = false
}

async function submitExport () {
    if (!exportInput.name.trim()) {
        exportError.value = t('Name is required.', SCOPE)
        return
    }
    exportLoading.value = true
    exportError.value = null
    try {
        const result = await exportCollectionToDataset(collectionId.value, {
            name: exportInput.name.trim(),
            description: exportInput.description.trim(),
            materialise_hierarchy: exportInput.keepHierarchy,
        })
        showExport.value = false
        if (result.skipped_count) {
            showToast(
                t('Dataset created with {count} item(s); {skipped} item(s) could not be copied.', SCOPE, {
                    count: result.exported_count,
                    skipped: result.skipped_count,
                }),
                'warning',
            )
        } else {
            showToast(t('Dataset created with {count} item(s).', SCOPE, { count: result.exported_count }), 'success')
        }
        router.push({ name: 'dataset', params: { id: result.dataset.object_hash ?? result.dataset.id } })
    } catch {
        exportError.value = t('Failed to export the collection.', SCOPE)
    } finally {
        exportLoading.value = false
    }
}

// ── Access rights ─────────────────────────────────────────────────────────

</script>

<template>
    <main class="page-view">
        <!-- Loading / error states -->
        <wa-spinner v-if="loading" class="loading-center"></wa-spinner>
        <wa-callout v-else-if="error" variant="danger">{{ error }}</wa-callout>

        <template v-else-if="collection">
            <!-- Header -->
            <header class="page-header">
                <div class="page-header-start">
                    <h1>{{ collection.name }}</h1>
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
                <wa-dropdown placement="bottom-end" @click.stop @wa-select="handleHeaderAction">
                    <wa-button appearance="plain" slot="trigger" variant="neutral">
                        <wa-icon name="ellipsis"></wa-icon>
                    </wa-button>
                    <wa-dropdown-item value="edit">
                        <wa-icon name="pencil" slot="icon"></wa-icon>
                        {{ t('Edit', SCOPE) }}
                    </wa-dropdown-item>
                    <wa-dropdown-item value="create-dataset">
                        <wa-icon name="database" slot="icon"></wa-icon>
                        {{ t('Create dataset', SCOPE) }}
                    </wa-dropdown-item>
                    <wa-dropdown-item value="export-dataset">
                        <wa-icon name="folders" slot="icon"></wa-icon>
                        {{ t('Export as dataset', SCOPE) }}
                    </wa-dropdown-item>
                    <wa-dropdown-item value="number-recordings">
                        <wa-icon name="list-ol" slot="icon"></wa-icon>
                        {{ t('Number recordings', SCOPE) }}
                    </wa-dropdown-item>
                    <wa-dropdown-item value="delete" variant="danger">
                        <wa-icon name="trash" slot="icon"></wa-icon>
                        {{ t('Move to trash', SCOPE) }}
                    </wa-dropdown-item>
                </wa-dropdown>
            </header>

            <p v-if="collection.description" class="collection-description">
                {{ collection.description }}
            </p>

            <!-- ── Contents section ─────────────────────────────────────── -->
            <div class="section-header">
                <h2>{{ t('Contents', SCOPE) }}</h2>
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
                    <wa-dropdown-item value="collection">
                        <wa-icon name="folder" slot="icon"></wa-icon>
                        {{ t('Collection', SCOPE) }}
                    </wa-dropdown-item>
                    <wa-dropdown-item value="recording">
                        <wa-icon name="file-music" slot="icon"></wa-icon>
                        {{ t('Recording', SCOPE) }}
                    </wa-dropdown-item>
                    <wa-dropdown-item value="media">
                        <wa-icon name="file" slot="icon"></wa-icon>
                        {{ t('Media file', SCOPE) }}
                    </wa-dropdown-item>
                </wa-dropdown>
            </div>

            <p v-if="!items.length && !subCollections.length" class="empty-state recordings-empty">
                {{ t('This collection is empty.', SCOPE) }}
            </p>

            <div v-else class="list-rows" ref="listRef">
                <div v-for="sub in subCollections"
                    :key="`col-${sub.id}`"
                    class="list-row clickable"
                    :class="{ 'list-row--selected': selectedSubCollections.has(sub.id) }"
                    role="button"
                    tabindex="0"
                    @click="onSubcollectionRowClick(sub, $event)"
                    @keydown.enter="enterSubcollection(sub)"
                >
                    <div class="list-row-main">
                        <span class="row-lead">
                            <input v-if="selectedSubCollections.has(sub.id) || totalSelected > 0"
                                :checked="selectedSubCollections.has(sub.id)"
                                class="row-checkbox"
                                type="checkbox"
                                @change.stop
                                @click.stop="toggleSubcollectionSelection(sub.id)"
                            />
                            <wa-icon v-else class="icon-folder" name="folder"></wa-icon>
                        </span>
                        <span class="list-row-name">{{ sub.name }}</span>
                        <wa-icon class="icon-chevron" name="angle-right"></wa-icon>
                    </div>
                </div>
                <template v-for="item in items" :key="item.id">
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
                            <wa-dropdown-item value="remove" variant="danger">
                                <wa-icon name="xmark" slot="icon"></wa-icon>
                                {{ t('Remove from collection', SCOPE) }}
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
                        @dropdown-action="removeItem(item)"
                        @open="openMedia(item.object_hash)"
                        @row-dblclick="openMedia(item.object_hash)"
                    >
                        <template #actions>
                            <wa-dropdown-item value="remove" variant="danger">
                                <wa-icon name="xmark" slot="icon"></wa-icon>
                                {{ t('Remove from collection', SCOPE) }}
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
                </template>
                <!-- Selection action bar -->
                <div v-if="totalSelected > 0" class="selection-bar">
                    <span class="selection-bar__count">{{ t('{count} selected', SCOPE, { count: totalSelected }) }}</span>
                    <wa-button
                        appearance="plain"
                        size="s"
                        variant="neutral"
                        @click="clearAllSelection"
                    >
                        {{ t('Clear selection', SCOPE) }}
                    </wa-button>
                    <wa-button
                        appearance="filled-outlined"
                        :loading="moveExecuting"
                        size="s"
                        variant="brand"
                        @click="openMovePicker"
                    >
                        <wa-icon name="folder" slot="start"></wa-icon>
                        {{ t('Move to...', SCOPE) }}
                    </wa-button>
                    <wa-button v-if="selected.size > 0"
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

        </template>
    </main>

    <!-- Edit dialog -->
    <wa-dialog :label="t('Edit collection', SCOPE)" :open="showEdit" @wa-hide.self="closeEdit">
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

    <!-- Number recordings dialog -->
    <wa-dialog
        :label="t('Number recordings', SCOPE)"
        :open="showNumber"
        @wa-hide.self="showNumber = false"
    >
        <div class="dialog-form">
            <wa-callout v-if="numberError" variant="danger">{{ numberError }}</wa-callout>
            <wa-callout variant="warning">
                {{ t('This replaces the display name of every recording in this collection. Custom names you have already set will be overwritten.', SCOPE) }}
            </wa-callout>
            <wa-input
                :disabled="numberLoading"
                :hint="t('Recordings become {example1}, {example2}, … in the order they were added.', SCOPE, numberExamples)"
                :label="t('Prefix', SCOPE)"
                size="s"
                type="text"
                v-wa="[numberInput, 'prefix']"
            ></wa-input>
        </div>
        <div slot="footer" class="form-actions">
            <wa-button
                appearance="filled-outlined"
                :disabled="numberLoading"
                variant="neutral"
                @click="showNumber = false"
            >
                {{ t('Cancel', SCOPE) }}
            </wa-button>
            <wa-button
                appearance="filled-outlined"
                :loading="numberLoading"
                variant="brand"
                @click="submitNumber"
            >
                {{ t('Number recordings', SCOPE) }}
            </wa-button>
        </div>
    </wa-dialog>

    <!-- Delete dialog -->
    <wa-dialog :label="t('Move to trash', SCOPE)" :open="showDelete" @wa-hide.self="closeDelete">
        <i18n-t class="dialog-text" keypath="CollectionView.move_to_trash_confirm" tag="p">
            <template #name>
                <strong>{{ collection?.name }}</strong>
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

    <!-- Create dataset dialog -->
    <CreateDatasetDialog
        :open="showCreateDataset"
        :preselect-collection-id="collectionId"
        @close="closeCreateDataset"
    />

    <!-- Export as dataset dialog -->
    <wa-dialog
        :label="t('Export as dataset', SCOPE)"
        :open="showExport"
        @wa-hide.self="closeExport"
    >
        <div class="dialog-form">
            <wa-callout v-if="exportError" variant="danger">{{ exportError }}</wa-callout>
            <p class="export-hint">
                {{ t('Copies this collection and its sub-collections into a new dataset you can share. The collection itself is not changed.', SCOPE) }}
            </p>
            <wa-input
                :disabled="exportLoading"
                :label="t('Name', SCOPE)"
                size="s"
                type="text"
                v-wa="[exportInput, 'name']"
            ></wa-input>
            <wa-textarea
                :disabled="exportLoading"
                :label="t('Description', SCOPE)"
                rows="3"
                size="s"
                v-wa="[exportInput, 'description']"
            ></wa-textarea>
            <wa-switch :disabled="exportLoading" v-wa="[exportInput, 'keepHierarchy']">
                {{ t('Recreate sub-collections as dataset folders', SCOPE) }}
            </wa-switch>
        </div>
        <div slot="footer" class="form-actions">
            <wa-button
                appearance="filled-outlined"
                :disabled="exportLoading"
                variant="neutral"
                @click="closeExport"
            >
                {{ t('Cancel', SCOPE) }}
            </wa-button>
            <wa-button
                appearance="filled-outlined"
                :loading="exportLoading"
                variant="brand"
                @click="submitExport"
            >
                {{ t('Export', SCOPE) }}
            </wa-button>
        </div>
    </wa-dialog>

    <!-- Create subcollection dialog -->
    <wa-dialog
        :label="t('Create collection', SCOPE)"
        :open="showCreateCollection"
        @wa-hide.self="closeCreateCollection"
    >
        <div class="dialog-form">
            <i18n-t class="dialog-context"
                keypath="CollectionView.create_collection_inside"
                tag="p"
            >
                <template #name>
                    <strong>{{ collection?.name }}</strong>
                </template>
            </i18n-t>
            <wa-callout v-if="createCollectionError" variant="danger">{{ createCollectionError }}</wa-callout>
            <wa-input
                :disabled="createCollectionLoading"
                :label="t('Name', SCOPE)"
                size="s"
                type="text"
                v-wa="[input, 'newCollectionName']"
            ></wa-input>
            <wa-textarea
                :disabled="createCollectionLoading"
                :label="t('Description', SCOPE)"
                rows="3"
                size="s"
                v-wa="[input, 'newCollectionDescription']"
            ></wa-textarea>
        </div>
        <div slot="footer" class="form-actions">
            <wa-button
                appearance="filled-outlined"
                :disabled="createCollectionLoading"
                variant="neutral"
                @click="closeCreateCollection"
            >
                {{ t('Cancel', SCOPE) }}
            </wa-button>
            <wa-button
                appearance="filled-outlined"
                :loading="createCollectionLoading"
                variant="brand"
                @click="submitCreateCollection"
            >
                {{ t('Create', SCOPE) }}
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
                {{ t('All available recordings are already in this collection.', SCOPE) }}
            </p>
            <template v-else>
                <wa-input
                    clearable
                    :disabled="addItemLoading"
                    :label="t('Recordings', SCOPE)"
                    :placeholder="t('Filter by name...', SCOPE)"
                    size="s"
                    v-wa="[input, 'addItemFilter']"
                ></wa-input>
                <select
                    class="recording-select"
                    :disabled="addItemLoading"
                    multiple
                    v-model="addItemSelectedHashes"
                >
                    <option
                        v-for="rec in filteredAddableRecordings"
                        :key="rec.hash"
                        :value="rec.hash"
                    >
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
        :exclude-hashes="collectionMediaHashes"
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

    <!-- Move target picker -->
    <CollectionPickerDialog
        :open="showMovePicker"
        mode="collection"
        :title="t('Move to...', SCOPE)"
        @close="closeMovePicker"
        @select="onMovePickerSelect"
    />

    <!-- Move conflict confirmation -->
    <wa-dialog
        :label="t('Names already in use', SCOPE)"
        :open="moveConflict.show"
        @wa-hide.self="cancelMoveConflict"
    >
        <div class="dialog-form">
            <i18n-t class="dialog-context"
                keypath="CollectionView.move_conflict_intro"
                tag="p"
            >
                <template #target>
                    <strong>{{ moveConflict.target?.name ?? t('the library root', SCOPE) }}</strong>
                </template>
            </i18n-t>
            <ul class="conflict-list">
                <li v-for="name in moveConflict.subcollections" :key="`sc-${name}`">
                    <wa-icon class="icon-folder" name="folder"></wa-icon>
                    {{ name }}
                </li>
                <li v-for="name in moveConflict.recordings" :key="`rc-${name}`">
                    <wa-icon class="icon-muted" name="file-music"></wa-icon>
                    {{ name }}
                </li>
            </ul>
            <p class="dialog-context">
                {{ t('Move anyway? Duplicate names will coexist; you can rename either side later.', SCOPE) }}
            </p>
        </div>
        <div slot="footer" class="form-actions">
            <wa-button
                appearance="filled-outlined"
                variant="neutral"
                @click="cancelMoveConflict"
            >
                {{ t('Cancel', SCOPE) }}
            </wa-button>
            <wa-button
                appearance="filled-outlined"
                variant="brand"
                @click="confirmMoveConflict"
            >
                {{ t('Move anyway', SCOPE) }}
            </wa-button>
        </div>
    </wa-dialog>

</template>

<style scoped>
.btn-create-collection {
    margin-left: auto;
}

.export-hint {
    color: var(--wa-color-text-quiet);
    margin: 0;
}

.collection-description {
    color: var(--wa-color-text-quiet);
    margin-bottom: 1.5rem;
    margin-top: -0.75rem;
}

.dialog-context {
    color: var(--wa-color-text-quiet);
    font-size: 0.875rem;
    margin: 0;
}

.icon-folder {
    color: var(--wa-color-warning-fill-loud);
    flex-shrink: 0;
}

.icon-chevron {
    color: var(--wa-color-text-quiet);
    flex-shrink: 0;
    font-size: 0.85em;
}

.recordings-empty {
    padding: 1.5rem 0;
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

.recording-select {
    border: 1px solid var(--wa-color-neutral-border-normal);
    border-radius: 6px;
    font-size: 0.875rem;
    min-height: 10rem;
    padding: 0.5rem 0.75rem;
    width: 100%;
}

.icon-muted {
    color: var(--wa-color-text-quiet);
    flex-shrink: 0;
}

.conflict-list {
    background: var(--wa-color-neutral-fill-quiet);
    border-radius: var(--wa-border-radius-m);
    display: flex;
    flex-direction: column;
    font-size: 0.875rem;
    gap: 0.25rem;
    list-style: none;
    margin: 0;
    max-height: 12rem;
    overflow-y: auto;
    padding: 0.5rem 0.75rem;
}

.conflict-list li {
    align-items: center;
    display: flex;
    gap: 0.4rem;
}
</style>
