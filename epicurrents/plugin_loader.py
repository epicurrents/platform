"""Plugin loader — enables zero or more plugins on top of the base settings.

Companion to :mod:`epicurrents.project_loader`. Where a *project* is the single
customisation layer that defines a deployment's purpose (selected by
``EPICURRENTS_PROJECT``), a *plugin* is a composable add-on: zero or more may be
enabled at once via the ``EPICURRENTS_PLUGINS`` environment variable, and none
of them owns the deployment's landing page or primary UX.

A plugin lives under ``plugins/<name>/`` in the repository root and may provide:

- ``apps.py``      — :class:`~epicurrents.plugins.PluginConfig` subclass (required)
- ``settings.py``  — additional / overriding Django settings
- ``models.py`` / ``migrations/`` — extra database models
- ``urls.py``      — Ninja API, mounted at ``/plugin/<name>/api/v1/``
- ``public_urls.py`` — plain Django URLs, mounted at ``/plugin/<name>/``
- ``signals.py`` / ``tasks.py`` / ``management/commands/``

Two entry points:

- :func:`apply_plugin_settings` runs at *settings-import* time (called from
  ``epicurrents/settings/common.py``). It registers each plugin app and merges
  its ``settings.py``. Settings precedence is
  ``common < plugins < project < .env``: plugins merge before the active
  project so a project always has the last word.
- :func:`validate_plugins` runs at *apps-ready* time (called from
  ``EpicurrentsConfig.ready``). It resolves declared ``requires`` dependencies
  and checks for URL-namespace collisions, raising a clear
  :class:`~django.core.exceptions.ImproperlyConfigured` at boot rather than
  letting a misconfiguration surface as a 500 on the first request.

Lifecycle management (enable / disable, submodule checkout) is handled by the
``scripts/enable_plugin.sh`` and ``scripts/disable_plugin.sh`` helpers, mirroring
``scripts/switch_project.sh`` for projects.
"""

from __future__ import annotations

import importlib
import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

# Reuse the project loader's merge-strategy key sets so plugin settings merge
# exactly like project settings — one source of truth for "which settings
# append vs. merge vs. replace".
from epicurrents.project_loader import _DICT_KEYS, _LIST_KEYS

# Absolute path to the repository root (parent of this file's package dir).
_BASE_DIR = Path(__file__).resolve().parent.parent


def get_active_plugins() -> list[str]:
    """Return the ordered list of enabled plugin short-names.

    Parsed from ``EPICURRENTS_PLUGINS`` — a comma-separated list. Whitespace
    around each entry is stripped, empty entries dropped, and order preserved
    (order fixes both settings-merge precedence and URL-mount order). Duplicate
    names are collapsed to their first occurrence.
    """
    raw = os.getenv("EPICURRENTS_PLUGINS", "")
    seen: set[str] = set()
    names: list[str] = []
    for part in raw.split(","):
        name = part.strip()
        if not name or name in seen:
            continue
        seen.add(name)
        names.append(name)
    return names


def _plugin_dir(name: str) -> Path:
    """Return the filesystem path to ``plugins/<name>/``."""
    return _BASE_DIR / "plugins" / name


def apply_plugin_settings(globs: dict) -> None:
    """Merge every enabled plugin's settings into *globs*.

    *globs* should be ``globals()`` from a Django settings module. For each name
    in ``EPICURRENTS_PLUGINS``, in order, the function:

    1. Validates that ``plugins/<name>/`` exists and contains ``apps.py``.
    2. Appends ``"plugins.<name>"`` to ``INSTALLED_APPS``.
    3. Imports ``plugins.<name>.settings`` (if present) and merges its public
       names using the same list / dict / scalar strategy as the project loader.

    A no-op when ``EPICURRENTS_PLUGINS`` is empty. Must be called *before*
    ``apply_project_settings`` so the active project's settings win over any
    plugin's.
    """
    for name in get_active_plugins():
        plugin_dir = _plugin_dir(name)
        if not plugin_dir.is_dir():
            raise ImproperlyConfigured(
                f"EPICURRENTS_PLUGINS lists {name!r} but plugins/{name}/ does "
                "not exist. Fix the EPICURRENTS_PLUGINS value in .env."
            )
        if not (plugin_dir / "apps.py").exists():
            raise ImproperlyConfigured(f"plugins/{name}/apps.py is missing — every plugin must define a PluginConfig.")

        installed: list = globs.setdefault("INSTALLED_APPS", [])
        plugin_app = f"plugins.{name}"
        if plugin_app not in installed:
            installed.append(plugin_app)

        try:
            plugin_settings = importlib.import_module(f"plugins.{name}.settings")
        except ModuleNotFoundError:
            continue

        for key, value in vars(plugin_settings).items():
            if key.startswith("_"):
                continue
            if key in _LIST_KEYS and key in globs and isinstance(globs[key], list):
                existing: list = globs[key]
                globs[key] = existing + [v for v in value if v not in existing]
            elif key in _DICT_KEYS and key in globs and isinstance(globs[key], dict):
                globs[key] = {**globs[key], **value}
            else:
                globs[key] = value


