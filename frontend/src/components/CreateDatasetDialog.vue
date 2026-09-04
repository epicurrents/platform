<script setup lang="ts">
/**
 * CreateDatasetDialog — modal for creating a dataset by selecting recordings
 * from the user's collection library.
 *
 * The library is shown as a lazy-loading tree (wa-tree / wa-tree-item) with
 * selection="multiple" so wa-tree handles checkbox rendering natively.
 * wa-selection-change fires whenever the selection changes; items are identified
 * via data-collection-id / data-recording-hash attributes set in CollectionTreeItem.
 *
 * On submit the modal:
 *   1. Recursively fetches items from every selected collection (and all their
 *      descendant sub-collections) that have not yet been loaded.
 *   2. Unions those hashes with any directly selected recording hashes.
 *   3. Creates the dataset, then batch-adds all recording items.
 *   4. Navigates to the new dataset page.
 *
 * Note: recordings can only belong to one collection (DB-level constraint), so
 * deduplication is a safety net only.
 */
import { reactive, ref, computed, watch, provide, nextTick } from 'vue'
import { useRouter } from 'vue-router'
import { t } from '#i18n'
import CollectionTreeItem, { TREE_CTX_KEY } from '#components/CollectionTreeItem.vue'
import type { TreeContext } from '#components/CollectionTreeItem.vue'
import {
    getCollection,
    listCollections,
    listCollectionItems,
    createDataset,
    type Collection,
    type CollectionItem,
} from '#api/library'
import { useLibraryStore } from '#stores/library'
import { showToast } from '#lib/toast'

const SCOPE = 'CreateDatasetDialog'

// ---------------------------------------------------------------------------
// Props / Emits
// ---------------------------------------------------------------------------

interface Props {
    open: boolean
    /**
     * Collection to pre-select when the dialog opens. Walks the ancestor chain
     * so the target's wa-tree-item exists in the DOM before checking it. The
     * caller typically passes the currently-open collection from CollectionView.
     */
    preselectCollectionId?: number | null
}

const props = defineProps<Props>()

const emit = defineEmits<{
    close: []
}>()

// ---------------------------------------------------------------------------
// Router / store
// ---------------------------------------------------------------------------

const router = useRouter()
const libraryStore = useLibraryStore()

// ---------------------------------------------------------------------------
// Tree state (all reactive Maps/Sets so Vue tracks .get()/.has()/.set())
// ---------------------------------------------------------------------------

/** Root-level collections, loaded when the dialog opens. */
const rootCollections = ref<Collection[]>([])
const rootLoading = ref(false)

/** Children of a given collection ID, loaded on first expand. */
const loadedChildren = reactive(new Map<number, Collection[]>())

/** Recording items per collection ID, loaded alongside children. */
const loadedItems = reactive(new Map<number, CollectionItem[]>())

/** Collection IDs currently being fetched. */
const loadingIds = reactive(new Set<number>())

/**
 * Promise cache so concurrent callers (onSelectionChange + gatherHashesFromCollection)
 * await the same in-flight request rather than racing.
 */
const loadingPromises = new Map<number, Promise<void>>()

// ---------------------------------------------------------------------------
// Selection state — populated from wa-selection-change events
// ---------------------------------------------------------------------------

/** Collection IDs checked by the user. */
const selectedCollectionIds = reactive(new Set<number>())

/** Hashes the user explicitly removed from the panel via the X button. */
const excludedHashes = reactive(new Set<string>())

/**
 * Directly selected recordings: hash → display name.
 * Reactive Map so the selection panel stays in sync.
 */
const selectedRecordingNames = reactive(new Map<string, string>())

const filteredPanelRecordings = computed(() => {
    const q = input.selectionFilter.trim().toLowerCase()
    if (!q) {
        return allPanelRecordings.value
    }
    return allPanelRecordings.value.filter(r => r.name.toLowerCase().includes(q))
})

const hasSelection = computed(() =>
    selectedCollectionIds.size > 0 || selectedRecordingNames.size > 0,
)

// ---------------------------------------------------------------------------
// Dataset form state
// ---------------------------------------------------------------------------

const input = reactive({ name: '', description: '', selectionFilter: '' })
const submitError = ref<string | null>(null)
const submitting = ref(false)

// ---------------------------------------------------------------------------
// Tree context — provided to the CollectionTreeItem subtree
// ---------------------------------------------------------------------------

