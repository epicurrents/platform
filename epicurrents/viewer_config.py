"""Loading and merging the viewer configuration served to the frontend.

The effective viewer config is the active project's
``projects/<name>/viewer-config.json`` seed merged with the editable
:class:`~epicurrents.models.ViewerConfigOverride` row for that project (the
overrides win). Both are flat maps of dotted-path settings field to value
(``{"eeg.defaultMontage": "lon"}``). The seed is read live on every request so a
project-source update to the defaults applies immediately, while the database
keeps the operator's live tweaks layered on top.
"""

import json
import logging
from pathlib import Path

from epicurrents.models import ViewerConfigOverride
from epicurrents.project_loader import get_active_project

logger = logging.getLogger(__name__)

_PROJECTS_DIR = Path(__file__).resolve().parent.parent / "projects"


def load_viewer_config_seed(project: str) -> dict:
    """Return a project's ``viewer-config.json`` seed, ``{}`` if absent or invalid.

    A missing file is the normal case (a project without viewer defaults) and is
    silent; a malformed file is logged but still treated as empty so a bad seed
    never breaks the viewer.
    """
    if not project:
        return {}
    seed_path = _PROJECTS_DIR / project / "viewer-config.json"
    try:
        with seed_path.open() as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, OSError):
        logger.warning("Malformed viewer-config.json seed for project %r; ignoring.", project)
        return {}
    return data if isinstance(data, dict) else {}


def get_project_overrides(project: str) -> dict:
    """Return the editable database overrides for a project, ``{}`` if none."""
    if not project:
        return {}
    row = ViewerConfigOverride.objects.filter(project=project).first()
    return dict(row.overrides) if row and isinstance(row.overrides, dict) else {}


def get_effective_viewer_config() -> dict:
    """Return the seed merged with the active project's overrides (overrides win)."""
    project = get_active_project()
    return {**load_viewer_config_seed(project), **get_project_overrides(project)}


def is_valid_overrides(value: object) -> bool:
    """Return True when *value* is a flat map of string field names to values.

    The single shape rule for every viewer-config override layer — the project
    overrides row and the per-dataset overrides both store the same flat
    dotted-path → value map. Value types are not constrained here; the viewer
    validates each field against its own settings schema when it applies them.
    """
    return isinstance(value, dict) and all(isinstance(key, str) for key in value)
