"""Django app configuration for the notifications app."""

from django.apps import AppConfig


class NotificationsConfig(AppConfig):
    """Django app configuration for web push notifications."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "notifications"

    def ready(self):
        """Register audit-trail handling for push-subscription secrets.

        The browser-generated encryption keys are masked out of audit
        payloads at write time; the endpoint URL (a per-device identifier)
        stays recorded but is scrubbed on GDPR Art. 17 erasure.
        """
        from activity.audit import register_masked_fields
        from activity.erasure import register_subject_pii

        register_masked_fields("notifications.pushsubscription", {"p256dh", "auth"})
        register_subject_pii(
            "notifications.pushsubscription",
            owner_field="user_id",
            pii_fields={"endpoint", "p256dh", "auth"},
        )
