"""What the mail test operation reports, and what it must not.

Its output is captured into ``MaintenanceJob.output`` and rendered in the
Maintenance tab. That column is deliberately outside the erasure registrations:
the maintenance app keeps its rows to ids and hashes so nothing in them needs
scrubbing when an account is erased. A command that printed the superusers'
addresses would put personal data into a store with no path to remove it, and
the only person positioned to notice is nobody.

The refusals matter as much as the send. The command exists to answer "is the
relay configured", asked from a deployment with no shell — so answering
"success" against the console backend, which writes to a container log the
asker cannot read either, is the one outcome that would mislead.
"""

import smtplib
from io import StringIO

import pytest
from django.core.mail.backends.base import BaseEmailBackend
from django.core.management import call_command
from django.core.management.base import CommandError

from epicurrents.mail import address_hash

SUPERUSER_ADDRESS = "root@example.org"
LOCMEM = "django.core.mail.backends.locmem.EmailBackend"
CONSOLE = "django.core.mail.backends.console.EmailBackend"


class _RefusingBackend(BaseEmailBackend):
    def send_messages(self, email_messages):
        raise smtplib.SMTPAuthenticationError(535, f"5.7.8 bad credentials for {SUPERUSER_ADDRESS}".encode())


@pytest.fixture
def addressed_superuser(superuser):
    superuser.email = SUPERUSER_ADDRESS
    superuser.save()
    return superuser


def _run(**kwargs):
    out = StringIO()
    call_command("send_test_email", stdout=out, stderr=out, **kwargs)
    return out.getvalue()


@pytest.mark.django_db
class TestItSends:
    def test_every_active_superuser_is_a_recipient(self, addressed_superuser, make_superuser, settings):
        from django.core import mail as django_mail

        settings.EMAIL_BACKEND = LOCMEM
        second = make_superuser(username="second_su")
        second.email = "second@example.org"
        second.save()
        django_mail.outbox.clear()
        _run()
        assert len(django_mail.outbox) == 1
        assert sorted(django_mail.outbox[0].to) == [SUPERUSER_ADDRESS, "second@example.org"]

    def test_an_ordinary_account_is_not_a_recipient(self, addressed_superuser, make_user, settings):
        from django.core import mail as django_mail

        settings.EMAIL_BACKEND = LOCMEM
        ordinary = make_user(username="not_su")
        ordinary.email = "ordinary@example.org"
        ordinary.save()
        django_mail.outbox.clear()
        _run()
        assert django_mail.outbox[0].to == [SUPERUSER_ADDRESS]

    def test_a_deactivated_superuser_is_not_a_recipient(self, addressed_superuser, make_superuser, settings):
        from django.core import mail as django_mail

        settings.EMAIL_BACKEND = LOCMEM
        gone = make_superuser(username="gone_su")
        gone.email = "gone@example.org"
        gone.is_active = False
        gone.save()
        django_mail.outbox.clear()
        _run()
        assert django_mail.outbox[0].to == [SUPERUSER_ADDRESS]


@pytest.mark.django_db
class TestTheReport:
    def test_no_address_reaches_the_output(self, addressed_superuser, settings):
        settings.EMAIL_BACKEND = LOCMEM
        assert SUPERUSER_ADDRESS not in _run()

    def test_recipients_appear_as_the_hash_the_mail_path_logs(self, addressed_superuser, settings):
        """Same truncated digest, so an operator can match this run against the
        delivery failure it produced without either holding an address."""
        settings.EMAIL_BACKEND = LOCMEM
        assert address_hash(SUPERUSER_ADDRESS) in _run()

    def test_it_names_the_relay_it_used(self, addressed_superuser, settings):
        settings.EMAIL_BACKEND = LOCMEM
        settings.EMAIL_HOST = "smtp.relay.example"
        assert "smtp.relay.example" in _run()


@pytest.mark.django_db
class TestItRefuses:
    def test_the_console_backend_is_not_a_success(self, addressed_superuser, settings):
        """The asker has no shell, so a container log answers nothing — and
        reporting success here is the outcome that sends them away believing a
        relay works when none is configured."""
        settings.EMAIL_BACKEND = CONSOLE
        with pytest.raises(CommandError, match="console backend"):
            _run()

    def test_no_addressed_superuser_is_refused(self, superuser, settings):
        settings.EMAIL_BACKEND = LOCMEM
        superuser.email = ""
        superuser.save()
        with pytest.raises(CommandError, match="nobody to send a test to"):
            _run()

    def test_a_rejection_reports_its_class_without_the_address(self, addressed_superuser, settings):
        settings.EMAIL_BACKEND = "epicurrents.tests.test_send_test_email_command._RefusingBackend"
        with pytest.raises(CommandError) as caught:
            _run()
        message = str(caught.value)
        assert "SMTPAuthenticationError" in message
        assert SUPERUSER_ADDRESS not in message
        assert "5.7.8" not in message

    def test_the_backend_exception_is_not_chained_onto_the_refusal(self, addressed_superuser, settings):
        """``CommandError`` is printed with its cause by ``call_command``'s
        caller, which would put the rejection text back in the job output one
        frame further down."""
        settings.EMAIL_BACKEND = "epicurrents.tests.test_send_test_email_command._RefusingBackend"
        with pytest.raises(CommandError) as caught:
            _run()
        assert caught.value.__cause__ is None


@pytest.mark.django_db
class TestItIsRegistered:
    def test_the_operation_names_this_command(self):
        from maintenance.operations import get_operation

        operation = get_operation("mail.send_test")
        assert operation is not None
        assert operation.executor == "celery"
        assert operation.command == "send_test_email"

    def test_it_needs_no_step_up(self):
        """It changes nothing, and a confirmation prompt on a diagnostic is a
        reason not to run the diagnostic."""
        from maintenance.operations import get_operation

        assert get_operation("mail.send_test").requires_step_up is False

    def test_it_takes_no_arguments(self):
        """Nothing in the request names a recipient. The command reads the
        superuser roster instead, so a request cannot mail an arbitrary address
        from the deployment's own sender domain."""
        from maintenance.operations import get_operation

        assert get_operation("mail.send_test").args_schema.model_fields == {}
