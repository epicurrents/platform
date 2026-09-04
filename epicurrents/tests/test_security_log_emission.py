"""Emission-contract tests for ``epicurrents.security_log``.

The taxonomy test (``test_security_log_taxonomy.py``) covers *what*
event types are valid.  This file covers *how* they reach the operator:
the logger name and the structured-log key that SIEM rules pivot on.

Both are operator-visible API.  Renaming either one without
coordinating the SIEM configuration leaves the application logging
happily while every downstream alert rule stops matching — the classic
silent-failure shape that the LOAD-BEARING convention defends against.
"""

import logging

from epicurrents.security_log import log_security_event, logger


def test_logger_name_is_stable():
    """SIEM rules filter on the logger name.  Renaming it is a silent
    SIEM-config break."""
    assert logger.name == "epicurrents.security", (
        f"Logger name has drifted to {logger.name!r}.  Every SIEM rule "
        f"filtering on 'epicurrents.security' now silently misses every "
        f"security event.  Restore the name or coordinate a SIEM-rule "
        f"rename across all deployments."
    )


def test_security_event_type_extra_key_is_emitted(caplog):
    """SIEM alert rules pivot on the ``security_event_type`` field in
    the log record's extra payload.  Renaming the key is a silent
    SIEM-config break — same failure mode as renaming the logger.
    """
    caplog.set_level(logging.WARNING, logger="epicurrents.security")
    log_security_event("auth.login_failed", actor_id=42)

    matching = [rec for rec in caplog.records if rec.name == "epicurrents.security"]
    assert matching, "log_security_event did not emit to the security logger."
    record = matching[-1]
    assert getattr(record, "security_event_type", None) == "auth.login_failed", (
        "The structured-log key 'security_event_type' is missing from the "
        "emitted record.  SIEM rules that key on this field now silently "
        "miss every event.  Restore the key name or coordinate a SIEM-rule "
        "rename across all deployments."
    )


def test_arbitrary_extra_fields_pass_through(caplog):
    """Caller-supplied fields beyond ``event_type`` must reach the log
    record so SIEM rules can filter on them (``actor_id``, ``ip``,
    ``reason``, etc.).  Confirms the ``**fields`` spread into ``extra``
    is intact."""
    caplog.set_level(logging.WARNING, logger="epicurrents.security")
    log_security_event(
        "permission.denied",
        actor_id=99,
        permission="write",
        object_type="Recording",
    )
    record = [rec for rec in caplog.records if rec.name == "epicurrents.security"][-1]
    assert getattr(record, "actor_id", None) == 99
    assert getattr(record, "permission", None) == "write"
    assert getattr(record, "object_type", None) == "Recording"


def test_warning_level_is_used(caplog):
    """Operators wire ``epicurrents.security`` to a WARNING-or-above
    handler.  Emitting at INFO or DEBUG would silently drop below the
    operator's threshold and never reach the SIEM."""
    caplog.set_level(logging.DEBUG, logger="epicurrents.security")
    log_security_event("auth.login_failed")
    record = [rec for rec in caplog.records if rec.name == "epicurrents.security"][-1]
    assert record.levelno == logging.WARNING, (
        f"log_security_event emitted at level {record.levelname!r}; "
        f"WARNING is the contract that operator log routing depends on."
    )


def test_celery_does_not_hijack_the_root_logger():
    """The worker must log through Django's configuration, not Celery's.

    ``epicurrents.security`` has no handler of its own; it propagates to root.
    Celery replaces the root logger's handlers on worker startup unless told
    otherwise, so with the hijack on, every security event emitted from a task
    is formatted by Celery — plain text where the rest of the production stream
    is JSON.

    That silently removes the highest-severity events from any consumer that
    parses the line as JSON. ``audit.chain_break``, ``audit.chain_gap``,
    ``audit.genesis_invalid`` and ``audit.derived_state_mismatch`` all come from
    ``activity.tasks.verify_audit_integrity``, a beat task, and are exactly what
    an off-host log sink exists to receive. Nothing reports the mismatch: the
    events are emitted, the shipper runs, the rules evaluate, and none of them
    match.
    """
    from django.conf import settings

    assert settings.CELERY_WORKER_HIJACK_ROOT_LOGGER is False
