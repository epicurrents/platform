<script setup lang="ts">
import { computed, ref, reactive, watch } from 'vue'
import { t } from '#i18n'
import { errorDetail } from '#lib/http'
import { showToast } from '#lib/toast'
import { searchUsers, listGroups } from '#api/user'
import type { UserSearchResult, Group } from '#api/user'
import type { AccessRight, AssessmentPayload, GrantAccessPayload } from '#api/library'
import { joinAssessmentReference, splitAssessmentReference, type AssessmentKind } from '#lib/assessment'

const SCOPE = 'AccessRightsPanel'

const props = defineProps<{
    accessRights: AccessRight[]
    grantFn: (payload: GrantAccessPayload) => Promise<AccessRight>
    revokeFn: (right: AccessRight) => Promise<void>
    /** When given, each grant whose `can_assess` is set gets an assessment control; see epicurrents.assessment. */
    assessFn?: (right: AccessRight, payload: AssessmentPayload) => Promise<AccessRight>
    infoMessage?: string
    readPermLabel?: string
}>()

const emit = defineEmits<{
    'update:accessRights': [rights: AccessRight[]]
}>()

// ── Dialog state ──────────────────────────────────────────────────────────────

const showGrantAccess = ref(false)
const grantMode = ref<'user' | 'group' | 'token'>('user')
const grantPerms = reactive({ canRead: true, canWrite: false, canShare: false })
const grantLoading = ref(false)
const grantError = ref<string | null>(null)

// ── User search ───────────────────────────────────────────────────────────────

const userQuery = ref('')
const userResults = ref<UserSearchResult[]>([])
const userSearchLoading = ref(false)
const selectedUser = ref<UserSearchResult | null>(null)
let userSearchTimer: ReturnType<typeof setTimeout> | undefined

watch(userQuery, (q) => {
    clearTimeout(userSearchTimer)
    userResults.value = []
    if (q.trim().length < 2) {
        return
    }
    userSearchTimer = setTimeout(async () => {
        userSearchLoading.value = true
        try {
            userResults.value = await searchUsers(q.trim())
        } catch {
            userResults.value = []
        } finally {
            userSearchLoading.value = false
        }
    }, 300)
})

function selectUser(user: UserSearchResult) {
    selectedUser.value = user
    userQuery.value = ''
    userResults.value = []
}

function clearUser() {
    selectedUser.value = null
    userQuery.value = ''
}

// ── Group list ────────────────────────────────────────────────────────────────

const groups = ref<Group[]>([])
const groupsLoading = ref(false)
const groupSelect = reactive({ value: '' })

async function loadGroups() {
    if (groups.value.length) {
        return
    }
    groupsLoading.value = true
    try {
        groups.value = await listGroups()
    } catch {
        groups.value = []
    } finally {
        groupsLoading.value = false
    }
}

watch(grantMode, (mode) => {
    if (mode === 'group') {
        loadGroups()
    }
})

// ── Token ─────────────────────────────────────────────────────────────────────

const tokenInput = reactive({ value: '' })
const tokenSettings = reactive({ originalData: false })
const showOriginalDataInfo = ref(false)

// ── Open / close ──────────────────────────────────────────────────────────────

function openGrantAccess() {
    grantMode.value = 'user'
    grantPerms.canRead = true
    grantPerms.canWrite = false
    grantPerms.canShare = false
    grantError.value = null
    userQuery.value = ''
    userResults.value = []
    selectedUser.value = null
    groupSelect.value = ''
    tokenInput.value = ''
    tokenSettings.originalData = false
    showOriginalDataInfo.value = false
    showGrantAccess.value = true
}

function closeGrantAccess() {
    showGrantAccess.value = false
}

defineExpose({ openGrantAccess })

// ── Submit ────────────────────────────────────────────────────────────────────

