/**
 * OHIF runtime configuration for the dicom plugin's standalone viewer.
 *
 * scripts/build_ohif.sh copies this file over the built dist's app-config.js, so the OHIF build itself stays
 * the upstream one and this file is the only place the platform configures it. The viewer is mounted at
 * /plugin/dicom/ohif/, and its routes are /<mode>/<data source>: `viewer` is the longitudinal mode.
 *
 * Two data sources, both same-origin, both carrying the session cookie:
 *
 * - `platform` (the default) is the plugin's DICOMweb surface. Its QIDO-RS search is the study list at the bare
 *   mount, listing the studies the caller may list, and a study opens at /plugin/dicom/ohif/viewer?StudyInstanceUIDs=…
 *   Metadata comes from WADO-RS, pixels from the WADO-URI endpoint (`imageRendering: 'wadouri'`).
 * - `dicomjson` opens one study by hash from the platform's study list:
 *   /plugin/dicom/ohif/viewer/dicomjson?url=<ohif-json endpoint>. It accepts any origin in `?url=` when OHIF has no
 *   authentication of its own, which is the case here; the platform's `connect-src 'self'` is what refuses a link
 *   pointing it elsewhere.
 */
window.config = {
  name: 'epicurrents',
  routerBasename: '/plugin/dicom/ohif',
  extensions: [],
  modes: [],
  customizationService: {},
  showStudyList: true,
  maxNumberOfWebWorkers: 3,
  // Warns when the page is not cross-origin isolated, which is the visible sign of a missing
  // ENABLE_CROSS_ORIGIN_ISOLATION (see the plugin README).
  showWarningMessageForCrossOrigin: true,
  showCPUFallbackMessage: true,
  showLoadingIndicator: true,
  strictZSpacingForVolumeViewport: true,
  showErrorDetails: 'dev',
  defaultDataSourceName: 'platform',
  dataSources: [
    {
      namespace: '@ohif/extension-default.dataSourcesModule.dicomweb',
      sourceName: 'platform',
      configuration: {
        friendlyName: 'Epicurrents',
        name: 'platform',
        qidoRoot: '/plugin/dicom/api/v1/dicomweb',
        wadoRoot: '/plugin/dicom/api/v1/dicomweb',
        wadoUriRoot: '/plugin/dicom/api/v1/dicom/wado/',
        imageRendering: 'wadouri',
        thumbnailRendering: 'wadouri',
        // The search answers a fixed attribute set and ignores includefield.
        qidoSupportsIncludeField: false,
        enableStudyLazyLoad: true,
        supportsFuzzyMatching: false,
        supportsWildcard: true,
        supportsReject: false,
        supportsStow: false,
        dicomUploadEnabled: false,
        omitQuotationForMultipartRequest: true,
      },
    },
    {
      namespace: '@ohif/extension-default.dataSourcesModule.dicomjson',
      sourceName: 'dicomjson',
      configuration: {
        friendlyName: 'Epicurrents DICOM',
        name: 'json',
      },
    },
  ],
  // OHIF calls this on a failed image request and throws when it is absent, which masks the
  // original failure behind a TypeError in the console.
  httpErrorHandler: error => {
    console.warn('Epicurrents DICOM request failed', error?.status);
  },
};
