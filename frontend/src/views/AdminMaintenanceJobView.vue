<script setup lang="ts">
/**
 * One maintenance job: its timeline, its output or log, and the actions its state allows.
 *
 * Polls while the job is in flight, backing off as nothing changes. The
 * confirmation window counts down from the server's deadline against the
 * server's clock, so a browser whose clock is off still shows the right
 * remaining time. Confirm is the page's primary action; Roll back is a
 * danger action behind a dialog that says what is lost.
 *
 * @package    epicurrents-platform
 */
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import JobStateBadge from '#components/JobStateBadge.vue'
import StepUpFields from '#components/StepUpFields.vue'
import {
    cancelJob,
    fetchJob,
    fetchJobLog,
    rollbackJob,
    verifyJob,
    type JobLog,
    type MaintenanceJob,
} from '#api/maintenance'
import { usePolling } from '#composables/usePolling'
import { t } from '#i18n'
import { formatDateTime } from '#lib/datetime'
import { errorDetail } from '#lib/http'
import { showToast } from '#lib/toast'
import { setPageTitle } from '#router'
import { useAuthStore } from '#stores/auth'
import { useMaintenanceStore } from '#stores/maintenance'

const SCOPE = 'AdminMaintenanceJobView'

const authStore = useAuthStore()
const maintenanceStore = useMaintenanceStore()
const route = useRoute()
const router = useRouter()

const jobId = String(route.params.id)

const job = ref<MaintenanceJob | null>(null)
const log = ref<JobLog | null>(null)
const loading = ref(true)
const loadError = ref('')

const canWrite = computed(() => authStore.isSuperuser)
const stepUp = computed(() => maintenanceStore.status?.step_up ?? { method: null, available: false, reason: null })

const showVerify = ref(false)
const showRollback = ref(false)
const showCancel = ref(false)
const acting = ref(false)
const actionError = ref('')
const credentials = reactive({ password: '', totp_code: '' })

/** Server clock minus browser clock, in milliseconds, from the last status read. */
const clockOffset = ref(0)
const now = ref(Date.now())
let clockTimer: ReturnType<typeof setInterval> | undefined

/** The one operation with a verification window; the confirm and roll-back actions belong to it alone. */
const UPDATE_OPERATION = 'platform.update'

const canCancel = computed(() => job.value?.state === 'requested')
const isUpdate = computed(() => job.value?.operation === UPDATE_OPERATION)
const canVerify = computed(() => isUpdate.value && job.value?.state === 'awaiting_verification')
const canRollback = computed(() => {
    if (!isUpdate.value || !job.value) {
        return false
    }
    return job.value.state === 'awaiting_verification' || (job.value.state === 'succeeded' && job.value.snapshot !== '')
})
/** An update that applied no migration is rolled back with the database kept; the dialog says which it is. */
const keepsDatabase = computed(() => job.value?.migrations_applied === false)

/** Seconds left in the confirmation window, or null when there is none. */
const secondsLeft = computed(() => {
    if (!job.value?.verify_deadline || job.value.state !== 'awaiting_verification') {
        return null
    }
    const deadline = Date.parse(job.value.verify_deadline)
    return Math.max(0, Math.round((deadline - (now.value + clockOffset.value)) / 1000))
})

const countdown = computed(() => {
    const seconds = secondsLeft.value
    if (seconds === null) {
        return ''
    }
    const minutes = Math.floor(seconds / 60)
    const rest = seconds % 60
    return `${minutes}:${String(rest).padStart(2, '0')}`
})