async function submitGrant() {
    grantError.value = null

    if (!grantPerms.canRead && !grantPerms.canWrite && !grantPerms.canShare) {
        grantError.value = t('At least one permission must be selected.', SCOPE)
        return
    }

    const isToken = grantMode.value === 'token'
    const payload: GrantAccessPayload = {
        can_read: grantPerms.canRead,
        can_write: isToken ? false : grantPerms.canWrite,
        can_share: isToken ? false : grantPerms.canShare,
        apply_middleware: isToken ? !tokenSettings.originalData : undefined,
    }

    if (grantMode.value === 'user') {
        if (!selectedUser.value) {
            grantError.value = t('Select a user to grant access to.', SCOPE)
            return
        }
        payload.access_target_id = selectedUser.value.id
    } else if (grantMode.value === 'group') {
        if (!groupSelect.value) {
            grantError.value = t('Select a group to grant access to.', SCOPE)
            return
        }
        payload.access_target_group_id = parseInt(groupSelect.value)
    } else {
        const token = tokenInput.value.trim()
        if (!token) {
            grantError.value = t('Share token is required.', SCOPE)
            return
        }
        payload.public_share_token = token
    }

    grantLoading.value = true
    try {
        const right = await props.grantFn(payload)
        emit('update:accessRights', [...props.accessRights, right])
        showGrantAccess.value = false
    } catch (e: unknown) {
        grantError.value = errorDetail(e, t('Failed to grant access.', SCOPE))
    } finally {
        grantLoading.value = false
    }
}

// ── Assessment ────────────────────────────────────────────────────────────────

const showAssessment = ref(false)
const assessmentTarget = ref<AccessRight | null>(null)
const assessmentForm = reactive({ kind: '' as AssessmentKind, reference: '', date: '' })
const assessmentLoading = ref(false)
const assessmentError = ref<string | null>(null)
const showAssessmentHelp = ref(false)

/** Today as `YYYY-MM-DD` in local time: the latest date an assessment can have been made, which the server enforces. */
function todayIso () {
    const now = new Date()
    const month = String(now.getMonth() + 1).padStart(2, '0')
    const day = String(now.getDate()).padStart(2, '0')
    return `${now.getFullYear()}-${month}-${day}`
}

const assessmentMaxDate = ref(todayIso())

function onAssessmentHelpShow () {
    showAssessmentHelp.value = true
}

function onAssessmentHelpHide () {
    showAssessmentHelp.value = false
}

const assessmentKindOptions = computed(() => [
    { value: '', label: t('Other document or link', SCOPE) },
    { value: 'assessment', label: t('Written contextual assessment', SCOPE) },
    { value: 'dpia', label: t('Data protection impact assessment', SCOPE) },
    { value: 'agreement', label: t('Data-sharing agreement with a re-identification prohibition', SCOPE) },
    { value: 'published', label: t('Anonymity statement of a published dataset', SCOPE) },
])

function openAssessment(right: AccessRight) {
    assessmentTarget.value = right
    const stored = splitAssessmentReference(right.assessment_reference ?? '')
    assessmentForm.kind = stored.kind
    assessmentForm.reference = stored.identifier
    assessmentForm.date = right.assessment_date ?? ''
    assessmentError.value = null
    assessmentMaxDate.value = todayIso()
    showAssessmentHelp.value = false
    showAssessment.value = true
}

function closeAssessment() {
    showAssessment.value = false
}

async function saveAssessment(clear = false) {
    const right = assessmentTarget.value
    if (!right || !props.assessFn) {
        return
    }
    const reference = clear ? '' : joinAssessmentReference(assessmentForm.kind, assessmentForm.reference)
    const date = clear ? '' : assessmentForm.date
    if ((reference === '') !== (date === '')) {
        assessmentError.value = t('Give both a reference and a date, or clear the assessment.', SCOPE)
        return
    }
    if (date > assessmentMaxDate.value) {
        assessmentError.value = t('The date cannot be in the future.', SCOPE)
        return
    }
    assessmentError.value = null
    assessmentLoading.value = true
    try {
        const updated = await props.assessFn(right, { assessment_reference: reference, assessment_date: date || null })
        emit('update:accessRights', props.accessRights.map(r => (r.id === updated.id ? updated : r)))
        showAssessment.value = false
        showToast(
            reference ? t('Assessment recorded.', SCOPE) : t('Assessment cleared.', SCOPE),
            'neutral',
        )
    } catch (e: unknown) {
        assessmentError.value = errorDetail(e, t('Failed to save the assessment.', SCOPE))
    } finally {
        assessmentLoading.value = false
    }
}

