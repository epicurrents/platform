/**
 * Platform-owned lean viewer setup (PROTOTYPE).
 *
 * Mirrors the viewer's internal `setups/base.ts`, but lives in the platform and
 * imports the viewer exclusively through its public `@epicurrents/*` package
 * specifiers — no `#`-prefixed interface-internal paths. This is the proof that
 * a consumer can register its own module set against the framework
 * (`createEpicurrentsApp` from `@epicurrents/interface`) without modifying the
 * viewer repository. Built by `vite.config.base.ts` into `viewer-dist/<project>/`
 * (or `viewer-dist/base/` when no project is active).
 *
 * Registers only the stable modalities — the EEG module with the EDF and DICOM
 * readers. acc / htm / pdf / pyodide are intentionally left out so the bundle
 * stays small. Project-specific readers are NOT registered here: the active
 * project contributes them through its own overlay, resolved at build time by
 * `vite.config.base.ts` from `VITE_PROJECT`.
 * A project's overlay lives in its own folder (`src/projects/<project>/viewer.ts`);
 * `overlays/none.ts` is the no-op stub used when no project (or a project without
 * an overlay) is active.
 */

import {
    createEpicurrentsApp as createFrameworkApp,
    type ApplicationInterfaceConfig,
    type SetupContext,
} from '@epicurrents/interface'
import * as interfaceEegModule from '@epicurrents/interface/modules/eeg'
import type { EegModuleConfiguration } from '@epicurrents/interface/modules/eeg'
import { inlineWorker } from '@epicurrents/core/util'
import { leadFieldProvider } from './leadFields'
// Core (modality) module.
import * as eegModule from '@epicurrents/eeg-module'
// Readers / importers.
import { EdfImporter, EdfWorkerSubstitute } from '@epicurrents/edf-reader'
import { DicomImporter, DicomWorkerSubstitute } from '@epicurrents/dicom-reader'
// The active project's module overlay, resolved at build time by
// `vite.config.base.ts` (the no-op `overlays/none.ts` when no project is active).
import { registerProjectOverlay } from '@viewer-overlay'

// A reader package exports its self-contained (umd) worker bundle under
// `./workers/*`; loaded as a raw source string and wrapped by inlineWorker as a
// classic Blob worker. Core's own workers need no entry here — it resolves and
// inlines them in its own build, and an unregistered name falls through to that.
import dcmWorkerSrc from '@epicurrents/dicom-reader/workers/dicom.worker.js?raw'
const dcmWorker = () => inlineWorker('DicomWorker', dcmWorkerSrc).create()
import edfWorkerSrc from '@epicurrents/edf-reader/workers/edf.worker.js?raw'
const edfWorker = () => inlineWorker('EdfWorker', edfWorkerSrc).create()

/**
 * Register the EEG module with the EDF and DICOM readers. Worker overrides
 * resolve to real workers only when shared memory is available and the module's
 * own memory-manager setting is on, otherwise to the synchronous substitutes.
 */
const registerBaseModules = ({ app, useSAB, setup, registerInterfaceModule }: SetupContext) => {
    app.registerModule('eeg', eegModule)
    // The eeg module ships useMemoryManager=false; opt it into the shared-memory
    // path (must be set after registerModule so 'eeg' resolves as a module field).
    app.configure({ 'eeg.useMemoryManager': useSAB })
    const edfLoader = new EdfImporter()
    edfLoader.setWorkerOverride('eeg', () => {
        const eegSAB = window.__EPICURRENTS__.RUNTIME!.SETTINGS.getFieldValue('eeg.useMemoryManager')
        return useSAB && eegSAB ? edfWorker() : new EdfWorkerSubstitute()
    })
    const eegEdfLoader = new eegModule.EegStudyLoader('EegEdfLoader', ['eeg'], edfLoader)
    app.registerStudyImporter('eeg/edf-file', 'Open EDF file', 'file', eegEdfLoader)
    app.registerStudyImporter('eeg/edf-folder', 'Open EDF files from folder', 'folder', eegEdfLoader)
    app.registerStudyImporter('eeg/edf-url', 'Open EDF from URL', 'url', eegEdfLoader)
    const dcmLoader = new DicomImporter()
    dcmLoader.setWorkerOverride('eeg', () => {
        const eegSAB = window.__EPICURRENTS__.RUNTIME!.SETTINGS.getFieldValue('eeg.useMemoryManager')
        return useSAB && eegSAB ? dcmWorker() : new DicomWorkerSubstitute()
    })
    const eegDcmLoader = new eegModule.EegStudyLoader('EegDicomLoader', ['eeg'], dcmLoader)
    app.registerStudyImporter('eeg/dcm-file', 'Open DICOM file', 'file', eegDcmLoader)
    app.registerStudyImporter('eeg/dcm-folder', 'Open DICOM files from folder', 'folder', eegDcmLoader)
    app.registerStudyImporter('eeg/dcm-url', 'Open DICOM from URL', 'url', eegDcmLoader)
    // Give the source-localisation tool a way to obtain lead fields. The viewer deliberately owns
    // no URLs for them; `leadFields.ts` holds the platform's static-bundle-then-compute-API
    // strategy. `SETUP.modules.eeg` is what the interface hands to EegModule.applyConfiguration()
    // at launch, so writing it here — after the host's own config has been merged into SETUP but
    // before launch — reaches the module without any project having to know about it. A project
    // that sets `modules.eeg` to a URL string cannot also carry a provider (JSON holds no
    // functions), so the object form wins.
    const existingEegConfig = setup.modules.eeg
    const eegConfig: EegModuleConfiguration = {
        ...(typeof existingEegConfig === 'object' && existingEegConfig !== null ? existingEegConfig : {}),
        leadFieldProvider,
    }
    setup.modules.eeg = eegConfig
    registerInterfaceModule('eeg', interfaceEegModule)
}

/**
 * Register the stable base modalities, then let the active project's overlay add
 * its own modules on top. The overlay runs after the base set so it can reuse the
 * modules the base registered (e.g. the EEG module for a project-specific reader).
 */
const registerModules = async (ctx: SetupContext) => {
    registerBaseModules(ctx)
    await registerProjectOverlay(ctx)
}

/** Create the core Epicurrents application with the base modalities plus the active project's overlay. */
export const createEpicurrentsApp = async (config?: ApplicationInterfaceConfig) => {
    const app = await createFrameworkApp(config, registerModules)
    // Host → viewer bridge (mirrors the announce() callback in the other direction): let the
    // platform tell the viewer the session was (re-)established after a login, so network loads
    // latched on a prior auth failure resume without reopening the recording.
    if (typeof window !== 'undefined' && window.__EPICURRENTS__) {
        window.__EPICURRENTS__.notifySessionRestored = () => app.notifySessionRestored()
    }
    return app
}
