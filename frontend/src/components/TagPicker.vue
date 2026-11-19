<script setup lang="ts">
/**
 * A tag picker: the chosen tags as removable chips above a search field whose list offers the tags within the
 * caller's reach.
 *
 * The picker searches (`GET /tags/?q=`) and, where `canCreate` allows, creates a tag the search did not find; what a
 * pick or a removal means is the host's. It emits `add` and `remove` with the tag, and the host writes them and
 * passes the new selection back. An empty search lists the root tags.
 *
 * The list follows the listbox pattern with focus moved into it rather than an active descendant, because the field
 * is `wa-input`, whose inner input sits in a shadow root that an `aria-activedescendant` cannot reach across. It opens
 * on a click, on typing or on Arrow Down, never on focus alone, so focus returned to the field after a pick leaves it
 * closed. Arrow Down from the field enters the list, the arrows, Home and End move in it, Enter or Space picks,
 * and Escape returns to the field.
 *
 * @package    epicurrents-platform
 */
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { t } from '#i18n'
import { createTag, listTags, type TagChip } from '#api/library'
import { errorDetail } from '#lib/http'
import { toastNameWarnings } from '#lib/nameWarnings'
import { showToast } from '#lib/toast'
import type WaInput from '@awesome.me/webawesome/dist/components/input/input.js'

const SCOPE = 'TagPicker'
const SEARCH_DELAY_MS = 200
const RESULT_LIMIT = 20

const props = withDefaults(defineProps<{
    /** The field's label. */
    label: string
    /** The tags chosen, shown as chips and left out of the list. */
    selected: TagChip[]
    /** Whether the list offers to create a tag the search did not find; the server decides in the end. */
    canCreate?: boolean
    /**
     * Whether the picker takes no picks or removals, while the host loads or writes. The field turns read-only rather
     * than disabled, so focus stays in it.
     */
    disabled?: boolean
}>(), {
    canCreate: false,
    disabled: false,
})

const emit = defineEmits<{
    add: [tag: TagChip]
    remove: [tag: TagChip]
}>()

const root = ref<HTMLElement | null>(null)
const input = ref<WaInput | null>(null)

const search = ref('')
const open = ref(false)
const results = ref<TagChip[]>([])
const loading = ref(false)
const failed = ref(false)
const creating = ref(false)
// Each search takes a number; an answer to an earlier one is dropped, so a slow search cannot overwrite a later one.
let searchSeq = 0
let searchTimer: ReturnType<typeof setTimeout> | null = null

interface PickerOption {
    key: string
    /** The tag to pick, or null for the option creating one named by the search. */
    tag: TagChip | null
    text: string
}

const options = computed<PickerOption[]>(() => {
    const chosen = new Set(props.selected.map(tag => tag.id))
    const name = search.value.trim()
    // The last answer may be for an earlier search until the next one lands, so it is narrowed to the text as typed.
    const list: PickerOption[] = results.value
        .filter(tag => !chosen.has(tag.id) && tag.name.toLowerCase().includes(name.toLowerCase()))
        .map(tag => ({ key: String(tag.id), tag, text: tag.name }))
    const known = [...results.value, ...props.selected].some(tag => tag.name.toLowerCase() === name.toLowerCase())
    if (props.canCreate && name && !known && !loading.value) {
        list.push({ key: 'create', tag: null, text: t('Create tag "{name}"', SCOPE, { name }) })
    }
    return list
})

/** What the list says when it has no options. */
const emptyText = computed(() => {
    if (loading.value) {
        return t('Searching…', SCOPE)
    }
    if (failed.value) {
        return t('The tags could not be loaded.', SCOPE)
    }
    return search.value.trim() ? t('No matching tags.', SCOPE) : t('No tags to add.', SCOPE)
})

async function runSearch () {
    const seq = ++searchSeq
    loading.value = true
    failed.value = false
    try {
        const tags = await listTags({ q: search.value.trim(), limit: RESULT_LIMIT })
        if (seq === searchSeq) {
            results.value = tags.map(tag => ({ id: tag.id, name: tag.name, curated: tag.curated }))
        }
    } catch {
        if (seq === searchSeq) {
            results.value = []
            failed.value = true
        }
    } finally {
        if (seq === searchSeq) {
            loading.value = false
        }
    }
}

