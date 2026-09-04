<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import axios from 'axios'
import { t } from '#i18n'
import { useAuthStore } from '#stores/auth'
import { fetchAuthConfig, requestPasswordReset, type OIDCProvider } from '#api/user'

const SCOPE = 'LoginView'
const router = useRouter()
const route = useRoute()
const authStore = useAuthStore()

const input = reactive({ username: '', password: '', resetEmail: '', code: '' })
const error = ref<string | null>(null)
const loading = ref(false)

// The screen is in exactly one of three states, so it is one value rather than
// a set of booleans whose combinations would each need ruling out. 'twofactor'
// means the password was accepted and the account owes a code; the half-
// finished login lives on the server session, so nothing identifying is held
// here.
type Mode = 'signin' | 'twofactor' | 'enrol' | 'backupcodes' | 'reset'
const mode = ref<Mode>('signin')
const enrolmentSecret = ref('')
const provisioningUri = ref('')
const backupCodes = ref<string[]>([])
const resetMessage = ref<string | null>(null)
const resetRateLimited = ref(false)

const oidcProviders = ref<OIDCProvider[]>([])

onMounted(async () => {
    // Don't present the sign-in form to a visitor who already holds a valid session
    // (a shared cookie from another tab, a bookmark, or a back-navigation) — send
    // them straight into the app. Reuse the auth store's resolved state and only
    // probe the server when it has not resolved yet, so a visitor the store already
    // knows is logged out is not re-checked with a redundant /me request (the guard
    // has usually already established that on its way here).
    if (!authStore.initialized) {
        await authStore.refresh()
    }
    if (authStore.isAuthenticated) {
        const target = typeof route.query.redirect === 'string' ? route.query.redirect : '/'
        await router.replace(target)
        return
    }
    if (route.query.error === 'oidc') {
        const reason = typeof route.query.reason === 'string' ? route.query.reason : ''
        error.value = oidcErrorMessage(reason)
    }
    try {
        const config = await fetchAuthConfig()
        oidcProviders.value = config.oidc_providers
    } catch {
        oidcProviders.value = []
    }
})

function oidcErrorMessage (reason: string): string {
    switch (reason) {
        case 'domain_not_allowed':
            return t('Your account domain is not permitted to sign in here.', SCOPE)
        case 'auto_create_disabled':
            return t('No account exists for this identity. Contact an administrator.', SCOPE)
        default:
            return t('Sign-in with your identity provider failed. Please try again.', SCOPE)
    }
}

function startOidc (provider: OIDCProvider) {
    const redirect = typeof route.query.redirect === 'string' ? route.query.redirect : '/'
    window.location.assign(`${provider.login_url}?redirect=${encodeURIComponent(redirect)}`)
}

async function goToApp () {
    const redirect = typeof route.query.redirect === 'string' ? route.query.redirect : '/'
    await router.push(redirect)
}

async function submit () {
    loading.value = true
    error.value = null
    try {
        const outcome = await authStore.login(input.username, input.password)
        if (outcome === 'two_factor_enrolment_required') {
            // The password was right; this deployment requires a second factor
            // and the account has none. Enrolment happens here rather than
            // after signing in, because signing in is what is being withheld.
            input.password = ''
            try {
                const enrolment = await authStore.startEnrolment()
                enrolmentSecret.value = enrolment.secret
                provisioningUri.value = enrolment.provisioning_uri
                mode.value = 'enrol'
            } catch {
                // Its own catch: the outer one reports a bad password, which is
                // the one thing this is not — the password was just accepted.
                error.value = t('Could not start two-step setup. Please try again.', SCOPE)
            }
            return
        }
        if (outcome === 'two_factor_required') {
            mode.value = 'twofactor'
            input.password = ''
            return
        }
        await goToApp()
    } catch {
        error.value = t('Invalid username or password', SCOPE)
    } finally {
        loading.value = false
    }
}

async function submitCode () {
    loading.value = true
    error.value = null
    try {
        const codes = await authStore.completeTwoFactor(input.code)
        if (codes && codes.length > 0) {
            // Issued once and never retrievable again, so they are shown before
            // the app is entered rather than after.
            backupCodes.value = codes
            mode.value = 'backupcodes'
            input.code = ''
            return
        }
        await goToApp()
    } catch (err) {
        input.code = ''
        if (axios.isAxiosError(err) && err.response?.status === 429) {
            // The lockout and the half-finished login expire on the same clock,
            // so leaving the code prompt up would offer an input that cannot
            // succeed again in this attempt. Send them back to the password.
            mode.value = 'signin'
            error.value = t('Too many incorrect codes. Please wait a few minutes and sign in again.', SCOPE)
        } else {
            error.value = t('That code was not accepted. Check your authenticator and try again.', SCOPE)
        }
    } finally {
        loading.value = false
    }
}

