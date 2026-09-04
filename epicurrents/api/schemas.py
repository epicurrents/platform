"""Response schemas shared by the apps that expose ``AccessRight`` rows.

An access right is one shape regardless of what it grants access to, and more
than one app now serves it — library for collections and datasets, recordings
for recordings. Defining it here rather than per app keeps a field added to the
model from reaching one serializer and not the other, which is the failure that
would show up as an access-management screen quietly missing a permission
column for one object type.

Serving a right does not imply anyone may see it: the caller's own permission
check decides that, and each app runs its own before calling in here.
"""

from datetime import datetime

from ninja import Schema


class AccessRightOut(Schema):
    """One access-right row, with the target's display name resolved."""

    id: int
    access_target_id: int | None
    access_target_username: str | None = None
    access_target_group_id: int | None
    access_target_group_name: str | None = None
    public_share_token: str | None
    can_read: bool
    can_write: bool
    can_share: bool
    apply_middleware: bool
    expires_at: datetime | None


def access_right_out(right) -> AccessRightOut:
    """Serialize an AccessRight ORM row, resolving display names from related objects.

    Caller must ensure *right* was fetched with ``select_related("access_target",
    "access_target_group")`` so no extra queries are issued here.
    """
    return AccessRightOut(
        id=right.id,
        access_target_id=right.access_target_id,
        access_target_username=(right.access_target.username if right.access_target_id is not None else None),
        access_target_group_id=right.access_target_group_id,
        access_target_group_name=(right.access_target_group.name if right.access_target_group_id is not None else None),
        public_share_token=right.public_share_token,
        can_read=right.can_read,
        can_write=right.can_write,
        can_share=right.can_share,
        apply_middleware=right.apply_middleware,
        expires_at=right.expires_at,
    )
