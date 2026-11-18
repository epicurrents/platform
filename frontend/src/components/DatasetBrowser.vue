<script setup lang="ts">
/**
 * The dataset browser: a page of a dataset's recordings with their ingest metadata, subject fields and tags, and a
 * details drawer per row with all of the recording's tags, its subject values and the codes on its annotations,
 * beside the registered vocabularies it has none from.
 *
 * Sorting and paging are the server's (`GET /datasets/{id}/browse/`), which applies the release gate and never
 * orders a gated dataset by arrival. The subject columns are the fields the server says the caller is shown, so a
 * reader who does not manage a gated dataset gets its displayed fields only. A manager edits a member's subject
 * values in the drawer; the server refuses a released member's, and the refusal is shown as it comes.
 *
 * Filtering is the server's too, and so are the facet counts the filters show. On a release-gated dataset a reader
 * who does not manage it is served no count below k: a filtered page matching fewer members comes back without rows
 * and is shown as such, never as an empty result.
 *
 * @package    epicurrents-platform
 */
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { t } from '#i18n'
import {
    browseDataset,
    listRecordingTags,
    updateDatasetMemberSubject,
    type DatasetBrowseFilters,
    type DatasetBrowseRow,
    type DatasetFacets,
    type SubjectField,
    type TagChip,
} from '#api/library'
import { getRecordingCodes, listVocabularies, type CodeSummary, type Vocabulary } from '#api/annotations'
import { getRecordingDetail, type Recording } from '#api/recordings'
import { errorDetail } from '#lib/http'
import { formatDuration, formatMonth, formatSamplingRate } from '#lib/format'
import { showToast } from '#lib/toast'
import DataTable, { type DataTableColumn } from '#components/DataTable.vue'
import DatasetBrowserFilters from '#components/DatasetBrowserFilters.vue'
import RecordingDetailsDrawer from '#components/RecordingDetailsDrawer.vue'

const SCOPE = 'DatasetBrowser'
const PAGE_SIZE = 50
const SUBJECT_PREFIX = 'subject.'

const props = defineProps<{
    /** The dataset's object hash or primary key. */
    datasetId: string
    /** Whether the caller may set subject values; the server decides in the end. */
    canManage: boolean
}>()

const router = useRouter()

// ── Page ─────────────────────────────────────────────────────────────────

const rows = ref<DatasetBrowseRow[]>([])
const subjectFields = ref<SubjectField[]>([])
const facets = ref<DatasetFacets>({ modality: [], subject: {}, tags: [] })
const filters = ref<DatasetBrowseFilters>({ modality: [], subject: {}, tags: [] })
const suppressionK = ref<number | null>(null)
// The matching row count; null when the server suppressed it.
const total = ref<number | null>(0)
const page = ref(1)
const sort = ref('name')
const loading = ref(true)
const error = ref<string | null>(null)
// Each load takes a number; an answer to an earlier one is dropped, so a slow page cannot overwrite a later one.
let loadSeq = 0

async function load () {
    const seq = ++loadSeq
    loading.value = true
    error.value = null
    try {
        const result = await browseDataset(props.datasetId, {
            limit: PAGE_SIZE,
            offset: (page.value - 1) * PAGE_SIZE,
            sort: sort.value,
        }, filters.value)
        if (seq !== loadSeq) {
            return
        }
        rows.value = result.items
        subjectFields.value = result.subject_fields
        facets.value = result.facets
        suppressionK.value = result.suppression_k
        total.value = result.count
    } catch (err) {
        if (seq === loadSeq) {
            error.value = errorDetail(err, t('Failed to load the recordings.', SCOPE))
        }
    } finally {
        if (seq === loadSeq) {
            loading.value = false
        }
    }
}

onMounted(() => {
    load()
    loadVocabularies()
})

watch(page, load)

function onSort (next: string) {
    sort.value = next
    if (page.value === 1) {
        load()
    } else {
        page.value = 1
    }
}

function onPage (next: number) {
    page.value = next
}

/** Fetch the first page under a new set of filters. */
function onFilter (next: DatasetBrowseFilters) {
    filters.value = next
    if (page.value === 1) {
        load()
    } else {
        page.value = 1
    }
}

const filtered = computed(() => {
    const { modality, subject, tags } = filters.value
    return modality.length > 0 || tags.length > 0 || Object.values(subject).some(values => values.length > 0)
})

/** The line above the table: how many recordings match; empty when too few match to say, which the table says. */
const summary = computed(() => {
    if (total.value === null) {
        return ''
    }
    if (filtered.value) {
        return t('{count} matching recordings', SCOPE, { count: total.value })
    }
    return t('{count} recordings', SCOPE, { count: total.value })
})

