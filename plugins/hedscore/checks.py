"""System checks for the *hedscore* plugin."""

from django.conf import settings
from django.core.checks import Error, register


@register()
def check_allowed_branches(app_configs=None, **kwargs):
    """Refuse a ``HEDSCORE_ALLOWED_BRANCHES`` entry the pinned schema has no branch for.

    A misspelt branch would refuse every code in it, so the deployment would learn of the typo only from a reader's
    422. Reported at ``manage.py check`` instead.
    """
    from plugins.hedscore.schema import load_schema

    branches = load_schema().branches
    unknown = [name for name in getattr(settings, "HEDSCORE_ALLOWED_BRANCHES", ()) if name not in branches]
    if not unknown:
        return []
    return [
        Error(
            f"HEDSCORE_ALLOWED_BRANCHES names {', '.join(unknown)}, which the pinned schema has no branch for",
            hint=f"Use top-level branch names of the schema: {', '.join(branches)}.",
            id="hedscore.E001",
        )
    ]
