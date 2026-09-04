"""Tests for the project-loader contract as the template exercises it.

``apply_project_settings`` is called with a settings module's ``globals()`` at boot; these tests
run it against a synthetic dict with ``EPICURRENTS_PROJECT=example`` so the template's own
``settings.py`` proves the documented merge rules — app registration, list append, scalar
replacement — without a Django restart. The pin test keeps ``requires_platform`` honest against
the live platform version, the same gate ``manage.py check`` applies at boot.
"""

import pytest

from epicurrents.project_loader import apply_project_settings


@pytest.fixture
def merged(monkeypatch):
    monkeypatch.setenv("EPICURRENTS_PROJECT", "example")
    globs = {
        "INSTALLED_APPS": ["django.contrib.auth", "recordings"],
        "MIDDLEWARE": ["django.middleware.security.SecurityMiddleware"],
        "RECORDINGS_TRASH_RETENTION_DAYS": 30,
    }
    apply_project_settings(globs)
    return globs


class TestSettingsMerge:
    def test_project_app_is_registered_before_extras(self, merged):
        assert "projects.example" in merged["INSTALLED_APPS"]

    def test_list_settings_append_without_duplicates(self, merged):
        middleware = merged["MIDDLEWARE"]
        assert middleware[0] == "django.middleware.security.SecurityMiddleware"
        assert len(middleware) == len(set(middleware))

    def test_scalar_settings_replace_the_base_value(self, merged):
        # settings.py overrides the 30-day default down to 7.
        assert merged["RECORDINGS_TRASH_RETENTION_DAYS"] == 7

    def test_project_specific_settings_arrive(self, merged):
        assert merged["EXAMPLE_INSTITUTION_NAME"] == "Example Clinic"
        assert merged["EXAMPLE_NOTE_MAX_LENGTH"] == 2000

    def test_no_project_is_a_no_op(self, monkeypatch):
        monkeypatch.setenv("EPICURRENTS_PROJECT", "")
        globs = {"INSTALLED_APPS": ["django.contrib.auth"]}
        apply_project_settings(globs)
        assert globs == {"INSTALLED_APPS": ["django.contrib.auth"]}

    def test_missing_project_directory_is_refused(self, monkeypatch):
        from django.core.exceptions import ImproperlyConfigured

        monkeypatch.setenv("EPICURRENTS_PROJECT", "nosuchproject")
        with pytest.raises(ImproperlyConfigured):
            apply_project_settings({"INSTALLED_APPS": []})


class TestPlatformPin:
    def test_requires_platform_accepts_the_current_version(self):
        from packaging.specifiers import SpecifierSet
        from packaging.version import Version

        from epicurrents.version import __version__
        from projects.example.apps import ExampleConfig

        pin = ExampleConfig.requires_platform
        assert Version(__version__) in SpecifierSet(pin, prereleases=True)

    def test_the_check_reports_no_issue_for_example(self):
        from django.apps import apps as django_apps

        from epicurrents.checks import check_platform_version_requirements

        issues = check_platform_version_requirements(django_apps.get_app_configs())
        assert [issue for issue in issues if "example" in str(issue.obj).lower() or "example" in issue.msg] == []
