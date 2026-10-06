import { defineStore } from 'pinia'
import { ref, watch } from 'vue'

export type ThemeMode = 'auto' | 'light' | 'dark'

const STORAGE_KEY = 'epicurrents-theme'

function systemIsDark(): boolean {
    return window.matchMedia('(prefers-color-scheme: dark)').matches
}

function applyTheme(mode: ThemeMode, lightOnly: boolean): void {
    const isDark = !lightOnly && (mode === 'dark' || (mode === 'auto' && systemIsDark()))
    document.documentElement.classList.toggle('wa-dark', isDark)
    document.documentElement.classList.toggle('wa-light', !isDark)
}

export const useThemeStore = defineStore('theme', () => {
    const mode = ref<ThemeMode>(
        (localStorage.getItem(STORAGE_KEY) as ThemeMode | null) ?? 'auto'
    )
    // Set while the current route hosts the viewer, which only has a light theme: a dark interface around it would
    // put dark drawers and dialogs against a light signal display. The user's choice is kept and applies again on
    // the next route without the flag.
    const lightOnly = ref(false)

    watch(mode, (m) => {
        localStorage.setItem(STORAGE_KEY, m)
    })
    watch([mode, lightOnly], ([m, light]) => applyTheme(m, light), { immediate: true })

    // Re-apply when the OS preference changes (only matters in auto mode).
    window.matchMedia('(prefers-color-scheme: dark)')
        .addEventListener('change', () => {
            if (mode.value === 'auto') applyTheme('auto', lightOnly.value)
        })

    function setMode(m: ThemeMode) {
        mode.value = m
    }

    function setLightOnly(value: boolean) {
        lightOnly.value = value
    }

    return { lightOnly, mode, setLightOnly, setMode }
})
