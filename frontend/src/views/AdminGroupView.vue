<script setup lang="ts">
/**
 * Group detail — rename, role assignment, and the group's member roll.
 *
 * Roles live here because they belong to groups; accounts inherit them through
 * membership and show them read-only. The platform knows the role mechanism
 * but never a role's meaning: every key, label and option rendered below comes
 * off `GET /roles` at runtime, so a deployment running an unknown project gets
 * working role management with no change to this file.
 *
 * Name and roles save together because the server writes them in one
 * transaction — a rejected role value aborts the rename with it, so reporting
 * them as separate outcomes would tell the operator something untrue.
 *
 * Membership is read here but never written. Assigning it from this side means
 * picking users out of a list, and a deployment has far more users than groups,
 * so the same edit is a short list of checkboxes on the account page and an
 * unbounded one here. The account page is therefore the only place it is set,
 * and it is also the safe one: both membership endpoints take a whole-membership
 * replacement, so a picker built from the capped account roster would drop every
 * member past the cap, which the group list on an account page cannot do.
 *
 * There is no single-group read endpoint; the group roster is the source.
 *
 * @package    epicurrents-platform
 */
import { computed, onMounted, reactive, ref } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import {
    listAccounts,
    listGroups,
    listRoleProviders,
    rolesPayload,
    updateGroup,
    type Account,
    type GroupDetail,
    type RoleProvider,
} from '#api/admin'
import { t } from '#i18n'
import { errorDetail } from '#lib/http'
import { showToast } from '#lib/toast'
import { setPageTitle } from '#router'
import { useAuthStore } from '#stores/auth'

const SCOPE = 'AdminGroupView'
/** The account roster's server-side cap; a full page means members may be missing from the roll. */
const ROSTER_LIMIT = 500

const authStore = useAuthStore()
const route = useRoute()
const router = useRouter()

const groupId = Number(route.params.id)

const group = ref<GroupDetail | null>(null)
const roleProviders = ref<RoleProvider[]>([])
const accounts = ref<Account[]>([])
const loading = ref(true)
const loadError = ref('')

const saving = ref(false)
const saveError = ref('')
const form = reactive({ name: '' })
/**
 * Selector state, keyed by role key. Only keys present here are sent, which is
 * what keeps the PATCH from carrying a padded map that would clear roles the
 * form never rendered.
 */
const roleValues = reactive<Record<string, string>>({})

const canWrite = computed(() => authStore.isSuperuser)

/** Nothing role-shaped renders on a deployment whose active project registers none. */
const hasRoles = computed(() => roleProviders.value.length > 0)

/** This group's members, as far as the account roster reaches. */
const members = computed(() => {
    return accounts.value.filter(account => account.groups.some(g => g.id === groupId))
})

/**
 * Whether the roll above is the whole membership.
 *
 * The roster is capped server-side, so a large deployment can hold members this
 * page never sees. `member_count` comes from the group itself and is the
 * authority; when the two disagree the page says so rather than presenting a
 * partial list as complete.
 */
const membersComplete = computed(() => {
    return group.value !== null && members.value.length === group.value.member_count
})

/**
 * Adopt a group the server just returned, without touching the group form.
 *
 * Kept separate from the form reset so a response can update the header counts
 * without reseeding the name and role selectors under an operator mid-edit.
 */
function setGroup (loaded: GroupDetail) {
    group.value = loaded
    setPageTitle(loaded.name)
}

/**
 * Seed the selectors from the group's roles, for the rendered providers only.
 *
 * The group payload carries an entry for every registered key including the
 * nulls, so seeding from it directly and submitting it back is the padded-map
 * clear. Reading it through the provider list is what bounds the write.
 */
function seedRoleValues (loaded: GroupDetail, providers: RoleProvider[]) {
    for (const provider of providers) {
        roleValues[provider.key] = loaded.roles[provider.key] ?? ''
    }
}

async function load () {
    loading.value = true
    loadError.value = ''
    try {
        const [allGroups, providers, roster] = await Promise.all([
            listGroups(),
            listRoleProviders(),
            listAccounts({ limit: ROSTER_LIMIT }),
        ])
        const found = allGroups.find(candidate => candidate.id === groupId)
        if (!found) {
            loadError.value = t('This group no longer exists.', SCOPE)
            return
        }
        roleProviders.value = providers
        accounts.value = roster
        setGroup(found)
        form.name = found.name
        seedRoleValues(found, providers)
    } catch (err) {
        loadError.value = errorDetail(err, t('This group could not be loaded.', SCOPE))
    } finally {
        loading.value = false
    }
}

/** Route to a member's account page, which is where membership is changed. */
function accountLink (accountId: number) {
    return { name: 'admin-account', params: { id: String(accountId) } }
}

