"""Tests for the ``requires_platform`` system check.

Two halves, and the second is the one that would fail quietly. The decision
logic — satisfied, unsatisfied, malformed, absent — is exercised against stub
configs. The *discovery* is exercised against the real app registry, because a
check that looks at the wrong set of apps reports "no issues" for a deployment
it never examined, which is indistinguishable from a clean run.
"""

import ast
import pathlib

import pytest
from django.apps import apps as django_apps
from django.core.checks import Error, Warning

from epicurrents import checks
from epicurrents.version import __version__, compatible_range


class _StubConfig:
    """Minimal stand-in for an AppConfig: the check reads only these attributes."""

    def __init__(self, label, name, requires_platform=...):
        self.label = label
        self.name = name
        if requires_platform is not ...:
            self.requires_platform = requires_platform


@pytest.fixture
def stub_configs(monkeypatch):
    """Replace the discovered configs with an explicit list."""

    def _install(*configs):
        monkeypatch.setattr(checks, "_pinnable_configs", lambda: list(configs))
        return checks.check_platform_version_requirements(None)

    return _install


class TestDecision:
    def test_a_satisfied_pin_reports_nothing(self, stub_configs):
        issues = stub_configs(_StubConfig("thing", "projects.thing", compatible_range(__version__)))
        assert issues == []

    def test_an_unsatisfied_pin_is_an_error(self, stub_configs):
        issues = stub_configs(_StubConfig("thing", "projects.thing", ">=99.0,<100"))
        assert len(issues) == 1
        assert isinstance(issues[0], Error)
        assert issues[0].id == "epicurrents.E001"
        assert __version__ in issues[0].msg, "the message must say which platform it actually found"

    def test_a_malformed_pin_is_a_distinct_error(self, stub_configs):
        """Separate id from E001 because the remedy is different: fix the string,
        not the checkout. A single id would send the reader to the wrong fix."""
        issues = stub_configs(_StubConfig("thing", "projects.thing", "1.0 or later"))
        assert len(issues) == 1
        assert issues[0].id == "epicurrents.E002"

    def test_a_missing_pin_is_a_warning_not_an_error(self, stub_configs):
        """The distinction that decides whether a deployment boots. Every project
        written before this existed has no declaration, and refusing to start over
        an absent one would be a worse outage than the drift it guards against."""
        issues = stub_configs(_StubConfig("thing", "projects.thing"))
        assert len(issues) == 1
        assert isinstance(issues[0], Warning)
        assert not isinstance(issues[0], Error)
        assert issues[0].id == "epicurrents.W001"

    def test_the_missing_pin_hint_offers_the_canonical_range(self, stub_configs):
        """The hint is a copy-paste remedy, so it has to be right for the running
        version rather than a frozen example — and it has to come from the one
        place the cap rule is encoded. Computing a cap here as "next major" reads
        as correct and is wrong for every 0.x release, which is what this
        platform is.
        """
        from epicurrents.version import satisfies

        hint = stub_configs(_StubConfig("thing", "projects.thing"))[0].hint
        assert compatible_range(__version__) in hint
        assert satisfies(__version__, compatible_range(__version__))

    def test_every_declaring_app_is_reported_separately(self, stub_configs):
        issues = stub_configs(
            _StubConfig("a", "projects.a", ">=99.0"),
            _StubConfig("b", "plugins.b", ">=99.0"),
            _StubConfig("c", "plugins.c", compatible_range(__version__)),
        )
        assert {issue.obj for issue in issues} == {"projects.a", "plugins.b"}

    def test_an_empty_deployment_reports_nothing(self, stub_configs):
        assert stub_configs() == []


class TestDiscovery:
    def test_core_apps_are_not_asked_to_declare_a_pin(self):
        """Core apps are the platform; pinning them to it is meaningless. If the
        filter admitted them, every deployment would carry a dozen warnings and
        the real one would be lost among them."""
        names = {config.name for config in checks._pinnable_configs()}
        for core in ("epicurrents", "recordings", "activity", "django.contrib.auth"):
            assert core not in names

    def test_finds_a_project_or_plugin_when_one_is_installed(self):
        """Guards the inverse: a filter matching nothing reports a clean run for
        a deployment it never looked at."""
        installed = {config.name for config in django_apps.get_app_configs()}
        expected = {name for name in installed if name.startswith(("projects.", "plugins."))}
        assert {config.name for config in checks._pinnable_configs()} == expected

    def test_every_in_repo_project_and_plugin_declares_a_pin(self):
        """Read from source rather than the registry, which holds only the apps
        this test run installed — one project at most, and no plugins."""
        repo = pathlib.Path(__file__).resolve().parent.parent.parent
        missing = []
        for apps_file in sorted(repo.glob("projects/*/apps.py")) + sorted(repo.glob("plugins/*/apps.py")):
            tree = ast.parse(apps_file.read_text())
            declared = any(
                isinstance(node, ast.Assign)
                and any(getattr(target, "id", None) == "requires_platform" for target in node.targets)
                for cls in tree.body
                if isinstance(cls, ast.ClassDef)
                for node in cls.body
            )
            if not declared:
                missing.append(str(apps_file.relative_to(repo)))
        assert not missing, f"no requires_platform declared in: {missing}"


class TestRegistration:
    def test_the_core_app_still_imports_the_checks_module(self):
        """A source assertion, because the runtime one cannot see this: importing
        this test module already registers the check, so a `ready()` that stopped
        importing it would leave every test here passing and every deployment
        unchecked. The same reasoning as activity/tests/test_checks.py.
        """
        source = (pathlib.Path(__file__).resolve().parent.parent / "apps.py").read_text()
        assert "from . import checks" in source, "EpicurrentsConfig.ready no longer imports the checks module"
