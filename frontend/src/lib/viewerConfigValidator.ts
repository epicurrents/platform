/**
 * Dry-validating viewer-config overrides against a live viewer settings tree.
 *
 * Whether an override is valid is exactly whether the viewer's
 * `setFieldValue(field, value)` accepts it — it returns true only when the
 * dotted-path field exists and the value's constructor matches the current
 * value's. That check can only run against a fully-populated settings tree
 * (interface settings shadowing core settings), which exists only after a
 * viewer launches and loads its modules. The editor page has no viewer of its
 * own, so this module launches a hidden, data-less instance (shadow-DOM
 * isolated, no shared memory, no study) and dry-runs each field against it. The
 * instance is reused across validations and is the same code path that applies
 * overrides at real viewer launch, so the verdict cannot drift from runtime.
 *
 * @package    epicurrents-platform
 */

import type { EpicurrentsApp, SettingsValue } from '#epicurrents/core/dist/types'
import type { ViewerSettingsOverrides } from '#lib/viewerConfig'

/** Per-field verdict from a dry validation run. */
export interface FieldValidation {
    /** The dotted-path settings field that was checked. */
    field: string
    /** True when the viewer would accept this field/value pair. */
    ok: boolean
    /** Why the field was rejected (omitted when `ok`). */
    reason?: 'unknown-field' | 'type-mismatch'
    /** For a type mismatch, the constructor name the viewer expects (e.g. 'Number'). */
    expectedType?: string
}

/** The interface and core settings stores both expose this read/write contract. */
interface SettingsAccess {
    getFieldValue (field: string, depth?: number): SettingsValue
    setFieldValue (field: string, value: SettingsValue): boolean
}

const CONTAINER_ID = 'config-validator'
const GLOBAL_WAIT_MS = 20000
const READY_WAIT_MS = 30000

/** Lazily-launched hidden viewer; null once a launch attempt has failed. */
let _instance: Promise<{ intf: SettingsAccess, core: SettingsAccess } | null> | null = null

function sleep (ms: number): Promise<void> {
    return new Promise(resolve => window.setTimeout(resolve, ms))
}

function withTimeout<T> (promise: Promise<T>, ms: number): Promise<T> {
    return new Promise((resolve, reject) => {
        const timer = window.setTimeout(() => reject(new Error('timeout')), ms)
        promise.then(
            value => {
                window.clearTimeout(timer)
                resolve(value)
            },
            error => {
                window.clearTimeout(timer)
                reject(error)
            },
        )
    })
}

/** Poll until the viewer UMD global is loaded; false if it never appears. */
async function waitForGlobal (): Promise<boolean> {
    const start = performance.now()
    while (typeof Epicurrents === 'undefined' || typeof Epicurrents?.createEpicurrentsApp !== 'function') {
        if (performance.now() - start > GLOBAL_WAIT_MS) {
            return false
        }
        await sleep(100)
    }
    return true
}

/**
 * Launch the hidden validation viewer once and return its settings stores, or
 * null if the viewer global never loaded or the launch threw. A non-fatal
 * failure lets the caller fall back to saving without validation.
 */
async function launchValidator (): Promise<{ intf: SettingsAccess, core: SettingsAccess } | null> {
    if (!(await waitForGlobal())) {
        return null
    }
    const hostId = `epicurrents-${CONTAINER_ID}`
    if (!document.getElementById(hostId)) {
        const host = document.createElement('div')
        host.id = hostId
        host.setAttribute('aria-hidden', 'true')
        host.style.cssText = 'position:fixed;left:-9999px;top:0;width:1px;height:1px;overflow:hidden;pointer-events:none;'
        document.body.appendChild(host)
    }
    const setup = Object.assign(new Object(null), {
        appId: CONTAINER_ID,
        assetPath: '/viewer',
        containerId: CONTAINER_ID,
        // Shadow-DOM isolate so launching does not strip the editor page's styles.
        embedded: true,
        isProduction: true,
        // Suppress the viewer's getFieldValue warnings on the reason-classification path.
        logThreshold: 'ERROR',
        // No signal data is loaded, so the shared-memory route (and its cross-origin
        // isolation requirement) is never needed.
        useSAB: false,
        user: null,
    })
    // Suppress the host announce bridge during launch so the hidden viewer's
    // startup warnings don't surface as toasts on the editor page; restore it
    // once the instance is up.
    const epicGlobal = window.__EPICURRENTS__
    const priorAnnounce = epicGlobal?.announce
    if (epicGlobal) {
        epicGlobal.announce = undefined
    }
    try {
        let epic: EpicurrentsApp
        try {
            epic = await Epicurrents.createEpicurrentsApp(setup)
        } catch (err) {
            console.warn('[viewer-config] validation viewer failed to launch:', err)
            return null
        }
        const intf = epic.runtime.INTERFACE as unknown as SettingsAccess & { app?: { disclaimerAccepted: number } }
        if (intf.app) {
            // Mark the disclaimer accepted so the hidden instance never opens a dialog.
            intf.app.disclaimerAccepted = 1
        }
        // Wait for the modules' settings to register before validating against them.
        const launched = epic.interface as { awaitReady?: () => Promise<boolean> } | null
        try {
            await withTimeout(launched?.awaitReady?.() ?? Promise.resolve(true), READY_WAIT_MS)
        } catch {
            // Validate against whatever loaded; a partial tree still catches typos.
        }
        const core = epic.runtime.SETTINGS as unknown as SettingsAccess
        // If even the core app settings are absent the tree never populated. Report
        // unavailable rather than rejecting every field as unknown.
        if (tryGet(core, 'app') === undefined) {
            console.warn('[viewer-config] validation viewer settings did not populate; skipping validation.')
            return null
        }
        return { intf, core }
    } finally {
        if (epicGlobal) {
            epicGlobal.announce = priorAnnounce
        }
    }
}

function getValidator (): Promise<{ intf: SettingsAccess, core: SettingsAccess } | null> {
    if (!_instance) {
        _instance = launchValidator()
    }
    return _instance
}

function trySet (store: SettingsAccess, field: string, value: SettingsValue): boolean {
    try {
        return store.setFieldValue(field, value)
    } catch {
        return false
    }
}

function tryGet (store: SettingsAccess, field: string): SettingsValue | undefined {
    try {
        return store.getFieldValue(field)
    } catch {
        return undefined
    }
}

/**
 * Dry-validate an overrides map against the hidden viewer. Returns a per-field
 * verdict list, or null when the viewer could not be launched (the caller then
 * decides whether to save without validation). Each field is routed through the
 * same interface-then-core `setFieldValue` fallback the applier uses; rejected
 * fields are classified as an unknown field or a type mismatch via a follow-up
 * `getFieldValue` probe.
 *
 * @param overrides - The dotted-path field → value map to check.
 */
export async function validateViewerOverrides (overrides: ViewerSettingsOverrides): Promise<FieldValidation[] | null> {
    const access = await getValidator()
    if (!access) {
        return null
    }
    const { intf, core } = access
    const results: FieldValidation[] = []
    for (const [field, value] of Object.entries(overrides)) {
        if (trySet(intf, field, value) || trySet(core, field, value)) {
            results.push({ field, ok: true })
            continue
        }
        const current = tryGet(intf, field) ?? tryGet(core, field)
        if (current === undefined || current === null) {
            results.push({ field, ok: false, reason: 'unknown-field' })
        } else {
            results.push({ field, ok: false, reason: 'type-mismatch', expectedType: current.constructor?.name })
        }
    }
    return results
}
