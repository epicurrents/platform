<script setup lang="ts">
/**
 * Segmented control switching between the administration sections.
 *
 * A route-linked segmented control rather than a `wa-tab-group`: the sections
 * are separate pages with their own URLs, so tab panels would put them all
 * behind one address and lose the deep link to an account or a job.
 *
 * The Maintenance segment renders only where the deployment has the feature
 * switched on; the store asks the server once and the answer is a 404 or not.
 *
 * @package    epicurrents-platform
 */
import { onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { t } from '#i18n'
import { useMaintenanceStore } from '#stores/maintenance'

const SCOPE = 'AdminTabs'

type Tab = 'accounts' | 'groups' | 'maintenance'

const props = defineProps<{
    /** Which section is showing, so the matching segment reads as pressed. */
    active: Tab
}>()

const router = useRouter()
const maintenanceStore = useMaintenanceStore()

/** `filled` marks the current section; `plain` leaves the others quiet. */
function appearanceFor (tab: Tab) {
    return props.active === tab ? 'filled' : 'plain'
}

function openAccounts () {
    router.push({ name: 'admin-accounts' })
}

function openGroups () {
    router.push({ name: 'admin-groups' })
}

function openMaintenance () {
    router.push({ name: 'admin-maintenance' })
}

onMounted(() => {
    maintenanceStore.probeFeature()
})
</script>

<template>
    <wa-button-group class="admin-tabs" :label="t('Administration sections', SCOPE)">
        <wa-button
            :appearance="appearanceFor('accounts')"
            size="s"
            @click="openAccounts"
        >
            <wa-icon name="users" slot="start"></wa-icon>
            {{ t('Accounts', SCOPE) }}
        </wa-button>
        <wa-button
            :appearance="appearanceFor('groups')"
            size="s"
            @click="openGroups"
        >
            <wa-icon name="user-group" slot="start"></wa-icon>
            {{ t('Groups', SCOPE) }}
        </wa-button>
        <wa-button v-if="maintenanceStore.featureEnabled"
            :appearance="appearanceFor('maintenance')"
            size="s"
            @click="openMaintenance"
        >
            <wa-icon name="screwdriver-wrench" slot="start"></wa-icon>
            {{ t('Maintenance', SCOPE) }}
        </wa-button>
    </wa-button-group>
</template>

<style scoped>
.admin-tabs {
    margin-bottom: var(--wa-space-m);
}
</style>
