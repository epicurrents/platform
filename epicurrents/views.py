"""Static-file views — SPA index fallback for ``/``, the viewer-library passthrough at ``/viewer/``, and the standalone cross-origin-isolated public viewer."""

import json
from pathlib import Path

from django.conf import settings
from django.http import HttpResponse, HttpResponseNotFound, JsonResponse
from django.middleware.csrf import get_token
from django.views.static import serve

FRONTEND_DIST = Path(settings.BASE_DIR) / "frontend" / "dist"
VIEWER_DIST = Path(settings.BASE_DIR) / "frontend" / "viewer-dist"
VENDOR_DIR = Path(settings.VENDOR_DIR)


def _with_corp(response):
    """Tag a same-origin asset with CORP so it loads under the public viewer's
    ``COEP: require-corp`` document while staying unembeddable cross-origin."""
    response.setdefault("Cross-Origin-Resource-Policy", "same-origin")
    return response


def api_not_found(request, path=""):
    """Answer 404 for an API-shaped path that matched no mounted route.

    The SPA fallback below serves index.html for anything unmatched, because a
    client-side route is indistinguishable from a typo at the server. For an API
    path it is distinguishable, and getting it wrong is expensive: a mistyped
    write returns 200 with an HTML body, and a caller that checks the status code
    — which is every caller — records a success that never happened. Answering
    404 here turns that into the error it always was.

    JSON rather than an HTML 404 page, in Ninja's own ``{"detail": ...}`` shape,
    so a client parsing the body of a failed API call gets the same thing whether
    the route was missing or the handler refused.
    """
    return JsonResponse({"detail": "Not found"}, status=404)


def frontend_view(request, path=""):
    # Serve the exact file if it exists; fall back to index.html for SPA routes.
    candidate = FRONTEND_DIST / path
    if not path or not candidate.is_file():
        path = "index.html"
    if path == "index.html":
        # Seed the csrftoken cookie on the SPA document so the axios client can
        # echo it back on session-authenticated writes (see epicurrents.auth).
        # Only the document needs it — static assets skip the Set-Cookie.
        get_token(request)
    response = _with_corp(serve(request, path, document_root=FRONTEND_DIST))
    if path.startswith("assets/"):
        # Content-hashed, immutable bundles carry no PHI — opt them back into
        # long-term caching (the no-store default targets dynamic/PHI responses).
        # index.html and other non-hashed files fall through to no-store.
        response["Cache-Control"] = "public, max-age=31536000, immutable"
    return response


def viewer_view(request, path=""):
    # Serve viewer library static files; fall back to the SPA index for route-like
    # paths so that /viewer/<hash> SPA routes load correctly in a new tab. A MISSING
    # file-like path (one with an extension) is a genuine 404 — returning index.html
    # with a 200 silently feeds an HTML page to a script/style request, which fails
    # confusingly at parse time rather than as an obvious missing asset.
    if not path or not (VIEWER_DIST / path).is_file():
        if path and "." in path.rsplit("/", 1)[-1]:
            return HttpResponseNotFound("Not found.")
        return frontend_view(request)
    response = serve(request, path, document_root=VIEWER_DIST)
    # Python's mimetypes module doesn't map .cjs → application/javascript by default,
    # causing browsers to reject execution. Override it explicitly.
    if path.endswith(".cjs"):
        response["Content-Type"] = "application/javascript; charset=utf-8"
    # Viewer assets carry no PHI. The lib has a FIXED filename, so it must be
    # revalidated on every load (no-cache) or a deploy serves a stale lib; the
    # content-hashed worker bundles are immutable and cache long-term. Anything
    # else (favicons, report templates) falls through to the no-store default.
    name = path.rsplit("/", 1)[-1]
    if name.startswith("epicurrents-lib."):
        response["Cache-Control"] = "no-cache"
    elif ".worker-" in name:
        response["Cache-Control"] = "public, max-age=31536000, immutable"
    return _with_corp(response)


