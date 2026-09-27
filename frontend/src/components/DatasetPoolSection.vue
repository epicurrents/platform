<script setup lang="ts">
/**
 * DatasetPoolSection — the dataset page's "Submission pool" section, for the dataset's author and superusers.
 *
 * Three states, all answered by `GET /datasets/{id}/pool/`: not a pool (with the action to make an empty dataset one),
 * configured (profile, dedicated group, intake switch, dissolve), and filling (the same with the profile and gate
 * locked, and file totals over every contributor together). The rules are the server's; this section shows them and
 * offers only what the current state allows.
 *
 * @package    epicurrents-platform
 */

import { computed, onMounted, reactive, ref, watch } from 'vue'
import { t } from '#i18n'
import {
    configureDatasetPool,
    dissolveDatasetPool,
    getDatasetPool,
    updateDatasetPool,
    type DatasetPool,
} from '#api/library'
import { listSubmissionProfiles, type SubmissionProfile } from '#api/recordings'
import { useAuthStore } from '#stores/auth'
import { errorDetail } from '#lib/http'
import { showToast } from '#lib/toast'

const SCOPE = 'DatasetPoolSection'

const props = defineProps<{
    /** The dataset's hash or primary key, as the dataset routes accept it. */
    datasetId: string
}>()

const emit = defineEmits<{
    /**
     * The pool state after loading or after a change, so the page can lock what the pool owns. `changed` is true
     * when this section wrote it, which also changes the dataset's gate.
     */
    change: [pool: DatasetPool, changed: boolean]
}>()

const authStore = useAuthStore()

const pool = ref<DatasetPool | null>(null)
const loading = ref(true)
const loadError = ref('')
const intake = reactive({ open: false })
const intakeSaving = ref(false)

const profiles = ref<SubmissionProfile[]>([])
const profilesLoading = ref(false)
const showConfigure = ref(false)
const configuring = ref(false)
const configureError = ref('')
const configureInput = reactive({ profile: '' })

const showDissolve = ref(false)
const dissolving = ref(false)

const isPool = computed(() => !!pool.value?.profile)
const filesSummary = computed(() => {
    if (!pool.value) {
        return ''
    }
    return t('{pending} waiting for ingest · {ingested} ingested · {failed} failed', SCOPE, {
        failed: pool.value.failed_count,
        ingested: pool.value.ingested_count,
        pending: pool.value.pending_count,
    })
})
const contributorsSummary = computed(() => {
    if (!pool.value) {
        return ''
    }
    if (pool.value.contributors_required === null) {
        return t('{count} members', SCOPE, { count: pool.value.contributor_count })
    }
    return t('{count} members · {required} needed to open intake', SCOPE, {
        count: pool.value.contributor_count,
        required: pool.value.contributors_required,
    })
})
// Opening is refused below the profile's m; closing never is, so an open pool keeps its switch.
const intakeBlocked = computed(() => {
    const current = pool.value
    if (!current || current.open || current.contributors_required === null) {
        return false
    }
    return current.contributor_count < current.contributors_required
})
const selectedProfile = computed(() => profiles.value.find(profile => profile.key === configureInput.profile) ?? null)

function setPool (next: DatasetPool, changed = true) {
    pool.value = next
    intake.open = next.open
    emit('change', next, changed)
}

async function load () {
    loading.value = true
    loadError.value = ''
    try {
        setPool(await getDatasetPool(props.datasetId), false)
    } catch (err) {
        loadError.value = errorDetail(err, t('The submission pool could not be loaded.', SCOPE))
    } finally {
        loading.value = false
    }
}

/**
 * One line summarising what a profile checks, for choosing between them.
 * @param profile - The profile to summarise.
 */
function profileSummary (profile: SubmissionProfile) {
    const parts: string[] = []
    if (profile.channels.length) {
        parts.push(t('{count} channels', SCOPE, { count: profile.channels.length }))
    }
    if (profile.sampling_rate !== null) {
        parts.push(t('{rate} Hz', SCOPE, { rate: profile.sampling_rate }))
    }
    if (profile.durations_seconds.length) {
        parts.push(t('{durations} s', SCOPE, { durations: profile.durations_seconds.join(' / ') }))
    }
    return parts.join(' · ')
}

async function openConfigure () {
    configureError.value = ''
    configureInput.profile = ''
    showConfigure.value = true
    if (profiles.value.length || profilesLoading.value) {
        return
    }
    profilesLoading.value = true
    try {
        profiles.value = await listSubmissionProfiles()
    } catch (err) {
        configureError.value = errorDetail(err, t('The ingest profiles could not be loaded.', SCOPE))
    } finally {
        profilesLoading.value = false
    }
}

