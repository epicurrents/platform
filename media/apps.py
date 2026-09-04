"""Django app configuration for the media app."""

from django.apps import AppConfig


class MediaConfig(AppConfig):
    """Django app configuration for the non-signal media domain."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "media"

    def ready(self):
        """Register the attachment read-permission extension.

        Attached media inherits the read access of its parent object, so a
        grantee who can read a recording can also read media attached to it.
        """
        from epicurrents.permissions import register_read_permission_extension
        from media.permissions import can_read_via_attachment

        register_read_permission_extension(can_read_via_attachment)

        # The uploaded filename is author-private and can embed subject
        # identifiers; mask it out of audit payloads at write time.
        from activity.audit import register_masked_fields

        register_masked_fields("media.mediafile", {"original_name"})
