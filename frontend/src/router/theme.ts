/**
 * Route hook that holds the interface light on routes hosting the viewer.
 *
 * Kept apart from the router so it can be unit-tested without instantiating the full route table. Registered via
 * `router.afterEach(syncRouteTheme)`.
 *
 * @package    epicurrents-platform
 */

import type { NavigationFailure, RouteLocationNormalized } from 'vue-router'
import { useThemeStore } from '#stores/theme'

/**
 * Hold the theme light while `to` or any of its parent routes sets `meta.lightOnly`, and release it otherwise.
 *
 * The viewer has no dark theme, so the interface around it stays light whatever the user chose. A navigation that
 * failed leaves the page, and so the theme, where it was.
 */
export function syncRouteTheme (
    to: RouteLocationNormalized,
    _from?: RouteLocationNormalized,
    failure?: NavigationFailure | void,
) {
    if (failure) {
        return
    }
    useThemeStore().setLightOnly(to.matched.some(record => record.meta.lightOnly === true))
}
