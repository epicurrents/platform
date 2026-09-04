<script setup lang="ts">
import { reactive, ref, onMounted } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { t } from '#i18n'
import { confirmPasswordReset } from '#api/user'

const SCOPE = 'ResetPasswordView'
const router = useRouter()
const route = useRoute()

const input = reactive({ newPassword: '', confirmPassword: '' })
const error = ref<string | null>(null)
const success = ref(false)
const loading = ref(false)
const invalidLink = ref(false)

const uid = ref('')
const token = ref('')

onMounted(() => {
    uid.value = typeof route.query.uid === 'string' ? route.query.uid : ''
    token.value = typeof route.query.token === 'string' ? route.query.token : ''
    if (!uid.value || !token.value) {
        invalidLink.value = true
    }
})

async function submit () {
    error.value = null
    if (input.newPassword !== input.confirmPassword) {
        error.value = t('Passwords do not match', SCOPE)
        return
    }
    loading.value = true
    try {
        await confirmPasswordReset(uid.value, token.value, input.newPassword)
        success.value = true
    } catch {
        error.value = t('Reset link is invalid or has expired.', SCOPE)
    } finally {
        loading.value = false
    }
}

function goToLogin () {
    router.push('/login')
}
</script>

<template>
    <main class="reset-view">
        <div class="reset-form">
            <h1>{{ t('Set new password', SCOPE) }}</h1>

            <wa-callout v-if="invalidLink" variant="danger">
                {{ t('This reset link is invalid. Please request a new one.', SCOPE) }}
            </wa-callout>

            <template v-else-if="success">
                <wa-callout variant="success">
                    {{ t('Your password has been reset. You can now sign in.', SCOPE) }}
                </wa-callout>
                <wa-button appearance="filled-outlined" variant="brand" @click="goToLogin">
                    {{ t('Go to sign in', SCOPE) }}
                </wa-button>
            </template>

            <form v-else @submit.prevent="submit">
                <wa-callout v-if="error" variant="danger">{{ error }}</wa-callout>
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
                    :loading="loading"
                    type="submit"
                    variant="brand"
                >
                    {{ t('Set password', SCOPE) }}
                </wa-button>
            </form>
        </div>
    </main>
</template>

<style scoped>
.reset-view {
    display: flex;
    justify-content: center;
    align-items: center;
    min-height: 100vh;
}

.reset-form {
    display: flex;
    flex-direction: column;
    gap: 1rem;
    width: 100%;
    max-width: 360px;
    padding: 2rem;
}

.reset-form h1 {
    margin: 0 0 0.5rem;
}

form {
    display: flex;
    flex-direction: column;
    gap: 1rem;
}
</style>
