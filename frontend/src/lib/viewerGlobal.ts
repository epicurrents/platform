/**
 * Typed accessors for the parts of `window.__EPICURRENTS__` that the core package types more
 * narrowly than the platform uses them.
 *
 * `@epicurrents/core` owns the ambient declaration of `Window.__EPICURRENTS__`, and a competing
 * declaration on the platform side does not merge — it is silently ignored (see
 * `src/types/core-global-augment.ts`). Additive fields therefore go through that augmentation,
 * while the three members whose core types are the wrong *shape* for platform code are reached
 * through the helpers here:
 *
 * - `SETUP` is `Readonly<ApplicationConfig>` in core, but the platform assembles it from project
 *   and plugin `extraSetup` fragments that carry keys core does not declare.
 * - `RUNTIME.INTERFACE` is `unknown` in core, which has no dependency on the interface layer. The
 *   platform knows the shape from this side.
 * - `EVENT_BUS` is a `ScopedEventBus`, whose interface omits `dispatchEvent` even though the
 *   concrete bus extends `EventTarget` and implements it.
 */

/**
 * The viewer's interface layer (`DefaultInterface`), as much of it as platform code reaches.
 */
export type ViewerInterface = {
    /** Top-level application state exposed by the interface. */
    app: Record<string, unknown>
    /** Per-module settings map — same Map-like API as the runtime module map. */
    modules: { get (name: string): { settings: unknown } | undefined }
    /** Write a dotted-path setting value (e.g. `'eeg.sensitivity'`). */
    setFieldValue (field: string, value: unknown): boolean | void
}

/** Get the viewer's interface layer, or null before a viewer app has been created. */
export const getViewerInterface = (): ViewerInterface | null => {
    return (window.__EPICURRENTS__?.RUNTIME?.INTERFACE as ViewerInterface | undefined) ?? null
}

/**
 * Replace the viewer SETUP object. Takes the platform's open-ended shape: `extraSetup` fragments
 * contributed by projects and plugins carry keys core's `ApplicationConfig` does not declare.
 */
export const setViewerSetup = (setup: Record<string, unknown>) => {
    window.__EPICURRENTS__.SETUP = setup as typeof window.__EPICURRENTS__.SETUP
}

/** Read the viewer SETUP object as an open-ended record. */
export const getViewerSetup = (): Record<string, unknown> => {
    return window.__EPICURRENTS__?.SETUP as unknown as Record<string, unknown> ?? {}
}

