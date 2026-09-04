<script setup lang="ts">
/**
 * FileTreeItem — renders a FolderNode as a wa-tree-item with its files and
 * sub-folders nested in the `children` slot, recursively.
 *
 * When `filePhases` is empty (the default) the component is in preview mode
 * and shows plain file-music icons. When `filePhases` has entries (during
 * or after upload) it shows phase icons with colours and a spin animation
 * for in-progress files.
 *
 * Preview-mode extras:
 *  - Folder header shows direct file/subfolder counts as compact icons.
 *  - Files are paginated at PAGE_SIZE; a prev/next row appears below them.
 *  - If a folder has more than FOLDER_COLLAPSE_THRESHOLD subfolders, the
 *    excess are hidden behind a "N more folders…" toggle.
 */
import { ref, computed } from 'vue'
import { t } from '#i18n'
import type { FolderNode, FileLeaf } from '#composables/useFileTree'
import FileTreeItem from './FileTreeItem.vue'

const SCOPE = 'FileTreeItem'

const PAGE_SIZE = 50
const FOLDER_COLLAPSE_THRESHOLD = 20

interface Props {
    folder: FolderNode
    /** key → phase string; empty map = preview mode (no phase icons). */
    filePhases?: Map<string, string>
}

const props = withDefaults(defineProps<Props>(), {
    filePhases: () => new Map(),
})

// Preview mode = no upload phases present yet.
const isPreview = computed(() => props.filePhases.size === 0)

// ---------------------------------------------------------------------------
// File pagination (preview only)
// ---------------------------------------------------------------------------

const filePage = ref(0)
const totalFilePages = computed(() => Math.ceil(props.folder.files.length / PAGE_SIZE))

const visibleFiles = computed((): FileLeaf[] => {
    if (!isPreview.value || props.folder.files.length <= PAGE_SIZE) {
        return props.folder.files
    }
    const start = filePage.value * PAGE_SIZE
    return props.folder.files.slice(start, start + PAGE_SIZE)
})

const showFilePagination = computed(() =>
    isPreview.value && props.folder.files.length > PAGE_SIZE,
)

// ---------------------------------------------------------------------------
// Subfolder collapse (preview only)
// ---------------------------------------------------------------------------

const showAllFolders = ref(false)

const visibleFolders = computed(() => {
    if (
        !isPreview.value ||
        showAllFolders.value ||
        props.folder.subfolders.length <= FOLDER_COLLAPSE_THRESHOLD
    ) {
        return props.folder.subfolders
    }
    return props.folder.subfolders.slice(0, FOLDER_COLLAPSE_THRESHOLD)
})

const hiddenFolderCount = computed(() =>
    props.folder.subfolders.length - FOLDER_COLLAPSE_THRESHOLD,
)

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function fileKey (leaf: FileLeaf) {
    return leaf.relativePath || leaf.file.name
}

function phaseIcon (phase: string | undefined) {
    switch (phase) {
        case 'ready':
            return 'circle-check'
        case 'failed':
        case 'timeout':
            return 'triangle-exclamation'
        case 'queued':
            return 'clock'
        default:
            return 'spinner'
    }
}

function phaseIconClass (phase: string | undefined) {
    if (phase === 'ready') {
        return 'phase-icon--ready'
    }
    if (phase === 'failed' || phase === 'timeout') {
        return 'phase-icon--error'
    }
    if (phase === 'queued') {
        return 'phase-icon--queued'
    }
    if (phase === 'uploading' || phase === 'processing') {
        return 'phase-icon--active phase-icon--spinning'
    }
    return 'phase-icon--active'
}

function prevFilePage () {
    filePage.value--
}

function nextFilePage () {
    filePage.value++
}

function showAllSubfolders () {
    showAllFolders.value = true
}
</script>

