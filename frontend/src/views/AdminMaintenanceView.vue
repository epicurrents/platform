<script setup lang="ts">
/**
 * Maintenance — the deployment's state, the operations a superuser may run, uploaded update packages, the
 * snapshots the host holds, and recent jobs.
 *
 * Staff read everything here; only a superuser sees the run, upload and
 * remove controls, matching the tier the API enforces. An operation's form is
 * built from the argument schema the server publishes, so a project's
 * registered operation gets a form without a frontend change; the two
 * arguments the form knows by name are `package_sha256`, rendered as a choice
 * among the applicable packages rather than a hash to type, and `snapshot`,
 * rendered as a choice among the snapshots the agent reports. The step-up
 * inputs come from the method the status reports for this account, and an
 * account that cannot confirm is told so instead of failing at the submit.
 *
 * A package is uploaded as the three files the packager writes, picked
 * together from one file input and sorted by name; the server checks the
 * signature and the manifest before it keeps anything, the worker hashes the
 * archive afterwards, and a refusal is shown as it was worded. A restore that
 * would bring back erased accounts asks for that to be acknowledged first.
 *
 * @package    epicurrents-platform
 */
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import axios from 'axios'
import AdminTabs from '#components/AdminTabs.vue'
import ErasureAcknowledgement from '#components/ErasureAcknowledgement.vue'
import JobStateBadge from '#components/JobStateBadge.vue'
import StepUpFields from '#components/StepUpFields.vue'
import {
    classifyPackageFiles,
    createJob,
    deletePackage,
    erasureConflict,
    listJobs,
    listOperations,
    listPackages,
    uploadPackage,
    type ArgSchemaProperty,
    type HostSnapshot,
    type MaintenanceJob,
    type MaintenanceOperation,
    type MaintenancePackage,
    type PackageFiles,
} from '#api/maintenance'
import { useDialog } from '#composables/useDialog'
import { usePolling } from '#composables/usePolling'
import { t } from '#i18n'
import { errorDetail } from '#lib/http'
import { packageStateHint, packageStateLabel } from '#lib/maintenanceLabels'
import { stepUpBody } from '#lib/stepUp'
import { showToast } from '#lib/toast'
import { useAuthStore } from '#stores/auth'
import { useMaintenanceStore } from '#stores/maintenance'

const SCOPE = 'AdminMaintenanceView'

/** The argument of `platform.update` the form renders as a package choice. */
const PACKAGE_ARG = 'package_sha256'
/** The argument of `platform.rollback` the form renders as a snapshot choice. */
const SNAPSHOT_ARG = 'snapshot'
const UPDATE_OPERATION = 'platform.update'
const BACKUP_OPERATION = 'platform.backup'
const ROLLBACK_OPERATION = 'platform.rollback'

const authStore = useAuthStore()
const maintenanceStore = useMaintenanceStore()
const router = useRouter()

const operations = ref<MaintenanceOperation[]>([])
const jobs = ref<MaintenanceJob[]>([])
const packages = ref<MaintenancePackage[]>([])
const loading = ref(true)
const loadError = ref('')

const runDialog = useDialog()
const selected = ref<MaintenanceOperation | null>(null)
/** The form's values, keyed by argument name; strings until the submit converts them. */
const argValues = reactive<Record<string, unknown>>({})
const credentials = reactive({ password: '', totp_code: '' })
/** Set once the request came back asking for erasures to be acknowledged: how many accounts it brings back. */
const erasures = ref<number | null>(null)
const erasureAck = reactive({ acknowledged: false })

const uploadDialog = useDialog()
const uploadProgress = ref(0)
/** The parts picked so far; the upload is enabled once all three are present. */
const picked = reactive<Partial<PackageFiles>>({})
const packageInputRef = ref<HTMLInputElement | null>(null)
let uploadAbort: AbortController | null = null

const removeDialog = useDialog()
const removing = ref<MaintenancePackage | null>(null)

const canWrite = computed(() => authStore.isSuperuser)
const status = computed(() => maintenanceStore.status)
/** The status says how this account confirms; before it has loaded, the account itself says the same. */
const stepUp = computed(() => status.value?.step_up ?? authStore.stepUp)
const hostTier = computed(() => status.value?.remote_update_enabled === true)