function assessmentLabel(right: AccessRight): string {
    if (!right.assessment_reference) {
        return ''
    }
    return t('Assessed {date}: {reference}', SCOPE, {
        date: right.assessment_date ?? '',
        reference: right.assessment_reference,
    })
}

// ── Revoke ────────────────────────────────────────────────────────────────────

async function revokeAccess(right: AccessRight) {
    try {
        await props.revokeFn(right)
        emit('update:accessRights', props.accessRights.filter(r => r.id !== right.id))
        showToast(t('Access revoked.', SCOPE), 'neutral')
    } catch {
        showToast(t('Failed to revoke access.', SCOPE), 'danger')
    }
}

// ── Display helpers ───────────────────────────────────────────────────────────

function accessTargetLabel(right: AccessRight): string {
    if (right.public_share_token) {
        return t('Token: {token}', SCOPE, { token: right.public_share_token })
    }
    if (right.access_target_username) {
        return right.access_target_username
    }
    if (right.access_target_group_name) {
        return right.access_target_group_name
    }
    if (right.access_target_id != null) {
        return t('User #{id}', SCOPE, { id: right.access_target_id })
    }
    if (right.access_target_group_id != null) {
        return t('Group #{id}', SCOPE, { id: right.access_target_group_id })
    }
    return t('Unknown', SCOPE)
}

function accessPermsLabel(right: AccessRight): string {
    const perms = []
    if (right.can_read) {
        perms.push(t('read', SCOPE))
    }
    if (right.can_write) {
        perms.push(t('write', SCOPE))
    }
    if (right.can_share) {
        perms.push(t('share', SCOPE))
    }
    return perms.join(' · ')
}

function userDisplayName(user: UserSearchResult): string {
    const full = [user.first_name, user.last_name].filter(Boolean).join(' ')
    return full ? `${user.username} (${full})` : user.username
}
</script>