const emptyText = computed(() => {
    if (total.value === null) {
        return t('Fewer than {k} recordings match these filters. Widen them to see the recordings.', SCOPE, {
            k: suppressionK.value ?? '',
        })
    }
    if (filtered.value) {
        return t('No recordings match these filters.', SCOPE)
    }
    return t('No recordings in this dataset yet.', SCOPE)
})

/** Reload the current page, after something outside the browser changed the membership or the gate. */
function reload () {
    return load()
}

defineExpose({ reload })

// ── Columns ──────────────────────────────────────────────────────────────

const columns = computed<DataTableColumn<DatasetBrowseRow>[]>(() => [
    { key: 'name', label: t('Name', SCOPE), sortable: true, wrap: true, format: row => row.display_name },
    { key: 'modality', label: t('Modality', SCOPE), sortable: true, format: row => row.modality.toUpperCase() },
    {
        key: 'duration',
        label: t('Duration', SCOPE),
        numeric: true,
        sortable: true,
        format: row => row.duration === null ? '' : formatDuration(row.duration),
    },
    {
        key: 'channels',
        label: t('Channels', SCOPE),
        numeric: true,
        sortable: true,
        format: row => row.channel_count === null ? '' : String(row.channel_count),
    },
    {
        key: 'sampling_rate',
        label: t('Sampling rate', SCOPE),
        numeric: true,
        sortable: true,
        format: row => formatSamplingRate(row.sampling_rate_max, row.sampling_rate_min),
    },
    ...subjectFields.value.map(field => ({
        key: `${SUBJECT_PREFIX}${field.key}`,
        label: field.label,
        sortable: true,
        format: (row: DatasetBrowseRow) => row.subject[field.key] ?? '',
    })),
    { key: 'date', label: t('Date', SCOPE), sortable: true, format: row => formatMonth(row.created_at) },
    { key: 'tags', label: t('Tags', SCOPE) },
])

function rowKey (row: DatasetBrowseRow) {
    return row.hash
}

/**
 * How many of a row's tags the cell leaves out.
 * @param row - A browser row.
 */
function hiddenTagCount (row: DatasetBrowseRow) {
    return row.tag_count - row.tags.length
}

// ── Details drawer ───────────────────────────────────────────────────────

const current = ref<DatasetBrowseRow | null>(null)
const detail = ref<Recording | null>(null)
const tags = ref<TagChip[]>([])
const codes = ref<CodeSummary[] | null>(null)
const codesError = ref(false)
const subjectDraft = reactive<Record<string, string>>({})
const savingSubject = ref(false)

/** Whether the drawer is still showing *hash*, so a late answer for a closed or replaced row is dropped. */
function stillShowing (hash: string) {
    return current.value?.hash === hash
}

function resetSubjectDraft (row: DatasetBrowseRow) {
    for (const key of Object.keys(subjectDraft)) {
        delete subjectDraft[key]
    }
    for (const field of subjectFields.value) {
        subjectDraft[field.key] = row.subject[field.key] ?? ''
    }
}

async function openDetails (row: DatasetBrowseRow) {
    const hash = row.hash
    current.value = row
    detail.value = null
    tags.value = row.tags
    codes.value = null
    codesError.value = false
    resetSubjectDraft(row)
    getRecordingDetail(hash).then(rec => {
        if (stillShowing(hash)) {
            detail.value = rec
        }
    }).catch(() => {
        if (stillShowing(hash)) {
            showToast(t('Failed to load the recording.', SCOPE), 'danger')
            closeDetails()
        }
    })
    getRecordingCodes(hash).then(summary => {
        if (stillShowing(hash)) {
            codes.value = summary
        }
    }).catch(() => {
        if (stillShowing(hash)) {
            codesError.value = true
        }
    })
    if (row.tag_count > row.tags.length) {
        try {
            const [answer] = await listRecordingTags([hash])
            if (stillShowing(hash) && answer) {
                tags.value = answer.tags
            }
        } catch {
            // The row's first tags stay shown; the drawer says how many there are.
        }
    }
}

function closeDetails () {
    current.value = null
}

function openInViewer () {
    if (!current.value) {
        return
    }
    const { href } = router.resolve({ name: 'viewer', query: { files: current.value.hash } })
    window.open(href, '_blank')
}

/** Whether the draft differs from the values the row was opened with. */
const subjectChanged = computed(() => {
    const row = current.value
    return !!row && subjectFields.value.some(field => (row.subject[field.key] ?? '') !== subjectDraft[field.key])
})

