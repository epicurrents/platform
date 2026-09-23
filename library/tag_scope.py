"""Which tags a caller can reach, and who may create one.

Tags used to be listed to every authenticated user regardless of access, and a
tag name is free text typed by whoever created it. The scope here is what the
tag endpoints consult before listing, reading or applying a tag:

- a superuser reaches every tag;
- everyone else reaches the curated tags (created by staff, the deployment's
  vocabulary), the tags they authored, the tags on objects they can read, and
  the ancestors of all of those, so the tree stays browsable from its root.

Reach through a readable object is decided with the same per-item check the
item listings use, over the distinct objects that carry a tag not already in
reach. Tagging is manual, so that set is bounded by what people have tagged
rather than by what the platform holds.

Creation is reserved for staff while ``LIBRARY_TAG_CREATION_REQUIRES_STAFF`` is
on, which is the default; a tag a staff member creates is stamped curated.
"""

from __future__ import annotations

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.db.models import Q

from library.item_access import user_can_read_item
from library.models import Tag, TaggedItem


def is_staff(user) -> bool:
    """Whether *user* is on the staff tier (staff or superuser)."""
    return bool(getattr(user, "is_staff", False) or getattr(user, "is_superuser", False))


def can_create_tag(user) -> bool:
    """Whether *user* may create a tag under the deployment's creation policy."""
    if not getattr(settings, "LIBRARY_TAG_CREATION_REQUIRES_STAFF", True):
        return True
    return is_staff(user)


def visible_tag_ids(user) -> set[int] | None:
    """Return the ids of the tags *user* can reach, or ``None`` for a superuser (no restriction)."""
    if getattr(user, "is_superuser", False):
        return None

    visible = set(Tag.objects.filter(Q(author=user) | Q(curated=True)).values_list("pk", flat=True))

    by_object: dict[tuple[int, str], set[int]] = {}
    rows = TaggedItem.objects.exclude(tag_id__in=visible).values_list("tag_id", "content_type_id", "object_id")
    for tag_id, ct_id, object_id in rows:
        by_object.setdefault((ct_id, object_id), set()).add(tag_id)
    for (ct_id, object_id), tag_ids in by_object.items():
        ct = ContentType.objects.get_for_id(ct_id)
        if user_can_read_item(user, ct, object_id):
            visible |= tag_ids

    if not visible:
        return visible

    parent_of = dict(Tag.objects.filter(parent__isnull=False).values_list("pk", "parent_id"))
    frontier = list(visible)
    while frontier:
        parent_id = parent_of.get(frontier.pop())
        if parent_id is not None and parent_id not in visible:
            visible.add(parent_id)
            frontier.append(parent_id)
    return visible


def tag_reachable(user, tag_id: int) -> bool:
    """Whether a single tag is within *user*'s reach; the per-object form of ``visible_tag_ids``."""
    ids = visible_tag_ids(user)
    return ids is None or tag_id in ids