<template>
    <div class="panel-header">
        <h2>{{ t('Shared access', SCOPE) }}</h2>
        <wa-button
            appearance="plain"
            size="s"
            variant="brand"
            @click="openGrantAccess"
        >
            <wa-icon name="share" slot="start"></wa-icon>
            {{ t('Grant access', SCOPE) }}
        </wa-button>
    </div>

    <wa-callout v-if="infoMessage" class="info-callout" variant="neutral">
        {{ infoMessage }}
    </wa-callout>

    <p v-else-if="!accessRights.length" class="empty-state">
        {{ t('No access grants yet.', SCOPE) }}
    </p>

    <div v-else>
        <div v-for="right in accessRights" :key="right.id" class="access-row">
            <wa-badge v-if="right.public_share_token" pill variant="neutral">
                {{ t('Token', SCOPE) }}
            </wa-badge>
            <wa-badge v-else-if="right.access_target_id != null" pill variant="brand">
                {{ t('User', SCOPE) }}
            </wa-badge>
            <wa-badge v-else pill variant="success">{{ t('Group', SCOPE) }}</wa-badge>
            <span class="access-target">
                {{ accessTargetLabel(right) }}
                <span v-if="right.assessment_reference" class="access-assessment">{{ assessmentLabel(right) }}</span>
            </span>
            <span class="access-perms">{{ accessPermsLabel(right) }}</span>
            <wa-button v-if="assessFn && right.can_assess"
                appearance="plain"
                size="s"
                :title="t('Contextual assessment', SCOPE)"
                @click="openAssessment(right)"
            >
                <wa-icon name="circle-check"></wa-icon>
            </wa-button>
            <wa-button
                appearance="plain"
                size="s"
                :title="t('Revoke', SCOPE)"
                variant="danger"
                @click="revokeAccess(right)"
            >
                <wa-icon name="xmark"></wa-icon>
            </wa-button>
        </div>
    </div>

    <!-- Assessment dialog -->
    <wa-dialog
        :label="t('Contextual assessment', SCOPE)"
        :open="showAssessment"
        @wa-hide.self="closeAssessment"
    >
        <div class="dialog-form">
            <wa-callout v-if="assessmentError" variant="danger">{{ assessmentError }}</wa-callout>
            <p class="search-hint">
                {{ t(
                    'Where you have documented that this recipient cannot identify anyone from what they receive, ' +
                    'point at that document here and say when it was made. The platform keeps the pointer beside ' +
                    'the grant and reports when it is due for re-checking; it does not make the finding, and the ' +
                    'recipient never sees this.',
                    SCOPE
                ) }}
            </p>
            <wa-details
                :open="showAssessmentHelp"
                :summary="t('What to record here', SCOPE)"
                @wa-show.self="onAssessmentHelpShow"
                @wa-hide.self="onAssessmentHelpHide"
            >
                <p class="assessment-help">
                    {{ t(
                        'Sharing through this platform hands the recipient pseudonymised personal data: the ' +
                        'identification in the file is removed, the signal itself is not. Under the EDPB ' +
                        'anonymisation guidelines a sharer may still conclude that the data is anonymous for ' +
                        'one particular recipient, by assessing what that recipient could reasonably do to ' +
                        'identify someone. That conclusion is yours to reach and to write down; this form only ' +
                        'records where you wrote it.',
                        SCOPE
                    ) }}
                </p>
                <p class="assessment-help">
                    {{ t(
                        'The document you point at should name the recipient and whoever stands behind them, ' +
                        'say why they cannot reach the original recordings or a reference recording of the ' +
                        'same person, state that they may not pass the data on, and carry a date. A written ' +
                        'contextual assessment following the guidelines is the direct form; a data protection ' +
                        'impact assessment or a signed data-sharing agreement with a re-identification ' +
                        'prohibition can carry the same finding.',
                        SCOPE
                    ) }}
                </p>
                <p class="assessment-help">
                    {{ t(
                        'Where the recordings come from a published dataset, the publisher\'s own anonymity ' +
                        'statement can be the document: a recipient who could fetch the same data from the ' +
                        'publisher gains nothing from your copy. Point at the dataset\'s citation or DOI and ' +
                        'where the statement lives. Relying on it makes the finding yours for this recipient, ' +
                        'and it needs re-checking like any other, since datasets are withdrawn when someone ' +
                        'is identified in them. The platform still labels what it serves as pseudonymised, ' +
                        'because it does not know per recording where the data came from.',
                        SCOPE
                    ) }}
                </p>
                <p class="assessment-help">
                    {{ t(
                        'The kind names what the document is and is stored in front of the reference. The ' +
                        'reference is whatever lets you find the document again: an identifier in your records ' +
                        'or a link. The date is when the assessment was made, or last re-run; update it each ' +
                        'time you re-check the finding, which the guidelines ask for after any security ' +
                        'incident and as capabilities change.',
                        SCOPE
                    ) }}
                </p>
            </wa-details>
            <wa-select
                :disabled="assessmentLoading"
                :label="t('Kind of document', SCOPE)"
                size="s"
                v-wa="[assessmentForm, 'kind']"
            >
                <wa-option v-for="option in assessmentKindOptions" :key="option.value" :value="option.value">
                    {{ option.label }}
                </wa-option>
            </wa-select>
            <wa-input
                :disabled="assessmentLoading"
                :label="t('Reference', SCOPE)"
                :placeholder="t('e.g. an identifier in your records or a link', SCOPE)"
                size="s"
                type="text"
                v-wa="[assessmentForm, 'reference']"
            ></wa-input>
            <wa-input
                :disabled="assessmentLoading"
                :label="t('Date made or last re-run', SCOPE)"
                :max="assessmentMaxDate"
                size="s"
                type="date"
                v-wa="[assessmentForm, 'date']"
            ></wa-input>
        </div>

        <div slot="footer" class="form-actions">
            <wa-button v-if="assessmentTarget?.assessment_reference"
                appearance="plain"
                :disabled="assessmentLoading"
                variant="danger"
                @click="saveAssessment(true)"
            >
                {{ t('Clear', SCOPE) }}
            </wa-button>
            <wa-button
                appearance="filled-outlined"
                :disabled="assessmentLoading"
                variant="neutral"
                @click="closeAssessment"
            >
                {{ t('Cancel', SCOPE) }}
            </wa-button>
            <wa-button
                appearance="filled-outlined"
                :loading="assessmentLoading"
                variant="brand"
                @click="saveAssessment()"
            >
                {{ t('Save', SCOPE) }}
            </wa-button>
        </div>
    </wa-dialog>

    <!-- Grant access dialog -->
    <wa-dialog
        :label="t('Grant access', SCOPE)"
        :open="showGrantAccess"
        @wa-hide.self="closeGrantAccess"
    >
        <div class="dialog-form">
            <wa-callout v-if="grantError" variant="danger">{{ grantError }}</wa-callout>

            <!-- Mode tabs -->
            <div class="mode-tabs" role="tablist">
                <button
                    class="mode-tab"
                    :class="{ active: grantMode === 'user' }"
                    role="tab"
                    type="button"
                    @click="grantMode = 'user'"
                >
                    <wa-icon name="user"></wa-icon>
                    {{ t('User', SCOPE) }}
                </button>
                <button
                    class="mode-tab"
                    :class="{ active: grantMode === 'group' }"
                    role="tab"
                    type="button"
                    @click="grantMode = 'group'"
                >
                    <wa-icon name="users"></wa-icon>
                    {{ t('Group', SCOPE) }}
                </button>
                <button
                    class="mode-tab"
                    :class="{ active: grantMode === 'token' }"
                    role="tab"
                    type="button"
                    @click="grantMode = 'token'"
                >
                    <wa-icon name="key"></wa-icon>
                    {{ t('Token', SCOPE) }}
                </button>
            </div>

            <!-- User mode -->
            <template v-if="grantMode === 'user'">
                <div v-if="selectedUser" class="selected-target">
                    <wa-icon class="selected-icon" name="user"></wa-icon>
                    <span class="selected-label">{{ userDisplayName(selectedUser) }}</span>
                    <wa-button
                        appearance="plain"
                        size="s"
                        @click="clearUser"
                    >
                        <wa-icon name="xmark"></wa-icon>
                    </wa-button>
                </div>
                <template v-else>
                    <wa-input
                        :disabled="grantLoading"
                        :label="t('Search users', SCOPE)"
                        :placeholder="t('Username or name (type at least 2 characters)…', SCOPE)"
                        size="s"
                        type="text"
                        :value="userQuery"
                        @input="userQuery = ($event.target as HTMLInputElement).value"
                    ></wa-input>
                    <wa-spinner v-if="userSearchLoading" class="search-spinner"></wa-spinner>
                    <div v-else-if="userResults.length" class="search-results">
                        <div
                            v-for="user in userResults"
                            :key="user.id"
                            class="search-result-row"
                            type="button"
                            @click="selectUser(user)"
                        >
                            {{ userDisplayName(user) }}
                        </div>
                    </div>
                    <p v-else-if="userQuery.trim().length >= 2" class="search-empty">
                        {{ t('No users found.', SCOPE) }}
                    </p>
                </template>
            </template>

            <!-- Group mode -->
            <template v-else-if="grantMode === 'group'">
                <wa-select
                    :disabled="grantLoading || groupsLoading"
                    :label="t('Group', SCOPE)"
                    :placeholder="groupsLoading ? t('Loading…', SCOPE) : t('Select or type to filter…', SCOPE)"
                    size="s"
                    v-wa="[groupSelect, 'value']"
                >
                    <wa-option
                        v-for="group in groups"
                        :key="group.id"
                        :value="String(group.id)"
                    >
                        {{ group.name }}
                    </wa-option>
                </wa-select>
                <p v-if="!groupsLoading && !groups.length" class="search-empty">
                    {{ t('No groups available.', SCOPE) }}
                </p>
            </template>

            <!-- Token mode -->
            <template v-else>
                <wa-input
                    :disabled="grantLoading"
                    :hint="t('Anyone who knows this token will have read permission.', SCOPE)"
                    :label="t('Share token', SCOPE)"
                    :placeholder="t('e.g. study-2026', SCOPE)"
                    size="s"
                    type="text"
                    v-wa="[tokenInput, 'value']"
                ></wa-input>
            </template>

            <!-- Permissions -->
            <div class="perm-form">
                <p class="perm-heading">{{ t('Permissions', SCOPE) }}</p>
                <wa-checkbox
                    :disabled="grantLoading || grantMode === 'token'"
                    v-wa="[grantPerms, 'canRead']"
                >
                    {{ readPermLabel ?? t('Read', SCOPE) }}
                </wa-checkbox>
                <template v-if="grantMode !== 'token'">
                    <wa-checkbox
                        :disabled="grantLoading"
                        v-wa="[grantPerms, 'canWrite']"
                    >
                        {{ t('Write', SCOPE) }}
                    </wa-checkbox>
                    <wa-checkbox
                        :disabled="grantLoading"
                        v-wa="[grantPerms, 'canShare']"
                    >
                        {{ t('Share (can grant further access)', SCOPE) }}
                    </wa-checkbox>
                </template>
                <template v-else>
                    <div class="perm-row">
                        <wa-checkbox
                            :disabled="grantLoading"
                            v-wa="[tokenSettings, 'originalData']"
                        >
                            {{ t('Original data', SCOPE) }}
                        </wa-checkbox>
                        <wa-button
                            appearance="plain"
                            class="info-toggle"
                            size="s"
                            variant="warning"
                            @click="showOriginalDataInfo = !showOriginalDataInfo"
                        >
                            <wa-icon name="triangle-exclamation" slot="start"></wa-icon>
                            {{ t('More information', SCOPE) }}
                        </wa-button>
                    </div>
                    <wa-callout v-if="showOriginalDataInfo" variant="warning">
                        {{ t(
                            'Enabling this allows the share token holder to read the data exactly as it is stored, ' +
                            'without the de-identification pass applied.',
                            SCOPE
                        ) }}
                    </wa-callout>
                </template>
            </div>
        </div>

        <div slot="footer" class="form-actions">
            <wa-button
                appearance="filled-outlined"
                :disabled="grantLoading"
                variant="neutral"
                @click="closeGrantAccess"
            >
                {{ t('Cancel', SCOPE) }}
            </wa-button>
            <wa-button
                appearance="filled-outlined"
                :loading="grantLoading"
                variant="brand"
                @click="submitGrant"
            >
                {{ t('Grant access', SCOPE) }}
            </wa-button>
        </div>
    </wa-dialog>
