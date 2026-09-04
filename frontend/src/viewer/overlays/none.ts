/**
 * No-op viewer overlay — the default when no project (or a project without a
 * viewer overlay) is active.
 *
 * `vite.config.base.ts` aliases `@viewer-overlay` to this file unless
 * `VITE_PROJECT` names a project that ships an `overlays/<project>.ts`. The base
 * build then registers only the stable modalities from `base.ts`.
 */
import type { SetupContext } from '@epicurrents/interface'

/** Register no additional modules — the base build ships only the stable modalities. */
export const registerProjectOverlay = (_ctx: SetupContext): void => {
    // Intentionally empty.
}
