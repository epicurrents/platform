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
from maintenance.notify import notify_job_state

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

    def test_the_state_is_still_announced(self, addressed_superuser, no_push, settings, caplog):
        """The return value is what the caller stores as ``last_notified_state``.

        A failure that returned ``None`` here would leave the job announcing the
        same state again on the next sync, once a minute, for as long as the
        relay stays broken.
        """
        settings.EMAIL_BACKEND = "maintenance.tests.test_notify_failure._RefusingBackend"
        with caplog.at_level(logging.WARNING):
            announced = notify_job_state(self._job(), state="awaiting_verification")
        assert announced == "awaiting_verification"

    def test_the_address_never_reaches_the_log(self, addressed_superuser, no_push, settings, caplog):
        settings.EMAIL_BACKEND = "maintenance.tests.test_notify_failure._RefusingBackend"
        with caplog.at_level(logging.WARNING):
            notify_job_state(self._job(), state="failed")
        logged = "\n".join(record.getMessage() for record in caplog.records)
        assert SUPERUSER_ADDRESS not in logged
        assert "5.1.1" not in logged

    def test_no_traceback_is_attached(self, addressed_superuser, no_push, settings, caplog):
        settings.EMAIL_BACKEND = "maintenance.tests.test_notify_failure._RefusingBackend"
        with caplog.at_level(logging.WARNING):
            notify_job_state(self._job(), state="failed")
        assert [record for record in caplog.records if record.exc_info] == []

    def test_the_failure_is_recorded_at_all(self, addressed_superuser, no_push, settings, caplog):
        """The shared path logs the hashed recipient; this adds which job it was."""
        settings.EMAIL_BACKEND = "maintenance.tests.test_notify_failure._RefusingBackend"
        job = self._job()
        with caplog.at_level(logging.WARNING):
            notify_job_state(job, state="failed")
        logged = "\n".join(record.getMessage() for record in caplog.records)
        assert str(job.job_id) in logged
        assert "SMTPRecipientsRefused" in logged
