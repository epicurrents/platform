<script setup lang="ts">
/**
 * The dataset browser's filters: one multiple select per facet, modality, each subject field the caller is shown and
 * the tags, each option carrying how many members it would match.
 *
 * The counts are the server's facets. An option that would match nothing is disabled unless it is already selected,
 * so a selection can always be undone. Every change emits the whole selection; the host fetches the first page under
 * it.
 *
 * @package    epicurrents-platform
 */
import { computed, reactive, watch } from 'vue'
import { t } from '#i18n'
import type { DatasetBrowseFilters, DatasetFacets, SubjectField } from '#api/library'
import type WaSelect from '@awesome.me/webawesome/dist/components/select/select.js'

const SCOPE = 'DatasetBrowserFilters'
const SUBJECT_PREFIX = 'subject.'

const props = defineProps<{
    /** The facets of the current page. */
    facets: DatasetFacets
    /** The subject fields the caller is shown, in schema order. */
    subjectFields: SubjectField[]
}>()

const emit = defineEmits<{ change: [filters: DatasetBrowseFilters] }>()

/** The selected values per facet, keyed `modality`, `subject.<key>` and `tag`; tag ids as strings, as options carry. */
const selection = reactive<Record<string, string[]>>({ modality: [], tag: [] })

interface FilterOption {
    value: string
    /** The value as shown, on its own; what a selected value's chip carries. */
    label: string
    /** The value with its count, in the list. */
    text: string
    disabled: boolean
}

interface FilterControl {
    name: string
    label: string
    options: FilterOption[]
}

/**
 * An option's text: the value and its count.
 * @param label - The value as shown.
 * @param count - The facet count.
 */
function optionLabel (label: string, count: number) {
    return t('{value} ({count})', SCOPE, { value: label, count })
}

/**
 * The options of one facet.
 * @param name - The facet's key in the selection.
 * @param entries - Each option's value, label and count.
 */
function options (name: string, entries: { value: string; label: string; count: number }[]) {
    const selected = new Set(selection[name] ?? [])
    return entries.map(entry => ({
        value: entry.value,
        label: entry.label,
        text: optionLabel(entry.label, entry.count),
        disabled: entry.count === 0 && !selected.has(entry.value),
    }))
}

const controls = computed<FilterControl[]>(() => {
    const list: FilterControl[] = []
    if (props.facets.modality.length) {
        list.push({
            name: 'modality',
            label: t('Modality', SCOPE),
            options: options('modality', props.facets.modality.map(entry => ({
                ...entry,
                label: entry.value.toUpperCase(),
            }))),
        })
    }
    for (const field of props.subjectFields) {
        const name = `${SUBJECT_PREFIX}${field.key}`
        const entries = (props.facets.subject[field.key] ?? []).map(entry => ({ ...entry, label: entry.value }))
        list.push({ name, label: field.label, options: options(name, entries) })
    }
    if (props.facets.tags.length) {
        list.push({
            name: 'tag',
            label: t('Tags', SCOPE),
            options: options('tag', props.facets.tags.map(tag => ({
                value: String(tag.id),
                label: tag.name,
                count: tag.count,
            }))),
        })
    }
    return list
})

// A field newly shown starts unselected; one no longer shown is dropped from the selection.
watch(() => props.subjectFields, fields => {
    const names = new Set(fields.map(field => `${SUBJECT_PREFIX}${field.key}`))
    for (const name of Object.keys(selection)) {
        if (name.startsWith(SUBJECT_PREFIX) && !names.has(name)) {
            delete selection[name]
        }
    }
    for (const name of names) {
        selection[name] ??= []
    }
}, { immediate: true })

const active = computed(() => Object.values(selection).some(values => values.length > 0))

/** The selection as the client sends it. */
function filters (): DatasetBrowseFilters {
    const subject: Record<string, string[]> = {}
    for (const [name, values] of Object.entries(selection)) {
        if (name.startsWith(SUBJECT_PREFIX) && values.length) {
            subject[name.slice(SUBJECT_PREFIX.length)] = [...values]
        }
    }
    return {
        modality: [...selection.modality],
        subject,
        tags: selection.tag.map(Number),
    }
}

/**
 * Take a select's new value and emit the selection. The value is read off the event rather than left to `v-wa`,
 * whose listener runs after this one.
 * @param name - The facet's key in the selection.
 * @param event - The select's change event.
 */
function onChange (name: string, event: Event) {
    const value = (event.target as WaSelect).value
    selection[name] = Array.isArray(value) ? [...value] : value ? [value] : []
    emit('change', filters())
}

function clearAll () {
    for (const name of Object.keys(selection)) {
        selection[name] = []
    }
    emit('change', filters())
}
</script>

<template>
    <div v-if="controls.length" class="browser-filters">
        <wa-select v-for="control in controls" :key="control.name"
            class="browser-filters__select"
            :label="control.label"
            max-options-visible="2"
            multiple
            :placeholder="t('Any', SCOPE)"
            size="s"
            with-clear
            v-wa="[selection, control.name]"
            @change="onChange(control.name, $event)"
        >
            <wa-option v-for="option in control.options" :key="option.value"
                :disabled="option.disabled"
                :label="option.label"
                :value="option.value"
            >
                {{ option.text }}
            </wa-option>
        </wa-select>
        <wa-button
            appearance="plain"
            class="browser-filters__clear"
            :disabled="!active"
            size="s"
            @click="clearAll"
        >
            {{ t('Clear filters', SCOPE) }}
        </wa-button>
    </div>
</template>

<style scoped>
.browser-filters {
    align-items: end;
    display: flex;
    flex-wrap: wrap;
    gap: var(--wa-space-s);
}

.browser-filters__select {
    flex: 1 1 12rem;
    max-width: 18rem;
}

.browser-filters__clear {
    flex: none;
}
</style>
