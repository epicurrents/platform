<script lang="ts">
// ---------------------------------------------------------------------------
// Inject key + context type (exported so CreateDatasetDialog can provide it)
// ---------------------------------------------------------------------------
import type { Collection, CollectionItem } from '#api/library'

export const TREE_CTX_KEY = Symbol('collection-tree-context')

export interface TreeContext {
    /** Children loaded per collection ID. */
    loadedChildren: Map<number, Collection[]>
    /** Recording items loaded per collection ID. */
    loadedItems: Map<number, CollectionItem[]>
    /** Set of collection IDs currently being fetched. */
    loadingIds: Set<number>
    /** Trigger lazy-loading of a collection's children + items. */
    loadCollection: (id: number) => void
}
</script>

<script setup lang="ts">
/**
 * CollectionTreeItem — renders one Collection as a wa-tree-item with its
 * sub-collections and recordings as children, recursively.
 *
 * All mutable state (loadedChildren, loadedItems, …) lives in the parent
 * CreateDatasetDialog and is shared via provide/inject using TREE_CTX_KEY.
 * This component only reads state and calls the provided handlers.
 *
 * Selection is handled natively by wa-tree selection="multiple"; each tree item
 * carries data-collection-id or data-recording-hash so the parent can identify
 * selections from wa-selection-change events.
 */
import { inject, computed } from 'vue'
import { t } from '#i18n'
// eslint-disable-next-line import/no-self-import
import CollectionTreeItem from './CollectionTreeItem.vue'

const SCOPE = 'CollectionTreeItem'

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

interface Props {
    collection: Collection
}

const props = defineProps<Props>()

// ---------------------------------------------------------------------------
// Context
// ---------------------------------------------------------------------------

const ctx = inject<TreeContext>(TREE_CTX_KEY)!

const children = computed<Collection[]>(() => ctx.loadedChildren.get(props.collection.id) ?? [])
const items = computed<CollectionItem[]>(() => ctx.loadedItems.get(props.collection.id) ?? [])
const isLoaded = computed(() => ctx.loadedChildren.has(props.collection.id))
const isLoading = computed(() => ctx.loadingIds.has(props.collection.id))

// ---------------------------------------------------------------------------
// Handlers
// ---------------------------------------------------------------------------

function onExpand () {
    if (!isLoaded.value) {
        ctx.loadCollection(props.collection.id)
    }
}
</script>

<template>
    <wa-tree-item
        :data-collection-id="collection.id"
        :data-collection-name="collection.name"
        @wa-expand="onExpand"
    >
        <wa-icon class="icon-folder" name="folder"></wa-icon>
        {{ collection.name }}

        <!-- Placeholder child ensures the expand toggle is visible before loading.
             Shows a spinner while the fetch is in flight. -->
        <wa-tree-item v-if="!isLoaded" disabled slot="children">
            <wa-spinner v-if="isLoading" class="item-spinner"></wa-spinner>
        </wa-tree-item>

        <!-- Loaded: sub-collections, recording items, and empty state -->
        <template v-if="isLoaded">
            <CollectionTreeItem v-for="child in children" :key="child.id"
                :collection="child"
                slot="children"
            />

            <wa-tree-item v-for="item in items" :key="item.id"
                :data-recording-hash="item.object_hash"
                :data-recording-name="item.object_name ?? item.object_id"
                slot="children"
            >
                <wa-icon class="icon-file" name="file-music"></wa-icon>
                {{ item.object_name ?? item.object_id }}
            </wa-tree-item>

            <wa-tree-item v-if="!children.length && !items.length" disabled slot="children">
                <span class="empty-note">{{ t('Empty collection', SCOPE) }}</span>
            </wa-tree-item>
        </template>
    </wa-tree-item>
</template>

<style scoped>
.icon-folder {
    color: var(--wa-color-warning-500);
    flex-shrink: 0;
}

.icon-file {
    color: var(--wa-color-neutral-400);
    flex-shrink: 0;
}

.empty-note {
    color: var(--wa-color-neutral-400);
    font-size: 0.8125rem;
    font-style: italic;
}

.item-spinner {
    font-size: 0.75rem;
}
</style>
