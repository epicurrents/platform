<script setup lang="ts">
/**
 * Progress of a recording's post-ingest stages and jobs: "N/X done", and how many failed.
 *
 * Shown while the recording is `processing` or `available`, polling its jobs until it settles. A recording with no
 * jobs shows nothing. When the recording settles while shown, the badge tells the reader with a toast — they were
 * watching it, so the awaited result is a `success`, or a `warning` when a job failed — and emits `settled` so the
 * owner can reload its list.
 */
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { t } from '#i18n'
import { getRecordingJobs, type Recording, type RecordingJobs } from '#api/recordings'
import { showToast } from '#lib/toast'

const SCOPE = 'RecordingJobsBadge'
const POLL_INTERVAL_MS = 3000

const props = defineProps<{
    hash: string
    /** Shown in the toast when the recording settles. */
    name: string
    status: Recording['status']
}>()

const emit = defineEmits<{
    settled: [status: Recording['status']]
}>()

const jobs = ref<RecordingJobs | null>(null)
let timer: ReturnType<typeof setTimeout> | null = null
let stopped = false

const unsettled = (status: Recording['status']) => status === 'processing' || status === 'available'

const label = computed(() => {
    if (!jobs.value) {
        return ''
    }
    return t('{done}/{total} done', SCOPE, { done: jobs.value.done, total: jobs.value.total })
})

async function poll() {
    timer = null
    if (stopped) {
        return
    }
    try {
        const latest = await getRecordingJobs(props.hash)
        const wasUnsettled = jobs.value ? unsettled(jobs.value.status) : unsettled(props.status)
        jobs.value = latest
        if (wasUnsettled && !unsettled(latest.status)) {
            announce(latest)
            emit('settled', latest.status)
            return
        }
    } catch {
        // A transient failure leaves the last count shown and tries again.
    }
    if (!stopped && (!jobs.value || unsettled(jobs.value.status))) {
        timer = setTimeout(poll, POLL_INTERVAL_MS)
    }
}

function announce(latest: RecordingJobs) {
    if (latest.failed) {
        showToast(
            t('"{name}" finished processing; {failed} background jobs failed.', SCOPE, {
                name: props.name,
                failed: latest.failed,
            }),
            'warning',
        )
        return
    }
    showToast(t('"{name}" has finished processing.', SCOPE, { name: props.name }), 'success')
}

function stop() {
    stopped = true
    if (timer) {
        clearTimeout(timer)
        timer = null
    }
}

onMounted(() => {
    if (unsettled(props.status)) {
        poll()
    }
})

watch(() => props.status, status => {
    if (unsettled(status) && !timer && !stopped) {
        poll()
    }
})

onUnmounted(stop)
</script>

<template>
    <wa-badge v-if="jobs && jobs.total && unsettled(jobs.status)" pill variant="neutral">
        {{ label }}
        <template v-if="jobs.failed">
            · {{ t('{failed} failed', SCOPE, { failed: jobs.failed }) }}
        </template>
    </wa-badge>
</template>