async function saveSubject () {
    const row = current.value
    if (!row || !subjectChanged.value) {
        return
    }
    const changes: Record<string, string | null> = {}
    for (const field of subjectFields.value) {
        const next = subjectDraft[field.key]
        if ((row.subject[field.key] ?? '') !== next) {
            changes[field.key] = next || null
        }
    }
    savingSubject.value = true
    try {
        const values = await updateDatasetMemberSubject(props.datasetId, row.hash, changes)
        // The server answers with every value the member carries; the row keeps the fields this caller is shown.
        const shown = new Set(subjectFields.value.map(field => field.key))
        row.subject = Object.fromEntries(Object.entries(values).filter(([key]) => shown.has(key)))
        if (stillShowing(row.hash)) {
            resetSubjectDraft(row)
        }
        showToast(t('Subject values saved.', SCOPE), 'neutral')
    } catch (err) {
        showToast(errorDetail(err, t('The subject values could not be saved.', SCOPE)), 'danger')
    } finally {
        savingSubject.value = false
    }
}

function revertSubject () {
    if (current.value) {
        resetSubjectDraft(current.value)
    }
}

/** The deployment's registered vocabularies, read once; the drawer names the ones a recording has no codes from. */
const vocabularies = ref<Vocabulary[]>([])

async function loadVocabularies () {
    try {
        vocabularies.value = await listVocabularies()
    } catch {
        // Without the list the drawer shows the codes present and nothing about the rest.
    }
}

/** The labels of the registered vocabularies the open recording has no codes from. */
const uncodedVocabularies = computed(() => {
    const present = new Set((codes.value ?? []).map(group => group.standard))
    return vocabularies.value.filter(vocabulary => !present.has(vocabulary.standard)).map(vocabulary => vocabulary.label)
})

/**
 * The heading of a standard's codes: its registered label, or the identifier for a standard the deployment has not
 * registered.
 * @param group - One standard's codes.
 */
function standardLabel (group: CodeSummary) {
    return group.label ?? group.standard
}
</script>

<template>
    <div class="dataset-browser">
        <wa-callout v-if="error" variant="danger">{{ error }}</wa-callout>
        <DatasetBrowserFilters
            :facets="facets"
            :subject-fields="subjectFields"
            :suppression-k="suppressionK"
            @change="onFilter"
        ></DatasetBrowserFilters>
        <p v-if="summary && (!loading || rows.length)" class="dataset-browser__summary" role="status">{{ summary }}</p>
        <DataTable v-if="!error"
            :columns="columns"
            :current-key="current?.hash ?? null"
            :label="t('Recordings in this dataset', SCOPE)"
            :loading="loading"
            :page="page"
            :page-size="PAGE_SIZE"
            :row-key="rowKey"
            :rows="rows"
            :sort="sort"
            :total="total ?? 0"
            @activate="openDetails"
            @update:page="onPage"
            @update:sort="onSort"
        >
            <template #cell-tags="{ row }">
                <span class="dataset-browser__tags">
                    <wa-tag v-for="tag in row.tags" :key="tag.id"
                        size="s"
                        :variant="tag.curated ? 'brand' : 'neutral'"
                    >
                        {{ tag.name }}
                    </wa-tag>
                    <span v-if="hiddenTagCount(row) > 0" class="dataset-browser__more">
                        {{ t('+{count}', SCOPE, { count: hiddenTagCount(row) }) }}
                    </span>
                </span>
            </template>
            <template #empty>{{ emptyText }}</template>
        </DataTable>
    </div>

    <RecordingDetailsDrawer
        :label="current?.display_name ?? ''"
        :open="!!current"
        :recording="detail"
        @close="closeDetails"
    >
        <template v-if="current">
            <section v-if="subjectFields.length" class="dataset-browser__section">
                <h3 class="dataset-browser__heading">{{ t('Subject', SCOPE) }}</h3>
                <div v-if="canManage" class="dataset-browser__subject-form">
                    <wa-select v-for="field in subjectFields" :key="field.key"
                        :disabled="savingSubject"
                        :label="field.label"
                        size="s"
                        v-wa="[subjectDraft, field.key]"
                    >
                        <wa-option value="">{{ t('Not set', SCOPE) }}</wa-option>
                        <wa-option v-for="choice in field.choices" :key="choice" :value="choice">
                            {{ choice }}
                        </wa-option>
                    </wa-select>
                    <div class="form-actions">
                        <wa-button
                            appearance="plain"
                            :disabled="!subjectChanged || savingSubject"
                            size="s"
                            @click="revertSubject"
                        >
                            {{ t('Revert', SCOPE) }}
                        </wa-button>
                        <wa-button
                            appearance="filled-outlined"
                            :disabled="!subjectChanged"
                            :loading="savingSubject"
                            size="s"
                            variant="brand"
                            @click="saveSubject"
                        >
                            {{ t('Save', SCOPE) }}
                        </wa-button>
                    </div>
                </div>
                <dl v-else class="wa-stack dataset-browser__list">
                    <div v-for="field in subjectFields" :key="field.key" class="wa-flank dataset-browser__flank">
                        <dt>{{ field.label }}</dt>
                        <dd>{{ current.subject[field.key] ?? t('Not set', SCOPE) }}</dd>
                    </div>
                </dl>
            </section>

            <section class="dataset-browser__section">
                <h3 class="dataset-browser__heading">{{ t('Tags', SCOPE) }}</h3>
                <p v-if="!tags.length" class="dataset-browser__quiet">{{ t('No tags.', SCOPE) }}</p>
                <div v-else class="dataset-browser__tags">
                    <wa-tag v-for="tag in tags" :key="tag.id"
                        size="s"
                        :variant="tag.curated ? 'brand' : 'neutral'"
                    >
                        {{ tag.name }}
                    </wa-tag>
                </div>
            </section>

            <section class="dataset-browser__section">
                <h3 class="dataset-browser__heading">{{ t('Codes', SCOPE) }}</h3>
                <p v-if="codesError" class="dataset-browser__quiet">{{ t('The codes could not be loaded.', SCOPE) }}</p>
                <wa-spinner v-else-if="codes === null"></wa-spinner>
                <p v-else-if="!codes.length" class="dataset-browser__quiet">
                    {{ t('No coded annotations.', SCOPE) }}
                </p>
                <template v-else>
                    <div v-for="group in codes" :key="group.standard" class="dataset-browser__codes">
                        <h4 class="dataset-browser__standard">
                            {{ standardLabel(group) }}
                            <wa-badge pill variant="neutral">{{ group.count }}</wa-badge>
                        </h4>
                        <ul class="dataset-browser__code-list">
                            <li v-for="code in group.codes" :key="code.value" class="dataset-browser__code">
                                <span class="dataset-browser__code-name">{{ code.name ?? code.value }}</span>
                                <span class="dataset-browser__code-count">{{ code.count }}</span>
                            </li>
                        </ul>
                    </div>
                </template>
                <p v-if="codes !== null && uncodedVocabularies.length" class="dataset-browser__quiet">
                    {{ t('No codes from {list}.', SCOPE, { list: uncodedVocabularies.join(', ') }) }}
                </p>
            </section>
        </template>

        <template #footer>
            <wa-button
                appearance="filled-outlined"
                size="s"
                variant="brand"
                @click="openInViewer"
            >
                <wa-icon name="arrow-up-right-from-square" slot="start"></wa-icon>
                {{ t('Open in viewer', SCOPE) }}
            </wa-button>
        </template>
    </RecordingDetailsDrawer>
