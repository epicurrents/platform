/**
 * Tests for holding the interface light on viewer routes: the store's override, and the route hook that sets it.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'
import type { NavigationFailure, RouteLocationNormalized } from 'vue-router'
import { useThemeStore } from '#stores/theme'
import { syncRouteTheme } from './theme'
import routerSource from './index.ts?raw'

/** Stub the OS colour-scheme query, returning a function that flips it and notifies the store. */
function stubSystemScheme (dark: boolean) {
    const listeners: (() => void)[] = []
    const query = {
        get matches () {
            return dark
        },
        addEventListener: (_type: string, listener: () => void) => listeners.push(listener),
    }
    vi.stubGlobal('matchMedia', vi.fn(() => query))
    return (next: boolean) => {
        dark = next
        listeners.forEach(listener => listener())
    }
}

function isDark () {
    const classes = document.documentElement.classList
    expect(classes.contains('wa-dark')).not.toBe(classes.contains('wa-light'))
    return classes.contains('wa-dark')
}

/** A resolved route whose matched records carry the given metas, outermost first. */
function route (...metas: Record<string, unknown>[]) {
    return { matched: metas.map(meta => ({ meta })) } as unknown as RouteLocationNormalized
}

describe('light-only viewer routes', () => {
    beforeEach(() => {
        localStorage.clear()
        setActivePinia(createPinia())
    })

    afterEach(() => {
        vi.unstubAllGlobals()
    })

    it('holds a dark choice light and restores it afterwards', async () => {
        stubSystemScheme(false)
        localStorage.setItem('epicurrents-theme', 'dark')
        const store = useThemeStore()
        expect(isDark()).toBe(true)

        syncRouteTheme(route({ lightOnly: true }))
        await nextTick()
        expect(isDark()).toBe(false)

        syncRouteTheme(route({}))
        await nextTick()
        expect(isDark()).toBe(true)
        expect(store.mode).toBe('dark')
    })

    it('keeps light when the system turns dark under the auto theme', async () => {
        const setSystemDark = stubSystemScheme(false)
        useThemeStore()
        syncRouteTheme(route({ lightOnly: true }))
        await nextTick()

        setSystemDark(true)
        expect(isDark()).toBe(false)
    })

    it('stores a choice made on a viewer route and applies it on leaving', async () => {
        stubSystemScheme(false)
        const store = useThemeStore()
        syncRouteTheme(route({ lightOnly: true }))
        store.setMode('dark')
        await nextTick()
        expect(isDark()).toBe(false)
        expect(localStorage.getItem('epicurrents-theme')).toBe('dark')

        syncRouteTheme(route({}))
        await nextTick()
        expect(isDark()).toBe(true)
    })

    it('applies a parent route flag to its children', () => {
        stubSystemScheme(true)
        const store = useThemeStore()
        syncRouteTheme(route({ lightOnly: true }, { title: 'Child' }))
        expect(store.lightOnly).toBe(true)
    })

    it('leaves the theme alone after a failed navigation', () => {
        stubSystemScheme(true)
        const store = useThemeStore()
        syncRouteTheme(route({ lightOnly: true }))
        syncRouteTheme(route({}), undefined, {} as NavigationFailure)
        expect(store.lightOnly).toBe(true)
    })
})

describe('the core viewer route', () => {
    it('is light-only, which is what covers every project overlay panel', () => {
        // Read from source: importing the router would build the whole route table and every view behind it.
        const viewerRoute = routerSource.match(/path: '\/viewer',[\s\S]*?meta: \{([\s\S]*?)\}/)
        expect(viewerRoute?.[1]).toContain('lightOnly: true')
        expect(routerSource).toContain('router.afterEach(syncRouteTheme)')
    })
})