</template>

<style scoped>
.panel-header {
    align-items: center;
    display: flex;
    justify-content: space-between;
    margin-bottom: var(--wa-space-s);
    margin-top: var(--wa-space-s);
}

.panel-header h2 {
    color: var(--wa-color-text-normal);
    font-size: var(--wa-font-size-m);
    font-weight: 600;
    margin: 0;
}

.info-callout {
    font-size: 0.875rem;
    margin-bottom: var(--wa-space-s);
}

.access-row {
    align-items: center;
    border-bottom: var(--wa-border-width-s) solid var(--wa-color-surface-border);
    display: flex;
    font-size: var(--wa-font-size-s);
    gap: var(--wa-space-s);
    padding: var(--wa-space-xs) var(--wa-space-s);
}

.access-row:last-child {
    border-bottom: none;
}

.access-target {
    flex: 1;
    font-weight: 500;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}

.access-perms {
    color: var(--wa-color-text-quiet);
    flex-shrink: 0;
    font-size: var(--wa-font-size-s);
}

.assessment-help {
    color: var(--wa-color-text-quiet);
    font-size: var(--wa-font-size-s);
    margin: 0 0 var(--wa-space-s);
}

.access-assessment {
    color: var(--wa-color-text-quiet);
    display: block;
    font-size: var(--wa-font-size-xs);
    font-weight: 400;
    overflow: hidden;
    text-overflow: ellipsis;
}

