<script setup lang="ts">
import { ref, reactive, watch } from 'vue'
import { t } from '#i18n'
import { showToast } from '#lib/toast'
import { searchUsers, listGroups } from '#api/user'
import type { UserSearchResult, Group } from '#api/user'
import type { AccessRight, GrantAccessPayload } from '#api/library'

const SCOPE = 'AccessRightsPanel'

const props = defineProps<{
    accessRights: AccessRight[]
    grantFn: (payload: GrantAccessPayload) => Promise<AccessRight>
    revokeFn: (right: AccessRight) => Promise<void>
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
        const msg = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail
        grantError.value = msg ?? t('Failed to grant access.', SCOPE)
    } finally {
        grantLoading.value = false
    }
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
            <span class="access-target">{{ accessTargetLabel(right) }}</span>
            <span class="access-perms">{{ accessPermsLabel(right) }}</span>
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
                            'without any additional anonymization applied.',
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
