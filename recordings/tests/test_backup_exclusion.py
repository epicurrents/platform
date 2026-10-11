"""The submission spool's table is the one the database backups leave the rows out of.

The borgmatic emitter and ``scripts/update.sh`` name the table as a string, so a
renamed model or an explicit ``db_table`` would leave both excluding a table that no
longer exists while the spool's rows went into every dump.
"""

import importlib.util
from pathlib import Path

from recordings.models import SubmissionFile

_EMITTER_PATH = Path(__file__).resolve().parents[2] / "borgmatic" / "emit_runtime_config.py"


def _excluded_tables() -> tuple[str, ...]:
    spec = importlib.util.spec_from_file_location("borgmatic_emit_runtime_config", _EMITTER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.EXCLUDED_TABLE_DATA


def test_the_spool_table_is_excluded_from_the_backups():
    assert SubmissionFile._meta.db_table in _excluded_tables()
