"""Annotations app — generic annotation and classification-code system.

Provides five model types (``Annotation``, ``Event``, ``Interruption``, ``Label``,
``Code``) that attach to any platform object via content types.  Signals handle
cascade-deletion of annotations when targets are deleted, and hash recomputation
when codes change.  API endpoints live at ``/annotations/api/v1/``.
"""

from django.apps import AppConfig


class AnnotationsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "annotations"

    def ready(self):
        import annotations.signals  # noqa: F401
