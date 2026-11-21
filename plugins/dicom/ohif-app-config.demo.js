/**
 * Development overlay for the dicom plugin's OHIF configuration: OHIF's public demo server as an extra data source.
 *
 * plugins/dicom/views.py appends this file to app-config.js only when DICOM_OHIF_DEMO_DATA is set and DEBUG is on,
 * so a production deployment never serves it. It exists so a developer can open CT, MR, PET, SEG, SR and the other
 * study types OHIF handles without having radiology data of their own; the demo studies are public sample data and
 * the requests to the demo server carry no platform cookie or data. The view adds the demo origin to the OHIF
 * responses' connect-src for the same requests.
 *
 * The demo study list is at /plugin/dicom/ohif/?datasources=demo; the bare mount stays the platform's own list.
 */
(function () {
  const DEMO_ROOT = 'https://d14fa38qiwhyfd.cloudfront.net/dicomweb';
  window.config.dataSources.push({
    namespace: '@ohif/extension-default.dataSourcesModule.dicomweb',
    sourceName: 'demo',
    configuration: {
      friendlyName: 'OHIF public demo server (development only)',
      name: 'aws',
      wadoUriRoot: DEMO_ROOT,
      qidoRoot: DEMO_ROOT,
      wadoRoot: DEMO_ROOT,
      qidoSupportsIncludeField: false,
      imageRendering: 'wadors',
      thumbnailRendering: 'thumbnail',
      thumbnailRequestStrategy: 'fetch',
      enableStudyLazyLoad: true,
      supportsFuzzyMatching: false,
      supportsWildcard: true,
      staticWado: true,
      singlepart: 'bulkdata,video',
      bulkDataURI: {
        enabled: true,
        relativeResolution: 'studies',
      },
      omitQuotationForMultipartRequest: true,
    },
  });
})();
