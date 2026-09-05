<script setup lang="ts">
import { computed, onMounted, onUnmounted } from 'vue'
import { RouterLink, RouterView, useRoute, useRouter } from 'vue-router'
import ToastStack from '#root/viewer/interface/src/app/ToastStack.vue'
import AppLogo from '#components/AppLogo.vue'
import { t } from '#i18n'
import { showToast } from '#lib/toast'
import { getViewerSetup, setViewerSetup } from '#lib/viewerGlobal'
import type { EpicurrentsGlobal } from '#epicurrents/core/dist/types'
import { plugin } from '#projects/active'
import { plugin as pluginsPlugin } from '#plugins/active'
import type { ProjectNavLink } from '#projects/types'
import { useAuthStore } from '#stores/auth'
import { useDeploymentStore } from '#stores/deployment'
import { useThemeStore, type ThemeMode } from '#stores/theme'

const SCOPE = 'App'

const authStore = useAuthStore()
const deploymentStore = useDeploymentStore()
const themeStore = useThemeStore()
const route = useRoute()
const router = useRouter()

// Bridge the viewer's callouts into the platform's showToast stack.
//
// The viewer ships as a UMD bundle that embeds its own `scoped-event-log`
// singleton. A `Log.addEventListener` call from here would subscribe on a
// *different* listener registry than the one the viewer dispatches into,
// so it would never fire. The callback hop is the rendezvous point between
// the two singletons — the viewer's interface/App.vue calls it when an
// announce event arrives in its bundle.
//
// Lives under `window.__EPICURRENTS__.announce` so the host-callback contract
// shares the existing viewer namespace; future host hooks (e.g. `confirm`,
// `progress`) belong on the same object.
onMounted(() => {
    deploymentStore.init()
    if (typeof window.__EPICURRENTS__ === 'undefined') {
        // The Epicurrents constructor seeds these fields on viewer startup,
        // but the platform App.vue mounts before any viewer instance exists.
        // Pre-seed defensively so writing `.announce` doesn't throw.
        window.__EPICURRENTS__ = {
            APP: null,
            EVENT_BUS: null,
            RUNTIME: null,
            SETUP: {} as EpicurrentsGlobal['SETUP'],
        }
    }
    // Serve Pyodide's runtime (wasm, stdlib, packages) from our own origin
    // rather than the jsdelivr CDN, so the installed app's compute works offline
    // and the assets are same-origin cacheable by the service worker. Overrides
    // the viewer's SETUP default, which is empty and means "use the interpreter
    // service's own pinned upstream distribution"; the pinned "full" distribution
    // is vendored at deploy under /vendor/pyodide/314.0.2/. A value here also
    // makes every package resolve from that folder's pyodide-lock.json, which the
    // vendoring step extends with mne.
    // Merged (not replaced) so a viewer-seeded SETUP keeps its other fields.
    setViewerSetup({
        ...getViewerSetup(),
        pyodideAssetPath: '/vendor/pyodide/314.0.2/',
    })
    window.__EPICURRENTS__.announce = (message, variant) => {
        showToast(message, variant)
    }
})

onUnmounted(() => {
    // Clear the slot on hot-reload / teardown so a stale closure can't leak
    // a reference to a destroyed component instance.
    if (window.__EPICURRENTS__) {
        window.__EPICURRENTS__.announce = undefined
    }
})

const THEME_OPTIONS: { value: ThemeMode; icon: string; label: string }[] = [
    { value: 'light',  icon: 'sun',     label: 'Light'  },
    { value: 'dark',   icon: 'moon',    label: 'Dark'   },
    { value: 'auto',   icon: 'display', label: 'System' },
]

const themeIcon = computed(() =>
    THEME_OPTIONS.find(o => o.value === themeStore.mode)?.icon ?? 'display'
)

const baseNavLinks: ProjectNavLink[] = [
    {
        id: 'home',
        icon: 'home',
        label: 'Home',
        order: 10,
        section: 'home',
        to: '/',
    },
    {
        id: 'library',
        icon: 'folders',
        label: 'Library',
        order: 20,
        section: 'library',
        to: '/library',
    },
    {
        id: 'datasets',
        icon: 'database',
        label: 'Datasets',
        order: 30,
        section: 'datasets',
        to: '/datasets',
    },
    {
        id: 'annotation-export',
        icon: 'download',
        label: 'Export annotations',
        order: 85,
        requiresStaff: true,
        section: 'annotation-export',
        to: '/annotations/export',
    },
    {
        id: 'viewer-config',
        icon: 'sliders',
        label: 'Viewer settings',
        order: 90,
        requiresStaff: true,
        section: 'viewer-config',
        to: '/settings/viewer',
    },
]

const activeSection = computed(() => {
    return String(route.meta.navSection ?? '')
})

