/**
 * Viewer-config overrides API — the deployment's editable viewer settings.
 *
 * @package    epicurrents-platform
 */

import { http } from '#lib/http'
import type { ViewerSettingsOverrides } from '#lib/viewerConfig'

/** Response shape of the viewer-config endpoints. */
export interface ViewerConfig {
    /** The active project's read-only seed (`projects/<name>/viewer-config.json`). */
    seed: ViewerSettingsOverrides
    /** The editable database overrides layered on the seed. */
    overrides: ViewerSettingsOverrides
    /** Seed merged with overrides (overrides win) — what the viewer applies. */
    effective: ViewerSettingsOverrides
}

/** Fetch the effective viewer config (seed + overrides) for the active project. */
export async function getViewerConfig(): Promise<ViewerConfig> {
    const { data } = await http.get<ViewerConfig>('/api/v1/viewer-config')
    return data
}

/** Replace the editable overrides; returns the new overrides and merged effective config. */
export async function updateViewerConfig(
    overrides: ViewerSettingsOverrides,
): Promise<{ overrides: ViewerSettingsOverrides, effective: ViewerSettingsOverrides }> {
    const { data } = await http.put<{ overrides: ViewerSettingsOverrides, effective: ViewerSettingsOverrides }>(
        '/api/v1/viewer-config',
        overrides,
    )
    return data
}
