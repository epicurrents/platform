<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useRoute } from 'vue-router'
import { t } from '#i18n'
import { showToast } from '#lib/toast'
import { useAuthStore } from '#stores/auth'
import { getRecordingDetail, recordingName } from '#api/recordings'
import { getDataset, listDatasetItems } from '#api/library'
import { getMediaDetail, listMediaFiles, type MediaFileDetail, type MediaFileSummary } from '#api/media'
import type {
    BiosignalResource,
    DataResource,
    EpicurrentsApp,
    EpicurrentsGlobal,
    VideoAttachment,
} from '#epicurrents/core/dist/types'
// viewer/interface has no compiled .d.ts output; declare only what we need here.
type InterfaceSettings = { app: { disclaimerAccepted: number } }
import type { Recording } from '#api/recordings'
import { plugin } from '#projects/active'
import { plugin as pluginsPlugin } from '#plugins/active'
import { waitForEventBus } from '#projects/eventBus'
import { getViewerConfig } from '#api/viewerConfig'
import { applyViewerSettingsOverrides, VIEWER_USER_SETTINGS_PATH } from '#lib/viewerConfig'

// The overlay panel comes from the active project if it defines one, otherwise
// from the first enabled plugin that does. A page hosts a single overlay.
const viewerPanel = plugin.viewerPanel ?? pluginsPlugin.viewerPanel

/** One viewer-side dataset to create plus the items to load into it. */
interface DatasetBundle {
    name: string
    shareToken: string | undefined
    items: BundleItem[]
}

/**
 * Per-item payload after the platform-side fetch. Discriminated so the
 * mount loop can pick the right importer + URL without re-querying the
 * list endpoint's metadata.
 */
type BundleItem =
    | { kind: 'recording'; recording: Recording }
    | { kind: 'media'; media: MediaFileDetail }

/**
 * Map a media file's extension to one of the viewer's pre-registered
 * document importers (default setup, viewer/interface/src/setups/default.ts).
 * The platform always loads media by URL (the file bytes live behind
 * /media/api/v1/<hash>/file) so we dispatch to the ``-url`` importer
 * variant rather than the ``-file`` one — the latter expects a File
 * object from a picker and silently misinterprets URL strings as
 * asset-relative filenames, which manifests as ``/assets/undefined``
 * requests against the SPA host. Returns null when the format is not
 * yet wired into the viewer — the caller skips the item rather than
 * failing the whole dataset load.
 */
