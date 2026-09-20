"""Telling every superuser when a job reaches a state that needs them.

Push through the notifications app, mail through Django's backend when the
deployment configured one. Each state notifies once: the caller persists
``last_notified_state`` in the same write as the state itself, so a restore that
rolls the row back also rolls the bookkeeping back and the state is announced
again, which is the right direction.
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


def notify_job_state(job, *, state: str | None = None) -> str | None:
    """Notify superusers of ``job``'s state if it is one worth announcing and not yet announced.

    ``state`` names the state being entered when the instance does not carry it
    yet. Returns the state announced, which the caller stores as
    ``last_notified_state`` in the same write as the state itself, or ``None``.
    Nothing on the instance is changed here. Delivery failures are logged, never
    raised: a job's state must not depend on a push service.
    """
    state = state or job.state
    if state not in ATTENTION_STATES or job.last_notified_state == state:
        return None
    title, body = _message(job, state)
    recipients = list(get_user_model().objects.filter(is_active=True, is_superuser=True))
    try:
        from notifications.tasks import send_push_to_user

        for user in recipients:
            send_push_to_user.delay(user.pk, title, body, data={"type": "maintenance", "job_id": str(job.job_id)})
    except Exception:
        logger.exception("Push notification for maintenance job %s could not be dispatched", job.job_id)
    if mail_configured():
        from django.core.mail import send_mail

        addresses = [user.email for user in recipients if user.email]
        if addresses:
            try:
                send_mail(title, body, None, addresses, fail_silently=True)
            except Exception:
                logger.exception("Mail notification for maintenance job %s could not be sent", job.job_id)
    return state
