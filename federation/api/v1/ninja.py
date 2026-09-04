"""Federation REST API.

Endpoints
---------
Peer management (superuser only)
    GET    /peers/                      — list known peers
    POST   /peers/                      — register a new peer (fetches its key)
    GET    /peers/{id}/                 — get peer detail
    PATCH  /peers/{id}/                 — update display_name / is_trusted
    DELETE /peers/{id}/                 — remove peer
    POST   /peers/{id}/refresh-key/     — re-fetch public key from remote

Grant management (authenticated users with can_share on target object)
    GET    /grants/                     — list federation grants I have given
    POST   /grants/                     — create a federation grant
    PATCH  /grants/{id}/               — update a grant's expiry (renew)
    DELETE /grants/{id}/               — revoke a federation grant

Inbound (called by remote instances; auth via FederatedBearer JWT)
    GET    /inbound/objects/{ct_id}/{object_id}/   — check access + return metadata
"""

from __future__ import annotations

import logging
from datetime import datetime

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from ninja import NinjaAPI, Schema
from ninja.errors import HttpError

from activity.audit import log_activity
from epicurrents.auth import enforce_session_csrf
from epicurrents.models import AccessRight
from federation import services
from federation.audit import log_federation_access
from federation.auth import parse_federation_auth
from federation.limits import QuotaExceeded, check_peer_inbound_rate
from federation.models import FederatedPeer
from federation.services import FederationServiceError

logger = logging.getLogger(__name__)

api = NinjaAPI(
    title="Federation API",
    version="1",
    urls_namespace="federation-api-v1",
    docs_url=settings.API_DOCS_URL,
    openapi_url=settings.API_OPENAPI_URL,
)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class PeerIn(Schema):
    """Payload for registering a new peer."""

    url: str
    display_name: str = ""


class PeerPatchIn(Schema):
    """Partial update for a peer."""

    display_name: str | None = None
    is_trusted: bool | None = None


class PeerOut(Schema):
    id: int
    url: str
    display_name: str
    public_key: str
    public_key_next: str
    public_key_fetched_at: datetime | None
    is_trusted: bool
    added_by_id: int | None
    created_at: datetime
    modified_at: datetime


class FederatedGrantIn(Schema):
    """Payload for creating a federation access right."""

    federated_peer_id: int
    remote_user_id: str = ""  # blank = any authenticated user from that peer
    content_type_id: int
    object_id: str
    can_read: bool = True
    can_write: bool = False
    can_share: bool = False
    # None applies the fail-safe default (True): EDF/BDF bytes served under
    # the grant are de-identified. Explicit False serves raw bytes.
    apply_middleware: bool | None = None
    expires_at: datetime | None = None


class FederatedGrantPatchIn(Schema):
    """Partial update for a federation grant. Currently only the expiry."""

    expires_at: datetime | None = None


class FederatedGrantOut(Schema):
    id: int
    federated_peer_id: int
    remote_user_id: str
    content_type_id: int
    object_id: str
    can_read: bool
    can_write: bool
    can_share: bool
    apply_middleware: bool
    expires_at: datetime | None
    created_at: datetime


class InboundObjectOut(Schema):
    """Minimal metadata returned to a trusted peer for an accessible object."""

    content_type_id: int
    object_id: str
    model: str  # e.g. "recording"
    app_label: str  # e.g. "recordings"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _require_auth(request):
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated:
        raise HttpError(401, "Authentication credentials were not provided")
    enforce_session_csrf(request)
    return user


def _require_superuser(request):
    user = _require_auth(request)
    if not getattr(user, "is_superuser", False):
        raise HttpError(403, "Superuser access required")
    return user


def _get_peer(peer_id: int) -> FederatedPeer:
    peer = FederatedPeer.objects.filter(pk=peer_id).first()
    if peer is None:
        raise HttpError(404, "Federated peer not found")
    return peer


def _require_federation_auth(request) -> tuple[FederatedPeer, str]:
    """Raising wrapper around :func:`federation.auth.parse_federation_auth`.

    Returns ``(peer, remote_user_id)`` on success; raises ``HttpError`` with
    the parser's status code and message on failure.
    """
    result = parse_federation_auth(request)
    if not result.ok:
        raise HttpError(*result.error)
    return result.peer, result.remote_user_id


def _service_call(fn, *args, **kwargs):
    """Invoke a ``federation.services`` function, mapping its error to ``HttpError``."""
    try:
        return fn(*args, **kwargs)
    except FederationServiceError as exc:
        raise HttpError(exc.code, exc.message) from exc


# ---------------------------------------------------------------------------
# Peer management
# ---------------------------------------------------------------------------


