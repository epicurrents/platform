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

    it('holds a read that falls due while the tab is hidden until it is shown', async () => {
        const read = vi.fn<() => Promise<boolean>>().mockResolvedValue(false)
        const setHidden = (hidden: boolean) => {
            Object.defineProperty(document, 'visibilityState', {
                configurable: true,
                get: () => hidden ? 'hidden' : 'visible',
            })
            document.dispatchEvent(new Event('visibilitychange'))
        }
        const { app } = mountPolling(read, { initial: 1000 })
        setHidden(true)
        await vi.advanceTimersByTimeAsync(5000)
        expect(read).not.toHaveBeenCalled()
        setHidden(false)
        await vi.advanceTimersByTimeAsync(0)
        expect(read).toHaveBeenCalledTimes(1)
        app.unmount()
    })

    it('waits out the Retry-After of a failure, bounded', async () => {
        const read = vi.fn<() => Promise<boolean>>()
        read.mockRejectedValueOnce(Object.assign(new Error('503'), { response: { headers: { 'retry-after': '20' } } }))
        read.mockResolvedValue(false)
        const { app } = mountPolling(read, { initial: 1000, max: 4000, retryAfterMax: 15000 })
        await vi.advanceTimersByTimeAsync(1000)
        expect(read).toHaveBeenCalledTimes(1)
        await vi.advanceTimersByTimeAsync(14000)
        expect(read).toHaveBeenCalledTimes(1)
        await vi.advanceTimersByTimeAsync(1000)
        expect(read).toHaveBeenCalledTimes(2)
        app.unmount()
    })

    it('never overlaps a refresh with a scheduled read', async () => {
        let running = 0
        let overlapped = false
        const read = vi.fn<() => Promise<boolean>>(async () => {
            running += 1
            if (running > 1) {
                overlapped = true
            }
            await new Promise(resolve => setTimeout(resolve, 500))
            running -= 1
            return false
        })
        const { app, handle } = mountPolling(read, { initial: 1000 })
        await vi.advanceTimersByTimeAsync(1100)
        void handle.refresh()
        await vi.advanceTimersByTimeAsync(1500)
        expect(read).toHaveBeenCalledTimes(2)
        expect(overlapped).toBe(false)
        app.unmount()
    })
})
