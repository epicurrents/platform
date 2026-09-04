"""Read-permission extension: an attached study inherits its parent's access.

``can_read_via_attachment`` is registered as a ``can_read_object`` extension
in ``plugins.dicom.apps.DicomConfig.ready()``, mirroring the media app's
extension of the same name. A ``DicomStudy`` attached to a parent object
(typically the ``Recording`` acquired in the same session) is readable by
anyone who can read that parent — from any source: a direct ``AccessRight``
on the parent, dataset share inheritance, or a share token.
"""

from epicurrents.permissions import ReadAccessTerms


def can_read_via_attachment(user, obj, share_token: str | None = None):
    """Return a ReadAccessTerms granting read on a DicomStudy via its attachment.

    Called automatically by ``can_read_object`` through the extension
    registry. Applies only to active (non-trashed) ``DicomStudy`` instances
    attached to a parent the caller can read; everything else yields
    ``granted=False`` so other extensions and the default deny still apply.
    Returns default metadata (``apply_middleware=False``) because DICOM bytes
    never pass through the EDF sanitization pipeline — the parent's
    header-anonymisation flag has no meaning for them.
    """
    from epicurrents.permissions import can_read_object
    from plugins.dicom.models import DicomStudy
    from recordings.models import Recording

    if not isinstance(obj, DicomStudy):
        return ReadAccessTerms(granted=False)
    # A trashed study is never readable, not even through its parent.
    if obj.deleted_at is not None:
        return ReadAccessTerms(granted=False)
    # GenericForeignKey; None when unattached or the target row is gone.
    parent = obj.attachment
    if parent is None:
        return ReadAccessTerms(granted=False)
    # FAILED-recording hiding: a failed upload is visible only to its author
    # and superusers. A grantee can still hold a stale can_read AccessRight on
    # a recording that later failed, which can_read_object below would resolve
    # as readable, so deny here to keep attached studies from becoming a side
    # channel around the hiding rule. Mirrors media.permissions.
    if isinstance(parent, Recording) and parent.status == Recording.Status.FAILED:
        is_author = bool(user and getattr(user, "is_authenticated", False) and parent.author_id == user.pk)
        is_superuser = bool(user and getattr(user, "is_superuser", False))
        if not (is_author or is_superuser):
            return ReadAccessTerms(granted=False)
    if can_read_object(user=user, obj=parent, share_token=share_token):
        return ReadAccessTerms(granted=True)
    return ReadAccessTerms(granted=False)
