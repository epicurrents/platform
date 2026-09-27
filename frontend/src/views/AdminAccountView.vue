<script setup lang="ts">
/**
 * Account detail — edit one account's fields, membership, password and second factor.
 *
 * Username is deliberately not editable: it is what an operator recognises an
 * account by in a log line, and renaming would silently rewrite the meaning of
 * every historical line that names it. There is no delete control either —
 * `erase_user` on the host is the sanctioned path, because it also unlinks
 * owned recording and media files that FK cascade never touches.
 *
 * Group membership is rendered as a checkbox per group rather than a multiple
 * `wa-select`: the `v-wa` directive writes a scalar onto the element's `value`,
 * which a multiple select would need an array for.
 *
 * Changes that grant a way in — a staff tier, a new address, an activation, a
 * group added — ask for the operator's own credentials through the shared
 * step-up prompt, and only those: a rename saves in one click. Setting a
 * password and clearing a second factor always ask, inside their own dialogs,
 * and neither is offered on the operator's own account, where the server
 * refuses both and the profile page is the route.
 *
 * @package    epicurrents-platform
 */
import { computed, onMounted, reactive, ref } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import StepUpFields from '#components/StepUpFields.vue'
import {
    fetchAccount,
    listGroups,
    listRoleProviders,
    resendAccountInvitation,
    resetAccountTwoFactor,
    setAccountGroups,
    setAccountPassword,
    updateAccount,
    type Account,
    type GroupDetail,
    type RoleProvider,
} from '#api/admin'
import { useDialog } from '#composables/useDialog'
import { withStepUp } from '#composables/useStepUpPrompt'
import { t } from '#i18n'
import { formatDate, formatDateTime } from '#lib/datetime'
import { errorDetail } from '#lib/http'
import { accountUpdateNeedsStepUp, membershipAddsAny, stepUpBody } from '#lib/stepUp'
import { showToast } from '#lib/toast'
import { setPageTitle } from '#router'
import { useAuthStore } from '#stores/auth'

const SCOPE = 'AdminAccountView'

const authStore = useAuthStore()
const route = useRoute()
const router = useRouter()

const accountId = Number(route.params.id)

const account = ref<Account | null>(null)
const groups = ref<GroupDetail[]>([])
const roleProviders = ref<RoleProvider[]>([])
const loading = ref(true)
const loadError = ref('')

const savingDetails = ref(false)
const savingGroups = ref(false)
const detailsError = ref('')
const form = reactive({
    email: '',
    firstName: '',
    lastName: '',
    isActive: true,
    isStaff: false,
    isSuperuser: false,
})
const selectedGroupIds = ref(new Set<number>())

const passwordDialog = useDialog()
const passwordForm = reactive({ newPassword: '' })
const resendingInvitation = ref(false)

const resetTwoFactorDialog = useDialog()
/** The operator's own credentials, for the two dialogs that always ask for them. */
const credentials = reactive({ password: '', totp_code: '' })

const canWrite = computed(() => authStore.isSuperuser)
/** The server refuses a password set and a second-factor clear on the caller's own account. */
const isSelf = computed(() => account.value !== null && account.value.id === authStore.user?.id)

const displayName = computed(() => {
    if (!account.value) {
        return ''
    }
    const name = `${account.value.first_name} ${account.value.last_name}`.trim()
    return name || account.value.username
})

/**
 * What this account inherits per registered role, with the groups conveying it.
 *
 * A user picks up a role from every group carrying it, so the value is a list
 * even though the group form assigns one — several groups may carry the same
 * role. Option labels come from the provider; a value with no matching choice
 * is shown raw rather than dropped, since it means the project's vocabulary moved
 * under a group that still holds the old value.
 */
