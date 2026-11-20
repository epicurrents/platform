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
 * Filtering is the server's too, and so are the facet counts the filters show.
 *
 * A manager tags and untags a recording in the drawer, and every recording the current filters match at once from
 * "Tag recordings…" (`POST` / `DELETE /datasets/{id}/tags/{tid}/`), which sends the same filters the page was fetched
 * with. A member the caller may not change is skipped by the server and counted in the outcome.
 *
 * Given a `shareToken`, the browser reads the dataset with it, for a reader without an account opening a shared or
 * publicly listed link. The server gives such a reader no tags and the annotation API takes no token, so the tag
 * column and the drawer's tags and codes are left out rather than shown empty; a signed-in reader keeps them.
 *
 * @package    epicurrents-platform
 */
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import type WaInput from '@awesome.me/webawesome/dist/components/input/input.js'
import { t } from '#i18n'
import {
    browseDataset,
    browseFiltersToQuery,
    getRecordingContentTypeId,
    listRecordingTags,
    tagDatasetMembers,
    tagObject,
    untagDatasetMembers,
    untagObject,
    updateDatasetMemberSubject,
    type DatasetBrowseFilters,
    type DatasetBrowseRow,
    type DatasetFacets,
    type DatasetTagBatch,
    type SubjectField,
    type SubjectValue,
    type TagChip,
} from '#api/library'
import { getRecordingCodes, listVocabularies, type CodeSummary, type Vocabulary } from '#api/annotations'
import { getRecordingDetail, type Recording } from '#api/recordings'
import { errorDetail } from '#lib/http'
import { formatDuration, formatMonth, formatSamplingRate, formatSubjectValue } from '#lib/format'
import { showToast } from '#lib/toast'
import DataTable, { type DataTableColumn } from '#components/DataTable.vue'
import DatasetBrowserFilters from '#components/DatasetBrowserFilters.vue'
import RecordingDetailsDrawer from '#components/RecordingDetailsDrawer.vue'
import TagPicker from '#components/TagPicker.vue'
import { useAuthStore } from '#stores/auth'

const SCOPE = 'DatasetBrowser'
const PAGE_SIZE = 50
const SUBJECT_PREFIX = 'subject.'
/** The most recordings one batch tagging covers; the server refuses a query matching more. */
const MAX_BATCH_TAGGED = 5000

const props = defineProps<{
    /** The dataset's object hash or primary key. */
    datasetId: string
    /** Whether the caller may set subject values; the server decides in the end. */
    canManage: boolean
    /** Share token the dataset is read with, for a reader opening a shared link. */
    shareToken?: string
}>()

const router = useRouter()
const authStore = useAuthStore()

/** Whether the tag picker offers to create a tag; creation is staff-only unless the deployment opens it. */
const canCreateTags = computed(() => authStore.isStaff)

/** Whether the caller reads through a share token alone, which reaches no tags and no annotations. */
const tokenOnly = computed(() => !!props.shareToken && !authStore.isAuthenticated)

// ── Page ─────────────────────────────────────────────────────────────────

const rows = ref<DatasetBrowseRow[]>([])
const subjectFields = ref<SubjectField[]>([])
const facets = ref<DatasetFacets>({ modality: [], subject: {}, ranges: {}, tags: [] })
const filters = ref<DatasetBrowseFilters>({ modality: [], subject: {}, ranges: {}, tags: [] })
const total = ref(0)
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
        }, filters.value, props.shareToken)
        if (seq !== loadSeq) {
            return
        }
        rows.value = result.items
        subjectFields.value = result.subject_fields
        facets.value = result.facets
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
    if (!tokenOnly.value) {
        loadVocabularies()
    }
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
    const { modality, subject, ranges, tags } = filters.value
    return modality.length > 0
        || tags.length > 0
        || Object.values(subject).some(values => values.length > 0)
        || Object.keys(ranges).length > 0
})

/** The line above the table: how many recordings match. */
const summary = computed(() => {
    if (filtered.value) {
        return t('{count} matching recordings', SCOPE, { count: total.value })
    }
    return t('{count} recordings', SCOPE, { count: total.value })
})