@api.get("/peers/", response=list[PeerOut])
def list_peers(request):
    """List all known federated peers (superuser only)."""
    _require_superuser(request)
    peers = list(FederatedPeer.objects.order_by("url"))
    log_activity(
        verb="federation.peer.list",
        metadata={"returned_count": len(peers)},
    )
    return peers


@api.post("/peers/", response=PeerOut)
def create_peer(request, payload: PeerIn):
    """Register a new federated peer.

    Automatically fetches the peer's public key from its well-known URL.
    The peer is created with ``is_trusted=False``; a superuser must explicitly
    trust it before inbound requests are accepted.
    """
    user = _require_superuser(request)
    return _service_call(
        services.register_peer,
        url=payload.url,
        display_name=payload.display_name,
        added_by=user,
    )


@api.get("/peers/{peer_id}/", response=PeerOut)
def get_peer(request, peer_id: int):
    """Get a specific federated peer (superuser only)."""
    _require_superuser(request)
    peer = _get_peer(peer_id)
    log_activity(verb="federation.peer.read", target=peer)
    return peer


@api.patch("/peers/{peer_id}/", response=PeerOut)
def update_peer(request, peer_id: int, payload: PeerPatchIn):
    """Update display name and/or trust status of a peer (superuser only)."""
    _require_superuser(request)
    peer = _get_peer(peer_id)

    fields_updated: list[str] = []
    if payload.display_name is not None:
        peer.display_name = payload.display_name
        fields_updated.append("display_name")
    if payload.is_trusted is not None:
        peer.is_trusted = payload.is_trusted
        fields_updated.append("is_trusted")
    with transaction.atomic():
        peer.save()
        log_activity(
            verb="federation.peer.update",
            target=peer,
            metadata={"fields_updated": fields_updated},
        )
    return peer


@api.delete("/peers/{peer_id}/")
def delete_peer(request, peer_id: int):
    """Remove a federated peer and all associated grants (superuser only)."""
    _require_superuser(request)
    peer = _get_peer(peer_id)
    services.delete_peer(peer)
    return {"detail": "Peer deleted"}


@api.post("/peers/{peer_id}/refresh-key/", response=PeerOut)
def refresh_peer_key(request, peer_id: int):
    """Re-fetch and update the public key for a peer (superuser only).

    A key rotation on the remote peer is the normal case for this endpoint;
    an unexpected key change is also how a man-in-the-middle would manifest.
    The service logs every change at WARNING with both fingerprints so it shows
    up in the audit trail even though it proceeds with the overwrite.
    """
    _require_superuser(request)
    peer = _get_peer(peer_id)
    peer, _ = _service_call(services.refresh_peer_key, peer)
    return peer


# ---------------------------------------------------------------------------
# Grant management
# ---------------------------------------------------------------------------


@api.get("/grants/", response=list[FederatedGrantOut])
def list_grants(request):
    """List federation grants I have given (authenticated users)."""
    user = _require_auth(request)
    qs = AccessRight.objects.filter(
        access_giver=user,
        federated_peer__isnull=False,
    ).order_by("-created_at")
    grants = list(qs)
    log_activity(
        verb="federation.grant.list",
        metadata={"returned_count": len(grants)},
    )
    return grants


@api.post("/grants/", response=FederatedGrantOut)
def create_grant(request, payload: FederatedGrantIn):
    """Create a federation access right.

    The caller must hold ``can_share=True`` on the target object
    (or be its author / superuser).
    """

    user = _require_auth(request)

    peer = FederatedPeer.objects.filter(pk=payload.federated_peer_id).first()
    if peer is None:
        raise HttpError(404, "Federated peer not found")

    ct = ContentType.objects.filter(pk=payload.content_type_id).first()
    if ct is None:
        raise HttpError(404, "Content type not found")

    return _service_call(
        services.create_grant,
        giver=user,
        peer=peer,
        content_type=ct,
        object_id=payload.object_id,
        remote_user_id=payload.remote_user_id,
        can_read=payload.can_read,
        can_write=payload.can_write,
        can_share=payload.can_share,
        apply_middleware=payload.apply_middleware,
        expires_at=payload.expires_at,
    )


@api.patch("/grants/{grant_id}/", response=FederatedGrantOut)
def patch_grant(request, grant_id: int, payload: FederatedGrantPatchIn):
    """Set a federation grant's expiry (must be the original giver or superuser).

    This is the only update path for a grant's ``expires_at``. The provided
    value replaces the current expiry; ``null`` makes the grant non-expiring.
    """
    user = _require_auth(request)
    grant = _service_call(services.get_grant, grant_id)
    return _service_call(services.renew_grant, grant=grant, actor=user, expires_at=payload.expires_at)


