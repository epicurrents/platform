<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { t } from '#i18n'
import { downloadAnnotationExport, listExportAnnotators } from '#api/annotationExport'
import type { ExportAnnotator, ExportFormat, ExportType } from '#api/annotationExport'
import { listDatasets } from '#api/library'
import type { Collection } from '#api/library'
import { readBlobError } from '#lib/download'
import { showToast } from '#lib/toast'
import { useAuthStore } from '#stores/auth'

const SCOPE = 'AnnotationExportView'
const authStore = useAuthStore()

/** Staff export across all annotators; everyone else is held to their own rows by the server. */
const canExportAllAuthors = computed(() => authStore.isStaff)

const form = reactive({
    datasetId: '',
    since: '',
    until: '',
    versionId: '',
})
const includeEvents = ref(true)
const includeLabels = ref(true)
const format = ref<ExportFormat>('json')
const datasets = ref<Collection[]>([])
const annotators = ref<ExportAnnotator[]>([])
const selectedAnnotators = ref(new Set<number>())
const rosterFailed = ref(false)
const exporting = ref(false)

const selectedTypes = computed<ExportType[]>(() => {
    const types: ExportType[] = []
    if (includeEvents.value) {
        types.push('events')
    }
    if (includeLabels.value) {
        types.push('labels')
    }
    return types
})

/**
 * CSV carries one type per file because events and labels do not share a column set. Rather than
 * letting the server reject the combination, the form surfaces it as a blocked Export button with
 * the reason spelled out.
 */
const csvNeedsOneType = computed(() => format.value === 'csv' && selectedTypes.value.length !== 1)

const allAnnotatorsSelected = computed(
    () => annotators.value.length > 0 && selectedAnnotators.value.size === annotators.value.length
)
const someAnnotatorsSelected = computed(() => selectedAnnotators.value.size > 0)
const noAnnotatorsSelected = computed(
    () => canExportAllAuthors.value && annotators.value.length > 0 && selectedAnnotators.value.size === 0
)

const canSubmit = computed(
    () => selectedTypes.value.length > 0 && !csvNeedsOneType.value && !noAnnotatorsSelected.value && !exporting.value
)

const blockedReason = computed(() => {
    if (selectedTypes.value.length === 0) {
        return t('Select at least one annotation type.', SCOPE)
    }
    if (csvNeedsOneType.value) {
        return t('CSV holds one annotation type per file. Select either events or labels, or switch to JSON.', SCOPE)
    }
    if (noAnnotatorsSelected.value) {
        return t('Select at least one annotator.', SCOPE)
    }
    return ''
})

onMounted(async () => {
    try {
        // 200 is the server-side maximum for the datasets listing (422 above it).
        datasets.value = await listDatasets({ limit: 200 })
    } catch {
        // A dataset list is a convenience filter, not a precondition for exporting.
        showToast(t('Could not load the dataset list; the dataset filter is unavailable.', SCOPE), 'warning')
    }
    if (canExportAllAuthors.value) {
        try {
            annotators.value = await listExportAnnotators()
            selectedAnnotators.value = new Set(annotators.value.map(a => a.id))
        } catch {
            // Without the roster the export simply covers every annotator, like an empty filter.
            rosterFailed.value = true
            showToast(t('Could not load the annotator list; the export will include all annotators.', SCOPE), 'warning')
        }
    }
})

function onSelectFormat (event: Event) {
    format.value = (event.target as HTMLInputElement).value as ExportFormat
}

function onToggleEvents (event: Event) {
    includeEvents.value = (event.target as HTMLInputElement).checked
}

function onToggleLabels (event: Event) {
    includeLabels.value = (event.target as HTMLInputElement).checked
}

function onToggleAnnotator (id: number, event: Event) {
    const next = new Set(selectedAnnotators.value)
    if ((event.target as HTMLInputElement).checked) {
        next.add(id)
    } else {
        next.delete(id)
    }
    selectedAnnotators.value = next
}

function onToggleAllAnnotators (event: Event) {
    const checked = (event.target as HTMLInputElement).checked
    selectedAnnotators.value = checked ? new Set(annotators.value.map(a => a.id)) : new Set()
}

