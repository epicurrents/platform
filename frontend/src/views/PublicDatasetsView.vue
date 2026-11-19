<script setup lang="ts">
/**
 * The public dataset listing: the datasets their authors list publicly, each linking to its read-only browser and
 * to the viewer through the share token it is listed with. Needs no account. Every dataset here republishes data its
 * publisher already releases, so each entry names the published datasets its recordings come from.
 *
 * A deployment that does not serve the listing answers 404, which the page shows as the listing being unavailable
 * rather than as an error.
 *
 * @package    epicurrents-platform
 */
import { onMounted, ref } from 'vue'
import { RouterLink, useRouter } from 'vue-router'
import axios from 'axios'
import { t } from '#i18n'
import { listPublicDatasets, type PublicDataset } from '#api/library'
import { errorDetail } from '#lib/http'
import { useAuthStore } from '#stores/auth'

const SCOPE = 'PublicDatasetsView'

const router = useRouter()
const authStore = useAuthStore()

const entries = ref<PublicDataset[]>([])
const loading = ref(true)
const unavailable = ref(false)
const error = ref<string | null>(null)

onMounted(async () => {
    authStore.init().catch(() => undefined)
    try {
        entries.value = await listPublicDatasets()
    } catch (err) {
        if (axios.isAxiosError(err) && err.response?.status === 404) {
            unavailable.value = true
        } else {
            error.value = errorDetail(err, t('Failed to load the public datasets.', SCOPE))
        }
    } finally {
        loading.value = false
    }
})

/**
 * The read-only browser of an entry, through its listed token.
 * @param entry - A listed dataset.
 */
function browseRoute (entry: PublicDataset) {
    return { name: 'dataset-browse', params: { id: entry.object_hash }, query: { token: entry.share_token } }
}

/**
 * Open an entry's whole dataset in the viewer through its listed token.
 * @param entry - A listed dataset.
 */
function openInViewer (entry: PublicDataset) {
    const { href } = router.resolve({
        name: 'viewer',
        query: { dataset: entry.object_hash, token: entry.share_token },
    })
    window.open(href, '_blank')
}

/**
 * The facts line of an entry: the recording count and the modalities.
 * @param entry - A listed dataset.
 */
function facts (entry: PublicDataset) {
    const parts = [t('{count} recordings', SCOPE, { count: entry.recording_count })]
    if (entry.modalities.length) {
        parts.push(entry.modalities.map(modality => modality.toUpperCase()).join(', '))
    }
    return parts.join(' · ')
}

/**
 * A published source as a link: a DOI through the DOI resolver, a URL as it is.
 * @param source - A recording's `public_source`.
 */
function sourceHref (source: string) {
    return /^https?:\/\//i.test(source) ? source : `https://doi.org/${source}`
}
</script>

<template>
    <main class="page-view">
        <header class="page-header">
            <h1>{{ t('Public datasets', SCOPE) }}</h1>
            <RouterLink v-if="!authStore.isAuthenticated" :to="{ name: 'login' }">
                {{ t('Sign in', SCOPE) }}
            </RouterLink>
        </header>

        <wa-callout variant="neutral">
            {{
                t(
                    'Datasets republished here from their original publishers. Anyone can browse their recordings ' +
                    'and open them in the viewer without an account. The recordings are de-identified copies, and ' +
                    'each dataset names the publication its recordings come from.',
                    SCOPE,
                )
            }}
        </wa-callout>

        <wa-spinner v-if="loading" class="loading-center"></wa-spinner>

        <p v-else-if="unavailable" class="empty-state">
            {{ t('This site does not list datasets publicly.', SCOPE) }}
        </p>

        <wa-callout v-else-if="error" variant="danger">{{ error }}</wa-callout>

        <p v-else-if="!entries.length" class="empty-state">
            {{ t('No datasets are listed publicly yet.', SCOPE) }}
        </p>

        <ul v-else class="public-datasets">
            <li v-for="entry in entries" :key="entry.object_hash" class="public-datasets__entry">
                <RouterLink class="public-datasets__name" :to="browseRoute(entry)">
                    <wa-icon class="icon-muted" name="database"></wa-icon>
                    {{ entry.name }}
                </RouterLink>
                <span v-if="entry.description" class="public-datasets__description">{{ entry.description }}</span>
                <span class="public-datasets__facts">{{ facts(entry) }}</span>
                <span v-if="entry.public_sources.length" class="public-datasets__facts">
                    {{ t('Source', SCOPE) }}:
                    <template v-for="(source, index) in entry.public_sources" :key="source">
                        <template v-if="index">, </template>
                        <a
                            :href="sourceHref(source)"
                            rel="noopener noreferrer"
                            target="_blank"
                        >
                            {{ source }}
                        </a>
                    </template>
                </span>
                <span v-if="entry.subject_fields.length || entry.license_spdx" class="public-datasets__tags">
                    <wa-tag v-if="entry.license_spdx" size="s" variant="brand">{{ entry.license_spdx }}</wa-tag>
                    <wa-tag v-for="label in entry.subject_fields" :key="label"
                        size="s"
                        variant="neutral"
                    >
                        {{ label }}
                    </wa-tag>
                </span>
                <div class="public-datasets__actions">
                    <RouterLink :to="browseRoute(entry)">{{ t('Browse recordings', SCOPE) }}</RouterLink>
                    <wa-button
                        appearance="plain"
                        size="s"
                        variant="brand"
                        @click="openInViewer(entry)"
                    >
                        <wa-icon name="arrow-up-right-from-square" slot="start"></wa-icon>
                        {{ t('Open in viewer', SCOPE) }}
                    </wa-button>
                </div>
            </li>
        </ul>
    </main>
</template>

<style scoped>
.public-datasets {
    display: flex;
    flex-direction: column;
    gap: var(--wa-space-s);
    list-style: none;
    margin: 0;
    padding: 0;
}

.public-datasets__entry {
    background: var(--wa-color-surface-default);
    border: var(--wa-border-width-s) solid var(--wa-color-surface-border);
    border-radius: var(--wa-border-radius-m);
    display: flex;
    flex-direction: column;
    gap: var(--wa-space-2xs);
    /* Web Awesome's native list styles indent an item; a card list sits flush with the column. */
    margin: 0;
    padding: var(--wa-space-m);
}

.public-datasets__name {
    align-items: center;
    color: inherit;
    display: flex;
    font-weight: var(--wa-font-weight-semibold);
    gap: var(--wa-space-xs);
    text-decoration: none;
}

.public-datasets__name:hover {
    text-decoration: underline;
}

.public-datasets__description {
    overflow-wrap: anywhere;
}

.public-datasets__facts {
    color: var(--wa-color-text-quiet);
    font-size: var(--wa-font-size-s);
    overflow-wrap: anywhere;
}

.public-datasets__tags {
    display: flex;
    flex-wrap: wrap;
    gap: var(--wa-space-2xs);
}

.public-datasets__actions {
    align-items: center;
    display: flex;
    flex-wrap: wrap;
    gap: var(--wa-space-m);
    margin-top: var(--wa-space-2xs);
}
</style>
