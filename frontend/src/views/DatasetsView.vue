<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { t } from '#i18n'
import CreateDatasetDialog from '#components/CreateDatasetDialog.vue'
import { useLibraryStore } from '#stores/library'
import { deleteDataset } from '#api/library'
import { showToast } from '#lib/toast'
import type { Collection } from '#api/library'

const SCOPE = 'DatasetsView'
const router = useRouter()
const store = useLibraryStore()

onMounted(() => store.loadDatasets())

// ── Create dataset ────────────────────────────────────────────────────────

const showCreate = ref(false)

function openCreate () {
    showCreate.value = true
}

function closeCreate () {
    showCreate.value = false
}

function openDataset (ds: Collection) {
    // Address the route by the opaque hash so dataset URLs expose no
    // sequential PK; the backend accepts either form.
    router.push({ name: 'dataset', params: { id: ds.object_hash ?? ds.id } })
}

// ── Delete dataset ────────────────────────────────────────────────────────

const deletingDs = ref<Collection | null>(null)
const deleteLoading = ref(false)

function openInViewer (ds: Collection) {
    const { href } = router.resolve({ name: 'viewer', query: { dataset: ds.object_hash ?? ds.id } })
    window.open(href, '_blank')
}

function openDelete (ds: Collection) {
    deletingDs.value = ds
}

function handleDatasetAction (event: Event, ds: Collection) {
    const value = (event as CustomEvent<{ item: { value: string } }>).detail.item.value
    if (value === 'delete') {
        openDelete(ds)
    } else {
        openInViewer(ds)
    }
}

function closeDelete () {
    deletingDs.value = null
}

async function confirmDelete () {
    if (!deletingDs.value) {
        return
    }
    deleteLoading.value = true
    try {
        await deleteDataset(deletingDs.value.id)
        store.removeDataset(deletingDs.value.id)
        showToast(`"${deletingDs.value.name}" moved to trash.`, 'neutral')
        closeDelete()
    } catch {
        showToast('Failed to delete dataset. Please try again.', 'danger')
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
            <h1>{{ t('Datasets', SCOPE) }}</h1>
            <wa-button
                appearance="filled-outlined"
                size="s"
                variant="brand"
                @click="openCreate"
            >
                <wa-icon name="plus" slot="start"></wa-icon>
                {{ t('New dataset', SCOPE) }}
            </wa-button>
        </header>

        <wa-callout variant="neutral">
            {{
                t(
                    'Datasets are sets of recordings that can be shared with other users. ' +
                    'A single recording can belong to multiple datasets.',
                    SCOPE,
                )
            }}
        </wa-callout>

        <wa-spinner v-if="store.datasetsLoading" class="loading-center"></wa-spinner>

        <wa-callout v-else-if="store.datasetsError" variant="danger">
            {{ store.datasetsError }}
        </wa-callout>

        <p v-else-if="!store.datasets.length" class="empty-state">
            {{ t('No datasets yet. Create one to share recordings with collaborators.', SCOPE) }}
        </p>

        <div v-else class="list-rows">
            <div v-for="ds in store.datasets"
                :key="ds.id"
                class="list-row clickable"
                @click="openDataset(ds)"
            >
                <div class="list-row-main">
                    <wa-icon class="icon-muted" name="database"></wa-icon>
                    <span class="list-row-name">{{ ds.name }}</span>
                    <span class="list-row-meta">{{ formatDate(ds.created_at) }}</span>
                    <div class="list-row-actions">
                        <wa-dropdown
                            placement="bottom-end"
                            @click.stop
                            @wa-select.stop="handleDatasetAction($event, ds)"
                        >
                            <wa-button
                                appearance="plain"
                                size="s"
                                slot="trigger"
                                variant="text"
                            >
                                <wa-icon name="ellipsis"></wa-icon>
                            </wa-button>
                            <wa-dropdown-item value="viewer">
                                <wa-icon name="arrow-up-right-from-square" slot="icon"></wa-icon>
                                {{ t('Open in viewer', SCOPE) }}
                            </wa-dropdown-item>
                            <wa-dropdown-item value="delete" variant="danger">
                                <wa-icon name="trash" slot="icon"></wa-icon>
                                {{ t('Move to trash', SCOPE) }}
                            </wa-dropdown-item>
                        </wa-dropdown>
                    </div>
                </div>
                <p v-if="ds.description" class="ds-description">{{ ds.description }}</p>
            </div>
        </div>
    </main>

    <CreateDatasetDialog
        :open="showCreate"
        @close="closeCreate"
    />

    <!-- Delete dataset dialog -->
    <wa-dialog :label="t('Move to trash', SCOPE)" :open="!!deletingDs" @wa-hide.self="closeDelete">
        <i18n-t class="dialog-text" keypath="DatasetsView.move_to_trash_confirm" tag="p">
            <template #name>
                <strong>{{ deletingDs?.name }}</strong>
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
.icon-muted {
    color: var(--wa-color-text-quiet);
    flex-shrink: 0;
}

.dialog-text {
    margin: 0;
}

.ds-description {
    color: var(--wa-color-text-quiet);
    font-size: 0.875rem;
    margin: 0 0 0 calc(20px + var(--wa-space-s));
}
</style>
