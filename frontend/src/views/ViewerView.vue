<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { onBeforeRouteLeave, useRoute } from 'vue-router'
import { t } from '#i18n'
import { showToast } from '#lib/toast'
import { useAuthStore } from '#stores/auth'
import { getRecordingDetail } from '#api/recordings'
import {
    browseFiltersFromQuery,
    getDataset,
    listAllDatasetItems,
    listMatchingRecordings,
    type DatasetBrowseFilters,
} from '#api/library'
import { getMediaDetail, listMediaFiles, type MediaFileDetail, type MediaFileSummary } from '#api/media'
import type {
    BiosignalResource,
    DataResource,
    EpicurrentsApp,
    EpicurrentsGlobal,
    MediaDataset,
    VideoAttachment,
} from '@epicurrents/core/dist/types'
// viewer/interface has no compiled .d.ts output; declare only what we need here.
type InterfaceSettings = { app: { disclaimerAccepted: number } }
import type { Recording } from '#api/recordings'
import { plugin } from '#projects/active'
import { plugin as pluginsPlugin } from '#plugins/active'
import { mergeSetup } from '#plugins/base'
import { waitForEventBus } from '#projects/eventBus'
import { getViewerConfig } from '#api/viewerConfig'
import { applyViewerSettingsOverrides, VIEWER_USER_SETTINGS_PATH, type ViewerSettingsOverrides } from '#lib/viewerConfig'
import { leadFieldProvider } from '#viewer/leadFields'
import {
    describeMedia,
    describeRecording,
    eagerStudyLoadLimit,
    listedFromBrowseRow,
    listedFromRecording,
    type DeferredStudyApp,
    type ListedRecording,
} from '#viewer/datasetStudies'
import { createUploadTarget } from '#lib/exportTargets'

// The overlay panel comes from the active project if it defines one, otherwise
// from the first enabled plugin that does. A page hosts a single overlay.
const viewerPanel = plugin.viewerPanel ?? pluginsPlugin.viewerPanel

/** One viewer-side dataset to create plus the items to load into it. */
interface DatasetBundle {
    name: string
    shareToken: string | undefined
    items: BundleItem[]
    /** Load every item when the viewer opens, rather than each when it is opened. */
    preload: boolean
    /** The dataset's own viewer-config overrides, for a bundle that is a dataset. */
    viewerConfig?: ViewerSettingsOverrides
}

/**
 * Per-item payload after the platform-side fetch. Discriminated so the mount loop can pick the right importer + URL
 * without re-querying the list endpoint's metadata. A recording is listed from what its dataset's browser rows give;
 * its detail, which carries the interruption table, is read when it is loaded, unless the link named it and it was
 * read up front.
 */
type BundleItem =
    | { kind: 'recording'; listed: ListedRecording; recording: Recording | null }
    | { kind: 'media'; media: MediaFileDetail }

/**
 * Seed the viewer resource with the platform's complete interruption table from the recording
 * metadata. The platform parses the whole file at ingest, so its gap table is authoritative;
 * marking it trusted lets the viewer allow random access on a discontinuous recording instead
 * of clamping navigation to the span it has decoded itself. Interruption starts are
 * data-position seconds — the same time base the viewer's signal cache uses — so the refs map
 * to the table without conversion. Non-biosignal resources are skipped.
 */
function seedTrustedInterruptions(resource: DataResource, recording: Recording): void {
    // Gate on the interruption rows themselves, not `meta.discontinuous`: the rows are the
    // authoritative parse and older recordings can carry them while their `discontinuous` flag
    // predates reliable detection. A discontinuous recording whose gap rows are absent
    // deliberately falls through — the viewer then self-discovers gaps and clamps, rather than
    // being told an empty table is complete.
    if (!recording.interruptions.length) {
        return
    }
    const target = resource as Partial<BiosignalResource>
    if (typeof target.setTrustedInterruptions !== 'function') {
        return
    }
    target.setTrustedInterruptions(
        new Map(recording.interruptions.map((intr) => [intr.start, intr.duration]))
    )
}

/**
 * Finish a study once the viewer has loaded it, whether up front or when it was opened: seed a recording's trusted
 * interruption table, from its detail, and attach its videos. Runs before the loaded resource is activated, which is
 * when the activation flow delivers the interruption table to the reader. A detail that cannot be read leaves the
 * viewer to discover the gaps itself.
 * @param resource - The loaded resource.
 * @param item - The item it was listed from.
 * @param shareToken - Share token the item's dataset was opened with, if any.
 */
