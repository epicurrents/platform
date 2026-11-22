<template>
    <main class="page-view">
        <header class="page-header">
            <h1>{{ t('DICOM Studies', SCOPE) }}</h1>
            <wa-button
                appearance="filled-outlined"
                size="s"
                variant="brand"
                @click="openUploadDialog"
            >
                {{ t('Upload DICOM files', SCOPE) }}
            </wa-button>
        </header>

        <wa-callout v-if="error" variant="danger">
            {{ error }}
        </wa-callout>

        <div v-if="loading" class="loading-state">
            <wa-spinner></wa-spinner>
            {{ t('Loading studies…', SCOPE) }}
        </div>

        <div v-else-if="studies.length === 0" class="empty-state">
            <wa-icon class="empty-state-icon" name="dicom"></wa-icon>
            <p>{{ t('No DICOM studies yet. Upload files to get started.', SCOPE) }}</p>
        </div>

        <table v-else class="studies-table">
            <thead>
                <tr>
                    <th>{{ t('Patient', SCOPE) }}</th>
                    <th>{{ t('Study Date', SCOPE) }}</th>
                    <th>{{ t('Description', SCOPE) }}</th>
                    <th>{{ t('Modalities', SCOPE) }}</th>
                    <th>{{ t('Instances', SCOPE) }}</th>
                    <th></th>
                </tr>
            </thead>
            <tbody>
                <tr v-for="study in studies" :key="study.hash">
                    <td>
                        <span class="patient-name">{{ study.patient_name || t('Unknown', SCOPE) }}</span>
                        <span v-if="study.patient_id" class="patient-id">{{ study.patient_id }}</span>
                    </td>
                    <td>{{ formatDate(study.study_date) }}</td>
                    <td>
                        <span class="study-description">{{ study.study_description || '—' }}</span>
                        <StoredAsUploadedTag v-if="studyStoredAsUploaded(study)"
                            :anchor="`dicom-study-as-uploaded-${study.hash}`"
                        ></StoredAsUploadedTag>
                    </td>
                    <td>
                        <wa-badge v-for="mod in modalityList(study)" :key="mod" variant="neutral">
                            {{ mod }}
                        </wa-badge>
                    </td>
                    <td class="instance-count">{{ study.num_instances }}</td>
                    <td class="actions">
                        <wa-button
                            appearance="plain"
                            size="s"
                            variant="brand"
                            @click="openInViewer(study.hash)"
                        >
                            {{ t('View', SCOPE) }}
                        </wa-button>
                        <wa-button appearance="plain" size="s" @click="openAddToDataset(study)">
                            {{ t('Add to dataset', SCOPE) }}
                        </wa-button>
                        <wa-button v-if="study.can_share"
                            appearance="plain"
                            size="s"
                            @click="openShareDialog(study)"
                        >
                            {{ t('Share', SCOPE) }}
                        </wa-button>
                        <wa-button v-if="canDeleteStudy(study)"
                            appearance="plain"
                            size="s"
                            variant="danger"
                            @click="confirmDelete(study)"
                        >
                            {{ t('Delete', SCOPE) }}
                        </wa-button>
                    </td>
                </tr>
            </tbody>
        </table>

        <!-- Upload dialog -->
        <wa-dialog
            :label="t('Upload DICOM Files', SCOPE)"
            :open="showUploadDialog"
            @wa-after-hide.self="closeUploadDialog"
            @wa-hide.self="onUploadDialogHide"
        >
            <div v-if="heldForReview.length" class="review-step">
                <wa-callout variant="warning">
                    {{
                        t(
                            '{count} file(s) may show identifying text or features in their content: every file '
                                + 'other than an original CT, MR or PET acquisition is held for this check. What the '
                                + 'files show is your responsibility: the platform clears identifying header fields '
                                + 'as a safeguard, but it cannot see text in an image.',
                            SCOPE,
                            { count: heldForReview.length }
                        )
                    }}
                </wa-callout>
                <wa-checkbox v-wa="[reviewForm, 'confirmed']">
                    {{
                        t(
                            'I have checked these images and they show no names, IDs, dates or other identifying text.',
                            SCOPE
                        )
                    }}
                </wa-checkbox>
            </div>
            <template v-else>
                <p>{{ t('Select one or more DICOM files (.dcm) or folders to upload.', SCOPE) }}</p>
                <p class="dialog-note">
                    {{
                        t(
                            'You are responsible for what the files contain. De-identify studies in the software '
                                + 'that exports them; the platform also clears identifying header fields, as a '
                                + 'safeguard rather than a guarantee.',
                            SCOPE
                        )
                    }}
                </p>
                <input
                    accept=".dcm,application/dicom"
                    class="file-input"
                    multiple
                    ref="fileInputRef"
                    type="file"
                    @change="onFilesSelected"
                />
                <wa-button appearance="plain" @click="chooseFiles">
                    {{ t('Choose files', SCOPE) }}
                </wa-button>
                <span v-if="selectedFiles.length" class="file-count">
                    {{ t('{count} file(s) selected', SCOPE, { count: selectedFiles.length }) }}
                </span>
            </template>

            <div class="dialog-footer" slot="footer">
                <wa-button appearance="plain" :disabled="uploading" @click="closeUploadDialog">
                    {{ t('Cancel', SCOPE) }}
                </wa-button>
                <wa-button v-if="heldForReview.length"
                    appearance="filled-outlined"
                    :disabled="!reviewForm.confirmed || uploading"
                    variant="brand"
                    @click="uploadReviewed"
                >
                    <wa-spinner v-if="uploading" slot="prefix"></wa-spinner>
                    {{ uploading ? t('Uploading…', SCOPE) : t('Upload checked images', SCOPE) }}
                </wa-button>
                <wa-button v-else
                    appearance="filled-outlined"
                    :disabled="selectedFiles.length === 0 || uploading"
                    variant="brand"
                    @click="startUpload"
                >
                    <wa-spinner v-if="uploading" slot="prefix"></wa-spinner>
                    {{ uploading ? t('Uploading…', SCOPE) : t('Upload', SCOPE) }}
                </wa-button>
            </div>
        </wa-dialog>

        <!-- Share dialog -->
        <wa-dialog
            class="share-dialog"
            :label="t('Share study', SCOPE)"
            :open="!!studyToShare"
            @wa-after-hide.self="closeShareDialog"
        >
            <div v-if="accessLoading" class="loading-state">
                <wa-spinner></wa-spinner>
                {{ t('Loading access rights…', SCOPE) }}
            </div>
            <template v-else-if="studyToShare">
                <wa-callout v-if="studyStoredAsUploaded(studyToShare)" class="as-uploaded-warning" variant="warning">
                    {{ asUploadedWarning() }}
                </wa-callout>
                <AccessRightsPanel
                    :grantFn="grantFn"
                    :infoMessage="shareNotice(studyToShare)"
                    :modes="SHARE_MODES"
                    :revokeFn="revokeFn"
                    v-model:accessRights="accessRights"
                ></AccessRightsPanel>
            </template>
        </wa-dialog>

        <!-- Add to dataset dialog -->
        <wa-dialog
            :label="t('Add to dataset', SCOPE)"
            :open="!!studyToAdd"
            @wa-after-hide.self="closeAddToDataset"
        >
            <div v-if="datasetsLoading" class="loading-state">
                <wa-spinner></wa-spinner>
                {{ t('Loading datasets…', SCOPE) }}
            </div>
            <template v-else>
                <wa-callout v-if="studyToAdd && studyStoredAsUploaded(studyToAdd)" variant="warning">
                    {{ asUploadedWarning() }}
                </wa-callout>
                <p v-if="!datasetChoices.length">
                    {{ t('You have no dataset this study can be added to.', SCOPE) }}
                </p>
                <wa-select v-else
                    :disabled="addingToDataset"
                    :label="t('Dataset', SCOPE)"
                    :placeholder="t('Choose a dataset', SCOPE)"
                    size="s"
                    v-wa="[addForm, 'datasetHash']"
                >
                    <wa-option v-for="dataset in datasetChoices" :key="dataset.hash" :value="dataset.hash">
                        {{ dataset.name }}
                    </wa-option>
                </wa-select>
                <p class="dialog-note">
                    {{
                        t(
                            'Everyone the dataset is shared with can read the study. Release-gated datasets cannot '
                                + 'hold studies.',
                            SCOPE
                        )
                    }}
                </p>
            </template>
            <div class="dialog-footer" slot="footer">
                <wa-button appearance="plain" @click="closeAddToDataset">
                    {{ t('Cancel', SCOPE) }}
                </wa-button>
                <wa-button
                    appearance="filled-outlined"
                    :disabled="!addForm.datasetHash || addingToDataset"
                    :loading="addingToDataset"
                    variant="brand"
                    @click="addToDataset"
                >
                    {{ t('Add', SCOPE) }}
                </wa-button>
            </div>
        </wa-dialog>

        <!-- Delete confirmation dialog -->
        <wa-dialog
            :label="t('Delete Study', SCOPE)"
            :open="!!studyToDelete"
            @wa-after-hide="cancelDelete"
        >
            <p>
                {{
                    t(
                        'Move the study for {patient} to the trash? It is removed permanently after the retention '
                            + 'period.',
                        SCOPE,
                        { patient: studyToDelete?.patient_name || t('Unknown patient', SCOPE) }
                    )
                }}
            </p>
            <div class="dialog-footer" slot="footer">
                <wa-button appearance="plain" @click="cancelDelete">
                    {{ t('Cancel', SCOPE) }}
                </wa-button>
                <wa-button
                    appearance="filled-outlined"
                    variant="danger"
                    @click="deleteStudy"
                >
                    {{ t('Delete', SCOPE) }}
                </wa-button>
            </div>
        </wa-dialog>
    </main>
