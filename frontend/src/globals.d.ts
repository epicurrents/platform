/**
 * Platform-only globals.
 *
 * `window.__EPICURRENTS__` is deliberately absent here: `@epicurrents/core` owns that ambient
 * declaration, and a second one on this side does not merge — core's wins and every platform
 * field reads as missing. Platform additions go through the module augmentation in
 * `src/types/core-global-augment.ts`; the members whose core types are too narrow for platform
 * use are reached through the helpers in `#lib/viewerGlobal`.
 */

declare global {
    const Epicurrents: any
    interface Window {
        PUBLIC_URL: string
        browserImportFunction: (moduleId: string) => void
    }
}
export {}