def vendor_view(request, path=""):
    """Serve deploy-vendored, version-pinned assets at /vendor/<path> — the
    self-hosted Pyodide runtime and its wheel closure.

    These load inside the cross-origin-isolated viewer (COEP: require-corp), so
    each response must carry CORP or the fetch is blocked. Version-pinned assets
    (wheels/wasm) cache long-term as immutable; the lockfile revalidates (it is
    edited post-vendoring). Unknown paths 404 — this is an asset tree, not an SPA
    route, so there is no index.html fallback."""
    if not path or not (VENDOR_DIR / path).is_file():
        return HttpResponseNotFound("Not found.")
    response = serve(request, path, document_root=VENDOR_DIR)
    # WebAssembly must be served as application/wasm or the browser refuses to
    # compile it; mimetypes does not reliably map .wasm/.mjs on every runtime.
    if path.endswith(".wasm"):
        response["Content-Type"] = "application/wasm"
    elif path.endswith(".mjs"):
        response["Content-Type"] = "text/javascript; charset=utf-8"
    # Version-pinned wheels/wasm/etc. are truly immutable and cache long-term. The
    # lockfile is NOT: it's edited post-vendoring (mne/pooch merge) and may be
    # regenerated, so it must revalidate — immutable caching pins a stale lock and
    # (via any service-worker cache) survives even DevTools "disable cache".
    if path.endswith(".json"):
        response["Cache-Control"] = "no-cache"
    else:
        response["Cache-Control"] = "public, max-age=31536000, immutable"
    return _with_corp(response)


# UMD bundle name assumed when a public-viewer mode does not declare its own
# ``lib_file``. The per-project builds under ``viewer-dist/<project>/`` emit this
# name; the builder edition copied into ``viewer-dist/`` emits ``.umd.js``, so a
# mode pointing there has to say so.
_DEFAULT_LIB_FILE = "epicurrents-lib.umd.cjs"

# The platform's lead-field provider, built by ``frontend/vite.config.leadfields.ts``
# into ``viewer-dist/`` and served by ``viewer_view`` above. The public viewer page
# is the only surface that runs no platform JavaScript of its own: its SETUP is
# JSON, and a provider is a function, so without this script the source-
# localisation tool reports every montage as unavailable. Absent on a deployment
# built before the script existed, where the tag 404s and the page loads without it.
_LEAD_FIELD_SCRIPT = "/viewer/epicurrents-leadfields.js"

_PUBLIC_VIEWER_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Epicurrents viewer</title>
<link rel="stylesheet" href="{lib}epicurrents-lib.css">
<style>html,body{{height:100%;margin:0}}#epicurrents-viewer{{height:100vh;width:100vw}}</style>
</head>
<body>
<div id="epicurrents-viewer"></div>
<script>window.__EPICURRENTS__={{EVENT_BUS:null,RUNTIME:null,SETUP:{setup}}};</script>
<script src="{lead_fields}"></script>
<script src="{lib}{lib_file}"></script>
<script>window.Epicurrents&&window.Epicurrents.createEpicurrentsApp&&window.Epicurrents.createEpicurrentsApp();</script>
</body>
</html>"""


def public_viewer_view(request, mode=""):
    # Standalone, cross-origin-isolated viewer page. The COOP/COEP headers make
    # the document crossOriginIsolated so the viewer's SharedArrayBuffer memory
    # manager is available; the lean lib and its assets carry CORP via the
    # asset views above. Auth-free by design — it can reach no platform data.
    # Modes are defined in settings.PUBLIC_VIEWER_MODES (project-overridable);
    # the feature is off unless ENABLE_PUBLIC_VIEWER is set. The route is only
    # mounted when enabled, but re-check here so a direct call still 404s.
    if not settings.ENABLE_PUBLIC_VIEWER:
        return HttpResponseNotFound("The public viewer is disabled.")
    config = settings.PUBLIC_VIEWER_MODES.get(mode)
    if config is None:
        return HttpResponseNotFound("Unknown public viewer mode.")
    html = _PUBLIC_VIEWER_TEMPLATE.format(
        lib=config["lib_path"],
        lib_file=config.get("lib_file", _DEFAULT_LIB_FILE),
        setup=json.dumps(config["setup"]),
        lead_fields=_LEAD_FIELD_SCRIPT,
    )
    response = HttpResponse(html, content_type="text/html; charset=utf-8")
    response["Cross-Origin-Opener-Policy"] = "same-origin"
    response["Cross-Origin-Embedder-Policy"] = "require-corp"
    response["Cross-Origin-Resource-Policy"] = "same-origin"
    return response