</template>

<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '#stores/auth'
import { showToast } from '#lib/toast'
import { t } from '#i18n'
import AccessRightsPanel from '#components/AccessRightsPanel.vue'
import StoredAsUploadedTag from '#components/StoredAsUploadedTag.vue'
import { addDatasetItem, listAllDatasets, type AccessRight, type GrantAccessPayload } from '#api/library'
import type { DicomStudyOut, DicomUploadOut } from './api'
import { BURNED_IN_CONFIRMATION_REQUIRED, dicomApi, studyStoredAsUploaded } from './api'
import { datasetChoicesFor, type DatasetChoice } from './datasetChoices'

const SCOPE = 'DicomStudiesView'
/** A study is shared with users only; see `dicomApi.grantAccess`. */
const SHARE_MODES: ['user'] = ['user']

const router = useRouter()

const authStore = useAuthStore()

const studies = ref<DicomStudyOut[]>([])
const loading = ref(true)
const error = ref('')
const showUploadDialog = ref(false)
const uploading = ref(false)
const selectedFiles = ref<File[]>([])
// Files the server held back until the uploader confirms their pixels show no identifying text.
const heldForReview = ref<File[]>([])
const reviewForm = reactive({ confirmed: false })
const fileInputRef = ref<HTMLInputElement | null>(null)
const studyToDelete = ref<DicomStudyOut | null>(null)
const studyToShare = ref<DicomStudyOut | null>(null)
const studyToAdd = ref<DicomStudyOut | null>(null)
const datasetChoices = ref<DatasetChoice[]>([])
const datasetsLoading = ref(false)
const addingToDataset = ref(false)
const addForm = reactive({ datasetHash: '' })
const accessRights = ref<AccessRight[]>([])
const accessLoading = ref(false)

