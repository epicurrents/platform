import { createI18n } from 'vue-i18n'

/**
 * Base locale messages for the frontend shell.
 */
const messages = {
    en: {
        CollectionView: {
            // i18n-t entries (required because the custom t() fallback does not apply to <i18n-t>)
            move_to_trash_confirm: 'Move collection {name} and everything in it to the trash? Sub-collections go too, and recordings return to the library root — nothing is deleted, and you can restore it all later.',
            create_collection_inside: 'Inside {name}',
            move_conflict_intro: 'The following names already exist in {target}:',
        },
            DatasetsView: {
                // i18n-t entries (required because the custom t() fallback does not apply to <i18n-t>)
                move_to_trash_confirm: 'Move dataset {name} to the trash? Recordings inside are not deleted; they will lose shared access until the dataset is restored.',
            },
        DatasetView: {
            // i18n-t entries (required because the custom t() fallback does not apply to <i18n-t>)
            move_to_trash_confirm: 'Move dataset {name} to the trash? Shared users will lose access until the dataset is restored.',
        },
        EduSessionsView: {
            // i18n-t entries (required because the custom t() fallback does not apply to <i18n-t>)
            delete_session_confirm: 'Delete session {name}? This action cannot be undone.',
        },
            HomeView: {
                // i18n-t entries (required because the custom t() fallback does not apply to <i18n-t>)
                move_to_trash_confirm: 'Move {name} to the trash? It can be restored via the activity log.',
            },
            LibraryView: {
                // i18n-t entries (required because the custom t() fallback does not apply to <i18n-t>)
                library_callout: 'Collections are personal folders for organising your recordings. Use {dataset} (via the nav) to share recordings with others.',
                move_to_trash_confirm: 'Move collection {name} and everything in it to the trash? Sub-collections go too, and recordings return to the library root — nothing is deleted, and you can restore it all later.',
            },
    },
}

/**
 * Global Vue I18n instance.
 */
export const i18n = createI18n({
    legacy: false,
    locale: 'en',
    fallbackLocale: 'en',
    messages,
})
/**
 * Override the default I18n translate method.
 * Returns a component-specific translation (default) or a
 * general translation (fallback) for the given key string.
 *
 * Uses i18n.global.t directly so it is safe to call from async functions
 * and Pinia stores — i.e. outside a Vue component setup context where
 * useI18n() / inject() would throw MUST_BE_CALL_SETUP_TOP.
 */
export const t = function (key: string, scope: string, params = {}, capitalized = false) {
    const message = i18n.global.t(`${scope}.${key}`, key, { named: params })
    return capitalized ? message.substring(0, 1).toLocaleUpperCase() + message.substring(1)
                       : message
}