function loadCollection (id: number): Promise<void> {
    if (loadedChildren.has(id)) {
        return Promise.resolve()
    }
    if (loadingPromises.has(id)) {
        return loadingPromises.get(id)!
    }
    const p = (async () => {
        loadingIds.add(id)
        try {
            const [children, items] = await Promise.all([
                listCollections({ parent_id: id, limit: 200 }),
                listCollectionItems(id, { limit: 200 }),
            ])
            loadedChildren.set(id, children)
            loadedItems.set(id, items)
        } catch {
            // Treat errors as empty so the spinner disappears.
            loadedChildren.set(id, [])
            loadedItems.set(id, [])
        } finally {
            loadingIds.delete(id)
            loadingPromises.delete(id)
        }
    })()
    loadingPromises.set(id, p)
    return p
}

/**
 * After a collection finishes loading, programmatically select its children in
 * the tree (visual checkmarks) and register any sub-collections so the panel
 * and submit logic pick them up.  Called via .then() so it runs only once the
 * load promise has resolved.
 */
async function _selectLoadedChildren (collectionId: number) {
    if (!selectedCollectionIds.has(collectionId) || !treeRef.value) {
        return
    }
    // Wait for Vue's DOM flush then one rAF so Lit has initialised the new items.
    await nextTick()
    await new Promise<void>(resolve => requestAnimationFrame(() => resolve()))
    if (!selectedCollectionIds.has(collectionId) || !treeRef.value) {
        return
    }

    const colEl = treeRef.value.querySelector(`[data-collection-id="${collectionId}"]`)
    if (!colEl) {
        return
    }

    // Visually select recording children (skip ones the user manually excluded).
    colEl.querySelectorAll('[data-recording-hash]').forEach((item: any) => {
        const hash = (item as HTMLElement).dataset.recordingHash
        if (!item.selected && (!hash || !excludedHashes.has(hash))) {
            item.selected = true
        }
    })

    // Visually select sub-collection children and register them so
    // collectionRecordingPanel includes their items once they load.
    colEl.querySelectorAll('[data-collection-id]').forEach((rawEl: any) => {
        const el = rawEl as HTMLElement & { selected: boolean }
        const subId = Number(el.dataset.collectionId)
        if (!subId) {
            return
        }
        if (!el.selected) {
            el.selected = true
        }
        if (!selectedCollectionIds.has(subId)) {
            selectedCollectionIds.add(subId)
        }
        if (!loadedChildren.has(subId)) {
            loadCollection(subId).then(() => _selectLoadedChildren(subId))
        }
    })
}

provide<TreeContext>(TREE_CTX_KEY, reactive({
    loadedChildren,
    loadedItems,
    loadingIds,
    loadCollection,
}))

// ---------------------------------------------------------------------------
// Selection panel — computed from tree selection + loaded collection items
// ---------------------------------------------------------------------------

/**
 * Recordings from selected collections that are already loaded.
 * Reacts automatically whenever `selectedCollectionIds` or `loadedItems`
 * changes, so the panel updates as soon as a collection finishes loading.
 * Skips hashes already present in `selectedRecordingNames` to avoid duplicates
 * when WA tree auto-checks recording children of a checked (loaded) collection.
 */
const collectionRecordingPanel = computed<Array<{ hash: string; name: string }>>(() => {
    const result: Array<{ hash: string; name: string }> = []
    const seen = new Set<string>()
    for (const id of selectedCollectionIds) {
        for (const item of loadedItems.get(id) ?? []) {
            if (item.object_hash && !selectedRecordingNames.has(item.object_hash) && !seen.has(item.object_hash) && !excludedHashes.has(item.object_hash)) {
                seen.add(item.object_hash)
                result.push({ hash: item.object_hash, name: item.object_name ?? String(item.object_id) })
            }
        }
    }
    return result
})

/** Merged list for the selection panel — all items are removable via the X button. */
const allPanelRecordings = computed<Array<{ hash: string; name: string; fromCollection: boolean }>>(() => {
    const out: Array<{ hash: string; name: string; fromCollection: boolean }> = []
    for (const item of collectionRecordingPanel.value) {
        out.push({ ...item, fromCollection: true })
    }
    for (const [hash, name] of selectedRecordingNames) {
        if (!excludedHashes.has(hash)) {
            out.push({ hash, name, fromCollection: false })
        }
    }
    return out
})

// ---------------------------------------------------------------------------
// Selection change — wa-tree fires this with all currently selected items
// ---------------------------------------------------------------------------

