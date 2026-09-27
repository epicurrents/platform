<script setup lang="ts">
/**
 * One maintenance job: its timeline, its output or log, and the actions its state allows.
 *
 * Polls while the job is in flight, and while a confirmation or rollback it
 * asked for waits for the agent to act on it, backing off as nothing
 * changes. The confirmation window counts down from the server's deadline
 * against the server's clock, so a browser whose clock is off still shows the
 * right remaining time. Confirm is the page's primary action; Roll back is a
 * danger action behind a dialog that says what is lost, and asks for an
 * explicit acknowledgement when the restore brings back erased accounts.
 * "Mark as failed" frees the one-job slot from a job whose executor is gone.
 *
 * @package    epicurrents-platform
 */
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import ErasureAcknowledgement from '#components/ErasureAcknowledgement.vue'
import JobStateBadge from '#components/JobStateBadge.vue'
import StepUpFields from '#components/StepUpFields.vue'
import {
    abandonJob,
    cancelJob,
    erasureConflict,
    fetchJob,
    fetchJobLog,
    rollbackJob,
    verifyJob,
    type JobLog,
    type MaintenanceJob,
} from '#api/maintenance'
import { useDialog } from '#composables/useDialog'
import { usePolling } from '#composables/usePolling'
import { t } from '#i18n'
import { formatDateTime } from '#lib/datetime'
import { errorDetail } from '#lib/http'
import { jobReasonLabel } from '#lib/maintenanceLabels'
import { stepUpBody } from '#lib/stepUp'
import { showToast } from '#lib/toast'
import { setPageTitle } from '#router'
import { useAuthStore } from '#stores/auth'
import { useMaintenanceStore } from '#stores/maintenance'

const SCOPE = 'AdminMaintenanceJobView'

const authStore = useAuthStore()
const maintenanceStore = useMaintenanceStore()
const route = useRoute()
const router = useRouter()

/** Follows the route, so moving from one job to another reuses the page with the right id. */
const jobId = computed(() => String(route.params.id))

const job = ref<MaintenanceJob | null>(null)
const log = ref<JobLog | null>(null)
const loading = ref(true)
const loadError = ref('')

const canWrite = computed(() => authStore.isSuperuser)
/** The status says how this account confirms; before it has loaded, the account itself says the same. */
const stepUp = computed(() => maintenanceStore.status?.step_up ?? authStore.stepUp)

const verifyDialog = useDialog()
const rollbackDialog = useDialog()
const cancelDialog = useDialog()
const abandonDialog = useDialog()
const credentials = reactive({ password: '', totp_code: '' })
/** Set once a rollback came back asking for erasures to be acknowledged: how many accounts it brings back. */
const erasures = ref<number | null>(null)
const erasureAck = reactive({ acknowledged: false })

/** Server clock minus browser clock, in milliseconds, from the last status read. */
const clockOffset = ref(0)
const now = ref(Date.now())
let clockTimer: ReturnType<typeof setInterval> | undefined
/** Sequence of the newest job read; an older answer landing later is dropped. */
let loadSeq = 0

/** The one operation with a verification window; the confirm and roll-back actions belong to it alone. */
const UPDATE_OPERATION = 'platform.update'

/** A host request the agent has claimed publishes its first step; from then on it cannot be withdrawn. */
const canCancel = computed(() => job.value?.state === 'requested' && !job.value.step)
const isUpdate = computed(() => job.value?.operation === UPDATE_OPERATION)
const verifyPending = computed(() => {
    return job.value?.state === 'awaiting_verification' && job.value.verify_requested_at !== null
})
const rollbackPending = computed(() => {
    const current = job.value
    if (!current || current.rollback_requested_at === null) {
        return false
    }
    return current.state === 'awaiting_verification' || current.state === 'succeeded'
})
const canVerify = computed(() => {
    const current = job.value
    if (!isUpdate.value || !current || current.state !== 'awaiting_verification') {
        return false
    }
    if (current.verify_requested_at !== null || current.rollback_requested_at !== null) {
        return false
    }
    return secondsLeft.value === null || secondsLeft.value > 0
})
const canRollback = computed(() => {
    const current = job.value
    if (!isUpdate.value || !current || current.rollback_requested_at !== null) {
        return false
    }
    if (current.state === 'awaiting_verification') {
        return current.verify_requested_at === null
    }
    return current.state === 'succeeded' && current.snapshot !== ''
})
/**
 * Whether the executor of an in-flight job is gone, which is when the server lets it be abandoned: a worker job at
 * any time, a host job only while the agent is absent or has stopped reporting.
 */
