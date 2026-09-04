import { createRouter, createWebHistory } from 'vue-router'
import { t } from '#i18n'
import { authGuard } from './guard'
import { plugin } from '#projects/active'
import { plugin as pluginsPlugin } from '#plugins/active'
import AnnotationExportView from '#views/AnnotationExportView.vue'
import CollectionView from '#views/CollectionView.vue'
import DatasetView from '#views/DatasetView.vue'
import DatasetsView from '#views/DatasetsView.vue'
import HomeView from '#views/HomeView.vue'
import LibraryView from '#views/LibraryView.vue'
import LoginView from '#views/LoginView.vue'
import NeedsAttentionView from '#views/NeedsAttentionView.vue'
import ProfileView from '#views/ProfileView.vue'
import ResetPasswordView from '#views/ResetPasswordView.vue'
import UnassignedRecordingsView from '#views/UnassignedRecordingsView.vue'
import UploadView from '#views/UploadView.vue'
import ViewerConfigView from '#views/ViewerConfigView.vue'
import ViewerView from '#views/ViewerView.vue'

const TITLE_SCOPE = 'Router'

/**
 * Application router configuration.
 *
 * Keeps route definitions in one place so views can be expanded incrementally.
 */
export const router = createRouter({
    history: createWebHistory(import.meta.env.VITE_BASE_URL),
    routes: [
        {
            path: '/',
            name: 'home',
            component: HomeView,
            meta: {
                navSection: 'home',
                requiresAuth: true,
                title: 'Home',
            },
        },
        {
            path: '/login',
            name: 'login',
            component: LoginView,
            meta: {
                title: 'Sign in',
            },
        },
        {
            path: '/profile',
            name: 'profile',
            component: ProfileView,
            meta: {
                navSection: 'profile',
                requiresAuth: true,
                title: 'Profile',
            },
        },
        {
            path: '/reset-password',
            name: 'reset-password',
            component: ResetPasswordView,
            meta: {
                title: 'Reset password',
            },
        },
        {
            path: '/upload',
            name: 'upload',
            component: UploadView,
            meta: {
                navSection: 'home',
                requiresAuth: true,
                title: 'Upload',
            },
        },
        {
            path: '/viewer',
            name: 'viewer',
            component: ViewerView,
            meta: {
                navSection: 'home',
                requiresAuthUnlessToken: true,
                title: 'Viewer',
            },
        },
        {
            path: '/library',
            name: 'library',
            component: LibraryView,
            meta: {
                navSection: 'library',
                requiresAuth: true,
                title: 'Library',
            },
        },
        {
            path: '/library/collections/:id',
            name: 'collection',
            component: CollectionView,
            meta: {
                navSection: 'library',
                requiresAuth: true,
                // CollectionView swaps in the collection name once loaded.
                title: 'Collection',
            },
        },
        {
            path: '/library/unassigned',
            name: 'unassigned-recordings',
            component: UnassignedRecordingsView,
            meta: {
                navSection: 'library',
                requiresAuth: true,
                title: 'Unassigned recordings',
            },
        },
        {
            path: '/library/attention',
            name: 'needs-attention',
            component: NeedsAttentionView,
            meta: {
                navSection: 'library',
                requiresAuth: true,
                title: 'Needs attention',
            },
        },
        {
            path: '/datasets',
            name: 'datasets',
            component: DatasetsView,
            meta: {
                navSection: 'datasets',
                requiresAuth: true,
                title: 'Datasets',
            },
        },
        {
            path: '/datasets/:id',
            name: 'dataset',
            component: DatasetView,
            meta: {
                navSection: 'datasets',
                requiresAuth: true,
                // DatasetView swaps in the dataset name once loaded.
                title: 'Dataset',
            },
        },
        {
            path: '/settings/viewer',
            name: 'viewer-config',
            component: ViewerConfigView,
            meta: {
                navSection: 'viewer-config',
                requiresAuth: true,
                requiresStaff: true,
                title: 'Viewer settings',
            },
        },
        {
            path: '/annotations/export',
            name: 'annotation-export',
            component: AnnotationExportView,
            meta: {
                navSection: 'annotation-export',
                requiresAuth: true,
                requiresStaff: true,
                title: 'Export annotations',
            },
        },
        ...(plugin.routes ?? []),
        ...(pluginsPlugin.routes ?? []),
    ],
})

router.beforeEach(authGuard)

/**
 * Set the browser tab title to `"<label> - Epicurrents"`, or just
 * `"Epicurrents"` when `label` is empty. Views that show a named resource
 * (CollectionView, DatasetView) call this once their data has loaded to
 * swap in the actual name; the afterEach hook below sets the static
 * route-level label first.
 */
export function setPageTitle (label: string) {
    document.title = label ? `${label} - Epicurrents` : 'Epicurrents'
}

router.afterEach((to) => {
    const key = (to.meta.title as string | undefined) ?? ''
    setPageTitle(key ? t(key, TITLE_SCOPE) : '')
})