async function onExport () {
    if (!canSubmit.value) {
        return
    }
    // A full selection equals no filter; only a genuine subset narrows the request.
    const annotatorIds = canExportAllAuthors.value && annotators.value.length > 0 && !allAnnotatorsSelected.value
        ? Array.from(selectedAnnotators.value)
        : undefined
    exporting.value = true
    try {
        await downloadAnnotationExport({
            types: selectedTypes.value,
            format: format.value,
            datasetId: form.datasetId ? Number(form.datasetId) : null,
            annotatorIds,
            since: form.since || null,
            until: form.until || null,
            versionId: form.versionId.trim() || null,
        })
        showToast(t('Export downloaded.', SCOPE), 'success')
    } catch (err) {
        const data = (err as { response?: { data?: unknown } }).response?.data
        const detail = await readBlobError(data)
        showToast(detail || t('Export failed.', SCOPE), 'danger')
    } finally {
        exporting.value = false
    }
}
</script>

<template>
    <main class="annotation-export-view">
        <wa-scroller orientation="vertical">
            <div class="annotation-export-view__scroll-wrap">
                <h1>{{ t('Export annotations', SCOPE) }}</h1>
                <p class="annotation-export-view__intro">
                    {{ t('Download events and labels as a file. Exported files identify annotators only by their user ID number — no names or usernames leave the platform — so the entries stay attributable through the annotator list held here.', SCOPE) }}
                </p>
                <p v-if="!canExportAllAuthors" class="annotation-export-view__intro">
                    {{ t('The export covers your own annotations, listed under annotator ID {id}. Exporting other annotators requires staff access.', SCOPE, { id: authStore.user?.id ?? '?' }) }}
                </p>

                <section class="annotation-export-section">
                    <h2>{{ t('Contents', SCOPE) }}</h2>
                    <div class="annotation-export-section__row">
                        <wa-checkbox :checked="includeEvents" @change="onToggleEvents">
                            {{ t('Events', SCOPE) }}
                        </wa-checkbox>
                        <wa-checkbox :checked="includeLabels" @change="onToggleLabels">
                            {{ t('Labels', SCOPE) }}
                        </wa-checkbox>
                    </div>
                    <wa-radio-group
                        :label="t('Format', SCOPE)"
                        :value="format"
                        @change="onSelectFormat"
                    >
                        <wa-radio value="json">{{ t('JSON — both types in one file, values kept intact', SCOPE) }}</wa-radio>
                        <wa-radio value="csv">{{ t('CSV — one type per file, opens in a spreadsheet', SCOPE) }}</wa-radio>
                    </wa-radio-group>
                </section>

                <section v-if="canExportAllAuthors" class="annotation-export-section">
                    <h2>{{ t('Annotators', SCOPE) }}</h2>
                    <p class="annotation-export-section__hint">
                        {{ t('Untick annotators to leave their entries out of the export. The same list maps the annotator IDs in an exported file back to people — it stays on the platform, so keep it at hand when working with exported data.', SCOPE) }}
                    </p>
                    <p v-if="rosterFailed" class="annotation-export-section__hint">
                        {{ t('The annotator list is unavailable; the export will include all annotators.', SCOPE) }}
                    </p>
                    <p v-else-if="annotators.length === 0" class="annotation-export-section__hint">
                        {{ t('No annotators yet.', SCOPE) }}
                    </p>
                    <table v-else class="annotation-export-annotators">
                        <thead>
                            <tr>
                                <th class="annotation-export-annotators__check">
                                    <wa-checkbox
                                        :checked="allAnnotatorsSelected"
                                        :indeterminate="someAnnotatorsSelected && !allAnnotatorsSelected"
                                        :title="t('Toggle all annotators', SCOPE)"
                                        @change="onToggleAllAnnotators"
                                    ></wa-checkbox>
                                </th>
                                <th>{{ t('ID', SCOPE) }}</th>
                                <th>{{ t('Name', SCOPE) }}</th>
                                <th>{{ t('Username', SCOPE) }}</th>
                                <th class="annotation-export-annotators__count">{{ t('Events', SCOPE) }}</th>
                                <th class="annotation-export-annotators__count">{{ t('Labels', SCOPE) }}</th>
                            </tr>
                        </thead>
                        <tbody>
                            <tr v-for="annotator in annotators" :key="annotator.id">
                                <td class="annotation-export-annotators__check">
                                    <wa-checkbox
                                        :checked="selectedAnnotators.has(annotator.id)"
                                        :title="t('Include this annotator', SCOPE)"
                                        @change="onToggleAnnotator(annotator.id, $event)"
                                    ></wa-checkbox>
                                </td>
                                <td>{{ annotator.id }}</td>
                                <td>{{ annotator.name }}</td>
                                <td>{{ annotator.username }}</td>
                                <td class="annotation-export-annotators__count">{{ annotator.events }}</td>
                                <td class="annotation-export-annotators__count">{{ annotator.labels }}</td>
                            </tr>
                        </tbody>
                    </table>
                </section>

                <section class="annotation-export-section">
                    <h2>{{ t('Filters', SCOPE) }}</h2>
                    <p class="annotation-export-section__hint">
                        {{ t('Leave a field empty to place no limit on it.', SCOPE) }}
                    </p>
                    <wa-select
                        :label="t('Dataset', SCOPE)"
                        v-wa="[form, 'datasetId']"
                    >
                        <wa-option value="">{{ t('All recordings', SCOPE) }}</wa-option>
                        <wa-option v-for="dataset in datasets" :key="dataset.id" :value="String(dataset.id)">
                            {{ dataset.name }}
                        </wa-option>
                    </wa-select>
                    <div class="annotation-export-section__row">
                        <wa-input
                            :label="t('From', SCOPE)"
                            type="date"
                            v-wa="[form, 'since']"
                        ></wa-input>
                        <wa-input
                            :label="t('To', SCOPE)"
                            type="date"
                            v-wa="[form, 'until']"
                        ></wa-input>
                    </div>
                    <wa-input
                        :hint="t('Signal version the annotations are bound to. Empty covers every version.', SCOPE)"
                        :label="t('Version', SCOPE)"
                        v-wa="[form, 'versionId']"
                    ></wa-input>
                </section>

                <div class="annotation-export-view__actions">
                    <p v-if="blockedReason" class="annotation-export-view__blocked">{{ blockedReason }}</p>
                    <wa-button
                        appearance="filled-outlined"
                        :disabled="!canSubmit"
                        :loading="exporting"
                        variant="brand"
                        @click="onExport"
                    >
                        {{ t('Export', SCOPE) }}
                    </wa-button>
                </div>
            </div>
        </wa-scroller>
    </main>