const canAbandon = computed(() => {
    const current = job.value
    if (!current?.in_flight) {
        return false
    }
    if (current.executor !== 'host') {
        return true
    }
    const agent = maintenanceStore.status?.agent
    return agent !== undefined && (!agent.installed || agent.stale === true)
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
        {
            label: t('Executor', SCOPE),
            value: current.executor === 'host' ? t('Host agent', SCOPE) : t('Worker', SCOPE),
        },
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
        rows.push({ label: t('Reason', SCOPE), value: jobReasonLabel(current.reason) })
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
    if (!current) {
        return ''
    }
    const marks = `${current.verify_requested_at ?? ''}:${current.rollback_requested_at ?? ''}`
    return `${current.state}:${current.step}:${current.finished_at ?? ''}:${current.verify_deadline ?? ''}:${marks}`
}

/** Whether the job still changes by itself: in flight, or holding a request the agent has yet to act on. */
function stillMoving (current: MaintenanceJob) {
    if (current.in_flight) {
        return true
    }
    return current.rollback_requested_at !== null && current.state === 'succeeded'
}

async function loadJob (): Promise<boolean> {
    const seq = ++loadSeq
    const id = jobId.value
    const fresh = await fetchJob(id)
    let freshLog: JobLog | null = null
    if (canWrite.value) {
        // The log grows while the job runs; a read per poll keeps it current.
        try {
            freshLog = await fetchJobLog(id)
        } catch {
            freshLog = null
        }
    }
    if (seq !== loadSeq) {
        return false
    }
    const changed = fingerprint(fresh) !== fingerprint(job.value)
    job.value = fresh
    log.value = freshLog
    loadError.value = ''
    setPageTitle(fresh.operation)
    if (changed) {
        const status = await maintenanceStore.refreshStatus()
        if (status) {
            clockOffset.value = Date.parse(status.server_now) - Date.now()
        }
    }
    if (stillMoving(fresh)) {
        poll.start()
    } else {
        poll.stop()
    }
    return changed
}

const poll = usePolling(loadJob, { immediate: false })

async function load () {
    poll.stop()
    job.value = null
    log.value = null
    loading.value = true
    loadError.value = ''
    try {
        await loadJob()
    } catch (err) {
        loadError.value = errorDetail(err, t('The job could not be loaded.', SCOPE))
        // A maintenance window or a restart answers with errors for a while; keep asking.
        poll.start()
    } finally {
        loading.value = false
    }
}

function goBack () {
    router.push({ name: 'admin-maintenance' })
}

function resetForm () {
    credentials.password = ''
    credentials.totp_code = ''
    erasures.value = null
    erasureAck.acknowledged = false
}

function openVerify () {
    resetForm()
    verifyDialog.show()
}

function openRollback () {
    resetForm()
    rollbackDialog.show()
}

function openCancel () {
    cancelDialog.show()
}

function openAbandon () {
    resetForm()
    abandonDialog.show()
}

/** Adopt what the server answered, keep polling while it has more to report, and say so. */
async function acted (fresh: MaintenanceJob | undefined, done: string) {
    if (!fresh) {
        return
    }
    job.value = fresh
    showToast(done, 'neutral')
    if (stillMoving(fresh)) {
        poll.start()
    }
    await poll.refresh()
}

async function confirmVerify () {
    const fresh = await verifyDialog.run(
        () => verifyJob(jobId.value, stepUpBody(credentials)),
        { fallback: t('The request was refused.', SCOPE) },
    )
    await acted(fresh, t('Confirmation sent. The agent completes the update on its next tick.', SCOPE))
}

async function confirmRollback () {
    const acknowledge = erasures.value !== null && erasureAck.acknowledged
    const fresh = await rollbackDialog.run(
        () => rollbackJob(jobId.value, {
            ...stepUpBody(credentials),
            ...(acknowledge ? { acknowledge_erasures: true } : {}),
        }),
        {
            fallback: t('The request was refused.', SCOPE),
            onError: (err) => {
                const count = erasureConflict(err)
                if (count === null) {
                    return false
                }
                erasures.value = count
                erasureAck.acknowledged = false
                return true
            },
        },
    )
    await acted(fresh, t('Rollback requested. The agent restores the previous release on its next tick.', SCOPE))
}

async function confirmCancel () {
    const fresh = await cancelDialog.run(
        () => cancelJob(jobId.value),
        { fallback: t('The request was refused.', SCOPE) },
    )
    await acted(fresh, t('Job cancelled.', SCOPE))
}

async function confirmAbandon () {
    const fresh = await abandonDialog.run(
        () => abandonJob(jobId.value, stepUpBody(credentials)),
        { fallback: t('The request was refused.', SCOPE) },
    )
    await acted(fresh, t('Job marked as failed.', SCOPE))
}

// Another job's id while this page stays mounted. Leaving for another route
// clears the param too, and must not fetch a job called "undefined".
watch(() => route.params.id, (id, previous) => {
    if (typeof id === 'string' && id !== previous) {
        load()
    }
})

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

        <wa-callout v-else-if="loadError && !job" variant="danger">
            {{ loadError }}
            <span class="callout-aside">{{ t('Trying again.', SCOPE) }}</span>
        </wa-callout>

        <template v-else-if="job">
            <header class="page-header">
                <h1>{{ job.operation }}</h1>
                <div class="job-actions">
                    <wa-button v-if="canWrite && canCancel"
                        appearance="plain"
                        size="s"
                        @click="openCancel"
                    >
                        <wa-icon name="ban" slot="start"></wa-icon>
                        {{ t('Cancel request', SCOPE) }}
                    </wa-button>
                    <wa-button v-if="canWrite && job.in_flight"
                        appearance="plain"
                        size="s"
                        @click="openAbandon"
                    >
                        <wa-icon name="circle-exclamation" slot="start"></wa-icon>
                        {{ t('Mark as failed', SCOPE) }}
                    </wa-button>
                    <wa-button v-if="canWrite && canRollback"
                        appearance="plain"
                        size="s"
                        variant="danger"
                        @click="openRollback"
                    >
                        <wa-icon name="rotate-left" slot="start"></wa-icon>
                        {{ t('Roll back', SCOPE) }}
                    </wa-button>
                    <wa-button v-if="canWrite && canVerify"
                        appearance="filled-outlined"
                        size="s"
                        variant="brand"
                        @click="openVerify"
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

            <wa-callout v-if="rollbackPending" variant="warning">
                {{ t('A rollback was requested. The agent restores the previous release on its next tick.', SCOPE) }}
            </wa-callout>

            <wa-callout v-else-if="verifyPending" variant="neutral">
                {{ t('The update was confirmed. The agent completes it on its next tick.', SCOPE) }}
            </wa-callout>

            <wa-callout v-else-if="job.state === 'awaiting_verification'" variant="warning">
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

    <wa-dialog :label="t('Confirm update', SCOPE)" :open="verifyDialog.open.value" @wa-hide.self="verifyDialog.onHide">
        <div class="job-form">
            <wa-callout v-if="verifyDialog.error.value" variant="danger">
                {{ verifyDialog.error.value }}
            </wa-callout>
            <p class="dialog-text">
                {{ t('Confirming keeps the new release. The pre-update snapshot stays available for a later rollback for as long as snapshots are kept.', SCOPE) }}
            </p>
            <StepUpFields :credentials="credentials" password-only :step-up="stepUp" />
        </div>
        <div slot="footer" class="form-actions">
            <wa-button
                appearance="filled-outlined"
                :disabled="verifyDialog.busy.value"
                variant="neutral"
                @click="verifyDialog.close"
            >
                {{ t('Cancel', SCOPE) }}
            </wa-button>
            <wa-button
                appearance="filled-outlined"
                :disabled="!stepUp.available"
                :loading="verifyDialog.busy.value"
                variant="brand"
                @click="confirmVerify"
            >
                {{ t('Confirm update', SCOPE) }}
            </wa-button>
        </div>
    </wa-dialog>

    <wa-dialog :label="t('Roll back', SCOPE)" :open="rollbackDialog.open.value" @wa-hide.self="rollbackDialog.onHide">
        <div class="job-form">
            <wa-callout v-if="rollbackDialog.error.value" variant="danger">
                {{ rollbackDialog.error.value }}
            </wa-callout>
            <wa-callout v-if="keepsDatabase" variant="warning">
                {{ t('This update applied no migration, so rolling back restores the previous code and keeps the database; nothing written since the update is lost. The platform is unavailable while the previous release is rebuilt.', SCOPE) }}
            </wa-callout>
            <wa-callout v-else variant="danger">
                {{ t('Rolling back restores the database as it was before the update. Everything written since then is lost, including your own changes. The platform is unavailable while the previous release is rebuilt.', SCOPE) }}
            </wa-callout>
            <ErasureAcknowledgement v-if="erasures !== null" :count="erasures" :state="erasureAck" />
            <StepUpFields :credentials="credentials" :step-up="stepUp" />
        </div>
        <div slot="footer" class="form-actions">
            <wa-button
                appearance="filled-outlined"
                :disabled="rollbackDialog.busy.value"
                variant="neutral"
                @click="rollbackDialog.close"
            >
                {{ t('Cancel', SCOPE) }}
            </wa-button>
            <wa-button
                appearance="filled-outlined"
                :disabled="!stepUp.available || (erasures !== null && !erasureAck.acknowledged)"
                :loading="rollbackDialog.busy.value"
                variant="danger"
                @click="confirmRollback"
            >
                {{ t('Roll back', SCOPE) }}
            </wa-button>
        </div>
    </wa-dialog>

    <wa-dialog :label="t('Cancel request', SCOPE)" :open="cancelDialog.open.value" @wa-hide.self="cancelDialog.onHide">
        <div class="job-form">
            <wa-callout v-if="cancelDialog.error.value" variant="danger">
                {{ cancelDialog.error.value }}
            </wa-callout>
            <p class="dialog-text">
                {{ t('Withdraw this request? It has not been picked up yet, so nothing has run.', SCOPE) }}
            </p>
        </div>
        <div slot="footer" class="form-actions">
            <wa-button
                appearance="filled-outlined"
                :disabled="cancelDialog.busy.value"
                variant="neutral"
                @click="cancelDialog.close"
            >
                {{ t('Keep it', SCOPE) }}
            </wa-button>
            <wa-button
                appearance="filled-outlined"
                :loading="cancelDialog.busy.value"
                variant="danger"
                @click="confirmCancel"
            >
                {{ t('Cancel request', SCOPE) }}
            </wa-button>
        </div>
    </wa-dialog>

    <wa-dialog
        :label="t('Mark as failed', SCOPE)"
        :open="abandonDialog.open.value"
        @wa-hide.self="abandonDialog.onHide"
    >
        <div class="job-form">
            <wa-callout v-if="abandonDialog.error.value" variant="danger">
                {{ abandonDialog.error.value }}
            </wa-callout>
            <p class="dialog-text">
                {{ t('Marking the job as failed frees the slot so another job can be requested. Use it for a job whose executor is gone: a worker job at any time, a host job only while the agent is not reporting. A running agent reports the outcome itself.', SCOPE) }}
            </p>
            <template v-if="canAbandon">
                <wa-callout variant="warning">
                    {{ t('Nothing is stopped or undone on the host. Check what the job left behind before starting another.', SCOPE) }}
                </wa-callout>
                <StepUpFields :credentials="credentials" :step-up="stepUp" />
            </template>
            <wa-callout v-else variant="neutral">
                {{ t('The host agent is running and reports the outcome of this job itself.', SCOPE) }}
            </wa-callout>
        </div>
        <div slot="footer" class="form-actions">
            <wa-button
                appearance="filled-outlined"
                :disabled="abandonDialog.busy.value"
                variant="neutral"
                @click="abandonDialog.close"
            >
                {{ t('Cancel', SCOPE) }}
            </wa-button>
            <wa-button
                appearance="filled-outlined"
                :disabled="!canAbandon || !stepUp.available"
                :loading="abandonDialog.busy.value"
                variant="danger"
                @click="confirmAbandon"
            >
                {{ t('Mark as failed', SCOPE) }}
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

.callout-aside {
    color: var(--wa-color-text-quiet);
    margin-left: var(--wa-space-2xs);
}
</style>