const emptyText = computed(() => {
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
        numeric: field.type === 'number',
        sortable: true,
        format: (row: DatasetBrowseRow) => formatSubjectValue(row.subject[field.key]),
    })),
    { key: 'date', label: t('Date', SCOPE), sortable: true, format: row => formatMonth(row.created_at) },
    ...(tokenOnly.value ? [] : [{ key: 'tags', label: t('Tags', SCOPE) }]),
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
/** Whether `tags` holds every tag of the open recording, not only the row's first ones; the picker waits for it. */
const tagsLoaded = ref(false)
/** Whether reading the open recording's full tag list failed, which leaves the picker read-only. */
const tagsFailed = ref(false)
const codes = ref<CodeSummary[] | null>(null)
const codesError = ref(false)
/** Whether the open recording's codes are not served to this reader, which leaves the section out. */
const codesUnavailable = ref(false)
// Each field's value as the form holds it: a choice, or a number field's value as typed; empty for none.
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
        subjectDraft[field.key] = draftOf(row, field)
    }
}

/**
 * A row's value for a field as the form holds it.
 * @param row - A browser row.
 * @param field - One of the shown subject fields.
 */
function draftOf (row: DatasetBrowseRow, field: SubjectField) {
    const value = row.subject[field.key]
    return value === undefined ? '' : String(value)
}

/**
 * The value a field's draft is sent as: null for an empty draft, a number for a number field, NaN for a number field
 * whose draft is not one.
 * @param field - One of the shown subject fields.
 */
function draftValue (field: SubjectField): SubjectValue | null {
    const draft = subjectDraft[field.key].trim()
    if (draft === '') {
        return null
    }
    return field.type === 'number' ? Number(draft) : draft
}

/**
 * Take a number field's draft as typed. The input is bound by hand because `v-wa` writes a number input back as a
 * number, NaN when empty, and the draft is compared and sent as the string the field holds.
 * @param key - The number field's key.
 * @param event - The input's input event.
 */
function onNumberDraft (key: string, event: Event) {
    subjectDraft[key] = String((event.target as WaInput).value ?? '')
}

async function openDetails (row: DatasetBrowseRow) {
    const hash = row.hash
    current.value = row
    detail.value = null
    tags.value = row.tags
    tagsLoaded.value = row.tag_count <= row.tags.length
    tagsFailed.value = false
    codes.value = null
    codesError.value = false
    codesUnavailable.value = false
    resetSubjectDraft(row)
    getRecordingDetail(hash, props.shareToken).then(rec => {
        if (stillShowing(hash)) {
            detail.value = rec
        }
    }).catch(() => {
        if (stillShowing(hash)) {
            showToast(t('Failed to load the recording.', SCOPE), 'danger')
            closeDetails()
        }
    })
    if (tokenOnly.value) {
        return
    }
    getRecordingCodes(hash).then(summary => {
        if (stillShowing(hash)) {
            codes.value = summary
        }
    }).catch(() => {
        if (stillShowing(hash)) {
            // The codes are read with the session alone, so a signed-in reader whose access is the link's token is
            // refused them; that is nothing to report, and the section is left out as it is without an account.
            codesError.value = !props.shareToken
            codesUnavailable.value = !!props.shareToken
        }
    })
    if (row.tag_count > row.tags.length) {
        try {
            const [answer] = await listRecordingTags([hash])
            // A recording the session does not read is left out of the answer, and the row's first tags stay shown.
            if (stillShowing(hash) && answer) {
                tags.value = answer.tags
                tagsLoaded.value = true
            }
        } catch {
            // The row's first tags stay shown, and the picker stays read-only rather than editing a partial list.
            if (stillShowing(hash)) {
                tagsFailed.value = true
            }
        }
    }
}

function closeDetails () {
    current.value = null
}

/**
 * Open the recordings the current filters match in the viewer, in the browser's order, every recording of the
 * dataset when nothing is filtered. The link carries the filters and the sort rather than the matching recordings,
 * so its length does not grow with them.
 */
function openMatchingInViewer () {
    const query: Record<string, string | string[]> = {
        dataset: props.datasetId,
        sort: sort.value,
        ...browseFiltersToQuery(filters.value),
    }
    if (props.shareToken) {
        query.token = props.shareToken
    }
    const { href } = router.resolve({ name: 'viewer', query })
    window.open(href, '_blank')
}

