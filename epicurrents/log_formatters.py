"""Logging formatters for the platform's diagnostic log stream.

``JSONLogFormatter`` renders each record as one JSON object per line,
including any fields passed via ``extra=`` — so the structured security
fields emitted by :func:`epicurrents.security_log.log_security_event`
(``security_event_type`` plus ``actor_id`` / ``ip`` / ``reason`` / …) arrive
as top-level JSON keys a log shipper can label and alert on, not buried
inside the human-readable ``message`` string.

Wired in production via ``epicurrents/settings/production.py``. See
``docs/operations.md`` → *Security log stream* for the shipping / alerting
walkthrough and ``examples/observability/`` for sample configs.
"""

import json
import logging

# Attributes the stdlib sets on every LogRecord. Anything on a record that is
# NOT in this set arrived via ``extra=`` and is emitted as a JSON field.
# Sourced from logging.LogRecord.__init__ plus the attrs the formatter adds
# (``message``, ``asctime``); ``taskName`` exists on Python 3.12+.
_STANDARD_RECORD_ATTRS = frozenset(
    {
        "name",
        "msg",
        "args",
        "levelname",
        "levelno",
        "pathname",
        "filename",
        "module",
        "exc_info",
        "exc_text",
        "stack_info",
        "lineno",
        "funcName",
        "created",
        "msecs",
        "relativeCreated",
        "thread",
        "threadName",
        "processName",
        "process",
        "taskName",
        "asctime",
        "message",
    }
)


class JSONLogFormatter(logging.Formatter):
    """Render a record as a single-line JSON object including ``extra`` fields.

    The base keys are ``time`` / ``level`` / ``logger`` / ``message``; every
    non-standard record attribute (the keys passed via ``extra=``) is added
    as a top-level key. Formatting never raises — non-serialisable values
    fall back to ``repr`` via ``json.dumps(default=...)``, and a final guard
    degrades to the base keys if serialisation fails entirely, because a
    formatter that raises would take the whole log handler down with it.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "time": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD_RECORD_ATTRS and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        try:
            return json.dumps(payload, default=repr)
        except Exception:
            return json.dumps(
                {
                    "time": payload["time"],
                    "level": payload["level"],
                    "logger": payload["logger"],
                    "message": payload["message"],
                }
            )
