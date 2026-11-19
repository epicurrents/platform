<script setup lang="ts">
/**
 * The dataset browser's filters: one multiple select per facet, modality, each subject field of choices the caller is
 * shown and the tags, each option carrying how many members it would match, and a pair of inputs per number field.
 *
 * The counts are the server's facets. An option that would match nothing is disabled unless it is already selected,
 * so a selection can always be undone. A number field's inputs are a range, both ends inclusive and an empty end
 * open; their placeholders are the lowest and highest value under the other filters. Every change emits the whole
 * selection; the host fetches the first page under it.
 *
 * @package    epicurrents-platform
 */
import { computed, reactive, watch } from 'vue'
import { t } from '#i18n'
import type { DatasetBrowseFilters, DatasetFacets, SubjectField, SubjectRange } from '#api/library'
import { formatSubjectValue } from '#lib/format'
import type WaInput from '@awesome.me/webawesome/dist/components/input/input.js'
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
/** Each number field's range as typed, keyed by field key; an empty string is an open end. */
const ranges = reactive<Record<string, { min: string; max: string }>>({})

interface FilterOption {
    value: string
    /** The value as shown, on its own; what a selected value's chip carries. */
    label: string
    /** The value with its count, in the list. */
    text: string
    disabled: boolean
}

interface SelectControl {
    kind: 'select'
    name: string
    label: string
    options: FilterOption[]
}

interface RangeControl {
    kind: 'range'
    /** The number field's key. */
    name: string
    label: string
    /** The lowest and highest value under the other filters, as shown; empty when no member has one. */
    low: string
    high: string
}

type FilterControl = SelectControl | RangeControl

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

/**
 * The range control of a number field.
 * @param field - A number field.
 */
function rangeControl (field: SubjectField): RangeControl {
    const facet = props.facets.ranges[field.key]
    return {
        kind: 'range',
        name: field.key,
        label: field.label,
        low: facet?.min === null || facet?.min === undefined ? '' : formatSubjectValue(facet.min),
        high: facet?.max === null || facet?.max === undefined ? '' : formatSubjectValue(facet.max),
    }
}

