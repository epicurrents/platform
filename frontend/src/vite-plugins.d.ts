/// <reference types="vite-plugin-pwa/client" />

declare module "vite-plugin-cross-origin-isolation"

interface ImportMetaEnv {
    /**
     * Active project name, mirroring the backend `EPICURRENTS_PROJECT`
     * setting.  Controls which `ViewerPlugin` is bundled into the frontend.
     * Any directory name under `projects/` that ships a `frontend/index.ts`;
     * empty (or unset) selects the base deployment with no project.
     */
    readonly VITE_PROJECT?: string

    /**
     * Enabled plugin names, comma-separated, mirroring the backend
     * `EPICURRENTS_PLUGINS` setting.  Each enabled plugin's `ViewerPlugin`
     * contributions are merged into the frontend bundle (see
     * `#plugins/active`).  Example: `'dicom'`.  Unset = no plugins.
     */
    readonly VITE_PLUGINS?: string
}

interface ImportMeta {
    readonly env: ImportMetaEnv
}

/**
 * Build-time flag defined in vite.config.ts: `true` when `dicom` is listed in
 * `VITE_PLUGINS`.  Replaced with a boolean literal at build time so disabled
 * plugins tree-shake out of the bundle (see `src/plugins/active.ts`).
 */
declare const __PLUGIN_DICOM__: boolean
