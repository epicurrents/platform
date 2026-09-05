/**
 * Standalone entry point that hands the platform's lead-field provider to the public viewer page.
 *
 * The public viewer at `/viewer/<mode>` is served by `epicurrents.views.public_viewer_view`, which
 * builds its SETUP with `json.dumps` from `settings.PUBLIC_VIEWER_MODES`. A provider is a function,
 * so no value in that dict can express one and the page had no way to receive it — the EEG module
 * came up with no lead-field source and reported source localisation unavailable for every montage.
 * The SPA supplies the provider by importing it (`views/ViewerView.vue`) and the per-project viewer
 * build bakes it in (`viewer/base.ts`); this file is the third host, for the one page that runs no
 * platform JavaScript of its own.
 *
 * Built by `vite.config.leadfields.ts` into `viewer-dist/epicurrents-leadfields.js` and loaded by
 * the page after its SETUP declaration and before the viewer library, so the library reads a SETUP
 * that already carries the provider rather than one amended behind it.
 */

import { leadFieldProvider } from './leadFields'

type ViewerSetup = {
    modules?: Record<string, unknown>
}

const globalScope = window as typeof window & {
    __EPICURRENTS__?: { SETUP?: ViewerSetup }
}
const setup = globalScope.__EPICURRENTS__?.SETUP

if (!setup) {
    // Loading before the page has declared its SETUP, or on a page that declares none. Warn rather
    // than throw: the viewer still runs, only without source localisation, and a thrown error here
    // would be indistinguishable from the library itself failing to load.
    console.warn(
        '[leadFields] window.__EPICURRENTS__.SETUP was not found; the source-localisation tool will '
        + 'report every montage as unavailable.'
    )
} else {
    // Both levels are guarded because this runs against raw JSON from `PUBLIC_VIEWER_MODES`,
    // before the viewer has looked at it — the declared type says these are objects and the
    // deployment's settings decide whether they are. `modules.eeg` as a URL string is the shape
    // JSON can carry and a provider cannot, and the object form wins exactly as in
    // `viewer/base.ts`. A non-object `modules` is a config error, but writing a property onto a
    // primitive is a silent no-op in a non-strict bundle, so the substitution below is what keeps
    // it from presenting as "the script ran and source localisation is still unavailable".
    const existingModules = setup.modules
    const modules = typeof existingModules === 'object' && existingModules !== null
        ? existingModules
        : {}
    const eegConfig = modules.eeg
    modules.eeg = {
        ...(typeof eegConfig === 'object' && eegConfig !== null ? eegConfig : {}),
        leadFieldProvider,
    }
    setup.modules = modules
}
