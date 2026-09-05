"""Tests for the standalone, auth-free public viewer at /viewer/<mode>.

The route is mounted whenever a mode is configured, but the view gates on
``ENABLE_PUBLIC_VIEWER`` (off by default) so a deployment that has not opted in
returns 404. Modes live in ``settings.PUBLIC_VIEWER_MODES`` and are
project-overridable, so the view resolves the lib + setup from settings at
request time rather than from a hardcoded table.

The tests for the shipped mode therefore read the expected paths back out of
settings instead of spelling them out. Hardcoding them once cost two failures:
the platform build moved from ``/viewer/base/`` to ``/viewer/`` and gained a
self-hosted ``pyodideAssetPath``, and the assertions went stale while the view
stayed correct. What is worth pinning is not the literal path — that is a build
layout detail, and the Pyodide path carries a version that will be bumped — but
that the page hands the viewer *exactly* what settings configured, and that the
shipped mode's asset roots are same-origin. Both survive a rebuild; a literal
does not. The override test still uses literals, since there the mode is
declared in the test itself.
"""

import json

import pytest
from django.conf import settings
from django.test import RequestFactory, override_settings

from epicurrents.views import _LEAD_FIELD_SCRIPT, public_viewer_view


def _rendered_setup(body: str) -> dict:
    """Return the SETUP blob the page hands the viewer, parsed back out of it.

    The template inlines it as ``SETUP:{...}};</script>``; JSON cannot contain
    ``<``, so the closing marker is unambiguous. Parsing rather than substring
    matching means a malformed blob fails here instead of at the viewer's feet.
    """
    blob = body.split("SETUP:", 1)[1].split("};</script>", 1)[0]
    return json.loads(blob)


@pytest.mark.django_db
@override_settings(ENABLE_PUBLIC_VIEWER=False)
def test_disabled_returns_404(client):
    # Route resolves (the "public" mode is configured), but the view 404s
    # because the feature is off. Overridden explicitly so the test does not
    # depend on the ambient .env (a dev box may have the viewer enabled).
    assert client.get("/viewer/public").status_code == 404


@pytest.mark.django_db
@override_settings(ENABLE_PUBLIC_VIEWER=True)
def test_enabled_serves_public_mode(client):
    mode = settings.PUBLIC_VIEWER_MODES["public"]
    response = client.get("/viewer/public")
    assert response.status_code == 200
    # Cross-origin isolation headers make the document crossOriginIsolated.
    assert response["Cross-Origin-Opener-Policy"] == "same-origin"
    assert response["Cross-Origin-Embedder-Policy"] == "require-corp"
    assert response["Cross-Origin-Resource-Policy"] == "same-origin"
    body = response.content.decode()
    # Loads the configured lib as a classic-script UMD that sets window.Epicurrents,
    # takes its stylesheet from the same directory, and calls the factory. The
    # bundle name comes from the mode, because the builder edition under /viewer/
    # and the per-project builds under /viewer/<project>/ name theirs differently.
    assert f"{mode['lib_path']}{mode['lib_file']}" in body
    assert f"{mode['lib_path']}epicurrents-lib.css" in body
    assert "createEpicurrentsApp()" in body
    # The setup blob is passed through verbatim — the viewer boots from settings,
    # so anything the view drops or rewrites is a bug caught here rather than a
    # surprise in the browser.
    assert _rendered_setup(body) == mode["setup"]


@pytest.mark.django_db
@override_settings(ENABLE_PUBLIC_VIEWER=True)
def test_public_mode_assets_are_same_origin(client):
    # The point of the shipped mode is a viewer that works offline and stays
    # cross-origin-isolated, so every asset root must be an absolute same-origin
    # path rather than a CDN URL. Pyodide is the one that bites: the viewer
    # defaults it to jsdelivr, and a COEP: require-corp document cannot load it
    # from there, so the runtime is vendored under /vendor/ at deploy time.
    # Asserted as a property so a Pyodide version bump needs no test edit.
    setup = _rendered_setup(client.get("/viewer/public").content.decode())
    for key in ("assetPath", "pyodideAssetPath"):
        assert key in setup, f"{key} missing from the public mode setup"
        value = setup[key]
        assert value.startswith("/"), f"{key} is not same-origin: {value!r}"
        assert "://" not in value, f"{key} is not same-origin: {value!r}"
    assert settings.PUBLIC_VIEWER_MODES["public"]["lib_path"].startswith("/")


