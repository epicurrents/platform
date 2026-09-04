<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import axios from 'axios'
import { t } from '#i18n'
import { useAuthStore } from '#stores/auth'
import {
    updateProfile,
    changePassword,
    confirmTwoFactorEnrolment,
    disableTwoFactor,
    fetchTwoFactorStatus,
    regenerateBackupCodes,
    startTwoFactorEnrolment,
    type TwoFactorStatus,
} from '#api/user'

const SCOPE = 'ProfileView'
const authStore = useAuthStore()

const input = reactive({
    firstName: authStore.user?.first_name ?? '',
    lastName: authStore.user?.last_name ?? '',
    email: authStore.user?.email ?? '',
    currentPassword: '',
    newPassword: '',
    confirmPassword: '',
    twoFactorPassword: '',
    twoFactorCode: '',
})
const profileError = ref<string | null>(null)
const profileSuccess = ref(false)
const profileLoading = ref(false)
const passwordError = ref<string | null>(null)
const passwordSuccess = ref(false)
const passwordLoading = ref(false)

async function submitProfile () {
    profileLoading.value = true
    profileError.value = null
    profileSuccess.value = false
    try {
        const updated = await updateProfile({
            email: input.email,
            first_name: input.firstName,
            last_name: input.lastName,
        })
        authStore.user = updated
        profileSuccess.value = true
    } catch {
        profileError.value = t('Failed to update profile. Please try again.', SCOPE)
    } finally {
        profileLoading.value = false
    }
}

// ── Two-step verification ────────────────────────────────────────────────────
// Three states, driven by what the server reports plus what this page has done
// since it loaded: not enrolled, mid-enrolment (a pending secret awaiting a
// code), and enabled. `backupCodes` is populated only by the two calls that
// return them, because that response is the one and only time they exist.

const twoFactorStatus = ref<TwoFactorStatus | null>(null)
const twoFactorError = ref<string | null>(null)
const twoFactorLoading = ref(false)
const provisioningUri = ref<string | null>(null)
const enrolmentSecret = ref<string | null>(null)
const backupCodes = ref<string[] | null>(null)

onMounted(loadTwoFactorStatus)

async function loadTwoFactorStatus () {
    try {
        twoFactorStatus.value = await fetchTwoFactorStatus()
    } catch {
        twoFactorStatus.value = null
    }
}

function clearTwoFactorInputs () {
    input.twoFactorPassword = ''
    input.twoFactorCode = ''
}

/**
 * Prefer the server's own message on a 409, fall back to the generic one.
 *
 * A 409 here means the account is in a state the request cannot apply to, and
 * the server says which — most usefully for an account that signs in through an
 * external provider, where "check your password" is not just unhelpful but
 * wrong, since the account has no password to check.
 */
function twoFactorErrorFrom (err: unknown, fallback: string): string {
    if (axios.isAxiosError(err) && err.response?.status === 409) {
        const detail = (err.response.data as { detail?: unknown } | undefined)?.detail
        if (typeof detail === 'string' && detail) {
            return detail
        }
    }
    return fallback
}

function cancelEnrolment () {
    provisioningUri.value = null
    enrolmentSecret.value = null
    twoFactorError.value = null
    clearTwoFactorInputs()
}

async function startEnrolment () {
    twoFactorLoading.value = true
    twoFactorError.value = null
    backupCodes.value = null
    try {
        const enrolment = await startTwoFactorEnrolment(input.twoFactorPassword)
        provisioningUri.value = enrolment.provisioning_uri
        enrolmentSecret.value = enrolment.secret
        input.twoFactorPassword = ''
    } catch (err) {
        twoFactorError.value = twoFactorErrorFrom(
            err,
            t('Could not start setup. Check your password and try again.', SCOPE),
        )
    } finally {
        twoFactorLoading.value = false
    }
}

async function confirmEnrolment () {
    twoFactorLoading.value = true
    twoFactorError.value = null
    try {
        backupCodes.value = await confirmTwoFactorEnrolment(input.twoFactorCode)
        provisioningUri.value = null
        enrolmentSecret.value = null
        clearTwoFactorInputs()
        await loadTwoFactorStatus()
        await authStore.refresh()
    } catch {
        input.twoFactorCode = ''
        twoFactorError.value = t('That code was not accepted. Check your authenticator and try again.', SCOPE)
    } finally {
        twoFactorLoading.value = false
    }
}

