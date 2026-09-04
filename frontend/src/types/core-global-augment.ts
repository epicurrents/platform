/**
 * Module augmentation adding the platform's own fields to the core-owned `EpicurrentsGlobal`, so
 * the single ambient `Window.__EPICURRENTS__` that `@epicurrents/core` declares carries the host
 * callbacks too. Declaring a competing `Window.__EPICURRENTS__` here instead does not merge — the
 * core declaration wins and every added field reads as absent.
 *
 * The augmentation silently no-ops under several conditions, so keep this file minimal and
 * isolated: no `declare global` block (it does not coexist with `declare module`); no
 * `EpicurrentsGlobal` name in scope (a local type or an import of that name shadows the interface
 * below); and no import of the augmented module `#epicurrents/core/dist/types`, directly or
 * transitively. That last constraint is why the signatures below are inlined rather than reused
 * from the declarations they mirror — keep them in sync with their definition sites. It is also
 * why the viewer's `announce` callback is absent here: reaching for the interface's
 * `InterfaceGlobalAdditions` would pull the augmented module in through that file's own imports.
 *
 * `announce` instead arrives from the interface's own augmentation of the same interface, which
 * `tsconfig.app.json` names in `include` so it joins this compilation unit. Both specifiers
 * resolve to the same core declaration file, so the two augmentations merge onto one
 * `EpicurrentsGlobal`.
 */

export {}

declare module '#epicurrents/core/dist/types' {
    interface EpicurrentsGlobal {
        /**
         * Host → viewer callback set by the viewer when its app is created: the platform calls it
         * after a (re-)login so network loads latched on a prior auth failure resume. Undefined
         * when no viewer is mounted, so callers guard with `?.()`.
         */
        notifySessionRestored?: () => void
        /**
         * The platform's WebAwesome `registerIconLibrary`, exposed so the embedded viewer UMD
         * bundle registers its icon libraries into the same WebAwesome instance that owns the
         * `wa-icon` custom element.
         */
        registerIconLibrary?: (
            name: string,
            options: {
                resolver: (name: string, family: string, variant: string) => string,
                mutator?: (svg: SVGElement) => void,
            },
        ) => void
    }
}