function onSelectionChange (event: CustomEvent<{ selection: HTMLElement[] }>) {
    // Snapshot the previous selection so we can detect newly-added collections below.
    const prevCollectionIds = new Set(selectedCollectionIds)

    // Build a set of hashes belonging to already-loaded selected collections so
    // we can skip them from `selectedRecordingNames` (they appear in
    // `collectionRecordingPanel` instead, avoiding double-counting).
    const collectionItemHashes = new Set<string>()
    const newCollectionIds = new Set<number>()
    for (const el of event.detail.selection) {
        if (el.dataset.collectionId) {
            newCollectionIds.add(Number(el.dataset.collectionId))
        }
    }
    for (const id of newCollectionIds) {
        for (const item of loadedItems.get(id) ?? []) {
            if (item.object_hash) {
                collectionItemHashes.add(item.object_hash)
            }
        }
    }

    selectedCollectionIds.clear()
    selectedRecordingNames.clear()
    for (const el of event.detail.selection) {
        const colId = el.dataset.collectionId
        const recHash = el.dataset.recordingHash
        if (colId) {
            selectedCollectionIds.add(Number(colId))
        } else if (recHash && !collectionItemHashes.has(recHash)) {
            // Only track recordings not already covered by a selected collection.
            selectedRecordingNames.set(recHash, el.dataset.recordingName ?? recHash)
        }
    }

    // When a collection transitions from unselected → selected, clear any prior
    // exclusions for its items so they are included again on reselection.
    for (const id of selectedCollectionIds) {
        if (!prevCollectionIds.has(id)) {
            for (const item of loadedItems.get(id) ?? []) {
                if (item.object_hash) {
                    excludedHashes.delete(item.object_hash)
                }
            }
        }
    }

    // For each selected collection that isn't loaded yet: start loading, then
    // programmatically select its children once the data lands in the DOM.
    // Already-loaded collections rely on WA's own cascade-select for visual state;
    // `collectionRecordingPanel` handles the panel reactively in all cases.
    for (const id of selectedCollectionIds) {
        if (!loadedChildren.has(id)) {
            loadCollection(id).then(() => _selectLoadedChildren(id))
        }
    }
}

// ---------------------------------------------------------------------------
// Dialog lifecycle
// ---------------------------------------------------------------------------

watch(() => props.open, async (open) => {
    if (!open) {
        return
    }
    _reset()
    rootLoading.value = true
    try {
        rootCollections.value = await listCollections({ limit: 200 })
    } catch {
        showToast(t('Failed to load library.', SCOPE), 'danger')
    } finally {
        rootLoading.value = false
    }

    if (props.preselectCollectionId != null) {
        await _preselectCollection(props.preselectCollectionId)
    }
})

/** Walk parent_id links from `id` up to the root, returning the path
 *  [root, ..., id]. Uses getCollection per level; depth is typically small. */
async function _ancestryPath (id: number): Promise<number[]> {
    const path: number[] = []
    let current: number | null = id
    while (current !== null) {
        path.unshift(current)
        try {
            const col = await getCollection(current)
            current = col.parent_id
        } catch {
            // Lost access partway up — return what we have and let the caller
            // proceed; the tree marking will silently no-op on missing ancestors.
            break
        }
    }
    return path
}

/** Mark `id` as selected in the tree, loading ancestors as needed so the
 *  target's wa-tree-item exists in the DOM. Tolerates network failures and
 *  missing tree nodes — selection state is updated regardless of visual outcome. */
async function _preselectCollection (id: number) {
    selectedCollectionIds.add(id)
    const path = await _ancestryPath(id)
    await Promise.all(path.map(ancestorId => loadCollection(ancestorId)))

    if (!treeRef.value) {
        return
    }
    await nextTick()
    await new Promise<void>(resolve => requestAnimationFrame(() => resolve()))
    if (!treeRef.value) {
        return
    }

    // Expand each ancestor so the user can see the cascade down to the target.
    for (const ancestorId of path.slice(0, -1)) {
        const el = treeRef.value.querySelector(`[data-collection-id="${ancestorId}"]`) as
            (HTMLElement & { expanded?: boolean }) | null
        if (el && el.expanded !== true) {
            el.expanded = true
        }
    }

    const targetEl = treeRef.value.querySelector(`[data-collection-id="${id}"]`) as
        (HTMLElement & { selected?: boolean }) | null
    if (targetEl && targetEl.selected !== true) {
        targetEl.selected = true
    }
    await _selectLoadedChildren(id)
}

function _reset () {
    rootCollections.value = []
    loadedChildren.clear()
    loadedItems.clear()
    loadingIds.clear()
    loadingPromises.clear()
    selectedCollectionIds.clear()
    selectedRecordingNames.clear()
    excludedHashes.clear()
    input.name = ''
    input.description = ''
    input.selectionFilter = ''
    submitError.value = null
    submitting.value = false
}