onMounted(loadStudies)

async function loadStudies() {
    loading.value = true
    error.value = ''
    try {
        studies.value = await dicomApi.listStudies()
    } catch {
        error.value = t('Failed to load studies. Please try again.', SCOPE)
    } finally {
        loading.value = false
    }
}

function canDeleteStudy(study: DicomStudyOut): boolean {
    // Mirrors the backend rule: author or superuser (ensure_can_write_object).
    return study.is_author || authStore.isSuperuser
}

function modalityList(study: DicomStudyOut): string[] {
    return study.modalities ? study.modalities.split(',') : []
}

function formatDate(dateStr: string): string {
    if (!dateStr || dateStr.length !== 8) {
        return dateStr || '—'
    }
    // DICOM date format: YYYYMMDD
    return `${dateStr.slice(0, 4)}-${dateStr.slice(4, 6)}-${dateStr.slice(6, 8)}`
}

function openInViewer(studyHash: string) {
    const { href } = router.resolve({ name: 'viewer', query: { dicom: studyHash } })
    window.open(href, '_blank')
}

function openUploadDialog() {
    showUploadDialog.value = true
}

/**
 * Close the upload dialog and drop the files held for review, unless an upload is out: its answer decides what the
 * dialog shows next, and a dialog closed meanwhile would keep the held files for the next time it opens.
 */
