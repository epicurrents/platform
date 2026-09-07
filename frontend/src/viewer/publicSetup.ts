/**
 * Platform-owned viewer setup for the public viewer page.
 *
 * The public viewer at `/viewer/<mode>` is served by `epicurrents.views.public_viewer_view`, which
 * renders its SETUP with `json.dumps` from `settings.PUBLIC_VIEWER_MODES`. That makes it the one
 * viewer surface whose configuration is JSON and nothing else — the SPA imports its setup
 * (`views/ViewerView.vue`) and the per-project viewer build bakes one in (`viewer/base.ts`). A
 * setting that has to hold a function therefore has nowhere to live: the EEG module asks for its
 * lead fields through a `LeadFieldProvider`, and without one the source-localisation tool reports
 * every montage as unavailable on a page that otherwise works.
 *
 * So the page loads this module between its SETUP declaration and the viewer library, and it
 * carries the whole platform-owned setup rather than only the provider. The split with Python is
 * by owner, not by type: `PUBLIC_VIEWER_MODES` keeps what the *deployment* decides — which library
 * a mode loads, the vendored Pyodide root the deploy step writes and `vendor_pyodide` reads back,
 * the container the template renders — and everything that is a platform decision about how the
 * viewer behaves lives here, where a provider function is as expressible as a trend epoch length.
 *
 * Precedence: a value the page declares wins over a default here, so a mode (or a project
 * overriding one through `project_loader`) can still override any of this by naming it in
 * `PUBLIC_VIEWER_MODES`. `modules.eeg` is merged a key deeper for the same reason in reverse — a
 * mode configuring trends must not drop the provider it has no way to restate.
 *
 * Built by `vite.config.publicsetup.ts` into `viewer-dist/epicurrents-public-setup.js`. A
 * deployment whose bundles predate it serves a 404 for the tag and loads the page on the viewer's
 * own defaults, with no source localisation.
 */

import type { EegModuleConfiguration } from '@epicurrents/interface/modules/eeg'
import { leadFieldProvider } from './leadFields'

/**
 * What this module contributes to `SETUP.modules.eeg`.
 *
 * `leadFieldProvider` is the reason the file exists. `trends` is the other half of the same
 * problem: trend configuration is plain data, so it needs no function to express — but it needs a
 * platform-owned setup to live in, or the page takes whatever the viewer package happens to
 * default to while every other surface can be tuned. Naming the key here — even with the defaults
 * — is what makes it a platform decision rather than an accident of which viewer version is
 * installed.
 */
const EEG_MODULE_DEFAULTS: EegModuleConfiguration = {
    leadFieldProvider,
    trends: {
        // Which trend the strip computes when a recording opens, and whether it opens with it.
        // Only the selected trend is ever computed, so this is a cost decision as much as a
        // display one — `showStrip: false` keeps the montage worker free for the first page
        // render, and the strip computes on the first toggle instead.
        defaultType: 'aeeg',
        showStrip: false,
    },
}

/**
 * Platform defaults for the page's SETUP, applied under whatever the mode declares.
 *
 * Deliberately short, because most of what a SETUP can hold is not settable from this page at all.
 * The two worth naming, since both looked settable and were not:
 *
 * - `activeModules` is resolved by the viewer build. The builder edition takes it from its build
 *   profile and assigns it *over* the page's SETUP, and a per-project build registers only the
 *   modules it bundles, so a value here is inert in both directions.
 * - `logThreshold` is decided by the interface from `isProduction` unless the *caller* of
 *   `createEpicurrentsApp` passes one. The page calls it with no argument (the SPA does not, which
 *   is why the SPA can choose), so the page's value is overwritten either way; it lands on 'WARN'
 *   in a build. A session that needs more takes the viewer's own `?log=DEBUG` flag, which wins
 *   over both.
 *
 * `assetPath`, `containerId` and `pyodideAssetPath` stay on the Python side instead — the first
 * two because they pair with the mode's `lib_path` and the template's container element, the third
 * because the deploy step writes that tree and `manage.py vendor_pyodide` reads the version back
 * out of the setting.
 */
const SETUP_DEFAULTS: Record<string, unknown> = {
    // The page sets its own COOP/COEP, so it is cross-origin isolated whatever the rest of the
    // site does and the viewer's shared-memory signal cache is available. Asked for explicitly
    // rather than left to the viewer's default, because this page is the one surface that
    // guarantees the isolation the setting depends on. The viewer still tests
    // `crossOriginIsolated` itself before using it, so asking costs nothing where it is absent.
    useSAB: true,
}

type ViewerSetup = Record<string, unknown> & {
    modules?: unknown
}

const globalScope = window as typeof window & {
    __EPICURRENTS__?: { SETUP?: ViewerSetup }
}
const setup = globalScope.__EPICURRENTS__?.SETUP

if (!setup) {
    // Loading before the page has declared its SETUP, or on a page that declares none. Warn rather
    // than throw: the viewer still runs, only on its own defaults, and a thrown error here would
    // be indistinguishable from the library itself failing to load.
    console.warn(
        '[publicSetup] window.__EPICURRENTS__.SETUP was not found; the viewer will start on its own '
        + 'defaults and the source-localisation tool will report every montage as unavailable.'
    )
} else {
    // `in` rather than a falsy test, so a mode can deliberately declare `useSAB: false` (or any
    // other zero value) and have it survive.
    for (const [key, value] of Object.entries(SETUP_DEFAULTS)) {
        if (!(key in setup)) {
            setup[key] = value
        }
    }
    // Both levels are guarded because this runs against raw JSON from `PUBLIC_VIEWER_MODES`,
    // before the viewer has looked at it — the declared type says these are objects and the
    // deployment's settings decide whether they are. `modules.eeg` as a URL string is the shape
    // JSON can carry and a provider cannot, so the object form wins, exactly as in
    // `viewer/base.ts`. A non-object `modules` is a config error, but writing a property onto a
    // primitive is a silent no-op in a non-strict bundle, so substituting one is what keeps it
    // from presenting as "the script ran and source localisation is still unavailable".
    //
    // Worth knowing before adding to this: the interface merges a host's SETUP over its own with
    // a shallow `Object.assign` (`interface/src/setups/index.ts`), so a host that writes `modules`
    // at all replaces the interface's default `modules` wholesale — today that costs the built-in
    // 'EKG cascade' montage for the `default:10-20` setup. Every platform host does it (the SPA's
    // `ViewerView.vue` and `viewer/base.ts` both assign `modules`), so this page is not a special
    // case, and the fix belongs in the interface's merge rather than in a copy of its defaults
    // here — a copy would drift the moment the viewer changed one.
    const existingModules = setup.modules
    const modules = typeof existingModules === 'object' && existingModules !== null
        ? existingModules as Record<string, unknown>
        : {}
    const eegConfig = modules.eeg
    modules.eeg = {
        ...EEG_MODULE_DEFAULTS,
        ...(typeof eegConfig === 'object' && eegConfig !== null ? eegConfig : {}),
    }
    setup.modules = modules
}
