import type { ViewerPlugin } from '../types'
import icons from './icons'
import { dicomViewerItems } from './viewerItems'

/**
 * Viewer plugin for the *dicom* plugin (DICOM file management and OHIF viewer).
 *
 * Routes
 * ------
 * /dicom/studies     — Study list with upload and delete (all authenticated users).
 *                      Opening a study opens the viewer on it in a new tab.
 *
 * Viewer integration
 * ------------------
 * The OHIF viewer is served at /plugin/dicom/ohif/ (a Django view in
 * plugins/dicom/views.py), standalone and framed by the viewer edition's
 * radiology view. `viewerItems` lists a study in the viewer through the
 * `rad/viewer-url` importer with the address of its OHIF page; a viewer URL
 * names studies with `?dicom=<hash>` (see viewerItems.ts).
 *
 * OHIF's ``dicomjson`` data source (plugins/dicom/ohif-app-config.js) loads the
 * study from the platform's /plugin/dicom/api/v1/dicom/studies/{hash}/ohif-json/
 * endpoint, whose instance URLs point at the WADO-URI endpoint
 * /plugin/dicom/api/v1/dicom/wado/.
 */
export const plugin: ViewerPlugin = {
    icons,

    viewerItems: [dicomViewerItems],

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
