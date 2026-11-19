<script setup lang="ts">
/**
 * A dataset's recordings to browse, read-only: the dataset's name, description and licence above the dataset
 * browser, with no management controls. The page a shared link opens, so it reads the dataset with the `?token=`
 * share token when one is given and needs no account then; a signed-in reader without a token reads it as
 * themselves. "Open in viewer" opens the whole dataset with the same token.
 *
 * @package    epicurrents-platform
 */
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { t } from '#i18n'
import { getDataset, type Collection } from '#api/library'
import { errorDetail } from '#lib/http'
import DatasetBrowser from '#components/DatasetBrowser.vue'
import { setPageTitle } from '#router'
import { useAuthStore } from '#stores/auth'

const SCOPE = 'DatasetBrowseView'

const route = useRoute()
const router = useRouter()
const authStore = useAuthStore()

const datasetId = computed(() => String(route.params.id))
const shareToken = computed(() => (route.query.token as string | undefined)?.trim() || undefined)

const dataset = ref<Collection | null>(null)
const loading = ref(true)
const error = ref<string | null>(null)

onMounted(async () => {
    // The browser leaves out tags and codes for a reader with a token and no account, so it waits to know which.
    await authStore.init().catch(() => undefined)
    try {
        dataset.value = await getDataset(datasetId.value, shareToken.value)
        setPageTitle(dataset.value.name)
    } catch (err) {
        error.value = errorDetail(err, t('This dataset could not be opened.', SCOPE))
    } finally {
        loading.value = false
    }
})

function openInViewer () {
    const query: Record<string, string> = { dataset: dataset.value?.object_hash ?? datasetId.value }
    if (shareToken.value) {
        query.token = shareToken.value
    }
    const { href } = router.resolve({ name: 'viewer', query })
    window.open(href, '_blank')
}
</script>

<template>
    <main class="page-view dataset-browse">
        <wa-spinner v-if="loading" class="loading-center"></wa-spinner>

        <wa-callout v-else-if="error" variant="danger">{{ error }}</wa-callout>

        <template v-else-if="dataset">
            <header class="page-header">
                <h1>{{ dataset.name }}</h1>
                <wa-button
                    appearance="filled-outlined"
                    variant="brand"
                    @click="openInViewer"
                >
                    <wa-icon name="arrow-up-right-from-square" slot="start"></wa-icon>
                    {{ t('Open in viewer', SCOPE) }}
                </wa-button>
            </header>
            <p v-if="dataset.description" class="dataset-browse__description">{{ dataset.description }}</p>
            <p v-if="dataset.license_spdx" class="dataset-browse__licence">
                {{ t('Licence', SCOPE) }}:
                <a v-if="dataset.license_url"
                    :href="dataset.license_url"
                    rel="noopener noreferrer"
                    target="_blank"
                >
                    {{ dataset.license_spdx }}
                </a>
                <span v-else>{{ dataset.license_spdx }}</span>
            </p>
            <DatasetBrowser
                :can-manage="false"
                :dataset-id="dataset.object_hash ?? datasetId"
                :share-token="shareToken"
            ></DatasetBrowser>
        </template>
    </main>
</template>

<style scoped>
.dataset-browse {
    /* The browser's table carries a column per subject field, so the page takes more width than a form page. */
    max-width: 1280px;
}

.dataset-browse__description {
    margin: 0 0 var(--wa-space-s);
    overflow-wrap: anywhere;
    white-space: pre-line;
}

.dataset-browse__licence {
    color: var(--wa-color-text-quiet);
    font-size: var(--wa-font-size-s);
    margin: 0 0 var(--wa-space-m);
}
</style>
