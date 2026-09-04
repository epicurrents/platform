<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { t } from '#i18n'
import NeedsAttentionRow from '#components/NeedsAttentionRow.vue'
import { deleteRecording, listRecordings, recordingName, type Recording } from '#api/recordings'
import { showToast } from '#lib/toast'

const SCOPE = 'NeedsAttentionView'
const router = useRouter()

/** Page size for the offset-based "Load more" pagination. */
const PAGE_SIZE = 50

const recordings = ref<Recording[]>([])
const offset = ref(0)
const hasMore = ref(true)
const loading = ref(true)
const loadingMore = ref(false)
const error = ref<string | null>(null)

/**
 * Fetch the next page of failed recordings, or restart from the top when `reset`
 * is true. The list endpoint returns no total, so a full page means more may
 * remain — that drives the "Load more" affordance.
 */
async function loadPage (reset = false) {
    if (reset) {
        recordings.value = []
        offset.value = 0
        hasMore.value = true
    }
    if (offset.value === 0) {
        loading.value = true
    } else {
        loadingMore.value = true
    }
    error.value = null
    try {
        const page = await listRecordings(PAGE_SIZE, offset.value, { status: 'failed' })
        recordings.value.push(...page)
        offset.value += page.length
        hasMore.value = page.length === PAGE_SIZE
    } catch {
        error.value = t('Failed to load recordings. Please try again.', SCOPE)
    } finally {
        loading.value = false
        loadingMore.value = false
    }
}

onMounted(() => loadPage(true))

function goToLibrary () {
    router.push({ name: 'library' })
}

function reupload () {
    router.push({ name: 'upload' })
}

// ── Delete ──────────────────────────────────────────────────────────────────

const deletingRec = ref<Recording | null>(null)
const deleteLoading = ref(false)

function openDelete (rec: Recording) {
    deletingRec.value = rec
}

function closeDelete () {
    deletingRec.value = null
}

async function confirmDelete () {
    if (!deletingRec.value) {
        return
    }
    deleteLoading.value = true
    try {
        await deleteRecording(deletingRec.value.hash)
        recordings.value = recordings.value.filter(r => r.hash !== deletingRec.value!.hash)
        showToast(t('"{name}" moved to trash.', SCOPE, { name: recordingName(deletingRec.value) }), 'neutral')
        closeDelete()
    } catch {
        showToast(t('Failed to delete recording. Please try again.', SCOPE), 'danger')
    } finally {
        deleteLoading.value = false
    }
}
</script>

<template>
    <main class="page-view">
        <header class="page-header">
            <div class="header-titles">
                <wa-button
                    appearance="plain"
                    size="s"
                    @click="goToLibrary"
                >
                    <wa-icon name="arrow-left" slot="start"></wa-icon>
                    {{ t('Library', SCOPE) }}
                </wa-button>
                <h1>{{ t('Needs attention', SCOPE) }}</h1>
            </div>
        </header>

        <wa-callout variant="neutral">
            {{ t('Recordings that failed processing. Review the error, then re-upload or delete each one.', SCOPE) }}
        </wa-callout>

        <wa-spinner v-if="loading" class="loading-center"></wa-spinner>

        <wa-callout v-else-if="error" variant="danger">{{ error }}</wa-callout>

        <p v-else-if="!recordings.length" class="empty-state">
            {{ t('Nothing needs attention. All your recordings processed successfully.', SCOPE) }}
        </p>

        <template v-else>
            <div class="attention-rows">
                <NeedsAttentionRow
                    v-for="rec in recordings"
                    :key="rec.hash"
                    :recording="rec"
                    @delete="openDelete(rec)"
                    @reupload="reupload"
                ></NeedsAttentionRow>
            </div>

            <div v-if="hasMore" class="load-more">
                <wa-button
                    appearance="plain"
                    :loading="loadingMore"
                    @click="loadPage(false)"
                >
                    {{ t('Load more', SCOPE) }}
                </wa-button>
            </div>
        </template>
    </main>

    <wa-dialog
        :label="t('Move to trash', SCOPE)"
        :open="!!deletingRec"
        @wa-hide.self="closeDelete"
    >
        <p class="dialog-text">
            {{ t('Move "{name}" to trash?', SCOPE, { name: deletingRec ? recordingName(deletingRec) : '' }) }}
        </p>
        <div slot="footer" class="form-actions">
            <wa-button
                appearance="filled-outlined"
                :disabled="deleteLoading"
                variant="neutral"
                @click="closeDelete"
            >
                {{ t('Cancel', SCOPE) }}
            </wa-button>
            <wa-button
                appearance="filled-outlined"
                :loading="deleteLoading"
                variant="danger"
                @click="confirmDelete"
            >
                {{ t('Move to trash', SCOPE) }}
            </wa-button>
        </div>
    </wa-dialog>
</template>

<style scoped>
.header-titles {
    /* flex-start so the back button keeps its own narrow width instead of
       stretching to the title's width below it. */
    align-items: flex-start;
    display: flex;
    flex-direction: column;
    gap: 0.25rem;
}

.attention-rows {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
}

.load-more {
    display: flex;
    justify-content: center;
    padding: 0.5rem 0 1rem;
}
</style>
