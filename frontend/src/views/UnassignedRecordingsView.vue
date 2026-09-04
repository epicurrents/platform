<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useRecordingSelection } from '#composables/useRecordingSelection'
import { t } from '#i18n'
import RecordingListRow from '#components/RecordingListRow.vue'
import EditRecordingDialog from '#components/EditRecordingDialog.vue'
import CollectionPickerDialog, { type PickerSelection } from '#components/CollectionPickerDialog.vue'
import { addCollectionItem, getRecordingContentTypeId } from '#api/library'
import { listRecordings, recordingName, type Recording } from '#api/recordings'
import { showToast } from '#lib/toast'

const SCOPE = 'UnassignedRecordingsView'
const router = useRouter()

/** Page size for the offset-based "Load more" pagination. */
const PAGE_SIZE = 50

const recordings = ref<Recording[]>([])
const offset = ref(0)
const hasMore = ref(true)
const loading = ref(true)
const loadingMore = ref(false)
const error = ref<string | null>(null)

/**
 * Fetch the next page of root (uncollected) recordings, or restart from the top
 * when `reset` is true. The list endpoint returns no total, so a full page means
 * more may remain — that drives the "Load more" affordance.
 */
async function loadPage (reset = false) {
    if (reset) {
        recordings.value = []
        offset.value = 0
        hasMore.value = true
    }
    if (offset.value === 0) {
        loading.value = true
    } else {
        loadingMore.value = true
    }
    error.value = null
    try {
        const page = await listRecordings(PAGE_SIZE, offset.value, { uncollected: true })
        recordings.value.push(...page)
        offset.value += page.length
        hasMore.value = page.length === PAGE_SIZE
    } catch {
        error.value = t('Failed to load recordings. Please try again.', SCOPE)
    } finally {
        loading.value = false
        loadingMore.value = false
    }
}

onMounted(() => loadPage(true))

const listRef = ref<HTMLElement | null>(null)
const { selected, focusedHash, onRowClick, onCheckboxClick, clearSelection } = useRecordingSelection(
    () => recordings.value.map(r => r.hash),
    listRef,
)

function openRecordings (hashes: string[]) {
    const { href } = router.resolve({ name: 'viewer', query: { files: hashes.join(',') } })
    window.open(href, '_blank')
}

function openSelectionInViewer () {
    openRecordings([...selected.value])
    clearSelection()
}

function goToLibrary () {
    router.push({ name: 'library' })
}

// ── Move selected recordings into a collection ─────────────────────────────

const showPicker = ref(false)
const moving = ref(false)
const moveTargets = ref<string[]>([])

/** Open the collection picker to move the given recordings — either one row (from
 *  its context menu) or the current selection (from the action bar). */
function startMove (hashes: string[]) {
    if (!hashes.length) {
        return
    }
    moveTargets.value = hashes
    showPicker.value = true
}

function onRowAction (action: string, rec: Recording) {
    if (action === 'move') {
        startMove([rec.hash])
    } else if (action === 'edit') {
        openEdit(rec)
    }
}

// ── Edit recording ─────────────────────────────────────────────────────────

const editingRec = ref<Recording | null>(null)

function openEdit (rec: Recording) {
    editingRec.value = rec
}

function onEdited (updated: Recording) {
    const idx = recordings.value.findIndex(r => r.hash === updated.hash)
    if (idx !== -1) {
        recordings.value[idx] = updated
    }
}

async function onPickCollection (selection: PickerSelection) {
    if (selection.type !== 'collection' || !selection.collection) {
        return
    }
    showPicker.value = false
    const target = selection.collection
    const hashes = moveTargets.value
    moving.value = true
    try {
        const ctId = await getRecordingContentTypeId()
        // Add each recording to the target; a per-item failure returns null so one
        // bad row never aborts the rest. Moved recordings now belong to a collection
        // and leave the root list.
        const moved = new Set(
            (await Promise.all(
                hashes.map(async (hash) => {
                    try {
                        await addCollectionItem(target.id, { content_type_id: ctId, object_id: hash })
                        return hash
                    } catch {
                        return null
                    }
                }),
            )).filter((hash): hash is string => hash !== null),
        )
        recordings.value = recordings.value.filter(r => !moved.has(r.hash))
        clearSelection()
        moveTargets.value = []
        const failed = hashes.length - moved.size
        if (failed > 0) {
            showToast(t('{count} could not be moved.', SCOPE, { count: failed }), 'warning')
        } else {
            showToast(t('Moved {count} to {name}.', SCOPE, { count: moved.size, name: target.name }), 'success')
        }
    } catch {
        showToast(t('Failed to move recordings. Please try again.', SCOPE), 'danger')
    } finally {
        moving.value = false
    }
}

const selectionCount = computed(() => selected.value.size)
</script>