function openInViewer () {
    if (!current.value) {
        return
    }
    const query: Record<string, string> = { files: current.value.hash }
    if (props.shareToken) {
        query.token = props.shareToken
    }
    const { href } = router.resolve({ name: 'viewer', query })
    window.open(href, '_blank')
}

/** Whether the draft differs from the values the row was opened with. */
const subjectChanged = computed(() => {
    const row = current.value
    return !!row && subjectFields.value.some(field => draftOf(row, field) !== subjectDraft[field.key].trim())
})

/** Whether a number field's draft is not a number, which the form refuses before it is sent. */
const subjectInvalid = computed(() => subjectFields.value.some(field => {
    const value = draftValue(field)
    return typeof value === 'number' && !Number.isFinite(value)
}))

async function saveSubject () {
    const row = current.value
    if (!row || !subjectChanged.value || subjectInvalid.value) {
        return
    }
    const changes: Record<string, SubjectValue | null> = {}
    for (const field of subjectFields.value) {
        if (draftOf(row, field) !== subjectDraft[field.key].trim()) {
            changes[field.key] = draftValue(field)
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

// ── Tagging ──────────────────────────────────────────────────────────────

const savingTags = ref(false)

/**
 * Show the tags the open recording carries after a write, in the drawer and in its row: the server's list, read
 * again so the order and the count are the server's, or *fallback* when that read fails.
 * @param row - The row written.
 * @param fallback - Every tag the recording carries as the write implies, from the tags shown before it.
 */
async function showTagsAfterWrite (row: DatasetBrowseRow, fallback: TagChip[]) {
    let next = fallback
    try {
        const [answer] = await listRecordingTags([row.hash])
        next = answer?.tags ?? []
    } catch {
        // The write landed; the drawer shows what it implies.
    }
    row.tags = next.slice(0, 3)
    row.tag_count = next.length
    if (stillShowing(row.hash)) {
        tags.value = next
    }
}

/**
 * Tag the open recording.
 * @param tag - The tag picked.
 */
async function addTag (tag: TagChip) {
    const row = current.value
    if (!row) {
        return
    }
    const before = tags.value
    savingTags.value = true
    try {
        await tagObject(tag.id, await getRecordingContentTypeId(), row.hash)
        await showTagsAfterWrite(row, [...before, tag])
    } catch (err) {
        showToast(errorDetail(err, t('The tag could not be added.', SCOPE)), 'danger')
    } finally {
        savingTags.value = false
    }
}

/**
 * Remove a tag from the open recording.
 * @param tag - The chip's tag.
 */
async function removeTag (tag: TagChip) {
    const row = current.value
    if (!row) {
        return
    }
    const before = tags.value
    savingTags.value = true
    try {
        await untagObject(tag.id, await getRecordingContentTypeId(), row.hash)
        await showTagsAfterWrite(row, before.filter(other => other.id !== tag.id))
    } catch (err) {
        showToast(errorDetail(err, t('The tag could not be removed.', SCOPE)), 'danger')
    } finally {
        savingTags.value = false
    }
}

const batchOpen = ref(false)
const batchTag = ref<TagChip | null>(null)
const batchBusy = ref<'add' | 'remove' | null>(null)

/** The batch dialog's account of what it applies to. */
const batchScope = computed(() => {
    if (filtered.value) {
        return t('The tag is added to or removed from the {count} recordings that match the current filters.', SCOPE, {
            count: total.value,
        })
    }
    return t('The tag is added to or removed from all {count} recordings in this dataset.', SCOPE, {
        count: total.value,
    })
})

const batchTooMany = computed(() => total.value > MAX_BATCH_TAGGED)

/** The batch picker's selection: the one tag chosen, if any. */
const batchSelected = computed(() => batchTag.value ? [batchTag.value] : [])

function openBatch () {
    batchTag.value = null
    batchOpen.value = true
}

/**
 * Close the batch dialog, unless a batch is running: the dialog's own close is then cancelled too, so it cannot
 * close while `batchOpen` stays set.
 * @param event - The dialog's `wa-hide`, when it asked to close.
 */
function closeBatch (event?: Event) {
    if (batchBusy.value) {
        event?.preventDefault()
        return
    }
    batchOpen.value = false
}

/**
 * Choose the batch's tag, replacing any chosen before.
 * @param tag - The tag picked.
 */
function chooseBatchTag (tag: TagChip) {
    batchTag.value = tag
}

function clearBatchTag () {
    batchTag.value = null
}

/**
 * The outcome of a batch as a sentence per count that is not zero.
 * @param result - The server's counts.
 * @param remove - Whether the tag was removed.
 */
function batchOutcome (result: DatasetTagBatch, remove: boolean) {
    if (!result.matched) {
        return t('No recordings matched.', SCOPE)
    }
    const parts = [
        remove
            ? t('{count} recordings untagged.', SCOPE, { count: result.changed })
            : t('{count} recordings tagged.', SCOPE, { count: result.changed }),
    ]
    if (result.unchanged) {
        parts.push(remove
            ? t('{count} did not have the tag.', SCOPE, { count: result.unchanged })
            : t('{count} already had it.', SCOPE, { count: result.unchanged }))
    }
    if (result.skipped) {
        parts.push(t('{count} skipped, as you cannot change them.', SCOPE, { count: result.skipped }))
    }
    return parts.join(' ')
}

/**
 * Add the chosen tag to, or remove it from, every recording the current filters match.
 * @param remove - Whether to remove the tag.
 */
async function runBatch (remove: boolean) {
    const tag = batchTag.value
    if (!tag || batchBusy.value) {
        return
    }
    batchBusy.value = remove ? 'remove' : 'add'
    try {
        const result = remove
            ? await untagDatasetMembers(props.datasetId, tag.id, filters.value)
            : await tagDatasetMembers(props.datasetId, tag.id, filters.value)
        showToast(batchOutcome(result, remove), 'success')
        // Not busy any more before the dialog closes, or its hide would be cancelled.
        batchBusy.value = null
        batchOpen.value = false
        await load()
    } catch (err) {
        const fallback = remove
            ? t('The tag could not be removed from the recordings.', SCOPE)
            : t('The recordings could not be tagged.', SCOPE)
        showToast(errorDetail(err, fallback), 'danger')
    } finally {
        batchBusy.value = null
    }
}

function addBatch () {
    return runBatch(false)
}

function removeBatch () {
    return runBatch(true)
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
            @change="onFilter"
        ></DatasetBrowserFilters>
        <div class="dataset-browser__toolbar">
            <p v-if="!loading || rows.length" class="dataset-browser__summary" role="status">{{ summary }}</p>
            <wa-button v-if="canManage"
                appearance="plain"
                :disabled="loading || !total"
                size="s"
                variant="brand"
                @click="openBatch"
            >
                <wa-icon name="tag" slot="start"></wa-icon>
                {{ t('Tag recordings…', SCOPE) }}
            </wa-button>
            <wa-button
                appearance="plain"
                :disabled="loading || !total"
                size="s"
                variant="brand"
                @click="openMatchingInViewer"
            >
                <wa-icon name="arrow-up-right-from-square" slot="start"></wa-icon>
                {{ filtered ? t('Open matching in viewer', SCOPE) : t('Open all in viewer', SCOPE) }}
            </wa-button>
        </div>
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
            :total="total"
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
                    <div v-for="(field, idx) in subjectFields" :key="field.key" class="dataset-browser__subject-row">
                        <wa-input v-if="field.type === 'number'"
                            :disabled="savingSubject"
                            inputmode="decimal"
                            :label="field.label"
                            :placeholder="t('Not set', SCOPE)"
                            size="s"
                            type="number"
                            :value="subjectDraft[field.key]"
                            @input="onNumberDraft(field.key, $event)"
                        ></wa-input>
                        <wa-select v-else
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
                        <div v-if="idx === subjectFields.length - 1" class="form-actions">
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
                                :disabled="!subjectChanged || subjectInvalid"
                                :loading="savingSubject"
                                size="s"
                                variant="brand"
                                @click="saveSubject"
                            >
                                {{ t('Save', SCOPE) }}
                            </wa-button>
                        </div>
                    </div>
                </div>
                <dl v-else class="wa-stack dataset-browser__list">
                    <div v-for="field in subjectFields" :key="field.key" class="wa-flank dataset-browser__flank">
                        <dt>{{ field.label }}</dt>
                        <dd>{{ current.subject[field.key] ?? t('Not set', SCOPE) }}</dd>
                    </div>
                </dl>
            </section>

            <section v-if="!tokenOnly" class="dataset-browser__section">
                <h3 class="dataset-browser__heading">{{ t('Tags', SCOPE) }}</h3>
                <TagPicker v-if="canManage"
                    :can-create="canCreateTags"
                    :disabled="savingTags || !tagsLoaded"
                    :label="t('Add a tag', SCOPE)"
                    :selected="tags"
                    @add="addTag"
                    @remove="removeTag"
                ></TagPicker>
                <p v-if="canManage && tagsFailed" class="dataset-browser__quiet">
                    {{ t('Not every tag could be loaded, so the tags cannot be changed here.', SCOPE) }}
                </p>
                <p v-else-if="!canManage && !tags.length" class="dataset-browser__quiet">{{ t('No tags.', SCOPE) }}</p>
                <div v-else-if="!canManage" class="dataset-browser__tags">
                    <wa-tag v-for="tag in tags" :key="tag.id"
                        size="s"
                        :variant="tag.curated ? 'brand' : 'neutral'"
                    >
                        {{ tag.name }}
                    </wa-tag>
                </div>
            </section>

            <section v-if="!tokenOnly && !codesUnavailable" class="dataset-browser__section">
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

    <wa-dialog
        class="dataset-browser__dialog"
        :label="t('Tag recordings', SCOPE)"
        :open="batchOpen"
        @wa-hide.self="closeBatch"
    >
        <div class="dataset-browser__section">
            <p class="dataset-browser__dialog-text">{{ batchScope }}</p>
            <p class="dataset-browser__quiet">{{ t('Recordings you cannot change are skipped.', SCOPE) }}</p>
            <wa-callout v-if="batchTooMany" size="s" variant="warning">
                {{ t('More than {count} recordings match. Narrow the filters first.', SCOPE, {
                    count: MAX_BATCH_TAGGED.toLocaleString(),
                }) }}
            </wa-callout>
            <TagPicker
                :can-create="canCreateTags"
                :disabled="!!batchBusy"
                :label="t('Tag', SCOPE)"
                :selected="batchSelected"
                @add="chooseBatchTag"
                @remove="clearBatchTag"
            ></TagPicker>
        </div>
        <div slot="footer" class="form-actions">
            <wa-button
                appearance="plain"
                :disabled="!!batchBusy"
                @click="closeBatch"
            >
                {{ t('Cancel', SCOPE) }}
            </wa-button>
            <wa-button
                appearance="plain"
                :disabled="!batchTag || batchTooMany || batchBusy === 'add'"
                :loading="batchBusy === 'remove'"
                variant="danger"
                @click="removeBatch"
            >
                {{ t('Remove tag', SCOPE) }}
            </wa-button>
            <wa-button
                appearance="filled-outlined"
                :disabled="!batchTag || batchTooMany || batchBusy === 'remove'"
                :loading="batchBusy === 'add'"
                variant="brand"
                @click="addBatch"
            >
                {{ t('Add tag', SCOPE) }}
            </wa-button>
        </div>
    </wa-dialog>
</template>

<style scoped>
.dataset-browser {
    display: flex;
    flex-direction: column;
    gap: var(--wa-space-m);
}

.dataset-browser__toolbar {
    align-items: center;
    display: flex;
    flex-wrap: wrap;
    gap: var(--wa-space-s);
    justify-content: space-between;
}

.dataset-browser__summary {
    color: var(--wa-color-text-quiet);
    font-size: var(--wa-font-size-s);
    margin: 0;
    /* Holds the buttons at the end of the row, whichever of them a reader is offered. */
    margin-inline-end: auto;
}

.dataset-browser__dialog {
    --width: 32rem;
}

.dataset-browser__dialog-text {
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
.dataset-browser__subject-row {
    display: flex;
    gap: var(--wa-space-s);
}
.dataset-browser__subject-row wa-input,
.dataset-browser__subject-row wa-select {
    flex: 1;
}

.dataset-browser__subject-row .form-actions {
    display: flex;
    align-items: flex-end;
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