.empty-state {
    color: var(--wa-color-text-quiet);
    font-size: var(--wa-font-size-s);
    margin: var(--wa-space-s) 0;
}

/* Dialog */

.dialog-form {
    display: flex;
    flex-direction: column;
    gap: var(--wa-space-m);
}

/* Mode tabs */

.mode-tabs {
    border-bottom: var(--wa-border-width-s) solid var(--wa-color-surface-border);
    display: flex;
    gap: 0;
}

.mode-tab {
    align-items: center;
    background: none;
    border: none;
    border-bottom: 2px solid transparent;
    border-radius: 0;
    color: var(--wa-color-text-quiet);
    cursor: pointer;
    display: flex;
    flex: 1;
    font-size: var(--wa-font-size-s);
    gap: var(--wa-space-xs);
    justify-content: center;
    margin-bottom: -1px;
    padding: var(--wa-space-xs) var(--wa-space-s);
    transition: color 0.15s, border-color 0.15s;
}

.mode-tab:hover {
    color: var(--wa-color-text-normal);
}

.mode-tab.active {
    border-bottom-color: var(--wa-color-brand-fill-loud);
    color: var(--wa-color-brand-fill-loud);
    font-weight: 600;
}

/* User search */

.search-spinner {
    align-self: center;
    display: block;
}

