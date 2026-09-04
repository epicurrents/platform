<script setup lang="ts">
import { reactive, ref, computed, watch, nextTick } from 'vue'
import { t } from '#i18n'
import {
    listCollections,
    createCollection,
    listCollectionItems,
    type Collection,
    type CollectionItem,
} from '#api/library'

const SCOPE = 'CollectionPickerDialog'

// ---------------------------------------------------------------------------
// Public types (re-exported so callers can import from this file)
// ---------------------------------------------------------------------------

/** Emitted when the user selects a collection (or the library root). */
export interface CollectionSelection {
    type: 'collection'
    /** `null` = library root (no parent). */
    collection: Collection | null
}

/** Emitted when the user selects a recording item (item mode only). */
export interface RecordingSelection {
    type: 'recording'
    item: CollectionItem
}

export type PickerSelection = CollectionSelection | RecordingSelection

// ---------------------------------------------------------------------------
// Props / Emits
// ---------------------------------------------------------------------------

interface Props {
    /**
     * 'collection' (default): user picks a destination folder.
     * 'item': user can pick a recording inside a folder, or pick the folder itself.
     */
    mode?: 'collection' | 'item'
    /** Dialog header label. Defaults to a sensible string per mode. */
    title?: string
    open: boolean
}

const props = withDefaults(defineProps<Props>(), {
    mode: 'collection',
    title: undefined,
})

const emit = defineEmits<{
    /** Fired when the user confirms a selection. Does NOT auto-close — caller must set open=false. */
    select: [selection: PickerSelection]
    /** Fired when the dialog requests to be closed (close button, Escape, overlay click). */
    close: []
}>()

// ---------------------------------------------------------------------------
// Internal state
// ---------------------------------------------------------------------------

/** Children of each visited level, keyed by parent id (null = library root).
 *  The backend's list_collections endpoint only returns one level at a time
 *  (parent_id omitted → roots only, parent_id set → that node's children),
 *  so we fetch lazily as the user navigates and cache by level. */
const loadedChildren = reactive(new Map<number | null, Collection[]>())
/** Current navigation stack; [] = library root. */
const path = ref<Collection[]>([])
/** Items (recordings) in the current folder — only populated in item mode. */
const currentItems = ref<CollectionItem[]>([])

const loading = ref(false)
const loadingItems = ref(false)

/** Whether the inline "new collection" form is visible. */
const isCreating = ref(false)
const saving = ref(false)
const input = reactive({ name: '' })
const saveError = ref<string | null>(null)

// ---------------------------------------------------------------------------
// Derived
// ---------------------------------------------------------------------------

const currentCollection = computed<Collection | null>(() =>
    path.value.length > 0 ? path.value[path.value.length - 1]! : null,
)

const currentParentId = computed<number | null>(() => currentCollection.value?.id ?? null)

/** Children at the current navigation level, read from the lazy cache. */
const visibleCollections = computed<Collection[]>(() =>
    loadedChildren.get(currentParentId.value) ?? [],
)

async function _loadChildren (parentId: number | null) {
    if (loadedChildren.has(parentId)) {
        return
    }
    try {
        const children = parentId === null
            ? await listCollections()
            : await listCollections({ parent_id: parentId, limit: 200 })
        loadedChildren.set(parentId, children)
    } catch {
        loadedChildren.set(parentId, [])
    }
}

const dialogLabel = computed(() =>
    props.title ??
    (props.mode === 'item' ? t('Browse library', SCOPE) : t('Choose upload location', SCOPE)),
)

// ---------------------------------------------------------------------------
// Lifecycle
// ---------------------------------------------------------------------------

watch(() => props.open, async (open) => {
    if (!open) {
        return
    }
    _reset()
    loading.value = true
    try {
        await _loadChildren(null)
    } finally {
        loading.value = false
    }
})

/** Lazy-load the next level's children whenever the user navigates into it. */
watch(currentParentId, async (id) => {
    if (!props.open) {
        return
    }
    await _loadChildren(id)
})

/** Reload items whenever the current folder changes (item mode only). */
watch(currentParentId, async (id) => {
    if (!props.open || props.mode !== 'item') {
        return
    }
    if (id === null) {
        currentItems.value = []
        return
    }
    loadingItems.value = true
    try {
        currentItems.value = await listCollectionItems(id)
    } catch {
        currentItems.value = []
    } finally {
        loadingItems.value = false
    }
})

function _reset () {
    path.value = []
    currentItems.value = []
    isCreating.value = false
    input.name = ''
    saveError.value = null
    loadedChildren.clear()
}

// ---------------------------------------------------------------------------
// Navigation
// ---------------------------------------------------------------------------

function enterFolder (collection: Collection) {
    path.value = [...path.value, collection]
    _cancelCreate()
}

/**
 * Navigate to a specific breadcrumb level.
 * @param index  Index in `path` to navigate to, or -1 for root.
 */
