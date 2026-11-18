<script lang="ts">
/** One column of a `DataTable`. */
export interface DataTableColumn<Row> {
    /** Names the column, its `cell-<key>` slot and, when sortable, the sort key emitted for it. */
    key: string
    /** The header text. */
    label: string
    /** Whether the header sorts by this column. */
    sortable?: boolean
    /** Right-aligns the column, for numbers. */
    numeric?: boolean
    /** Lets the cells wrap, for long text such as a name; other cells stay on one line and the table scrolls. */
    wrap?: boolean
    /** The cell text when no `cell-<key>` slot is given. */
    format?: (row: Row) => string
}
</script>

<script setup lang="ts" generic="Row">
/**
 * A server-sorted, server-paged table on Web Awesome's native `<table>` styles.
 *
 * The table holds no data of its own: the host fetches a page for the current `sort` and `page` and hands the rows
 * in, so sorting and paging stay on the server, which a release-gated dataset requires. A sortable header emits
 * `update:sort` with the column key, prefixed with `-` when it already sorts ascending by that column; any other
 * header click starts ascending. `aria-sort` names the order on the sorted header. A row activates on click, Enter
 * or Space, and the arrow keys move focus between rows.
 *
 * While the first page loads the body shows skeleton rows. Later loads keep the current rows, dimmed, so the table
 * does not jump while a page changes.
 *
 * @package    epicurrents-platform
 */
import { computed } from 'vue'
import type WaPagination from '@awesome.me/webawesome/dist/components/pagination/pagination.js'

/** Skeleton rows while the first page loads; enough to suggest a table without filling a short screen. */
const SKELETON_ROWS = 5

const props = defineProps<{
    /** The columns in display order. */
    columns: DataTableColumn<Row>[]
    /** Names the table to assistive technology. */
    label: string
    /** Whether a page is being fetched. */
    loading: boolean
    /** The current page, starting at 1. */
    page: number
    /** Rows per page. */
    pageSize: number
    /** A unique key per row. */
    rowKey: (row: Row) => string
    /** The rows of the current page. */
    rows: Row[]
    /** The key of the row to mark as current, such as the one whose details are open. */
    currentKey?: string | null
    /** The current sort key, `-` prefixed for descending. */
    sort: string
    /** The number of rows across every page. */
    total: number
}>()

const emit = defineEmits<{
    activate: [row: Row]
    'update:page': [page: number]
    'update:sort': [sort: string]
}>()

const sortKey = computed(() => props.sort.replace(/^-/, ''))
const descending = computed(() => props.sort.startsWith('-'))
const showSkeleton = computed(() => props.loading && !props.rows.length)

/**
 * The `aria-sort` value of a header: set on the sorted column only.
 * @param column - The header's column.
 */
function ariaSort (column: DataTableColumn<Row>) {
    if (!column.sortable || column.key !== sortKey.value) {
        return undefined
    }
    return descending.value ? 'descending' : 'ascending'
}

/**
 * The icon beside a sortable header: the direction on the sorted column, a neutral mark elsewhere.
 * @param column - The header's column.
 */
function sortIcon (column: DataTableColumn<Row>) {
    if (column.key !== sortKey.value) {
        return 'sort'
    }
    return descending.value ? 'arrow-down' : 'arrow-up'
}

function onSort (column: DataTableColumn<Row>) {
    const next = column.key === sortKey.value && !descending.value ? `-${column.key}` : column.key
    emit('update:sort', next)
}

function cellText (column: DataTableColumn<Row>, row: Row) {
    if (column.format) {
        return column.format(row)
    }
    const value = (row as Record<string, unknown>)[column.key]
    return value === null || value === undefined ? '' : String(value)
}

function activate (row: Row) {
    emit('activate', row)
}

/**
 * Enter and Space activate the focused row; the arrow keys move focus to the row above or below.
 * @param event - The key event on a row.
 * @param row - The row it happened on.
 */
function onRowKeydown (event: KeyboardEvent, row: Row) {
    if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault()
        activate(row)
        return
    }
    if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') {
        return
    }
    const current = event.currentTarget as HTMLElement
    const sibling = event.key === 'ArrowDown' ? current.nextElementSibling : current.previousElementSibling
    if (sibling instanceof HTMLElement) {
        event.preventDefault()
        sibling.focus()
    }
}

