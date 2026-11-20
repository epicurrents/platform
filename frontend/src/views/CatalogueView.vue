<script setup lang="ts">
/**
 * The catalogue: the datasets a reader can browse that the dataset list does not show them, by name, in two parts.
 * The released datasets are the release-gated datasets the caller can read that have released recordings, each with
 * how many released recordings it holds, their modalities, its latest release month, its licence and the subject
 * fields its browser shows, linking to the dataset page. The public datasets are the deployment's public listing,
 * each linking to its read-only browser through the share token it is listed with; the part is left out when the
 * deployment does not serve the listing or lists nothing.
 *
 * Ungated datasets shared with the caller directly are in the dataset list and not here.
 *
 * @package    epicurrents-platform
 */
import { onMounted, ref } from 'vue'
import { RouterLink } from 'vue-router'
import axios from 'axios'
import { t } from '#i18n'
import { listCatalogue, listPublicDatasets, type CatalogueEntry, type PublicDataset } from '#api/library'
import { errorDetail } from '#lib/http'
import { formatMonth } from '#lib/format'

const SCOPE = 'CatalogueView'
/** The server's largest page; a catalogue longer than this is not expected. */
const CATALOGUE_LIMIT = 200

const entries = ref<CatalogueEntry[]>([])
const publicEntries = ref<PublicDataset[]>([])
const loading = ref(true)
const error = ref<string | null>(null)
const publicError = ref<string | null>(null)

onMounted(async () => {
    const [released, listed] = await Promise.allSettled([
        listCatalogue({ limit: CATALOGUE_LIMIT }),
        listPublicDatasets(),
    ])
    if (released.status === 'fulfilled') {
        entries.value = released.value
    } else {
        error.value = errorDetail(released.reason, t('Failed to load the catalogue.', SCOPE))
    }
    if (listed.status === 'fulfilled') {
        publicEntries.value = listed.value
    } else if (!(axios.isAxiosError(listed.reason) && listed.reason.response?.status === 404)) {
        // A 404 is a deployment that does not serve the listing, which leaves the part out.
        publicError.value = errorDetail(listed.reason, t('Failed to load the public datasets.', SCOPE))
    }
    loading.value = false
})

/**
 * The dataset page of a released entry.
 * @param entry - A catalogue entry.
 */
function datasetRoute (entry: CatalogueEntry) {
    return { name: 'dataset', params: { id: entry.object_hash } }
}

/**
 * The read-only browser of a public entry, through its listed token.
 * @param entry - A listed dataset.
 */
function browseRoute (entry: PublicDataset) {
    return { name: 'dataset-browse', params: { id: entry.object_hash }, query: { token: entry.share_token } }
}

/**
 * The recording count and the modalities of an entry.
 * @param entry - A released or a listed dataset.
 */
function countAndModalities (entry: CatalogueEntry | PublicDataset) {
    const parts = [t('{count} recordings', SCOPE, { count: entry.recording_count })]
    if (entry.modalities.length) {
        parts.push(entry.modalities.map(modality => modality.toUpperCase()).join(', '))
    }
    return parts
}

/**
 * The facts line of a released entry: the recording count, the modalities and the latest release.
 * @param entry - A catalogue entry.
 */
function facts (entry: CatalogueEntry) {
    const parts = countAndModalities(entry)
    parts.push(t('released {month}', SCOPE, { month: formatMonth(entry.latest_release) }))
    return parts.join(' · ')
}

/**
 * The facts line of a public entry: the recording count, the modalities and the published sources.
 * @param entry - A listed dataset.
 */
function publicFacts (entry: PublicDataset) {
    const parts = countAndModalities(entry)
    if (entry.public_sources.length) {
        parts.push(t('from {sources}', SCOPE, { sources: entry.public_sources.join(', ') }))
    }
    return parts.join(' · ')
}
</script>

<template>
    <main class="page-view">
        <header class="page-header">
            <h1>{{ t('Catalogue', SCOPE) }}</h1>
        </header>

        <wa-spinner v-if="loading" class="loading-center"></wa-spinner>

        <template v-else>
            <section>
                <div class="section-header">
                    <h2>{{ t('Released datasets', SCOPE) }}</h2>
                </div>
                <p class="catalogue__hint">
                    {{
                        t(
                            'Datasets whose recordings are released on a schedule, once they meet the conditions ' +
                            'set for them. Every dataset you can read that has released recordings is listed here.',
                            SCOPE,
                        )
                    }}
                </p>

                <wa-callout v-if="error" variant="danger">{{ error }}</wa-callout>

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
                            <span v-if="entry.description" class="catalogue__description">
                                {{ entry.description }}
                            </span>
                            <span class="catalogue__facts">{{ facts(entry) }}</span>
                            <span v-if="entry.subject_fields.length || entry.license_spdx" class="catalogue__tags">
                                <wa-tag v-if="entry.license_spdx" size="s" variant="brand">
                                    {{ entry.license_spdx }}
                                </wa-tag>
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
            </section>

            <section v-if="publicError || publicEntries.length">
                <div class="section-header">
                    <h2>{{ t('Public datasets', SCOPE) }}</h2>
                </div>
                <p class="catalogue__hint">
                    {{
                        t(
                            'Datasets republished here from their original publishers, which anyone can browse ' +
                            'and open in the viewer without an account.',
                            SCOPE,
                        )
                    }}
                </p>

                <wa-callout v-if="publicError" variant="danger">{{ publicError }}</wa-callout>

                <ul v-else class="catalogue">
                    <li v-for="entry in publicEntries" :key="entry.object_hash">
                        <RouterLink class="catalogue__entry" :to="browseRoute(entry)">
                            <span class="catalogue__name">
                                <wa-icon class="icon-muted" name="database"></wa-icon>
                                {{ entry.name }}
                            </span>
                            <span v-if="entry.description" class="catalogue__description">
                                {{ entry.description }}
                            </span>
                            <span class="catalogue__facts">{{ publicFacts(entry) }}</span>
                            <span v-if="entry.subject_fields.length || entry.license_spdx" class="catalogue__tags">
                                <wa-tag v-if="entry.license_spdx" size="s" variant="brand">
                                    {{ entry.license_spdx }}
                                </wa-tag>
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
            </section>
        </template>
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

.catalogue__hint {
    color: var(--wa-color-text-quiet);
    margin-block: 0 var(--wa-space-s);
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
    overflow-wrap: anywhere;
}

.catalogue__tags {
    display: flex;
    flex-wrap: wrap;
    gap: var(--wa-space-2xs);
}
</style>
