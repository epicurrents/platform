import type { Component } from 'vue'
import type { LocationQuery, RouteLocationRaw, RouteRecordRaw } from 'vue-router'
import type { DataResource, EpicurrentsApp } from '@epicurrents/core/dist/types'
import type { DatasetItem } from '#api/library'
import type { RecordingAttachment } from '#api/recordings'
import type { StudyDescription } from '#viewer/datasetStudies'

/** The `meta` a code of one standard is sent with; see `ViewerPlugin.codeMeta`. */
export type CodeMetaProvider = () => unknown | Promise<unknown>

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
 * One dataset behind a session token, as `resolveSessionDatasets` returns it.
 * `shareToken` is per dataset rather than per session, so each is loaded under
 * its own grant. `preload` loads the dataset's members up front for this
 * session, as the dataset's own `preload_members` flag does for every reader;
 * a project whose session workflow spans the members sets it here rather than
 * on a dataset the session only reads.
 */
export interface SessionDatasetRef {
    id: number
    name: string
    preload?: boolean
    share_token: string
}

/**
 * One kind of item a plugin lists in the viewer. Each answer is the `StudyDescription` the viewer lists an item by,
 * so the viewer never learns what the item is.
 */
export interface ViewerItemSource {
    /**
     * Query parameters of a viewer URL that name this source's items, as `files` names recordings. A URL carrying one
     * opens the viewer on the items it names rather than blank.
     */
    params: string[]
    /**
     * The items the viewer URL's query names, in the order it names them; empty when it names none. A rejection means
     * an item could not be found or read, and the viewer reports it as it does for a recording.
     * @param query - The viewer route's query.
     */
    fromQuery: (query: LocationQuery) => Promise<StudyDescription[]>
    /**
     * Describe an attachment of a type this source owns, which the viewer lists after the recording it is attached
     * to; null for any other type, or for one of its own it cannot open.
     * @param attachment - The attachment as the recording attachments listing gives it.
     * @param shareToken - Share token the recording was read with, if any.
     */
    describeAttachment?: (attachment: RecordingAttachment, shareToken?: string) => StudyDescription | null
    /**
     * Describe a dataset member of a type this source owns, as the dataset item listing gives it (`object_type`,
     * `object_hash`, `object_name`); null for any other type, or for one of its own it cannot open.
     * @param item - The member as the dataset item listing gives it.
     * @param shareToken - Share token the dataset was opened with, if any.
     */
    describeDatasetItem?: (item: DatasetItem, shareToken?: string) => StudyDescription | null
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
     *  - `lightOnly: true` — on any route that mounts the viewer. The viewer has
     *    a light theme only, so the interface around it (navigation bar,
     *    drawers, dialogs, the project toggle tab) is held light while the
     *    route is active, whatever theme the user chose. Set on a parent route,
     *    it covers the children. A `viewerPanel` needs nothing: it renders on
     *    the core viewer route, which sets the flag.
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

    /**
     * Optional Vue component rendered as a floating overlay inside the viewer
     * page.  When provided, `ViewerView` mounts it via `<component :is="...">` and
     * passes `datasetId: number | null` as a prop.  The component is responsible
     * for deciding whether to render anything (e.g. by checking the user's role).
     */
    viewerPanel?: Component | null

    /**
     * Additional properties merged into the Epicurrents SETUP object that is
     * passed to `Epicurrents.createEpicurrentsApp()`.  These are merged after
     * (and can therefore override) the base setup built by the viewer.
     */
    extraSetup?: Record<string, unknown>

    /**
     * The `meta` to send with a code of each standard this plugin's vocabulary owns, keyed by standard. A vocabulary
     * whose server-side validator pins its `meta`, such as a schema release, answers it here, so every client that
     * saves a viewer event's codes through `#lib/annotationCodes` stamps it the same way. The provider runs on the
     * client, and should answer what the client itself coded against rather than what the server expects, so a
     * client out of step with the server is refused instead of being papered over.
     */
    codeMeta?: Record<string, CodeMetaProvider>

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
     * Called once the viewer has listed every item and loaded the ones it
     * loads up front, as the first item is being opened. Use this hook for
     * post-load actions such as registering event listeners, triggering
     * annotation fetches, or adjusting the dataset displayed in the sidebar.
     *
     * Only the studies loaded up front are passed: every member of a dataset
     * flagged `preload_members` or named with `preload` by
     * `resolveSessionDatasets`, and every item of a `?files=` link, within the
     * viewer's eager load limit, and otherwise only the items the platform
     * holds no metadata to list. The rest are listed and load when opened, so
     * a workflow spanning a dataset's members needs one of the two. An
     * attachment listed under a recording is not a study of the dataset: it
     * always loads when opened and is never passed.
     *
     * @param epic - The Epicurrents application instance.
     * @param studies - The studies loaded up front, in the order they were
     *   passed to the viewer.
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

    /**
     * Sources of viewer items of a type this plugin owns, which the viewer lists beside recordings and media.
     *
     * The viewer page knows recordings and media files only; a plugin that stores something the viewer can open
     * describes its items here, as the importer, name and URL the viewer lists them by, so the page needs no change
     * for a new type. Every source of every plugin and of the project is consulted.
     */
    viewerItems?: ViewerItemSource[]
}
