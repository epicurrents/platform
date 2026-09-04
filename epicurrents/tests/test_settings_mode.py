"""Tests for ``epicurrents.settings_mode.get_settings_module``.

The function resolves ``DJANGO_MODE`` to the matching settings module
path. A misspelled value (e.g. ``"prod"``) used to fall back silently
to development — booting a container with ``DEBUG=True`` and a
placeholder ``SECRET_KEY`` while the operator believed they were in
production. The behaviour now raises so the misconfiguration surfaces
at startup instead of in production logs months later.
"""

import os
import warnings
from unittest import mock

import pytest
from django.core.exceptions import ImproperlyConfigured

from epicurrents.settings_mode import get_settings_module


class TestRecognisedModes:
    def test_production_resolves_to_production_module(self):
        with mock.patch.dict(os.environ, {"DJANGO_MODE": "production"}, clear=True):
            assert get_settings_module() == "epicurrents.settings.production"

    def test_development_resolves_to_development_module(self):
        with mock.patch.dict(os.environ, {"DJANGO_MODE": "development"}, clear=True):
            assert get_settings_module() == "epicurrents.settings.development"

    def test_mode_is_case_insensitive(self):
        with mock.patch.dict(os.environ, {"DJANGO_MODE": "PRODUCTION"}, clear=True):
            assert get_settings_module() == "epicurrents.settings.production"


class TestMisspelledModeRaises:
    """The headline regression: silent fallback on misspelled DJANGO_MODE."""

    def test_prod_typo_raises(self):
        with mock.patch.dict(os.environ, {"DJANGO_MODE": "prod"}, clear=True):
            with pytest.raises(ImproperlyConfigured, match="prod"):
                get_settings_module()

    def test_garbage_raises(self):
        with mock.patch.dict(os.environ, {"DJANGO_MODE": "definitely-not-a-mode"}, clear=True):
            with pytest.raises(ImproperlyConfigured):
                get_settings_module()


class TestFallback:
    def test_unset_mode_falls_back_to_explicit_settings_module(self):
        env = {"DJANGO_SETTINGS_MODULE": "my.custom.settings"}
        with mock.patch.dict(os.environ, env, clear=True):
            assert get_settings_module() == "my.custom.settings"

    def test_unset_mode_and_no_explicit_module_defaults_to_development(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            assert get_settings_module() == "epicurrents.settings.development"

    def test_empty_mode_string_is_treated_as_unset(self):
        with mock.patch.dict(os.environ, {"DJANGO_MODE": "   "}, clear=True):
            # Whitespace-only string is stripped to "" and treated as unset.
            assert get_settings_module() == "epicurrents.settings.development"


class TestConflictWarning:
    """The pre-existing warning for conflicting DJANGO_MODE +
    DJANGO_SETTINGS_MODULE values still fires."""

    def test_conflicting_values_warn_and_prefer_mode(self):
        env = {
            "DJANGO_MODE": "production",
            "DJANGO_SETTINGS_MODULE": "my.custom.settings",
        }
        with mock.patch.dict(os.environ, env, clear=True), warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            result = get_settings_module()
            assert result == "epicurrents.settings.production"
            assert any("conflicting values" in str(w.message) for w in caught)