function closeConfigure () {
    if (configuring.value) {
        return
    }
    showConfigure.value = false
}

async function confirmConfigure () {
    if (!configureInput.profile) {
        configureError.value = t('Choose a profile.', SCOPE)
        return
    }
    configuring.value = true
    configureError.value = ''
    try {
        setPool(await configureDatasetPool(props.datasetId, configureInput.profile))
        showConfigure.value = false
        const message = t('The dataset is now a submission pool. Add contributors to its group, then open intake.', SCOPE)
        showToast(message, 'brand')
    } catch (err) {
        configureError.value = errorDetail(err, t('The pool could not be configured. Nothing was changed.', SCOPE))
    } finally {
        configuring.value = false
    }
}

async function setIntake (open: boolean) {
    intakeSaving.value = true
    try {
        setPool(await updateDatasetPool(props.datasetId, { open }))
        showToast(open ? t('Intake opened.', SCOPE) : t('Intake closed.', SCOPE), 'neutral')
    } catch (err) {
        intake.open = pool.value?.open ?? false
        showToast(errorDetail(err, t('Intake could not be changed.', SCOPE)), 'danger')
    } finally {
        intakeSaving.value = false
    }
}

function openDissolve () {
    showDissolve.value = true
}

function closeDissolve () {
    if (dissolving.value) {
        return
    }
    showDissolve.value = false
}

async function confirmDissolve () {
    dissolving.value = true
    try {
        setPool(await dissolveDatasetPool(props.datasetId))
        showDissolve.value = false
        showToast(t('The submission pool was dissolved.', SCOPE), 'neutral')
    } catch (err) {
        showToast(errorDetail(err, t('The pool could not be dissolved.', SCOPE)), 'danger')
    } finally {
        dissolving.value = false
    }
}

// The switch writes `intake.open`; a value that differs from the server's is the person's toggle.
watch(
    () => intake.open,
    (open) => {
        if (pool.value && isPool.value && open !== pool.value.open && !intakeSaving.value) {
            setIntake(open)
        }
    },
)

onMounted(load)
</script>

