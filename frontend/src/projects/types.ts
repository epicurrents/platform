import type { Component } from 'vue'
import type { RouteLocationRaw, RouteRecordRaw } from 'vue-router'
import type { DataResource, EpicurrentsApp } from '#epicurrents/core/dist/types'

export interface ProjectNavLink {
    /** Stable identifier used as a Vue key and for debugging. */
    id: string
    /** Top-nav grouping key, matched against route.meta.navSection. */
    section: string
    /** Label passed to t(label, 'App') as key/fallback text. */
    label: string
    /** Route target consumed by <RouterLink :to>. */
    to: RouteLocationRaw
    /** Optional WebAwesome icon name, rendered before the label. */
    icon?: string
    /** Sort order among other nav links. Lower comes first. */
    order?: number
    /**
     * When true the link is hidden unless the current user has staff or
     * superuser status.  Use this for admin-only sections.
     */
    requiresStaff?: boolean
    /**
     * When true the link is hidden unless the current user has superuser
     * status.
     */
    requiresSuperuser?: boolean
}

/**
 * Plugin interface that project-specific modules implement to customise the
 * Epicurrents viewer without modifying the core ViewerView.
 *
 * All hooks are optional. Projects only need to implement the hooks relevant
 * to their customisation needs; the rest default to no-ops via `base.ts`.
 *
 * ## Event bus
 *
 * The Epicurrents event bus (`window.__EPICURRENTS__.EVENT_BUS`) becomes
 * available as soon as `createEpicurrentsApp()` returns, i.e. at the start of
 * `onAppReady`.  Use the helpers in `../eventBus` to subscribe, unsubscribe,
 * and dispatch events without touching the global directly:
 *
 * ```ts
 * import { onEvent, offEvent, emitEvent } from '../eventBus'
 * ```
 */
export interface ViewerPlugin {
    /**
     * Optional Vue component rendered as a floating overlay inside the viewer
     * page.  When provided, `ViewerView` mounts it via `<component :is="...">` and
     * passes `datasetId: number | null` as a prop.  The component is responsible
     * for deciding whether to render anything (e.g. by checking the user's role).
     */
    /**
     * Additional routes registered into the application router at startup.
     * Use this to add project-specific pages (e.g. a student join page) that
     * live outside the standard view hierarchy.  Routes defined here are
     * appended to the router exactly as written.
     *
     * Recommended `meta` fields:
     *  - `requiresAuth: true` — gate the route behind the login redirect.
     *  - `title: 'Label'` — drives the browser tab title via the global
     *    afterEach hook.  Becomes `"<title> - Epicurrents"`.  Omit to fall
     *    back to plain `"Epicurrents"`.  The label is run through
     *    `t(key, 'Router')`, so it can also be supplied as an i18n key.
     *  - `navSection: 'foo'` — matches the `section` field on a sibling
     *    `ProjectNavLink` to drive the active-nav highlight.
     */
    routes?: RouteRecordRaw[]

    /**
     * Additional top-navigation links rendered in App.vue.
     *
     * These links are only visible when this plugin is active
     * (`VITE_PROJECT=<name>`), so project-specific UI stays isolated in the
     * project folder.
     */
    navLinks?: ProjectNavLink[]

    /**
     * Additional icon registrations for this project only.
     *
     * Icons are merged with the base icon registry at app startup. If a name
     * already exists in base icons, the base icon is kept and the project
     * entry is ignored.
     */
    icons?: Record<string, string>

    /**
     * Optional named icon libraries for variant icons (e.g. regular/solid).
     *
     * Each key is a WebAwesome library name consumed via
     * `<wa-icon library="<name>" name="...">`.
     *
     * This leaves the default icon flow unchanged and enables multiple
     * variants of the same icon name in parallel without renaming.
     */
    iconLibraries?: Record<string, Record<string, string>>

    viewerPanel?: Component | null

    /**
     * Additional properties merged into the Epicurrents SETUP object that is
     * passed to `Epicurrents.createEpicurrentsApp()`.  These are merged after
     * (and can therefore override) the base setup built by the viewer.
     */
    extraSetup?: Record<string, unknown>

    /**
     * Called once both `Epicurrents.createEpicurrentsApp()` has resolved **and**
     * `window.__EPICURRENTS__.EVENT_BUS` is confirmed non-null.  Both conditions
     * are awaited by `ViewerView` before invoking this hook, so it is safe to
     * call `onEvent` / `offEvent` / `emitEvent` (or use the `bus` argument
     * directly) without any additional timing guards.
     *
     * Use this hook to configure viewer behaviour, register custom components,
     * adjust UI state, or set up event listeners — all before any study loads.
     *
     * @param epic - The newly created Epicurrents application instance.
     * @param bus - The live event bus (`window.__EPICURRENTS__.EVENT_BUS`),
     *   passed in directly so projects do not need to import `getEventBus()`.
     */
    onAppReady?: (epic: EpicurrentsApp, bus: EventTarget) => void | Promise<void>

    /**
     * Called after all studies have been loaded and the first one has been
     * opened in the viewer.  Use this hook for post-load actions such as
     * registering event listeners, triggering annotation fetches, or
     * adjusting the dataset displayed in the sidebar.
     *
     * @param epic - The Epicurrents application instance.
     * @param studies - All successfully loaded `DataResource` objects, in
     *   the same order as the recordings passed to the viewer.
     */
    onStudiesReady?: (epic: EpicurrentsApp, studies: DataResource[]) => void | Promise<void>

    /**
     * Resolve a `?session=<token>` viewer URL to the datasets it should load.
     *
     * Session mode is a project concept — a token standing for a set of
     * datasets, each with its own share token — but the viewer has to
     * understand the URL, so the branch lives in `ViewerView` and the meaning
     * comes from here. A project that has no such concept simply omits this,
     * and a session URL then reports that the deployment does not support one.
     *
     * A hook rather than a direct import, because a core view naming one
     * project makes the platform unbuildable without that project — and after
     * the extraction the platform ships without any of them.
     *
     * @param sessionToken - The raw token from the URL. It is the credential in
     *   its own right, so the call is unauthenticated.
     */
    resolveSessionDatasets?: (sessionToken: string) => Promise<SessionDatasetRef[]>
}

/**
 * One dataset behind a session token, as `resolveSessionDatasets` returns it.
 * `shareToken` is per dataset rather than per session, so each is loaded under
 * its own grant.
 */
export interface SessionDatasetRef {
    id: number
    name: string
    share_token: string
}