const inheritedRoles = computed(() => {
    const held = account.value?.roles ?? {}
    const memberOf = new Set((account.value?.groups ?? []).map(group => group.id))
    return roleProviders.value.map(provider => {
        const choices = new Map(provider.choices.map(pair => [pair[0], pair[1]]))
        const badges = (held[provider.key] ?? []).map(value => {
            const carriers = groups.value.filter(group => {
                return memberOf.has(group.id) && group.roles[provider.key] === value
            })
            return {
                key: value,
                label: choices.get(value) ?? value,
                carriers,
                // One badge per role value, not per carrying group: two groups
                // conveying the same role would otherwise render as two
                // identical badges. The link is offered only when a single group
                // carries it, since with several there is no non-arbitrary
                // target; the tooltip names them either way.
                group: carriers.length === 1 ? carriers[0] : null,
            }
        })
        return { key: provider.key, label: provider.label, badges }
    })
})

/**
 * Adopt an account the server just returned, without touching the details form.
 *
 * Kept separate from the form reset because every write on this page answers
 * with the whole account: saving group membership would otherwise overwrite
 * unsaved edits in the details form with the server's copy, discarding them
 * silently.
 */
function setAccount (loaded: Account) {
    account.value = loaded
    selectedGroupIds.value = new Set(loaded.groups.map(group => group.id))
    setPageTitle(loaded.username)
}

/** Seed the details form from the account. Only on load and after saving the details themselves. */
function resetDetailsForm (loaded: Account) {
    form.email = loaded.email
    form.firstName = loaded.first_name
    form.lastName = loaded.last_name
    form.isActive = loaded.is_active
    form.isStaff = loaded.is_staff
    form.isSuperuser = loaded.is_superuser
}

async function load () {
    loading.value = true
    loadError.value = ''
    try {
        // The roster carries every group an account belongs to, but the group
        // picker needs the full set, and the role list names what a role means.
        const [loaded, allGroups, providers] = await Promise.all([
            fetchAccount(accountId),
            listGroups(),
            listRoleProviders(),
        ])
        setAccount(loaded)
        resetDetailsForm(loaded)
        groups.value = allGroups
        roleProviders.value = providers
    } catch (err) {
        loadError.value = errorDetail(err, t('This account could not be loaded.', SCOPE))
    } finally {
        loading.value = false
    }
}

/**
 * Hover text naming the groups a role arrives through.
 *
 * Empty when none can be resolved, which leaves the badge without a tooltip
 * rather than asserting an origin the page does not have.
 */
function carrierHint (carriers: GroupDetail[]) {
    if (carriers.length === 0) {
        return ''
    }
    return t('Carried by {groups}', SCOPE, { groups: carriers.map(group => group.name).join(', ') })
}

/** Route to the group conveying a role, so the operator can change it where it lives. */
function groupLink (groupId: number) {
    return { name: 'admin-group', params: { id: String(groupId) } }
}

function goBack () {
    router.push({ name: 'admin-accounts' })
}

function isGroupSelected (groupId: number) {
    return selectedGroupIds.value.has(groupId)
}

function toggleGroup (groupId: number, event: Event) {
    const checked = (event.target as HTMLInputElement).checked
    const next = new Set(selectedGroupIds.value)
    if (checked) {
        next.add(groupId)
    } else {
        next.delete(groupId)
    }
    selectedGroupIds.value = next
}

async function saveDetails () {
    const current = account.value
    if (!current || savingDetails.value) {
        return
    }
    detailsError.value = ''
    savingDetails.value = true
    const payload = {
        email: form.email.trim(),
        first_name: form.firstName.trim(),
        last_name: form.lastName.trim(),
        is_active: form.isActive,
        is_staff: form.isStaff,
        is_superuser: form.isSuperuser,
    }
    try {
        const saved = await withStepUp(
            accountUpdateNeedsStepUp(current, payload),
            {
                title: t('Confirm the change', SCOPE),
                message: t('Changing a staff tier or the email address, or activating an account, asks for your own credentials.', SCOPE),
            },
            async (stepUp) => {
                const updated = await updateAccount(accountId, { ...payload, ...stepUp })
                setAccount(updated)
                resetDetailsForm(updated)
            },
        )
        if (saved) {
            showToast(t('Account updated.', SCOPE), 'neutral')
        }
    } catch (err) {
        // Covers the last-active-superuser guard, which is decided against
        // server state this client cannot see.
        detailsError.value = errorDetail(err, t('The account could not be updated.', SCOPE))
    } finally {
        savingDetails.value = false
    }
}

