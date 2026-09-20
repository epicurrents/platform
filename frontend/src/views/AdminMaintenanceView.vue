<script setup lang="ts">
/**
 * Maintenance — the deployment's state, the operations a superuser may run, uploaded update packages, and recent jobs.
 *
 * Staff read everything here; only a superuser sees the run, upload and
 * remove controls, matching the tier the API enforces. An operation's form is
 * built from the argument schema the server publishes, so a project's
 * registered operation gets a form without a frontend change; the one
 * argument the form knows by name is `package_sha256`, which it renders as a
 * choice among the applicable packages rather than a hash to type. The
 * step-up inputs come from the method the status reports for this account,
 * and an account that cannot confirm is told so instead of failing at the
 * submit.
 *
 * A package is uploaded as the three files the packager writes, picked
 * together from one file input and sorted by name; the server verifies the
 * signature and the manifest before it keeps anything, and its refusal is
 * shown as it was worded.
 *
 * @package    epicurrents-platform
 */
import { computed, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import AdminTabs from '#components/AdminTabs.vue'
import JobStateBadge from '#components/JobStateBadge.vue'
import StepUpFields from '#components/StepUpFields.vue'
import {
    classifyPackageFiles,
    createJob,
    deletePackage,
    listJobs,
    listOperations,
    listPackages,
    uploadPackage,
    type ArgSchemaProperty,
    type MaintenanceJob,
    type MaintenanceOperation,
    type MaintenancePackage,
    type PackageFiles,
} from '#api/maintenance'
import { usePolling } from '#composables/usePolling'
import { t } from '#i18n'
import { errorDetail } from '#lib/http'
import { showToast } from '#lib/toast'
import { useAuthStore } from '#stores/auth'
import { useMaintenanceStore } from '#stores/maintenance'

const SCOPE = 'AdminMaintenanceView'

/** The argument of `platform.update` the form renders as a package choice. */
const PACKAGE_ARG = 'package_sha256'
const UPDATE_OPERATION = 'platform.update'

const authStore = useAuthStore()
const maintenanceStore = useMaintenanceStore()
const router = useRouter()

const operations = ref<MaintenanceOperation[]>([])
const jobs = ref<MaintenanceJob[]>([])
const packages = ref<MaintenancePackage[]>([])
const loading = ref(true)
const loadError = ref('')

const showRun = ref(false)
const running = ref(false)
const runError = ref('')
const selected = ref<MaintenanceOperation | null>(null)
/** The form's values, keyed by argument name; strings until the submit converts them. */
const argValues = reactive<Record<string, unknown>>({})
const credentials = reactive({ password: '', totp_code: '' })

const showUpload = ref(false)
const uploading = ref(false)
const uploadError = ref('')
const uploadProgress = ref(0)
/** The parts picked so far; the upload is enabled once all three are present. */
const picked = reactive<Partial<PackageFiles>>({})
const packageInputRef = ref<HTMLInputElement | null>(null)

const removing = ref<MaintenancePackage | null>(null)
const removeLoading = ref(false)
const removeError = ref('')

const canWrite = computed(() => authStore.isSuperuser)
const status = computed(() => maintenanceStore.status)
const stepUp = computed(() => status.value?.step_up ?? { method: null, available: false, reason: null })
const hostTier = computed(() => status.value?.remote_update_enabled === true)

/** What the in-flight job is, when there is one, so the page can say why the run controls are off. */
const inFlight = computed(() => jobs.value.find(job => job.in_flight) ?? null)

const updateOperation = computed(() => operations.value.find(operation => operation.key === UPDATE_OPERATION) ?? null)
const applicablePackages = computed(() => packages.value.filter(pkg => pkg.applicable))
/** The section shows once the host tier is on, and stays for the history once anything was uploaded. */
const showPackages = computed(() => hostTier.value || packages.value.length > 0)
const uploadReady = computed(() => Boolean(picked.package && picked.manifest && picked.signature))

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

async function loadPackages () {
    packages.value = await listPackages()
}

async function loadJobs (): Promise<boolean> {
    const rows = await listJobs()
    const changed = fingerprint(rows) !== fingerprint(jobs.value)
    jobs.value = rows
    if (changed) {
        // A job that settled may have applied a package; the list says so.
        await Promise.all([maintenanceStore.refreshStatus(), loadPackages()])
    }
    return changed
}

const poll = usePolling(loadJobs, { immediate: false })

async function load () {
    loading.value = true
    loadError.value = ''
    try {
        const [ops] = await Promise.all([listOperations(), maintenanceStore.refreshStatus(), loadJobs(), loadPackages()])
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

function isPackageArg (name: string) {
    return name === PACKAGE_ARG
}

function openRun (operation: MaintenanceOperation, preset: Record<string, unknown> = {}) {
    runError.value = ''
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
    Object.assign(argValues, preset)
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

/** Open the update form with this package chosen. */
function applyPackage (pkg: MaintenancePackage) {
    const operation = updateOperation.value
    if (!operation) {
        return
    }
    openRun(operation, { [PACKAGE_ARG]: pkg.sha256 })
}

function canApply (pkg: MaintenancePackage) {
    const operation = updateOperation.value
    return pkg.applicable && operation !== null && operation.available && inFlight.value === null && stepUp.value.available
}

function canRemove (pkg: MaintenancePackage) {
    return pkg.state !== 'pruned' && inFlight.value?.package_sha256 !== pkg.sha256
}

function packageStateLabel (pkg: MaintenancePackage) {
    switch (pkg.state) {
        case 'applied':
            return t('Applied', SCOPE)
        case 'pruned':
            return t('Removed', SCOPE)
        default:
            return pkg.applicable ? t('Available', SCOPE) : t('Not newer than the installed version', SCOPE)
    }
}

function packageStateVariant (pkg: MaintenancePackage) {
    if (pkg.state === 'applied') {
        return 'success'
    }
    if (pkg.state === 'available' && pkg.applicable) {
        return 'brand'
    }
    return 'neutral'
}

function formatSize (bytes: number) {
    if (bytes < 1024 * 1024) {
        return `${(bytes / 1024).toFixed(0)} KB`
    }
    return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

function shortHash (sha256: string) {
    return sha256.slice(0, 12)
}

function openUpload () {
    uploadError.value = ''
    uploadProgress.value = 0
    delete picked.package
    delete picked.manifest
    delete picked.signature
    showUpload.value = true
}

function closeUpload () {
    if (uploading.value) {
        return
    }
    showUpload.value = false
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
    uploadError.value = ''
    uploading.value = true
    uploadProgress.value = 0
    try {
        const pkg = await uploadPackage(
            { package: picked.package, manifest: picked.manifest, signature: picked.signature },
            fraction => { uploadProgress.value = Math.round(fraction * 100) },
        )
        showUpload.value = false
        showToast(t('Package {version} uploaded and verified.', SCOPE, { version: pkg.version }), 'success')
        await loadPackages()
    } catch (err) {
        // The server names the refusal: a signature that does not verify, a
        // version that is not newer, a package for another deployment.
        uploadError.value = errorDetail(err, t('The package could not be uploaded.', SCOPE))
    } finally {
        uploading.value = false
    }
}

function openRemove (pkg: MaintenancePackage) {
    removeError.value = ''
    removing.value = pkg
}

function closeRemove () {
    if (removeLoading.value) {
        return
    }
    removing.value = null
}

async function confirmRemove () {
    if (!removing.value) {
        return
    }
    removeError.value = ''
    removeLoading.value = true
    try {
        await deletePackage(removing.value.sha256)
        removing.value = null
        showToast(t('Package removed.', SCOPE), 'neutral')
        await loadPackages()
    } catch (err) {
        // A job may have picked the package up between the list and the click.
        removeError.value = errorDetail(err, t('The package could not be removed.', SCOPE))
    } finally {
        removeLoading.value = false
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
                                <div class="row-badges">
                                    <wa-badge appearance="outlined" :variant="packageStateVariant(pkg)">
                                        {{ packageStateLabel(pkg) }}
                                    </wa-badge>
                                    <span class="list-row-meta">
                                        <wa-relative-time :date="pkg.uploaded_at"></wa-relative-time>
                                        <template v-if="pkg.uploaded_by"> · {{ pkg.uploaded_by }}</template>
                                    </span>
                                </div>
                            </div>
                            <div v-if="canWrite" class="package-actions">
                                <wa-button
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
                <wa-switch v-else-if="argType(selected.args_schema.properties![name]!) === 'boolean'" v-wa="[argValues, name]">
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

    <wa-dialog :label="t('Upload package', SCOPE)" :open="showUpload" @wa-hide.self="closeUpload">
        <div class="maintenance-form">
            <wa-callout v-if="uploadError" variant="danger">
                {{ uploadError }}
            </wa-callout>
            <p class="maintenance-hint">
                {{ t('Select the three files of a release together: the archive (.tar.gz), its manifest (.manifest.json) and the signature (.manifest.sig). The package is verified before it is kept, and can be applied from this page afterwards.', SCOPE) }}
            </p>
            <wa-button appearance="filled-outlined" :disabled="uploading" variant="neutral" @click="triggerPackageSelect">
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
            <wa-progress-bar v-if="uploading" :value="uploadProgress"></wa-progress-bar>
        </div>
        <div slot="footer" class="form-actions">
            <wa-button
                appearance="filled-outlined"
                :disabled="uploading"
                variant="neutral"
                @click="closeUpload"
            >
                {{ t('Cancel', SCOPE) }}
            </wa-button>
            <wa-button
                appearance="filled-outlined"
                :disabled="!uploadReady"
                :loading="uploading"
                variant="brand"
                @click="confirmUpload"
            >
                {{ t('Upload', SCOPE) }}
            </wa-button>
        </div>
    </wa-dialog>

    <wa-dialog :label="t('Remove package', SCOPE)" :open="!!removing" @wa-hide.self="closeRemove">
        <wa-callout v-if="removeError" variant="danger">
            {{ removeError }}
        </wa-callout>
        <p class="dialog-text">
            {{ t('Remove the package for version {version}? Its files are deleted from the server; the record of jobs that used it stays.', SCOPE, { version: removing?.version ?? '' }) }}
        </p>
        <div slot="footer" class="form-actions">
            <wa-button
                appearance="filled-outlined"
                :disabled="removeLoading"
                variant="neutral"
                @click="closeRemove"
            >
                {{ t('Cancel', SCOPE) }}
            </wa-button>
            <wa-button
                appearance="filled-outlined"
                :loading="removeLoading"
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
</style>