function cancelTwoFactor () {
    mode.value = 'signin'
    input.code = ''
    input.password = ''
    enrolmentSecret.value = ''
    provisioningUri.value = ''
    error.value = null
}

async function finishEnrolment () {
    backupCodes.value = []
    await goToApp()
}

async function sendResetLink () {
    loading.value = true
    error.value = null
    resetMessage.value = null
    try {
        await requestPasswordReset(input.resetEmail)
        resetMessage.value = t('If that email is registered you will receive a reset link shortly.', SCOPE)
        resetRateLimited.value = true
        setTimeout(() => { resetRateLimited.value = false }, 5 * 60 * 1000)
    } catch (err) {
        if (axios.isAxiosError(err) && err.response?.status === 429) {
            resetRateLimited.value = true
            setTimeout(() => { resetRateLimited.value = false }, 5 * 60 * 1000)
            error.value = t('You can only request a reset link once every 5 minutes. Please wait and try again.', SCOPE)
        } else {
            error.value = t('Failed to send reset email. Please try again.', SCOPE)
        }
    } finally {
        loading.value = false
    }
}

function toggleForgot () {
    mode.value = mode.value === 'reset' ? 'signin' : 'reset'
    error.value = null
    resetMessage.value = null
    resetRateLimited.value = false
}
</script>

<template>
    <main class="login-view">
        <form v-if="mode === 'enrol'" class="login-form" @submit.prevent="submitCode">
            <h1>{{ t('Set up two-step verification', SCOPE) }}</h1>
            <p class="login-hint">
                {{ t('This account needs a second factor before it can sign in. Scan the code with your authenticator app, then enter the code it shows.', SCOPE) }}
            </p>
            <wa-callout v-if="error" variant="danger">{{ error }}</wa-callout>
            <div class="login-qr wa-light">
                <wa-qr-code
                    background="white"
                    fill="black"
                    :label="t('Two-step verification setup code', SCOPE)"
                    size="256"
                    :value="provisioningUri"
                ></wa-qr-code>
            </div>
            <p class="login-hint">
                {{ t('Cannot scan? Enter this key instead:', SCOPE) }}
                <code class="login-secret">{{ enrolmentSecret }}</code>
            </p>
            <wa-input
                autocomplete="one-time-code"
                :label="t('Code from your authenticator', SCOPE)"
                required
                size="s"
                type="text"
                v-wa="[input, 'code']"
            ></wa-input>
            <wa-button
                appearance="filled-outlined"
                :loading="loading"
                type="submit"
                variant="brand"
            >
                {{ t('Confirm and sign in', SCOPE) }}
            </wa-button>
            <button type="button" class="forgot-link" @click="cancelTwoFactor">
                {{ t('Cancel', SCOPE) }}
            </button>
        </form>

        <div v-if="mode === 'backupcodes'" class="login-form">
            <h1>{{ t('Save your recovery codes', SCOPE) }}</h1>
            <p class="login-hint">
                {{ t('These are shown once and cannot be retrieved again. Store them somewhere other than the device holding your authenticator — each one signs you in if you lose it.', SCOPE) }}
            </p>
            <ul class="login-backup-codes">
                <li v-for="code in backupCodes" :key="code">{{ code }}</li>
            </ul>
            <wa-button
                appearance="filled-outlined"
                type="button"
                variant="brand"
                @click="finishEnrolment"
            >
                {{ t('I have saved them, continue', SCOPE) }}
            </wa-button>
        </div>

        <form v-if="mode === 'twofactor'" class="login-form" @submit.prevent="submitCode">
            <h1>{{ t('Two-step verification', SCOPE) }}</h1>
            <p class="login-hint">
                {{ t('Enter the six-digit code from your authenticator app, or one of your recovery codes.', SCOPE) }}
            </p>
            <wa-callout v-if="error" variant="danger">{{ error }}</wa-callout>
            <wa-input
                autocomplete="one-time-code"
                :label="t('Verification code', SCOPE)"
                required
                size="s"
                type="text"
                v-wa="[input, 'code']"
            ></wa-input>
            <wa-button
                appearance="filled-outlined"
                :loading="loading"
                type="submit"
                variant="brand"
            >
                {{ t('Verify', SCOPE) }}
            </wa-button>
            <button type="button" class="forgot-link" @click="cancelTwoFactor">
                {{ t('Back to sign in', SCOPE) }}
            </button>
        </form>

        <form v-else-if="mode === 'signin'" class="login-form" @submit.prevent="submit">
            <h1>{{ t('Sign in', SCOPE) }}</h1>
            <wa-callout v-if="error" variant="danger">{{ error }}</wa-callout>
            <wa-input
                autocomplete="username"
                :label="t('Username', SCOPE)"
                required
                size="s"
                type="text"
                v-wa="[input, 'username']"
            ></wa-input>
            <wa-input
                autocomplete="current-password"
                :label="t('Password', SCOPE)"
                required
                size="s"
                type="password"
                v-wa="[input, 'password']"
            ></wa-input>
            <wa-button
                appearance="filled-outlined"
                :loading="loading"
                type="submit"
                variant="brand"
            >
                {{ t('Sign in', SCOPE) }}
            </wa-button>
            <button type="button" class="forgot-link" @click="toggleForgot">
                {{ t('Forgot password?', SCOPE) }}
            </button>
        </form>

        <div v-if="mode === 'signin' && oidcProviders.length" class="oidc-methods">
            <div class="oidc-divider">{{ t('or', SCOPE) }}</div>
            <wa-button
                v-for="provider in oidcProviders"
                :key="provider.name"
                appearance="outlined"
                class="oidc-button"
                type="button"
                @click="startOidc(provider)"
            >
                {{ t('Sign in with {provider}', SCOPE, { provider: provider.label }) }}
            </wa-button>
        </div>

        <form v-if="mode === 'reset'" class="login-form" @submit.prevent="sendResetLink">
            <h1>{{ t('Reset password', SCOPE) }}</h1>
            <wa-callout v-if="error" variant="danger">{{ error }}</wa-callout>
            <wa-callout v-if="resetMessage" variant="success">{{ resetMessage }}</wa-callout>
            <wa-input
                autocomplete="email"
                :label="t('Email address', SCOPE)"
                required
                size="s"
                type="email"
                v-wa="[input, 'resetEmail']"
            ></wa-input>
            <wa-button
                appearance="filled-outlined"
                :disabled="resetRateLimited || loading"
                :loading="loading"
                type="submit"
                variant="brand"
            >
                {{ t('Send reset link', SCOPE) }}
            </wa-button>
            <button type="button" class="forgot-link" @click="toggleForgot">
                {{ t('Back to sign in', SCOPE) }}
            </button>
        </form>
    </main>