function scheduleSearch () {
    if (searchTimer !== null) {
        clearTimeout(searchTimer)
    }
    searchTimer = setTimeout(() => {
        searchTimer = null
        runSearch()
    }, SEARCH_DELAY_MS)
}

onBeforeUnmount(() => {
    if (searchTimer !== null) {
        clearTimeout(searchTimer)
    }
})

watch(() => props.disabled, disabled => {
    if (disabled) {
        open.value = false
    }
})

function openList () {
    if (props.disabled || open.value) {
        return
    }
    open.value = true
    runSearch()
}

function closeList () {
    open.value = false
}

function focusInput () {
    input.value?.focus()
}

/**
 * Move focus to an option, wrapping at either end.
 * @param index - The option's position; out of range wraps around.
 */
function focusOption (index: number) {
    const elements = optionElements()
    if (!elements.length) {
        return
    }
    elements[(index + elements.length) % elements.length]?.focus()
}

/** The option elements in list order, read from the DOM, whose order a `v-for` ref array does not promise. */
function optionElements () {
    return Array.from(root.value?.querySelectorAll<HTMLElement>('.tag-picker__option') ?? [])
}

/**
 * Take the typed search and look it up after a pause.
 * @param event - The field's input event.
 */
function onInput (event: Event) {
    search.value = String((event.target as WaInput).value ?? '')
    open.value = true
    // Holds "Create tag" back until the search has answered whether the name exists, and drops an answer still
    // out for the text before, which would otherwise clear `loading` against results for that text.
    ++searchSeq
    loading.value = true
    scheduleSearch()
}

/**
 * The field's keys: Arrow Down enters the list, Escape closes it.
 * @param event - The keydown on the field.
 */
async function onInputKeydown (event: KeyboardEvent) {
    if (event.key === 'ArrowDown') {
        event.preventDefault()
        openList()
        await nextTick()
        focusOption(0)
    } else if (event.key === 'Escape' && open.value) {
        event.preventDefault()
        event.stopPropagation()
        closeList()
    }
}

/**
 * An option's keys: the arrows, Home and End move, Enter and Space pick, Escape returns to the field.
 * @param event - The keydown on the option.
 * @param index - The option's position.
 */
function onOptionKeydown (event: KeyboardEvent, index: number) {
    switch (event.key) {
        case 'ArrowDown': {
            event.preventDefault()
            focusOption(index + 1)
            break
        }
        case 'ArrowUp': {
            event.preventDefault()
            if (index === 0) {
                focusInput()
            } else {
                focusOption(index - 1)
            }
            break
        }
        case 'Home': {
            event.preventDefault()
            focusOption(0)
            break
        }
        case 'End': {
            event.preventDefault()
            focusOption(optionElements().length - 1)
            break
        }
        case 'Enter':
        case ' ': {
            event.preventDefault()
            pick(options.value[index])
            break
        }
        case 'Escape': {
            // Kept from the drawer or dialog around the picker, which would otherwise close on it.
            event.preventDefault()
            event.stopPropagation()
            closeList()
            focusInput()
            break
        }
    }
}

/**
 * Close the list when focus leaves the picker.
 * @param event - The focusout on the picker.
 */
function onFocusout (event: FocusEvent) {
    const next = event.relatedTarget as Node | null
    if (!next || !root.value?.contains(next)) {
        closeList()
    }
}

/**
 * Pick an option: emit its tag, or create the tag the search names first.
 * @param option - The option picked.
 */
async function pick (option: PickerOption | undefined) {
    if (!option || creating.value || props.disabled) {
        return
    }
    let tag = option.tag
    if (!tag) {
        creating.value = true
        try {
            const created = await createTag({ name: search.value.trim() })
            toastNameWarnings(created.warnings)
            tag = { id: created.id, name: created.name, curated: created.curated }
        } catch (err) {
            showToast(errorDetail(err, t('The tag could not be created.', SCOPE)), 'danger')
            return
        } finally {
            creating.value = false
        }
    }
    emit('add', tag)
    search.value = ''
    if (input.value) {
        input.value.value = ''
    }
    closeList()
    focusInput()
}

/**
 * Pick the option clicked.
 * @param option - The option clicked.
 */
function onOptionClick (option: PickerOption) {
    pick(option)
}

