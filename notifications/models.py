"""Push subscription model — persists browser/device push endpoints per user."""

from django.conf import settings
from django.db import models


class PushSubscription(models.Model):
    """Browser push subscription for a single user/device pair.

    Each browser on each device produces a unique endpoint. A user may have
    multiple active subscriptions (desktop Chrome, mobile Firefox, etc.). The
    endpoint is stable for the lifetime of the subscription but is replaced when
    the user re-subscribes (e.g. after clearing browser data).

    Stale subscriptions (push service returns 410) are cleaned up automatically
    by send_push_to_user after a failed delivery attempt.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="push_subscriptions",
    )
    # The full push endpoint URL provided by the browser's push service.
    endpoint = models.TextField(unique=True)
    # Browser-generated encryption keys required to encrypt the push payload.
    p256dh = models.TextField()
    auth = models.TextField()

    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        from urllib.parse import urlparse

        host = urlparse(self.endpoint).netloc or self.endpoint[:40]
        return f"PushSubscription(user={self.user_id} @ {host})"

    class Meta:
        indexes = [
            models.Index(fields=["user"]),
        ]
