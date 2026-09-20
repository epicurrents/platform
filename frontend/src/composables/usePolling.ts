/**
 * Repeat an async read on a backing-off timer while a component is mounted.
 *
 * A job page wants fresh state every few seconds while something is running
 * and less often once it is not; a maintenance outage answers with errors for
 * a while and must not turn into a tight loop. The interval starts at
 * `initial`, grows by `factor` on every failure and every unchanged read, and
 * resets on a successful read that changed something.
 *
 * @package    epicurrents-platform
 */

import { onBeforeUnmount, onMounted, ref } from 'vue'

export interface PollingOptions {
    /** First delay, in milliseconds. */
    initial?: number
    /** Longest delay the backoff reaches, in milliseconds. */
    max?: number
    /** Multiplier applied on a failure or an unchanged read. */
    factor?: number
    /** Start on mount; otherwise the caller starts it. */
    immediate?: boolean
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
    const immediate = options.immediate ?? true

    const active = ref(false)
    let delay = initial
    let timer: ReturnType<typeof setTimeout> | undefined
    /** Guards against a slow read resolving after the poll was stopped and rescheduling itself. */
    let generation = 0

    async function tick(mine: number) {
        let changed = false
        try {
            changed = await read()
        } catch {
            changed = false
        }
        if (mine !== generation || !active.value) {
            return
        }
        delay = changed ? initial : Math.min(max, Math.round(delay * factor))
        timer = setTimeout(() => tick(mine), delay)
    }

    function start() {
        if (active.value) {
            return
        }
        active.value = true
        delay = initial
        const mine = ++generation
        timer = setTimeout(() => tick(mine), delay)
    }

    function stop() {
        active.value = false
        generation += 1
        clearTimeout(timer)
        timer = undefined
    }

    /** Read now, outside the schedule, and reset the backoff. */
    async function refresh() {
        delay = initial
        try {
            await read()
        } catch {
            // The next scheduled tick reports the failure through its own path.
        }
    }

    onMounted(() => {
        if (immediate) {
            start()
        }
    })
    onBeforeUnmount(stop)

    return { active, start, stop, refresh }
}
