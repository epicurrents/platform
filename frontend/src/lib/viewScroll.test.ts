/**
 * The view area's scroll position: held in place when the chrome above it moves its top edge, and carried across a
 * reload the maintenance store makes — kept per path, applied once the page is tall enough, held at the end for a
 * reader who was there, and given up when the reader scrolls first.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { registerViewScroller, rememberViewScroll, RESTORE_WAIT_MS, restoreViewScroll } from './viewScroll'

/** A scroller whose content height the test sets, as jsdom does no layout. */
function makeScroller (contentHeight: number, clientHeight = 500) {
    const element = document.createElement('div')
    let top = 0
    const box = { client: clientHeight, content: contentHeight }
    Object.defineProperty(element, 'clientHeight', { configurable: true, get: () => box.client })
    Object.defineProperty(element, 'scrollHeight', { configurable: true, get: () => box.content })
    Object.defineProperty(element, 'scrollTop', {
        configurable: true,
        get: () => top,
        set: (value: number) => {
            top = Math.max(0, Math.min(value, box.content - box.client))
        },
    })
    return { box, element }
}

let frames: FrameRequestCallback[] = []
let now = 0

function runFrames (count: number) {
    for (let i = 0; i < count; i++) {
        const pending = frames
        frames = []
        now += 16
        pending.forEach(frame => frame(now))
    }
}

beforeEach(() => {
    frames = []
    now = 0
    sessionStorage.clear()
    vi.stubGlobal('requestAnimationFrame', (frame: FrameRequestCallback) => frames.push(frame))
    vi.spyOn(performance, 'now').mockImplementation(() => now)
    window.history.replaceState(null, '', '/admin/maintenance/job-1')
})

afterEach(() => {
    registerViewScroller(null)
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
})

describe('view scroll across a reload', () => {
    it('restores the offset once the reloaded page is tall enough', () => {
        const before = makeScroller(2000)
        registerViewScroller(before.element)
        before.element.scrollTop = 600
        rememberViewScroll()

        const after = makeScroller(700)
        registerViewScroller(after.element)
        restoreViewScroll()
        runFrames(2)
        expect(after.element.scrollTop).toBe(200)
        after.box.content = 2000
        runFrames(1)
        expect(after.element.scrollTop).toBe(600)
        runFrames(5)
        expect(after.element.scrollTop).toBe(600)
    })

    it('keeps a reader who was at the end at the end while the page grows', () => {
        const before = makeScroller(2000)
        registerViewScroller(before.element)
        before.element.scrollTop = 1500
        rememberViewScroll()

        const after = makeScroller(2000)
        registerViewScroller(after.element)
        restoreViewScroll()
        runFrames(1)
        expect(after.element.scrollTop).toBe(1500)
        after.box.content = 2100
        runFrames(1)
        expect(after.element.scrollTop).toBe(1600)
    })

    it('stops holding the end once the wait runs out', () => {
        const before = makeScroller(2000)
        registerViewScroller(before.element)
        before.element.scrollTop = 1500
        rememberViewScroll()

        const after = makeScroller(2000)
        registerViewScroller(after.element)
        restoreViewScroll()
        runFrames(Math.ceil(RESTORE_WAIT_MS / 16) + 2)
        after.box.content = 2500
        runFrames(1)
        expect(after.element.scrollTop).toBe(1500)
    })

    it('gives up when the reader scrolls first', () => {
        const before = makeScroller(2000)
        registerViewScroller(before.element)
        before.element.scrollTop = 1500
        rememberViewScroll()

        const after = makeScroller(2000)
        registerViewScroller(after.element)
        restoreViewScroll()
        runFrames(1)
        after.element.scrollTop = 300
        after.box.content = 2500
        runFrames(3)
        expect(after.element.scrollTop).toBe(300)
    })

    it('applies a position only to the path it was taken on, and only once', () => {
        const before = makeScroller(2000)
        registerViewScroller(before.element)
        before.element.scrollTop = 600
        rememberViewScroll()

        window.history.replaceState(null, '', '/admin/maintenance')
        const after = makeScroller(2000)
        registerViewScroller(after.element)
        restoreViewScroll()
        runFrames(3)
        expect(after.element.scrollTop).toBe(0)

        window.history.replaceState(null, '', '/admin/maintenance/job-1')
        restoreViewScroll()
        runFrames(3)
        expect(after.element.scrollTop).toBe(0)
    })

    it('records nothing without a registered view area', () => {
        rememberViewScroll()
        expect(sessionStorage.length).toBe(0)
    })
})

describe('view area held in place when chrome above it moves', () => {
    let resized: (() => void) | null
    let edge: number

    beforeEach(() => {
        resized = null
        edge = 86
        vi.stubGlobal('ResizeObserver', class {
            constructor (callback: () => void) {
                resized = callback
            }
            disconnect () {
                resized = null
            }
            observe () {}
        })
    })

    function register (contentHeight: number) {
        const view = makeScroller(contentHeight)
        view.element.getBoundingClientRect = () => ({ top: edge } as DOMRect)
        registerViewScroller(view.element)
        return view
    }

    it('scrolls by however far a banner moves the top edge, so a reader at the end stays there', () => {
        const view = register(2000)
        view.element.scrollTop = 1500
        edge = 124
        view.box.client = 462
        resized?.()
        expect(view.element.scrollTop).toBe(1538)
        edge = 86
        view.box.client = 500
        resized?.()
        expect(view.element.scrollTop).toBe(1500)
    })

    it('leaves a reader at the top of the page at the top', () => {
        const view = register(2000)
        edge = 124
        resized?.()
        expect(view.element.scrollTop).toBe(0)
    })

    it('leaves a resize that keeps the top edge to the browser', () => {
        const view = register(2000)
        view.element.scrollTop = 700
        resized?.()
        expect(view.element.scrollTop).toBe(700)
    })

    it('stops watching a view area that is no longer registered', () => {
        register(2000)
        registerViewScroller(null)
        expect(resized).toBeNull()
    })
})
