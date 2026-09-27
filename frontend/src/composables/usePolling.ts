/**
 * Repeat an async read on a backing-off timer while a component is mounted.
 *
 * A job page wants fresh state every few seconds while something is running
 * and less often once it is not; a maintenance outage answers with errors for
 * a while and must not turn into a tight loop. The interval starts at
 * `initial`, grows by `factor` on every failure and every unchanged read, and
 * resets on a successful read that changed something. A failure that carries
 * a `Retry-After` waits that long instead, bounded by `retryAfterMax`.
 *
 * Reads never overlap: a `refresh()` issued while a scheduled read is out
 * waits for it, so two answers cannot land out of order. A read that falls
 * due while the tab is hidden is held until the tab is shown again.
 *
 * @package    epicurrents-platform
 */

import { onBeforeUnmount, onMounted, ref } from 'vue'
import { parseRetryAfter } from '#lib/maintenanceLock'

export interface PollingOptions {
    /** First delay, in milliseconds. */
    initial?: number
    /** Longest delay the backoff reaches, in milliseconds. */
    max?: number
    /** Multiplier applied on a failure or an unchanged read. */
    factor?: number
    /** Longest delay a failure's `Retry-After` may impose, in milliseconds. */
    retryAfterMax?: number
    /** Start on mount; otherwise the caller starts it. */
    immediate?: boolean
}

/** The `Retry-After` of a failed request, in milliseconds, or `null` when it carried none. */
function retryAfterMs(error: unknown): number | null {
    const headers = (error as { response?: { headers?: Record<string, unknown> } })?.response?.headers
    const seconds = parseRetryAfter(headers?.['retry-after'])
    return seconds === null ? null : seconds * 1000
}

function isHidden() {
    return typeof document !== 'undefined' && document.visibilityState === 'hidden'
}

/**
 * Poll `read`, which returns whether anything changed, until stopped.
 *
 * @param read - the read to repeat; resolve `true` when the data changed (resets the interval), `false` otherwise.
 * @param options - timing; defaults to three seconds, growing to ten.
 */
export function usePolling(read: () => Promise<boolean>, options: PollingOptions = {}) {
    const initial = options.initial ?? 3_000
    const max = options.max ?? 10_000
    const factor = options.factor ?? 1.5
    const retryAfterMax = options.retryAfterMax ?? 60_000
    const immediate = options.immediate ?? true

    const active = ref(false)
    let delay = initial
    let timer: ReturnType<typeof setTimeout> | undefined
    /** Guards against a slow read resolving after the poll was stopped and rescheduling itself. */
    let generation = 0
    /** The read in progress, or a settled promise; the next read queues behind it. */
    let chain: Promise<unknown> = Promise.resolve()
    /** A read fell due while the tab was hidden. */
    let deferred = false

    function serialRead(): Promise<boolean> {
        const next = chain.then(() => read())
        chain = next.catch(() => undefined)
        return next
    }

    function schedule(mine: number) {
        timer = setTimeout(() => {
            if (isHidden()) {
                deferred = true
                return
            }
            void tick(mine)
        }, delay)
    }

    async function tick(mine: number) {
        let changed = false
        let wait: number | null = null
        try {
            changed = await serialRead()
        } catch (err) {
            changed = false
            wait = retryAfterMs(err)
        }
        if (mine !== generation || !active.value) {
            return
        }
        delay = changed ? initial : Math.min(max, Math.round(delay * factor))
        if (wait !== null) {
            delay = Math.min(retryAfterMax, Math.max(delay, wait))
        }
        schedule(mine)
    }

    function start() {
        if (active.value) {
            return
        }
        active.value = true
        delay = initial
        deferred = false
        schedule(++generation)
    }

    function stop() {
        active.value = false
        generation += 1
        deferred = false
        clearTimeout(timer)
        timer = undefined
    }

    /** Read now, outside the schedule, and reset the backoff. */
    async function refresh() {
        delay = initial
        try {
            await serialRead()
        } catch {
            // The next scheduled tick reports the failure through its own path.
        }
    }

    function onVisibilityChange() {
        if (!isHidden() && deferred && active.value) {
            deferred = false
            void tick(generation)
        }
    }

    onMounted(() => {
        document.addEventListener('visibilitychange', onVisibilityChange)
        if (immediate) {
            start()
        }
    })
    onBeforeUnmount(() => {
        document.removeEventListener('visibilitychange', onVisibilityChange)
        stop()
    })

    return { active, start, stop, refresh }
}