@api.delete("/grants/{grant_id}/")
def delete_grant(request, grant_id: int):
    """Revoke a federation grant (must be the original giver or superuser)."""
    user = _require_auth(request)
    grant = _service_call(services.get_grant, grant_id)
    _service_call(services.revoke_grant, grant=grant, actor=user)
    return {"detail": "Grant revoked"}


# ---------------------------------------------------------------------------
# Inbound endpoints (called by remote instances with FederatedBearer auth)
# ---------------------------------------------------------------------------


@api.get("/inbound/objects/{ct_id}/{object_id}/", response=InboundObjectOut)
def inbound_check_object(request, ct_id: int, object_id: str):
    """Verify that the calling peer's user may read the specified object.

    The remote instance calls this endpoint when a local user (on the remote
    side) requests an object that lives here.  The JWT carries the remote
    user's identifier in ``sub``.

    Returns 200 + metadata if access is permitted.  All "no result" outcomes
    (missing content type, missing object, no grant) collapse to a single 404
    with the same message — an attacker must not be able to distinguish
    "object does not exist" from "object exists but you're not authorized",
    since the latter leaks the existence of PHI-adjacent records to remote
    peers.  Auth failures keep their distinct 401 because they signal a
    peer-side credential problem, not an object-existence question.
    """
    peer, remote_user_id = _require_federation_auth(request)

    # Set the verb up front so every outcome — success, rate-limit, deny,
    # FAILED-hidden — carries the federation.inbound.probe taxonomy on
    # its Activity row. Peer metadata applies to every branch; the
    # probed_* identifiers are added only in the deny/rate-limit branches
    # below, since on the success path target_* (set via target=obj)
    # carries the same information without duplication.
    log_activity(
        verb="federation.inbound.probe",
        metadata={
            "peer_id": peer.pk,
            "peer_url": peer.url,
            "remote_user_id": remote_user_id,
        },
    )

    # Inbound rate limit — slows object-id enumeration by a compromised peer.
    # Checked before any DB work so a rejected probe is cheap to serve.
    try:
        check_peer_inbound_rate(peer)
    except QuotaExceeded as exc:
        log_activity(
            verb="federation.inbound.probe",
            metadata={
                "probed_content_type_id": ct_id,
                "probed_object_id": str(object_id),
            },
        )
        log_federation_access(
            peer=peer,
            remote_user_id=remote_user_id,
            action="inbound_check_object",
            target_content_type_id=ct_id,
            target_object_id=str(object_id),
            status_code=429,
        )
        raise HttpError(429, str(exc))

    # NOT_FOUND_MSG is reused across every below-auth failure path so the
    # response body is byte-identical regardless of which branch triggered it.
    NOT_FOUND_MSG = "Object not found or access denied"

    def deny(obj=None):
        # The peer-facing response is identical regardless of branch — see
        # commit 5's indistinguishability invariant — but the audit row records
        # the probed identifier so forensics can distinguish "peer is probing
        # non-existent IDs" from "peer keeps hitting objects whose grant was
        # revoked".  ``target=obj`` is used when we resolved a real instance;
        # otherwise the raw path params carry the probed identifier.
        log_activity(
            verb="federation.inbound.probe",
            metadata={
                "probed_content_type_id": ct_id,
                "probed_object_id": str(object_id),
            },
        )
        log_federation_access(
            peer=peer,
            remote_user_id=remote_user_id,
            action="inbound_check_object",
            target=obj,
            target_content_type_id=ct_id if obj is None else None,
            target_object_id=str(object_id) if obj is None else "",
            status_code=404,
        )
        raise HttpError(404, NOT_FOUND_MSG)

    ct = ContentType.objects.filter(pk=ct_id).first()
    if ct is None:
        deny()

    model_class = ct.model_class()
    if model_class is None:
        deny()

    obj = model_class.objects.filter(pk=object_id).first()
    if obj is None:
        deny()

    if not AccessRight.can_federated_peer_read(peer=peer, remote_user_id=remote_user_id, obj=obj):
        deny(obj=obj)

    # FAILED recordings are hidden from federated peers (and every other
    # grantee surface).  Collapse into the same 404 as the missing-object
    # path so the peer cannot distinguish "this recording was rejected by
    # ingest" from "no such object".
    from recordings.models import Recording

    if isinstance(obj, Recording) and obj.status == Recording.Status.FAILED:
        deny(obj=obj)

    log_federation_access(
        peer=peer,
        remote_user_id=remote_user_id,
        action="inbound_check_object",
        target=obj,
        status_code=200,
    )
    log_activity(verb="federation.inbound.probe", target=obj)
    return InboundObjectOut(
        content_type_id=ct_id,
        object_id=object_id,
        model=ct.model,
        app_label=ct.app_label,
    )
