<script setup lang="ts">
/**
 * The catalogue: the release-gated datasets the caller can read that have released recordings, by name, each with
 * how many released recordings it holds, their modalities, its latest release month, its licence and the subject
 * fields its browser shows. Each entry links to the dataset page.
 *
 * @package    epicurrents-platform
 */
import { onMounted, ref } from 'vue'
import { RouterLink } from 'vue-router'
import { t } from '#i18n'
import { listCatalogue, type CatalogueEntry } from '#api/library'
import { errorDetail } from '#lib/http'
import { formatMonth } from '#lib/format'

const SCOPE = 'CatalogueView'
/** The server's largest page; a catalogue longer than this is not expected. */
const CATALOGUE_LIMIT = 200

const entries = ref<CatalogueEntry[]>([])
const loading = ref(true)
const error = ref<string | null>(null)

onMounted(async () => {
    try {
        entries.value = await listCatalogue({ limit: CATALOGUE_LIMIT })
    } catch (err) {
        error.value = errorDetail(err, t('Failed to load the catalogue.', SCOPE))
    } finally {
        loading.value = false
    }
})

/**
 * The dataset page of an entry.
 * @param entry - A catalogue entry.
 */
function datasetRoute (entry: CatalogueEntry) {
    return { name: 'dataset', params: { id: entry.object_hash } }
}

/**
 * The facts line of an entry: the recording count, the modalities and the latest release.
 * @param entry - A catalogue entry.
 */
function facts (entry: CatalogueEntry) {
    const parts = [t('{count} recordings', SCOPE, { count: entry.recording_count })]
    if (entry.modalities.length) {
        parts.push(entry.modalities.map(modality => modality.toUpperCase()).join(', '))
    }
    parts.push(t('released {month}', SCOPE, { month: formatMonth(entry.latest_release) }))
    return parts.join(' · ')
}
</script>

<template>
    <main class="page-view">
        <header class="page-header">
            <h1>{{ t('Catalogue', SCOPE) }}</h1>
        </header>

        <wa-callout variant="neutral">
            {{
                t(
                    'Datasets whose recordings are released on a schedule, once they meet the conditions set for ' +
                    'them. Every dataset you can read that has released recordings is listed here.',
                    SCOPE,
                )
            }}
        </wa-callout>

        <wa-spinner v-if="loading" class="loading-center"></wa-spinner>

        <wa-callout v-else-if="error" variant="danger">{{ error }}</wa-callout>

        <p v-else-if="!entries.length" class="empty-state">
            {{ t('No released datasets are shared with you yet.', SCOPE) }}
        </p>

        <ul v-else class="catalogue">
            <li v-for="entry in entries" :key="entry.object_hash">
                <RouterLink class="catalogue__entry" :to="datasetRoute(entry)">
                    <span class="catalogue__name">
                        <wa-icon class="icon-muted" name="database"></wa-icon>
                        {{ entry.name }}
                    </span>
                    <span v-if="entry.description" class="catalogue__description">{{ entry.description }}</span>
                    <span class="catalogue__facts">{{ facts(entry) }}</span>
                    <span v-if="entry.subject_fields.length || entry.license_spdx" class="catalogue__tags">
                        <wa-tag v-if="entry.license_spdx" size="s" variant="brand">{{ entry.license_spdx }}</wa-tag>
                        <wa-tag v-for="label in entry.subject_fields" :key="label"
                            size="s"
                            variant="neutral"
                        >
                            {{ label }}
                        </wa-tag>
                    </span>
                </RouterLink>
            </li>
        </ul>
    </main>
</template>

<style scoped>
.catalogue {
    display: flex;
    flex-direction: column;
    gap: var(--wa-space-s);
    list-style: none;
    margin: 0;
    padding: 0;
}

.catalogue > li {
    /* Web Awesome's native list styles indent an item; a card list sits flush with the column. */
    margin: 0;
    padding: 0;
}

.catalogue__entry {
    background: var(--wa-color-surface-default);
    border: var(--wa-border-width-s) solid var(--wa-color-surface-border);
    border-radius: var(--wa-border-radius-m);
    color: inherit;
    display: flex;
    flex-direction: column;
    gap: var(--wa-space-2xs);
    padding: var(--wa-space-m);
    text-decoration: none;
}

.catalogue__entry:hover {
    background: var(--wa-color-surface-raised);
}

.catalogue__entry:focus-visible {
    outline: var(--wa-focus-ring);
    outline-offset: var(--wa-focus-ring-offset);
}

.catalogue__name {
    align-items: center;
    display: flex;
    font-weight: var(--wa-font-weight-semibold);
    gap: var(--wa-space-xs);
}

.catalogue__description {
    overflow-wrap: anywhere;
}

.catalogue__facts {
    color: var(--wa-color-text-quiet);
    font-size: var(--wa-font-size-s);
}

.catalogue__tags {
    display: flex;
    flex-wrap: wrap;
    gap: var(--wa-space-2xs);
}
</style>
