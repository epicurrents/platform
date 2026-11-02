/**
 * The view area's scroll position: held in place when chrome above it comes and goes, and carried across a reload the
 * platform makes itself.
 *
 * The maintenance banner sits above the view area in the flow, so raising it shortens the view area from the top and
 * every page inside it would drop by the banner's height, a reader at the end no longer at the end. A registered view
 * area is therefore scrolled by however far its top edge moves, which leaves its content where it was on screen,
 * unless it is at the top of the page. A window resized from the bottom does not move the top edge and keeps the
 * browser's own behaviour.
 *
 * Pages scroll inside `.route-view-wrapper`, not the document, and a browser restores only the document's position
 * on reload. The maintenance store reloads the page when an update moves past its code change, which without this
 * would drop a reader watching the end of the job log back at the top of the page. The position is kept per path in
 * session storage, read once and removed, and applied once the reloaded page is tall enough to take it; storage that
 * cannot be reached leaves the page at the top, as before. A reader at the end of the page is kept at its end rather
 * than at the same offset, since the reloaded page may draw more above it, as the job page does with its notice.
 *
 * @package    epicurrents-platform
 */

const STORAGE_KEY = 'epicurrents.viewScroll'
/** Within this many pixels of the end, the reader is at the end of the page. */
const END_SLACK = 24
/** How long a restore waits for the reloaded page to grow tall enough, or keeps it at its end, in milliseconds. */
export const RESTORE_WAIT_MS = 5_000

let scroller: HTMLElement | null = null
let observer: ResizeObserver | null = null
/** The scroll position this module last gave the view area; any other value is the reader's own. */
let placedTop: number | null = null

function place (element: HTMLElement, top: number) {
    element.scrollTop = top
    placedTop = element.scrollTop
}

function currentPath() {
    return window.location.pathname + window.location.search
}

/**
 * Name the element whose position is held and carried; App registers the view area on mount and `null` on unmount.
 *
 * @param element - the scrolling view area, or `null` to forget it.
 */
export function registerViewScroller (element: HTMLElement | null) {
    observer?.disconnect()
    observer = null
    scroller = element
    placedTop = null
    if (!element || typeof ResizeObserver === 'undefined') {
        return
    }
    // Chrome above the view area changes its height, which is what the observer sees; the top edge says by how much.
    let edge = element.getBoundingClientRect().top
    observer = new ResizeObserver(() => {
        const top = element.getBoundingClientRect().top
        const moved = top - edge
        edge = top
        // A reader at the top of the page keeps seeing its top, as the browser's own scroll anchoring does.
        if (moved !== 0 && element.scrollTop > 0) {
            place(element, element.scrollTop + moved)
        }
    })
    observer.observe(element)
}

/** Record the view area's position for the current path, immediately before a reload. */
export function rememberViewScroll () {
    if (!scroller) {
        return
    }
    try {
        const atEnd = scroller.scrollHeight - scroller.clientHeight - scroller.scrollTop <= END_SLACK
        sessionStorage.setItem(STORAGE_KEY, JSON.stringify({ atEnd, path: currentPath(), top: scroller.scrollTop }))
    } catch {
        // Without storage the reload lands at the top, which is what it did before.
    }
}

/**
 * Apply a position recorded for the current path, once the page has drawn enough to take it, or hold the page at its
 * end for the length of the wait when the reader was there. Gives up when the reader scrolls first, and settles for
 * the furthest position reachable when the wait runs out.
 */
export function restoreViewScroll () {
    let saved: { atEnd?: unknown, path?: unknown, top?: unknown } | null = null
    try {
        const raw = sessionStorage.getItem(STORAGE_KEY)
        sessionStorage.removeItem(STORAGE_KEY)
        saved = raw ? JSON.parse(raw) : null
    } catch {
        return
    }
    const element = scroller
    if (!element || !saved || saved.path !== currentPath() || typeof saved.top !== 'number' || saved.top <= 0) {
        return
    }
    const target = saved.top
    const atEnd = saved.atEnd === true
    const deadline = performance.now() + RESTORE_WAIT_MS
    placedTop = element.scrollTop
    const step = () => {
        if (scroller !== element || element.scrollTop !== placedTop) {
            return
        }
        const reachable = element.scrollHeight - element.clientHeight
        if (performance.now() >= deadline || (!atEnd && reachable >= target)) {
            place(element, atEnd ? reachable : Math.min(target, reachable))
            return
        }
        place(element, reachable)
        requestAnimationFrame(step)
    }
    requestAnimationFrame(step)
}
