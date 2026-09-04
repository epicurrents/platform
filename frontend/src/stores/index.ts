import { createPinia } from 'pinia'
import { useAuthStore } from './auth'
import { useLibraryStore } from './library'
import { useRecordingsStore } from './recordings'

/**
 * Root Pinia instance used by the Vue app.
 */
export const pinia = createPinia()

export {
    useAuthStore,
    useLibraryStore,
    useRecordingsStore,
}