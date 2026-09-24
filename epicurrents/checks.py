"""Django system checks: the platform pin, the text-hygiene patterns and the anonymisation assessment's age.

A project lives in its own repository now, and nothing keeps it in step with the
platform it is checked out beside. The failure that follows is rarely a clean
ImportError: a renamed keyword argument, a changed return shape, a permission
helper that grew a parameter — these surface as wrong behaviour somewhere
downstream, on a deployment carrying clinical data.

So a project states the platform range it was built against, as
``requires_platform`` on its ``AppConfig``:

    class MyprojectConfig(AppConfig):
        name = "projects.myproject"
        label = "myproject"
        requires_platform = ">=0.1,<0.2"

The AppConfig rather than ``settings.py`` because plugins need the same thing,
and several of them can be enabled at once: a settings key would have each
loaded plugin silently overwrite the last one's value, since the loader replaces
scalars rather than merging them. Every app config is its own declaration, and
this check reads all of them.

Registered here rather than tested, for the reason ``activity/checks.py`` gives:
a check runs after every app's ``ready()``, so it sees the project and plugins
the deployment actually has, which no test under the platform test settings can.
It also runs on ``manage.py check``, which Django performs before ``runserver``
and before ``migrate`` — so a mismatched pair stops the stack coming up rather
than being discovered by whatever it corrupts first.

An unsatisfied pin is an Error and stops the deployment. A missing one is a
Warning: it is what every project written before this existed looks like, and
refusing to boot over an absent declaration would be a worse failure than the
drift it is guarding against.
"""

import re

from django.apps import apps as django_apps
from django.conf import settings
from django.core.checks import Error, Tags, Warning, register

from epicurrents.assessment import assessment_currency
from epicurrents.version import InvalidVersion, __version__, compatible_range, satisfies

# Apps that are the platform itself, and so cannot be pinned to it. Everything
# under `projects.` or `plugins.` is a candidate; core apps are skipped without
# comment, since asking Django's own `auth` app to declare a pin is nonsense.
_OWNER_PREFIXES = ("projects.", "plugins.")


def _pinnable_configs():
    """App configs belonging to a project or plugin, which may declare a pin."""
    return [config for config in django_apps.get_app_configs() if config.name.startswith(_OWNER_PREFIXES)]


@register(Tags.compatibility)
def check_platform_version_requirements(app_configs, **kwargs):
    """Verify every project's and plugin's ``requires_platform`` against this platform."""
    issues = []
    for config in _pinnable_configs():
        specifier = getattr(config, "requires_platform", None)
        if specifier is None:
            issues.append(
                Warning(
                    f"{config.label} does not declare which platform versions it supports.",
                    hint=(
                        "Set requires_platform on its AppConfig, e.g. "
                        f'requires_platform = "{compatible_range(__version__)}". '
                        "Without it, nothing detects the project drifting out of step "
                        "with the platform it is checked out beside."
                    ),
                    obj=config.name,
                    id="epicurrents.W001",
                )
            )
            continue
        try:
            satisfied = satisfies(__version__, specifier)
        except InvalidVersion as exc:
            issues.append(
                Error(
                    f"{config.label} declares requires_platform = {specifier!r}, which cannot be read: {exc}",
                    hint=f'Use comma-separated clauses, as in "{compatible_range(__version__)}".',
                    obj=config.name,
                    id="epicurrents.E002",
                )
            )
            continue
        if not satisfied:
            issues.append(
                Error(
                    f"{config.label} requires platform {specifier}, but this platform is {__version__}.",
                    hint=(
                        "Check out a platform release inside that range, or update the "
                        f"project for {__version__} and widen its requires_platform. "
                        "Do not downgrade a platform a deployment has already migrated: "
                        "migrations are forward-only and the audit chain is versioned."
                    ),
                    obj=config.name,
                    id="epicurrents.E001",
                )
            )
    return issues


@register(Tags.security)
def check_text_hygiene_patterns(app_configs, **kwargs):
    """Verify ``TEXT_HYGIENE_PATTERNS`` is a mapping of kind to a regular expression that compiles.

    The patterns run on every rename and upload that carries a label, so one
    that does not compile would fail those requests rather than the boot.
    """
    configured = getattr(settings, "TEXT_HYGIENE_PATTERNS", {})
    if not isinstance(configured, dict):
        return [
            Error(
                "TEXT_HYGIENE_PATTERNS must be a dict of kind -> regular expression.",
                id="epicurrents.E010",
            )
        ]
    issues = []
    for kind, pattern in configured.items():
        if not isinstance(kind, str) or not kind or not isinstance(pattern, str):
            issues.append(
                Error(
                    f"TEXT_HYGIENE_PATTERNS entry {kind!r} must map a non-empty string kind to a string pattern.",
                    id="epicurrents.E011",
                )
            )
            continue
        try:
            re.compile(pattern)
        except re.error as exc:
            issues.append(
                Error(
                    f"TEXT_HYGIENE_PATTERNS[{kind!r}] does not compile: {exc}",
                    id="epicurrents.E012",
                )
            )
    return issues


@register(Tags.security)
def check_assessment_currency(app_configs, **kwargs):
    """Warn when the anonymisation assessment is past its review date.

    A warning rather than an error: an overdue review is a document to re-read, not a reason to
    keep a deployment from booting, and the pressure it applies is that every ``manage.py check``
    and every deploy repeats it until ``ASSESSMENT_REVIEWED_ON`` moves. The date moves in
    ``epicurrents/assessment.py`` and in the document's Currency block together.
    """
    currency = assessment_currency()
    if not currency["overdue"]:
        return []
    return [
        Warning(
            f"The anonymisation assessment was last reviewed on {currency['reviewed_on'].isoformat()} and its "
            f"review was due on {currency['review_by'].isoformat()}, {-currency['days_remaining']} day(s) ago.",
            hint=(
                "Re-read docs/anonymisation-compliance.md against the current serving surfaces and the current "
                f"version of the {currency['guidelines']} (the assessment cites version "
                f"{currency['guidelines_version']}, {currency['guidelines_status']}), then move reviewed_on in "
                "its Currency block and ASSESSMENT_REVIEWED_ON in epicurrents/assessment.py in the same commit."
            ),
            id="epicurrents.W020",
        )
    ]
