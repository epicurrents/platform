/**
 * Tests for the backing-off poll: it slows down while nothing changes, resets on a change, and stops cleanly.
 */

import { vi, describe, it, expect, beforeEach, afterEach } from 'vitest'
import { createApp, defineComponent, h } from 'vue'
import { usePolling, type PollingOptions } from '#composables/usePolling'

/** Mount the composable inside a throwaway component, so its lifecycle hooks run. */
function mountPolling (read: () => Promise<boolean>, options: PollingOptions = {}) {
    let handle!: ReturnType<typeof usePolling>
    const Host = defineComponent({
        setup () {
            handle = usePolling(read, options)
            return () => h('div')
        },
    })
    const app = createApp(Host)
    app.mount(document.createElement('div'))
    return { app, handle }
}

describe('usePolling', () => {
    beforeEach(() => {
        vi.useFakeTimers()
    })

    afterEach(() => {
        vi.useRealTimers()
    })

    it('backs off while reads report no change and resets when one does', async () => {
        const read = vi.fn<() => Promise<boolean>>()
        read.mockResolvedValue(false)
        mountPolling(read, { initial: 1000, max: 4000, factor: 2 })
        await vi.advanceTimersByTimeAsync(1000)
        expect(read).toHaveBeenCalledTimes(1)
        await vi.advanceTimersByTimeAsync(2000)
        expect(read).toHaveBeenCalledTimes(2)
        await vi.advanceTimersByTimeAsync(4000)
        expect(read).toHaveBeenCalledTimes(3)
        // Capped: 8000 would follow, but max is 4000.
        await vi.advanceTimersByTimeAsync(4000)
        expect(read).toHaveBeenCalledTimes(4)
        read.mockResolvedValueOnce(true)
        await vi.advanceTimersByTimeAsync(4000)
        expect(read).toHaveBeenCalledTimes(5)
        // A change resets the interval to the initial delay.
        await vi.advanceTimersByTimeAsync(1000)
        expect(read).toHaveBeenCalledTimes(6)
    })

    it('treats a failed read like an unchanged one', async () => {
        const read = vi.fn<() => Promise<boolean>>()
        read.mockRejectedValue(new Error('offline'))
        mountPolling(read, { initial: 1000, max: 10000, factor: 2 })
        await vi.advanceTimersByTimeAsync(1000)
        await vi.advanceTimersByTimeAsync(2000)
        expect(read).toHaveBeenCalledTimes(2)
    })

    it('stops on unmount and on stop(), and a late read does not reschedule', async () => {
        let release!: (value: boolean) => void
        const read = vi.fn<() => Promise<boolean>>(() => new Promise(resolve => {
            release = resolve
        }))
        const { app, handle } = mountPolling(read, { initial: 1000 })
        await vi.advanceTimersByTimeAsync(1000)
        expect(read).toHaveBeenCalledTimes(1)
        handle.stop()
        release(true)
        await vi.advanceTimersByTimeAsync(5000)
        expect(read).toHaveBeenCalledTimes(1)
        handle.start()
        await vi.advanceTimersByTimeAsync(1000)
        expect(read).toHaveBeenCalledTimes(2)
        app.unmount()
        release(true)
        await vi.advanceTimersByTimeAsync(5000)
        expect(read).toHaveBeenCalledTimes(2)
    })

    it('does not start on mount when told not to', async () => {
        const read = vi.fn<() => Promise<boolean>>().mockResolvedValue(false)
        const { handle } = mountPolling(read, { initial: 1000, immediate: false })
        await vi.advanceTimersByTimeAsync(3000)
        expect(read).not.toHaveBeenCalled()
        expect(handle.active.value).toBe(false)
    })
})