.search-results {
    border: var(--wa-border-width-s) solid var(--wa-color-surface-border);
    border-radius: var(--wa-border-radius-m);
    display: flex;
    flex-direction: column;
    max-height: 180px;
    overflow-y: auto;
}

.search-result-row {
    background: none;
    border: none;
    cursor: pointer;
    font-size: var(--wa-font-size-s);
    padding: var(--wa-space-xs) var(--wa-space-s);
    text-align: left;
}

.search-result-row:hover {
    background: var(--wa-color-neutral-fill-subtle);
}

.search-empty,
.search-hint {
    color: var(--wa-color-text-quiet);
    font-size: var(--wa-font-size-s);
    margin: 0;
}

/* Selected target chip */

.selected-target {
    align-items: center;
    background: var(--wa-color-neutral-fill-subtle);
    border: var(--wa-border-width-s) solid var(--wa-color-surface-border);
    border-radius: var(--wa-border-radius-m);
    display: flex;
    gap: var(--wa-space-xs);
    padding: var(--wa-space-xs) var(--wa-space-s);
}

.selected-icon {
    color: var(--wa-color-text-quiet);
    flex-shrink: 0;
}

.selected-label {
    flex: 1;
    font-size: var(--wa-font-size-s);
    font-weight: 500;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}

/* Permissions */

.perm-form {
    display: flex;
    flex-direction: column;
    gap: var(--wa-space-xs);
}

.perm-heading {
    font-size: var(--wa-font-size-s);
    font-weight: 500;
    margin: 0;
}

.perm-row {
    align-items: center;
    display: flex;
    gap: var(--wa-space-xs);
    max-height: 1.5rem;
}

.perm-form wa-callout {
    margin: 0;
}

.info-toggle {
    background: none;
    border: none;
    color: var(--wa-color-text-quiet);
    cursor: pointer;
    font-size: var(--wa-font-size-s);
    padding: 0;
    text-decoration: underline;
}
.info-toggle:hover {
    color: var(--wa-color-text-normal);
}

.form-actions {
    display: flex;
    gap: var(--wa-space-s);
    justify-content: flex-end;
}
</style>
