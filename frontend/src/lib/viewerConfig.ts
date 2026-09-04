/**
 * Applying viewer setting overrides to a launched Epicurrents app.
 *
 * A viewer config is a flat map of dotted-path settings to values
 * (`{ 'eeg.defaultMontage': 'lon', 'eeg.trends.amplitude.epochLength': 2 }`).
 * The same shape is used for the project seed and the editable database
 * overrides; the effective config is their merge.
 *
 * @package    epicurrents-platform
 */

import type { EpicurrentsApp, SettingsValue } from '#epicurrents/core/dist/types'

/**
 * Backend the viewer mirrors a signed-in user's settings to, passed as the `userSettingsBackend`
 * property of the setup object and applied to the viewer's `app.userSettingsBackend` setting before
 * its modules load.
 *
 * Annotators do not always work from the same machine, so the viewer's own storage — session
 * storage, plus local storage when the settings cookie is on — is the wrong place for a preference
 * like the chosen montage to live alone. With this set, the viewer reads the account copy on top of
 * the device copy at startup and writes changes back to it.
 *
 * Only pass it for a signed-in session: the endpoint is session-authenticated, and a share-token or
 * anonymous viewer would just collect 401s.
 *
 * Built from `VITE_API_BASE_URL` for the same reason [`#lib/http`](http.ts) is — the viewer resolves
 * this with `fetch`, which would otherwise send it to the document origin and miss a split-origin
 * dev backend entirely.
 */
export const VIEWER_USER_SETTINGS_PATH =
    `${(import.meta.env.VITE_API_BASE_URL ?? '/').replace(/\/$/, '')}/api/v1/user/preferences?scope=viewer`

/** A flat map of dotted-path settings field → value. */
export type ViewerSettingsOverrides = Record<string, SettingsValue>

/** Minimal shape of a `setFieldValue`-bearing settings store. */
type SettingsWriter = { setFieldValue (field: string, value: SettingsValue): boolean }

/**
 * Apply a map of viewer setting overrides to a launched Epicurrents app.
 *
 * Each entry is routed through the viewer's interface-then-core `setFieldValue`
 * fallback — interface settings shadow core, mirroring the store's
 * `set-settings-value` mutation. An entry naming an unknown field, or a value of
 * the wrong type, is skipped with a warning so a bad config entry is visible
 * rather than silently dropped.
 *
 * Call after the modules are loaded (so the setting paths exist) and before
 * studies open (so per-recording reads such as `defaultMontage` and the aEEG
 * epoch length pick up the override).
 *
 * @param epic - The launched Epicurrents app.
 * @param overrides - Dotted-path field → value map.
 */
export function applyViewerSettingsOverrides (epic: EpicurrentsApp, overrides: ViewerSettingsOverrides): void {
    // INTERFACE is typed `unknown` on the runtime (an upstream typing gap); it
    // exposes the same setFieldValue contract as the core settings store.
    const intf = epic.runtime.INTERFACE as SettingsWriter
    const core = epic.runtime.SETTINGS
    for (const [field, value] of Object.entries(overrides)) {
        const applied = intf.setFieldValue(field, value) || core.setFieldValue(field, value)
        if (!applied) {
            console.warn(`[viewer-config] override '${field}' was not applied (unknown field or type mismatch).`)
        }
    }
}