async function studyLoaded(resource: DataResource, item: BundleItem, shareToken: string | undefined): Promise<void> {
    if (item.kind !== 'recording') {
        return
    }
    const hash = item.listed.hash
    const [recording] = await Promise.all([
        item.recording
            ? Promise.resolve(item.recording)
            : getRecordingDetail(hash, shareToken).catch((e: unknown) => {
                console.warn(`[Epicurrents] Could not read the interruption table of ${hash}.`, e)
                return null
            }),
        attachRecordingVideos(resource, hash),
    ])
    if (recording) {
        seedTrustedInterruptions(resource, recording)
    }
}

/**
 * Fetch the videos attached to a recording and expose them on the viewer
 * resource so the ACC module's video window can play them. Session auth only:
 * the ``<video>`` element fetches ``/media/api/v1/<hash>/file`` directly with
 * cookies — federated and share-token video playback are not supported (a
 * browser video element cannot carry the FederatedBearer header; see
 * media/README.md). Unsupported rows (extension outside the live allowlist) and
 * fetch failures are skipped so a missing video never blocks the recording.
 */
async function attachRecordingVideos(resource: DataResource, hash: string): Promise<void> {
    let mediaVideos: MediaFileSummary[]
    try {
        mediaVideos = await listMediaFiles({
            attachedToType: 'recording',
            attachedToId: hash,
            mediaType: 'video',
        })
    } catch (e) {
        console.warn(`[Epicurrents] Could not load attached videos for ${hash}.`, e)
        return
    }
    const playable = mediaVideos.filter(v => v.is_supported)
    if (!playable.length) {
        return
    }
    const attachments: VideoAttachment[] = playable.map(v => {
        // Bind the relative path to a local before ``new URL(...)``: Vite's
        // minifier otherwise mishandles an inline template literal here and
        // emits ``new URL(undefined, ...)``, which resolves to
        // ``/assets/undefined``. The recording and media study branches above
        // sidestep the same trap the same way.
        const relativePath = `/media/api/v1/${v.content_hash}/file`
        return {
            // Duration isn't stored server-side yet, so the end point is left
            // open. The ACC video window plays videos[0] directly and doesn't
            // depend on it; a real end point follows once the conversion step
            // probes duration.
            endTime: Number.MAX_SAFE_INTEGER,
            group: 0,
            startTime: v.time_offset ?? 0,
            syncPoints: [],
            url: new URL(relativePath, import.meta.url).toString(),
        }
    })
    ;(resource as BiosignalResource).videos = attachments
}

const SCOPE = 'ViewerView'
const route = useRoute()

/** Resolver for the in-flight route change the leave prompt is holding. */
const pendingLeaveResolver = ref<((leave: boolean) => void) | null>(null)
const showLeavePrompt = ref(false)

let epic = null as null | EpicurrentsApp

const auth = useAuthStore()

/**
 * All recording hashes passed via the `files` query param (comma-separated).
 * Stored upper-cased to match the API convention.
 */
const hashes = computed(() =>
    ((route.query.files as string | undefined) ?? '')
        .split(',')
        .map(h => h.trim())
        .filter(Boolean)
        .map(h => h.toUpperCase()),
)

/**
 * Media-file content hashes passed via the `media` query param. Same
 * shape as `files` — comma-separated, upper-cased — for opening one or
 * more media items in a standalone viewer session.
 */
const mediaHashes = computed(() =>
    ((route.query.media as string | undefined) ?? '')
        .split(',')
        .map(h => h.trim())
        .filter(Boolean)
        .map(h => h.toUpperCase()),
)

/**
 * Optional dataset identifier passed via the `dataset` query param — the
 * opaque object_hash in links the frontend builds, with the integer PK also
 * accepted. Passed through verbatim; the backend resolves either form. When
 * present, all recordings in the dataset are loaded into the viewer.
 */
const datasetId = computed(() => {
    const raw = (route.query.dataset as string | undefined)?.trim()
    return raw ? raw : null
})

/**
 * Optional public share token passed via the `token` query param.
 * When present, API calls are made without requiring session authentication.
 */