@override_settings(ENABLE_PUBLIC_VIEWER=True)
def test_trailing_slash_serves_public_mode(client):
    # /viewer/<mode> accepts an optional trailing slash; without it the slashed
    # form falls through to the file-serving passthrough and lands on the SPA
    # index instead of the standalone viewer page.
    mode = settings.PUBLIC_VIEWER_MODES["public"]
    response = client.get("/viewer/public/")
    assert response.status_code == 200
    assert f"{mode['lib_path']}{mode['lib_file']}" in response.content.decode()


@override_settings(ENABLE_PUBLIC_VIEWER=True)
def test_unknown_mode_404():
    # The URL route matches only configured keys, so an unknown mode reaches the
    # view only by a direct call — exercise the view's defensive 404.
    request = RequestFactory().get("/viewer/bogus")
    assert public_viewer_view(request, mode="bogus").status_code == 404


@override_settings(ENABLE_PUBLIC_VIEWER=True)
def test_lead_field_script_loads_before_the_lib(client):
    # The page is the only viewer surface that runs no platform JavaScript of its
    # own — its SETUP is JSON, and a lead-field provider is a function, so nothing
    # in PUBLIC_VIEWER_MODES can carry one. This script is how it arrives, and
    # order is the contract: after the SETUP declaration, so there is an object to
    # write into, and before the lib, so the viewer reads a SETUP that already has
    # the provider rather than one amended behind it.
    body = client.get("/viewer/public").content.decode()
    mode = settings.PUBLIC_VIEWER_MODES["public"]
    setup_at = body.index("SETUP:")
    script_at = body.index(f'<script src="{_LEAD_FIELD_SCRIPT}">')
    lib_at = body.index(f"{mode['lib_path']}{mode['lib_file']}")
    assert setup_at < script_at < lib_at


@override_settings(ENABLE_PUBLIC_VIEWER=True)
def test_lead_field_script_is_same_origin(client):
    # It loads inside a COEP: require-corp document, so it has to come from this
    # origin and carry CORP — which viewer_view gives every file it serves out of
    # viewer-dist. A CDN URL here would be blocked with nothing in any server log.
    assert _LEAD_FIELD_SCRIPT.startswith("/")
    assert "://" not in _LEAD_FIELD_SCRIPT
    assert _LEAD_FIELD_SCRIPT in client.get("/viewer/public").content.decode()


@override_settings(
    ENABLE_PUBLIC_VIEWER=True,
    PUBLIC_VIEWER_MODES={
        "project": {
            "lib_path": "/viewer/course/",
            "setup": {
                "activeModules": ["eeg", "acc"],
                "assetPath": "/viewer/course/",
                "containerId": "viewer",
            },
        },
    },
)
def test_project_overridable_mode():
    # A project can point a mode at its own viewer build with its own module set;
    # the view renders the lib + setup straight from settings. The path is the
    # one a project build produces (VITE_PROJECT=course builds into
    # /viewer/course/) so this exercises a genuinely different lib root instead
    # of restating the platform default.
    request = RequestFactory().get("/viewer/project")
    response = public_viewer_view(request, mode="project")
    assert response.status_code == 200
    body = response.content.decode()
    assert "/viewer/course/epicurrents-lib.umd.cjs" in body
    assert '"acc"' in body
    # Project settings REPLACE this key rather than deep-merging it (only
    # _LIST_KEYS and CELERY_BEAT_SCHEDULE merge — see epicurrents/project_loader.py),
    # so a mode that omits a platform default really does omit it. A project
    # overriding the viewer opts out of the platform's whole setup, vendored
    # Pyodide root included, and has to restate whatever it still wants.
    assert "pyodideAssetPath" not in _rendered_setup(body)
    # The lead-field script is not part of the mode config, so an overriding
    # project keeps it without restating anything. It is the one piece of the page
    # a project cannot accidentally opt out of by replacing the setup.
    assert _LEAD_FIELD_SCRIPT in body