async function newBackupCodes () {
    twoFactorLoading.value = true
    twoFactorError.value = null
    try {
        backupCodes.value = await regenerateBackupCodes(input.twoFactorPassword)
        clearTwoFactorInputs()
        await loadTwoFactorStatus()
    } catch (err) {
        twoFactorError.value = twoFactorErrorFrom(
            err,
            t('Could not issue new recovery codes. Check your password and try again.', SCOPE),
        )
    } finally {
        twoFactorLoading.value = false
    }
}

async function turnOffTwoFactor () {
    twoFactorLoading.value = true
    twoFactorError.value = null
    try {
        await disableTwoFactor(input.twoFactorPassword)
        backupCodes.value = null
        clearTwoFactorInputs()
        await loadTwoFactorStatus()
        await authStore.refresh()
    } catch (err) {
        twoFactorError.value = twoFactorErrorFrom(
            err,
            t('Could not turn off two-step verification. Check your password and try again.', SCOPE),
        )
    } finally {
        twoFactorLoading.value = false
    }
}

async function submitPassword () {
    passwordError.value = null
    passwordSuccess.value = false
    if (input.newPassword !== input.confirmPassword) {
        passwordError.value = t('New passwords do not match.', SCOPE)
        return
    }
    passwordLoading.value = true
    try {
        await changePassword(input.currentPassword, input.newPassword)
        input.currentPassword = ''
        input.newPassword = ''
        input.confirmPassword = ''
        passwordSuccess.value = true
    } catch {
        passwordError.value = t('Failed to change password. Check your current password and try again.', SCOPE)
    } finally {
        passwordLoading.value = false
    }
}
</script>

