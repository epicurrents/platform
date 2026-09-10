<script setup lang="ts">
/**
 * Account roster — the administration surface's landing page.
 *
 * Staff read the roster; only a superuser sees the create control, matching
 * the tier the API enforces. The roster endpoint answers with a bare list and
 * no total, so paging can say "there is more" but never "N of M"; the Next
 * button is enabled on a full page and that is the whole signal available.
 *
 * @package    epicurrents-platform
 */
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import AdminTabs from '#components/AdminTabs.vue'
import { createAccount, listAccounts, type Account } from '#api/admin'
import { t } from '#i18n'
import { errorDetail } from '#lib/http'
import { showToast } from '#lib/toast'
import { useAuthStore } from '#stores/auth'

const SCOPE = 'AdminAccountsView'
const PAGE_SIZE = 50
/** Keystroke-to-request delay for the search box, in milliseconds. */
const SEARCH_DEBOUNCE = 250

const authStore = useAuthStore()
const router = useRouter()

const accounts = ref<Account[]>([])
const loading = ref(true)
const loadError = ref('')
const offset = ref(0)
const search = reactive({ q: '' })

const showCreate = ref(false)
const creating = ref(false)
const createError = ref('')
const createForm = reactive({
    username: '',
    password: '',
    email: '',
    firstName: '',
    lastName: '',
    isActive: true,
    isStaff: false,
    isSuperuser: false,
})

/** Writes are superuser-only; a staff account sees the roster read-only. */
const canWrite = computed(() => authStore.isSuperuser)

/** A full page is the only evidence that more rows exist — there is no total to compare against. */
const hasNextPage = computed(() => accounts.value.length === PAGE_SIZE)

const hasPrevPage = computed(() => offset.value > 0)

let searchTimer: ReturnType<typeof setTimeout> | undefined
/**
 * Sequence number of the most recently issued roster request.
 *
 * Typing issues one request per debounce window and the answers can arrive in
 * any order, so a slow early query resolving after a fast later one would leave
 * the list showing results for a search the box no longer holds. Only the
 * newest request is allowed to write.
 */
let requestSeq = 0

async function load () {
    const seq = ++requestSeq
    loading.value = true
    loadError.value = ''
    try {
        const rows = await listAccounts({
            q: search.q.trim(),
            limit: PAGE_SIZE,
            offset: offset.value,
        })
        if (seq !== requestSeq) {
            return
        }
        accounts.value = rows
    } catch (err) {
        if (seq !== requestSeq) {
            return
        }
        loadError.value = errorDetail(err, t('The account roster could not be loaded.', SCOPE))
        accounts.value = []
    } finally {
        if (seq === requestSeq) {
            loading.value = false
        }
    }
}

/** Full name when the account has one, falling back to the username so a row is never blank. */
function displayName (account: Account) {
    const name = `${account.first_name} ${account.last_name}`.trim()
    return name || account.username
}

function openAccount (account: Account) {
    router.push({ name: 'admin-account', params: { id: String(account.id) } })
}

function openCreate () {
    createError.value = ''
    createForm.username = ''
    createForm.password = ''
    createForm.email = ''
    createForm.firstName = ''
    createForm.lastName = ''
    createForm.isActive = true
    createForm.isStaff = false
    createForm.isSuperuser = false
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
        const account = await createAccount({
            username: createForm.username.trim(),
            password: createForm.password,
            email: createForm.email.trim(),
            first_name: createForm.firstName.trim(),
            last_name: createForm.lastName.trim(),
            is_active: createForm.isActive,
            is_staff: createForm.isStaff,
            is_superuser: createForm.isSuperuser,
        })
        showCreate.value = false
        showToast(t('Account {username} created.', SCOPE, { username: account.username }), 'success')
        router.push({ name: 'admin-account', params: { id: String(account.id) } })
    } catch (err) {
        // The server owns every refusal here — duplicate username, rejected
        // password, malformed email — so show what it said rather than guessing.
        createError.value = errorDetail(err, t('The account could not be created.', SCOPE))
    } finally {
        creating.value = false
    }
}

function nextPage () {
    offset.value += PAGE_SIZE
    load()
}

function prevPage () {
    offset.value = Math.max(0, offset.value - PAGE_SIZE)
    load()
}