/** What the in-flight job is, when there is one, so the page can say why the run controls are off. */
const inFlight = computed(() => jobs.value.find(job => job.in_flight) ?? null)

const updateOperation = computed(() => operations.value.find(operation => operation.key === UPDATE_OPERATION) ?? null)
const backupOperation = computed(() => operations.value.find(operation => operation.key === BACKUP_OPERATION) ?? null)
const rollbackOperation = computed(() => {
    return operations.value.find(operation => operation.key === ROLLBACK_OPERATION) ?? null
})
const applicablePackages = computed(() => packages.value.filter(pkg => pkg.applicable))
/** The section shows once the host tier is on, and stays for the history once anything was uploaded. */
const showPackages = computed(() => hostTier.value || packages.value.length > 0)
/** The snapshots the agent reports; the section shows once an agent has reported and the host tier is on. */
const snapshots = computed(() => status.value?.agent.snapshots ?? [])
const showSnapshots = computed(() => hostTier.value && status.value?.agent.installed === true)

/** The release keys in play: what the platform accepts uploads from, and what the agent trusts. */
const releaseKeySummary = computed(() => {
    const current = status.value
    if (!current) {
        return ''
    }
    const platform = current.release_key_ids.length ? current.release_key_ids.join(', ') : t('none', SCOPE)
    const agent = current.agent
    if (!agent.installed || !agent.key_id) {
        return platform
    }
    const trusted = agent.next_key_id ? `${agent.key_id}, ${agent.next_key_id}` : agent.key_id
    return t('{platform} (agent trusts {trusted})', SCOPE, { platform, trusted })
})
const uploadReady = computed(() => Boolean(picked.package && picked.manifest && picked.signature))

const agentSummary = computed(() => {
    const agent = status.value?.agent
    if (!agent || !agent.installed) {
        return {
            variant: 'neutral',
            text: t('The host agent is not installed. Updates from this page need it; the other operations do not.', SCOPE),
        }
    }
    if (!agent.enabled) {
        return { variant: 'warning', text: t('The host agent is installed but not enabled.', SCOPE) }
    }
    if (agent.stale) {
        return { variant: 'warning', text: t('The host agent has not reported for more than two minutes.', SCOPE) }
    }
    return {
        variant: 'success',
        text: t('The host agent is running (version {version}).', SCOPE, { version: agent.version ?? '?' }),
    }
})

/**
 * Whether the run form can be sent: step-up possible when the operation needs it, and the package or snapshot the
 * operation names actually chosen. A request missing either is refused by the server anyway.
 */
const runReady = computed(() => {
    const operation = selected.value
    if (!operation) {
        return false
    }
    if (operation.requires_step_up && !stepUp.value.available) {
        return false
    }
    const properties = operation.args_schema.properties ?? {}
    for (const name of operation.args_schema.required ?? []) {
        if (!(name in properties)) {
            continue
        }
        const value = argValues[name]
        if (value === '' || value === undefined || value === null) {
            return false
        }
    }
    if (erasures.value !== null && !erasureAck.acknowledged) {
        return false
    }
    return true
})

/** A stable fingerprint of what the list shows, so the poll can tell a change from a repeat. */
function fingerprint (rows: MaintenanceJob[]) {
    return rows.map(job => `${job.job_id}:${job.state}:${job.step}:${job.finished_at ?? ''}`).join('|')
}

async function loadPackages () {
    packages.value = await listPackages()
}

/** Refresh the job list; a change also refreshes the status and packages, since a settled job may have applied one. */
async function loadJobs (): Promise<boolean> {
    const rows = await listJobs()
    const changed = fingerprint(rows) !== fingerprint(jobs.value)
    jobs.value = rows
    const verifying = packages.value.some(pkg => pkg.state === 'unverified')
    if (changed) {
        await Promise.all([maintenanceStore.refreshStatus(), loadPackages()])
    } else if (verifying) {
        // The worker settles an unverified package by itself; watch for it.
        await loadPackages()
    }
    return changed
}

