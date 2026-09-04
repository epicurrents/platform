"""Tests for ``epicurrents.log_formatters.JSONLogFormatter``.

The production log stream is the SIEM rule surface: alert rules pivot on
``security_event_type`` (and other structured fields) as discrete JSON keys.
The emission contract (``test_security_log_emission.py``) proves those fields
reach the LogRecord; this file proves the formatter actually serialises them
into the emitted line — the half that makes them shippable.
"""

import json
import logging

from epicurrents.log_formatters import JSONLogFormatter
from epicurrents.security_log import log_security_event


def _format_one(record: logging.LogRecord) -> dict:
    return json.loads(JSONLogFormatter().format(record))


def test_base_fields_present():
    record = logging.LogRecord(
        name="recordings.tasks",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="processing %d",
        args=(42,),
        exc_info=None,
    )
    out = _format_one(record)
    assert out["level"] == "INFO"
    assert out["logger"] == "recordings.tasks"
    assert out["message"] == "processing 42"
    assert "time" in out


def test_security_event_type_is_a_top_level_field(caplog):
    """The structured key must land as a discrete JSON key, not only inside
    the message string — this is what a SIEM rule filters on."""
    caplog.set_level(logging.WARNING, logger="epicurrents.security")
    log_security_event("federation.auth_failed", actor_id=7, reason="signature")
    record = [r for r in caplog.records if r.name == "epicurrents.security"][-1]

    out = _format_one(record)
    assert out["security_event_type"] == "federation.auth_failed"
    assert out["actor_id"] == 7
    assert out["reason"] == "signature"
    assert out["logger"] == "epicurrents.security"


def test_non_serialisable_extra_falls_back_to_repr():
    class Weird:
        def __repr__(self):
            return "<weird>"

    record = logging.LogRecord(
        name="x",
        level=logging.WARNING,
        pathname=__file__,
        lineno=1,
        msg="m",
        args=(),
        exc_info=None,
    )
    record.obj = Weird()
    out = _format_one(record)
    assert out["obj"] == "<weird>"


def test_exception_info_is_included():
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        record = logging.LogRecord(
            name="x",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="failed",
            args=(),
            exc_info=sys.exc_info(),
        )
    out = _format_one(record)
    assert "ValueError: boom" in out["exc_info"]


def test_format_never_raises_on_bad_payload():
    """A formatter that raises takes the log handler down; guard holds even
    when an extra is itself unserialisable in a way default=repr can't fix."""
    record = logging.LogRecord(
        name="x",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="m",
        args=(),
        exc_info=None,
    )
    # A key whose repr is fine but value is deeply recursive.
    recursive = {}
    recursive["self"] = recursive
    record.payload = recursive
    # Should not raise; json.dumps(default=repr) handles the cycle via repr.
    line = JSONLogFormatter().format(record)
    assert isinstance(line, str) and line.startswith("{")
