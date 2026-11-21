"""How an attached media file is listed among a recording's attachments (``epicurrents.attachments``)."""

from typing import Any


def describe_media(media) -> dict[str, Any]:
    """The public hash, grantee-visible name and opening details of an attached media file.

    ``details`` carries what a client chooses its importer by, the media type and extension, whether the live
    allowlist still admits the extension, and the timeline offset of footage tied to the recording's cursor. The
    author-private ``original_name`` is never part of it.
    """
    from media.api.v1.ninja import _is_supported, _resolve_display_name

    return {
        "hash": media.content_hash,
        "name": _resolve_display_name(media),
        "details": {
            "file_extension": media.file_extension,
            "is_supported": _is_supported(media),
            "media_type": media.media_type,
            "time_offset": media.time_offset,
        },
    }