async function saveGroups () {
    const current = account.value
    if (!current || savingGroups.value) {
        return
    }
    savingGroups.value = true
    const groupIds = [...selectedGroupIds.value]
    try {
        const saved = await withStepUp(
            membershipAddsAny(current.groups.map(group => group.id), groupIds),
            {
                title: t('Confirm the change', SCOPE),
                message: t('Adding an account to a group grants what the group carries, so it asks for your own credentials.', SCOPE),
            },
            async (stepUp) => {
                const updated = await setAccountGroups(accountId, groupIds, stepUp)
                // Deliberately not resetDetailsForm: unsaved edits in the details
                // form are the operator's, not the server's to overwrite.
                setAccount(updated)
            },
        )
        if (saved) {
            showToast(t('Group membership updated.', SCOPE), 'neutral')
        }
    } catch (err) {
        showToast(errorDetail(err, t('Group membership could not be updated.', SCOPE)), 'danger')
    } finally {
        savingGroups.value = false
    }
}

function clearCredentials () {
    credentials.password = ''
    credentials.totp_code = ''
}

function openPassword () {
    passwordForm.newPassword = ''
    clearCredentials()
    passwordDialog.show()
}

async function confirmPassword () {
    const done = await passwordDialog.run(
        async () => {
            await setAccountPassword(accountId, passwordForm.newPassword, stepUpBody(credentials))
            return true
        },
        // The password validators answer with their messages joined into one string.
        { fallback: t('The password could not be set.', SCOPE) },
    )
    if (!done) {
        credentials.totp_code = ''
        return
    }
    // The account now has a password, so the invitation no longer applies.
    // Patched rather than reloaded, for the reason the second-factor reset
    // patches: re-applying the whole account would discard unsaved edits in
    // the details form above.
    if (account.value) {
        account.value = { ...account.value, is_invite_pending: false }
    }
    showToast(t('Password set.', SCOPE), 'neutral')
}

async function resendInvitation () {
    resendingInvitation.value = true
    try {
        const result = await resendAccountInvitation(accountId)
        if (result.invitation_sent) {
            showToast(t('Invitation sent again.', SCOPE), 'success')
        } else {
            showToast(t('Outgoing mail is not configured; no invitation was sent.', SCOPE), 'warning')
        }
    } catch (err) {
        showToast(errorDetail(err, t('The invitation could not be sent.', SCOPE)), 'danger')
    } finally {
        resendingInvitation.value = false
    }
}

function openResetTwoFactor () {
    clearCredentials()
    resetTwoFactorDialog.show()
}

async function confirmResetTwoFactor () {
    const done = await resetTwoFactorDialog.run(
        async () => {
            await resetAccountTwoFactor(accountId, stepUpBody(credentials))
            return true
        },
        { fallback: t('The second factor could not be cleared.', SCOPE) },
    )
    if (!done) {
        credentials.totp_code = ''
        return
    }
    // Only the flag changed; re-applying the whole account would also reset
    // the details form and discard edits the operator has not saved yet.
    if (account.value) {
        account.value = { ...account.value, is_2fa_enabled: false }
    }
    showToast(t('Second factor cleared.', SCOPE), 'neutral')
}

onMounted(load)
</script>