function goBack () {
    router.push({ name: 'admin-groups' })
}

function displayName (account: Account) {
    const name = `${account.first_name} ${account.last_name}`.trim()
    return name ? `${name} (${account.username})` : account.username
}

async function save () {
    saveError.value = ''
    saving.value = true
    try {
        const updated = await updateGroup(groupId, {
            name: form.name.trim(),
            // Rendered keys only. `rolesPayload` maps the blank option to null,
            // which is how "no role" is expressed — an empty string is not one
            // of a provider's declared choices and the server rejects it.
            ...(hasRoles.value
                ? { roles: rolesPayload(roleProviders.value.map(provider => provider.key), roleValues) }
                : {}),
        })
        setGroup(updated)
        form.name = updated.name
        seedRoleValues(updated, roleProviders.value)
        showToast(t('Group saved.', SCOPE), 'neutral')
    } catch (err) {
        saveError.value = errorDetail(err, t('The group could not be saved. Nothing was changed.', SCOPE))
    } finally {
        saving.value = false
    }
}

onMounted(load)
</script>

<template>
    <main class="page-view">
        <header class="page-header">
            <div class="page-header-start">
                <wa-button appearance="plain" size="s" @click="goBack">
                    <wa-icon name="arrow-left" slot="start"></wa-icon>
                    {{ t('Groups', SCOPE) }}
                </wa-button>
            </div>
        </header>

        <wa-spinner v-if="loading" class="loading-center"></wa-spinner>

        <wa-callout v-else-if="loadError" variant="danger">
            {{ loadError }}
        </wa-callout>

        <template v-else-if="group">
            <header class="page-header">
                <h1>{{ group.name }}</h1>
                <div class="row-badges">
                    <wa-badge appearance="outlined" variant="neutral">
                        {{ t('{count} members', SCOPE, { count: group.member_count }) }}
                    </wa-badge>
                    <wa-badge appearance="outlined" variant="neutral">
                        {{ t('{count} grants', SCOPE, { count: group.grant_count }) }}
                    </wa-badge>
                </div>
            </header>

            <section class="admin-section">
                <div class="section-header">
                    <h2>{{ t('Group', SCOPE) }}</h2>
                </div>
                <wa-callout v-if="saveError" variant="danger">
                    {{ saveError }}
                </wa-callout>
                <div class="admin-form">
                    <wa-input
                        :disabled="!canWrite"
                        :label="t('Name', SCOPE)"
                        v-wa="[form, 'name']"
                    ></wa-input>
                    <wa-select v-for="provider in roleProviders"
                        :key="provider.key"
                        :disabled="!canWrite"
                        :label="provider.label"
                        v-wa="[roleValues, provider.key]"
                    >
                        <wa-option value="">{{ t('No role', SCOPE) }}</wa-option>
                        <wa-option v-for="choice in provider.choices" :key="choice[0]" :value="choice[0]">
                            {{ choice[1] }}
                        </wa-option>
                    </wa-select>
                </div>
                <p v-if="hasRoles" class="admin-hint">
                    {{ t('Members inherit these roles through their membership of this group.', SCOPE) }}
                </p>
                <div v-if="canWrite" class="form-actions">
                    <wa-button
                        appearance="filled-outlined"
                        :loading="saving"
                        variant="brand"
                        @click="save"
                    >
                        {{ t('Save group', SCOPE) }}
                    </wa-button>
                </div>
            </section>

            <section class="admin-section">
                <div class="section-header">
                    <h2>{{ t('Members', SCOPE) }}</h2>
                </div>
                <p class="admin-hint">
                    {{ t('Membership is set on each account, under Groups.', SCOPE) }}
                </p>
                <p v-if="!members.length && membersComplete" class="admin-hint">
                    {{ t('No members yet.', SCOPE) }}
                </p>
                <div v-else-if="members.length" class="list-rows">
                    <RouterLink v-for="member in members"
                        :key="member.id"
                        class="list-row clickable member-row"
                        :to="accountLink(member.id)"
                    >
                        <div class="list-row-main">
                            <wa-icon class="icon-muted" name="user"></wa-icon>
                            <span class="list-row-name">{{ displayName(member) }}</span>
                        </div>
                    </RouterLink>
                </div>
                <p v-if="!membersComplete" class="admin-hint">
                    {{ t('Showing {shown} of {total} — the rest are past the end of the account list.', SCOPE, { shown: members.length, total: group.member_count }) }}
                </p>
            </section>
        </template>
    </main>
</template>

<style scoped>
/* The roll is anchors where every other list row is a div; keep the row chrome
   doing the work of signalling interactivity rather than link styling. */
.member-row {
    color: inherit;
    text-decoration: none;
}

.icon-muted {
    color: var(--wa-color-text-quiet);
    flex-shrink: 0;
}

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
</style>