<template>
    <main class="page-view">
        <header class="page-header">
            <div class="header-titles">
                <wa-button
                    appearance="plain"
                    size="s"
                    @click="goToLibrary"
                >
                    <wa-icon name="arrow-left" slot="start"></wa-icon>
                    {{ t('Library', SCOPE) }}
                </wa-button>
                <h1>{{ t('Unassigned recordings', SCOPE) }}</h1>
            </div>
        </header>

        <wa-callout variant="neutral">
            {{ t('Recordings that are not in any collection. Select recordings to move them into a collection.', SCOPE) }}
        </wa-callout>

        <wa-spinner v-if="loading" class="loading-center"></wa-spinner>

        <wa-callout v-else-if="error" variant="danger">{{ error }}</wa-callout>

        <p v-else-if="!recordings.length" class="empty-state">
            {{ t('No unassigned recordings. Everything is organised into collections.', SCOPE) }}
        </p>

        <template v-else>
            <div v-if="selectionCount > 0" class="selection-bar">
                <span class="selection-bar__count">
                    {{ t('{count} selected', SCOPE, { count: selectionCount }) }}
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
                    appearance="plain"
                    size="s"
                    @click="openSelectionInViewer"
                >
                    <wa-icon name="arrow-up-right-from-square" slot="start"></wa-icon>
                    {{
                        selectionCount === 1
                        ? t('Open recording', SCOPE)
                        : t('Open {count} recordings', SCOPE, { count: selectionCount })
                    }}
                </wa-button>
                <wa-button
                    appearance="filled-outlined"
                    :loading="moving"
                    size="s"
                    variant="brand"
                    @click="startMove([...selected])"
                >
                    <wa-icon name="folder-plus" slot="start"></wa-icon>
                    {{ t('Move to a collection', SCOPE) }}
                </wa-button>
            </div>

            <div ref="listRef" class="list-rows">
                <RecordingListRow
                    v-for="rec in recordings"
                    :key="rec.hash"
                    :hash="rec.hash"
                    :is-focused="focusedHash === rec.hash"
                    :is-selected="selected.has(rec.hash)"
                    :name="recordingName(rec)"
                    :selection-active="selectionCount > 0"
                    @checkbox-click="onCheckboxClick(rec.hash)"
                    @dropdown-action="onRowAction($event, rec)"
                    @open="openRecordings([rec.hash])"
                    @row-click="onRowClick(rec.hash, $event)"
                    @row-dblclick="openRecordings([rec.hash])"
                >
                    <template #meta>
                        <span class="list-row-meta">
                            <wa-relative-time :date="rec.created_at"></wa-relative-time>
                            <wa-badge
                                v-if="rec.trashed_collection"
                                class="trashed-cue"
                                :title="t('Its collection {name} is in the trash; restore it to re-file this recording.', SCOPE, { name: rec.trashed_collection.name })"
                                variant="neutral"
                            >
                                <wa-icon name="trash" slot="start"></wa-icon>
                                {{ t('In trash: {name}', SCOPE, { name: rec.trashed_collection.name }) }}
                            </wa-badge>
                        </span>
                    </template>
                    <template #actions>
                        <wa-dropdown-item value="edit">
                            <wa-icon name="pencil" slot="icon"></wa-icon>
                            {{ t('Edit', SCOPE) }}
                        </wa-dropdown-item>
                        <wa-dropdown-item value="move">
                            <wa-icon name="folder-plus" slot="icon"></wa-icon>
                            {{ t('Move to a collection', SCOPE) }}
                        </wa-dropdown-item>
                    </template>
                </RecordingListRow>
            </div>

            <div v-if="hasMore" class="load-more">
                <wa-button
                    appearance="plain"
                    :loading="loadingMore"
                    @click="loadPage(false)"
                >
                    {{ t('Load more', SCOPE) }}
                </wa-button>
            </div>
        </template>
    </main>

    <CollectionPickerDialog
        mode="collection"
        :open="showPicker"
        :title="t('Move to a collection', SCOPE)"
        @close="showPicker = false"
        @select="onPickCollection"
    >
    </CollectionPickerDialog>

    <EditRecordingDialog
        :recording="editingRec"
        @close="editingRec = null"
        @updated="onEdited"
    />
</template>

<style scoped>
.header-titles {
    /* flex-start so the back button keeps its own narrow width instead of
       stretching to the title's width below it. */
    align-items: flex-start;
    display: flex;
    flex-direction: column;
    gap: 0.25rem;
}

/* The global .selection-bar only carries a top margin; add one below so the
   action bar does not sit flush against the first recording. */
.selection-bar {
    margin-bottom: var(--wa-space-xs);
}

.load-more {
    display: flex;
    justify-content: center;
    padding: 0.5rem 0 1rem;
}

.trashed-cue {
    margin-inline-start: 0.5rem;
}
</style>
