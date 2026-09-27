"""What a failed maintenance notification is allowed to do, and to say.

Two properties, both invisible in ordinary operation. A relay that rejects the
message must not take the job's state with it — a maintenance job's outcome
cannot depend on mail working. And the failure must not put a superuser's
address into the log stream: the traceback of an SMTP rejection quotes the
address it rejected, so a handler that logs one publishes it.

The second property is also why the send is not silenced inside the backend. A
swallowed failure never reaches the handler that records it, and the notice that
would go missing is ``awaiting_verification`` — the one message whose absence
decides an outcome, since an update nobody confirms is rolled back when the
window closes.
"""

import logging
import smtplib

import pytest
from django.core.mail.backends.base import BaseEmailBackend

from maintenance.models import MaintenanceJob
from maintenance.notify import send_notice

SUPERUSER_ADDRESS = "root@example.org"


class _RefusingBackend(BaseEmailBackend):
    def send_messages(self, email_messages):
        raise smtplib.SMTPRecipientsRefused(
            {SUPERUSER_ADDRESS: (550, f"5.1.1 unknown recipient: {SUPERUSER_ADDRESS}".encode())}
        )


@pytest.fixture
def addressed_superuser(superuser):
    superuser.email = SUPERUSER_ADDRESS
    superuser.save()
    return superuser


@pytest.mark.django_db
class TestMailFailureIsContained:
    def _job(self):
        return MaintenanceJob.objects.create(operation="platform.update", executor="host", state="running")

    def test_a_refused_send_is_contained(self, addressed_superuser, no_push, settings, caplog):
        """The state was saved before the notice was queued; the send failing must not raise into the worker."""
        settings.EMAIL_BACKEND = "maintenance.tests.test_notify_failure._RefusingBackend"
        with caplog.at_level(logging.WARNING):
            delivered = send_notice(self._job(), "awaiting_verification")
        assert delivered == 0

    def test_the_address_never_reaches_the_log(self, addressed_superuser, no_push, settings, caplog):
        settings.EMAIL_BACKEND = "maintenance.tests.test_notify_failure._RefusingBackend"
        with caplog.at_level(logging.WARNING):
            send_notice(self._job(), "failed")
        logged = "\n".join(record.getMessage() for record in caplog.records)
        assert SUPERUSER_ADDRESS not in logged
        assert "5.1.1" not in logged

    def test_no_traceback_is_attached(self, addressed_superuser, no_push, settings, caplog):
        settings.EMAIL_BACKEND = "maintenance.tests.test_notify_failure._RefusingBackend"
        with caplog.at_level(logging.WARNING):
            send_notice(self._job(), "failed")
        assert [record for record in caplog.records if record.exc_info] == []

    def test_the_failure_is_recorded_at_all(self, addressed_superuser, no_push, settings, caplog):
        """The shared path logs the hashed recipient; this adds which job it was."""
        settings.EMAIL_BACKEND = "maintenance.tests.test_notify_failure._RefusingBackend"
        job = self._job()
        with caplog.at_level(logging.WARNING):
            send_notice(job, "failed")
        logged = "\n".join(record.getMessage() for record in caplog.records)
        assert str(job.job_id) in logged
        assert "SMTPRecipientsRefused" in logged


class _RecordingBackend(BaseEmailBackend):
    sent: list = []

    def send_messages(self, email_messages):
        type(self).sent.extend(email_messages)
        return len(email_messages)


@pytest.mark.django_db
def test_every_superuser_gets_a_message_of_their_own(make_superuser, no_push, settings):
    """A shared To line would hand every superuser's address to every other, and one refusal would fail them all."""
    settings.EMAIL_BACKEND = "maintenance.tests.test_notify_failure._RecordingBackend"
    _RecordingBackend.sent = []
    for index in range(3):
        make_superuser(email=f"root{index}@example.org")
    job = MaintenanceJob.objects.create(operation="platform.update", executor="host", state="failed")
    assert send_notice(job, "failed") == 3
    assert sorted(tuple(message.to) for message in _RecordingBackend.sent) == [
        ("root0@example.org",),
        ("root1@example.org",),
        ("root2@example.org",),
    ]
