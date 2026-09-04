"""Non-API Django views for the *dicom* plugin.

``ohif_viewer``
    Serves the built OHIF viewer SPA. The viewer is a React application that
    must be built from ``plugins/dicom/ohif-viewer/`` (the git submodule) via
    ``scripts/build_ohif.sh`` and whose ``dist/`` output is expected at
    ``DICOM_OHIF_DIST_PATH``.

OHIF's WASM-based decoders (JPEG-LS, JPEG 2000, HTJ2K) need ``SharedArrayBuffer``,
which requires the COOP/COEP/CORP triple on responses. Those headers are set
platform-wide by ``epicurrents.middleware.CrossOriginIsolationMiddleware`` when
``ENABLE_CROSS_ORIGIN_ISOLATION`` is true. Deployments enabling this plugin must
enable that setting — see ``plugins/dicom/README.md``.
"""

import mimetypes
import os

from django.conf import settings
from django.core.exceptions import SuspiciousFileOperation
from django.http import FileResponse, Http404, HttpResponse
from django.utils._os import safe_join


def _ohif_dist_path() -> str:
    return getattr(
        settings,
        "DICOM_OHIF_DIST_PATH",
        os.path.join(os.path.dirname(__file__), "ohif-dist"),
    )


def ohif_viewer(request, subpath: str = ""):
    """Serve the OHIF viewer SPA.

    Any sub-path that doesn't resolve to a real file falls back to
    ``index.html`` so that the React Router's client-side routing works.
    """
    dist = _ohif_dist_path()
    if not os.path.isdir(dist):
        return HttpResponse(
            "OHIF viewer has not been built yet. Run scripts/build_ohif.sh to build it.",
            status=503,
        )

    # Attempt to serve a real file first.
    if subpath:
        try:
            candidate = safe_join(dist, subpath)
        except SuspiciousFileOperation:
            candidate = None
        if candidate and os.path.isfile(candidate):
            mime, _ = mimetypes.guess_type(candidate)
            return FileResponse(open(candidate, "rb"), content_type=mime or "application/octet-stream")

    # Fall back to index.html (SPA entry point).
    index_path = os.path.join(dist, "index.html")
    if not os.path.isfile(index_path):
        raise Http404("OHIF viewer dist/index.html not found.")

    return FileResponse(open(index_path, "rb"), content_type="text/html")
