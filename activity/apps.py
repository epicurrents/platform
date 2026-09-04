"""Activity app — audit trail for API requests and model-level change logging.

Registers the ``Activity`` and ``ObjectChangeLog`` models and connects the
signal receivers in ``activity.signals`` so that create/modify/delete events
on all non-excluded models are auto-logged during API requests.
"""

from django.apps import AppConfig


class ActivityConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "activity"

    def ready(self):
        # Both imported for their side effects: `signals` connects the audit
        # receivers, and `checks` registers the erasure-registry system checks
        # via @register. Importing `checks` here rather than at module scope is
        # what makes them run after every app's ready(), so they cover a
        # project's registrations and not just the core ones.
        from . import checks, signals  # noqa: F401