<template>
    <main class="page-view">
        <header class="page-header">
            <div class="page-header-start">
                <wa-button appearance="plain" size="s" @click="goBack">
                    <wa-icon name="arrow-left" slot="start"></wa-icon>
                    {{ t('Accounts', SCOPE) }}
                </wa-button>
            </div>
        </header>

        <wa-spinner v-if="loading" class="loading-center"></wa-spinner>

        <wa-callout v-else-if="loadError" variant="danger">
            {{ loadError }}
        </wa-callout>

        <template v-else-if="account">
            <header class="page-header">
                <h1>{{ displayName }}</h1>
                <div class="row-badges">
                    <wa-badge v-if="!account.is_active" appearance="filled" variant="neutral">
                        {{ t('Inactive', SCOPE) }}
                    </wa-badge>
                    <wa-badge v-if="account.is_superuser" appearance="filled" variant="brand">
                        {{ t('Superuser', SCOPE) }}
                    </wa-badge>
                    <wa-badge v-else-if="account.is_staff" appearance="filled" variant="neutral">
                        {{ t('Staff', SCOPE) }}
                    </wa-badge>
                </div>
            </header>

            <dl class="admin-facts">
                <dt>{{ t('Username', SCOPE) }}</dt>
                <dd>{{ account.username }}</dd>
                <dt>{{ t('Joined', SCOPE) }}</dt>
                <dd>
                    {{ formatDate(account.date_joined) }}
                    <span class="fact-aside">
                        (<wa-relative-time :date="account.date_joined"></wa-relative-time>)
                    </span>
                </dd>
                <dt>{{ t('Last sign-in', SCOPE) }}</dt>
                <dd v-if="account.last_login">
                    {{ formatDateTime(account.last_login) }}
                    <span class="fact-aside">
                        (<wa-relative-time :date="account.last_login"></wa-relative-time>)
                    </span>
                </dd>
                <dd v-else class="admin-hint">{{ t('Never', SCOPE) }}</dd>
            </dl>

            <section class="admin-section">
                <div class="section-header">
                    <h2>{{ t('Details', SCOPE) }}</h2>
                </div>
                <wa-callout v-if="detailsError" variant="danger">
                    {{ detailsError }}
                </wa-callout>
                <div class="admin-form">
                    <wa-input
                        :disabled="!canWrite"
                        :label="t('Email', SCOPE)"
                        type="email"
                        v-wa="[form, 'email']"
                    ></wa-input>
                    <wa-input
                        :disabled="!canWrite"
                        :label="t('First name', SCOPE)"
                        v-wa="[form, 'firstName']"
                    ></wa-input>
                    <wa-input
                        :disabled="!canWrite"
                        :label="t('Last name', SCOPE)"
                        v-wa="[form, 'lastName']"
                    ></wa-input>
                    <wa-switch :disabled="!canWrite" v-wa="[form, 'isActive']">
                        {{ t('Active', SCOPE) }}
                    </wa-switch>
                    <wa-switch :disabled="!canWrite" v-wa="[form, 'isStaff']">
                        {{ t('Staff', SCOPE) }}
                    </wa-switch>
                    <wa-switch :disabled="!canWrite" v-wa="[form, 'isSuperuser']">
                        {{ t('Superuser', SCOPE) }}
                    </wa-switch>
                </div>
                <div v-if="canWrite" class="form-actions">
                    <wa-button
                        appearance="filled-outlined"
                        :loading="savingDetails"
                        variant="brand"
                        @click="saveDetails"
                    >
                        {{ t('Save details', SCOPE) }}
                    </wa-button>
                </div>
            </section>

            <section class="admin-section">
                <div class="section-header">
                    <h2>{{ t('Groups', SCOPE) }}</h2>
                </div>
                <p v-if="!groups.length" class="admin-hint">
                    {{ t('This deployment has no groups yet.', SCOPE) }}
                </p>
                <div v-else class="admin-form">
                    <wa-checkbox v-for="group in groups"
                        :key="group.id"
                        :checked="isGroupSelected(group.id)"
                        :disabled="!canWrite"
                        @change="toggleGroup(group.id, $event)"
                    >
                        {{ group.name }}
                    </wa-checkbox>
                </div>
                <div v-if="canWrite && groups.length" class="form-actions">
                    <wa-button
                        appearance="filled-outlined"
                        :loading="savingGroups"
                        variant="brand"
                        @click="saveGroups"
                    >
                        {{ t('Save groups', SCOPE) }}
                    </wa-button>
                </div>
            </section>

            <section v-if="inheritedRoles.length" class="admin-section">
                <div class="section-header">
                    <h2>{{ t('Roles', SCOPE) }}</h2>
                </div>
                <p class="admin-hint">
                    {{ t('Roles are inherited from group membership and are assigned on the group, not here.', SCOPE) }}
                </p>
                <dl class="admin-facts">
                    <template v-for="role in inheritedRoles" :key="role.key">
                        <dt>{{ role.label }}</dt>
                        <dd>
                            <span v-if="!role.badges.length" class="admin-hint">&mdash;</span>
                            <div v-else class="row-badges">
                                <template v-for="badge in role.badges" :key="badge.key">
                                    <RouterLink v-if="badge.group"
                                        class="role-badge"
                                        :title="carrierHint(badge.carriers)"
                                        :to="groupLink(badge.group.id)"
                                    >
                                        <wa-badge appearance="filled-outlined" variant="brand">
                                            {{ badge.label }}
                                        </wa-badge>
                                    </RouterLink>
                                    <wa-badge v-else
                                        appearance="filled-outlined"
                                        :title="carrierHint(badge.carriers)"
                                        variant="brand"
                                    >
                                        {{ badge.label }}
                                    </wa-badge>
                                </template>
                            </div>
                        </dd>
                    </template>
                </dl>
            </section>

            <section class="admin-section">
                <div class="section-header">
                    <h2>{{ t('Sign-in', SCOPE) }}</h2>
                </div>
                <p v-if="account.external_provider" class="admin-hint">
                    {{ t('Signs in through {provider}. There is no password on this platform to set or reset.', SCOPE, { provider: account.external_provider }) }}
                </p>
                <p v-else-if="account.is_invite_pending && !account.is_active" class="admin-hint">
                    {{ t('No invitation has been sent: the account is deactivated. Activate it to send one.', SCOPE) }}
                </p>
                <p v-else-if="account.is_invite_pending" class="admin-hint">
                    {{ t('Invited, and has not chosen a password yet.', SCOPE) }}
                </p>
                <p class="admin-hint">
                    {{ t('Second factor', SCOPE) }}:
                    {{ account.is_2fa_enabled ? t('enrolled', SCOPE) : t('not enrolled', SCOPE) }}
                </p>
                <p v-if="canWrite && isSelf" class="admin-hint">
                    {{ t('This is your own account. Change its password and second factor on your profile page.', SCOPE) }}
                </p>
                <div v-if="canWrite" class="form-actions">
                    <wa-button v-if="account.is_invite_pending"
                        appearance="plain"
                        :disabled="resendingInvitation || !account.email || !account.is_active"
                        variant="brand"
                        @click="resendInvitation"
                    >
                        <wa-icon name="envelope" slot="start"></wa-icon>
                        {{ t('Resend invitation', SCOPE) }}
                    </wa-button>
                    <wa-button v-if="!account.external_provider && !isSelf" appearance="plain" @click="openPassword">
                        <wa-icon name="key" slot="start"></wa-icon>
                        {{ t('Set password', SCOPE) }}
                    </wa-button>
                    <wa-button v-if="!isSelf"
                        appearance="plain"
                        :disabled="!account.is_2fa_enabled"
                        variant="danger"
                        @click="openResetTwoFactor"
                    >
                        {{ t('Clear second factor', SCOPE) }}
                    </wa-button>
                </div>
            </section>

            <wa-callout variant="neutral">
                {{ t('Accounts are not deleted from here. Erasing an account also unlinks the recording and media files it owns, which only the erase_user command on the host does.', SCOPE) }}
            </wa-callout>
        </template>
    </main>

    <wa-dialog
        :label="t('Set password', SCOPE)"
        :open="passwordDialog.open.value"
        @wa-hide.self="passwordDialog.onHide"
    >
        <div class="admin-form">
            <wa-callout v-if="passwordDialog.error.value" variant="danger">
                {{ passwordDialog.error.value }}
            </wa-callout>
            <p class="admin-hint">
                {{ t('This does not sign the account out of its open sessions. Deactivate the account instead if the credentials may be compromised.', SCOPE) }}
            </p>
            <wa-input
                autocomplete="new-password"
                :label="t('New password', SCOPE)"
                password-toggle
                required
                type="password"
                v-wa="[passwordForm, 'newPassword']"
            ></wa-input>
            <StepUpFields :credentials="credentials" :step-up="authStore.stepUp" />
        </div>
        <div slot="footer" class="form-actions">
            <wa-button
                appearance="filled-outlined"
                :disabled="passwordDialog.busy.value"
                variant="neutral"
                @click="passwordDialog.close"
            >
                {{ t('Cancel', SCOPE) }}
            </wa-button>
            <wa-button
                appearance="filled-outlined"
                :disabled="!authStore.stepUp.available"
                :loading="passwordDialog.busy.value"
                variant="brand"
                @click="confirmPassword"
            >
                {{ t('Set password', SCOPE) }}
            </wa-button>
        </div>
    </wa-dialog>

    <wa-dialog
        :label="t('Clear second factor', SCOPE)"
        :open="resetTwoFactorDialog.open.value"
        @wa-hide.self="resetTwoFactorDialog.onHide"
    >
        <div class="admin-form">
            <wa-callout v-if="resetTwoFactorDialog.error.value" variant="danger">
                {{ resetTwoFactorDialog.error.value }}
            </wa-callout>
            <i18n-t class="dialog-text" keypath="AdminAccountView.clear_two_factor_confirm" tag="p">
                <template #name>
                    <strong>{{ account?.username }}</strong>
                </template>
            </i18n-t>
            <StepUpFields :credentials="credentials" :step-up="authStore.stepUp" />
        </div>
        <div slot="footer" class="form-actions">
            <wa-button
                appearance="filled-outlined"
                :disabled="resetTwoFactorDialog.busy.value"
                variant="neutral"
                @click="resetTwoFactorDialog.close"
            >
                {{ t('Cancel', SCOPE) }}
            </wa-button>
            <wa-button
                appearance="filled-outlined"
                :disabled="!authStore.stepUp.available"
                :loading="resetTwoFactorDialog.busy.value"
                variant="danger"
                @click="confirmResetTwoFactor"
            >
                {{ t('Clear second factor', SCOPE) }}
            </wa-button>
        </div>
    </wa-dialog>
