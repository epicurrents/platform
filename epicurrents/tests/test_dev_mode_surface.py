"""Tests for the dev-mode posture surfaces.

``/api/v1/health`` returns ``mode`` and ``debug`` so the SPA can show its
bottom-of-page "DEV MODE" banner.

Django admin's ``base_site.html`` override used to inject a second banner from
the ``debug_mode`` context variable. That template went with the admin mount —
it extends ``admin/base.html`` and reverses ``admin:index``, so it could not
render at all once the URLs were unmounted. The ``debug_mode`` context processor
survives it and is still covered below; it has no consumer today, and is kept
because it is a three-line generic that any future template can read.
"""

import os
from unittest import mock

import pytest


@pytest.mark.django_db
class TestHealthEndpointMode:
    def test_mode_reflects_django_mode_production(self, client):
        with mock.patch.dict(os.environ, {"DJANGO_MODE": "production"}):
            response = client.get("/api/v1/health")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert body["mode"] == "production"
        assert isinstance(body["debug"], bool)

    def test_mode_reflects_django_mode_development(self, client):
        with mock.patch.dict(os.environ, {"DJANGO_MODE": "development"}):
            response = client.get("/api/v1/health")
        assert response.status_code == 200
        assert response.json()["mode"] == "development"

    def test_mode_is_unset_when_django_mode_blank(self, client):
        with mock.patch.dict(os.environ, {"DJANGO_MODE": ""}):
            response = client.get("/api/v1/health")
        assert response.status_code == 200
        assert response.json()["mode"] == "unset"

    def test_mode_is_unset_for_unknown_value(self, client):
        with mock.patch.dict(os.environ, {"DJANGO_MODE": "staging"}):
            response = client.get("/api/v1/health")
        assert response.status_code == 200
        assert response.json()["mode"] == "unset"

    def test_debug_field_reflects_settings(self, client, settings):
        settings.DEBUG = True
        with mock.patch.dict(os.environ, {"DJANGO_MODE": "development"}):
            response = client.get("/api/v1/health")
        assert response.json()["debug"] is True

        settings.DEBUG = False
        response = client.get("/api/v1/health")
        assert response.json()["debug"] is False


class TestDebugContextProcessor:
    def test_returns_true_when_settings_debug_true(self, settings, rf):
        from epicurrents.context_processors import debug_mode

        settings.DEBUG = True
        assert debug_mode(rf.get("/")) == {"debug_mode": True}

    def test_returns_false_when_settings_debug_false(self, settings, rf):
        from epicurrents.context_processors import debug_mode

        settings.DEBUG = False
        assert debug_mode(rf.get("/")) == {"debug_mode": False}