function handleClose () {
    emit('close')
}

// ---------------------------------------------------------------------------
// Submission helpers
// ---------------------------------------------------------------------------

/**
 * Recursively ensures all descendants of `id` are loaded, then collects every
 * recording hash from items under `id` and its descendants into `out`.
 */
async function gatherHashesFromCollection (id: number, out: Set<string>) {
    await loadCollection(id)
    for (const item of loadedItems.get(id) ?? []) {
        if (item.object_hash && !excludedHashes.has(item.object_hash)) {
            out.add(item.object_hash)
        }
    }
    await Promise.all(
        (loadedChildren.get(id) ?? []).map(child => gatherHashesFromCollection(child.id, out)),
    )
}

// ---------------------------------------------------------------------------
// Submit
// ---------------------------------------------------------------------------

async function submit () {
    if (!input.name.trim()) {
        submitError.value = t('Name is required.', SCOPE)
        return
    }
    submitting.value = true
    submitError.value = null

    try {
        // 1. Gather all recording hashes from selected collections (recursively).
        const hashes = new Set<string>()
        await Promise.all([...selectedCollectionIds].map(id => gatherHashesFromCollection(id, hashes)))

        // 2. Union with directly selected recordings.
        for (const hash of selectedRecordingNames.keys()) {
            hashes.add(hash)
        }

        // 3. Create the dataset (may be empty — recordings can be added later).
        const dataset = await createDataset({
            name: input.name.trim(),
            description: input.description.trim(),
            recording_hashes: [...hashes],
        })

        // Reflect in the store so DatasetsView shows the new entry without reload.
        libraryStore.datasets.push(dataset)

        // 4. Navigate to the new dataset page.
        emit('close')
        router.push({ name: 'dataset', params: { id: dataset.object_hash ?? dataset.id } })
    } catch {
        submitError.value = t('Failed to create dataset. Please try again.', SCOPE)
    } finally {
        submitting.value = false
    }
}

// ---------------------------------------------------------------------------
// Remove from selection panel (with bidirectional tree sync)
// ---------------------------------------------------------------------------

const treeRef = ref<HTMLElement | null>(null)

function removeRecording (hash: string) {
    if (selectedRecordingNames.has(hash)) {
        selectedRecordingNames.delete(hash)
    } else {
        // Collection-sourced: exclude so it stays removed even after re-loads.
        excludedHashes.add(hash)
    }
    // Deselect the matching tree item so the checkbox clears visually.
    const item = treeRef.value?.querySelector(`[data-recording-hash="${hash}"]`) as any
    if (item?.selected !== undefined) {
        item.selected = false
    }
}
</script>