const shareToken = computed(() => (route.query.token as string | undefined) || undefined)

/**
 * Optional teaching session token passed via the `session` query param.
 * When present, a project's viewer panel can open its drawer and pre-select the session.
 */
const sessionToken = computed(() => (route.query.session as string | undefined) || undefined)

/**
 * The dataset browser's query, when a `?dataset=` link carries one: its filters (`modality`, `subject.<key>`, `tag`)
 * and its `sort`, named as the browse endpoint names them. The viewer then opens the recordings the browser lists
 * under that query, in its order. Carrying the query rather than the matching hashes keeps the link short however
 * many recordings it opens.
 */
const datasetFilters = computed(() => browseFiltersFromQuery(route.query))
const datasetSort = computed(() => (route.query.sort as string | undefined)?.trim() || null)

const bundles = ref<DatasetBundle[]>([])
const loading = ref(true)
const error = ref<string | null>(null)
/**
 * What the viewer is doing between the items being read and the first of them opening: listing the items, then
 * loading those it loads up front. Null once the first item is opened.
 */
const openingStatus = ref<string | null>(null)

/** Total items across all bundles — used for empty-state detection. */
const totalItems = computed(
    () => bundles.value.reduce((n, b) => n + b.items.length, 0),
)

/**
 * No reference params at all (`files` / `media` / `dataset` / `session`): open
 * the viewer blank so its own load UI is available, instead of erroring.
 */
const emptyMode = computed(() =>
    !sessionToken.value
    && datasetId.value === null
    && !hashes.value.length
    && !mediaHashes.value.length,
)

/**
 * A dataset's members as a bundle the viewer lists. Recordings are listed from the dataset browser's rows, a page of
 * 200 at a time, so the size of the dataset sets the number of pages rather than of requests; a media member is read
 * for its detail. With a browser query, only the recordings the browser lists under it, in its order; without one,
 * every member in the dataset's order. A member the browser does not list to the caller, one they cannot read, is
 * left out.
 * @param id - The dataset's object hash or primary key.
 * @param token - Share token the dataset is read with, if any.
 * @param filters - The browser filters the link carries, or null.
 * @param sort - The browser sort the link carries, or null.
 */
async function loadDatasetBundle(
    id: number | string,
    token: string | undefined,
    filters: DatasetBrowseFilters | null = null,
    sort: string | null = null,
): Promise<DatasetBundle> {
    const browsed = filters !== null || sort !== null
    // The browser lists recordings only, so a browsed link opens no media and needs no member listing.
    const [dataset, rows, allItems] = await Promise.all([
        getDataset(id, token),
        listMatchingRecordings(id, filters, sort ?? 'name', token),
        browsed ? Promise.resolve(null) : listAllDatasetItems(id, token),
    ])
    const listedByHash = new Map(rows.map(row => [row.hash, listedFromBrowseRow(row)]))
    const recordingItem = (hash: string): BundleItem | null => {
        const listed = listedByHash.get(hash)
        return listed ? { kind: 'recording', listed, recording: null } : null
    }
    // Preserve dataset order. Unsupported media files are dropped here so the mount loop never tries to load them.
    const bundleItems = allItems === null
        ? rows.map(row => recordingItem(row.hash)).filter((i): i is BundleItem => i !== null)
        : (await Promise.all(
            allItems.map(async (item): Promise<BundleItem | null> => {
                if (!item.object_hash) {
                    return null
                }
                if (item.object_type === 'recording') {
                    return recordingItem(item.object_hash)
                }
                if (item.object_type === 'mediafile') {
                    if (item.is_supported === false) {
                        console.warn(`[Epicurrents] Skipping unsupported media ${item.object_hash}`)
                        return null
                    }
                    const media = await getMediaDetail(item.object_hash)
                    return { kind: 'media', media }
                }
                return null
            }),
        )).filter((i): i is BundleItem => i !== null)
    return {
        items: bundleItems,
        name: dataset.name,
        preload: dataset.preload_members ?? false,
        shareToken: token,
        viewerConfig: dataset.viewer_config,
    }
}

function cancelLeave () {
    showLeavePrompt.value = false
    pendingLeaveResolver.value?.(false)
    pendingLeaveResolver.value = null
}