<template>
    <section class="dataset-pool">
        <div class="section-header">
            <h2>{{ t('Submission pool', SCOPE) }}</h2>
            <wa-button v-if="pool && !isPool"
                appearance="plain"
                :disabled="!pool.configurable"
                size="s"
                variant="brand"
                @click="openConfigure"
            >
                {{ t('Make this a submission pool', SCOPE) }}
            </wa-button>
        </div>

        <wa-spinner v-if="loading"></wa-spinner>

        <wa-callout v-else-if="loadError" variant="danger">{{ loadError }}</wa-callout>

        <template v-else-if="pool && !isPool">
            <p v-if="pool.configurable" class="dataset-pool__hint">
                {{ t('Contributors in a dedicated group send prepared recordings to a pool from the viewer. Each file is checked against the pool\'s profile, and the recordings stay hidden until a release run publishes them.', SCOPE) }}
            </p>
            <p v-else class="dataset-pool__hint">
                {{ t('Only an empty dataset can become a submission pool, because every member must have passed the same checks.', SCOPE) }}
            </p>
        </template>

        <template v-else-if="pool">
            <dl class="dataset-pool__facts">
                <dt>{{ t('Profile', SCOPE) }}</dt>
                <dd>
                    <code>{{ pool.profile }}</code>
                    <wa-badge v-if="pool.filling" appearance="outlined" variant="neutral">{{ t('Locked', SCOPE) }}</wa-badge>
                </dd>
                <dt>{{ t('Contributors', SCOPE) }}</dt>
                <dd>
                    <router-link v-if="authStore.isStaff && pool.group_id !== null"
                        :to="{ name: 'admin-group', params: { id: String(pool.group_id) } }"
                    >
                        {{ pool.group_name }}
                    </router-link>
                    <span v-else>{{ pool.group_name }}</span>
                    <span class="dataset-pool__quiet">{{ contributorsSummary }}</span>
                </dd>
                <dt>{{ t('Release gate', SCOPE) }}</dt>
                <dd>
                    {{ t('On', SCOPE) }}
                    <wa-badge v-if="pool.filling" appearance="outlined" variant="neutral">{{ t('Locked', SCOPE) }}</wa-badge>
                </dd>
                <template v-if="pool.filling">
                    <dt>{{ t('Files', SCOPE) }}</dt>
                    <dd>{{ filesSummary }}</dd>
                </template>
            </dl>
            <wa-switch :disabled="intakeSaving || intakeBlocked" size="s" v-wa="[intake, 'open']">
                {{ t('Accept submissions', SCOPE) }}
            </wa-switch>
            <p v-if="intakeBlocked" class="dataset-pool__hint">
                {{ t('Intake opens once the contributor group has {required} active members, so that no single contributor makes up the pool.', SCOPE, { required: pool.contributors_required }) }}
            </p>
            <p class="dataset-pool__hint">
                {{ t('Members of the group can send files only while intake is open. Closing it leaves files already accepted to be ingested.', SCOPE) }}
            </p>
            <p v-if="pool.filling" class="dataset-pool__hint">
                {{ t('The profile, the group and the gate are fixed now that the pool has accepted a file. Members are withdrawn through the purge path, not removed here.', SCOPE) }}
            </p>
            <div v-else class="form-actions">
                <wa-button appearance="plain" size="s" variant="danger" @click="openDissolve">
                    {{ t('Dissolve pool', SCOPE) }}
                </wa-button>
            </div>
        </template>
    </section>

    <wa-dialog
        :label="t('Make this a submission pool', SCOPE)"
        :open="showConfigure"
        @wa-hide.self="closeConfigure"
    >
        <div class="dataset-pool__form">
            <wa-callout v-if="configureError" variant="danger">{{ configureError }}</wa-callout>
            <wa-select
                :disabled="configuring || profilesLoading"
                :label="t('Ingest profile', SCOPE)"
                :placeholder="profilesLoading ? t('Loading…', SCOPE) : t('Choose a profile', SCOPE)"
                size="s"
                v-wa="[configureInput, 'profile']"
            >
                <wa-option v-for="profile in profiles" :key="profile.key" :value="profile.key">
                    {{ profile.key }}
                </wa-option>
            </wa-select>
            <p v-if="selectedProfile" class="dataset-pool__hint">{{ profileSummary(selectedProfile) }}</p>
            <p v-if="selectedProfile && selectedProfile.channels.length" class="dataset-pool__channels">
                {{ selectedProfile.channels.join(', ') }}
            </p>
            <p class="dataset-pool__hint">
                {{ t('The dataset becomes release-gated and gets a new group for its contributors. Intake starts closed. Until the first file is accepted, the profile can change and the pool can be dissolved.', SCOPE) }}
            </p>
        </div>
        <div slot="footer" class="form-actions">
            <wa-button
                appearance="filled-outlined"
                :disabled="configuring"
                variant="neutral"
                @click="closeConfigure"
            >
                {{ t('Cancel', SCOPE) }}
            </wa-button>
            <wa-button
                appearance="filled-outlined"
                :disabled="!configureInput.profile"
                :loading="configuring"
                variant="brand"
                @click="confirmConfigure"
            >
                {{ t('Make pool', SCOPE) }}
            </wa-button>
        </div>
    </wa-dialog>

    <wa-dialog
        :label="t('Dissolve pool', SCOPE)"
        :open="showDissolve"
        @wa-hide.self="closeDissolve"
    >
        <p>
            {{ t('The pool\'s configuration and its contributor group are removed, and the release gate turns off. The dataset itself stays.', SCOPE) }}
        </p>
        <div slot="footer" class="form-actions">
            <wa-button
                appearance="filled-outlined"
                :disabled="dissolving"
                variant="neutral"
                @click="closeDissolve"
            >
                {{ t('Cancel', SCOPE) }}
            </wa-button>
            <wa-button
                appearance="filled-outlined"
                :loading="dissolving"
                variant="danger"
                @click="confirmDissolve"
            >
                {{ t('Dissolve', SCOPE) }}
            </wa-button>
        </div>
    </wa-dialog>
</template>

<style scoped>
.dataset-pool__facts {
    display: grid;
    gap: var(--wa-space-2xs) var(--wa-space-m);
    grid-template-columns: max-content 1fr;
    margin: 0 0 var(--wa-space-m);
}

.dataset-pool__facts dt {
    color: var(--wa-color-text-quiet);
}

.dataset-pool__facts dd {
    align-items: center;
    display: flex;
    gap: var(--wa-space-xs);
    margin: 0;
}

.dataset-pool__quiet {
    color: var(--wa-color-text-quiet);
}

.dataset-pool__form {
    display: flex;
    flex-direction: column;
    gap: var(--wa-space-s);
}

.dataset-pool__hint {
    color: var(--wa-color-text-quiet);
    font-size: var(--wa-font-size-s);
    margin: var(--wa-space-xs) 0;
}

.dataset-pool__channels {
    font-family: var(--wa-font-family-code);
    font-size: var(--wa-font-size-s);
    margin: 0;
}
</style>