const facts = computed(() => {
    const current = job.value
    if (!current) {
        return []
    }
    const rows: { label: string, value: string }[] = [
        { label: t('Operation', SCOPE), value: current.operation },
        { label: t('Executor', SCOPE), value: current.executor === 'host' ? t('Host agent', SCOPE) : t('Worker', SCOPE) },
        { label: t('Requested by', SCOPE), value: current.requested_by ?? t('Unknown', SCOPE) },
        { label: t('Requested', SCOPE), value: formatDateTime(current.created_at) },
    ]
    if (current.started_at) {
        rows.push({ label: t('Started', SCOPE), value: formatDateTime(current.started_at) })
    }
    if (current.finished_at) {
        rows.push({ label: t('Finished', SCOPE), value: formatDateTime(current.finished_at) })
    }
    if (current.step) {
        rows.push({ label: t('Step', SCOPE), value: current.step })
    }
    if (current.reason) {
        rows.push({ label: t('Reason', SCOPE), value: current.reason })
    }
    if (current.target_version) {
        rows.push({ label: t('Target version', SCOPE), value: current.target_version })
    }
    if (current.installed_version_before) {
        rows.push({ label: t('Version before', SCOPE), value: current.installed_version_before })
    }
    if (current.running_version) {
        rows.push({ label: t('Running version', SCOPE), value: current.running_version })
    }
    if (current.snapshot) {
        rows.push({ label: t('Snapshot', SCOPE), value: current.snapshot })
    }
    if (current.post_snapshot) {
        rows.push({
            label: current.operation === UPDATE_OPERATION ? t('Post-update snapshot', SCOPE) : t('Safety snapshot', SCOPE),
            value: current.post_snapshot,
        })
    }
    if (current.operation === UPDATE_OPERATION && current.migrations_applied !== null) {
        rows.push({
            label: t('Database', SCOPE),
            value: current.migrations_applied
                ? t('Migrated by this update; a rollback restores it', SCOPE)
                : t('Unchanged by this update; a rollback keeps it', SCOPE),
        })
    }
    if (Object.keys(current.args).length) {
        rows.push({ label: t('Arguments', SCOPE), value: JSON.stringify(current.args) })
    }
    return rows
})

const outputText = computed(() => {
    if (log.value) {
        return log.value.log
    }
    return job.value?.output ?? ''
})

function fingerprint (current: MaintenanceJob | null) {
    return current ? `${current.state}:${current.step}:${current.finished_at ?? ''}:${current.verify_deadline ?? ''}` : ''
}

async function loadJob (): Promise<boolean> {
    const fresh = await fetchJob(jobId)
    const changed = fingerprint(fresh) !== fingerprint(job.value)
    job.value = fresh
    setPageTitle(fresh.operation)
    if (canWrite.value) {
        // The log grows while the job runs; a read per poll keeps it current.
        try {
            log.value = await fetchJobLog(jobId)
        } catch {
            log.value = null
        }
    }
    if (changed) {
        const status = await maintenanceStore.refreshStatus()
        if (status) {
            clockOffset.value = Date.parse(status.server_now) - Date.now()
        }
    }
    if (!fresh.in_flight) {
        poll.stop()
    }
    return changed
}

const poll = usePolling(loadJob, { immediate: false })

async function load () {
    loading.value = true
    loadError.value = ''
    try {
        await loadJob()
        if (job.value?.in_flight) {
            poll.start()
        }
    } catch (err) {
        loadError.value = errorDetail(err, t('The job could not be loaded.', SCOPE))
    } finally {
        loading.value = false
    }
}

function goBack () {
    router.push({ name: 'admin-maintenance' })
}

function openDialog (which: 'verify' | 'rollback' | 'cancel') {
    actionError.value = ''
    credentials.password = ''
    credentials.totp_code = ''
    showVerify.value = which === 'verify'
    showRollback.value = which === 'rollback'
    showCancel.value = which === 'cancel'
}

function closeDialogs () {
    if (acting.value) {
        return
    }
    showVerify.value = false
    showRollback.value = false
    showCancel.value = false
}

async function act (perform: () => Promise<MaintenanceJob>, done: string) {
    actionError.value = ''
    acting.value = true
    try {
        job.value = await perform()
        closeDialogs()
        showToast(done, 'neutral')
        if (job.value.in_flight) {
            poll.start()
        }
        await poll.refresh()
    } catch (err) {
        actionError.value = errorDetail(err, t('The request was refused.', SCOPE))
    } finally {
        acting.value = false
    }
}

function confirmVerify () {
    act(
        () => verifyJob(jobId, { password: credentials.password || undefined }),
        t('Confirmation sent. The agent completes the update on its next tick.', SCOPE),
    )
}

function confirmRollback () {
    act(
        () => rollbackJob(jobId, { password: credentials.password || undefined, totp_code: credentials.totp_code || undefined }),
        t('Rollback requested. The agent restores the previous release on its next tick.', SCOPE),
    )
}

function confirmCancel () {
    act(() => cancelJob(jobId), t('Job cancelled.', SCOPE))
}

