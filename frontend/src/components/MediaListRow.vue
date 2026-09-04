<script setup lang="ts">
import { computed } from 'vue'
import { t } from '#i18n'

const SCOPE = 'MediaListRow'

const props = defineProps<{
    hash: string
    isFocused: boolean
    isSelected: boolean
    name: string
    /** Lower-case dot-prefixed extension; used to pick the file-type icon. */
    fileExtension: string | null
    /** False when the file's extension is no longer in the project's allowlist. */
    isSupported: boolean
    /** When true, all rows in the list show their checkbox (selection mode is active). */
    selectionActive?: boolean
    /** When true, show the file extension in the listing. Defaults to false. */
    showExtension?: boolean
}>()

/** Display name, with file extension stripped unless showExtension is true. */
const displayName = computed(() =>
    props.showExtension ? props.name : props.name.replace(/\.[^.]+$/, ''),
)

/**
 * Icon shown next to a supported file. Maps the extension to a WebAwesome
 * file-* icon; falls back to a generic "file" for unknown types so a future
 * .docx / .epub never renders blank.
 */
const fileIcon = computed(() => {
    const ext = (props.fileExtension ?? '').toLowerCase()
    if (ext === '.pdf') return 'file-pdf'
    if (ext === '.md') return 'file-lines'
    if (ext === '.htm' || ext === '.html') return 'file-code'
    return 'file'
})

const tooltipLabel = computed(() =>
    props.isSupported
        ? t('Media file', SCOPE)
        : t("This file type isn't supported by the current project.", SCOPE),
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
        if (!props.isSupported) {
            return
        }
        emit('open')
    } else {
        emit('dropdown-action', value)
    }
}

function handleRowClick (event: MouseEvent) {
    emit('row-click', event)
}

function handleRowDoubleClick () {
    if (!props.isSupported) {
        return
    }
    emit('row-dblclick')
}

function handleCheckboxClick () {
    emit('checkbox-click')
}
</script>

<template>
    <div
        class="list-row clickable"
        :class="{
            'list-row--selected': isFocused || isSelected,
            'list-row--unsupported': !isSupported,
        }"
        @click="handleRowClick"
        @dblclick.stop="handleRowDoubleClick"
    >
        <div class="list-row-main">
            <span class="row-lead">
                <input v-if="isFocused || isSelected || selectionActive"
                    :checked="isSelected"
                    class="row-checkbox"
                    :title="t(isSelected ? 'Remove from selection' : 'Add to selection', SCOPE)"
                    type="checkbox"
                    @change.stop
                    @click.stop="handleCheckboxClick"
                />
                <wa-icon v-else
                    class="file-icon"
                    :name="isSupported ? fileIcon : 'lock'"
                    :title="tooltipLabel"
                ></wa-icon>
            </span>
            <span class="list-row-name" :title="!isSupported ? tooltipLabel : undefined">
                {{ displayName }}
            </span>
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
                    <wa-dropdown-item :disabled="!isSupported" value="__open__">
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

/* Greyed text + non-clickable affordance for files the active project can't
   open. Selection / removal still work — only the click-to-open path is
   muted, with the lock icon explaining the state at a glance. */
.list-row--unsupported {
    color: var(--wa-color-text-quiet);
    cursor: default;
}

.list-row--unsupported .list-row-name {
    color: var(--wa-color-text-quiet);
}

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

.file-icon {
    color: var(--wa-color-text-quiet);
    font-size: 1rem;
}
</style>
