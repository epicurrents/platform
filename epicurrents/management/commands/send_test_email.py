"""Management command — send a test message to every active superuser, to prove the relay works.

Exists because the deployment this is for has no shell access. A relay is
configured through ``.env`` and nothing else, and the only way to find out
whether the credentials, the port and the sender domain are right is to send
something — which without this means waiting for a password reset or a
maintenance notice to be the first real message, discovering the answer at the
moment it matters.

Registered as the ``mail.send_test`` maintenance operation, so a superuser runs
it from the Maintenance tab and reads the outcome there.

The report names no address. It is captured into ``MaintenanceJob.output``,
which the maintenance app keeps free of personal data on purpose — the rows
hold ids and hashes so that nothing there needs scrubbing when an account is
erased. Recipients appear as the same truncated hash the mail path logs, which
is what lets an operator match this run against the delivery failure it caused.
"""

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from epicurrents.mail import MailDeliveryError, address_hash, send_mail


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
        self.stdout.write(f"Recipients: {len(recipients)} — {[address_hash(address) for address in recipients]}")

        try:
            send_mail(
                subject="Epicurrents test message",
                message=(
                    "This is a test message from the Epicurrents maintenance tab.\n\n"
                    "Receiving it means the relay accepted the message and delivered it, so the platform's "
                    "password-reset, invitation and maintenance mail will reach this address too.\n\n"
                    "Nothing needs doing about it."
                ),
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=recipients,
                context="mail.send_test",
            )
        except MailDeliveryError as exc:
            # The message is already sanitised — the failure's class name and the
            # recipient hashes, never an address or the rejection text.
            raise CommandError(
                f"The relay refused the message: {exc}. Check the credentials, the port, and whether the "
                "sender domain is one the relay is allowed to send for."
            ) from None

        self.stdout.write(
            self.style.SUCCESS(
                "The relay accepted the message. Delivery is the relay's from here — check the inbox, "
                "and the spam folder, before concluding the DNS records are right."
            )
        )