function importerForMedia(media: MediaFileDetail): string | null {
    const ext = (media.file_extension || '').toLowerCase()
    if (ext === '.pdf') return 'doc/pdf-url'
    if (ext === '.md') return 'doc/htm-url'
    return null
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

const bundles = ref<DatasetBundle[]>([])
const loading = ref(true)
const error = ref<string | null>(null)

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

async function loadDatasetBundle(id: number | string, token: string | undefined): Promise<DatasetBundle> {
    const [dataset, items] = await Promise.all([
        getDataset(id, token),
        listDatasetItems(id, undefined, token),
    ])
    // Preserve dataset order; resolve each item to its detail row (or null
    // when the item type / state is not viewable). Unsupported media files
    // are dropped here so the mount loop never tries to load them.
    const bundleItems = (await Promise.all(
        items.map(async (item): Promise<BundleItem | null> => {
            if (!item.object_hash) {
                return null
            }
            if (item.object_type === 'recording') {
                const rec = await getRecordingDetail(item.object_hash, token)
                return { kind: 'recording', recording: rec }
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
    return { name: dataset.name, shareToken: token, items: bundleItems }
}

onMounted(async () => {
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
            bundles.value = await Promise.all(
                sessionDatasets.map(d => loadDatasetBundle(d.id, d.share_token)),
            )
        } else if (datasetId.value !== null) {
            // Single-dataset mode.
            bundles.value = [await loadDatasetBundle(datasetId.value, shareToken.value)]
        } else if (hashes.value.length || mediaHashes.value.length) {
            // Individual items mode — one anonymous bundle that mixes
            // recordings (`?files=`) and media (`?media=`) in the order they
            // were declared in the URL.
            const recordingItems = await Promise.all(
                hashes.value.map(async h => ({
                    kind: 'recording' as const,
                    recording: await getRecordingDetail(h, shareToken.value),
                })),
            )
            const mediaItems = await Promise.all(
                mediaHashes.value.map(async h => ({
                    kind: 'media' as const,
                    media: await getMediaDetail(h),
                })),
            )
            bundles.value = [{
                name: 'Dataset',
                shareToken: shareToken.value,
                items: [...recordingItems, ...mediaItems],
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

    // Poll until the Epicurrents global is available, then mount.
    let retries = 0
    const awaitApp = async () => {
        if (typeof Epicurrents === 'undefined') {
            if (retries < 400) {
                retries++
                console.debug(`Waiting for Epicurrents to be available (retry ${retries})...`)
                setTimeout(awaitApp, 1000)
            }
            return
        }
        const setup = Object.assign(
            new Object(null),
            {
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
                // share-token or anonymous viewer would collect 401s for no benefit.
                userSettingsBackend: auth.user ? VIEWER_USER_SETTINGS_PATH : '',
            },
            // Plugins first, then the active project — the project has the last
            // word on key conflicts, matching the backend's plugins < project
            // settings precedence.
            pluginsPlugin.extraSetup ?? {},
            plugin.extraSetup ?? {},
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
                    try {
                        const ds = await getDataset(datasetId.value, shareToken.value)
                        overrides = { ...overrides, ...ds.viewer_config }
                    } catch (err) {
                        console.warn('[viewer-config] could not load dataset overrides:', err)
                    }
                }
                applyViewerSettingsOverrides(epic, overrides)
            } catch (err) {
                console.warn('[viewer-config] could not load deployment overrides:', err)
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
                    // Runtime exposes setActiveResource at runtime but the
                    // RuntimeState interface does not surface it; narrow cast.
                    ;(epic.runtime as unknown as {
                        setActiveResource: (r: DataResource) => void
                    }).setActiveResource(first)
                }
            })

            let firstRec = null as DataResource | null
            const loadedStudies: DataResource[] = []
            for (const [bundleIdx, bundle] of bundles.value.entries()) {
                // Only the first dataset is marked active; later ones must not
                // override the active selection or the UI ends up in an
                // inconsistent state on mount.
                const viewerDataset = epic?.createDataset(bundle.name, bundleIdx === 0)
                for (const item of bundle.items) {
                    try {
                        let importerName: string | null = null
                        let downloadUrl = ''
                        let displayName = ''

                        if (item.kind === 'recording') {
                            const rec = item.recording
                            // Build an absolute URL — Workers (used for Range fetches on the SAB path) have no
                            // document.baseURI, so a relative path throws "Failed to parse URL" inside the
                            // EDF reader worker. Main-thread fetches would resolve it; resolve here so both
                            // paths see the same string.
                            const relativePath = bundle.shareToken
                                ? `/recordings/api/v1/${rec.hash}/file?share_token=${encodeURIComponent(bundle.shareToken)}`
                                : `/recordings/api/v1/${rec.hash}/file`
                            downloadUrl = new URL(relativePath, import.meta.url).toString()
                            displayName = recordingName(rec).replace(/\.[^.]+$/, '')
                            // Lower-case the extension before lookup — loader names are registered as
                            // ``eeg/edf-file`` etc., and a recording uploaded with ``.EDF`` (whose
                            // ``file_extension`` was previously stored verbatim) would otherwise produce
                            // ``eeg/EDF-file`` and fail to resolve. Defensive even after the upload-side
                            // fix lands, since existing rows may still carry an uppercase extension.
                            const normalizedExt = rec.file_extension.replace(/^\./, '').toLowerCase()
                            importerName = `${rec.modality}/${normalizedExt}-file`
                        } else {
                            const media = item.media
                            importerName = importerForMedia(media)
                            if (!importerName) {
                                console.warn(`[Epicurrents] No importer registered for media ${media.file_extension}; skipping ${media.content_hash}`)
                                continue
                            }
                            // Media download endpoint is session-authenticated — the
                            // browser sends cookies on the worker fetch. Share-token
                            // access for media is a phase-4 follow-up; until then
                            // anonymous dataset shares won't render media items.
                            //
                            // The relative path is bound to a local before the
                            // ``new URL(...)`` call because Vite's minifier mis-handles
                            // an inline template literal here and emits
                            // ``new URL(Object.assign({})[`/media/...`], ...)``,
                            // which evaluates to ``new URL(undefined, ...)`` and
                            // resolves to ``/assets/undefined``. Recording's branch
                            // sidesteps it by binding ``relativePath`` first; we
                            // match that shape for the same reason.
                            const mediaRelativePath = `/media/api/v1/${media.content_hash}/file`
                            downloadUrl = new URL(mediaRelativePath, import.meta.url).toString()
                            displayName = media.display_name
                        }
                        const study = await epic?.loadStudy(
                            importerName,
                            downloadUrl,
                            { dataset: viewerDataset, name: displayName, isValidated: true },
                        )
                        if (study) {
                            loadedStudies.push(study)
                            if (!firstRec) firstRec = study
                            if (item.kind === 'recording') {
                                seedTrustedInterruptions(study, item.recording)
                                await attachRecordingVideos(study, item.recording.hash)
                            }
                        }
                        console.debug(`[Epicurrents] Study ${study!.name} successfully loaded.`)
                    } catch (e) {
                        const hash = item.kind === 'recording' ? item.recording.hash : item.media.content_hash
                        console.error(`[Epicurrents] Failed to load study from hash ${hash}.`)
                        console.error(e)
                    }
                }
            }
            if (firstRec) {
                // Open the first recording in the viewer. The rest will be available in the dataset sidebar.
                window.setTimeout(() => {
                    epic?.interface?.openResource(firstRec)
                }, 250)
            }
            // Notify the active project and enabled plugins that all studies are loaded and the first is open.
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

        <wa-callout v-else-if="error" variant="danger">{{ error }}</wa-callout>

        <template v-else-if="totalItems || emptyMode">
            <!-- Epicurrents viewer mount point. -->
            <div
                class="viewer-container"
                :data-file-hashes="hashes.join(',')"
                id="epicurrents-viewer"
            ></div>
            <!-- Project- or plugin-provided overlay panel (e.g. a teaching project's session panel). -->
            <component v-if="viewerPanel"
                :datasetId="datasetId"
                :is="viewerPanel"
                :sessionToken="sessionToken"
            ></component>
        </template>
    </main>
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

.viewer-container {
    flex: 1;
    position: relative;
    overflow: hidden;
    background: var(--wa-color-neutral-950, #030712);
}
</style>
