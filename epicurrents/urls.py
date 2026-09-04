"""Root URL configuration — mounts every app's API, static, viewer, and SPA catch-all.

There is deliberately no ``admin/`` mount. Account and group management lives at
``/api/v1/user/admin/`` instead, where the audit trail, the CSRF chokepoint and
the de-identification rules apply; see user/README.md → *Account administration*.

⚠️ LOAD-BEARING — fallback ordering. The three entries appended at the bottom of
this file must stay below every real mount, and nothing may be appended after
them. The SPA catch-all matches every path, so a route registered after it is
unreachable; and ``api_not_found`` matches every API-shaped path, so hoisting it
above the mounts 404s the whole API. Both failures are silent in the direction
that matters — the second turns "you are not signed in" into "no such endpoint",
and an earlier version of this file inserted project and plugin routes at a
fixed offset from the end, so adding a fallback would have reordered them
without a word. Contract test: epicurrents/tests/test_api_path_not_found.py.

The active project's ``urls.py`` / ``public_urls.py`` are mounted conditionally
at ``/project/api/v1/`` and ``/project/<name>/`` when ``EPICURRENTS_PROJECT`` is
set. Each enabled plugin's ``urls.py`` / ``public_urls.py`` are mounted at
``/plugin/<name>/api/v1/`` and ``/plugin/<name>/`` for every name in
``EPICURRENTS_PLUGINS``.
"""

import importlib
import os
import re

from django.conf import settings
from django.contrib.staticfiles.views import serve as staticfiles_serve
from django.urls import include, path, re_path

from activity.api.v1.ninja import api as activity_api
from epicurrents.api.v1.ninja import api as epicurrents_api
from epicurrents.views import (
    api_not_found,
    frontend_view,
    public_viewer_view,
    vendor_view,
    viewer_view,
)
from federation.api.v1.ninja import api as federation_api
from federation.views import federation_well_known
from library.api.v1.ninja import api as library_api
from notifications.api.v1.ninja import api as notifications_api
from user.api.v1.ninja import api as user_api

# Mount the public viewer route whenever at least one mode is configured; the
# mode segment matches only the configured keys, so an unknown /viewer/<x> still
# falls through to the viewer/SPA passthrough below. The ENABLE_PUBLIC_VIEWER
# gate lives in the view (read per request) so it 404s cleanly when disabled
# without depending on URLconf import-time settings.
_public_viewer_routes = []
if settings.PUBLIC_VIEWER_MODES:
    _mode_alternation = "|".join(re.escape(m) for m in settings.PUBLIC_VIEWER_MODES)
    _public_viewer_routes = [
        re_path(rf"^viewer/(?P<mode>{_mode_alternation})/?$", public_viewer_view),
    ]

urlpatterns = [
    path("api/v1/", epicurrents_api.urls),
    path("api/v1/user/", user_api.urls),
    path("api/v1/activity/", activity_api.urls),
    path("api/v1/notifications/", notifications_api.urls),
    path("api/v1/library/", library_api.urls),
    path("api/v1/federation/", federation_api.urls),
    path(
        ".well-known/epicurrents-federation.json",
        federation_well_known,
        name="federation-well-known",
    ),
    path("annotations/", include("annotations.urls")),
    path("compute/", include("compute.urls")),
    path("recordings/", include("recordings.urls")),
    path("media/", include("media.urls")),
    # Standalone, cross-origin-isolated public viewer modes — matched before the
    # generic viewer passthrough so /viewer/<mode> gets the isolation headers and
    # the configured lib rather than the SPA fallback. Only mounted when enabled;
    # the alternation is built from the configured mode keys (project-overridable).
    *_public_viewer_routes,
    # Serve viewer library static files at /viewer/<file>.
    re_path(r"^viewer/(?P<path>.+)$", viewer_view),
    # Serve deploy-vendored, version-pinned assets (self-hosted Pyodide runtime +
    # its wheel closure) at /vendor/<file>. CORP + immutable caching in the view.
    re_path(r"^vendor/(?P<path>.+)$", vendor_view),
    # Serve Django static files via the staticfiles finders. insecure=True allows
    # this to work when DEBUG=False (Docker production mode).
    re_path(r"^static/(?P<path>.+)$", staticfiles_serve, {"insecure": True}),
]