function navigateTo (index: number) {
    path.value = index < 0 ? [] : path.value.slice(0, index + 1)
    _cancelCreate()
}

// ---------------------------------------------------------------------------
// Selection
// ---------------------------------------------------------------------------

function selectHere () {
    emit('select', { type: 'collection', collection: currentCollection.value })
}

function selectItem (item: CollectionItem) {
    emit('select', { type: 'recording', item })
}

// ---------------------------------------------------------------------------
// Inline collection creation
// ---------------------------------------------------------------------------

async function confirmCreate () {
    const name = input.name.trim()
    if (!name) {
        return
    }
    saving.value = true
    saveError.value = null
    try {
        const created = await createCollection({ name, parent_id: currentParentId.value })
        const siblings = loadedChildren.get(currentParentId.value) ?? []
        loadedChildren.set(currentParentId.value, [...siblings, created])
        isCreating.value = false
        input.name = ''
    } catch {
        saveError.value = t('Could not create collection. Please try again.', SCOPE)
    } finally {
        saving.value = false
    }
}

function _cancelCreate () {
    isCreating.value = false
    input.name = ''
    saveError.value = null
}

function handleDialogHide () {
    emit('close')
}

function handleCreateEscape (event: KeyboardEvent) {
    event.preventDefault()
    _cancelCreate()
}

const createNameInput = ref<HTMLElement | null>(null)

function startCreate () {
    isCreating.value = true
    // wa-input does not honour the HTML autofocus attribute when its host is
    // mounted via v-if, so focus must be moved programmatically once Vue has
    // flushed the new DOM node.
    nextTick(() => {
        createNameInput.value?.focus()
    })
}

</script>

<template>
    <wa-dialog
        :open="open"
        :label="dialogLabel"
        class="collection-picker-dialog"
        @wa-hide.self="handleDialogHide"
    >
        <!-- Breadcrumb -------------------------------------------------------- -->
        <wa-breadcrumb class="breadcrumb" :aria-label="t('Collection navigation', SCOPE)">
            <wa-breadcrumb-item
                class="crumb-item"
                :class="{ 'crumb-item--active': path.length === 0 }"
                @click="navigateTo(-1)"
            >
                <wa-icon slot="prefix" name="folders"></wa-icon>
                {{ t('Library', SCOPE) }}
            </wa-breadcrumb-item>
            <wa-breadcrumb-item
                v-for="(coll, idx) in path"
                :key="coll.id"
                class="crumb-item"
                :class="{ 'crumb-item--active': idx === path.length - 1 }"
                @click="navigateTo(idx)"
            >{{ coll.name }}</wa-breadcrumb-item>
        </wa-breadcrumb>

        <!-- Body -------------------------------------------------------------- -->
        <div class="picker-body">

            <!-- Loading -->
            <div v-if="loading" class="picker-state">
                <wa-icon name="spinner" class="spinner-icon spinner-icon--large"></wa-icon>
            </div>

            <!-- Empty -->
            <p
                v-else-if="visibleCollections.length === 0 && currentItems.length === 0 && !isCreating"
                class="picker-state picker-state--empty"
            >
                {{ t('No collections here yet.', SCOPE) }}
            </p>

            <!-- List -->
            <ul v-else class="picker-list">
                <li
                    v-for="coll in visibleCollections"
                    :key="coll.id"
                    class="picker-row picker-row--folder"
                    role="button"
                    tabindex="0"
                    @click="enterFolder(coll)"
                    @keydown.enter="enterFolder(coll)"
                >
                    <wa-icon name="folder" class="picker-row__icon picker-row__icon--folder"></wa-icon>
                    <span class="picker-row__name">{{ coll.name }}</span>
                    <wa-icon name="angle-right" class="picker-row__chevron"></wa-icon>
                </li>

                <!-- Item mode: recordings in current collection -->
                <template v-if="mode === 'item'">
                    <li v-if="loadingItems" class="picker-row picker-row--loading">
                        <wa-icon name="spinner" class="spinner-icon"></wa-icon>
                        <span>{{ t('Loading…', SCOPE) }}</span>
                    </li>
                    <li
                        v-else
                        v-for="item in currentItems"
                        :key="item.id"
                        class="picker-row picker-row--recording"
                        role="button"
                        tabindex="0"
                        @click="selectItem(item)"
                        @keydown.enter="selectItem(item)"
                    >
                        <wa-icon name="file-music" class="picker-row__icon picker-row__icon--file"></wa-icon>
                        <span class="picker-row__name">{{ item.object_name ?? item.object_id }}</span>
                        <wa-button
                            appearance="filled-outlined"
                            size="s"
                            variant="brand"
                            @click.stop="selectItem(item)"
                        >
                            {{ t('Select', SCOPE) }}
                        </wa-button>
                    </li>
                </template>
            </ul>

            <!-- Inline new-collection form -->
            <form v-if="isCreating" class="create-form" @submit.prevent="confirmCreate">
                <wa-input
                    ref="createNameInput"
                    size="s"
                    :disabled="saving"
                    :placeholder="t('Collection name', SCOPE)"
                    v-wa="[input, 'name']"
                    @keydown.escape="handleCreateEscape"
                ></wa-input>
                <wa-button size="s" variant="brand" appearance="filled-outlined" type="submit" :loading="saving">
                    {{ t('Create', SCOPE) }}
                </wa-button>
                <wa-button size="s" appearance="filled-outlined" type="button" @click="_cancelCreate">
                    {{ t('Cancel', SCOPE) }}
                </wa-button>
                <p v-if="saveError" class="create-form__error">{{ saveError }}</p>
            </form>
        </div>

        <!-- Footer ------------------------------------------------------------ -->
        <div slot="footer" class="picker-footer">
            <wa-button
                variant="brand"
                appearance="filled-outlined"
                :disabled="isCreating"
                @click="startCreate"
            >
                <wa-icon slot="start" name="plus"></wa-icon>
                {{ t('New collection', SCOPE) }}
            </wa-button>

            <div class="picker-footer__right">
                <wa-button
                    v-if="mode === 'collection'"
                    variant="brand"
                    appearance="filled-outlined"
                    @click="selectHere"
                >
                    {{ currentCollection ? t('Select this folder', SCOPE) : t('Select root', SCOPE) }}
                </wa-button>
                <wa-button variant="neutral" appearance="filled-outlined" data-dialog="close">
                    {{ t('Cancel', SCOPE) }}
                </wa-button>
            </div>
        </div>
    </wa-dialog>
