"""Non-API URL patterns for the *dicom* plugin.

Mounted at ``/plugin/dicom/`` by the extended URL loader in
``epicurrents/urls.py``. These patterns serve the OHIF viewer SPA and are
intentionally kept separate from the Django Ninja API patterns in ``urls.py``
so the API and the SPA can evolve independently.

Routes
------
GET  /plugin/dicom/                  Redirect to the study list in the platform SPA.
GET  /plugin/dicom/ohif/             OHIF's study list (index.html), searching the plugin's DICOMweb surface.
GET  /plugin/dicom/ohif/<subpath>    OHIF viewer assets and client-side routes.

The OHIF build is made for this mount (``PUBLIC_URL`` in
``scripts/build_ohif.sh``, ``routerBasename`` in ``ohif-app-config.js``); moving
it means rebuilding the viewer.
"""

from django.http import HttpResponseRedirect
from django.urls import path, re_path

from plugins.dicom.views import ohif_viewer


def _redirect_to_studies(request):
    return HttpResponseRedirect("/dicom/studies")


urlpatterns = [
    path("", _redirect_to_studies, name="dicom-root"),
    # Any sub-path is passed through so OHIF's own React Router routes
    # (/plugin/dicom/ohif/viewer/dicomjson?url=…) reach index.html.
    path("ohif/", ohif_viewer, {"subpath": ""}, name="dicom-viewer"),
    re_path(r"^ohif/(?P<subpath>.+)$", ohif_viewer, name="dicom-viewer-asset"),
]
