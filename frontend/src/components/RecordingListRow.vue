<script setup lang="ts">
import { computed } from 'vue'
import { t } from '#i18n'
import eegIcon from '../assets/eeg-icon.svg?raw'

const SCOPE = 'RecordingListRow'

const props = defineProps<{
    hash: string
    isFocused: boolean
    isSelected: boolean
    name: string
    /** When true, all rows in the list show their checkbox (selection mode is active). */
    selectionActive?: boolean
    /** When true, show the file extension in the listing. Defaults to false. */
    showExtension?: boolean
}>()

/** Display name, with file extension stripped unless showExtension is true. */
const displayName = computed(() =>
    props.showExtension ? props.name : props.name.replace(/\.[^.]+$/, ''),
)

const emit = defineEmits<{
    'checkbox-click': []
    'dropdown-action': [value: string]
    'open': []
    'row-click': [event: MouseEvent]
    'row-dblclick': []
}>()

function handleDropdownSelect (event: Event) {
    const value = (event as CustomEvent<{ item: { value: string } }>).detail.item.value
    if (value === '__open__') {
        emit('open')
    } else {
        emit('dropdown-action', value)
    }
}

function handleRowClick (event: MouseEvent) {
    emit('row-click', event)
}

function handleRowDoubleClick () {
    emit('row-dblclick')
}

function handleCheckboxClick () {
    emit('checkbox-click')
}
</script>

<template>
    <div
        class="list-row clickable"
        :class="{ 'list-row--selected': isFocused || isSelected }"
        @click="handleRowClick"
        @dblclick.stop="handleRowDoubleClick"
    >
        <div class="list-row-main">
            <!-- Fixed-width lead: keeps name aligned whether showing checkbox or icon -->
            <span class="row-lead">
                <input v-if="isFocused || isSelected || selectionActive"
                    :checked="isSelected"
                    class="row-checkbox"
                    :title="t(isSelected ? 'Remove from selection' : 'Add to selection', 'RecordingListRow')"
                    type="checkbox"
                    @change.stop
                    @click.stop="handleCheckboxClick"
                />
                <!-- EEG brain icon -->
                <span v-else
                    class="recording-icon"
                    :title="t('EEG recording', 'RecordingListRow')"
                    v-html="eegIcon"
                />
            </span>
            <span class="list-row-name">{{ displayName }}</span>
            <slot name="meta"></slot>
            <div class="list-row-actions">
                <wa-dropdown
                    placement="bottom-end"
                    @click.stop
                    @wa-select.stop="handleDropdownSelect"
                >
                    <wa-button
                        appearance="plain"
                        size="s"
                        slot="trigger"
                        variant="text"
                    >
                        <wa-icon name="ellipsis"></wa-icon>
                    </wa-button>
                    <wa-dropdown-item value="__open__">
                        <wa-icon slot="icon" name="arrow-up-right-from-square"></wa-icon>
                        {{ t('Open in viewer', SCOPE) }}
                    </wa-dropdown-item>
                    <slot name="actions"></slot>
                </wa-dropdown>
            </div>
        </div>
    </div>
</template>

<style scoped>
/* The component's rows carry border-radius (from .list-row.clickable), so the
   global border-bottom would curve at both ends. Override it with a position-
   relative + ::after pseudo-element that is inset by the border-radius so the
   line stops before the corners curve. */
.list-row {
    border-bottom: none;
    position: relative;
}

.list-row::after {
    border-bottom: var(--wa-border-width-s) solid var(--wa-color-neutral-border-quiet);
    bottom: 0;
    content: '';
    left: var(--wa-border-radius-m);
    position: absolute;
    right: var(--wa-border-radius-m);
}

.list-row:last-child::after {
    display: none;
}

.list-row--selected {
    background-color: var(--wa-color-brand-fill-quiet) !important;
}

/* Fixed-width container ensures the name column stays aligned regardless of
   whether a checkbox or icon is showing. */
.row-lead {
    align-items: center;
    display: flex;
    flex-shrink: 0;
    height: 1.5rem;
    justify-content: center;
    width: 1.5rem;
}

.row-checkbox {
    accent-color: var(--wa-color-brand-500);
    cursor: pointer;
    height: 1rem;
    margin: 0.25rem 0;
    width: 1rem;
}

.recording-icon {
    color: var(--wa-color-text-quiet);
    display: contents;
}

.recording-icon svg {
    height: 1rem;
    width: 1rem;
}
</style>