<template>
    <wa-tree-item class="item" expanded>
        <wa-icon name="folder" class="icon-folder"></wa-icon>
        {{ folder.name }}
        <!-- Compact file / subfolder counts -->
        <span class="folder-meta">
            <wa-icon name="file-music" class="folder-meta__icon"></wa-icon>
            {{ folder.files.length }}
            <wa-icon name="folder" class="folder-meta__icon folder-meta__icon--folder"></wa-icon>
            {{ folder.subfolders.length }}
        </span>

        <!-- Files (paginated in preview, all shown during/after upload) -->
        <wa-tree-item v-for="leaf in visibleFiles" :key="fileKey(leaf)"
            class="item"
            slot="children"
        >
            <div class="file-row">
                <wa-icon
                    v-if="filePhases.size > 0"
                    :class="phaseIconClass(filePhases.get(fileKey(leaf)))"
                    :name="phaseIcon(filePhases.get(fileKey(leaf)))"
                ></wa-icon>
                <wa-icon v-else name="file-music" class="icon-file"></wa-icon>
                <span class="file-row__name">{{ leaf.file.name }}</span>
                <wa-format-bytes class="file-row__size" :value="leaf.file.size"></wa-format-bytes>
            </div>
        </wa-tree-item>

        <!-- File pagination row (preview only) -->
        <wa-tree-item v-if="showFilePagination" slot="children" class="pagination-row">
            <span class="pagination-controls">
                <button
                    class="page-btn"
                    :disabled="filePage === 0"
                    @click.stop="prevFilePage"
                >‹</button>
                <span class="page-label">{{ filePage + 1 }}&thinsp;/&thinsp;{{ totalFilePages }}</span>
                <button
                    class="page-btn"
                    :disabled="filePage >= totalFilePages - 1"
                    @click.stop="nextFilePage"
                >›</button>
            </span>
        </wa-tree-item>

        <!-- Sub-folders (collapsed beyond threshold in preview) -->
        <file-tree-item
            v-for="sub in visibleFolders"
            :key="sub.name"
            :folder="sub"
            :file-phases="filePhases"
            slot="children"
        />

        <!-- "N more folders…" toggle (preview only) -->
        <wa-tree-item
            v-if="isPreview && !showAllFolders && folder.subfolders.length > FOLDER_COLLAPSE_THRESHOLD"
            slot="children"
        >
            <span class="show-more-btn" @click.stop="showAllSubfolders">
                <wa-icon name="folder" class="icon-folder"></wa-icon>
                {{
                    t(
                        '{count} more {kind}...',
                        SCOPE,
                        {
                            count: hiddenFolderCount,
                            kind: hiddenFolderCount === 1 ? t('folder', SCOPE) : t('folders', SCOPE),
                        },
                    )
                }}
            </span>
        </wa-tree-item>
    </wa-tree-item>
</template>

<style scoped>
.item::part(label) {
    flex: 1;
}

.icon-folder {
    color: var(--wa-color-warning-fill-loud);
    margin-inline-end: 0.5em;
}

.icon-file {
    color: var(--wa-color-text-quiet);
}

/* Queued phase: gray static clock. Matches UploadView. */
.phase-icon--queued {
    color: var(--wa-color-text-quiet);
}

/* Unknown-phase fallback: gray static glyph. Matches UploadView. */
.phase-icon--active {
    color: var(--wa-color-text-quiet);
}

/* Active phase (uploading / processing): blue spinning glyph. */
.phase-icon--active.phase-icon--spinning {
    color: var(--wa-color-brand-fill-loud);
}

.phase-icon--error {
    color: var(--wa-color-danger-fill-loud);
}

.phase-icon--ready {
    color: var(--wa-color-success-fill-loud);
}

.phase-icon--spinning {
    animation: spin 1s linear infinite;
}

/* File row layout — icon on the left, filename takes the remaining space,
   size pushed to the right edge. Matches the root-file rows in UploadView. */
.file-row {
    align-items: center;
    display: flex;
    gap: 0.5em;
    justify-content: space-evenly;
    width: 100%;
}

.file-row__name {
    flex: 1;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}

.file-row__size {
    color: var(--wa-color-text-quiet);
    flex-shrink: 0;
    font-size: 0.85em;
}

/* Folder header: compact file/subfolder counts */
.folder-meta {
    display: inline-flex;
    align-items: center;
    gap: 0.2em;
    margin-left: 0.6em;
    color: var(--wa-color-neutral-400);
    font-size: 0.8em;
}

.folder-meta__icon {
    font-size: 0.9em;
    color: var(--wa-color-neutral-on-quiet);
}

.folder-meta__icon--folder {
    margin-left: 0.4em;
    color: var(--wa-color-warning-400);
}

/* Pagination row */
.pagination-controls {
    display: inline-flex;
    align-items: center;
    gap: 0.35rem;
    color: var(--wa-color-neutral-500);
    font-size: 0.8em;
}

.page-btn {
    all: unset;
    cursor: pointer;
    padding: 0 0.3rem;
    border-radius: var(--wa-border-radius-s);
    line-height: 1.5;
    color: var(--wa-color-neutral-600);
}

.page-btn:hover:not(:disabled) {
    background: var(--wa-color-neutral-100);
    color: var(--wa-color-neutral-900);
}

.page-btn:disabled {
    opacity: 0.35;
    cursor: default;
}

.page-label {
    min-width: 3em;
    text-align: center;
}

/* Show more folders toggle */
.show-more-btn {
    display: inline-flex;
    align-items: center;
    gap: 0.4em;
    cursor: pointer;
    color: var(--wa-color-neutral-500);
    font-size: 0.85em;
    font-style: italic;
}

.show-more-btn:hover {
    color: var(--wa-color-neutral-700);
}

@keyframes spin {
    to { transform: rotate(360deg); }
}
</style>
