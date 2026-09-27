"""Management command — send a test message to every active superuser, to prove the relay works.

Exists because the deployment this is for has no shell access. A relay is
configured through ``.env`` and nothing else, and the only way to find out
whether the credentials, the port and the sender domain are right is to send
something — which without this means waiting for a password reset or a
maintenance notice to be the first real message, discovering the answer at the
moment it matters.

Registered as the ``mail.send_test`` maintenance operation, so a superuser runs
it from the Maintenance tab and reads the outcome there.

The report names no recipient, not even by hash. It is captured into
``MaintenanceJob.output``, which no erasure path reaches, and a hash of an
address is still a stable identifier of the person who owns it. The report
carries counts; the per-recipient hashes are in the worker log, where the mail
path writes them for any failed delivery.

One message per recipient rather than one addressed to all of them, so no
superuser's address is disclosed to the others, and so one rejected recipient
does not fail the test for the rest.
"""

import re

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from epicurrents.mail import MailDeliveryError, send_mail

_MESSAGE = (
    "This is a test message from the Epicurrents maintenance tab.\n\n"
    "Receiving it means the relay accepted the message and delivered it, so the platform's "
    "password-reset, invitation and maintenance mail will reach this address too.\n\n"
    "Nothing needs doing about it."
)


def _failure_class(exc: MailDeliveryError) -> str:
    """The backend exception's class name out of a :class:`MailDeliveryError`, without the hashes it also carries.

    The error's text is ``"<context>: <ClassName> for [<hashes>]"``; only the
    class name belongs in the job output.
    """
    match = re.match(r"^[^:]*: (\w+) for ", str(exc))
    return match.group(1) if match else "delivery error"


class Command(BaseCommand):
    help = "Send a test email to every active superuser and report what the relay did."

    def handle(self, *args, **options):
        backend = getattr(settings, "EMAIL_BACKEND", "")
        if backend.endswith("console.EmailBackend"):
            raise CommandError(
                "Outgoing mail is going to the container log: EMAIL_BACKEND is still the console backend. "
                "Configure EMAIL_HOST, EMAIL_PORT, EMAIL_HOST_USER and EMAIL_HOST_PASSWORD, then run this again."
            )

        recipients = [
            user.email
            for user in get_user_model().objects.filter(is_active=True, is_superuser=True).order_by("pk")
            if user.email
        ]
        if not recipients:
            raise CommandError(
                "No active superuser has an email address, so there is nobody to send a test to. "
                "Add one on the account page first."
            )

        host = getattr(settings, "EMAIL_HOST", "") or "(unset)"
        sender = getattr(settings, "DEFAULT_FROM_EMAIL", "") or "(unset)"
        self.stdout.write(f"Relay:     {host}:{getattr(settings, 'EMAIL_PORT', '?')}")
        self.stdout.write(f"From:      {sender}")
        self.stdout.write(f"Timeout:   {getattr(settings, 'EMAIL_TIMEOUT', None)}s")
        self.stdout.write(f"Recipients: {len(recipients)}")

        failures: list[str] = []
        for address in recipients:
            try:
                send_mail(
                    subject="Epicurrents test message",
                    message=_MESSAGE,
                    from_email=settings.DEFAULT_FROM_EMAIL,
                    recipient_list=[address],
                    context="mail.send_test",
                )
            except MailDeliveryError as exc:
                failures.append(_failure_class(exc))

        if failures:
            classes = ", ".join(sorted(set(failures)))
            raise CommandError(
                f"The relay refused {len(failures)} of {len(recipients)} message(s): {classes}. Check the "
                "credentials, the port, and whether the sender domain is one the relay is allowed to send for."
            )

        self.stdout.write(
            self.style.SUCCESS(
                f"The relay accepted all {len(recipients)} message(s). Delivery is the relay's from here — check "
                "the inbox, "
                "and the spam folder, before concluding the DNS records are right."
            )
        )
