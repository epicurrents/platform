import type { ViewerPlugin } from '../types'
import icons from './icons'

/**
 * Viewer plugin for the *dicom* plugin (DICOM file management and OHIF viewer).
 *
 * Routes
 * ------
 * /dicom/studies     — Study list with upload and delete (all authenticated users).
 *                      Opening a study launches the OHIF viewer in a new tab.
 *
 * Viewer integration
 * ------------------
 * The OHIF viewer is served as a standalone SPA at /plugin/dicom/viewer/ (a
 * Django view in plugins/dicom/views.py).  The Vue plugin opens it in a new
 * browser tab rather than embedding it in an iframe, avoiding cross-origin
 * header complications and keeping the OHIF experience full-screen.
 *
 * OHIF is configured via its app-config.js to use the platform's
 * /plugin/dicom/api/v1/dicom/studies/{hash}/ohif-json/ endpoint as its
 * ``dicomjson`` datasource and /plugin/dicom/api/v1/dicom/wado/ as its WADO-URI
 * endpoint.
 */
export const plugin: ViewerPlugin = {
    icons,

    navLinks: [
        {
            id: 'dicom-studies',
            section: 'dicom',
            label: 'DICOM Studies',
            to: { name: 'dicom-studies' },
            icon: 'dicom',
            order: 10,
        },
    ],

    routes: [
        {
            path: '/dicom/studies',
            name: 'dicom-studies',
            // Lazy-loaded so the view (and its styles) live in their own chunk,
            // fetched only when the route is visited.
            component: () => import('./DicomStudiesView.vue'),
            meta: { requiresAuth: true, navSection: 'dicom', title: 'DICOM Studies' },
        },
    ],
}