</template>

<style scoped>
.admin-section {
    margin-bottom: var(--wa-space-xl);
}

.admin-form {
    display: flex;
    flex-direction: column;
    gap: var(--wa-space-s);
}

.admin-hint {
    color: var(--wa-color-text-quiet);
    font-size: var(--wa-font-size-s);
    margin: 0 0 var(--wa-space-s);
}

/*
 * Rows centre rather than stretch: a term is one line of text while its value
 * may be a row of badges, and the grid default leaves the two hugging opposite
 * edges of a row sized by the taller one.
 */
.admin-facts {
    display: grid;
    grid-template-columns: max-content 1fr;
    align-items: center;
    gap: var(--wa-space-2xs) var(--wa-space-m);
    margin: 0 0 var(--wa-space-l);
}

.admin-facts dt {
    color: var(--wa-color-text-quiet);
    font-size: var(--wa-font-size-s);
}

.admin-facts dd {
    margin: 0;
}

/* Relative time beside an absolute one: secondary, so it reads as a gloss on
   the timestamp rather than a second fact. */
.fact-aside {
    color: var(--wa-color-text-quiet);
    font-size: var(--wa-font-size-s);
    margin-left: var(--wa-space-2xs);
}

/* The badge is the link, so the anchor contributes layout and nothing visual. */
.role-badge {
    display: inline-flex;
    text-decoration: none;
}

.dialog-text {
    margin: 0;
}
</style>
