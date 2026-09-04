"""Notifications Celery tasks — single delivery path for web push.

``send_push_to_user`` is the only entry point.  Other apps import it
and call ``.delay(...)`` from their own Celery tasks or signal
handlers; this app does not decide *when* to notify anyone.
"""

import json
import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task
def send_push_to_user(user_id: int, title: str, body: str, data: dict | None = None):
    """Send a web push notification to all active subscriptions for a user.

    Silently skips if WEBPUSH_VAPID_PRIVATE_KEY is not configured (e.g. during
    development without VAPID keys set up). Stale subscriptions that respond
    with HTTP 410 Gone are automatically deleted.
    """
    from django.conf import settings
    from pywebpush import WebPushException, webpush

    from notifications.models import PushSubscription

    private_key = getattr(settings, "WEBPUSH_VAPID_PRIVATE_KEY", "")
    if not private_key:
        logger.debug("send_push_to_user: WEBPUSH_VAPID_PRIVATE_KEY not set — skipping")
        return {"sent": 0, "stale": 0}

    vapid_claims = {"sub": getattr(settings, "WEBPUSH_VAPID_SUBJECT", "mailto:admin@localhost")}
    payload = json.dumps({"title": title, "body": body, **(data or {})})

    subscriptions = list(PushSubscription.objects.filter(user_id=user_id))
    sent = 0
    stale_ids = []

    for sub in subscriptions:
        try:
            webpush(
                subscription_info={
                    "endpoint": sub.endpoint,
                    "keys": {"p256dh": sub.p256dh, "auth": sub.auth},
                },
                data=payload,
                vapid_private_key=private_key,
                vapid_claims=vapid_claims,
            )
            sent += 1
        except WebPushException as exc:
            response = getattr(exc, "response", None)
            status = response.status_code if response is not None else None
            if status in (404, 410):
                # Subscription has expired or been revoked by the push service.
                stale_ids.append(sub.pk)
            else:
                logger.error(
                    "send_push_to_user: delivery failed for subscription %d (status=%s): %s",
                    sub.pk,
                    status,
                    exc,
                )

    if stale_ids:
        # Audit attribution: only the stale-cleanup branch opens an
        # audited scope. Wrapping the whole task body would inflate the
        # Activity table with one row per push send — those are
        # operational telemetry, not user-data state changes. Each
        # removed PushSubscription fires pre_delete inside the scope and
        # lands as a DELETE ObjectChangeLog entry. user_id rides on
        # metadata rather than as a generic-FK target so the cleanup
        # does not need a separate User SELECT.
        from activity.models import Activity
        from activity.system_activity import with_system_activity

        with with_system_activity(
            "notifications.subscription.purge_stale",
            interface=Activity.Interface.CELERY,
            metadata={"user_id": user_id, "count": len(stale_ids)},
        ):
            PushSubscription.objects.filter(pk__in=stale_ids).delete()
        logger.info("send_push_to_user: removed %d stale subscription(s)", len(stale_ids))

    logger.info(
        "send_push_to_user: user=%d sent=%d stale=%d",
        user_id,
        sent,
        len(stale_ids),
    )
    return {"sent": sent, "stale": len(stale_ids)}
