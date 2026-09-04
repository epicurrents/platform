"""Base :class:`~django.apps.AppConfig` for Epicurrents plugins.

A *plugin* is a Django app under ``plugins/<name>/`` that composes with the
active project rather than defining the deployment's purpose. Unlike a project
(of which exactly one is active per deployment, selected by
``EPICURRENTS_PROJECT``), zero or more plugins may be enabled at once via the
``EPICURRENTS_PLUGINS`` environment variable.

Every plugin's ``apps.py`` subclasses :class:`PluginConfig` instead of the bare
:class:`~django.apps.AppConfig`. The extra class attributes declared here are
read by :mod:`epicurrents.plugin_loader` at boot to validate dependencies and
URL-prefix uniqueness before the first request is served.

See ``docs/plugins.md`` for the operator- and author-facing contract.
"""

from __future__ import annotations

from django.apps import AppConfig


class PluginConfig(AppConfig):
    """App config every plugin subclasses.

    Subclasses set the standard :class:`~django.apps.AppConfig` attributes
    (``name = "plugins.<name>"``, ``label``, ``default_auto_field``) **and
    must set** ``default = True``. Importing :class:`PluginConfig` puts a
    second ``AppConfig`` subclass in the ``apps.py`` namespace, which makes
    Django's config auto-detection ambiguous — and on ambiguity without an
    explicit default it silently instantiates the bare ``AppConfig``, so
    :meth:`ready` never runs and none of the plugin's wiring happens.
    ``validate_plugins`` turns that silent failure into a boot error.

    Wiring done in :meth:`ready` (signal registration, audit masking, erasure
    registration) follows the same rules as any core app.

    Attributes:
        plugin_url_namespace: URL-mount segment for this plugin. When ``None``
            (the default), the loader derives it from the plugin's short name
            (the part after ``plugins.``), so a plugin ``plugins.dicom`` mounts
            at ``/plugin/dicom/``. Set this only to override the derived value.
        requires: Names of core Django apps and other plugins this plugin
            depends on. Core apps are always available; required *plugins* must
            themselves be listed in ``EPICURRENTS_PLUGINS`` or boot fails with a
            clear message. Names may be given short (``"dicom"``) or fully
            qualified (``"plugins.dicom"``); the loader normalises both.
    """

    plugin_url_namespace: str | None = None
    requires: list[str] = []

    @property
    def short_name(self) -> str:
        """The plugin's name without the ``plugins.`` prefix.

        ``plugins.dicom`` → ``dicom``. Used for the default URL mount segment
        and for human-facing messages.
        """
        return self.name.split(".", 1)[-1]

    @property
    def url_namespace(self) -> str:
        """Resolved URL-mount segment — explicit override or derived short name."""
        return self.plugin_url_namespace or self.short_name