<template>
    <wa-dialog
        class="create-dataset-dialog"
        :label="t('Create dataset', SCOPE)"
        :open="open"
        @wa-hide.self="handleClose"
    >
        <div class="dialog-body">
            <!-- ── Dataset name / description ──────────────────────────── -->
            <div class="fields">
                <wa-callout v-if="submitError" variant="danger">{{ submitError }}</wa-callout>
                <wa-input
                    autofocus
                    :disabled="submitting"
                    :label="t('Name', SCOPE)"
                    :placeholder="t('e.g. EEG Motor Imagery Study 2025', SCOPE)"
                    size="s"
                    type="text"
                    v-wa="[input, 'name']"
                ></wa-input>
                <wa-textarea
                    :disabled="submitting"
                    :label="t('Description', SCOPE)"
                    :placeholder="t('Optional description', SCOPE)"
                    rows="2"
                    size="s"
                    v-wa="[input, 'description']"
                ></wa-textarea>
            </div>

            <!-- ── Tree browser ───────────────────────────────────────── -->
            <p class="section-label">{{ t('Browse library', SCOPE) }}</p>

            <div class="tree-scroll">
                <wa-spinner v-if="rootLoading" class="root-spinner"></wa-spinner>

                <p v-else-if="!rootCollections.length" class="tree-empty">
                    {{ t('No collections yet.', SCOPE) }}
                </p>

                <wa-tree v-else
                    ref="treeRef"
                    selection="multiple"
                    @wa-selection-change="onSelectionChange"
                >
                    <CollectionTreeItem v-for="col in rootCollections" :key="col.id"
                        :collection="col"
                    />
                </wa-tree>
            </div>

            <!-- ── Selected recordings panel ──────────────────────────── -->
            <template v-if="hasSelection">
                <div class="selection-header">
                    <p class="section-label">
                        {{
                            t(
                                'Selected recordings ({count})',
                                SCOPE,
                                { count: allPanelRecordings.length },
                            )
                        }}
                    </p>
                </div>

                <wa-input v-if="allPanelRecordings.length > 0"
                    clearable
                    :disabled="submitting"
                    :placeholder="t('Filter recordings…', SCOPE)"
                    size="s"
                    v-wa="[input, 'selectionFilter']"
                ></wa-input>

                <div v-if="allPanelRecordings.length > 0" class="selection-list">
                    <div v-for="rec in filteredPanelRecordings" :key="`rec-${rec.hash}`"
                        class="selection-row"
                    >
                        <wa-icon class="sel-icon" name="file-music"></wa-icon>
                        <span class="sel-name">{{ rec.name }}</span>
                        <wa-button
                            appearance="plain"
                            class="sel-remove"
                            :disabled="submitting"
                            size="s"
                            variant="neutral"
                            @click="removeRecording(rec.hash)"
                        >
                            <wa-icon name="xmark"></wa-icon>
                        </wa-button>
                    </div>
                    <p v-if="filteredPanelRecordings.length === 0" class="sel-empty">
                        {{ t('No recordings match the filter.', SCOPE) }}
                    </p>
                </div>

                <wa-callout class="snapshot-note" variant="neutral">
                    {{
                        t(
                            'Collections are expanded at creation time — the dataset will reflect ' +
                            'the recordings present now and will not update automatically.',
                            SCOPE,
                        )
                    }}
                </wa-callout>
            </template>
        </div>

        <!-- ── Footer ─────────────────────────────────────────────────── -->
        <div class="form-actions" slot="footer">
            <wa-button
                appearance="filled-outlined"
                :disabled="submitting"
                variant="neutral"
                @click="handleClose"
            >
                {{ t('Cancel', SCOPE) }}
            </wa-button>
            <wa-button
                appearance="filled-outlined"
                :disabled="!input.name.trim()"
                :loading="submitting"
                variant="brand"
                @click="submit"
            >
                {{ t('Create dataset', SCOPE) }}
            </wa-button>
        </div>
    </wa-dialog>
</template>

<style scoped>
.create-dataset-dialog {
    --width: 660px;
}

/* ── Body layout ──────────────────────────────────────────────────────────── */
.dialog-body {
    display: flex;
    flex-direction: column;
    gap: 1rem;
}

/* ── Form fields ──────────────────────────────────────────────────────────── */
.fields {
    display: flex;
    flex-direction: column;
    gap: 0.75rem;
}

/* ── Section labels ───────────────────────────────────────────────────────── */
.section-label {
    font-size: 0.875rem;
    font-weight: 500;
    margin: 0;
}

/* ── Tree browser ─────────────────────────────────────────────────────────── */
.tree-scroll {
    border: 1px solid var(--wa-color-neutral-border-normal);
    border-radius: var(--wa-border-radius-m);
    max-height: 280px;
    min-height: 40px;
    overflow-y: auto;
    padding: 0.25rem 0.5rem;
}

.root-spinner {
    display: block;
    margin: 1.5rem auto;
}

.tree-empty {
    color: var(--wa-color-text-quiet);
    font-size: 0.875rem;
    margin: 0;
    padding: 1rem 0;
    text-align: center;
}

/* ── Selection panel header ───────────────────────────────────────────────── */
.selection-header {
    align-items: baseline;
    display: flex;
    gap: 0.5rem;
}

.collection-count-note {
    color: var(--wa-color-text-quiet);
    font-size: 0.8125rem;
}

/* ── Selected recordings list ─────────────────────────────────────────────── */
.selection-list {
    border: 1px solid var(--wa-color-neutral-border-normal);
    border-radius: var(--wa-border-radius-m);
    display: flex;
    flex-direction: column;
    max-height: 180px;
    overflow-y: auto;
}

.selection-row {
    align-items: center;
    display: flex;
    font-size: 0.8125rem;
    gap: 0.4rem;
    padding: 0.25rem 0.5rem;
}

.selection-row:not(:last-child) {
    border-bottom: 1px solid var(--wa-color-neutral-border-quiet);
}

.sel-icon {
    color: var(--wa-color-neutral-400);
    flex-shrink: 0;
    font-size: 0.875rem;
}

.sel-name {
    flex: 1;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}

.sel-remove {
    flex-shrink: 0;
}

.sel-empty {
    color: var(--wa-color-text-quiet);
    font-size: 0.8125rem;
    margin: 0;
    padding: 0.5rem 0.75rem;
    text-align: center;
}

/* ── Snapshot callout ─────────────────────────────────────────────────────── */
.snapshot-note {
    font-size: 0.8125rem;
}
</style>