# Any path carrying an ``api/v<n>`` segment, with or without app / project /
# plugin segments in front of it: ``api/v1/...``, ``recordings/api/v1/...``,
# ``plugin/dicom/api/v1/...``. Appended below every real mount, so a request that
# reaches it named an API route that does not exist.
#
# The ``(?:/|$)`` tail also catches a bare ``/api/v1`` — a mount prefix with its
# trailing slash dropped resolves nowhere, and APPEND_SLASH never rescued it
# because the SPA catch-all below already resolved everything.
_API_SHAPED_PATH = r"^(?:[\w-]+/)*api/v\d+(?:/|$)"

# Conditionally mount the active project's URL patterns.
#
# Two optional modules are loaded from the active project:
#
#   urls.py        — Django Ninja API endpoints, mounted at /project/api/v1/.
#                    Use this for REST endpoints the Vue SPA calls.
#
#   public_urls.py — Plain Django URL patterns, mounted at /project/<name>/.
#                    Use this for non-API content that needs custom response
#                    headers (e.g. a viewer SPA with COOP/COEP for WASM) or
#                    that must live outside the /api/v1/ namespace.
#
# Both modules are optional — projects that need neither can omit them.
_active_project = os.getenv("EPICURRENTS_PROJECT", "").strip()
if _active_project:
    try:
        _project_urls = importlib.import_module(f"projects.{_active_project}.urls")
        urlpatterns.append(path("project/api/v1/", include(_project_urls)))
    except ModuleNotFoundError:
        pass
    try:
        _project_public_urls = importlib.import_module(f"projects.{_active_project}.public_urls")
        urlpatterns.append(path(f"project/{_active_project}/", include(_project_public_urls)))
    except ModuleNotFoundError:
        pass

# Conditionally mount each enabled plugin's URL patterns.
#
# Symmetric with the project mount above, but a deployment may enable zero or
# more plugins. Each plugin contributes the same two optional modules:
#
#   urls.py        — Django Ninja API endpoints, mounted at
#                    /plugin/<namespace>/api/v1/.
#   public_urls.py — Plain Django URL patterns, mounted at /plugin/<namespace>/.
#
# Both are optional. Modules are imported by the plugin's directory name; the
# mount segment is the PluginConfig's resolved url_namespace (the directory
# name unless plugin_url_namespace overrides it). The order follows
# EPICURRENTS_PLUGINS; namespace collisions are caught at boot by
# epicurrents.plugin_loader.validate_plugins, so nothing here has to defend
# against a duplicate mount segment. Django loads this URLconf lazily after
# the app registry is populated, so apps.get_app_config is safe here.
from django.apps import apps as _django_apps

from epicurrents.plugin_loader import get_active_plugins

for _plugin_name in get_active_plugins():
    _plugin_cfg = _django_apps.get_app_config(_plugin_name)
    _plugin_ns = getattr(_plugin_cfg, "url_namespace", _plugin_name)
    try:
        _plugin_urls = importlib.import_module(f"plugins.{_plugin_name}.urls")
        urlpatterns.append(path(f"plugin/{_plugin_ns}/api/v1/", include(_plugin_urls)))
    except ModuleNotFoundError:
        pass
    try:
        _plugin_public_urls = importlib.import_module(f"plugins.{_plugin_name}.public_urls")
        urlpatterns.append(path(f"plugin/{_plugin_ns}/", include(_plugin_public_urls)))
    except ModuleNotFoundError:
        pass

# ── Fallbacks ────────────────────────────────────────────────────────────────
# Appended last, after every app, project and plugin mount above, so a request
# reaches these only when nothing real matched. Nothing may be appended below
# them; an earlier version of this file inserted project and plugin routes at a
# fixed offset from the end, which made adding a fallback quietly reorder them.
urlpatterns += [
    # An API-shaped path that matched nothing is a 404, not the SPA. Without this
    # a mistyped write answers 200 with index.html and the caller records a
    # success that never happened.
    re_path(_API_SHAPED_PATH, api_not_found),
    # Serve the built Vue SPA at root; fall back to index.html for SPA routes.
    # Root-level assets (manifest.json, push-sw.js, favicon, etc.) are also served here.
    path("", frontend_view),
    re_path(r"^(?P<path>.+)$", frontend_view),
]
