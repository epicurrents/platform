"""Telling every superuser when a job reaches a state that needs them.

Push through the notifications app, mail through Django's backend when the
deployment configured one, one message per recipient. Each state notifies once:
the caller decides with :func:`needs_notice`, persists ``last_notified_state``
in the same write as the state itself, and only then :func:`dispatch_notice`
queues the sending for after the commit. A restore that rolls the row back also
rolls the bookkeeping back and the state is announced again, which is the right
direction; a write that fails announces nothing.
"""

import logging

from django.conf import settings
from django.contrib.auth import get_user_model

logger = logging.getLogger(__name__)

# States a superuser should hear about. The transient ones (accepted, running)
# are visible on the page and would only add noise to a phone.
ATTENTION_STATES = frozenset(
    {
        "awaiting_verification",
        "succeeded",
        "failed",
        "rolling_back",
        "rolled_back",
        "rollback_failed",
    }
)

_CONSOLE_BACKEND = "django.core.mail.backends.console.EmailBackend"


def mail_configured() -> bool:
    """Whether outgoing mail goes somewhere other than the container log."""
    return getattr(settings, "EMAIL_BACKEND", _CONSOLE_BACKEND) != _CONSOLE_BACKEND


def _message(job, state: str) -> tuple[str, str]:
    label = job.operation
    version = f" to {job.target_version}" if job.target_version else ""
    match state:
        case "awaiting_verification":
            return (
                f"Update{version} needs your confirmation",
                (
                    f"The platform update{version} is running. Confirm it in the Maintenance tab before the window "
                    "closes, or it is rolled back."
                ),
            )
        case "succeeded":
            return f"{label} succeeded", f"The maintenance job {label} finished successfully."
        case "failed":
            return f"{label} failed", f"The maintenance job {label} failed ({job.reason or 'no reason given'})."
        case "rolling_back":
            return f"Rolling back{version}", f"The update{version} is being rolled back ({job.reason or 'requested'})."
        case "rolled_back":
            return "Rollback complete", f"The update{version} was rolled back and the previous release is serving."
        case "rollback_failed":
            return (
                "Rollback failed",
                f"The rollback of the update{version} failed. The deployment needs an operator with shell access.",
            )
    return f"{label}: {state}", f"The maintenance job {label} is now {state}."


def needs_notice(job, state: str | None = None) -> bool:
    """Whether entering ``state`` (``job.state`` when omitted) is worth announcing and not yet announced."""
    state = state or job.state
    return state in ATTENTION_STATES and job.last_notified_state != state


def dispatch_notice(job, state: str) -> None:
    """Send the notice for ``state`` from a worker once the current transaction commits.

    The caller has already saved ``last_notified_state = state`` with the state
    itself, so a notice goes out only for a state that was actually recorded:
    a save that fails rolls the transaction back and discards the callback,
    and the next attempt decides afresh. Sending from a task keeps a slow relay
    out of the caller, which for the spool sync holds a five-second lock.
    """
    from django.db import transaction

    job_pk = job.pk

    def _dispatch():
        try:
            from maintenance.tasks import send_job_notice

            send_job_notice.delay(job_pk, state)
        except Exception:
            logger.exception("Notice for maintenance job %s could not be dispatched", job.job_id)

    transaction.on_commit(_dispatch)


def send_notice(job, state: str) -> int:
    """Push and mail every active superuser about ``job`` entering ``state``; returns the recipients reached.

    One message per recipient, never one message addressed to all of them: a
    shared ``To`` line hands every superuser's address to every other, and one
    address the relay refuses fails the send for everyone. Delivery failures are
    logged, never raised: a job's state must not depend on a push service or a
    mail relay.
    """
    title, body = _message(job, state)
    recipients = list(get_user_model().objects.filter(is_active=True, is_superuser=True))
    try:
        from notifications.tasks import send_push_to_user

        for user in recipients:
            send_push_to_user.delay(user.pk, title, body, data={"type": "maintenance", "job_id": str(job.job_id)})
    except Exception:
        logger.exception("Push notification for maintenance job %s could not be dispatched", job.job_id)
    delivered = 0
    if mail_configured():
        from epicurrents.mail import send_mail

        for user in recipients:
            if not user.email:
                continue
            try:
                send_mail(
                    subject=title,
                    message=body,
                    from_email=None,
                    recipient_list=[user.email],
                    context=f"maintenance.notify[{state}]",
                )
                delivered += 1
            except Exception:
                # Caught here rather than silenced in the backend: the shared
                # path has already logged what failed, hashed, and a job's state
                # must not depend on a mail relay. Silencing it inside the
                # backend would let an awaiting_verification notice go missing
                # with nothing recorded anywhere — the one message whose absence
                # decides an outcome, since an update nobody confirms is rolled
                # back when the window closes.
                logger.warning("Mail notification for maintenance job %s was not delivered", job.job_id)
    return delivered
