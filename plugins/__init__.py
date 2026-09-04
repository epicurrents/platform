"""Namespace package for Epicurrents plugins.

Each subdirectory ``plugins/<name>/`` is a composable Django app enabled via the
``EPICURRENTS_PLUGINS`` environment variable. See ``plugins/README.md`` and
``docs/plugins.md`` for the plugin contract, and :mod:`epicurrents.plugin_loader`
for the machinery that loads and validates them.
"""
