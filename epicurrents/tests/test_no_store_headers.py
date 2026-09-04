"""Contract test for the PHI no-store cache policy.

PHI-bearing responses (API JSON, recording/media byte serving) must carry
``Cache-Control: no-store`` so neither the browser nor an intermediary proxy
stores them. ``SecurityHeadersMiddleware`` applies that default via
``setdefault``; it is on unless ``DISABLE_NO_STORE_HEADERS`` is set (default
off). Views that serve non-PHI static assets opt back into caching by setting
their own Cache-Control, which the default must not clobber.
"""

import pytest
from django.http import HttpResponse
from django.test import RequestFactory, override_settings

from epicurrents.middleware import SecurityHeadersMiddleware


def _run(view_headers=None):
    """Run a response through a freshly-built middleware (so it reads the
    current ``DISABLE_NO_STORE_HEADERS``), optionally with view-set headers."""

    def get_response(request):
        response = HttpResponse("x")
        for key, value in (view_headers or {}).items():
            response[key] = value
        return response

    middleware = SecurityHeadersMiddleware(get_response)
    return middleware(RequestFactory().get("/api/v1/"))


def test_no_store_default_on_dynamic_response():
    assert "no-store" in _run()["Cache-Control"]


@override_settings(DISABLE_NO_STORE_HEADERS=True)
def test_disabled_emits_no_cache_control():
    assert "no-store" not in _run().get("Cache-Control", "")


def test_view_set_cache_control_is_preserved():
    # An immutable static asset opts back into caching; the no-store default
    # must not overwrite it.
    response = _run(view_headers={"Cache-Control": "public, max-age=31536000, immutable"})
    assert response["Cache-Control"] == "public, max-age=31536000, immutable"
    assert "no-store" not in response["Cache-Control"]


@pytest.mark.django_db
def test_api_response_carries_no_store_end_to_end(client):
    # Through the real middleware stack — the health endpoint is unauthenticated.
    response = client.get("/api/v1/")
    assert "no-store" in response.get("Cache-Control", "")
