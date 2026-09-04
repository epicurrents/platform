/**
 * Ambient declaration for the `#project` alias.
 *
 * Kept in its own file rather than appended to vite-plugins.d.ts, where a
 * shorthand ambient declaration above it (`declare module "x"` with no body)
 * put the parser in a state that rejected this one.
 */
/**
 * The active project's viewer plugin, resolved by the `#project` alias in
 * vite.config.ts to `projects/<VITE_PROJECT>/frontend/index.ts` — or to the
 * base no-op plugin when no project is set.
 *
 * Declared rather than mapped through tsconfig `paths` because the target is
 * chosen at build time from an environment variable, which `paths` cannot
 * interpolate. What is stable is the contract, so that is what is stated here:
 * whatever the alias resolves to exports a `ViewerPlugin`. The project's own
 * code is type-checked on its own terms via the `include` entry that reaches
 * each project's frontend directory, so nothing is taken on trust — this only spares
 * `active.ts` from naming a module that does not exist until the build picks
 * one.
 */
declare module "#project" {
    export const plugin: import("../projects/types").ViewerPlugin
}