function onPageChange (event: Event) {
    const page = (event.target as WaPagination).page
    if (page !== props.page) {
        emit('update:page', page)
    }
}
</script>

<template>
    <div class="data-table">
        <div class="data-table__scroll">
            <table
                :aria-busy="loading"
                :aria-label="label"
                class="wa-hover-rows wa-tabular-nums data-table__table"
                :class="{ 'data-table__table--busy': loading && !showSkeleton }"
            >
                <thead>
                    <tr>
                        <th v-for="column in columns" :key="column.key"
                            :aria-sort="ariaSort(column)"
                            :class="{ 'data-table__numeric': column.numeric }"
                            scope="col"
                        >
                            <button v-if="column.sortable"
                                class="data-table__sort"
                                :class="{ 'data-table__sort--active': column.key === sortKey }"
                                type="button"
                                @click="onSort(column)"
                            >
                                {{ column.label }}
                                <wa-icon class="data-table__sort-icon" :name="sortIcon(column)"></wa-icon>
                            </button>
                            <template v-else>{{ column.label }}</template>
                        </th>
                    </tr>
                </thead>
                <tbody>
                    <template v-if="showSkeleton">
                        <tr v-for="index in SKELETON_ROWS" :key="`skeleton-${index}`" aria-hidden="true">
                            <td v-for="column in columns" :key="column.key">
                                <wa-skeleton effect="sheen"></wa-skeleton>
                            </td>
                        </tr>
                    </template>
                    <tr v-for="row in rows" :key="rowKey(row)"
                        :aria-current="rowKey(row) === currentKey ? 'true' : undefined"
                        class="data-table__row"
                        tabindex="0"
                        @click="activate(row)"
                        @keydown="onRowKeydown($event, row)"
                    >
                        <td v-for="column in columns" :key="column.key"
                            :class="{ 'data-table__numeric': column.numeric, 'data-table__wrap': column.wrap }"
                        >
                            <slot :name="`cell-${column.key}`" :row="row">{{ cellText(column, row) }}</slot>
                        </td>
                    </tr>
                </tbody>
            </table>
        </div>
        <p v-if="!loading && !rows.length" class="empty-state data-table__empty">
            <slot name="empty"></slot>
        </p>
        <wa-pagination
            class="data-table__pagination"
            :disabled="loading"
            hide-single-page
            :label="label"
            :page="page"
            :page-size="pageSize"
            :total="total"
            with-summary
            @wa-page-change="onPageChange"
        ></wa-pagination>
    </div>
</template>

<style scoped>
.data-table {
    display: flex;
    flex-direction: column;
    gap: var(--wa-space-m);
    min-width: 0;
}

.data-table__scroll {
    /* A wide table scrolls inside its own box, never the page. */
    overflow-x: auto;
}

.data-table__table {
    margin: 0;
    transition: opacity 0.15s;
    width: 100%;
}

.data-table__table--busy {
    opacity: 0.6;
}

.data-table__table th,
.data-table__table td {
    white-space: nowrap;
}

.data-table__table td.data-table__wrap {
    min-width: 16ch;
    white-space: normal;
}

.data-table__numeric {
    text-align: end;
}

.data-table__sort {
    align-items: center;
    background: none;
    border: none;
    color: inherit;
    cursor: pointer;
    display: inline-flex;
    font: inherit;
    gap: var(--wa-space-2xs);
    padding: 0;
}

.data-table__sort-icon {
    color: var(--wa-color-text-quiet);
    font-size: 0.9em;
}

.data-table__sort--active .data-table__sort-icon {
    color: var(--wa-color-text-normal);
}

.data-table__sort:focus-visible {
    outline: var(--wa-focus-ring);
    outline-offset: var(--wa-focus-ring-offset);
}

.data-table__row {
    cursor: pointer;
}

.data-table__row:focus-visible {
    outline: var(--wa-focus-ring);
    outline-offset: calc(-1 * var(--wa-focus-ring-width));
}

.data-table__row[aria-current='true'] {
    background-color: var(--wa-color-neutral-fill-quiet);
}

.data-table__empty {
    margin: 0;
}

.data-table__pagination {
    align-self: center;
}
</style>
