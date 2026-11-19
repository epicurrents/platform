<script setup lang="ts">
/**
 * DatasetPublicListingSection — the dataset page's "Public listing" section, for the dataset's author and superusers.
 *
 * Lists the dataset on the deployment's public dataset listing through one of its share tokens, or takes it off.
 * Whether it qualifies is the server's to say (`GET /datasets/{id}/public-listing/`): an ungated dataset of recordings
 * that each name the published dataset they came from, through a de-identifying token. The server checks again on
 * every read of the listing, so a dataset listed here can drop off it later, and this section says why.
 *
 * @package    epicurrents-platform
 */

import { computed, onMounted, reactive, ref, watch } from 'vue'
import { RouterLink } from 'vue-router'
import { t } from '#i18n'
import {
    clearPublicListing,
    getPublicListing,
    setPublicListing,
    type AccessRight,
    type ListingProblem,
    type PublicListing,
} from '#api/library'
import { errorDetail } from '#lib/http'
import { showToast } from '#lib/toast'

const SCOPE = 'DatasetPublicListingSection'

const props = defineProps<{
    /** The dataset's hash or primary key, as the dataset routes accept it. */
    datasetId: string
    /** The dataset's access rights, of which the share tokens are offered. */
    accessRights: AccessRight[]
}>()

const emit = defineEmits<{
    /** The token the dataset is listed through after a load or a change, null when it is not listed. */
    change: [rightId: number | null]
}>()

const listing = ref<PublicListing | null>(null)
const loading = ref(true)
const loadError = ref<string | null>(null)
const saving = ref(false)
const form = reactive({ rightId: '' })

const tokens = computed(() => props.accessRights.filter(right => !!right.public_share_token))

async function load () {
    loadError.value = null
    try {
        listing.value = await getPublicListing(props.datasetId)
        emit('change', listing.value.access_right_id)
        // The chosen token may have been revoked since, which leaves the first remaining one chosen instead.
        if (!tokens.value.some(right => String(right.id) === form.rightId)) {
            form.rightId = tokens.value.length ? String(tokens.value[0].id) : ''
        }
    } catch (err) {
        loadError.value = errorDetail(err, t('Failed to load the public listing.', SCOPE))
    } finally {
        loading.value = false
    }
}

onMounted(load)

// A token granted, revoked or edited on the page changes what qualifies, and revoking the listed one unlists.
watch(() => props.accessRights, load)

/** The listed token's label. */
const listedToken = computed(() => {
    const id = listing.value?.access_right_id
    return tokens.value.find(right => right.id === id)?.public_share_token ?? null
})

/**
 * A problem as a sentence, with the members it concerns where it is about members.
 * @param problem - One of the server's reasons.
 */
function problemText (problem: ListingProblem) {
    if (problem.count !== null) {
        return t('{detail} ({count} concerned)', SCOPE, { detail: problem.detail, count: problem.count })
    }
    return problem.detail
}

async function list () {
    const rightId = Number(form.rightId)
    if (!rightId) {
        return
    }
    saving.value = true
    try {
        listing.value = await setPublicListing(props.datasetId, rightId)
        emit('change', listing.value.access_right_id)
        showToast(t('The dataset is listed publicly.', SCOPE), 'neutral')
    } catch (err) {
        showToast(errorDetail(err, t('The dataset could not be listed.', SCOPE)), 'danger')
    } finally {
        saving.value = false
    }
}

async function unlist () {
    saving.value = true
    try {
        listing.value = await clearPublicListing(props.datasetId)
        emit('change', null)
        showToast(t('The dataset is no longer listed publicly.', SCOPE), 'neutral')
    } catch (err) {
        showToast(errorDetail(err, t('The dataset could not be taken off the listing.', SCOPE)), 'danger')
    } finally {
        saving.value = false
    }
}
</script>

