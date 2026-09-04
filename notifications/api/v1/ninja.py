"""Notifications API v1 — VAPID key distribution and push subscription management.

Endpoints
---------
GET  /vapid-public-key   Return the VAPID public key (unauthenticated).
POST /subscribe          Save or upsert a browser push subscription.
DELETE /subscribe        Remove a browser push subscription by endpoint.
"""

from django.conf import settings
from django.db import IntegrityError, transaction
from ninja import NinjaAPI, Schema
from ninja.errors import HttpError

from activity.audit import log_activity
from epicurrents.auth import enforce_session_csrf

api = NinjaAPI(
    title="Notifications API",
    version="1",
    urls_namespace="notifications-api-v1",
    docs_url=settings.API_DOCS_URL,
    openapi_url=settings.API_OPENAPI_URL,
)


class SubscribeIn(Schema):
    """Push subscription object returned by the browser's pushManager.subscribe()."""

    endpoint: str
    p256dh: str
    auth: str


class UnsubscribeIn(Schema):
    """Endpoint to remove."""

    endpoint: str


def _require_auth(request):
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated:
        raise HttpError(401, "Authentication credentials were not provided")
    enforce_session_csrf(request)
    return user


def _validate_push_endpoint(endpoint: str, user) -> None:
    """Reject push endpoints that could turn the worker into an SSRF proxy.

    ``send_push_to_user`` makes an outbound HTTP request to the stored
    endpoint URL, so an unvalidated endpoint lets any authenticated user
    point the Celery worker at internal services or the cloud metadata
    address. Requires ``https`` and a globally-routable resolved address;
    the ``FEDERATION_ALLOW_PRIVATE_PEER_URLS`` development override
    applies through ``check_url_is_safe``.
    """
    import urllib.parse

    from django.conf import settings as django_settings

    from epicurrents.security_log import log_security_event
    from federation.auth import check_url_is_safe

    scheme = urllib.parse.urlparse(endpoint).scheme
    allow_private = getattr(django_settings, "FEDERATION_ALLOW_PRIVATE_PEER_URLS", False)
    if scheme != "https" and not allow_private:
        log_security_event(
            "notifications.subscription_rejected",
            reason="non_https_endpoint",
            actor_id=user.pk,
        )
        raise HttpError(400, "Push endpoint must use https")
    try:
        check_url_is_safe(endpoint)
    except ValueError:
        log_security_event(
            "notifications.subscription_rejected",
            reason="unsafe_endpoint",
            actor_id=user.pk,
        )
        raise HttpError(400, "Push endpoint is not acceptable")


@api.get("/vapid-public-key")
def vapid_public_key(request):
    """Return the VAPID public key needed by the browser to set up a push subscription.

    This endpoint is intentionally unauthenticated — the VAPID public key is
    designed to be distributed to all clients.
    """
    return {"vapid_public_key": getattr(settings, "WEBPUSH_VAPID_PUBLIC_KEY", "")}


@api.post("/subscribe")
def subscribe(request, payload: SubscribeIn):
    """Save or update a browser push subscription for the authenticated user.

    Upserting by (user, endpoint) means re-subscribing after a key rotation
    silently replaces the old entry rather than creating a duplicate, while
    an endpoint registered by a different user cannot be reassigned — the
    request is acknowledged without touching the existing row, so the
    response does not confirm whether the endpoint is known.
    """
    from epicurrents.security_log import log_security_event
    from notifications.models import PushSubscription

    user = _require_auth(request)
    _validate_push_endpoint(payload.endpoint, user)
    with transaction.atomic():
        existing = PushSubscription.objects.filter(endpoint=payload.endpoint).first()
        if existing is not None and existing.user_id != user.pk:
            log_security_event(
                "notifications.subscription_rejected",
                reason="endpoint_owned_by_other_user",
                actor_id=user.pk,
                owner_id=existing.user_id,
            )
            return {"status": "ok"}
        try:
            # Savepoint so a caught IntegrityError doesn't poison the
            # enclosing transaction.
            with transaction.atomic():
                subscription, created = PushSubscription.objects.update_or_create(
                    user=user,
                    endpoint=payload.endpoint,
                    defaults={
                        "p256dh": payload.p256dh,
                        "auth": payload.auth,
                    },
                )
        except IntegrityError:
            # Concurrent registration of the same endpoint by another user
            # between the ownership check and the upsert — same outcome as
            # the pre-checked case.
            return {"status": "ok"}
        log_activity(
            verb="notifications.subscription.create",
            target=subscription,
            metadata={"upserted": not created},
        )
    return {"status": "ok"}


@api.delete("/subscribe")
def unsubscribe(request, payload: UnsubscribeIn):
    """Remove a push subscription by endpoint."""

    from notifications.models import PushSubscription

    user = _require_auth(request)
    with transaction.atomic():
        subscription = PushSubscription.objects.filter(user=user, endpoint=payload.endpoint).first()
        # log_activity runs BEFORE delete so target_object_id is preserved.
        # ``row_existed`` is the derived insight not in the linked CL row
        # (because no CL row is written when the queryset is empty).
        log_activity(
            verb="notifications.subscription.delete",
            target=subscription,
            metadata={"row_existed": subscription is not None},
        )
        if subscription is not None:
            subscription.delete()
    return {"status": "ok"}
