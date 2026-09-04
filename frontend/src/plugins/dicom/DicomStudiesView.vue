<template>
    <div class="dicom-studies-view">
        <header class="page-header">
            <h1>{{ t('DICOM Studies', SCOPE) }}</h1>
            <wa-button
                appearance="filled-outlined"
                variant="brand"
                @click="openUploadDialog"
            >
                {{ t('Upload DICOM files', SCOPE) }}
            </wa-button>
        </header>

        <wa-alert v-if="error" open variant="danger">
            {{ error }}
        </wa-alert>

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
                    <td>{{ study.study_description || '—' }}</td>
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
                        <wa-button
                            v-if="canDeleteStudy(study)"
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
            @wa-after-hide="closeUploadDialog"
        >
            <p>{{ t('Select one or more DICOM files (.dcm) or folders to upload.', SCOPE) }}</p>
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

            <div class="dialog-footer" slot="footer">
                <wa-button appearance="plain" @click="closeUploadDialog">
                    {{ t('Cancel', SCOPE) }}
                </wa-button>
                <wa-button
                    appearance="filled-outlined"
                    variant="brand"
                    :disabled="selectedFiles.length === 0 || uploading"
                    @click="startUpload"
                >
                    <wa-spinner v-if="uploading" slot="prefix"></wa-spinner>
                    {{ uploading ? t('Uploading…', SCOPE) : t('Upload', SCOPE) }}
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
                        'Move the study for {patient} to the trash? It is removed permanently after the retention period.',
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
    </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useAuthStore } from '#stores/auth'
import { showToast } from '#lib/toast'
import { t } from '#i18n'
import type { DicomStudyOut } from './api'
import { dicomApi } from './api'

const SCOPE = 'DicomStudiesView'

const authStore = useAuthStore()

const studies = ref<DicomStudyOut[]>([])
const loading = ref(true)
const error = ref('')
const showUploadDialog = ref(false)
const uploading = ref(false)
const selectedFiles = ref<File[]>([])
const fileInputRef = ref<HTMLInputElement | null>(null)
const studyToDelete = ref<DicomStudyOut | null>(null)

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
    const viewerUrl = dicomApi.ohifViewerUrl(studyHash)
    window.open(viewerUrl, '_blank')
}

function openUploadDialog() {
    showUploadDialog.value = true
}

function closeUploadDialog() {
    showUploadDialog.value = false
}

function chooseFiles() {
    fileInputRef.value?.click()
}

function onFilesSelected(event: Event) {
    const input = event.target as HTMLInputElement
    selectedFiles.value = Array.from(input.files ?? [])
}

async function startUpload() {
    if (!selectedFiles.value.length) {
        return
    }
    uploading.value = true
    try {
        const result = await dicomApi.uploadFiles(selectedFiles.value)
        showUploadDialog.value = false
        selectedFiles.value = []
        // Ingest is synchronous — the returned studies are immediately ready.
        showToast(
            t('Uploaded {count} file(s).', SCOPE, { count: result.accepted }),
            'success',
        )
        if (result.rejected > 0) {
            const firstError = result.files.find(f => !f.accepted)?.error ?? ''
            showToast(
                t(
                    '{count} file(s) were rejected: {reason}',
                    SCOPE,
                    { count: result.rejected, reason: firstError }
                ),
                'warning',
            )
        }
        await loadStudies()
    } catch {
        showToast(t('Upload failed. Please try again.', SCOPE), 'danger')
    } finally {
        uploading.value = false
    }
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
.dicom-studies-view {
    display: flex;
    flex-direction: column;
    gap: 1.5rem;
    padding: 1.5rem;
    max-width: 1200px;
    margin: 0 auto;
}

.page-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
}

.page-header h1 {
    margin: 0;
    font-size: 1.5rem;
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

.dialog-footer {
    display: flex;
    gap: 0.5rem;
    justify-content: flex-end;
}
</style>
