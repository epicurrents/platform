<script setup lang="ts">
/**
 * The deployment's user agreement: read by anyone, accepted by an account before it can use anything else.
 *
 * The route is public, so the agreement can be read before signing in and the guard's redirect here cannot loop. An
 * account that has not accepted the binding version arrives with `?redirect=` naming where it was going, and goes there
 * once it accepts. The text is the deployment's own Markdown, rendered with raw HTML disabled.
 *
 * @package    epicurrents-platform
 */
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import MarkdownIt from 'markdown-it'
import { t } from '#i18n'
import { fetchAgreement, type UserAgreement } from '#api/user'
import { errorDetail } from '#lib/http'
import { useAuthStore } from '#stores/auth'

const SCOPE = 'AgreementView'

const authStore = useAuthStore()
const route = useRoute()
const router = useRouter()

const markdown = new MarkdownIt({ html: false, linkify: true, typographer: true })

const agreement = ref<UserAgreement | null>(null)
const loadError = ref<string | null>(null)
const acceptError = ref<string | null>(null)
const accepting = ref(false)

const rendered = computed(() => agreement.value?.text ? markdown.render(agreement.value.text) : '')
const redirectTarget = computed(() => typeof route.query.redirect === 'string' ? route.query.redirect : '/')
const mustAccept = computed(() => authStore.isAuthenticated && agreement.value?.published && !agreement.value.accepted)
const publishedLine = computed(() => t('Version {version}, published {date}', SCOPE, {
    version: agreement.value?.version ?? '',
    date: formatDate(agreement.value?.published_at ?? null),
}))
/** What the account's standing is once nothing is asked of it: the version it accepted, or that nothing is asked. */
const acceptedLine = computed(() => agreement.value?.accepted_version
    ? t('You accepted version {version} on {date}.', SCOPE, {
        version: agreement.value.accepted_version,
        date: formatDate(agreement.value.accepted_at),
    })
    : t('Nothing is asked of this account.', SCOPE))
const notPublishedLine = computed(() => t('This deployment has not published a user agreement.', SCOPE))

/**
 * Format an ISO timestamp as a date in the reader's locale.
 *
 * @param value - the timestamp, or null.
 */
function formatDate (value: string | null) {
    return value ? new Date(value).toLocaleDateString() : ''
}

async function load () {
    loadError.value = null
    try {
        agreement.value = await fetchAgreement()
    } catch (error) {
        loadError.value = errorDetail(error, t('The user agreement could not be loaded.', SCOPE))
    }
}

async function onAccept () {
    const version = agreement.value?.version
    if (!version) {
        return
    }
    accepting.value = true
    acceptError.value = null
    try {
        await authStore.acceptAgreement(version)
        await router.replace(redirectTarget.value)
    } catch (error) {
        const status = (error as { response?: { status?: number } })?.response?.status
        if (status === 409) {
            await load()
            acceptError.value = t(
                'A newer version was published while you were reading. Read it before accepting.',
                SCOPE,
            )
        } else {
            acceptError.value = errorDetail(error, t('Accepting the user agreement failed.', SCOPE))
        }
    } finally {
        accepting.value = false
    }
}

async function onContinue () {
    await router.replace(redirectTarget.value)
}

async function onSignOut () {
    await authStore.logout()
    await router.replace({ name: 'login' })
}

onMounted(async () => {
    await authStore.init()
    await load()
})
</script>

<template>
    <main class="agreement-view">
        <wa-scroller orientation="vertical">
            <div class="agreement-view__scroll-wrap">
                <h1>{{ t('User agreement', SCOPE) }}</h1>
                <p v-if="loadError" class="agreement-view__error">{{ loadError }}</p>
                <template v-else-if="agreement && !agreement.published">
                    <p class="agreement-view__meta">{{ notPublishedLine }}</p>
                </template>
                <template v-else-if="agreement">
                    <p class="agreement-view__meta">{{ publishedLine }}</p>
                    <p v-if="mustAccept" class="agreement-view__notice">
                        {{ t('Read the agreement below. You can use your account once you have accepted it.', SCOPE) }}
                    </p>
                    <article class="agreement-view__text" v-html="rendered"></article>
                    <div class="agreement-view__actions">
                        <template v-if="mustAccept">
                            <p v-if="acceptError" class="agreement-view__error">{{ acceptError }}</p>
                            <div class="agreement-view__buttons">
                                <wa-button appearance="plain" @click="onSignOut">{{ t('Sign out', SCOPE) }}</wa-button>
                                <wa-button
                                    appearance="filled-outlined"
                                    :loading="accepting"
                                    variant="brand"
                                    @click="onAccept"
                                >
                                    {{ t('I accept', SCOPE) }}
                                </wa-button>
                            </div>
                        </template>
                        <template v-else-if="authStore.isAuthenticated">
                            <p class="agreement-view__meta">{{ acceptedLine }}</p>
                            <div class="agreement-view__buttons">
                                <wa-button appearance="filled-outlined" variant="brand" @click="onContinue">
                                    {{ t('Continue', SCOPE) }}
                                </wa-button>
                            </div>
                        </template>
                        <p v-else class="agreement-view__meta">
                            {{ t('Signed-in users accept the agreement before using their account.', SCOPE) }}
                        </p>
                    </div>
                </template>
            </div>
        </wa-scroller>
    </main>
</template>

<style scoped>
.agreement-view {
    /* Fill the remaining vertical space within .route-view-wrapper so the internal wa-scroller
     * resolves its height against a bounded box. */
    display: flex;
    flex: 1;
    flex-direction: column;
    min-height: 0;
    overflow: hidden;
    padding: 2rem 1rem;
    width: 100%;
}

.agreement-view__scroll-wrap {
    margin: 0 auto;
    max-width: 720px;
    width: 100%;
}

.agreement-view h1 {
    margin: 0 0 0.5rem;
}

.agreement-view__actions {
    border-top: 1px solid var(--wa-color-surface-border);
    display: flex;
    flex-direction: column;
    gap: 0.75rem;
    margin-top: 1.5rem;
    padding-top: 1rem;
}

.agreement-view__buttons {
    display: flex;
    flex-wrap: wrap;
    gap: 0.5rem;
    justify-content: flex-end;
}

.agreement-view__error {
    color: var(--wa-color-danger-fill-loud);
    margin: 0;
}

.agreement-view__meta {
    color: var(--wa-color-text-quiet);
    margin: 0 0 1rem;
}

.agreement-view__notice {
    margin: 0 0 1rem;
}

.agreement-view__text {
    line-height: 1.6;
}
</style>
