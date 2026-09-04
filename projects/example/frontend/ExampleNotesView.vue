<template>
    <main class="example-notes-view">
        <header>
            <h1>{{ t('Project notes', SCOPE) }}</h1>
            <p class="example-notes-view__subtitle">
                {{ t('This page comes from the example project plugin.', SCOPE) }}
            </p>
        </header>
        <wa-card>
            <p>
                {{ t('A project plugin can register routes, navigation links, and icons of its own. Replace this view with your project UI.', SCOPE) }}
            </p>
            <wa-icon name="example-note"></wa-icon>
        </wa-card>
    </main>
</template>

<script setup lang="ts">
/**
 * Placeholder view for the example project — the frontend half of the scaffolded template.
 *
 * Demonstrates the conventions a project view follows: user-facing text through `t(key, SCOPE)`,
 * WebAwesome components, a project-registered icon, and scoped styles using WA colour tokens.
 */
import { t } from '#i18n'

const SCOPE = 'ExampleNotesView'
</script>

<style scoped>
.example-notes-view {
    margin: 0 auto;
    max-width: 48rem;
    padding: 1rem;
}

.example-notes-view__subtitle {
    color: var(--wa-color-text-quiet);
}
</style>
