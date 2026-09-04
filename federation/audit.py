"""Audit log helpers for inbound federation requests.

The single entry point ``log_federation_access`` writes one
``FederationAuditLog`` row per inbound federation request that reached the
access-decision stage.  Endpoint handlers call it after the access decision
(success or denial); the helper takes care of the content-type lookup so call
sites stay one line.

Why not middleware: the audit row needs the target object, which only the
endpoint knows.  Stashing it on ``request`` and writing in middleware is
possible but adds a level of indirection for no benefit at our request volume.

Why not a model classmethod: the helper imports ``ContentType`` lazily so it
can be called from contexts that import ``federation.audit`` early in app
load without triggering circular imports.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db import models

if TYPE_CHECKING:
    from federation.models import FederatedPeer


def log_federation_access(
    *,
    peer: FederatedPeer,
    remote_user_id: str,
    action: str,
    target: models.Model | None = None,
    target_content_type_id: int | None = None,
    target_object_id: str = "",
    status_code: int = 200,
) -> None:
    """Write one ``FederationAuditLog`` row.

    Pass ``target`` for the normal case (granted access, denials of objects
    that exist).  When the endpoint denied access without ever resolving an
    actual model instance — a probe for a non-existent object id, an invalid
    content-type id — pass ``target_content_type_id`` and ``target_object_id``
    from the request path so the audit row still carries the probed identifier
    instead of degenerating into an indistinguishable empty-target row.

    Args:
        peer: The authenticated federated peer making the request.
        remote_user_id: The JWT ``sub`` claim (user identity on the peer).
        action: Endpoint name — match the request handler symbol so audit
            reports group cleanly ("download_recording", "inbound_check_object",
            ...).
        target: The object being accessed.  Takes precedence over
            ``target_content_type_id`` / ``target_object_id`` when given.
        target_content_type_id: Raw content-type PK from the request path,
            for probe-detection rows where no model instance was resolved.
            Ignored when ``target`` is given.
        target_object_id: Raw object identifier from the request path.
            Ignored when ``target`` is given.
        status_code: HTTP status returned to the peer.  ``200`` for granted
            access, ``404`` for denials and missing objects (see
            ``inbound_check_object`` docstring for the indistinguishability
            invariant), other codes for unusual conditions.
    """
    from django.contrib.contenttypes.models import ContentType

    from federation.models import FederationAuditLog

    target_ct = None
    object_id = ""
    if target is not None:
        target_ct = ContentType.objects.get_for_model(target, for_concrete_model=False)
        object_id = str(target.pk)
    else:
        if target_content_type_id is not None:
            target_ct = ContentType.objects.filter(pk=target_content_type_id).first()
        object_id = target_object_id

    FederationAuditLog.objects.create(
        peer=peer,
        peer_url=peer.url,
        remote_user_id=remote_user_id,
        action=action,
        target_content_type=target_ct,
        target_object_id=object_id,
        status_code=status_code,
    )