<template>
    <main class="profile-view">
        <wa-scroller orientation="vertical">
            <div class="profile-view__scroll-wrap">
                <h1>{{ t('Profile', SCOPE) }}</h1>

                <section class="profile-section">
                    <h2>{{ t('Personal information', SCOPE) }}</h2>
                    <form @submit.prevent="submitProfile">
                        <wa-callout v-if="profileError" variant="danger">{{ profileError }}</wa-callout>
                        <wa-callout v-if="profileSuccess" variant="success">{{ t('Profile updated.', SCOPE) }}</wa-callout>
                        <wa-input
                            autocomplete="given-name"
                            :label="t('First name', SCOPE)"
                            size="s"
                            type="text"
                            v-wa="[input, 'firstName']"
                        ></wa-input>
                        <wa-input
                            autocomplete="family-name"
                            :label="t('Last name', SCOPE)"
                            size="s"
                            type="text"
                            v-wa="[input, 'lastName']"
                        ></wa-input>
                        <wa-input
                            autocomplete="email"
                            :label="t('Email', SCOPE)"
                            required
                            size="s"
                            type="email"
                            v-wa="[input, 'email']"
                        ></wa-input>
                        <wa-button
                            appearance="filled-outlined"
                            :loading="profileLoading"
                            type="submit"
                            variant="brand"
                        >
                            {{ t('Save changes', SCOPE) }}
                        </wa-button>
                    </form>
                </section>

                <section class="profile-section">
                    <h2>{{ t('Change password', SCOPE) }}</h2>
                    <form @submit.prevent="submitPassword">
                        <wa-callout v-if="passwordError" variant="danger">{{ passwordError }}</wa-callout>
                        <wa-callout v-if="passwordSuccess" variant="success">{{ t('Password changed.', SCOPE) }}</wa-callout>
                        <wa-input
                            autocomplete="current-password"
                            :label="t('Current password', SCOPE)"
                            required
                            size="s"
                            type="password"
                            v-wa="[input, 'currentPassword']"
                        ></wa-input>
                        <wa-input
                            autocomplete="new-password"
                            :label="t('New password', SCOPE)"
                            required
                            size="s"
                            type="password"
                            v-wa="[input, 'newPassword']"
                        ></wa-input>
                        <wa-input
                            autocomplete="new-password"
                            :label="t('Confirm new password', SCOPE)"
                            required
                            size="s"
                            type="password"
                            v-wa="[input, 'confirmPassword']"
                        ></wa-input>
                        <wa-button
                            appearance="filled-outlined"
                            :loading="passwordLoading"
                            type="submit"
                            variant="brand"
                        >
                            {{ t('Change password', SCOPE) }}
                        </wa-button>
                    </form>
                </section>

                <section class="profile-section">
                    <h2>{{ t('Two-step verification', SCOPE) }}</h2>
                    <p class="profile-hint">
                        {{ t('Ask for a code from an authenticator app in addition to your password when you sign in.', SCOPE) }}
                    </p>
                    <wa-callout v-if="twoFactorError" variant="danger">{{ twoFactorError }}</wa-callout>

                    <wa-callout v-if="backupCodes" variant="warning">
                        {{ t('Save these recovery codes somewhere safe. Each one signs you in once if you lose your authenticator, and they will not be shown again.', SCOPE) }}
                    </wa-callout>
                    <ul v-if="backupCodes" class="backup-codes">
                        <li v-for="code in backupCodes" :key="code">{{ code }}</li>
                    </ul>

                    <form v-if="!twoFactorStatus?.enabled && !provisioningUri" @submit.prevent="startEnrolment">
                        <wa-input
                            autocomplete="current-password"
                            :label="t('Confirm your password to begin', SCOPE)"
                            required
                            size="s"
                            type="password"
                            v-wa="[input, 'twoFactorPassword']"
                        ></wa-input>
                        <wa-button
                            appearance="filled-outlined"
                            :loading="twoFactorLoading"
                            type="submit"
                            variant="brand"
                        >
                            {{ t('Set up two-step verification', SCOPE) }}
                        </wa-button>
                    </form>

                    <div v-if="provisioningUri" class="enrolment">
                        <p class="profile-hint">
                            {{ t('Scan this with your authenticator app, then enter the code it shows.', SCOPE) }}
                        </p>
                        <div class="enrolment__qr wa-light">
                            <wa-qr-code
                                background="white"
                                fill="black"
                                :label="t('Two-step verification setup code', SCOPE)"
                                size="256"
                                :value="provisioningUri"
                            ></wa-qr-code>
                        </div>
                        <p class="profile-hint">
                            {{ t('Cannot scan? Enter this key instead:', SCOPE) }}
                            <code class="secret">{{ enrolmentSecret }}</code>
                        </p>
                        <form @submit.prevent="confirmEnrolment">
                            <wa-input
                                autocomplete="one-time-code"
                                :label="t('Code from your authenticator', SCOPE)"
                                required
                                size="s"
                                type="text"
                                v-wa="[input, 'twoFactorCode']"
                            ></wa-input>
                            <wa-button
                                appearance="filled-outlined"
                                :loading="twoFactorLoading"
                                type="submit"
                                variant="brand"
                            >
                                {{ t('Turn on', SCOPE) }}
                            </wa-button>
                            <wa-button appearance="plain" type="button" @click="cancelEnrolment">
                                {{ t('Cancel', SCOPE) }}
                            </wa-button>
                        </form>
                    </div>

                    <div v-if="twoFactorStatus?.enabled" class="enrolled">
                        <wa-callout variant="success">
                            {{ t('Two-step verification is on. Recovery codes remaining: {count}.', SCOPE, { count: twoFactorStatus.backup_codes_remaining }) }}
                        </wa-callout>
                        <form @submit.prevent="newBackupCodes">
                            <wa-input
                                autocomplete="current-password"
                                :label="t('Confirm your password to make a change', SCOPE)"
                                required
                                size="s"
                                type="password"
                                v-wa="[input, 'twoFactorPassword']"
                            ></wa-input>
                            <wa-button appearance="plain" :loading="twoFactorLoading" type="submit" variant="brand">
                                {{ t('Issue new recovery codes', SCOPE) }}
                            </wa-button>
                            <wa-button
                                appearance="plain"
                                :loading="twoFactorLoading"
                                type="button"
                                variant="danger"
                                @click="turnOffTwoFactor"
                            >
                                {{ t('Turn off two-step verification', SCOPE) }}
                            </wa-button>
                        </form>
                    </div>
                </section>
            </div>
        </wa-scroller>
    </main>
</template>

<style scoped>
.profile-view {
    /* Fill the remaining vertical space within .route-view-wrapper so the
     * internal wa-scroller resolves its height against a bounded box. The
     * padding stays on the host so it shows as a visible inset around the
     * scroller — content scrolls within the inset rather than sliding
     * flush with the viewport edges. */
    flex: 1;
    display: flex;
    flex-direction: column;
    min-height: 0;
    overflow: hidden;
    width: 100%;
    padding: 2rem 1rem;
}