</template>

<style scoped>
.collection-picker-dialog {
    --width: 520px;
}

/* Breadcrumb --------------------------------------------------------------- */
.breadcrumb {
    padding: 0.5rem 0 0.75rem;
    font-size: 0.875rem;
}

/* Make non-active items feel interactive; the active (last) item is not clickable. */
.crumb-item {
    cursor: pointer;
}

.crumb-item--active {
    cursor: default;
    pointer-events: none;
}

/* Body --------------------------------------------------------------------- */
.picker-body {
    min-height: 180px;
    max-height: 340px;
    overflow-y: auto;
    display: flex;
    flex-direction: column;
}

.picker-state {
    display: flex;
    align-items: center;
    justify-content: center;
    flex: 1;
    padding: 2rem;
    color: var(--wa-color-neutral-500);
}

.spinner-icon {
    animation: spin 1s linear infinite;
}

.spinner-icon--large {
    font-size: 1.5rem;
}

.picker-state--empty {
    font-size: 0.875rem;
    margin: 0;
}

/* List --------------------------------------------------------------------- */
.picker-list {
    list-style: none;
    margin: 0;
    padding: 0;
    flex: 1;
}

.picker-row {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    padding: 0.45rem 0.5rem;
    border-radius: var(--wa-border-radius-s);
    font-size: 0.875rem;
    cursor: pointer;
    transition: background 0.1s;
}

.picker-row:hover {
    background: var(--wa-color-neutral-fill-quiet);
}

.picker-row--folder {
    color: var(--wa-color-neutral-800);
}

.picker-row--recording {
    color: var(--wa-color-neutral-700);
}

.picker-row--loading {
    cursor: default;
    color: var(--wa-color-neutral-400);
    gap: 0.5rem;
}

.picker-row__icon {
    flex-shrink: 0;
    font-size: 0.9rem;
}

.picker-row__icon--folder {
    color: var(--wa-color-warning-500);
}

.picker-row__icon--file {
    color: var(--wa-color-neutral-400);
}

.picker-row__name {
    flex: 1;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}

.picker-row__chevron {
    font-size: 0.75rem;
    color: var(--wa-color-neutral-400);
    flex-shrink: 0;
}

/* Create form -------------------------------------------------------------- */
.create-form {
    display: flex;
    align-items: flex-start;
    flex-wrap: wrap;
    gap: 0.5rem;
    padding: 0.5rem 0.5rem 0.25rem;
    border-top: 1px solid var(--wa-color-neutral-100);
    margin-top: 0.25rem;
}

.create-form wa-input {
    flex: 1;
    min-width: 160px;
}

.create-form__error {
    width: 100%;
    margin: 0;
    font-size: 0.8125rem;
    color: var(--wa-color-danger-600);
}

/* Footer ------------------------------------------------------------------- */
.picker-footer {
    display: flex;
    align-items: center;
    justify-content: space-between;
    width: 100%;
    gap: 0.5rem;
}

.picker-footer__right {
    display: flex;
    gap: 0.5rem;
}

@keyframes spin {
    to { transform: rotate(360deg); }
}
</style>