function closeUploadDialog() {
    if (uploading.value) {
        return
    }
    showUploadDialog.value = false
    heldForReview.value = []
    reviewForm.confirmed = false
}

/**
 * The upload dialog's `wa-hide` handler: refuse to close while an upload is out, otherwise close.
 * @param event - The dialog's `wa-hide`, which asked to close.
 */
function onUploadDialogHide(event: Event) {
    if (uploading.value) {
        event.preventDefault()
        return
    }
    closeUploadDialog()
}

function chooseFiles() {
    fileInputRef.value?.click()
}

/**
 * Clear the selection and the file input with it. The input keeps its value otherwise, and picking the same files
 * again would then fire no `change`.
 */
function clearSelection() {
    selectedFiles.value = []
    if (fileInputRef.value) {
        fileInputRef.value.value = ''
    }
}

function onFilesSelected(event: Event) {
    const input = event.target as HTMLInputElement
    selectedFiles.value = Array.from(input.files ?? [])
}

async function startUpload() {
    if (!selectedFiles.value.length) {
        return
    }
    await upload(selectedFiles.value, false)
}

async function uploadReviewed() {
    if (!heldForReview.value.length || !reviewForm.confirmed) {
        return
    }
    await upload(heldForReview.value, true)
}

/**
 * Upload *files* and report the outcome. Files the server holds back for a pixel check stay in the dialog, which
 * then asks for the confirmation instead of closing.
 * @param files - The batch, in the order the report's `index` refers to.
 * @param confirmBurnedIn - The uploader confirmed the batch's images show no identifying text.
 */
async function upload(files: File[], confirmBurnedIn: boolean) {
    if (uploading.value) {
        return
    }
    uploading.value = true
    let result: DicomUploadOut
    try {
        result = await dicomApi.uploadFiles(files, confirmBurnedIn)
    } catch {
        showToast(t('Upload failed. Please try again.', SCOPE), 'danger')
        return
    } finally {
        // Before the dialog is closed below: it announces the close as a hide, which is refused while uploading.
        uploading.value = false
    }
    const held = result.files
        .filter(f => f.code === BURNED_IN_CONFIRMATION_REQUIRED)
        .map(f => files[f.index])
    clearSelection()
    reviewForm.confirmed = false
    heldForReview.value = held
    if (!held.length) {
        showUploadDialog.value = false
    }
    reportUpload(result)
    await loadStudies()
}

function reportUpload(result: DicomUploadOut) {
    // Ingest is synchronous — the returned studies are immediately ready.
    if (result.accepted > 0) {
        showToast(
            t('Uploaded {count} file(s).', SCOPE, { count: result.accepted }),
            'success',
        )
    }
    if (result.warnings.length) {
        showToast(
            t(
                'A description is stored as written and may identify someone: {reason}',
                SCOPE,
                { reason: result.warnings[0].message }
            ),
            'warning',
        )
    }
    const refused = result.files.filter(f => !f.accepted && f.code !== BURNED_IN_CONFIRMATION_REQUIRED)
    if (refused.length) {
        showToast(
            t(
                '{count} file(s) were rejected: {reason}',
                SCOPE,
                { count: refused.length, reason: refused[0].error ?? '' }
            ),
            'warning',
        )
    }
}

async function openShareDialog(study: DicomStudyOut) {
    studyToShare.value = study
    accessRights.value = []
    accessLoading.value = true
    try {
        accessRights.value = await dicomApi.listAccess(study.hash)
    } catch {
        showToast(t('Failed to load access rights.', SCOPE), 'danger')
        studyToShare.value = null
    } finally {
        accessLoading.value = false
    }
}

/**
 * The warning shown before a study stored as uploaded is shared or added to a dataset: who receives it as it is.
 * A reader on de-identifying terms is refused such a study, so the readers it reaches are the others.
 */
function asUploadedWarning() {
    return t(
        'This study was stored without the platform\'s header pass. Readers on terms that are not de-identifying '
            + 'receive it as uploaded.',
        SCOPE
    )
}

function shareNotice(study: DicomStudyOut): string {
    if (studyStoredAsUploaded(study)) {
        return t(
            'The platform applied no header safeguard to this study: everyone you share it with receives the '
                + 'files as they were uploaded, patient details included.',
            SCOPE
        )
    }
    return t(
        'The platform cleared identifying header fields when this study was uploaded, as a safeguard. What the study '
            + 'contains remains the uploader\'s responsibility: the platform does not check the images for text, and '
            + 'for every file other than an original CT, MR or PET acquisition the uploader confirmed it shows none.',
        SCOPE
    )
}