const controls = computed<FilterControl[]>(() => {
    const list: FilterControl[] = []
    if (props.facets.modality.length) {
        list.push({
            kind: 'select',
            name: 'modality',
            label: t('Modality', SCOPE),
            options: options('modality', props.facets.modality.map(entry => ({
                ...entry,
                label: entry.value.toUpperCase(),
            }))),
        })
    }
    for (const field of props.subjectFields) {
        if (field.type === 'number') {
            list.push(rangeControl(field))
            continue
        }
        const name = `${SUBJECT_PREFIX}${field.key}`
        const entries = (props.facets.subject[field.key] ?? []).map(entry => ({ ...entry, label: entry.value }))
        list.push({ kind: 'select', name, label: field.label, options: options(name, entries) })
    }
    if (props.facets.tags.length) {
        list.push({
            kind: 'select',
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
    const choiceNames = new Set(
        fields.filter(field => field.type !== 'number').map(field => `${SUBJECT_PREFIX}${field.key}`)
    )
    const numberKeys = new Set(fields.filter(field => field.type === 'number').map(field => field.key))
    for (const name of Object.keys(selection)) {
        if (name.startsWith(SUBJECT_PREFIX) && !choiceNames.has(name)) {
            delete selection[name]
        }
    }
    for (const name of choiceNames) {
        selection[name] ??= []
    }
    for (const key of Object.keys(ranges)) {
        if (!numberKeys.has(key)) {
            delete ranges[key]
        }
    }
    for (const key of numberKeys) {
        ranges[key] ??= { min: '', max: '' }
    }
}, { immediate: true })

const active = computed(() =>
    Object.values(selection).some(values => values.length > 0)
    || Object.values(ranges).some(range => range.min !== '' || range.max !== '')
)

/**
 * A typed range as the client sends it, leaving out an end that is empty or not a number.
 * @param range - The range as typed.
 */
function parsedRange (range: { min: string; max: string }): SubjectRange {
    const parsed: SubjectRange = {}
    const min = range.min === '' ? NaN : Number(range.min)
    const max = range.max === '' ? NaN : Number(range.max)
    if (Number.isFinite(min)) {
        parsed.min = min
    }
    if (Number.isFinite(max)) {
        parsed.max = max
    }
    return parsed
}

/** The selection as the client sends it. */
function filters (): DatasetBrowseFilters {
    const subject: Record<string, string[]> = {}
    for (const [name, values] of Object.entries(selection)) {
        if (name.startsWith(SUBJECT_PREFIX) && values.length) {
            subject[name.slice(SUBJECT_PREFIX.length)] = [...values]
        }
    }
    const sent: Record<string, SubjectRange> = {}
    for (const [key, range] of Object.entries(ranges)) {
        const parsed = parsedRange(range)
        if (parsed.min !== undefined || parsed.max !== undefined) {
            sent[key] = parsed
        }
    }
    return {
        modality: [...selection.modality],
        subject,
        ranges: sent,
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

/**
 * Take one end of a range as committed and emit the selection, when it changes what is sent.
 * @param key - The number field's key.
 * @param end - Which end of the range.
 * @param event - The input's change event.
 */
function onRange (key: string, end: 'min' | 'max', event: Event) {
    const before = JSON.stringify(parsedRange(ranges[key]))
    ranges[key][end] = String((event.target as WaInput).value ?? '').trim()
    if (JSON.stringify(parsedRange(ranges[key])) !== before) {
        emit('change', filters())
    }
}

function clearAll () {
    for (const name of Object.keys(selection)) {
        selection[name] = []
    }
    for (const range of Object.values(ranges)) {
        range.min = ''
        range.max = ''
    }
    emit('change', filters())
}
</script>

<template>
    <div v-if="controls.length" class="browser-filters">
        <template v-for="control in controls" :key="control.name">
            <wa-select v-if="control.kind === 'select'"
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
            <fieldset v-else class="browser-filters__range">
                <legend class="browser-filters__legend">{{ control.label }}</legend>
                <div class="browser-filters__ends">
                    <wa-input
                        :aria-label="t('{field}, from', SCOPE, { field: control.label })"
                        class="browser-filters__end"
                        inputmode="decimal"
                        :placeholder="control.low || t('From', SCOPE)"
                        size="s"
                        type="number"
                        :value="ranges[control.name].min"
                        @change="onRange(control.name, 'min', $event)"
                    ></wa-input>
                    <span aria-hidden="true" class="browser-filters__dash">–</span>
                    <wa-input
                        :aria-label="t('{field}, to', SCOPE, { field: control.label })"
                        class="browser-filters__end"
                        inputmode="decimal"
                        :placeholder="control.high || t('To', SCOPE)"
                        size="s"
                        type="number"
                        :value="ranges[control.name].max"
                        @change="onRange(control.name, 'max', $event)"
                    ></wa-input>
                </div>
            </fieldset>
        </template>
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
    /* Labels align across the row, so a range's hint below its inputs leaves the inputs level with the selects'. */
    align-items: start;
    display: flex;
    flex-wrap: wrap;
    gap: var(--wa-space-s);
}

.browser-filters__select {
    flex: 1 1 12rem;
    max-width: 18rem;
}

.browser-filters__range {
    border: none;
    display: flex;
    flex: 1 1 12rem;
    flex-direction: column;
    gap: var(--wa-space-3xs);
    margin: 0;
    max-width: 18rem;
    min-width: 0;
    padding: 0;
}

.browser-filters__legend {
    font-size: var(--wa-font-size-s);
    font-weight: var(--wa-form-control-label-font-weight);
    margin-bottom: var(--wa-space-3xs);
    padding: 0;
}

.browser-filters__ends {
    align-items: center;
    display: flex;
    gap: var(--wa-space-2xs);
}

.browser-filters__end {
    flex: 1 1 0;
    min-width: 0;
}

.browser-filters__dash {
    color: var(--wa-color-text-quiet);
}

.browser-filters__clear {
    align-self: end;
    flex: none;
}
</style>