onMounted(() => {
    load()
    clockTimer = setInterval(() => {
        now.value = Date.now()
    }, 1000)
})

onBeforeUnmount(() => {
    clearInterval(clockTimer)
})
</script>

<template>
    <main class="page-view">
        <header class="page-header">
            <div class="page-header-start">
                <wa-button appearance="plain" size="s" @click="goBack">
                    <wa-icon name="arrow-left" slot="start"></wa-icon>
                    {{ t('Maintenance', SCOPE) }}
                </wa-button>
            </div>
        </header>

        <wa-spinner v-if="loading" class="loading-center"></wa-spinner>

        <wa-callout v-else-if="loadError" variant="danger">
            {{ loadError }}
        </wa-callout>

        <template v-else-if="job">
            <header class="page-header">
                <h1>{{ job.operation }}</h1>
                <div class="job-actions">
                    <wa-button v-if="canWrite && canCancel"
                        appearance="plain"
                        size="s"
                        @click="openDialog('cancel')"
                    >
                        <wa-icon name="ban" slot="start"></wa-icon>
                        {{ t('Cancel request', SCOPE) }}
                    </wa-button>
                    <wa-button v-if="canWrite && canRollback"
                        appearance="plain"
                        size="s"
                        variant="danger"
                        @click="openDialog('rollback')"
                    >
                        <wa-icon name="rotate-left" slot="start"></wa-icon>
                        {{ t('Roll back', SCOPE) }}
                    </wa-button>
                    <wa-button v-if="canWrite && canVerify"
                        appearance="filled-outlined"
                        size="s"
                        variant="brand"
                        @click="openDialog('verify')"
                    >
                        <wa-icon name="check" slot="start"></wa-icon>
                        {{ t('Confirm update', SCOPE) }}
                    </wa-button>
                </div>
            </header>

            <div class="row-badges job-state">
                <JobStateBadge :state="job.state" />
                <wa-badge v-if="job.in_flight" appearance="outlined" variant="neutral">
                    {{ t('Live', SCOPE) }}
                </wa-badge>
            </div>

            <wa-callout v-if="job.state === 'awaiting_verification'" variant="warning">
                <strong>{{ t('The update is running and waits for your confirmation.', SCOPE) }}</strong>
                {{ t('Check that the platform works, then confirm. Without a confirmation it is rolled back when the window closes.', SCOPE) }}
                <span v-if="countdown"> {{ t('Time left: {countdown}', SCOPE, { countdown }) }}</span>
            </wa-callout>

            <wa-callout v-else-if="job.state === 'rollback_failed'" variant="danger">
                {{ t('The rollback failed. The deployment needs an operator with shell access.', SCOPE) }}
            </wa-callout>

            <wa-callout v-else-if="job.state === 'failed' && job.reason === 'orphaned'" variant="warning">
                {{ t('The agent left no record of this job. It was marked failed; nothing was changed.', SCOPE) }}
            </wa-callout>

            <dl class="job-facts">
                <template v-for="fact in facts" :key="fact.label">
                    <dt>{{ fact.label }}</dt>
                    <dd>{{ fact.value }}</dd>
                </template>
            </dl>

            <section v-if="canWrite" class="job-section">
                <div class="section-header">
                    <h2>{{ job.executor === 'host' ? t('Agent log', SCOPE) : t('Output', SCOPE) }}</h2>
                </div>
                <p v-if="!outputText" class="job-hint">
                    {{ job.in_flight ? t('Nothing yet.', SCOPE) : t('The job produced no output.', SCOPE) }}
                </p>
                <template v-else>
                    <p v-if="log?.truncated" class="job-hint">
                        {{ t('Showing the end of the log.', SCOPE) }}
                    </p>
                    <pre class="job-output">{{ outputText }}</pre>
                </template>
            </section>
        </template>
    </main>

    <wa-dialog :label="t('Confirm update', SCOPE)" :open="showVerify" @wa-hide.self="closeDialogs">
        <div class="job-form">
            <wa-callout v-if="actionError" variant="danger">
                {{ actionError }}
            </wa-callout>
            <p class="dialog-text">
                {{ t('Confirming keeps the new release. The pre-update snapshot stays available for a later rollback for as long as snapshots are kept.', SCOPE) }}
            </p>
            <StepUpFields :credentials="credentials" password-only :step-up="stepUp" />
        </div>
        <div slot="footer" class="form-actions">
            <wa-button
                appearance="filled-outlined"
                :disabled="acting"
                variant="neutral"
                @click="closeDialogs"
            >
                {{ t('Cancel', SCOPE) }}
            </wa-button>
            <wa-button
                appearance="filled-outlined"
                :loading="acting"
                variant="brand"
                @click="confirmVerify"
            >
                {{ t('Confirm update', SCOPE) }}
            </wa-button>
        </div>
    </wa-dialog>

    <wa-dialog :label="t('Roll back', SCOPE)" :open="showRollback" @wa-hide.self="closeDialogs">
        <div class="job-form">
            <wa-callout v-if="actionError" variant="danger">
                {{ actionError }}
            </wa-callout>
            <wa-callout v-if="keepsDatabase" variant="warning">
                {{ t('This update applied no migration, so rolling back restores the previous code and keeps the database; nothing written since the update is lost. The platform is unavailable while the previous release is rebuilt.', SCOPE) }}
            </wa-callout>
            <wa-callout v-else variant="danger">
                {{ t('Rolling back restores the database as it was before the update. Everything written since then is lost, including your own changes. The platform is unavailable while the previous release is rebuilt.', SCOPE) }}
            </wa-callout>
            <StepUpFields :credentials="credentials" :step-up="stepUp" />
        </div>
        <div slot="footer" class="form-actions">
            <wa-button
                appearance="filled-outlined"
                :disabled="acting"
                variant="neutral"
                @click="closeDialogs"
            >
                {{ t('Cancel', SCOPE) }}
            </wa-button>
            <wa-button
                appearance="filled-outlined"
                :loading="acting"
                variant="danger"
                @click="confirmRollback"
            >
                {{ t('Roll back', SCOPE) }}
            </wa-button>
        </div>
    </wa-dialog>

    <wa-dialog :label="t('Cancel request', SCOPE)" :open="showCancel" @wa-hide.self="closeDialogs">
        <div class="job-form">
            <wa-callout v-if="actionError" variant="danger">
                {{ actionError }}
            </wa-callout>
            <p class="dialog-text">
                {{ t('Withdraw this request? It has not been picked up yet, so nothing has run.', SCOPE) }}
            </p>
        </div>
        <div slot="footer" class="form-actions">
            <wa-button
                appearance="filled-outlined"
                :disabled="acting"
                variant="neutral"
                @click="closeDialogs"
            >
                {{ t('Keep it', SCOPE) }}
            </wa-button>
            <wa-button
                appearance="filled-outlined"
                :loading="acting"
                variant="danger"
                @click="confirmCancel"
            >
                {{ t('Cancel request', SCOPE) }}
            </wa-button>
        </div>
    </wa-dialog>