/**
 * Remove a chip's tag.
 * @param tag - The chip's tag.
 */
function onRemove (tag: TagChip) {
    if (props.disabled) {
        return
    }
    emit('remove', tag)
    // The chip goes with its tag, and focus with it unless it is moved.
    focusInput()
}

/**
 * Keep focus where it is when a part of the list that is not an option is pressed, so the press does not close it.
 * @param event - The pointerdown on the list.
 */
function onListPointerdown (event: PointerEvent) {
    if (!(event.target as Element).closest('.tag-picker__option')) {
        event.preventDefault()
    }
}
</script>

<template>
    <div class="tag-picker" ref="root" @focusout="onFocusout">
        <div v-if="selected.length" class="tag-picker__chips">
            <wa-tag v-for="tag in selected" :key="tag.id"
                size="s"
                :variant="tag.curated ? 'brand' : 'neutral'"
                with-remove
                @wa-remove="onRemove(tag)"
            >
                {{ tag.name }}
            </wa-tag>
        </div>
        <wa-popup
            :active="open"
            distance="4"
            flip
            placement="bottom-start"
            shift
            sync="width"
        >
            <wa-input
                autocomplete="off"
                :label="label"
                :placeholder="t('Search tags', SCOPE)"
                :readonly="disabled || creating"
                ref="input"
                size="s"
                slot="anchor"
                @click="openList"
                @input="onInput"
                @keydown="onInputKeydown"
            >
                <wa-spinner v-if="creating" slot="end"></wa-spinner>
            </wa-input>
            <ul
                :aria-label="label"
                class="tag-picker__list"
                role="listbox"
                @pointerdown="onListPointerdown"
            >
                <li v-for="(option, index) in options" :key="option.key"
                    aria-selected="false"
                    class="tag-picker__option"
                    :class="{ 'tag-picker__option--create': !option.tag }"
                    role="option"
                    tabindex="-1"
                    @click="onOptionClick(option)"
                    @keydown="onOptionKeydown($event, index)"
                >
                    <wa-icon v-if="!option.tag" name="plus"></wa-icon>
                    <span class="tag-picker__name">{{ option.text }}</span>
                    <span v-if="option.tag?.curated" class="tag-picker__curated">{{ t('Curated', SCOPE) }}</span>
                </li>
                <li v-if="!options.length" aria-disabled="true" class="tag-picker__empty" role="option">
                    {{ emptyText }}
                </li>
            </ul>
        </wa-popup>
    </div>
</template>

<style scoped>
.tag-picker {
    display: flex;
    flex-direction: column;
    gap: var(--wa-space-xs);
}

.tag-picker__chips {
    display: flex;
    flex-wrap: wrap;
    gap: var(--wa-space-2xs);
}

.tag-picker__list {
    background: var(--wa-color-surface-raised);
    border: var(--wa-border-width-s) solid var(--wa-color-surface-border);
    border-radius: var(--wa-border-radius-m);
    box-shadow: var(--wa-shadow-m);
    list-style: none;
    margin: 0;
    max-height: 16rem;
    overflow-y: auto;
    padding: var(--wa-space-3xs);
}

.tag-picker__option,
.tag-picker__empty {
    align-items: center;
    border-radius: var(--wa-border-radius-s);
    display: flex;
    font-size: var(--wa-font-size-s);
    gap: var(--wa-space-xs);
    padding: var(--wa-space-2xs) var(--wa-space-xs);
}

.tag-picker__option {
    color: var(--wa-color-text-normal);
    cursor: pointer;
}

.tag-picker__option:hover,
.tag-picker__option:focus {
    background: var(--wa-color-neutral-fill-quiet);
    outline: none;
}

.tag-picker__option:focus-visible {
    outline: var(--wa-focus-ring);
    outline-offset: calc(-1 * var(--wa-focus-ring-width));
}

.tag-picker__option--create {
    color: var(--wa-color-brand-on-quiet);
}

.tag-picker__empty {
    color: var(--wa-color-text-quiet);
}

.tag-picker__name {
    flex: 1;
    min-width: 0;
    overflow-wrap: anywhere;
}

.tag-picker__curated {
    color: var(--wa-color-text-quiet);
    font-size: var(--wa-font-size-xs);
}
</style>
