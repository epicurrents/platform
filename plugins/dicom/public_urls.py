"""Non-API URL patterns for the *dicom* project.

Mounted at ``/plugin/dicom/`` by the extended URL loader in
``epicurrents/urls.py``. These patterns serve the OHIF viewer SPA and are
intentionally kept separate from the Django Ninja API patterns in ``urls.py``
so the API and the SPA can evolve independently.

Routes
------
GET  /plugin/dicom/                  Redirect to the viewer.
GET  /plugin/dicom/viewer/           OHIF viewer index.html.
GET  /plugin/dicom/viewer/<subpath>  OHIF viewer assets and sub-routes.
"""

from django.http import HttpResponseRedirect
from django.urls import path, re_path

from plugins.dicom.views import ohif_viewer


def _redirect_to_viewer(request):
    return HttpResponseRedirect("/plugin/dicom/viewer/")


urlpatterns = [
    # Redirect bare /plugin/dicom/ to the viewer.
    path("", _redirect_to_viewer, name="dicom-root"),
    # Serve the OHIF viewer SPA. Any sub-path is passed through so that
    # OHIF's own React Router routes (e.g. /viewer/studyUID) work correctly.
    path("viewer/", ohif_viewer, {"subpath": ""}, name="dicom-viewer"),
    re_path(r"^viewer/(?P<subpath>.+)$", ohif_viewer, name="dicom-viewer-asset"),
]
