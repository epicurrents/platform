"""Contract tests for the shared mail path in :mod:`epicurrents.mail`.

The property under test is what a delivery failure is allowed to say. An SMTP
rejection quotes the address it rejected, so the exception's text and its
traceback both carry a recipient address into the log stream — which the
log-hygiene rule in AGENTS.md forbids, and which no erasure path can reach once
a log is shipped. A test that only checked "a warning was logged" would pass on
the version of this that logged the traceback, so these assert on the absence of
the address and of the exception's message.
"""

import logging
import smtplib

import pytest
from django.conf import settings
from django.core import mail as django_mail
from django.core.mail.backends.base import BaseEmailBackend

from epicurrents.mail import MailDeliveryError, address_hash, send_mail

RECIPIENT = "Person@Example.ORG"


class _RefusingBackend(BaseEmailBackend):
    """A backend that fails the way a real relay does: quoting the address back."""

    def send_messages(self, email_messages):
        raise smtplib.SMTPRecipientsRefused({RECIPIENT: (550, f"5.1.1 unknown recipient: {RECIPIENT}".encode())})


class TestAddressHash:
    def test_normalises_case_and_surrounding_space(self):
        assert address_hash(" person@example.org ") == address_hash("PERSON@EXAMPLE.ORG")

    def test_is_a_prefix_of_the_digest_the_security_log_uses(self):
        """The reset rate limiter hashes the full digest of the same normalised text.

        Truncating here rather than hashing differently is what lets an operator
        match a failed delivery against the rate-limit and reset events for the
        same address: one is a prefix of the other, and neither is the address.
        """
        import hashlib

        full = hashlib.sha256(b"person@example.org").hexdigest()
        assert full.startswith(address_hash("person@example.org"))

    def test_does_not_return_the_address(self):
        assert "person" not in address_hash("person@example.org")


class TestFailureLogging:
    def _send(self, caplog):
        with caplog.at_level(logging.WARNING, logger="epicurrents.mail"):
            with pytest.raises(MailDeliveryError):
                send_mail(
                    subject="Subject",
                    message="Body",
                    from_email=None,
                    recipient_list=[RECIPIENT],
                    context="test.flow",
                )
        return "\n".join(record.getMessage() for record in caplog.records)

    def test_the_address_never_reaches_the_log(self, caplog, settings):
        settings.EMAIL_BACKEND = "epicurrents.tests.test_mail._RefusingBackend"
        logged = self._send(caplog)
        assert RECIPIENT not in logged
        assert RECIPIENT.lower() not in logged.lower()

    def test_the_exception_text_never_reaches_the_log(self, caplog, settings):
        """The class name only. The rejection's own message holds the address."""
        settings.EMAIL_BACKEND = "epicurrents.tests.test_mail._RefusingBackend"
        logged = self._send(caplog)
        assert "SMTPRecipientsRefused" in logged
        assert "5.1.1" not in logged
        assert "unknown recipient" not in logged

    def test_no_traceback_is_attached(self, caplog, settings):
        """``logger.exception`` would attach one, and it quotes the raising frame's values."""
        settings.EMAIL_BACKEND = "epicurrents.tests.test_mail._RefusingBackend"
        with caplog.at_level(logging.WARNING, logger="epicurrents.mail"):
            with pytest.raises(MailDeliveryError):
                send_mail(
                    subject="Subject",
                    message="Body",
                    from_email=None,
                    recipient_list=[RECIPIENT],
                    context="test.flow",
                )
        assert [record for record in caplog.records if record.exc_info] == []

    def test_the_flow_is_named(self, caplog, settings):
        settings.EMAIL_BACKEND = "epicurrents.tests.test_mail._RefusingBackend"
        assert "test.flow" in self._send(caplog)

    def test_the_hash_is_there_to_correlate_on(self, caplog, settings):
        settings.EMAIL_BACKEND = "epicurrents.tests.test_mail._RefusingBackend"
        assert address_hash(RECIPIENT) in self._send(caplog)


class TestDelivery:
    def test_a_successful_send_reaches_the_backend(self, settings):
        settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
        django_mail.outbox.clear()
        send_mail(
            subject="Subject",
            message="Body",
            from_email=None,
            recipient_list=["person@example.org"],
            context="test.flow",
        )
        assert len(django_mail.outbox) == 1
        assert django_mail.outbox[0].to == ["person@example.org"]

    def test_failures_are_raised_rather_than_swallowed(self, settings):
        """No ``fail_silently`` here by design: a caller that wants to continue
        catches the exception, which keeps that decision visible at its own call
        site rather than buried in an argument."""
        settings.EMAIL_BACKEND = "epicurrents.tests.test_mail._RefusingBackend"
        with pytest.raises(MailDeliveryError):
            send_mail(
                subject="Subject",
                message="Body",
                from_email=None,
                recipient_list=[RECIPIENT],
                context="test.flow",
            )


class TestTheExceptionThatEscapes:
    """What leaves this module has to be safe for anyone downstream to print.

    Careful logging inside the module is not enough. A celery task that exhausts
    its retries hands its exception back to the worker, which logs the failure
    with a traceback; a notifier could include one in its own message. Whatever
    escapes is therefore the real contract, and it must carry no address, no
    rejection text, and no chained cause holding either.
    """

    def _raise(self, settings):
        settings.EMAIL_BACKEND = "epicurrents.tests.test_mail._RefusingBackend"
        with pytest.raises(MailDeliveryError) as caught:
            send_mail(
                subject="Subject",
                message="Body",
                from_email=None,
                recipient_list=[RECIPIENT],
                context="test.flow",
            )
        return caught.value

    def test_its_text_holds_no_address(self, settings):
        text = str(self._raise(settings))
        assert RECIPIENT not in text
        assert RECIPIENT.lower() not in text.lower()
        assert "5.1.1" not in text

    def test_its_text_identifies_the_failure(self, settings):
        text = str(self._raise(settings))
        assert "SMTPRecipientsRefused" in text
        assert "test.flow" in text
        assert address_hash(RECIPIENT) in text

    def test_the_original_is_not_chained_onto_it(self, settings):
        """``raise ... from None``. A chained cause is printed with the traceback,
        which would reintroduce the rejection text one frame further down."""
        error = self._raise(settings)
        assert error.__cause__ is None
        assert error.__context__ is None or error.__suppress_context__

    def test_the_backend_exception_does_not_escape(self, settings):
        settings.EMAIL_BACKEND = "epicurrents.tests.test_mail._RefusingBackend"
        with pytest.raises(Exception) as caught:
            send_mail(
                subject="Subject",
                message="Body",
                from_email=None,
                recipient_list=[RECIPIENT],
                context="test.flow",
            )
        assert not isinstance(caught.value, smtplib.SMTPException)


class TestTimeout:
    def test_a_send_cannot_block_for_ever(self):
        """Django defaults ``EMAIL_TIMEOUT`` to None, which is the socket's own
        default and in practice no deadline. Mail goes out inside the workers —
        the reset task, and the maintenance notifier called from a beat job that
        runs every minute — so a relay that accepts a connection and then stalls
        would hold a worker child indefinitely and take another next minute.
        """
        assert isinstance(settings.EMAIL_TIMEOUT, int)
        assert 0 < settings.EMAIL_TIMEOUT <= 60