async function loadAll () {
    const [ops, rows] = await Promise.all([
        listOperations(),
        listJobs(),
        maintenanceStore.refreshStatus(),
        loadPackages(),
    ])
    operations.value = ops
    jobs.value = rows
    loadError.value = ''
}

/** The poll's read: the job list once the page has loaded, the whole page while it has not. */
async function pollRead (): Promise<boolean> {
    if (loadError.value) {
        await loadAll()
        return true
    }
    return loadJobs()
}

const poll = usePolling(pollRead, { immediate: false })

async function load () {
    loading.value = true
    loadError.value = ''
    try {
        await loadAll()
    } catch (err) {
        loadError.value = errorDetail(err, t('The maintenance state could not be loaded.', SCOPE))
    } finally {
        loading.value = false
        // Polled either way: a failed load retries until it answers.
        poll.start()
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

/** A bound or pattern of a property, from the property itself or from the non-null branch of its `anyOf`. */
function argConstraint (property: ArgSchemaProperty, key: 'minimum' | 'maximum' | 'pattern') {
    if (property[key] !== undefined) {
        return property[key]
    }
    const branch = property.anyOf?.find(option => option.type !== 'null')
    return branch?.[key]
}

function argLabel (name: string, property: ArgSchemaProperty) {
    return property.title ?? name.replace(/_/g, ' ')
}

function isPackageArg (name: string) {
    return name === PACKAGE_ARG
}

function isSnapshotArg (name: string) {
    return name === SNAPSHOT_ARG
}

function openRun (operation: MaintenanceOperation, preset: Record<string, unknown> = {}) {
    selected.value = operation
    for (const key of Object.keys(argValues)) {
        delete argValues[key]
    }
    for (const [name, property] of Object.entries(operation.args_schema.properties ?? {})) {
        argValues[name] = property.default ?? (argType(property) === 'boolean' ? false : '')
    }
    // A package choice defaults to the newest applicable one, since that is
    // nearly always the one meant; the select still lets another be picked.
    if (PACKAGE_ARG in argValues && applicablePackages.value.length > 0) {
        argValues[PACKAGE_ARG] = applicablePackages.value[0]!.sha256
    }
    if (SNAPSHOT_ARG in argValues && snapshots.value.length > 0) {
        argValues[SNAPSHOT_ARG] = snapshots.value[0]!.name
    }
    Object.assign(argValues, preset)
    credentials.password = ''
    credentials.totp_code = ''
    erasures.value = null
    erasureAck.acknowledged = false
    runDialog.show()
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
        if (raw === '' || raw === undefined || raw === null || (typeof raw === 'number' && Number.isNaN(raw))) {
            continue
        }
        args[name] = type === 'integer' || type === 'number' ? Number(raw) : String(raw)
    }
    return args
}

async function confirmRun () {
    const operation = selected.value
    if (!operation || !runReady.value) {
        return
    }
    const acknowledge = erasures.value !== null && erasureAck.acknowledged
    const job = await runDialog.run(
        () => createJob({
            operation: operation.key,
            args: buildArgs(operation),
            ...(operation.requires_step_up ? stepUpBody(credentials) : {}),
            ...(acknowledge ? { acknowledge_erasures: true } : {}),
        }),
        {
            // The server owns every refusal: a job in flight, a wrong password, a
            // host operation while the host tier is off, the lock. Show what it said.
            fallback: t('The operation could not be requested.', SCOPE),
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
    if (!job) {
        return
    }
    showToast(t('{label} requested.', SCOPE, { label: operation.label }), 'neutral')
    router.push({ name: 'admin-maintenance-job', params: { id: job.job_id } })
}

/** Open the update form with this package chosen. */
function applyPackage (pkg: MaintenancePackage) {
    const operation = updateOperation.value
    if (!operation) {
        return
    }
    openRun(operation, { [PACKAGE_ARG]: pkg.sha256 })
}

/** Whether a host operation can be requested now: registered, its tier and agent ready, nothing in flight, step-up possible. */
function canRunHost (operation: MaintenanceOperation | null) {
    return operation !== null && operation.available && inFlight.value === null && stepUp.value.available
}

/** Whether an operation's Run button is live. */
function canRun (operation: MaintenanceOperation) {
    if (!operation.available || inFlight.value !== null) {
        return false
    }
    return !operation.requires_step_up || stepUp.value.available
}

function canApply (pkg: MaintenancePackage) {
    return pkg.applicable && canRunHost(updateOperation.value)
}

/** An unverified or invalid package can never be applied, so it gets no Apply button at all. */
function isApplicableState (pkg: MaintenancePackage) {
    return pkg.state !== 'unverified' && pkg.state !== 'invalid'
}

const canTakeSnapshot = computed(() => canRunHost(backupOperation.value))
const canRestoreSnapshot = computed(() => canRunHost(rollbackOperation.value))

function takeSnapshot () {
    const operation = backupOperation.value
    if (!operation) {
        return
    }
    openRun(operation)
}

/** Open the roll-back form with this snapshot chosen. */
function restoreSnapshot (snapshot: HostSnapshot) {
    const operation = rollbackOperation.value
    if (!operation) {
        return
    }
    openRun(operation, { [SNAPSHOT_ARG]: snapshot.name })
}

/** How a snapshot came to be, from the label update.sh gave it. */
function snapshotKindLabel (snapshot: HostSnapshot) {
    if (snapshot.name.startsWith('pre-update-')) {
        return t('Before an update', SCOPE)
    }
    if (snapshot.name.startsWith('post-update-')) {
        return t('Before a rollback', SCOPE)
    }
    if (snapshot.name.startsWith('pre-rollback-')) {
        return t('Before a rollback', SCOPE)
    }
    if (snapshot.name.startsWith('backup-')) {
        return t('Requested', SCOPE)
    }
    return t('Manual', SCOPE)
}

function canRemove (pkg: MaintenancePackage) {
    return pkg.state !== 'pruned' && inFlight.value?.package_sha256 !== pkg.sha256
}

function packageStateVariant (pkg: MaintenancePackage) {
    switch (pkg.state) {
        case 'applied':
            return 'success'
        case 'invalid':
            return 'danger'
        case 'unverified':
            return 'warning'
        case 'available':
            return pkg.applicable ? 'brand' : 'neutral'
        default:
            return 'neutral'
    }
}

/** A byte count in binary units, one decimal throughout so a list of sizes lines up. */
function formatSize (bytes: number) {
    const units = ['B', 'KB', 'MB', 'GB']
    let value = bytes
    let unit = 0
    while (value >= 1024 && unit < units.length - 1) {
        value /= 1024
        unit += 1
    }
    return unit === 0 ? `${value} ${units[0]}` : `${value.toFixed(1)} ${units[unit]}`
}

function shortHash (sha256: string) {
    return sha256.slice(0, 12)
}

function openUpload () {
    uploadProgress.value = 0
    delete picked.package
    delete picked.manifest
    delete picked.signature
    uploadDialog.show()
}

/** The upload dialog's Cancel: stops a transfer in progress, closes the dialog otherwise. */
function cancelUpload () {
    if (uploadDialog.busy.value) {
        uploadAbort?.abort()
        return
    }
    uploadDialog.close()
}

function triggerPackageSelect () {
    packageInputRef.value?.click()
}

/** Sort the picked files into the three parts; a later pick replaces only the kinds it carries. */
function onPackageFilesChange (event: Event) {
    const input = event.target as HTMLInputElement
    Object.assign(picked, classifyPackageFiles(input.files ?? []))
    input.value = ''
}

async function confirmUpload () {
    if (!uploadReady.value || !picked.package || !picked.manifest || !picked.signature) {
        return
    }
    const files = { package: picked.package, manifest: picked.manifest, signature: picked.signature }
    uploadProgress.value = 0
    uploadAbort = new AbortController()
    const signal = uploadAbort.signal
    const pkg = await uploadDialog.run(
        () => uploadPackage(files, fraction => {
            uploadProgress.value = Math.round(fraction * 100)
        }, signal),
        {
            // The server names the refusal: a signature that does not verify, a
            // version that is not newer, a package for another deployment, the lock.
            fallback: t('The package could not be uploaded.', SCOPE),
            onError: (err) => {
                if (!axios.isCancel(err)) {
                    return false
                }
                uploadProgress.value = 0
                showToast(t('Upload cancelled.', SCOPE), 'neutral')
                return true
            },
        },
    )
    uploadAbort = null
    if (!pkg) {
        return
    }
    showToast(
        pkg.state === 'unverified'
            ? t('Package {version} uploaded. The archive is being verified; it can be applied once that holds.', SCOPE, {
                version: pkg.version,
            })
            : t('Package {version} uploaded and verified.', SCOPE, { version: pkg.version }),
        'success',
    )
    await loadPackages()
}

function openRemove (pkg: MaintenancePackage) {
    removing.value = pkg
    removeDialog.show()
}

async function confirmRemove () {
    const target = removing.value
    if (!target) {
        return
    }
    // A job may have picked the package up between the list and the click, or the lock may be up.
    const removed = await removeDialog.run(
        () => deletePackage(target.sha256),
        { fallback: t('The package could not be removed.', SCOPE) },
    )
    if (!removed) {
        return
    }
    showToast(t('Package removed.', SCOPE), 'neutral')
    await loadPackages()
}

function openJob (job: MaintenanceJob) {
    router.push({ name: 'admin-maintenance-job', params: { id: job.job_id } })
}

onMounted(load)

onBeforeUnmount(() => {
    uploadAbort?.abort()
})
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
            <span class="callout-aside">{{ t('Trying again.', SCOPE) }}</span>
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
                <template v-if="hostTier">
                    <dt>{{ t('Release key', SCOPE) }}</dt>
                    <dd>{{ releaseKeySummary }}</dd>
                </template>
            </dl>

            <wa-callout v-if="status && !status.spool_writable" variant="warning">
                {{ t('The maintenance spool is not writable by the platform, so host-tier operations cannot be requested. An operator needs to fix the ownership of the update directory.', SCOPE) }}
            </wa-callout>

            <wa-callout v-if="status?.lock" variant="warning">
                {{ t('The platform is locked for maintenance ({phase}): {message}', SCOPE, { phase: status.lock.phase, message: status.lock.message }) }}
            </wa-callout>

            <wa-callout v-if="canWrite && status && !stepUp.available" variant="warning">
                {{ stepUp.reason ?? t('This account cannot confirm sensitive actions.', SCOPE) }}
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
                                :disabled="!canRun(operation)"
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

            <section v-if="showPackages" class="maintenance-section">
                <div class="section-header">
                    <h2>{{ t('Update packages', SCOPE) }}</h2>
                    <wa-button v-if="canWrite && hostTier"
                        appearance="plain"
                        size="s"
                        variant="brand"
                        @click="openUpload"
                    >
                        <wa-icon name="cloud-arrow-up" slot="start"></wa-icon>
                        {{ t('Upload package', SCOPE) }}
                    </wa-button>
                </div>
                <p v-if="status && !status.release_key_present" class="maintenance-hint">
                    {{ t('No release key is installed on this deployment, so no package can be verified. The key ships at the root of every distribution package.', SCOPE) }}
                </p>
                <p v-if="!packages.length" class="empty-state">
                    {{ t('No package has been uploaded yet. A release is uploaded as its three files: the archive, its manifest and the signature.', SCOPE) }}
                </p>
                <div v-else class="list-rows">
                    <div v-for="pkg in packages" :key="pkg.sha256" class="list-row">
                        <div class="list-row-main operation-row">
                            <div class="operation-text">
                                <span class="list-row-name">{{ t('Version {version}', SCOPE, { version: pkg.version }) }}</span>
                                <span class="operation-description">
                                    {{ formatSize(pkg.size) }} · {{ shortHash(pkg.sha256) }}
                                    <template v-if="pkg.project"> · {{ pkg.project }}</template>
                                    <template v-if="pkg.plugins.length"> · {{ pkg.plugins.join(', ') }}</template>
                                </span>
                                <span v-if="packageStateHint(pkg.state)" class="operation-description">
                                    {{ packageStateHint(pkg.state) }}
                                </span>
                                <div class="row-badges">
                                    <wa-badge appearance="outlined" :variant="packageStateVariant(pkg)">
                                        {{ packageStateLabel(pkg.state, pkg.applicable) }}
                                    </wa-badge>
                                    <span class="list-row-meta">
                                        <wa-relative-time :date="pkg.uploaded_at"></wa-relative-time>
                                        <template v-if="pkg.uploaded_by"> · {{ pkg.uploaded_by }}</template>
                                    </span>
                                </div>
                            </div>
                            <div v-if="canWrite" class="package-actions">
                                <wa-button v-if="isApplicableState(pkg)"
                                    appearance="plain"
                                    :disabled="!canApply(pkg)"
                                    size="s"
                                    variant="brand"
                                    @click="applyPackage(pkg)"
                                >
                                    <wa-icon name="play" slot="start"></wa-icon>
                                    {{ t('Apply', SCOPE) }}
                                </wa-button>
                                <wa-button
                                    appearance="plain"
                                    :disabled="!canRemove(pkg)"
                                    size="s"
                                    @click="openRemove(pkg)"
                                >
                                    <wa-icon name="trash" slot="start"></wa-icon>
                                    {{ t('Remove', SCOPE) }}
                                </wa-button>
                            </div>
                        </div>
                    </div>
                </div>
            </section>

            <section v-if="showSnapshots" class="maintenance-section">
                <div class="section-header">
                    <h2>{{ t('Snapshots on the host', SCOPE) }}</h2>
                    <wa-button v-if="canWrite && backupOperation"
                        appearance="plain"
                        :disabled="!canTakeSnapshot"
                        size="s"
                        variant="brand"
                        @click="takeSnapshot"
                    >
                        <wa-icon name="camera" slot="start"></wa-icon>
                        {{ t('Take a snapshot', SCOPE) }}
                    </wa-button>
                </div>
                <p class="maintenance-hint">
                    {{ t('A snapshot holds the code, the database and the configuration as they were. Rolling back to one restores the database too unless it is kept, and the platform is unavailable while the release is rebuilt.', SCOPE) }}
                </p>
                <p v-if="!snapshots.length" class="empty-state">
                    {{ t('The host holds no snapshot. One is taken before every update, and one can be requested here.', SCOPE) }}
                </p>
                <div v-else class="list-rows">
                    <div v-for="snapshot in snapshots" :key="snapshot.name" class="list-row">
                        <div class="list-row-main operation-row">
                            <div class="operation-text">
                                <span class="list-row-name">{{ snapshot.name }}</span>
                                <span class="operation-description">
                                    {{ snapshotKindLabel(snapshot) }}
                                    <template v-if="snapshot.version"> · {{ t('version {version}', SCOPE, { version: snapshot.version }) }}</template>
                                </span>
                                <div class="row-badges">
                                    <wa-badge v-if="!snapshot.code" appearance="outlined" variant="warning">
                                        {{ t('Database only', SCOPE) }}
                                    </wa-badge>
                                    <wa-badge v-if="snapshot.migrations === 'none'" appearance="outlined" variant="success">
                                        {{ t('No migration since', SCOPE) }}
                                    </wa-badge>
                                    <span v-if="snapshot.taken_at" class="list-row-meta">
                                        <wa-relative-time :date="snapshot.taken_at"></wa-relative-time>
                                    </span>
                                </div>
                            </div>
                            <wa-button v-if="canWrite && rollbackOperation"
                                appearance="plain"
                                :disabled="!canRestoreSnapshot"
                                size="s"
                                variant="danger"
                                @click="restoreSnapshot(snapshot)"
                            >
                                <wa-icon name="rotate-left" slot="start"></wa-icon>
                                {{ t('Roll back to this', SCOPE) }}
                            </wa-button>
                        </div>
                    </div>
                </div>
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

    <wa-dialog :label="selected?.label ?? ''" :open="runDialog.open.value" @wa-hide.self="runDialog.onHide">
        <div v-if="selected" class="maintenance-form">
            <wa-callout v-if="runDialog.error.value" variant="danger">
                {{ runDialog.error.value }}
            </wa-callout>
            <p class="maintenance-hint">{{ selected.description }}</p>
            <template v-for="name in argNames(selected)" :key="name">
                <template v-if="isPackageArg(name)">
                    <wa-select v-if="applicablePackages.length"
                        :help-text="selected.args_schema.properties![name]!.description"
                        :label="t('Package', SCOPE)"
                        v-wa="[argValues, name]"
                    >
                        <wa-option v-for="pkg in applicablePackages" :key="pkg.sha256" :value="pkg.sha256">
                            {{ t('Version {version} ({hash})', SCOPE, { version: pkg.version, hash: shortHash(pkg.sha256) }) }}
                        </wa-option>
                    </wa-select>
                    <wa-callout v-else variant="warning">
                        {{ t('No uploaded package is newer than the installed version. Upload one first.', SCOPE) }}
                    </wa-callout>
                </template>
                <template v-else-if="isSnapshotArg(name)">
                    <wa-select v-if="snapshots.length"
                        :help-text="selected.args_schema.properties![name]!.description"
                        :label="t('Snapshot', SCOPE)"
                        v-wa="[argValues, name]"
                    >
                        <wa-option v-for="snapshot in snapshots" :key="snapshot.name" :value="snapshot.name">
                            {{ snapshot.version ? t('{name} (version {version})', SCOPE, { name: snapshot.name, version: snapshot.version }) : snapshot.name }}
                        </wa-option>
                    </wa-select>
                    <wa-callout v-else variant="warning">
                        {{ t('The host holds no snapshot to roll back to.', SCOPE) }}
                    </wa-callout>
                    <wa-callout variant="danger">
                        {{ t('Rolling back restores the snapshot over the running platform. Unless the database is kept, everything written since the snapshot is lost. The platform is unavailable while the release is rebuilt.', SCOPE) }}
                    </wa-callout>
                </template>
                <wa-switch v-else-if="argType(selected.args_schema.properties![name]!) === 'boolean'" v-wa="[argValues, name]">
                    {{ argLabel(name, selected.args_schema.properties![name]!) }}
                </wa-switch>
                <wa-input v-else-if="['integer', 'number'].includes(argType(selected.args_schema.properties![name]!))"
                    :help-text="selected.args_schema.properties![name]!.description"
                    :label="argLabel(name, selected.args_schema.properties![name]!)"
                    :max="argConstraint(selected.args_schema.properties![name]!, 'maximum')"
                    :min="argConstraint(selected.args_schema.properties![name]!, 'minimum')"
                    type="number"
                    v-wa="[argValues, name]"
                ></wa-input>
                <wa-input v-else
                    :help-text="selected.args_schema.properties![name]!.description"
                    :label="argLabel(name, selected.args_schema.properties![name]!)"
                    :pattern="argConstraint(selected.args_schema.properties![name]!, 'pattern')"
                    :required="selected.args_schema.required?.includes(name)"
                    v-wa="[argValues, name]"
                ></wa-input>
            </template>
            <ErasureAcknowledgement v-if="erasures !== null" :count="erasures" :state="erasureAck" />
            <StepUpFields v-if="selected.requires_step_up" :credentials="credentials" :step-up="stepUp" />
        </div>
        <div slot="footer" class="form-actions">
            <wa-button
                appearance="filled-outlined"
                :disabled="runDialog.busy.value"
                variant="neutral"
                @click="runDialog.close"
            >
                {{ t('Cancel', SCOPE) }}
            </wa-button>
            <wa-button
                appearance="filled-outlined"
                :disabled="!runReady"
                :loading="runDialog.busy.value"
                variant="brand"
                @click="confirmRun"
            >
                {{ t('Run', SCOPE) }}
            </wa-button>
        </div>
    </wa-dialog>

    <wa-dialog :label="t('Upload package', SCOPE)" :open="uploadDialog.open.value" @wa-hide.self="uploadDialog.onHide">
        <div class="maintenance-form">
            <wa-callout v-if="uploadDialog.error.value" variant="danger">
                {{ uploadDialog.error.value }}
            </wa-callout>
            <p class="maintenance-hint">
                {{ t('Select the three files of a release together: the archive (.tar.gz), its manifest (.manifest.json) and the signature (.manifest.sig). The package is verified before it is kept, and can be applied from this page afterwards.', SCOPE) }}
            </p>
            <wa-button
                appearance="filled-outlined"
                :disabled="uploadDialog.busy.value"
                variant="neutral"
                @click="triggerPackageSelect"
            >
                <wa-icon name="folder-open" slot="start"></wa-icon>
                {{ t('Select files', SCOPE) }}
            </wa-button>
            <input
                ref="packageInputRef"
                accept=".tar.gz,.tgz,.json,.sig"
                class="hidden-input"
                multiple
                type="file"
                @change="onPackageFilesChange"
            />
            <ul class="package-parts">
                <li :class="{ 'package-part-present': picked.package }">
                    <wa-icon :name="picked.package ? 'circle-check' : 'file'"></wa-icon>
                    <span>{{ t('Archive', SCOPE) }}</span>
                    <span v-if="picked.package" class="package-part-name">{{ picked.package.name }}</span>
                </li>
                <li :class="{ 'package-part-present': picked.manifest }">
                    <wa-icon :name="picked.manifest ? 'circle-check' : 'file'"></wa-icon>
                    <span>{{ t('Manifest', SCOPE) }}</span>
                    <span v-if="picked.manifest" class="package-part-name">{{ picked.manifest.name }}</span>
                </li>
                <li :class="{ 'package-part-present': picked.signature }">
                    <wa-icon :name="picked.signature ? 'circle-check' : 'file'"></wa-icon>
                    <span>{{ t('Signature', SCOPE) }}</span>
                    <span v-if="picked.signature" class="package-part-name">{{ picked.signature.name }}</span>
                </li>
            </ul>
            <wa-progress-bar v-if="uploadDialog.busy.value" :value="uploadProgress"></wa-progress-bar>
        </div>
        <div slot="footer" class="form-actions">
            <wa-button
                appearance="filled-outlined"
                variant="neutral"
                @click="cancelUpload"
            >
                {{ uploadDialog.busy.value ? t('Stop upload', SCOPE) : t('Cancel', SCOPE) }}
            </wa-button>
            <wa-button
                appearance="filled-outlined"
                :disabled="!uploadReady"
                :loading="uploadDialog.busy.value"
                variant="brand"
                @click="confirmUpload"
            >
                {{ t('Upload', SCOPE) }}
            </wa-button>
        </div>
    </wa-dialog>

    <wa-dialog :label="t('Remove package', SCOPE)" :open="removeDialog.open.value" @wa-hide.self="removeDialog.onHide">
        <wa-callout v-if="removeDialog.error.value" variant="danger">
            {{ removeDialog.error.value }}
        </wa-callout>
        <p class="dialog-text">
            {{ t('Remove the package for version {version}? Its files are deleted from the server; the record of jobs that used it stays.', SCOPE, { version: removing?.version ?? '' }) }}
        </p>
        <div slot="footer" class="form-actions">
            <wa-button
                appearance="filled-outlined"
                :disabled="removeDialog.busy.value"
                variant="neutral"
                @click="removeDialog.close"
            >
                {{ t('Cancel', SCOPE) }}
            </wa-button>
            <wa-button
                appearance="filled-outlined"
                :loading="removeDialog.busy.value"
                variant="danger"
                @click="confirmRemove"
            >
                {{ t('Remove package', SCOPE) }}
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

/* The buttons sit at the row's end; the text column takes the rest. */
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

.package-actions {
    display: flex;
    flex-shrink: 0;
    gap: var(--wa-space-2xs);
}

.package-parts {
    display: flex;
    flex-direction: column;
    gap: var(--wa-space-2xs);
    list-style: none;
    margin: 0;
    padding: 0;
}

.package-parts li {
    align-items: center;
    color: var(--wa-color-text-quiet);
    display: flex;
    gap: var(--wa-space-xs);
}

.package-parts li.package-part-present {
    color: var(--wa-color-text-normal);
}

.package-part-name {
    color: var(--wa-color-text-quiet);
    font-size: var(--wa-font-size-s);
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}

.hidden-input {
    display: none;
}

.dialog-text {
    margin: 0;
}

.callout-aside {
    color: var(--wa-color-text-quiet);
    margin-left: var(--wa-space-2xs);
}
</style>
