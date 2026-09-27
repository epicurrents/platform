"""One delivery path for every message the platform sends.

Three classes of mail exist — the welcome / invite for a new account, the
password reset a user asked for, and maintenance job lifecycle notices to
superusers — written in two apps. What they have in common is not the message
but the failure: an SMTP rejection text routinely echoes the recipient address
back, and a traceback carries it into the log stream, which the log-hygiene rule
in AGENTS.md says must never hold a raw address. ``send_mail`` here is what the
rule is enforced in, so a new flow inherits it by calling this rather than by
remembering the rule.

The address is reduced to a truncated SHA-256 and the exception to its class
name — enough for an operator to correlate "this delivery failed" across log
lines and with the audit trail, and useless to anyone reading the log for
addresses. Every sender gets that treatment by construction, which is why it
lives here rather than in whichever app happened to need it first.
"""

import hashlib
import logging

logger = logging.getLogger(__name__)


class MailDeliveryError(Exception):
    """A send failed. Carries the failure's class name and hashed recipients, never their addresses.

    The reason this is a distinct type rather than a re-raise: the original
    exception escapes the careful logging above the moment anything else prints
    it. A Celery task that exhausts its retries hands the exception it was given
    back to the worker, which logs the failure with a traceback — so an
    ``SMTPRecipientsRefused`` raised here would put the rejected address in the
    worker log through a path that never passes this module. It is raised
    ``from None`` so the original is not chained into that traceback either.
    """


def address_hash(address: str) -> str:
    """Return the log-safe form of an email address: a truncated SHA-256 of its normalised text.

    Normalised the same way the password-reset rate limiter normalises before
    hashing, so the two streams name one address identically and an operator can
    follow a single recipient across both without either holding the address.
    """
    return hashlib.sha256(address.strip().lower().encode()).hexdigest()[:16]


def send_mail(*, subject: str, message: str, from_email: str | None, recipient_list: list[str], context: str) -> None:
    """Send one message, raising on failure after logging what failed without naming whom.

    ``context`` labels the flow in the log line — a task name, or the operation a
    notification is about — because the recipient hash alone does not say which
    of the three classes failed.

    Raises :class:`MailDeliveryError` on failure, never the backend's own
    exception. Callers decide what that means: a Celery task retries, a notifier
    logs and carries on. Nothing is swallowed here, and ``fail_silently`` is
    deliberately not offered — a caller that wants to continue past a failure
    catches the exception, which at least leaves the decision visible at the call
    site.
    """
    from django.core.mail import send_mail as django_send_mail

    try:
        django_send_mail(
            subject=subject,
            message=message,
            from_email=from_email,
            recipient_list=recipient_list,
            fail_silently=False,
        )
    except Exception as exc:
        # Never the exception itself, and never a traceback: SMTP rejections
        # commonly quote the rejected address, so both carry it into the log.
        hashes = [address_hash(addr) for addr in recipient_list]
        logger.warning("%s: delivery failed to %s — %s", context, hashes, type(exc).__name__)
        raise MailDeliveryError(f"{context}: {type(exc).__name__} for {hashes}") from None
