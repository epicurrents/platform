"""Notifications app — web push subscription storage and delivery.

Provides ``PushSubscription`` (browser/device push endpoints keyed by VAPID),
a subscribe/unsubscribe/vapid-public-key API at ``/api/v1/notifications/``, and
the ``send_push_to_user`` Celery task that delivers notifications to all of a
user's active subscriptions.
"""
