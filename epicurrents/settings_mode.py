"""Settings-module selector — resolves ``DJANGO_MODE`` to the matching settings module path.

``DJANGO_MODE`` takes precedence over ``DJANGO_SETTINGS_MODULE`` when both
are set; conflicting values emit a ``RuntimeWarning``.

A misspelled ``DJANGO_MODE`` (e.g. ``"prod"`` instead of ``"production"``)
raises immediately rather than silently falling back to development — a
fallback would boot a container with ``DEBUG=True``, the placeholder
``SECRET_KEY``, and the local-memory cache while the operator believed
they were in production.
"""

import os
import warnings

from django.core.exceptions import ImproperlyConfigured

_VALID_MODES = frozenset({"development", "production"})


def get_settings_module() -> str:
    """Resolve settings module, preferring DJANGO_MODE over DJANGO_SETTINGS_MODULE.

    Raises :class:`ImproperlyConfigured` when ``DJANGO_MODE`` is set to an
    unrecognised value. An empty / unset ``DJANGO_MODE`` is fine — it
    falls back to ``DJANGO_SETTINGS_MODULE`` or the development default.
    """

    mode = os.getenv("DJANGO_MODE", "").strip().lower()
    explicit_module = os.getenv("DJANGO_SETTINGS_MODULE")

    if mode and mode not in _VALID_MODES:
        raise ImproperlyConfigured(
            f"DJANGO_MODE={mode!r} is not a recognised value. "
            f"Expected one of {sorted(_VALID_MODES)!r}, or leave it unset "
            "to fall back to DJANGO_SETTINGS_MODULE / the development "
            "default. Misspelling DJANGO_MODE silently boots the container "
            "in development mode with DEBUG=True and a placeholder "
            "SECRET_KEY, which is rejected here to prevent that footgun."
        )

    if mode in _VALID_MODES:
        mode_module = f"epicurrents.settings.{mode}"
        if explicit_module and explicit_module != mode_module:
            warnings.warn(
                (
                    "Both DJANGO_MODE and DJANGO_SETTINGS_MODULE are set with conflicting values "
                    f"(DJANGO_MODE={mode!r} -> {mode_module}, "
                    f"DJANGO_SETTINGS_MODULE={explicit_module!r}). "
                    "Using DJANGO_MODE-derived settings module."
                ),
                RuntimeWarning,
                stacklevel=2,
            )
        return mode_module

    if explicit_module:
        return explicit_module

    return "epicurrents.settings.development"