</template>

<style scoped>
.dataset-browser {
    display: flex;
    flex-direction: column;
    gap: var(--wa-space-m);
}

.dataset-browser__summary {
    color: var(--wa-color-text-quiet);
    font-size: var(--wa-font-size-s);
    margin: 0;
}

.dataset-browser__tags {
    align-items: center;
    display: flex;
    gap: var(--wa-space-2xs);
}

.dataset-browser__section .dataset-browser__tags {
    flex-wrap: wrap;
}

.dataset-browser__more,
.dataset-browser__quiet {
    color: var(--wa-color-text-quiet);
    font-size: var(--wa-font-size-s);
    margin: 0;
}

.dataset-browser__section {
    display: flex;
    flex-direction: column;
    gap: var(--wa-space-s);
}

.dataset-browser__heading {
    font-size: var(--wa-font-size-m);
    margin: var(--wa-space-m) 0 0;
}

.dataset-browser__subject-form {
    display: flex;
    flex-direction: column;
    gap: var(--wa-space-s);
}

.dataset-browser__list {
    margin: 0;
}

.dataset-browser__flank {
    --flank-size: 14ch;
}

.dataset-browser__codes {
    display: flex;
    flex-direction: column;
    gap: var(--wa-space-2xs);
}

.dataset-browser__standard {
    align-items: center;
    display: flex;
    font-size: var(--wa-font-size-s);
    gap: var(--wa-space-xs);
    margin: 0;
}

.dataset-browser__code-list {
    list-style: none;
    margin: 0;
    padding: 0;
}

.dataset-browser__code {
    display: flex;
    font-size: var(--wa-font-size-s);
    gap: var(--wa-space-s);
    justify-content: space-between;
    padding-block: var(--wa-space-3xs);
}

.dataset-browser__code-name {
    min-width: 0;
    overflow-wrap: anywhere;
}

.dataset-browser__code-count {
    color: var(--wa-color-text-quiet);
    font-variant-numeric: tabular-nums;
}
</style>