function confirmLeave () {
    showLeavePrompt.value = false
    // The application stays alive in this document after the route changes, so its own unload guard
    // would ask again when the tab is closed, about a session the user has already given up.
    window.__EPICURRENTS__?.APP?.allowUnload?.()
    pendingLeaveResolver.value?.(true)
    pendingLeaveResolver.value = null
}

// In-app navigation, the Back button included. The viewer installs its own `beforeunload` handler
// for the document-level exits, which a route change never triggers.
onBeforeRouteLeave(() => {
    if (!window.__EPICURRENTS__?.APP?.unloadNeedsConfirmation) {
        return true
    }
    return new Promise<boolean>(resolve => {
        pendingLeaveResolver.value = resolve
        showLeavePrompt.value = true
    })
})

onMounted(async () => {
    if (window.__EPICURRENTS__?.APP) {
        // The viewer mounts its interface into the container div in this template, which Vue
        // destroys when the view unmounts, and the framework allows one application per document
        // with no way to re-attach the existing one. A remount — a route change away and back, or
        // the browser Back button — therefore has to begin from a fresh document; rendering into
        // the new container would show an empty viewer over a still-running application, and
        // building a second one would leave the first holding its worker and its shared buffer for
        // as long as the tab lives. The waiver is what keeps that application's own unload guard
        // from prompting about a reload the user has already answered for.
        window.__EPICURRENTS__.APP.allowUnload?.()
        window.location.reload()
        return
    }
    try {
        if (sessionToken.value) {
            // Session mode: resolve the session token to its dataset list, then
            // load every dataset (each with its own per-dataset share token).
            // Session mode is defined by the active project, not by the
            // viewer — see ViewerPlugin.resolveSessionDatasets.
            if (!plugin.resolveSessionDatasets) {
                error.value = t('This deployment does not support session links.', SCOPE)
                return
            }
            const sessionDatasets = await plugin.resolveSessionDatasets(sessionToken.value)
            if (!sessionDatasets.length) {
                error.value = t('This session has no datasets.', SCOPE)
                return
            }
            bundles.value = await Promise.all(sessionDatasets.map(async d => {
                const bundle = await loadDatasetBundle(d.id, d.share_token)
                // The session may ask for its datasets' members up front without the datasets being flagged.
                return { ...bundle, preload: bundle.preload || d.preload === true }
            }))
        } else if (datasetId.value !== null) {
            // Single-dataset mode.
            bundles.value = [
                await loadDatasetBundle(datasetId.value, shareToken.value, datasetFilters.value, datasetSort.value),
            ]
        } else if (hashes.value.length || mediaHashes.value.length) {
            // Individual items mode — one unauthenticated bundle that mixes
            // recordings (`?files=`) and media (`?media=`) in the order they
            // were declared in the URL.
            const recordingItems = await Promise.all(
                hashes.value.map(async h => {
                    const recording = await getRecordingDetail(h, shareToken.value)
                    return { kind: 'recording' as const, listed: listedFromRecording(recording), recording }
                }),
            )
            const mediaItems = await Promise.all(
                mediaHashes.value.map(async h => ({
                    kind: 'media' as const,
                    media: await getMediaDetail(h),
                })),
            )
            // A link naming its items is a selection the user made, so every item loads up front.
            bundles.value = [{
                name: 'Dataset',
                shareToken: shareToken.value,
                items: [...recordingItems, ...mediaItems],
                preload: true,
            }]
        }
        // No ref params (emptyMode): leave bundles empty and fall through to
        // mount the viewer blank — its own load UI takes over.
    } catch {
        error.value = t('One or more items could not be found or access was denied.', SCOPE)
    } finally {
        loading.value = false
    }

    if (totalItems.value === 0 && !emptyMode.value) {
        if (!error.value) {
            error.value = t('This dataset contains no viewable items.', SCOPE)
        }
        return
    }
    if (totalItems.value > 1) {
        openingStatus.value = t('Opening {count} items…', SCOPE, { count: totalItems.value })
    }

    // Poll until the Epicurrents global is available, then mount.
    let retries = 0
    const awaitApp = async () => {
        if (typeof Epicurrents === 'undefined') {
            if (retries < 400) {
                retries++
                console.debug(`Waiting for Epicurrents to be available (retry ${retries})...`)
                setTimeout(awaitApp, 1000)
            } else {
                openingStatus.value = null
            }
            return
        }
        const setup = mergeSetup(
            Object.assign(new Object(null), {
                assetPath: '/viewer',
                // import.meta.hot is defined only under the Vite dev server, and
                // undefined in any build — so this is DEBUG while developing and
                // WARN in the deployed bundle. import.meta.env.PROD can't be used
                // here: the dist is built with NODE_ENV=development, so PROD is false.
                logThreshold: import.meta.hot ? 'DEBUG' : 'WARN',
                // Production unless this platform SPA is dev-served. The viewer lib can't detect
                // the platform's dev mode itself (its own import.meta.hot is frozen at lib-build
                // time), so the host decides. Production suppresses the native context menu so it
                // can't shadow the plot's own right-click menu.
                isProduction: !import.meta.hot,
                user: auth.user ? `${auth.user.first_name} ${auth.user.last_name}` : null,
                // Only for a signed-in session — the endpoint is session-authenticated, so a
                // share-token or unauthenticated viewer would collect 401s for no benefit.
                userSettingsBackend: auth.user ? VIEWER_USER_SETTINGS_PATH : '',
            }),
            // Plugins first, then the active project — the project has the last
            // word on a conflicting value, matching the backend's plugins < project
            // settings precedence. The merge recurses, so a plugin and the project
            // can both configure one module.
            pluginsPlugin.extraSetup ?? {},
            plugin.extraSetup ?? {},
            // Tell the EEG module where lead fields come from. The viewer holds no URLs
            // of its own, so its source-localisation tool reports itself unavailable
            // unless a host supplies a provider.
            { modules: { eeg: { leadFieldProvider } } },
        )
        // Create the global Epicurrents object if not available.
        if (typeof window.__EPICURRENTS__ === 'undefined') {
            window.__EPICURRENTS__ = {
                APP: null,
                EVENT_BUS: null,
                RUNTIME: null,
                SETUP: setup as EpicurrentsGlobal['SETUP'],
            }
        } else {
            if (typeof window.__EPICURRENTS__.EVENT_BUS === 'undefined') {
                window.__EPICURRENTS__.EVENT_BUS = null
            }
            if (typeof window.__EPICURRENTS__.RUNTIME === 'undefined') {
                window.__EPICURRENTS__.RUNTIME = null
            }
            if (typeof window.__EPICURRENTS__.SETUP === 'undefined') {
                window.__EPICURRENTS__.SETUP = setup as EpicurrentsGlobal['SETUP']
            } else {
                window.__EPICURRENTS__.SETUP = Object.assign(
                    new Object(null),
                    setup,
                    window.__EPICURRENTS__.SETUP
                )
            }
        }
        requestAnimationFrame(async () => {
            epic = await Epicurrents.createEpicurrentsApp(setup)
            if (!epic) {
                console.error('Failed to initialize Epicurrents app.')
                openingStatus.value = null
                return
            }
            if (totalItems.value === 1) {
                // Hide the dataset navigator sidebar when only one item is loaded, to maximize screen space.
                ;(epic.runtime.APP as any).uiComponentVisible.navigator = false
            }
            // TODO: A temporary fix to avoid the disclaimer; this needs a user setting in the future.
            const interfaceSettings = epic.runtime.INTERFACE as InterfaceSettings
            interfaceSettings.app.disclaimerAccepted = 1
            // Apply the deployment's viewer-config overrides (project seed merged
            // with the editable database overrides) before the project hook and
            // before any study loads, so per-recording reads like the default
            // montage and aEEG epoch length pick them up. Non-fatal — the viewer
            // keeps its built-in defaults if the fetch fails (e.g. mock backend).
            try {
                const config = await getViewerConfig()
                let overrides = config.effective
                // A dataset opened via ?dataset=<id> layers its own overrides on
                // top of the deployment config (most specific wins).
                if (datasetId.value !== null) {
                    overrides = { ...overrides, ...bundles.value[0]?.viewerConfig }
                }
                applyViewerSettingsOverrides(epic, overrides)
            } catch (err) {
                console.warn('[viewer-config] could not load deployment overrides:', err)
            }
            // A recording the person opened from their own disk can be uploaded to
            // their recordings from the viewer's file menu; the viewer offers it for
            // nothing it loaded from a URL. Signed-in sessions only, since the upload is
            // session-authenticated, and never under a share token, whose holder has no
            // recordings of their own here.
            if (auth.user && !shareToken.value) {
                epic.registerSignalExportTarget('platform/upload', createUploadTarget())
            }
            // Wait for the event bus to be live (may resolve asynchronously after
            // createEpicurrentsApp), then give the plugin a chance to configure
            // the app and wire up event listeners before any study loads.
            const bus = await waitForEventBus()
            await plugin.onAppReady?.(epic, bus)
            await pluginsPlugin.onAppReady?.(epic, bus)

            // Surface the viewer's per-origin network breaker transitions (each service emits them
            // on the shared bus) as toasts: a warning while a load endpoint is unreachable and
            // retrying, a danger toast on an auth failure, and a success toast once it recovers. The
            // `closed` toast fires only for an endpoint that was previously warned, so a normal load
            // stays silent. Recovery after re-login is driven separately by notifySessionRestored().
            const warnedEndpoints = new Set<string>()
            bus.addEventListener('network-status', (e: Event) => {
                const detail = (e as CustomEvent).detail as { endpoint?: string, state?: string } | null
                const endpoint = detail?.endpoint ?? ''
                if (detail?.state === 'open-unavailable') {
                    warnedEndpoints.add(endpoint)
                    showToast(t('Connection problem while loading data — retrying…', SCOPE), 'warning')
                } else if (detail?.state === 'open-auth') {
                    warnedEndpoints.add(endpoint)
                    showToast(t('Your session expired while loading data. Sign in to continue.', SCOPE), 'danger')
                } else if (detail?.state === 'closed' && warnedEndpoints.delete(endpoint)) {
                    showToast(t('Connection restored.', SCOPE), 'success')
                }
            })

            // Auto-activate the first resource of any dataset the user switches
            // to that has no active resource yet, so the user is never greeted
            // by an empty signal area after clicking a dataset in the sidebar.
            bus.addEventListener('set-active-dataset', (e: Event) => {
                const detail = (e as CustomEvent).detail as {
                    payload: {
                        activeResources?: ReadonlyArray<DataResource>
                        resources?: Map<string, { resource: DataResource }>
                    } | null
                    phase?: 'before' | 'after'
                } | null
                if (detail?.phase !== 'after') {
                    return
                }
                const ds = detail.payload
                if (!ds || (ds.activeResources?.length ?? 0) > 0) {
                    return
                }
                const first = ds.resources?.values().next().value?.resource
                if (first && epic) {
                    // Through the interface, which loads a listed study before activating it.
                    epic.interface?.openResource(first)
                }
            })

            // Every item is listed first and loads when it is opened, so a large dataset costs no fetch and no reader
            // per member. A dataset flagged to preload, and an item the platform holds no metadata to list, are loaded
            // up front instead, as many as the viewer's eager load limit allows.
            const app = epic as DeferredStudyApp
            const eagerLimit = eagerStudyLoadLimit(app)
            const eager: DataResource[] = []
            const viewerDatasets: MediaDataset[] = []
            for (const [bundleIdx, bundle] of bundles.value.entries()) {
                // Only the first dataset is marked active; later ones must not
                // override the active selection or the UI ends up in an
                // inconsistent state on mount.
                const viewerDataset = app.createDataset(bundle.name, bundleIdx === 0)
                viewerDatasets.push(viewerDataset)
                for (const item of bundle.items) {
                    const description = item.kind === 'recording'
                        ? describeRecording(item.listed, bundle.shareToken)
                        : describeMedia(item.media)
                    if (!description) {
                        const media = (item as { media: MediaFileDetail }).media
                        console.warn(`[Epicurrents] No importer registered for media ${media.file_extension}; skipping ${media.content_hash}`)
                        continue
                    }
                    const listing = app.addDeferredStudy({
                        loader: description.loader,
                        name: description.name,
                        onLoaded: (resource) => studyLoaded(resource, item, bundle.shareToken),
                        options: { isValidated: true, name: description.name },
                        properties: description.properties ?? undefined,
                        source: description.source,
                    }, { dataset: viewerDataset })
                    if (!listing) {
                        const hash = item.kind === 'recording' ? item.listed.hash : item.media.content_hash
                        console.error(`[Epicurrents] Could not list the item ${hash}.`)
                        continue
                    }
                    if (bundle.preload || !description.properties) {
                        eager.push(listing)
                    }
                }
            }
            if (eager.length > eagerLimit) {
                console.warn(
                    `[Epicurrents] Loading ${eagerLimit} of ${eager.length} items up front; the rest load when opened.`
                )
            }
            const loadedStudies: DataResource[] = []
            const eagerLoads = eager.slice(0, eagerLimit)
            for (const [index, listing] of eagerLoads.entries()) {
                if (eagerLoads.length > 1) {
                    openingStatus.value = t(
                        'Loading {done} of {count} items…', SCOPE, { done: index + 1, count: eagerLoads.length },
                    )
                }
                const loaded = await app.openDeferredResource(listing)
                if (loaded) {
                    loadedStudies.push(loaded)
                }
            }
            // Open the first item that did not fail to load, loading it if it is still a listing. The rest stay in the
            // dataset sidebar.
            const first = viewerDatasets[0]?.resources.find(ctx => ctx.resource.state !== 'error')?.resource
            if (first) {
                window.setTimeout(() => {
                    app.interface?.openResource(first)
                    openingStatus.value = null
                }, 250)
            } else {
                openingStatus.value = null
            }
            // Notify the active project and enabled plugins of the studies loaded up front: every member of a dataset
            // flagged to preload, within the eager load limit, and none of a dataset listed from metadata.
            await plugin.onStudiesReady?.(epic, loadedStudies)
            await pluginsPlugin.onStudiesReady?.(epic, loadedStudies)
        })
    }
    awaitApp()
})
</script>