async function openAddToDataset(study: DicomStudyOut) {
    studyToAdd.value = study
    addForm.datasetHash = ''
    datasetsLoading.value = true
    try {
        datasetChoices.value = datasetChoicesFor(
            await listAllDatasets(), authStore.user?.id ?? null, authStore.isSuperuser,
        )
    } catch {
        showToast(t('Failed to load datasets.', SCOPE), 'danger')
        studyToAdd.value = null
    } finally {
        datasetsLoading.value = false
    }
}

function closeAddToDataset() {
    studyToAdd.value = null
}

async function addToDataset() {
    const study = studyToAdd.value
    if (!study || !addForm.datasetHash) {
        return
    }
    addingToDataset.value = true
    try {
        const contentTypeId = await dicomApi.studyContentTypeId()
        await addDatasetItem(addForm.datasetHash, { content_type_id: contentTypeId, object_id: study.hash })
        showToast(t('Study added to the dataset.', SCOPE), 'neutral')
        studyToAdd.value = null
    } catch (e) {
        const status = (e as { response?: { status?: number } }).response?.status
        showToast(
            status === 409
                ? t('The study is already in that dataset, or the dataset cannot hold it.', SCOPE)
                : status === 403
                    ? t('You cannot add items to that dataset.', SCOPE)
                    : t('The study could not be added to the dataset.', SCOPE),
            'danger',
        )
    } finally {
        addingToDataset.value = false
    }
}

function closeShareDialog() {
    studyToShare.value = null
}

function grantFn(payload: GrantAccessPayload): Promise<AccessRight> {
    return dicomApi.grantAccess(studyToShare.value!.hash, payload)
}

function revokeFn(right: AccessRight): Promise<void> {
    return dicomApi.revokeAccess(studyToShare.value!.hash, right.id)
}

function confirmDelete(study: DicomStudyOut) {
    studyToDelete.value = study
}

function cancelDelete() {
    studyToDelete.value = null
}

async function deleteStudy() {
    if (!studyToDelete.value) {
        return
    }
    const hash = studyToDelete.value.hash
    studyToDelete.value = null
    try {
        await dicomApi.deleteStudy(hash)
        studies.value = studies.value.filter(s => s.hash !== hash)
        showToast(t('Study moved to trash.', SCOPE), 'neutral')
    } catch {
        showToast(t('Failed to delete study.', SCOPE), 'danger')
    }
}
</script>

<style scoped>
.share-dialog {
    --width: min(44rem, 95vw);
}

.dialog-note {
    color: var(--wa-color-text-quiet);
    font-size: var(--wa-font-size-s);
}

.as-uploaded-warning {
    margin-bottom: var(--wa-space-m);
}

.study-description {
    margin-right: var(--wa-space-xs);
}

.loading-state,
.empty-state {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 1rem;
    padding: 3rem;
    color: var(--wa-color-text-quiet);
}

.empty-state-icon {
    font-size: 3rem;
    opacity: 0.3;
}

.file-input {
    display: none;
}

.studies-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 0.9rem;
}

.studies-table th {
    text-align: left;
    padding: 0.5rem 0.75rem;
    border-bottom: 1px solid var(--wa-color-surface-border);
    color: var(--wa-color-text-quiet);
    font-weight: 600;
}

.studies-table td {
    padding: 0.6rem 0.75rem;
    border-bottom: 1px solid var(--wa-color-surface-border);
    vertical-align: middle;
}

.patient-name {
    display: block;
    font-weight: 500;
}

.patient-id {
    display: block;
    font-size: 0.8rem;
    color: var(--wa-color-text-quiet);
}

.instance-count {
    text-align: right;
}

.actions {
    display: flex;
    gap: 0.25rem;
    justify-content: flex-end;
}

.file-count {
    margin-left: 0.75rem;
    color: var(--wa-color-text-quiet);
    font-size: 0.9rem;
}

.review-step {
    display: flex;
    flex-direction: column;
    gap: 1rem;
}

.dialog-footer {
    display: flex;
    gap: 0.5rem;
    justify-content: flex-end;
}
</style>
