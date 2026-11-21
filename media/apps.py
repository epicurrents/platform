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

        # Attached media is listed with every other attachment of a recording. The platform applies no pass to
        # media content: what a file shows is its submitter's to answer for.
        from epicurrents.attachments import PASS_NONE, AttachmentType, register_attachment_type
        from media.attachments import describe_media

        register_attachment_type(
            AttachmentType(
                key="mediafile",
                model_label="media.mediafile",
                describe=describe_media,
                platform_pass=lambda _media: PASS_NONE,
                live=lambda qs: qs.filter(deleted_at__isnull=True),
            )
        )

        # The uploaded filename is author-private and can embed subject
        # identifiers; mask it out of audit payloads at write time.
        from activity.audit import register_masked_fields

        register_masked_fields("media.mediafile", {"original_name"})
