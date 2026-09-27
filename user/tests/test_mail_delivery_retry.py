"""What the reset task hands to ``retry``, and what it says on the way.

The exception given to ``retry`` is not only a retry signal: celery re-raises it
once the retries run out, and the worker then logs the task failure with a
traceback. So the object passed here is the one that ends up in the log on the
last attempt, and it has to be the sanitised
:class:`~epicurrents.mail.MailDeliveryError` rather than the backend's own
exception, whose text quotes the rejected address.

Exercised through a task double rather than the eager celery path, because what
is under test is the argument ``retry`` receives — which an eager run consumes
without ever showing it.
"""

import logging
import smtplib

import pytest
from django.core.mail.backends.base import BaseEmailBackend

from epicurrents.mail import MailDeliveryError
from user.tasks import _deliver

RECIPIENT = "person@example.org"


class _RefusingBackend(BaseEmailBackend):
    def send_messages(self, email_messages):
        raise smtplib.SMTPRecipientsRefused({RECIPIENT: (550, f"5.1.1 unknown: {RECIPIENT}".encode())})


class _Request:
    def __init__(self, retries):
        self.retries = retries


class _TaskDouble:
    """The three attributes ``_deliver`` uses, and a ``retry`` that records its argument."""

    name = "user.tasks.send_password_reset_email"
    max_retries = 3

    def __init__(self, retries=0):
        self.request = _Request(retries)
        self.retried_with = None

    def retry(self, exc=None):
        self.retried_with = exc
        return RuntimeError("retry signal")


def _deliver_through(task, settings):
    settings.EMAIL_BACKEND = "user.tests.test_mail_delivery_retry._RefusingBackend"
    with pytest.raises(RuntimeError):
        raise _deliver(task, "Subject", "Body", None, [RECIPIENT])


class TestWhatRetryReceives:
    def test_it_is_the_sanitised_error(self, settings):
        task = _TaskDouble()
        _deliver_through(task, settings)
        assert isinstance(task.retried_with, MailDeliveryError)
        assert not isinstance(task.retried_with, smtplib.SMTPException)

    def test_its_text_holds_no_address(self, settings):
        task = _TaskDouble()
        _deliver_through(task, settings)
        text = str(task.retried_with)
        assert RECIPIENT not in text
        assert "5.1.1" not in text


class TestAttemptLogging:
    def _messages(self, caplog, settings, retries):
        task = _TaskDouble(retries=retries)
        with caplog.at_level(logging.WARNING, logger="user.tasks"):
            _deliver_through(task, settings)
        return "\n".join(record.getMessage() for record in caplog.records if record.name == "user.tasks")

    def test_an_attempt_with_retries_left_says_it_is_retrying(self, caplog, settings):
        assert "retrying delivery (attempt 1/4)" in self._messages(caplog, settings, retries=0)

    def test_the_last_attempt_does_not_claim_a_retry_that_will_not_happen(self, caplog, settings):
        """``retry`` raises rather than retrying once the budget is spent, so a
        line saying "retrying" there sends an operator looking for a later
        attempt that never arrives."""
        logged = self._messages(caplog, settings, retries=3)
        assert "giving up" in logged
        assert "retrying" not in logged