.profile-view__scroll-wrap {
    /* Centring lives here, inside the scroller, rather than on the host: the
     * scroller has to span the full width so its scrollbar rides the viewport
     * edge instead of the content column. Flex-1 wrap for wa-scroller — see
     * AGENTS.md → WebAwesome shadow-DOM layout gotchas. */
    flex: 1;
    display: flex;
    flex-direction: column;
    margin: 0 auto;
    max-width: 480px;
    min-height: 0;
    overflow: hidden;
    width: 100%;
}

.profile-view h1 {
    margin: 0 0 2rem;
}

.profile-section {
    display: flex;
    flex-direction: column;
    gap: 1rem;
    margin-bottom: 3rem;
}

.profile-section h2 {
    margin: 0;
}

.profile-section form {
    display: flex;
    flex-direction: column;
    gap: 1rem;
}

.profile-hint {
    color: var(--wa-color-text-quiet);
    font-size: 0.875rem;
    margin: 0;
}

.enrolment,
.enrolled {
    display: flex;
    flex-direction: column;
    gap: 1rem;
}

/* The QR code keeps black-on-white regardless of colour mode. A code drawn in
 * theme tokens loses the contrast a camera needs in one of the two modes, and
 * this is the sanctioned invariant-colour exception in AGENTS.md.
 *
 * `color` and `background-color` are the load-bearing pair, not the fill and
 * background attributes on the tag. Those are deprecated in WebAwesome, and the
 * component falls back to the host's computed `color` when they are absent — so
 * the day they are removed, a QR without these two lines is drawn in the
 * theme's text colour, which in dark mode is a light code on a light card. The
 * attributes stay for the current version; these lines are what survive it.
 *
 * The explicit width is equally load-bearing, for a different reason. The
 * component's host is `display: inline-flex; aspect-ratio: 1` with no intrinsic
 * width, and its canvas is `width: 100%` — a percentage resolved against a
 * content-sized parent, which collapses. The component only ever sets
 * `max-width`, never a width. The canvas is rendered at twice `size` and then
 * downscaled into whatever the layout gave it, so a collapsed host does not
 * merely look small: downsampling a 1-bit pattern greys it out, worst on the
 * three finder squares, and a camera stops recognising it. Same shape as the
 * `wa-tab-panel` trap in AGENTS.md — a definite-looking host whose inner
 * element has nothing to size against. */
/* The wrapper carries `wa-light`, which is the point rather than decoration.
 * The component draws the code in its own `fill` when that is set and in the
 * host's computed `color` otherwise, and `fill` is deprecated — so in dark mode
 * the fallback is light-on-transparent, which is a code no camera will read.
 * A `wa-light` scope makes every WebAwesome token inside resolve to the light
 * palette, so the fallback lands on a dark colour by construction instead of by
 * an override that has to keep winning a specificity argument.
 *
 * The explicit colours below are the second of three belts: attribute, theme
 * scope, host colour. Any one of them is sufficient today, and which one is
 * sufficient tomorrow depends on when WebAwesome finishes removing the
 * attributes. */
.enrolment__qr {
    align-self: center;
    background-color: white;
    border-radius: var(--wa-border-radius-m);
    color: black;
    display: inline-flex;
    padding: 0.75rem;
}

/* The width is load-bearing for a different reason than the colours. The
 * component's host is `display: inline-flex; aspect-ratio: 1` with no intrinsic
 * width and a canvas at `width: 100%`, and the component sets only `max-width`.
 * A percentage against a content-sized parent collapses, and since the canvas is
 * rendered at twice `size` and downscaled into whatever the layout gave it, a
 * collapsed host does not merely look small — downsampling a 1-bit pattern greys
 * it out, worst on the three finder squares, and the code stops scanning. Same
 * shape as the `wa-tab-panel` trap in AGENTS.md. */
.enrolment wa-qr-code {
    background-color: white;
    color: black;
    height: 256px;
    width: 256px;
}

.secret {
    font-family: var(--wa-font-family-code, monospace);
    letter-spacing: 0.05em;
    user-select: all;
    word-break: break-all;
}

.backup-codes {
    display: grid;
    gap: 0.25rem 1rem;
    grid-template-columns: repeat(auto-fit, minmax(11rem, 1fr));
    font-family: var(--wa-font-family-code, monospace);
    list-style: none;
    margin: 0;
    padding: 0.75rem;
    background: var(--wa-color-surface-lowered);
    border-radius: var(--wa-border-radius-m);
    user-select: all;
}
</style>