</template>

<style scoped>
/* Black-on-white regardless of colour mode, and a definite size. See the
 * wa-qr-code notes in AGENTS.md: the component host has no intrinsic width and
 * its fill falls back to the theme's text colour, either of which yields a code
 * no camera will read. */
.login-qr {
    align-self: center;
    background-color: white;
    border-radius: var(--wa-border-radius-m);
    color: black;
    display: inline-flex;
    padding: 0.75rem;
}

.login-qr wa-qr-code {
    background-color: white;
    color: black;
    height: 256px;
    width: 256px;
}

.login-secret {
    font-family: var(--wa-font-family-code, monospace);
    letter-spacing: 0.05em;
    user-select: all;
    word-break: break-all;
}

.login-backup-codes {
    display: grid;
    font-family: var(--wa-font-family-code, monospace);
    gap: 0.25rem 1rem;
    grid-template-columns: 1fr 1fr;
    list-style: none;
    margin: 0;
    padding: 0;
    user-select: all;
}

.login-view {
    display: flex;
    justify-content: center;
    align-items: center;
    min-height: 100vh;
}

.login-form {
    display: flex;
    flex-direction: column;
    gap: 1rem;
    width: 100%;
    max-width: 360px;
    padding: 2rem;
}

.login-form h1 {
    margin: 0 0 0.5rem;
}

.login-hint {
    color: var(--wa-color-text-quiet);
    font-size: 0.875rem;
    margin: 0;
}

.forgot-link {
    background: none;
    border: none;
    padding: 0;
    color: var(--wa-color-brand-600, #0066cc);
    cursor: pointer;
    font-size: 0.875rem;
    text-align: center;
    text-decoration: underline;
}

.forgot-link:hover {
    color: var(--wa-color-brand-700, #0052a3);
}

.oidc-methods {
    display: flex;
    flex-direction: column;
    gap: 0.75rem;
    width: 100%;
    max-width: 360px;
    padding: 0 2rem 2rem;
}

.oidc-divider {
    color: var(--wa-color-text-quiet);
    font-size: 0.875rem;
    text-align: center;
}

.oidc-button {
    width: 100%;
}
</style>