<template>
    <main class="viewer-view">

        <wa-spinner v-if="loading" class="spinner"></wa-spinner>

        <wa-callout v-else-if="error" class="error" variant="danger">{{ error }}</wa-callout>

        <template v-else-if="totalItems || emptyMode">
            <!-- Epicurrents viewer mount point. -->
            <div
                class="viewer-container"
                :data-file-hashes="hashes.join(',')"
                id="epicurrents-viewer"
            ></div>
            <div v-if="openingStatus" class="viewer-opening" role="status">
                <wa-spinner></wa-spinner>
                <span>{{ openingStatus }}</span>
            </div>
            <!-- Project- or plugin-provided overlay panel (e.g. a teaching project's session panel). -->
            <component v-if="viewerPanel"
                :datasetId="datasetId"
                :is="viewerPanel"
                :sessionToken="sessionToken"
            ></component>
        </template>
    </main>

    <wa-dialog
        :label="t('Leave the viewer?', SCOPE)"
        :open="showLeavePrompt"
        @wa-hide.self="cancelLeave"
    >
        <p>
            {{
                t(
                    'Leaving this page ends the viewer session, closing any open recordings and losing unsaved changes. Leave anyway?',
                    SCOPE,
                )
            }}
        </p>
        <div slot="footer" class="form-actions">
            <wa-button appearance="filled-outlined" variant="neutral" @click="cancelLeave">
                {{ t('Stay in the viewer', SCOPE) }}
            </wa-button>
            <wa-button appearance="filled-outlined" variant="danger" @click="confirmLeave">
                {{ t('Leave anyway', SCOPE) }}
            </wa-button>
        </div>
    </wa-dialog>
</template>

<style scoped>
.viewer-view {
    display: flex;
    flex-direction: column;
    height: 100vh;
    overflow: hidden;
    padding: 0;
    position: relative;
}

.spinner {
    display: block;
    margin: 3rem auto;
    font-size: 2rem;
}

.error {
    align-self: center;
    margin: 3rem 1rem;
}

/* Over the viewer while it lists and loads a set, and never in the way of it: the viewer's own interface is live
   underneath, and the status goes once the first item opens. */
.viewer-opening {
    align-items: center;
    background: var(--wa-color-surface-raised);
    border: var(--wa-border-width-s) solid var(--wa-color-surface-border);
    border-radius: var(--wa-border-radius-m);
    box-shadow: var(--wa-shadow-m);
    color: var(--wa-color-text-normal);
    display: flex;
    gap: var(--wa-space-s);
    left: 50%;
    padding: var(--wa-space-s) var(--wa-space-m);
    pointer-events: none;
    position: absolute;
    top: 40%;
    transform: translateX(-50%);
    z-index: 10;
}

.viewer-container {
    flex: 1;
    position: relative;
    overflow: hidden;
    background: var(--wa-color-neutral-950, #030712);
}
</style>
