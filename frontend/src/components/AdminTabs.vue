<script setup lang="ts">
/**
 * Segmented control switching between the two administration rosters.
 *
 * A route-linked segmented control rather than a `wa-tab-group`: the two
 * halves are separate pages with their own URLs, so tab panels would put both
 * rosters behind one address and lose the deep link to an account.
 *
 * @package    epicurrents-platform
 */
import { useRouter } from 'vue-router'
import { t } from '#i18n'

const SCOPE = 'AdminTabs'

const props = defineProps<{
    /** Which roster is showing, so the matching segment reads as pressed. */
    active: 'accounts' | 'groups'
}>()

const router = useRouter()

/** `filled` marks the current roster; `plain` leaves the other one quiet. */
function appearanceFor (tab: 'accounts' | 'groups') {
    return props.active === tab ? 'filled' : 'plain'
}

function openAccounts () {
    router.push({ name: 'admin-accounts' })
}

function openGroups () {
    router.push({ name: 'admin-groups' })
}
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
    </wa-button-group>
</template>

<style scoped>
.admin-tabs {
    margin-bottom: var(--wa-space-m);
}
</style>
