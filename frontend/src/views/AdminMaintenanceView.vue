<script setup lang="ts">
/**
 * Maintenance — the deployment's state, the operations a superuser may run, and recent jobs.
 *
 * Staff read everything here; only a superuser sees the run controls, matching
 * the tier the API enforces. An operation's form is built from the argument
 * schema the server publishes, so a project's registered operation gets a
 * form without a frontend change. The step-up inputs come from the method the
 * status reports for this account, and an account that cannot confirm is told
 * so instead of failing at the submit.
 *
 * @package    epicurrents-platform
 */
import { computed, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import AdminTabs from '#components/AdminTabs.vue'
import JobStateBadge from '#components/JobStateBadge.vue'
import StepUpFields from '#components/StepUpFields.vue'
import {
    createJob,
    listJobs,
    listOperations,
    type ArgSchemaProperty,
    type MaintenanceJob,
    type MaintenanceOperation,
} from '#api/maintenance'
import { usePolling } from '#composables/usePolling'
import { t } from '#i18n'
import { errorDetail } from '#lib/http'
import { showToast } from '#lib/toast'
import { useAuthStore } from '#stores/auth'
import { useMaintenanceStore } from '#stores/maintenance'

const SCOPE = 'AdminMaintenanceView'

const authStore = useAuthStore()
const maintenanceStore = useMaintenanceStore()
const router = useRouter()

const operations = ref<MaintenanceOperation[]>([])
const jobs = ref<MaintenanceJob[]>([])
const loading = ref(true)
const loadError = ref('')

const showRun = ref(false)
const running = ref(false)
const runError = ref('')
const selected = ref<MaintenanceOperation | null>(null)
/** The form's values, keyed by argument name; strings until the submit converts them. */
const argValues = reactive<Record<string, unknown>>({})
const credentials = reactive({ password: '', totp_code: '' })

const canWrite = computed(() => authStore.isSuperuser)
const status = computed(() => maintenanceStore.status)
const stepUp = computed(() => status.value?.step_up ?? { method: null, available: false, reason: null })

/** What the in-flight job is, when there is one, so the page can say why the run controls are off. */
const inFlight = computed(() => jobs.value.find(job => job.in_flight) ?? null)

const agentSummary = computed(() => {
    const agent = status.value?.agent
    if (!agent || !agent.installed) {
        return { variant: 'neutral', text: t('The host agent is not installed. Updates from this page need it; the other operations do not.', SCOPE) }
    }
    if (!agent.enabled) {
        return { variant: 'warning', text: t('The host agent is installed but not enabled.', SCOPE) }
    }
    if (agent.stale) {
        return { variant: 'warning', text: t('The host agent has not reported for more than two minutes.', SCOPE) }
    }
    return { variant: 'success', text: t('The host agent is running (version {version}).', SCOPE, { version: agent.version ?? '?' }) }
})

/** A stable fingerprint of what the list shows, so the poll can tell a change from a repeat. */
function fingerprint (rows: MaintenanceJob[]) {
    return rows.map(job => `${job.job_id}:${job.state}:${job.step}:${job.finished_at ?? ''}`).join('|')
}

async function loadJobs (): Promise<boolean> {
    const rows = await listJobs()
    const changed = fingerprint(rows) !== fingerprint(jobs.value)
    jobs.value = rows
    if (changed) {
        await maintenanceStore.refreshStatus()
    }
    return changed
}

const poll = usePolling(loadJobs, { immediate: false })

async function load () {
    loading.value = true
    loadError.value = ''
    try {
        const [ops] = await Promise.all([listOperations(), maintenanceStore.refreshStatus(), loadJobs()])
        operations.value = ops
        poll.start()
    } catch (err) {
        loadError.value = errorDetail(err, t('The maintenance state could not be loaded.', SCOPE))
    } finally {
        loading.value = false
    }
}

/** The argument names of an operation, in the order the schema lists them. */
function argNames (operation: MaintenanceOperation) {
    return Object.keys(operation.args_schema.properties ?? {})
}

/** The JSON type of a property, seeing through the `anyOf` a nullable field carries. */
function argType (property: ArgSchemaProperty): string {
    if (property.type) {
        return property.type
    }
    const first = property.anyOf?.find(option => option.type !== 'null')
    return first?.type ?? 'string'
}

function argLabel (name: string, property: ArgSchemaProperty) {
    return property.title ?? name.replace(/_/g, ' ')
}

function openRun (operation: MaintenanceOperation) {
    runError.value = ''
    selected.value = operation
    for (const key of Object.keys(argValues)) {
        delete argValues[key]
    }
    for (const [name, property] of Object.entries(operation.args_schema.properties ?? {})) {
        argValues[name] = property.default ?? (argType(property) === 'boolean' ? false : '')
    }
    credentials.password = ''
    credentials.totp_code = ''
    showRun.value = true
}

function closeRun () {
    if (running.value) {
        return
    }
    showRun.value = false
}

/** Turn the form's values into the arguments the schema expects; blanks are omitted so the server default applies. */
function buildArgs (operation: MaintenanceOperation): Record<string, unknown> {
    const args: Record<string, unknown> = {}
    for (const [name, property] of Object.entries(operation.args_schema.properties ?? {})) {
        const raw = argValues[name]
        const type = argType(property)
        if (type === 'boolean') {
            args[name] = Boolean(raw)
            continue
        }
        if (raw === '' || raw === undefined || raw === null) {
            continue
        }
        args[name] = type === 'integer' || type === 'number' ? Number(raw) : String(raw)
    }
    return args
}

async function confirmRun () {
    const operation = selected.value
    if (!operation) {
        return
    }
    runError.value = ''
    running.value = true
    try {
        const job = await createJob({
            operation: operation.key,
            args: buildArgs(operation),
            ...(operation.requires_step_up
                ? { password: credentials.password || undefined, totp_code: credentials.totp_code || undefined }
                : {}),
        })
        showRun.value = false
        showToast(t('{label} requested.', SCOPE, { label: operation.label }), 'neutral')
        router.push({ name: 'admin-maintenance-job', params: { id: job.job_id } })
    } catch (err) {
        // The server owns every refusal: a job in flight, a wrong password, a
        // host operation while the host tier is off. Show what it said.
        runError.value = errorDetail(err, t('The operation could not be requested.', SCOPE))
    } finally {
        running.value = false
    }
}

function openJob (job: MaintenanceJob) {
    router.push({ name: 'admin-maintenance-job', params: { id: job.job_id } })
}

onMounted(load)
</script>

<template>
    <main class="page-view">
        <header class="page-header">
            <h1>{{ t('Administration', SCOPE) }}</h1>
        </header>

        <AdminTabs active="maintenance" />

        <wa-callout v-if="!canWrite" variant="neutral">
            {{ t('You can view the maintenance state and job history. Running an operation requires superuser access.', SCOPE) }}
        </wa-callout>

        <wa-spinner v-if="loading" class="loading-center"></wa-spinner>

        <wa-callout v-else-if="loadError" variant="danger">
            {{ loadError }}
        </wa-callout>

        <template v-else>
            <dl v-if="status" class="maintenance-facts">
                <dt>{{ t('Installed version', SCOPE) }}</dt>
                <dd>{{ status.installed_version }}</dd>
                <dt>{{ t('Remote updates', SCOPE) }}</dt>
                <dd>{{ status.remote_update_enabled ? t('Enabled', SCOPE) : t('Disabled', SCOPE) }}</dd>
                <dt>{{ t('Host agent', SCOPE) }}</dt>
                <dd>
                    <wa-badge appearance="outlined" :variant="agentSummary.variant">
                        {{ agentSummary.text }}
                    </wa-badge>
                </dd>
            </dl>

            <wa-callout v-if="status && !status.spool_writable" variant="warning">
                {{ t('The maintenance spool is not writable by the platform, so host-tier operations cannot be requested. An operator needs to fix the ownership of the update directory.', SCOPE) }}
            </wa-callout>

            <wa-callout v-if="status?.lock" variant="warning">
                {{ t('The platform is locked for maintenance ({phase}): {message}', SCOPE, { phase: status.lock.phase, message: status.lock.message }) }}
            </wa-callout>

            <wa-callout v-if="canWrite && !stepUp.available" variant="warning">
                {{ stepUp.reason }}
            </wa-callout>

            <section class="maintenance-section">
                <div class="section-header">
                    <h2>{{ t('Operations', SCOPE) }}</h2>
                </div>
                <p v-if="!operations.length" class="empty-state">
                    {{ t('No operations are registered.', SCOPE) }}
                </p>
                <div v-else class="list-rows">
                    <div v-for="operation in operations" :key="operation.key" class="list-row">
                        <div class="list-row-main operation-row">
                            <div class="operation-text">
                                <span class="list-row-name">{{ operation.label }}</span>
                                <span class="operation-description">{{ operation.description }}</span>
                                <div class="row-badges">
                                    <wa-badge v-if="operation.executor === 'host'" appearance="outlined" variant="neutral">
                                        {{ t('Host agent', SCOPE) }}
                                    </wa-badge>
                                    <wa-badge v-if="operation.requires_step_up" appearance="outlined" variant="neutral">
                                        {{ t('Confirmation required', SCOPE) }}
                                    </wa-badge>
                                    <wa-badge v-if="!operation.available" appearance="filled" variant="neutral">
                                        {{ t('Unavailable', SCOPE) }}
                                    </wa-badge>
                                </div>
                            </div>
                            <wa-button v-if="canWrite"
                                appearance="plain"
                                :disabled="!operation.available || inFlight !== null || (operation.requires_step_up && !stepUp.available)"
                                size="s"
                                variant="brand"
                                @click="openRun(operation)"
                            >
                                <wa-icon name="play" slot="start"></wa-icon>
                                {{ t('Run', SCOPE) }}
                            </wa-button>
                        </div>
                    </div>
                </div>
                <p v-if="canWrite && inFlight" class="maintenance-hint">
                    {{ t('One job runs at a time; another can be requested once the current one finishes.', SCOPE) }}
                </p>
            </section>

            <section class="maintenance-section">
                <div class="section-header">
                    <h2>{{ t('Jobs', SCOPE) }}</h2>
                </div>
                <p v-if="!jobs.length" class="empty-state">
                    {{ t('No maintenance job has been requested yet.', SCOPE) }}
                </p>
                <div v-else class="list-rows">
                    <div v-for="job in jobs"
                        :key="job.job_id"
                        class="list-row clickable"
                        @click="openJob(job)"
                    >
                        <div class="list-row-main">
                            <wa-icon class="icon-muted" name="screwdriver-wrench"></wa-icon>
                            <span class="list-row-name">{{ job.operation }}</span>
                            <div class="row-badges">
                                <JobStateBadge :state="job.state" />
                            </div>
                            <span class="list-row-meta">
                                <wa-relative-time :date="job.created_at"></wa-relative-time>
                                <template v-if="job.requested_by"> · {{ job.requested_by }}</template>
                            </span>
                        </div>
                    </div>
                </div>
            </section>
        </template>
    </main>

    <wa-dialog :label="selected?.label ?? ''" :open="showRun" @wa-hide.self="closeRun">
        <div v-if="selected" class="maintenance-form">
            <wa-callout v-if="runError" variant="danger">
                {{ runError }}
            </wa-callout>
            <p class="maintenance-hint">{{ selected.description }}</p>
            <template v-for="name in argNames(selected)" :key="name">
                <wa-switch v-if="argType(selected.args_schema.properties![name]!) === 'boolean'" v-wa="[argValues, name]">
                    {{ argLabel(name, selected.args_schema.properties![name]!) }}
                </wa-switch>
                <wa-input v-else-if="['integer', 'number'].includes(argType(selected.args_schema.properties![name]!))"
                    :help-text="selected.args_schema.properties![name]!.description"
                    :label="argLabel(name, selected.args_schema.properties![name]!)"
                    :max="selected.args_schema.properties![name]!.maximum"
                    :min="selected.args_schema.properties![name]!.minimum"
                    type="number"
                    v-wa="[argValues, name]"
                ></wa-input>
                <wa-input v-else
                    :help-text="selected.args_schema.properties![name]!.description"
                    :label="argLabel(name, selected.args_schema.properties![name]!)"
                    :pattern="selected.args_schema.properties![name]!.pattern"
                    :required="selected.args_schema.required?.includes(name)"
                    v-wa="[argValues, name]"
                ></wa-input>
            </template>
            <StepUpFields v-if="selected.requires_step_up" :credentials="credentials" :step-up="stepUp" />
        </div>
        <div slot="footer" class="form-actions">
            <wa-button
                appearance="filled-outlined"
                :disabled="running"
                variant="neutral"
                @click="closeRun"
            >
                {{ t('Cancel', SCOPE) }}
            </wa-button>
            <wa-button
                appearance="filled-outlined"
                :loading="running"
                variant="brand"
                @click="confirmRun"
            >
                {{ t('Run', SCOPE) }}
            </wa-button>
        </div>
    </wa-dialog>
</template>

<style scoped>
.icon-muted {
    color: var(--wa-color-text-quiet);
    flex-shrink: 0;
}

.maintenance-section {
    margin-bottom: var(--wa-space-xl);
}

.maintenance-form {
    display: flex;
    flex-direction: column;
    gap: var(--wa-space-s);
}

.maintenance-hint {
    color: var(--wa-color-text-quiet);
    font-size: var(--wa-font-size-s);
    margin: 0 0 var(--wa-space-s);
}

.maintenance-facts {
    display: grid;
    grid-template-columns: max-content 1fr;
    align-items: center;
    gap: var(--wa-space-2xs) var(--wa-space-m);
    margin: 0 0 var(--wa-space-l);
}

.maintenance-facts dt {
    color: var(--wa-color-text-quiet);
    font-size: var(--wa-font-size-s);
}

.maintenance-facts dd {
    margin: 0;
}

/* The run button sits at the row's end; the text column takes the rest. */
.operation-row {
    align-items: flex-start;
    justify-content: space-between;
}

.operation-text {
    display: flex;
    flex: 1;
    flex-direction: column;
    gap: var(--wa-space-2xs);
    min-width: 0;
}

.operation-description {
    color: var(--wa-color-text-quiet);
    font-size: var(--wa-font-size-s);
}
</style>
