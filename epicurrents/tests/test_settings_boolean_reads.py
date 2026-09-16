"""Contract test: settings modules read booleans through env_bool, never config(cast=bool).

decouple's boolean cast maps an empty value to False, and a key written with no value is
present rather than absent, so the declared default never applies. ``NAME=`` in .env
therefore reads as a decision to turn the setting off. Where the default is the safe
direction that silently selects the unsafe one — a bare SESSION_CSRF_ENFORCED line
disables the CSRF chokepoint on every session-authenticated write, and the production
transport flags downgrade every cookie.

env_bool treats an empty value as unanswered. This scan is what stops the raw shape
coming back: the next boolean setting is written by copying the line above it.
"""

from pathlib import Path

import pytest

from epicurrents.settings import env

SETTINGS_DIR = Path(env.__file__).parent

# env.py itself is the one place the raw cast belongs — it applies the cast only after
# ruling out the empty value, which is the whole point of the helper.
EXEMPT = {"env.py"}


def _settings_modules():
    return sorted(path for path in SETTINGS_DIR.glob("*.py") if path.name not in EXEMPT)


def test_the_scan_covers_the_settings_modules():
    """Guard the guard: an empty or mis-pointed glob would pass every assertion below."""
    names = {path.name for path in _settings_modules()}

    assert {"common.py", "development.py", "production.py"} <= names


@pytest.mark.parametrize("path", _settings_modules(), ids=lambda path: path.name)
def test_no_settings_module_casts_a_boolean_directly(path):
    source = path.read_text(encoding="utf-8")

    assert "cast=bool" not in source, (
        f"{path.name} reads a boolean with config(..., cast=bool), which reads a bare "
        "NAME= line in .env as False and discards the declared default. Use env_bool "
        "from epicurrents.settings.env instead."
    )
