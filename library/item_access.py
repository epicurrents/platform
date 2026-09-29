"""Per-item read check shared by the library's mixed-type listings and the tag scope.

Collection, dataset and tag item rows point at objects of any content type
through a generic key, so a listing decides row by row whether the caller may
see what the row points at. ``user_can_read_item`` is that decision: the
superuser and author fast paths, soft-deleted and missing targets treated as
unreadable, and ``can_read_object`` for everything else, so every extension
registered with the permission layer (dataset membership, attached media)
applies without this module knowing about it.
"""

from __future__ import annotations

from django.contrib.contenttypes.models import ContentType


def user_can_read_item(user, ct: ContentType, object_id: str) -> bool:
    """Return True if *user* may read the object referenced by *ct* + *object_id*.

    Fast paths (no AccessRight query):
    - superuser → always True
    - object author → True

    Falls back to ``can_read_object`` (which checks AccessRights and all
    registered extensions including Dataset membership). Soft-deleted or
    missing objects are treated as unreadable and return False.
    """
    from epicurrents.permissions import can_read_object

    if getattr(user, "is_superuser", False):
        return True

    model_class = ct.model_class()
    if model_class is None:
        return False

    qs = model_class.objects.filter(pk=object_id)
    if hasattr(model_class, "deleted_at"):
        qs = qs.filter(deleted_at__isnull=True)
    obj = qs.first()
    if obj is None:
        return False

    if getattr(obj, "author_id", None) == user.pk:
        return True

    return can_read_object(user=user, obj=obj)
