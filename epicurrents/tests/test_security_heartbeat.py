"""Tests for the security-stream heartbeat.

The heartbeat is the positive signal an off-host log sink keys its dead-man
rules on, so the properties that matter are the ones a receiver depends on: it
reaches the ``epicurrents.security`` logger under the documented event type, it
publishes the interval so the receiving window can be derived rather than
duplicated, and it carries nothing that identifies anybody — it is shipped to a
host outside the deployment precisely because it cannot.
"""

import logging

from django.test import override_settings

from epicurrents.tasks import SECURITY_HEARTBEAT_INTERVAL_SECONDS, emit_security_heartbeat


def _capture(caplog):
    return [r for r in caplog.records if r.name == "epicurrents.security"]


def test_emits_on_the_security_logger_with_the_documented_event_type(caplog):
    with caplog.at_level(logging.WARNING, logger="epicurrents.security"):
        emit_security_heartbeat()

    records = _capture(caplog)
    assert len(records) == 1
    assert records[0].security_event_type == "system.heartbeat"


def test_publishes_the_configured_interval(caplog):
    schedule = {"emit-security-heartbeat": {"task": "epicurrents.tasks.emit_security_heartbeat", "schedule": 42}}
    with override_settings(CELERY_BEAT_SCHEDULE=schedule):
        with caplog.at_level(logging.WARNING, logger="epicurrents.security"):
            result = emit_security_heartbeat()

    assert result == {"interval_seconds": 42}
    assert _capture(caplog)[0].interval_seconds == 42


def test_falls_back_when_the_beat_entry_is_missing(caplog):
    # Rescheduling the task by hand — or renaming the beat entry — must not
    # make the event lie about its interval or omit the field the receiver
    # derives its alerting window from.
    with override_settings(CELERY_BEAT_SCHEDULE={}):
        with caplog.at_level(logging.WARNING, logger="epicurrents.security"):
            result = emit_security_heartbeat()

    assert result == {"interval_seconds": SECURITY_HEARTBEAT_INTERVAL_SECONDS}
    assert _capture(caplog)[0].interval_seconds == SECURITY_HEARTBEAT_INTERVAL_SECONDS


def test_carries_no_identifiers(caplog):
    # The stream leaves the deployment for an off-premises sink, so this event
    # is held to the stricter of the two rules: not "no raw PII", but nothing
    # that identifies a person, an account or a path at all.
    with caplog.at_level(logging.WARNING, logger="epicurrents.security"):
        emit_security_heartbeat()

    record = _capture(caplog)[0]
    forbidden = {"actor_id", "ip", "path", "peer_url", "peer_id", "target_id", "username", "email"}
    assert not forbidden.intersection(vars(record))


def test_registered_in_the_beat_schedule():
    from django.conf import settings

    entry = settings.CELERY_BEAT_SCHEDULE["emit-security-heartbeat"]
    assert entry["task"] == "epicurrents.tasks.emit_security_heartbeat"
    # A heartbeat slower than the receiver's window turns every quiet period
    # into a page. The evidence-host rule allows 20 minutes; keep a margin of
    # several beats inside it.
    assert entry["schedule"] <= 600


def test_non_numeric_schedule_does_not_stop_the_heartbeat(caplog):
    # django-celery-beat accepts crontab and solar schedules. int() on one
    # raises, and a heartbeat that raises stops arriving — which at the
    # receiving end is indistinguishable from the host being compromised.
    # Fail towards still beating.
    class Crontab:
        def __str__(self):
            return "crontab(*/5 * * * *)"

    schedule = {"emit-security-heartbeat": {"task": "epicurrents.tasks.emit_security_heartbeat", "schedule": Crontab()}}
    with override_settings(CELERY_BEAT_SCHEDULE=schedule):
        with caplog.at_level(logging.WARNING, logger="epicurrents.security"):
            result = emit_security_heartbeat()

    assert result == {"interval_seconds": SECURITY_HEARTBEAT_INTERVAL_SECONDS}
    assert _capture(caplog)[0].security_event_type == "system.heartbeat"
