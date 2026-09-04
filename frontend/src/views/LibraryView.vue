<script setup lang="ts">
import { reactive, ref, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { t } from '#i18n'
import { useLibraryStore } from '#stores/library'
import { deleteCollection } from '#api/library'
import { listRecordings } from '#api/recordings'
import { showToast } from '#lib/toast'
import type { Collection } from '#api/library'

const SCOPE = 'LibraryView'
const router = useRouter()
const store = useLibraryStore()

/** Count of recordings that failed processing, driving the "Needs attention" entry. */
const failedCount = ref(0)

onMounted(async () => {
    store.loadCollections()
    try {
        failedCount.value = (await listRecordings(200, 0, { status: 'failed' })).length
    } catch {
        failedCount.value = 0
    }
})

function goToUnassigned () {
    router.push({ name: 'unassigned-recordings' })
}

function goToNeedsAttention () {
    router.push({ name: 'needs-attention' })
}

// ── Create collection ─────────────────────────────────────────────────────

const showCreate = ref(false)
const input = reactive({ name: '', description: '' })
const createLoading = ref(false)
const createError = ref<string | null>(null)

function openCreate () {
    input.name = ''
    input.description = ''
    createError.value = null
    showCreate.value = true
}

function closeCreate () {
    showCreate.value = false
}

function openCollection (colId: number) {
    router.push({ name: 'collection', params: { id: colId } })
}

async function submitCreate () {
    if (!input.name.trim()) {
        createError.value = 'Name is required.'
        return
    }
    createLoading.value = true
    createError.value = null
    try {
        const created = await store.addCollection(input.name.trim(), input.description.trim())
        showCreate.value = false
        router.push({ name: 'collection', params: { id: created.id } })
    } catch {
        createError.value = 'Failed to create collection. Please try again.'
    } finally {
        createLoading.value = false
    }
}

// ── Delete collection ─────────────────────────────────────────────────────

const deletingCol = ref<Collection | null>(null)
const deleteLoading = ref(false)

function openDelete (col: Collection) {
    deletingCol.value = col
}

function closeDelete () {
    deletingCol.value = null
}

async function confirmDelete () {
    if (!deletingCol.value) {
        return
    }
    deleteLoading.value = true
    try {
        await deleteCollection(deletingCol.value.id)
        store.removeCollection(deletingCol.value.id)
        showToast(`"${deletingCol.value.name}" moved to trash.`, 'neutral')
        closeDelete()
    } catch {
        showToast('Failed to delete collection. Please try again.', 'danger')
    } finally {
        deleteLoading.value = false
    }
}

function formatDate (iso: string) {
    return new Date(iso).toLocaleDateString(undefined, {
        year: 'numeric', month: 'short', day: 'numeric',
    })
}
</script>

<template>
    <main class="page-view">
        <header class="page-header">
            <h1>{{ t('Library', SCOPE) }}</h1>
            <wa-button
                appearance="filled-outlined"
                size="s"
                variant="brand"
                @click="openCreate"
            >
                <wa-icon name="plus" slot="start"></wa-icon>
                {{ t('New collection', SCOPE) }}
            </wa-button>
        </header>

        <i18n-t
            keypath="LibraryView.library_callout"
            tag="wa-callout"
            variant="neutral"
        >
            <template #dataset>
                <strong>{{ t('Datasets', SCOPE) }}</strong>
            </template>
        </i18n-t>

        <div class="list-rows">
            <div class="list-row clickable unassigned-row" @click="goToUnassigned">
                <div class="list-row-main">
                    <wa-icon name="file"></wa-icon>
                    <span class="list-row-name">{{ t('Unassigned recordings', SCOPE) }}</span>
                    <span class="list-row-meta">{{ t('Recordings not in any collection', SCOPE) }}</span>
                    <div class="list-row-actions">
                        <wa-button
                            appearance="plain"
                            size="s"
                            variant="brand"
                            @click.stop="goToUnassigned"
                        >
                            <wa-icon name="arrow-right"></wa-icon>
                        </wa-button>
                    </div>
                </div>
            </div>
            <div
                v-if="failedCount > 0"
                class="list-row clickable attention-row"
                @click="goToNeedsAttention"
            >
                <div class="list-row-main">
                    <wa-icon name="triangle-exclamation"></wa-icon>
                    <span class="list-row-name">{{ t('Needs attention', SCOPE) }}</span>
                    <span class="list-row-meta">{{ t('Recordings that failed processing', SCOPE) }}</span>
                    <wa-badge pill variant="danger">{{ failedCount }}</wa-badge>
                    <div class="list-row-actions">
                        <wa-button
                            appearance="plain"
                            size="s"
                            variant="brand"
                            @click.stop="goToNeedsAttention"
                        >
                            <wa-icon name="arrow-right"></wa-icon>
                        </wa-button>
                    </div>
                </div>
            </div>
        </div>

        <wa-divider></wa-divider>

        <wa-spinner v-if="store.collectionsLoading" class="loading-center"></wa-spinner>

        <wa-callout v-else-if="store.collectionsError" variant="danger">
            {{ store.collectionsError }}
        </wa-callout>

        <p v-else-if="!store.collections.length" class="empty-state">
            {{ t('No collections yet. Create one to start organising your recordings.', SCOPE) }}
        </p>

        <div v-else class="list-rows">
            <div
                v-for="col in store.collections"
                :key="col.id"
                class="list-row clickable"
                @click="openCollection(col.id)"
            >
                <div class="list-row-main">
                    <wa-icon class="icon-muted" name="folder"></wa-icon>
                    <span class="list-row-name">{{ col.name }}</span>
                    <span class="list-row-meta">{{ formatDate(col.created_at) }}</span>
                    <div class="list-row-actions">
                        <wa-dropdown placement="bottom-end" @click.stop @wa-select.stop="openDelete(col)">
                            <wa-button
                                appearance="plain"
                                size="s"
                                slot="trigger"
                                variant="text"
                            >
                                <wa-icon name="ellipsis"></wa-icon>
                            </wa-button>
                            <wa-dropdown-item value="delete" variant="danger">
                                <wa-icon name="trash" slot="icon"></wa-icon>
                                {{ t('Move to trash', SCOPE) }}
                            </wa-dropdown-item>
                        </wa-dropdown>
                    </div>
                </div>
                <p v-if="col.description" class="col-description">{{ col.description }}</p>
            </div>
        </div>
    </main>

    <!-- Create collection dialog -->
    <wa-dialog :label="t('New collection', SCOPE)" :open="showCreate" @wa-hide.self="closeCreate">
        <div class="dialog-form">
            <wa-callout v-if="createError" variant="danger">{{ createError }}</wa-callout>
            <wa-input
                autofocus
                :disabled="createLoading"
                :label="t('Name', SCOPE)"
                :placeholder="t('e.g. Patient cohort A', SCOPE)"
                size="s"
                type="text"
                v-wa="[input, 'name']"
            ></wa-input>
            <wa-textarea
                :disabled="createLoading"
                :label="t('Description', SCOPE)"
                :placeholder="t('Optional description', SCOPE)"
                rows="3"
                size="s"
                v-wa="[input, 'description']"
            ></wa-textarea>
        </div>
        <div slot="footer" class="form-actions">
            <wa-button
                appearance="filled-outlined"
                :disabled="createLoading"
                variant="neutral"
                @click="closeCreate"
            >
                {{ t('Cancel', SCOPE) }}
            </wa-button>
            <wa-button
                appearance="filled-outlined"
                :loading="createLoading"
                variant="brand"
                @click="submitCreate"
            >
                {{ t('Create', SCOPE) }}
            </wa-button>
        </div>
    </wa-dialog>

    <!-- Delete collection dialog -->
    <wa-dialog :label="t('Move to trash', SCOPE)" :open="!!deletingCol" @wa-hide.self="closeDelete">
        <i18n-t class="dialog-text" keypath="LibraryView.move_to_trash_confirm" tag="p">
            <template #name>
                <strong>{{ deletingCol?.name }}</strong>
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
</template>

<style scoped>
.page-view wa-divider {
    margin: var(--wa-space-xs) 0;
}

.icon-muted {
    color: var(--wa-color-text-quiet);
    flex-shrink: 0;
}

/* Brand treatment so the library-root link stands out from the neutral
   collection rows. The trailing button carries variant="brand" itself. */
.list-row.unassigned-row:hover {
    background-color: var(--wa-color-brand-fill-quiet);
}

.unassigned-row .list-row-name,
.unassigned-row .list-row-main > wa-icon {
    color: var(--wa-color-brand-on-normal);
}

.unassigned-row .list-row-actions {
    opacity: 1;
}

.attention-row .list-row-main > wa-icon {
    color: var(--wa-color-danger-fill-loud);
}

.attention-row .list-row-actions {
    opacity: 1;
}

.dialog-form {
    display: flex;
    flex-direction: column;
    gap: 1rem;
}

.dialog-text {
    margin: 0;
}

.col-description {
    margin: 0 0 0 calc(20px + var(--wa-space-s));
    font-size: 0.875rem;
    color: var(--wa-color-text-quiet);
}
</style>