</template>

<style scoped>
.job-actions {
    display: flex;
    flex-wrap: wrap;
    gap: var(--wa-space-xs);
}

.job-state {
    margin-bottom: var(--wa-space-m);
}

.job-section {
    margin-bottom: var(--wa-space-xl);
}

.job-form {
    display: flex;
    flex-direction: column;
    gap: var(--wa-space-s);
}

.job-hint {
    color: var(--wa-color-text-quiet);
    font-size: var(--wa-font-size-s);
    margin: 0 0 var(--wa-space-s);
}

.job-facts {
    display: grid;
    grid-template-columns: max-content 1fr;
    align-items: center;
    gap: var(--wa-space-2xs) var(--wa-space-m);
    margin: 0 0 var(--wa-space-l);
}

.job-facts dt {
    color: var(--wa-color-text-quiet);
    font-size: var(--wa-font-size-s);
}

.job-facts dd {
    margin: 0;
    overflow-wrap: anywhere;
}

/* A log is read top to bottom and may be wide; it scrolls inside its box rather than the page. */
.job-output {
    background: var(--wa-color-surface-lowered);
    border: 1px solid var(--wa-color-surface-border);
    border-radius: var(--wa-border-radius-m);
    font-size: var(--wa-font-size-xs);
    margin: 0;
    max-height: 60vh;
    overflow: auto;
    padding: var(--wa-space-s);
    white-space: pre-wrap;
}

.dialog-text {
    margin: 0;
}
</style>
