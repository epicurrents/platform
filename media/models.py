"""Media models — non-signal files (documents and video, later images / audio).

``MediaFile``
    Core model: file identity (hash, path, size), media-type taxonomy
    (``document`` and ``video``, room for ``image`` / ``audio`` later), an
    optional timeline position for time-aligned media, soft-delete timestamp.
    Reverse GenericRelations match Recording so the same library / access /
    annotation plumbing applies.
"""

from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey, GenericRelation
from django.contrib.contenttypes.models import ContentType
from django.db import models


class MediaFile(models.Model):
    """A non-signal media file: a document, image, audio, or video.

    Storage and de-identification rules mirror :class:`recordings.Recording`:
    ``original_name`` is author-private, ``display_name`` is the grantee-
    visible label, ``content_hash`` is the de-identified identifier used in
    URLs. The viewer dispatches by ``media_type`` + ``file_extension`` to the
    appropriate reader (htm, pdf, …).
    """

    class MediaType(models.TextChoices):
        # Documents — markdown, html, pdf today; rendered by the viewer's
        # doc-module (HtmImporter / PdfImporter).
        DOCUMENT = "document", "Document"
        # Footage attached to a signal recording; the viewer ties playback to
        # the main cursor via ``time_offset``. Served inline with HTTP Range
        # support so the browser can seek.
        VIDEO = "video", "Video"
        # Future taxonomy seats; storage path and access plumbing are
        # already media-type-agnostic, only the viewer dispatch table needs
        # extending when these land.
        # IMAGE = "image", "Image"
        # AUDIO = "audio", "Audio"

    media_type = models.CharField(
        max_length=16,
        choices=MediaType.choices,
        db_index=True,
    )
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="media_files",
    )
    # Optional generic attachment to a parent object — covers video-EEG,
    # audio tracks alongside polysomnography, supplementary docs per case,
    # and any future target model (collections, sessions, …) without a
    # schema change. The GFK target row is **not** declared with a reverse
    # ``GenericRelation`` on Recording (or any other target): we want a
    # purged target to *orphan* the media row rather than cascade-delete
    # it, mirroring the SET_NULL semantics that would apply if this were a
    # real FK. Stale ``(attachment_content_type, attachment_object_id)``
    # pairs are handled at read time by the serialiser, which surfaces the
    # row with ``attached_to: null`` once the target is gone.
    attachment_content_type = models.ForeignKey(
        ContentType,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="media_attachments_by_type",
    )
    attachment_object_id = models.CharField(max_length=255, blank=True, default="")
    attachment = GenericForeignKey("attachment_content_type", "attachment_object_id")
    # Position in seconds of this media on the attached parent's timeline. For
    # a video or audio clip it is the offset that aligns playback to the
    # recording's t=0; for an image it is the moment the image pins to. Null
    # when the media carries no timeline position (a supplementary document, or
    # an as-yet-unaligned clip). The value is attachment-scoped but kept across
    # a detach so re-attaching to the same parent restores the alignment.
    time_offset = models.FloatField(null=True, blank=True, default=None)
    # Filename as uploaded. Visible only to the author and superusers; never
    # returned to grantees, share-token holders, or federated peers.
    original_name = models.CharField(max_length=255)
    # Grantee-visible label. Nullable: when unset, list / detail responses
    # fall back to the stored-hash prefix.
    display_name = models.CharField(max_length=255, null=True, blank=True, default=None)
    # Hex hash + original file extension, unique on disk.
    stored_name = models.CharField(max_length=255, unique=True)
    file_extension = models.CharField(max_length=32, blank=True, default="")
    file_size = models.BigIntegerField()
    file_path = models.CharField(max_length=1024)
    # SHA-256 of the raw file bytes. Recorded for integrity checks; no
    # de-duplication query exists yet, so the field is deliberately unindexed.
    file_hash = models.CharField(max_length=64, blank=True, default="")
    # De-identified identifier used in URLs (the public ``hash`` parameter).
    # A 32-char random token, not a digest — the name mirrors the Recording
    # API field for response parity.
    content_hash = models.CharField(max_length=32, blank=True, default="")

    # Reverse GenericRelations so hard-delete cascades cleanly through every
    # reference row that targets this media file via a GenericForeignKey.
    # Soft-delete is unaffected — the cascade only runs when the row is
    # actually removed by Django.
    access_rights = GenericRelation("epicurrents.AccessRight")
    collection_memberships = GenericRelation("library.CollectionItem")
    dataset_memberships = GenericRelation("library.DatasetItem")
    tagged_items = GenericRelation("library.TaggedItem")

    created_at = models.DateTimeField(auto_now_add=True)
    modified_at = models.DateTimeField(auto_now=True)
    deleted_at = models.DateTimeField(null=True, blank=True, default=None, db_index=True)

    class Meta:
        indexes = [
            models.Index(fields=["author", "created_at"]),
            models.Index(fields=["created_at"]),
            models.Index(fields=["content_hash"]),
            models.Index(
                fields=["attachment_content_type", "attachment_object_id"],
                name="media_attachment_lookup_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"MediaFile({self.media_type} {self.original_name!r} by {self.author_id})"
