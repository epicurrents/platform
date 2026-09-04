import type { ViewerPlugin } from './types'

/**
 * Default no-op plugin used when no project is active (`VITE_PROJECT` is
 * unset or empty).  All hooks are omitted so the viewer runs with its
 * built-in behaviour unchanged.
 */
export const plugin: ViewerPlugin = {}
