"""Read-permission extension: attached media inherits its parent's read access.

⚠️ LOAD-BEARING — `can_read_via_attachment` is registered as a `can_read_object`
extension in `media.apps.MediaConfig.ready()`. It is the only path by which a
grantee reaches media attached to a recording: there is no separate sharing
surface for attached media, so a regression here silently hides every attached
clip / document from everyone but the author. Contract test:
`media/tests/test_permissions.py`.

A `MediaFile` attached to a parent object (video-EEG footage on a Recording,
a report document on a Recording, …) is readable by anyone who can read that
parent — from any source: a direct `AccessRight` on the parent, dataset
share inheritance, or a share token. The extension delegates to
`can_read_object` on the parent so the media transparently picks up whatever
access the parent has, wherever it comes from.
"""

from epicurrents.permissions import ReadAccessTerms


def can_read_via_attachment(user, obj, share_token: str | None = None):
    """Return a ReadAccessTerms granting read on a MediaFile via its attachment.

    Called automatically by `can_read_object` through the extension registry.
    Applies only to `MediaFile` instances attached to a parent the caller can
    read; everything else yields `granted=False` so other extensions and the
    default deny still apply. Returns default metadata (`apply_middleware=False`)
    because media is never EDF — the parent's header-anonymisation flag is
    irrelevant to a media byte stream.
    """
    from epicurrents.permissions import can_read_object
    from media.models import MediaFile
    from recordings.models import Recording

    if not isinstance(obj, MediaFile):
        return ReadAccessTerms(granted=False)
    # GenericForeignKey; None when unattached or the target row is gone.
    parent = obj.attachment
    if parent is None:
        return ReadAccessTerms(granted=False)
    # FAILED-recording hiding: a failed upload is visible only to its author and
    # superusers — every other caller gets 404 from the recording surfaces. The
    # read-visibility gate (recordings/permissions.py) already makes the
    # can_read_object call below deny stale grants on a FAILED or trashed
    # parent; this explicit check stays as defence-in-depth so attached media
    # cannot become a side channel even if the gate registration regresses.
    if isinstance(parent, Recording) and parent.status == Recording.Status.FAILED:
        is_author = bool(user and getattr(user, "is_authenticated", False) and parent.author_id == user.pk)
        is_superuser = bool(user and getattr(user, "is_superuser", False))
        if not (is_author or is_superuser):
            return ReadAccessTerms(granted=False)
    if can_read_object(user=user, obj=parent, share_token=share_token):
        return ReadAccessTerms(granted=True)
    return ReadAccessTerms(granted=False)
