/**
 * Typed helpers for the Epicurrents event bus (`window.__EPICURRENTS__.EVENT_BUS`).
 *
 * The event bus is populated by `Epicurrents.createEpicurrentsApp()`, but may
 * become available asynchronously after the app creation promise resolves.
 * `ViewerView` awaits {@link waitForEventBus} before calling
 * `ViewerPlugin.onAppReady`, so inside that hook the bus is always live and
 * the helpers below are safe to call.
 *
 * Outside of `onAppReady` (e.g. in response to a user action after mount),
 * the bus will already be set and the helpers are equally safe — the guard in
 * {@link getBus} throws only if called before the bus has been initialised.
 *
 * Typical usage inside a project plugin:
 *
 * ```ts
 * import { onEvent, offEvent, emitEvent } from '../eventBus'
 *
 * export const plugin: ViewerPlugin = {
 *     onAppReady: (_epic, bus) => {
 *         // 'bus' is the same EventTarget as window.__EPICURRENTS__.EVENT_BUS,
 *         // passed in directly so you don't need to import getEventBus().
 *         onEvent('recording:opened', handleRecordingOpened)
 *     },
 * }
 *
 * function handleRecordingOpened(e: Event) {
 *     const detail = (e as CustomEvent).detail
 *     // ...
 * }
 * ```
 */

/** Maximum milliseconds to wait for the event bus before giving up. */
const WAIT_TIMEOUT_MS = 10_000
/** Polling interval while waiting for the event bus. */
const WAIT_INTERVAL_MS = 50

/**
 * Resolve once `window.__EPICURRENTS__.EVENT_BUS` is a non-null `EventTarget`.
 * Rejects if the bus has not appeared within {@link WAIT_TIMEOUT_MS} ms.
 *
 * Called by `ViewerView` after `createEpicurrentsApp()` so that
 * `ViewerPlugin.onAppReady` is only invoked once the bus is confirmed live.
 */
export function waitForEventBus(): Promise<EventTarget> {
    return new Promise((resolve, reject) => {
        const deadline = Date.now() + WAIT_TIMEOUT_MS
        const poll = () => {
            const bus = getEventTarget()
            if (bus) {
                resolve(bus)
                return
            }
            if (Date.now() >= deadline) {
                reject(new Error(
                    `[project] Epicurrents event bus did not become available within ${WAIT_TIMEOUT_MS} ms.`
                ))
                return
            }
            setTimeout(poll, WAIT_INTERVAL_MS)
        }
        poll()
    })
}

/**
 * The live bus as a plain `EventTarget`, or null before the viewer has created one.
 *
 * The `ScopedEventBus` interface the viewer exports omits `dispatchEvent`, but the concrete bus
 * extends `EventTarget` and implements it — which `emitEvent` below relies on.
 */
function getEventTarget(): EventTarget | null {
    return (window.__EPICURRENTS__?.EVENT_BUS as unknown as EventTarget | null | undefined) ?? null
}

function getBus(): EventTarget {
    const bus = getEventTarget()
    if (!bus) {
        throw new Error(
            '[project] Epicurrents event bus is not yet available. ' +
            'Only call event bus helpers from onAppReady or later.'
        )
    }
    return bus
}

/**
 * Subscribe to an event on the Epicurrents event bus.
 *
 * @param type - Event type string (e.g. `'recording:opened'`).
 * @param handler - Listener to invoke when the event fires.
 * @param options - Optional `AddEventListenerOptions` (e.g. `{ once: true }`).
 */
export function onEvent(
    type: string,
    handler: EventListener,
    options?: AddEventListenerOptions,
): void {
    getBus().addEventListener(type, handler, options)
}

/**
 * Unsubscribe a previously registered listener from the Epicurrents event bus.
 *
 * @param type - Event type string.
 * @param handler - The exact listener reference passed to {@link onEvent}.
 */
export function offEvent(type: string, handler: EventListener): void {
    getBus().removeEventListener(type, handler)
}

/**
 * Dispatch a `CustomEvent` on the Epicurrents event bus.
 *
 * @param type - Event type string.
 * @param detail - Optional payload attached as `event.detail`.
 */
export function emitEvent(type: string, detail?: unknown): void {
    getBus().dispatchEvent(new CustomEvent(type, { detail }))
}

/**
 * Dispatch a scoped event on the Epicurrents event bus.
 *
 * Unlike {@link emitEvent} (a plain `CustomEvent`, which only native
 * `addEventListener` subscribers receive), this reaches listeners registered
 * with `addScopedEventListener` as well — required when the viewer subscribes to
 * an event under a specific scope.
 *
 * @param type - Event type string.
 * @param scope - Scope the event originates from (e.g. the reserved `'interface'` scope).
 * @param phase - Event phase (`'before'` or `'after'`, default `'after'`).
 * @param detail - Optional payload merged into `event.detail`.
 */
export function emitScopedEvent(
    type: string,
    scope: string,
    phase: 'before' | 'after' = 'after',
    detail: Record<string, unknown> = {},
): void {
    const bus = getBus() as unknown as {
        dispatchScopedEvent?: (
            event: string,
            scope?: string,
            phase?: 'before' | 'after',
            detail?: Record<string, unknown>,
        ) => boolean
    }
    bus.dispatchScopedEvent?.(type, scope, phase, detail)
}

/**
 * Return the raw `EventTarget` for cases where direct access is needed
 * (e.g. passing the bus to a third-party library).
 */
export function getEventBus(): EventTarget {
    return getBus()
}
