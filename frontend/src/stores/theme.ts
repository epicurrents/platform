import { defineStore } from 'pinia'
import { ref, watch } from 'vue'

export type ThemeMode = 'auto' | 'light' | 'dark'

const STORAGE_KEY = 'epicurrents-theme'

function systemIsDark(): boolean {
    return window.matchMedia('(prefers-color-scheme: dark)').matches
}

function applyTheme(mode: ThemeMode): void {
    const isDark = mode === 'dark' || (mode === 'auto' && systemIsDark())
    document.documentElement.classList.toggle('wa-dark', isDark)
    document.documentElement.classList.toggle('wa-light', !isDark)
}

export const useThemeStore = defineStore('theme', () => {
    const mode = ref<ThemeMode>(
        (localStorage.getItem(STORAGE_KEY) as ThemeMode | null) ?? 'auto'
    )

    watch(mode, (m) => {
        localStorage.setItem(STORAGE_KEY, m)
        applyTheme(m)
    }, { immediate: true })

    // Re-apply when the OS preference changes (only matters in auto mode).
    window.matchMedia('(prefers-color-scheme: dark)')
        .addEventListener('change', () => {
            if (mode.value === 'auto') applyTheme('auto')
        })

    function setMode(m: ThemeMode) {
        mode.value = m
    }

    return { mode, setMode }
})
