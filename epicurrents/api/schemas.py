"""Response schemas shared by the apps that expose ``AccessRight`` rows.

An access right is one shape regardless of what it grants access to, and more
than one app now serves it — library for collections and datasets, recordings
for recordings. Defining it here rather than per app keeps a field added to the
model from reaching one serializer and not the other, which is the failure that
would show up as an access-management screen quietly missing a permission
column for one object type.

Serving a right does not imply anyone may see it: the caller's own permission
check decides that, and each app runs its own before calling in here. The
assessment pair is the one part of the row with a second gate: it is the
sharer's record, served to the row's giver, the object's author and superusers,
and ``None`` to everyone else, so ``access_right_out`` takes that decision as a
required argument rather than guessing.
"""

from datetime import date, datetime

from ninja import Schema
from ninja.errors import HttpError

from epicurrents.assessment import normalise_assessment


class AccessRightOut(Schema):
    """One access-right row, with the target's display name resolved.

    ``assessment_reference`` and ``assessment_date`` are ``None`` both when no
    assessment is recorded and when the caller may not see it, so a grantee who
    can list rights cannot tell the two apart.
    """

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
    assessment_reference: str | None = None
    assessment_date: date | None = None


class AssessmentIn(Schema):
    """Record, update or clear the sharer's contextual assessment on a grant.

    Both fields or neither: an empty reference with a null date clears the record.
    """

    assessment_reference: str = ""
    assessment_date: date | None = None


def assessment_values(reference: str | None, assessment_date: date | None) -> tuple[str, date | None]:
    """The stored ``(reference, date)`` pair for a request's values, or a 400 naming the problem."""
    try:
        return normalise_assessment(reference, assessment_date)
    except ValueError as exc:
        raise HttpError(400, str(exc)) from exc


def apply_assessment(right, payload: AssessmentIn) -> None:
    """Set the assessment pair on *right* from *payload* without saving, or raise a 400."""
    right.assessment_reference, right.assessment_date = assessment_values(
        payload.assessment_reference, payload.assessment_date
    )


def access_right_out(right, *, assessment_visible: bool) -> AccessRightOut:
    """Serialize an AccessRight ORM row, resolving display names from related objects.

    Caller must ensure *right* was fetched with ``select_related("access_target",
    "access_target_group")`` so no extra queries are issued here, and must decide
    *assessment_visible* with ``epicurrents.assessment.assessment_visible``.
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
        assessment_reference=(right.assessment_reference or None) if assessment_visible else None,
        assessment_date=right.assessment_date if assessment_visible else None,
    )
