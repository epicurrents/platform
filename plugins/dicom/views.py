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

The image loader's codec worker compiles code with ``new Function``, which the
platform's ``script-src`` refuses: every compressed study (JPEG 2000 and the
other WASM codecs) then fails to decode. Every response this view serves
therefore carries the platform policy with ``'unsafe-eval'`` added to
``script-src``; the security middleware sets its policy with ``setdefault``, so
the SPA and the API keep the strict one. The relaxation is scoped to the whole
OHIF mount rather than the worker because the worker is an unnamed numbered
chunk whose name changes with every build.

The viewer edition's radiology view frames this mount, so its responses also
allow framing by the platform's own pages: ``frame-ancestors 'self'`` in place
of whatever the platform policy names, and ``X-Frame-Options: SAMEORIGIN``,
which ``XFrameOptionsMiddleware`` leaves standing because the header is already
set. Every other response keeps ``frame-ancestors 'none'`` and ``DENY``.
"""

import mimetypes
import os
import posixpath

from django.conf import settings
from django.core.exceptions import SuspiciousFileOperation
from django.http import FileResponse, Http404, HttpResponse
from django.utils._os import safe_join

# Types the OHIF dist depends on that the platform's mimetypes table may lack:
# a .wasm module served as octet-stream fails WebAssembly.instantiateStreaming,
# and a .mjs worker served as anything but JavaScript is refused as a module.
_TYPES = {
    ".mjs": "text/javascript",
    ".wasm": "application/wasm",
}


# OHIF's public demo server, which ohif-app-config.demo.js adds as a data source.
DEMO_ORIGIN = "https://d14fa38qiwhyfd.cloudfront.net"
_DEMO_OVERLAY = os.path.join(os.path.dirname(__file__), "ohif-app-config.demo.js")


def demo_data_enabled() -> bool:
    """Return True when the development demo overlay is served: the setting on, and DEBUG on."""
    return bool(settings.DEBUG and getattr(settings, "DICOM_OHIF_DEMO_DATA", False))


def _extend(directives: list[str], name: str, source: str) -> None:
    """Add *source* to directive *name*, deriving the directive from default-src when the policy has none.

    Naming the directive keeps the addition from opening more than that one
    fetch type, which widening default-src would.
    """
    for i, directive in enumerate(directives):
        if directive.split()[0] == name:
            if source not in directive.split():
                directives[i] = f"{directive} {source}"
            return
    default = next((d for d in directives if d.split()[0] == "default-src"), "default-src 'self'")
    directives.append(f"{name} {' '.join(default.split()[1:])} {source}")


def _replace(directives: list[str], name: str, value: str) -> None:
    """Set directive *name* to *value*, replacing the policy's own sources for it."""
    for i, directive in enumerate(directives):
        if directive.split()[0] == name:
            directives[i] = f"{name} {value}"
            return
    directives.append(f"{name} {value}")


def _ohif_csp() -> tuple[str, str] | None:
    """Return the (header, value) pair for OHIF responses, or None when CSP is off."""
    policy = (getattr(settings, "CONTENT_SECURITY_POLICY", "") or "").strip()
    if not policy:
        return None
    directives = [d.strip() for d in policy.split(";") if d.strip()]
    _extend(directives, "script-src", "'unsafe-eval'")
    _replace(directives, "frame-ancestors", "'self'")
    if demo_data_enabled():
        _extend(directives, "connect-src", DEMO_ORIGIN)
    report_only = bool(getattr(settings, "CSP_REPORT_ONLY", True))
    header = "Content-Security-Policy-Report-Only" if report_only else "Content-Security-Policy"
    return header, "; ".join(directives)


def _with_csp(response):
    """Give an OHIF response the mount's policy: eval for the codec worker, framing by the platform's own pages."""
    csp = _ohif_csp()
    if csp:
        response[csp[0]] = csp[1]
    response["X-Frame-Options"] = "SAMEORIGIN"
    return response


def _ohif_dist_path() -> str:
    return getattr(
        settings,
        "DICOM_OHIF_DIST_PATH",
        os.path.join(os.path.dirname(__file__), "ohif-dist"),
    )


def _content_type(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    if ext in _TYPES:
        return _TYPES[ext]
    mime, _ = mimetypes.guess_type(path)
    return mime or "application/octet-stream"


def _demo_config(dist: str) -> HttpResponse:
    """Return the built app-config.js with the development demo overlay appended."""
    if not os.path.isfile(os.path.join(dist, "app-config.js")):
        raise Http404("OHIF asset not found.")
    with (
        open(os.path.join(dist, "app-config.js"), encoding="utf-8") as base,
        open(_DEMO_OVERLAY, encoding="utf-8") as demo,
    ):
        body = f"{base.read()}\n{demo.read()}"
    return HttpResponse(body, content_type="text/javascript")


def ohif_viewer(request, subpath: str = ""):
    """Serve the OHIF viewer SPA.

    A sub-path that names a file in the dist is served as that file. Any other
    sub-path falls back to ``index.html`` so OHIF's client-side routes resolve,
    except one whose last segment has a file extension: that is a missing asset,
    and answering it with the HTML document would have the browser run the page
    as a script chunk and fail far from the cause.
    """
    dist = _ohif_dist_path()
    if not os.path.isdir(dist):
        return HttpResponse(
            "OHIF viewer has not been built yet. Run scripts/build_ohif.sh to build it.",
            status=503,
        )

    if subpath == "app-config.js" and demo_data_enabled():
        return _with_csp(_demo_config(dist))

    if subpath:
        try:
            candidate = safe_join(dist, subpath)
        except SuspiciousFileOperation:
            raise Http404("Not found.")
        if os.path.isfile(candidate):
            return _with_csp(FileResponse(open(candidate, "rb"), content_type=_content_type(candidate)))
        if posixpath.splitext(posixpath.basename(subpath))[1]:
            raise Http404("OHIF asset not found.")

    index_path = os.path.join(dist, "index.html")
    if not os.path.isfile(index_path):
        raise Http404("OHIF viewer dist/index.html not found.")

    return _with_csp(FileResponse(open(index_path, "rb"), content_type="text/html"))
