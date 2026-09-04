"""Delegation capping for object-level access grants.

The granting right (``can_share``) is a delegation, and a delegation must not
amplify: a grant created by anyone but the object's author or a superuser may
confer only rights the grantor actually holds on that object. Without this cap
a share-only grantee could mint write access they do not hold, or — the severe
variant — grant *themselves* a row with ``apply_middleware=False`` and read raw
bytes their own de-identified grant was scoped to withhold.

Every endpoint that creates or deletes ``AccessRight`` rows on an existing
object routes through the helpers here, so the rule has one construction site
(the lesson of the serving-pipeline parity contract). Author-issued grants made
at object creation (e.g. the recordings upload endpoint) are outside this
module's scope: their grantor is the author, whose rights are unrestricted.

Contract tests: ``library/tests/test_grant_capping.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from django.contrib.contenttypes.models import ContentType
from django.utils import timezone
from ninja.errors import HttpError

from epicurrents.models import AccessRight
from epicurrents.permissions import can_modify_object
from epicurrents.security_log import get_client_ip, log_security_event


@dataclass(frozen=True)
class ConferrableRights:
    """The ceiling of rights one user may confer on one object.

    ``unrestricted`` marks the author / superuser case; every flag below it is
    then irrelevant. ``can_read`` through ``raw_access``
    record what the grantor's own active rows hold; ``raw_access`` marks whether
    their read access includes the raw bytes (some active row with ``can_read=True`` and
    ``apply_middleware=False``), which is what entitles them to confer
    ``apply_middleware=False`` onward. ``share_until`` is the latest expiry
    among the grantor's active ``can_share`` rows — ``None`` means unbounded —
    and caps the expiry of any grant they create.
    """

    unrestricted: bool = False
    can_read: bool = False
    can_write: bool = False
    can_share: bool = False
    raw_access: bool = False
    share_until: datetime | None = None


def conferrable_rights(user: Any, obj: Any) -> ConferrableRights:
    """Resolve the rights *user* may confer on *obj*.

    Authors and superusers are unrestricted. Everyone else is folded over
    their *active* direct rows (user- or group-targeted, unexpired) — expired
    share rows confer nothing.
    Extension-derived read access (dataset membership) never carries
    ``can_share`` and therefore never reaches this fold.
    """

    if can_modify_object(user=user, obj=obj):
        return ConferrableRights(unrestricted=True, can_read=True, can_write=True, can_share=True, raw_access=True)

    can_read = False
    can_write = False
    can_share = False
    raw_access = False
    share_until: datetime | None = None
    share_unbounded = False
    for row in AccessRight.objects.for_object(obj).for_target(user).active():
        can_read = can_read or row.can_read
        can_write = can_write or row.can_write
        if row.can_read and not row.apply_middleware:
            raw_access = True
        if row.can_share:
            can_share = True
            if row.expires_at is None:
                share_unbounded = True
            elif share_until is None or row.expires_at > share_until:
                share_until = row.expires_at

    return ConferrableRights(
        unrestricted=False,
        can_read=can_read,
        can_write=can_write,
        can_share=can_share,
        raw_access=raw_access,
        share_until=None if share_unbounded else share_until,
    )


def ensure_can_manage_access(user: Any, obj: Any, *, object_label: str, action: str) -> ConferrableRights:
    """Require author / superuser / active share rights on *obj*, or raise 403.

    Returns the resolved :class:`ConferrableRights` so the caller can pass it
    on to :func:`ensure_can_confer` without a second query. ``action`` selects
    the endpoint's established message ("share" for grant creation, "manage
    access for" for listing and revocation).
    """

    rights = conferrable_rights(user, obj)
    if not (rights.unrestricted or rights.can_share):
        raise HttpError(403, f"You do not have permission to {action} this {object_label}")
    return rights


def ensure_can_confer(
    request: Any,
    user: Any,
    obj: Any,
    rights: ConferrableRights,
    *,
    can_read: bool,
    can_write: bool,
    can_share: bool,
    apply_middleware: bool,
    expires_at: datetime | None,
    share_token: bool = False,
) -> None:
    """Refuse a grant that would amplify the grantor's rights.

    Assumes :func:`ensure_can_manage_access` already qualified the caller.
    Share-token rows are refused ``can_write`` / ``can_share`` outright — for
    every grantor, authors included — per the platform invariant that token
    rows never carry them; refusal beats silent downgrade so a miswired client
    finds out. Each amplification refusal is security-logged with identifiers
    only, since a grant exceeding the grantor's rights is what a privilege
    escalation attempt looks like.
    """

    if share_token and (can_write or can_share):
        raise HttpError(400, "Share-token grants cannot carry write or share permissions")

    if rights.unrestricted:
        return

    # Clients are not obliged to send timezone-aware expiries; a naive one is
    # interpreted in the server's current timezone rather than crashing the
    # comparison against the grantor's (aware) share expiry.
    if expires_at is not None and timezone.is_naive(expires_at):
        expires_at = timezone.make_aware(expires_at)

    if can_read and not rights.can_read:
        refused = "can_read"
    elif can_write and not rights.can_write:
        refused = "can_write"
    elif not apply_middleware and not rights.raw_access:
        refused = "apply_middleware"
    elif rights.share_until is not None and (expires_at is None or expires_at > rights.share_until):
        refused = "expires_at"
    else:
        return

    content_type = ContentType.objects.get_for_model(obj, for_concrete_model=False)
    log_security_event(
        "permission.grant_amplification_refused",
        actor_id=user.pk,
        ip=get_client_ip(request),
        object_type=f"{content_type.app_label}.{content_type.model}",
        object_id=str(obj.pk),
        reason=refused,
    )
    raise HttpError(403, "You cannot confer rights beyond those you hold on this object")


def ensure_can_revoke(
    request: Any,
    user: Any,
    obj: Any,
    right: AccessRight,
    rights: ConferrableRights,
) -> None:
    """Refuse revocation of the author's own access-right row by a delegate.

    A share-holder revoking arbitrary rows is the intended management surface,
    but the author's own row is load-bearing — several read paths resolve the
    author's access through it — so deleting it is a lockout, not management.
    Only the author or a superuser may remove it.
    """

    if rights.unrestricted:
        return
    author_id = getattr(obj, "author_id", None)
    if author_id is not None and right.access_target_id == author_id:
        content_type = ContentType.objects.get_for_model(obj, for_concrete_model=False)
        log_security_event(
            "permission.author_grant_revoke_refused",
            actor_id=user.pk,
            ip=get_client_ip(request),
            object_type=f"{content_type.app_label}.{content_type.model}",
            object_id=str(obj.pk),
        )
        raise HttpError(403, "Only the author or a superuser may revoke the author's own access right")
