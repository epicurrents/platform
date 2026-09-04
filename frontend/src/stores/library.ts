import { defineStore } from 'pinia'
import { ref } from 'vue'
import {
    listCollections,
    createCollection,
    listDatasets,
    createDataset,
    type Collection,
} from '#api/library'

/**
 * Pinia store for the user's Collections (Library) and Datasets.
 *
 * Only top-level lists are managed here.  Items and access rights for a
 * specific collection/dataset are fetched locally by the detail views.
 */
export const useLibraryStore = defineStore('library', () => {
    // ── Collections ──────────────────────────────────────────────────────
    const collections = ref<Collection[]>([])
    const collectionsLoading = ref(false)
    const collectionsError = ref<string | null>(null)

    async function loadCollections() {
        collectionsLoading.value = true
        collectionsError.value = null
        try {
            collections.value = await listCollections()
        } catch (e) {
            collectionsError.value = e instanceof Error ? e.message : 'Failed to load collections'
        } finally {
            collectionsLoading.value = false
        }
    }

    async function addCollection(name: string, description = ''): Promise<Collection> {
        const created = await createCollection({ name, description })
        collections.value.push(created)
        return created
    }

    function removeCollection(id: number) {
        const idx = collections.value.findIndex(c => c.id === id)
        if (idx !== -1) collections.value.splice(idx, 1)
    }

    // ── Datasets ─────────────────────────────────────────────────────────
    const datasets = ref<Collection[]>([])
    const datasetsLoading = ref(false)
    const datasetsError = ref<string | null>(null)

    async function loadDatasets() {
        datasetsLoading.value = true
        datasetsError.value = null
        try {
            datasets.value = await listDatasets()
        } catch (e) {
            datasetsError.value = e instanceof Error ? e.message : 'Failed to load datasets'
        } finally {
            datasetsLoading.value = false
        }
    }

    async function addDataset(name: string, description = ''): Promise<Collection> {
        const created = await createDataset({ name, description })
        datasets.value.push(created)
        return created
    }

    function removeDataset(id: number) {
        const idx = datasets.value.findIndex(d => d.id === id)
        if (idx !== -1) datasets.value.splice(idx, 1)
    }

    return {
        collections, collectionsLoading, collectionsError,
        loadCollections, addCollection, removeCollection,
        datasets, datasetsLoading, datasetsError,
        loadDatasets, addDataset, removeDataset,
    }
})
