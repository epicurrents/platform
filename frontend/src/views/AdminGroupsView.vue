<script setup lang="ts">
/**
 * Group roster — groups with the two counts that decide whether one can be deleted.
 *
 * `grant_count` is why a deletion is refused: removing a group that access
 * grants stand on would revoke everyone's access at once and cascade the
 * `AccessRight` rows away with it, leaving no record of what was withdrawn.
 * The count is shown on every row so that refusal is never a surprise.
 *
 * @package    epicurrents-platform
 */
import { computed, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import AdminTabs from '#components/AdminTabs.vue'
import {
    createGroup,
    deleteGroup,
    listGroups,
    listRoleProviders,
    type GroupDetail,
    type RoleProvider,
} from '#api/admin'
import { t } from '#i18n'
import { errorDetail } from '#lib/http'
import { showToast } from '#lib/toast'
import { useAuthStore } from '#stores/auth'

const SCOPE = 'AdminGroupsView'

const authStore = useAuthStore()
const router = useRouter()

const groups = ref<GroupDetail[]>([])
const roleProviders = ref<RoleProvider[]>([])
const loading = ref(true)
const loadError = ref('')

const showCreate = ref(false)
const creating = ref(false)
const createError = ref('')
const createForm = reactive({ name: '' })

const deletingGroup = ref<GroupDetail | null>(null)
const deleteLoading = ref(false)
const deleteError = ref('')

const canWrite = computed(() => authStore.isSuperuser)

async function load () {
    loading.value = true
    loadError.value = ''
    try {
        // `/roles` is the sole authority for which roles exist and what their
        // option labels read as; the group payload carries only raw values.
        const [allGroups, providers] = await Promise.all([listGroups(), listRoleProviders()])
        groups.value = allGroups
        roleProviders.value = providers
    } catch (err) {
        loadError.value = errorDetail(err, t('The group list could not be loaded.', SCOPE))
        groups.value = []
    } finally {
        loading.value = false
    }
}

/**
 * The roles a group carries, as `[provider label, option label]` pairs, skipping
 * providers it holds nothing for.
 *
 * A value with no matching choice is shown as-is rather than dropped: it means
 * the project changed its vocabulary under a group that still carries the old
 * value, and hiding that would hide the thing the operator has to fix.
 */
function roleBadges (group: GroupDetail) {
    const badges: { key: string, label: string, value: string }[] = []
    for (const provider of roleProviders.value) {
        const value = group.roles[provider.key]
        if (!value) {
            continue
        }
        const choice = provider.choices.find(pair => pair[0] === value)
        badges.push({ key: provider.key, label: provider.label, value: choice ? choice[1] : value })
    }
    return badges
}

function openGroup (group: GroupDetail) {
    router.push({ name: 'admin-group', params: { id: String(group.id) } })
}

function openCreate () {
    createError.value = ''
    createForm.name = ''
    showCreate.value = true
}

function closeCreate () {
    if (creating.value) {
        return
    }
    showCreate.value = false
}

async function confirmCreate () {
    createError.value = ''
    creating.value = true
    try {
        const group = await createGroup(createForm.name.trim())
        showCreate.value = false
        showToast(t('Group {name} created.', SCOPE, { name: group.name }), 'success')
        router.push({ name: 'admin-group', params: { id: String(group.id) } })
    } catch (err) {
        createError.value = errorDetail(err, t('The group could not be created.', SCOPE))
    } finally {
        creating.value = false
    }
}

function openDelete (group: GroupDetail) {
    deleteError.value = ''
    deletingGroup.value = group
}

function closeDelete () {
    if (deleteLoading.value) {
        return
    }
    deletingGroup.value = null
}

async function confirmDelete () {
    if (!deletingGroup.value) {
        return
    }
    deleteError.value = ''
    deleteLoading.value = true
    try {
        await deleteGroup(deletingGroup.value.id)
        deletingGroup.value = null
        showToast(t('Group deleted.', SCOPE), 'neutral')
        await load()
    } catch (err) {
        // The grant count is counted server-side at the moment of deletion, so
        // a row that looked deletable can still be refused here.
        deleteError.value = errorDetail(err, t('The group could not be deleted.', SCOPE))
    } finally {
        deleteLoading.value = false
    }
}

function handleGroupAction (event: Event, group: GroupDetail) {
    const value = (event as CustomEvent<{ item: { value: string } }>).detail.item.value
    if (value === 'delete') {
        openDelete(group)
    }
}

onMounted(load)
</script>

<template>
    <main class="page-view">
        <header class="page-header">
            <h1>{{ t('Administration', SCOPE) }}</h1>
            <wa-button v-if="canWrite"
                appearance="filled-outlined"
                size="s"
                variant="brand"
                @click="openCreate"
            >
                <wa-icon name="plus" slot="start"></wa-icon>
                {{ t('New group', SCOPE) }}
            </wa-button>
        </header>

        <AdminTabs active="groups" />

        <wa-callout variant="neutral">
            {{ t('Groups carry access grants and project roles. Members inherit both through membership.', SCOPE) }}
        </wa-callout>

        <wa-spinner v-if="loading" class="loading-center"></wa-spinner>

        <wa-callout v-else-if="loadError" variant="danger">
            {{ loadError }}
        </wa-callout>

        <p v-else-if="!groups.length" class="empty-state">
            {{ t('No groups yet.', SCOPE) }}
        </p>

        <div v-else class="list-rows">
            <div v-for="group in groups"
                :key="group.id"
                class="list-row clickable"
                @click="openGroup(group)"
            >
                <div class="list-row-main">
                    <wa-icon class="icon-muted" name="user-group"></wa-icon>
                    <span class="list-row-name">{{ group.name }}</span>
                    <div class="row-badges">
                        <wa-badge v-for="badge in roleBadges(group)"
                            :key="badge.key"
                            appearance="outlined"
                            :title="badge.label"
                            variant="neutral"
                        >
                            {{ badge.value }}
                        </wa-badge>
                    </div>
                    <span class="list-row-meta">
                        {{ t('{count} members', SCOPE, { count: group.member_count }) }}
                    </span>
                    <span class="list-row-meta">
                        {{ t('{count} grants', SCOPE, { count: group.grant_count }) }}
                    </span>
                    <div v-if="canWrite" class="list-row-actions">
                        <wa-dropdown
                            placement="bottom-end"
                            @click.stop
                            @wa-select.stop="handleGroupAction($event, group)"
                        >
                            <wa-button
                                appearance="plain"
                                size="s"
                                slot="trigger"
                            >
                                <wa-icon name="ellipsis"></wa-icon>
                            </wa-button>
                            <wa-dropdown-item value="delete" variant="danger">
                                <wa-icon name="trash" slot="icon"></wa-icon>
                                {{ t('Delete group', SCOPE) }}
                            </wa-dropdown-item>
                        </wa-dropdown>
                    </div>
                </div>
            </div>
        </div>
    </main>

    <wa-dialog :label="t('New group', SCOPE)" :open="showCreate" @wa-hide.self="closeCreate">
        <div class="admin-form">
            <wa-callout v-if="createError" variant="danger">
                {{ createError }}
            </wa-callout>
            <wa-input :label="t('Group name', SCOPE)" required v-wa="[createForm, 'name']"></wa-input>
        </div>
        <div slot="footer" class="form-actions">
            <wa-button
                appearance="filled-outlined"
                :disabled="creating"
                variant="neutral"
                @click="closeCreate"
            >
                {{ t('Cancel', SCOPE) }}
            </wa-button>
            <wa-button
                appearance="filled-outlined"
                :loading="creating"
                variant="brand"
                @click="confirmCreate"
            >
                {{ t('Create group', SCOPE) }}
            </wa-button>
        </div>
    </wa-dialog>

    <wa-dialog
        :label="t('Delete group', SCOPE)"
        :open="!!deletingGroup"
        @wa-hide.self="closeDelete"
    >
        <wa-callout v-if="deleteError" variant="danger">
            {{ deleteError }}
        </wa-callout>
        <i18n-t class="dialog-text" keypath="AdminGroupsView.delete_group_confirm" tag="p">
            <template #name>
                <strong>{{ deletingGroup?.name }}</strong>
            </template>
        </i18n-t>
        <div slot="footer" class="form-actions">
            <wa-button
                appearance="filled-outlined"
                :disabled="deleteLoading"
                variant="neutral"
                @click="closeDelete"
            >
                {{ t('Cancel', SCOPE) }}
            </wa-button>
            <wa-button
                appearance="filled-outlined"
                :loading="deleteLoading"
                variant="danger"
                @click="confirmDelete"
            >
                {{ t('Delete group', SCOPE) }}
            </wa-button>
        </div>
    </wa-dialog>
</template>

<style scoped>
.icon-muted {
    color: var(--wa-color-text-quiet);
    flex-shrink: 0;
}

.admin-form {
    display: flex;
    flex-direction: column;
    gap: var(--wa-space-s);
}

.dialog-text {
    margin: 0;
}
</style>
