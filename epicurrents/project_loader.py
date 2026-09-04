"""Project loader — activates a project on top of the base settings.

Called from ``epicurrents/settings/common.py`` at module load time via
``apply_project_settings(globals())``.  The active project is identified by
the ``EPICURRENTS_PROJECT`` environment variable.

A *project* is a self-contained customisation layer that lives under
``projects/<name>/`` in the repository root.  It may provide:

- ``settings.py``  — additional or overriding Django settings
- ``models.py`` / ``migrations/`` — extra database models with FKs to base models
- ``middleware.py`` — :class:`~federation.middleware.EDFHeaderMiddleware` /
  :class:`~federation.middleware.EDFSignalMiddleware` subclasses
- ``urls.py`` — Ninja API or Django endpoints (mounted at ``/project/api/v1/``)
- ``apps.py`` — :class:`~django.apps.AppConfig` (required)

Only one project can be active at a time.  The active project is determined
exclusively by the ``EPICURRENTS_PROJECT`` environment variable; the variable
must be set *before* the Django process starts (e.g. in ``.env``).

Lifecycle management (table archiving, restoration, removal) is handled by the
management commands ``activate_project``, ``deactivate_project``, and
``remove_project_data``.
"""

from __future__ import annotations

import importlib
import json
import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

# Absolute path to the repository root (parent of this file's package dir).
_BASE_DIR = Path(__file__).resolve().parent.parent

# Path to the project lifecycle state registry.
STATE_FILE = _BASE_DIR / "projects" / ".state.json"

# Table-name prefix used by deactivate_project to archive project tables,
# and matched by activate_project / remove_project_data when restoring or
# dropping them.  Defined once here so the three commands cannot drift
# apart.
ARCHIVE_PREFIX = "_archived_"

# ── Settings merge strategy ───────────────────────────────────────────────────
#
# List-typed settings: project entries are *appended* (duplicates skipped).
# Dict-typed settings: project dict is *merged* into the base dict (project
#   wins on key conflicts).
# All other settings: project value *replaces* the base value.
#
_LIST_KEYS: frozenset[str] = frozenset(
    {
        "INSTALLED_APPS",
        "MIDDLEWARE",
        "AUTH_PASSWORD_VALIDATORS",
        "AUTHENTICATION_BACKENDS",
        "PASSWORD_HASHERS",
    }
)
_DICT_KEYS: frozenset[str] = frozenset({"CELERY_BEAT_SCHEDULE"})


def get_active_project() -> str:
    """Return the active project name from ``EPICURRENTS_PROJECT``, or ``""``."""
    return os.getenv("EPICURRENTS_PROJECT", "").strip()


def apply_project_settings(globs: dict) -> None:
    """Merge the active project's settings into *globs*.

    *globs* should be ``globals()`` from a Django settings module (e.g.
    ``epicurrents/settings/common.py``).  The function:

    1. Validates that ``projects/<name>/`` exists.
    2. Appends ``"projects.<name>"`` to ``INSTALLED_APPS``.
    3. Imports ``projects.<name>.settings`` (if present) and merges its public
       names into *globs* using the list / dict / scalar strategy described at
       the top of this module.

    Calling this with no ``EPICURRENTS_PROJECT`` set is a no-op.
    """
    name = get_active_project()
    if not name:
        return

    project_dir = _BASE_DIR / "projects" / name
    if not project_dir.is_dir():
        raise ImproperlyConfigured(f"EPICURRENTS_PROJECT={name!r} is set but projects/{name}/ does not exist.")
    if not (project_dir / "apps.py").exists():
        raise ImproperlyConfigured(f"projects/{name}/apps.py is missing — every project must define an AppConfig.")

    # Register the project as a Django app before merging its settings so that
    # if the project's settings.py itself extends INSTALLED_APPS with extra
    # third-party apps, those appear after the project app in the list.
    installed: list = globs.setdefault("INSTALLED_APPS", [])
    project_app = f"projects.{name}"
    if project_app not in installed:
        installed.append(project_app)

    # Merge project settings (optional — projects without a settings.py are valid).
    try:
        proj_mod = importlib.import_module(f"projects.{name}.settings")
    except ModuleNotFoundError:
        return

    for key, value in vars(proj_mod).items():
        if key.startswith("_"):
            continue
        if key in _LIST_KEYS and key in globs and isinstance(globs[key], list):
            # Extend, preserving order and skipping duplicates.
            existing: list = globs[key]
            globs[key] = existing + [v for v in value if v not in existing]
        elif key in _DICT_KEYS and key in globs and isinstance(globs[key], dict):
            globs[key] = {**globs[key], **value}
        else:
            globs[key] = value


# ── State file helpers ────────────────────────────────────────────────────────
#
# These are used exclusively by the management commands (activate_project,
# deactivate_project, remove_project_data).  The Django server itself does not
# read the state file.
#
# State entry schema:
#   {
#     "<project_name>": {
#       "status": "archived" | "active",
#       "archived_at": "<ISO-8601 timestamp> | null"
#     }
#   }


def get_project_state() -> dict:
    """Return the contents of ``projects/.state.json``, or ``{}`` if absent.

    A missing file is treated as "no projects have been lifecycle-managed
    yet" and returns an empty dict.  A malformed file (truncated write,
    manual edit that broke JSON) is *not* swallowed — the
    ``json.JSONDecodeError`` propagates so the operator sees corruption
    loudly rather than silently losing the archive registry.
    """
    try:
        return json.loads(STATE_FILE.read_text())
    except FileNotFoundError:
        return {}


def set_project_state(state: dict) -> None:
    """Write *state* to ``projects/.state.json``."""
    STATE_FILE.write_text(json.dumps(state, indent=2, default=str) + "\n")


def rename_db_table(cursor, old: str, new: str) -> None:
    """Rename a database table using a vendor-appropriate DDL statement.

    Supports PostgreSQL and SQLite (both use ``ALTER TABLE … RENAME TO``).
    Used by the ``activate_project`` and ``deactivate_project`` management
    commands when archiving or restoring project tables.

    Identifier quoting goes through ``connection.ops.quote_name`` rather
    than a manual f-string so the vendor-correct quoting style is used
    and any pathological characters in *old* / *new* (a ``"`` inside a
    project name, for example) cannot break out of the identifier and
    inject SQL.  Current call sites only ever pass Django-introspected
    table names + a fixed ``_archived_`` prefix, but the function is a
    public utility and should defend the contract regardless.

    Raises ``django.core.management.base.CommandError`` for unsupported vendors.
    """
    from django.core.management.base import CommandError
    from django.db import connection

    vendor = connection.vendor
    if vendor in {"postgresql", "sqlite"}:
        quote = connection.ops.quote_name
        cursor.execute(f"ALTER TABLE {quote(old)} RENAME TO {quote(new)}")
    else:
        raise CommandError(f"Unsupported database vendor {vendor!r}. Manual table rename required.")
