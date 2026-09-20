"""User Celery tasks — outbound email delivery for password reset and similar transactional flows.

Every task here takes identifiers and renders its own message. A generic
"send this text to this address" task is deliberately absent: the broker
persists to an append-only file, so its arguments outlive the send, and a task
signature that accepts a recipient address invites exactly the payload the
inventory in docs/gdpr-compliance.md says the store does not hold. A new mail
flow adds a task that takes a primary key, as the one below does.
"""

import logging

from celery import shared_task

logger = logging.getLogger(__name__)


def _deliver(task, subject: str, message: str, from_email: str, recipient_list: list[str]):
    """Send via the configured backend, retrying the calling task on failure.

    Takes the task instance rather than being a method so any task in this module
    shares one retry policy. The hygiene half — hashed recipients, exception
    class only — lives in :mod:`epicurrents.mail`, where the maintenance
    notifier reaches it too; what is added here is the attempt count, which only
    a task has.

    Only :class:`~epicurrents.mail.MailDeliveryError` is caught, which is also
    what is handed to ``retry``: celery re-raises the exception it was given once
    the retries run out, and the worker logs that failure with a traceback, so
    handing it the backend's own exception would publish the rejection text — and
    the address in it — through a path this module never sees.
    """
    from epicurrents.mail import MailDeliveryError, send_mail

    try:
        send_mail(
            subject=subject,
            message=message,
            from_email=from_email,
            recipient_list=recipient_list,
            context=task.name,
        )
    except MailDeliveryError as exc:
        attempt = task.request.retries + 1
        if attempt > task.max_retries:
            logger.warning("%s: delivery failed after %d attempts, giving up", task.name, attempt)
        else:
            logger.warning("%s: retrying delivery (attempt %d/%d)", task.name, attempt, task.max_retries + 1)
        raise task.retry(exc=exc)


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def send_password_reset_email(self, user_id: int):
    """Render and send the password-reset mail entirely inside the worker.

    The argument is a primary key rather than the finished message because the
    broker persists. Redis carries the queue to an append-only file, and a
    payload holding the reset URL would leave a token valid for three days, next
    to the recipient's address, sitting on disk until an AOF rewrite — which on a
    low-volume deployment can be months away, long past the token's expiry. The
    token is therefore minted here, at send time, and the address is read from
    the row. This also matches every other ``.delay`` in the codebase, which pass
    identifiers and let the worker fetch what it needs.

    A missing or deactivated user is not an error: the account can be closed
    between the request and the send, and the endpoint deliberately answers
    identically either way to avoid disclosing which addresses exist.
    """
    from django.conf import settings
    from django.contrib.auth import get_user_model
    from django.contrib.auth.tokens import default_token_generator
    from django.utils.encoding import force_bytes
    from django.utils.http import urlsafe_base64_encode

    try:
        user = get_user_model().objects.get(pk=user_id, is_active=True)
    except get_user_model().DoesNotExist:
        return

    uid = urlsafe_base64_encode(force_bytes(user.pk))
    token = default_token_generator.make_token(user)
    # rstrip because a trailing slash in FRONTEND_URL is expected enough that the
    # boot guard in epicurrents/apps.py strips one too; without it every link is
    # minted with a doubled slash.
    reset_url = f"{settings.FRONTEND_URL.rstrip('/')}/reset-password?uid={uid}&token={token}"

    _deliver(
        self,
        subject="Reset your Epicurrents password",
        message=f"Click the link below to reset your password:\n\n{reset_url}\n\nThis link expires in 3 days.",
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[user.email],
    )
