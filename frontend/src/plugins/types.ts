/**
 * Type contract for frontend plugins.
 *
 * Plugins reuse the exact same `ViewerPlugin` / `ProjectNavLink` contract that
 * projects implement — a plugin customises the viewer through the same hooks; it
 * simply composes alongside the active project instead of owning the landing
 * page. Re-exported here so a plugin under `frontend/src/plugins/<name>/` can
 * import its types from a sibling path (`../types`) the same way a project does.
 *
 * @package epicurrents-frontend
 */
export type { ProjectNavLink, ViewerPlugin } from '#projects/types'