</template>

<style scoped>
.annotation-export-view {
    /* Fill the remaining vertical space within .route-view-wrapper so the internal wa-scroller
     * resolves its height against a bounded box. */
    flex: 1;
    display: flex;
    flex-direction: column;
    min-height: 0;
    overflow: hidden;
    width: 100%;
    padding: 2rem 1rem;
}

.annotation-export-view__scroll-wrap {
    /* Flex-1 wrap for wa-scroller — see AGENTS.md → WebAwesome shadow-DOM layout gotchas. */
    flex: 1;
    display: flex;
    flex-direction: column;
    margin: 0 auto;
    max-width: 720px;
    min-height: 0;
    overflow: hidden;
}

.annotation-export-view h1 {
    margin: 0 0 0.5rem;
}

.annotation-export-view__intro {
    color: var(--wa-color-text-quiet);
    margin: 0 0 1rem;
}

.annotation-export-section {
    display: flex;
    flex-direction: column;
    gap: 0.75rem;
    margin-bottom: 2.5rem;
}

.annotation-export-section h2 {
    margin: 0;
}

.annotation-export-section__hint {
    color: var(--wa-color-text-quiet);
    font-size: 0.9rem;
    margin: 0;
}

.annotation-export-section__row {
    display: flex;
    flex-wrap: wrap;
    gap: 1rem;
}

.annotation-export-annotators {
    border-collapse: collapse;
    width: 100%;
}

.annotation-export-annotators th,
.annotation-export-annotators td {
    border-bottom: 1px solid var(--wa-color-surface-border);
    padding: 0.4rem 0.6rem;
    text-align: left;
}

.annotation-export-annotators th {
    color: var(--wa-color-text-quiet);
    font-weight: 600;
}

.annotation-export-annotators__check {
    width: 2.5rem;
}

.annotation-export-annotators th.annotation-export-annotators__count,
.annotation-export-annotators td.annotation-export-annotators__count {
    text-align: right;
}

.annotation-export-view__actions {
    align-items: center;
    display: flex;
    gap: 1rem;
    justify-content: flex-end;
}

.annotation-export-view__blocked {
    color: var(--wa-color-text-quiet);
    font-size: 0.9rem;
    margin: 0;
}
</style>