const navLinks = computed(() => {
    const merged = [...baseNavLinks, ...(plugin.navLinks ?? []), ...(pluginsPlugin.navLinks ?? [])]
    return merged
        .filter(link => {
            if (link.requiresSuperuser && !authStore.isSuperuser) return false
            if (link.requiresStaff && !authStore.isStaff) return false
            return true
        })
        .sort((a, b) => {
            const aOrder = a.order ?? 100
            const bOrder = b.order ?? 100
            return aOrder - bOrder
        })
})

const showNavigation = computed(() => {
    return authStore.isAuthenticated && route.name !== 'viewer' && !route.meta.fullscreen
})

const profileName = computed(() => {
    const firstName = authStore.user?.first_name ?? ''
    const lastName = authStore.user?.last_name ?? ''
    return `${firstName} ${lastName}`.trim()
})

function navLinkClass (section: string) {
    if (activeSection.value === section) {
        return 'nav-link active'
    }
    return 'nav-link'
}

async function logout () {
    await authStore.logout()
    router.push({ name: 'login' })
}
</script>

<template>
    <nav v-if="showNavigation" class="app-nav">
        <RouterLink class="nav-brand" to="/">
            <AppLogo class="nav-brand__logo" :stroke-width="12" />
            Epicurrents
        </RouterLink>
        <div class="nav-links">
            <RouterLink v-for="link in navLinks"
                :key="link.id"
                :class="navLinkClass(link.section)"
                :to="link.to"
            >
                <wa-icon v-if="link.icon"
                    class="nav-icon"
                    :name="link.icon"
                ></wa-icon>
                {{ t(link.label, SCOPE) }}
            </RouterLink>
        </div>
        <div class="nav-end">
            <!-- Theme picker -->
            <wa-dropdown>
                <wa-button
                    appearance="plain"
                    slot="trigger"
                    size="s"
                    title="Color theme"
                >
                    <wa-icon :name="themeIcon"></wa-icon>
                </wa-button>
                <wa-dropdown-item
                    v-for="opt in THEME_OPTIONS"
                    :key="opt.value"
                    :checked="themeStore.mode === opt.value"
                    type="checkbox"
                    @click="themeStore.setMode(opt.value)"
                >
                    <wa-icon :name="opt.icon" slot="prefix"></wa-icon>
                    {{ opt.label }}
                </wa-dropdown-item>
            </wa-dropdown>
            <RouterLink :class="navLinkClass('profile')" to="/profile">
                <wa-icon class="nav-icon" name="user"></wa-icon>
                {{ profileName }}
            </RouterLink>
            <wa-button
                appearance="filled-outlined"
                size="s"
                variant="text"
                @click="logout"
            >
                {{ t('Sign out', SCOPE) }}
            </wa-button>
        </div>
    </nav>

    <div class="route-view-wrapper">
        <RouterView />
    </div>

    <!-- Global toast stack — backed by the reactive `toasts` array in lib/toast.ts. -->
    <ToastStack icon-library="default" />

    <!--
        Dev-mode banner. Sourced from /api/v1/health.mode; visible only when
        the backend reports DJANGO_MODE=development. Pinned to the bottom of
        the viewport so it stays in sight without dominating the layout.
    -->
    <div v-if="deploymentStore.isDevelopmentMode" class="dev-mode-banner">
        {{ t('Dev mode — not for production data', SCOPE) }}
    </div>
</template>

<style scoped>
.nav-icon {
    /* 0.9em rendered the toolbar icons noticeably smaller than the
     * adjacent text caps — particularly the profile-link icon, which
     * fell below the readable / click-target threshold. Brought up to
     * match the surrounding text size (with a slight bump for
     * legibility). */
    font-size: 1.1em;
    margin-right: 0.35em;
    vertical-align: -0.15em;
}

/* Fills the remaining viewport height so full-bleed layout views
   (e.g. CourseLayout with wa-page) get a real height to work with.
   Standard .page-view children already use flex: 1 so they are unaffected. */
.route-view-wrapper {
    display: flex;
    flex: 1;
    flex-direction: column;
    min-height: 0;
}

/* Dev-mode banner: pinned to the bottom of the viewport at 1.5rem max
   height. Uses the WebAwesome warning fill so the colour adapts to
   light / dark / system themes and stays consistent with other
   warning-coloured surfaces in the app. */
.dev-mode-banner {
    align-items: center;
    background: var(--wa-color-warning-fill-loud);
    bottom: 0;
    color: white;
    display: flex;
    font-size: 0.75rem;
    font-weight: 600;
    height: 1.5rem;
    justify-content: center;
    left: 0;
    letter-spacing: 0.02em;
    position: fixed;
    right: 0;
    text-transform: uppercase;
    z-index: 9999;
}
</style>