def validate_plugins() -> None:
    """Validate enabled plugins once the app registry is populated.

    Called from ``EpicurrentsConfig.ready``. Checks, for every enabled plugin:

    - **Dependencies.** Each entry in the plugin's ``requires`` list resolves to
      either a loaded core Django app or another *enabled* plugin. A required
      plugin that is not itself in ``EPICURRENTS_PLUGINS`` is a hard error —
      enabling B without its dependency A would fail unpredictably at runtime.
    - **URL-namespace uniqueness.** No two enabled plugins may resolve to the
      same ``/plugin/<namespace>/`` mount segment.

    Any violation raises :class:`~django.core.exceptions.ImproperlyConfigured`
    with a message that names the offending plugin and the fix, so the operator
    edits ``EPICURRENTS_PLUGINS`` rather than debugging a first-request 500.
    """
    from django.apps import apps

    from epicurrents.plugins import PluginConfig

    active = get_active_plugins()
    if not active:
        return

    active_set = set(active)
    # Every non-plugin app label and dotted name counts as an available
    # dependency target; a plugin may declare a core app in ``requires``.
    core_names: set[str] = set()
    for cfg in apps.get_app_configs():
        if isinstance(cfg, PluginConfig):
            continue
        core_names.add(cfg.label)
        core_names.add(cfg.name)

    namespaces: dict[str, str] = {}
    for name in active:
        try:
            cfg = apps.get_app_config(name)
        except LookupError:
            # apply_plugin_settings already validated the directory; a missing
            # app config here means apps.py did not register a config for the
            # expected label. Surface it explicitly.
            raise ImproperlyConfigured(
                f"Plugin {name!r} is enabled but no AppConfig with label "
                f"{name!r} is registered. Ensure plugins/{name}/apps.py sets "
                f'label = "{name}".'
            )

        if not isinstance(cfg, PluginConfig):
            # Django's config auto-detection silently falls back to the bare
            # AppConfig when apps.py holds more than one AppConfig subclass
            # (PluginConfig is imported there) and none declares an explicit
            # default — ready() then never runs and the plugin's wiring
            # (signals, permission extensions, audit masking) is silently
            # absent. Fail loudly instead.
            raise ImproperlyConfigured(
                f"Plugin {name!r} loaded as {type(cfg).__name__!r} instead of "
                "its PluginConfig subclass. Ensure the config class in "
                f"plugins/{name}/apps.py subclasses PluginConfig and sets "
                "default = True."
            )

        requires = list(getattr(cfg, "requires", []) or [])
        for dep in requires:
            short = dep.split(".", 1)[-1]
            satisfied = dep in core_names or short in core_names or short in active_set or dep in active_set
            if not satisfied:
                raise ImproperlyConfigured(
                    f"Plugin {name!r} requires {dep!r}, which is neither a "
                    "loaded core app nor an enabled plugin. Add it to "
                    "EPICURRENTS_PLUGINS or remove the dependency."
                )

        namespace = getattr(cfg, "url_namespace", name)
        if namespace in namespaces:
            raise ImproperlyConfigured(
                f"Plugins {namespaces[namespace]!r} and {name!r} both claim the "
                f"URL namespace '/plugin/{namespace}/'. Give one an explicit "
                "plugin_url_namespace so the mounts do not collide."
            )
        namespaces[namespace] = name