// Searching restarts paging: an offset carried over from the previous query
// would land in the middle of a result set the operator has not seen the top of.
watch(() => search.q, () => {
    clearTimeout(searchTimer)
    searchTimer = setTimeout(() => {
        offset.value = 0
        load()
    }, SEARCH_DEBOUNCE)
})

onMounted(load)

// A pending debounce would otherwise fire after the view is gone.
onUnmounted(() => clearTimeout(searchTimer))
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
                {{ t('New account', SCOPE) }}
            </wa-button>
        </header>

        <AdminTabs active="accounts" />

        <wa-callout v-if="!canWrite" variant="neutral">
            {{ t('You can view accounts and groups. Changing them requires superuser access.', SCOPE) }}
        </wa-callout>

        <wa-input
            class="admin-search"
            :label="t('Search', SCOPE)"
            :placeholder="t('Username, name or email', SCOPE)"
            type="search"
            v-wa="[search, 'q']"
        >
            <wa-icon name="user" slot="start"></wa-icon>
        </wa-input>

        <wa-spinner v-if="loading" class="loading-center"></wa-spinner>

        <wa-callout v-else-if="loadError" variant="danger">
            {{ loadError }}
        </wa-callout>

        <p v-else-if="!accounts.length" class="empty-state">
            {{ t('No accounts match that search.', SCOPE) }}
        </p>

        <div v-else class="list-rows">
            <div v-for="account in accounts"
                :key="account.id"
                class="list-row clickable"
                @click="openAccount(account)"
            >
                <div class="list-row-main">
                    <wa-icon class="icon-muted" name="user"></wa-icon>
                    <span class="list-row-name">{{ displayName(account) }}</span>
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
                        <wa-badge v-if="account.is_2fa_enabled" appearance="outlined" variant="success">
                            {{ t('2FA', SCOPE) }}
                        </wa-badge>
                    </div>
                    <span class="list-row-meta">{{ account.username }}</span>
                </div>
            </div>
        </div>

        <div v-if="!loading && !loadError" class="admin-paging">
            <wa-button
                appearance="plain"
                :disabled="!hasPrevPage"
                size="s"
                @click="prevPage"
            >
                <wa-icon name="arrow-left" slot="start"></wa-icon>
                {{ t('Previous', SCOPE) }}
            </wa-button>
            <wa-button
                appearance="plain"
                :disabled="!hasNextPage"
                size="s"
                @click="nextPage"
            >
                {{ t('Next', SCOPE) }}
                <wa-icon name="arrow-right" slot="end"></wa-icon>
            </wa-button>
        </div>
    </main>

    <wa-dialog :label="t('New account', SCOPE)" :open="showCreate" @wa-hide.self="closeCreate">
        <div class="admin-form">
            <wa-callout v-if="createError" variant="danger">
                {{ createError }}
            </wa-callout>
            <wa-input
                autocomplete="off"
                :label="t('Username', SCOPE)"
                required
                v-wa="[createForm, 'username']"
            ></wa-input>
            <wa-input
                autocomplete="new-password"
                :label="t('Password', SCOPE)"
                password-toggle
                required
                type="password"
                v-wa="[createForm, 'password']"
            ></wa-input>
            <wa-input
                :label="t('Email', SCOPE)"
                type="email"
                v-wa="[createForm, 'email']"
            ></wa-input>
            <wa-input :label="t('First name', SCOPE)" v-wa="[createForm, 'firstName']"></wa-input>
            <wa-input :label="t('Last name', SCOPE)" v-wa="[createForm, 'lastName']"></wa-input>
            <wa-switch v-wa="[createForm, 'isActive']">{{ t('Active', SCOPE) }}</wa-switch>
            <wa-switch v-wa="[createForm, 'isStaff']">{{ t('Staff', SCOPE) }}</wa-switch>
            <wa-switch v-wa="[createForm, 'isSuperuser']">{{ t('Superuser', SCOPE) }}</wa-switch>
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
                {{ t('Create account', SCOPE) }}
            </wa-button>
        </div>
    </wa-dialog>
</template>

<style scoped>
.icon-muted {
    color: var(--wa-color-text-quiet);
    flex-shrink: 0;
}

.admin-search {
    margin-bottom: var(--wa-space-m);
}

.admin-form {
    display: flex;
    flex-direction: column;
    gap: var(--wa-space-s);
}

.admin-paging {
    display: flex;
    justify-content: space-between;
    margin-top: var(--wa-space-m);
}
</style>