<template>
    <section class="dataset-listing">
        <div class="section-header">
            <h2>{{ t('Public listing', SCOPE) }}</h2>
        </div>

        <wa-spinner v-if="loading"></wa-spinner>

        <wa-callout v-else-if="loadError" variant="danger">{{ loadError }}</wa-callout>

        <p v-else-if="listing && !listing.enabled" class="dataset-listing__hint">
            {{ t('This deployment does not serve a public dataset listing.', SCOPE) }}
        </p>

        <template v-else-if="listing">
            <p class="dataset-listing__hint">
                {{ t('The public listing names the dataset and one of its share tokens to anyone, so its recordings can be browsed and opened without an account. Only a dataset republishing data that is already public can be listed: every recording must name the published dataset it was taken from, and the token must serve de-identified data.', SCOPE) }}
            </p>

            <template v-if="listing.access_right_id !== null">
                <p class="dataset-listing__status">
                    <wa-badge :variant="listing.listed ? 'brand' : 'warning'" pill>
                        {{ listing.listed ? t('Listed', SCOPE) : t('Not shown', SCOPE) }}
                    </wa-badge>
                    <span v-if="listedToken">{{ t('Through the token {token}.', SCOPE, { token: listedToken }) }}</span>
                    <RouterLink v-if="listing.listed" :to="{ name: 'public-datasets' }">
                        {{ t('View the listing', SCOPE) }}
                    </RouterLink>
                </p>
                <wa-callout v-if="!listing.listed" size="s" variant="warning">
                    {{ t('The listing leaves the dataset out until this is fixed:', SCOPE) }}
                    <ul class="dataset-listing__problems">
                        <li v-for="problem in listing.problems" :key="problem.code">{{ problemText(problem) }}</li>
                    </ul>
                </wa-callout>
                <div class="form-actions">
                    <wa-button
                        appearance="plain"
                        :loading="saving"
                        size="s"
                        @click="unlist"
                    >
                        {{ t('Take off the listing', SCOPE) }}
                    </wa-button>
                </div>
            </template>

            <template v-else>
                <ul v-if="listing.problems.length" class="dataset-listing__problems">
                    <li v-for="problem in listing.problems" :key="problem.code">{{ problemText(problem) }}</li>
                </ul>
                <p v-if="!tokens.length" class="dataset-listing__hint">
                    {{ t('Grant access by share token first; the listing publishes that token.', SCOPE) }}
                </p>
                <div v-else class="dataset-listing__form">
                    <wa-select
                        :disabled="saving"
                        :label="t('Share token', SCOPE)"
                        size="s"
                        v-wa="[form, 'rightId']"
                    >
                        <wa-option v-for="right in tokens" :key="right.id" :value="String(right.id)">
                            {{ right.public_share_token }}
                        </wa-option>
                    </wa-select>
                    <wa-button
                        appearance="plain"
                        :disabled="!form.rightId || listing.problems.length > 0"
                        :loading="saving"
                        size="s"
                        variant="brand"
                        @click="list"
                    >
                        {{ t('List publicly', SCOPE) }}
                    </wa-button>
                </div>
            </template>
        </template>
    </section>
</template>

<style scoped>
.section-header {
    margin: 0;
}
.dataset-listing__hint {
    color: var(--wa-color-text-quiet);
    font-size: var(--wa-font-size-s);
    margin: var(--wa-space-xs) 0;
}

.dataset-listing__status {
    align-items: center;
    display: flex;
    flex-wrap: wrap;
    gap: var(--wa-space-xs);
    margin: var(--wa-space-s) 0;
}

.dataset-listing__problems {
    font-size: var(--wa-font-size-s);
    margin: var(--wa-space-xs) 0;
    padding-left: var(--wa-space-l);
}

.dataset-listing__form {
    align-items: flex-end;
    display: flex;
    flex-wrap: wrap;
    gap: var(--wa-space-s);
}

.dataset-listing__form wa-select {
    min-width: 16rem;
}
</style>
