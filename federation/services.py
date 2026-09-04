"""Shared federation domain operations for the REST API and management commands.

The Ninja endpoints and the federation management commands both call these
functions, so the SSRF guard, the trust gate, the object-level share check, and
the audited writes have a single implementation instead of drifting between an
API copy and a CLI copy. Request-layer concerns stay with the caller: the API
keeps session-CSRF and superuser gating in its endpoints, and the CLI assumes
operator authority (shell access). Each function raises
:class:`FederationServiceError` on a domain failure — the API maps it to
``HttpError``, the CLI to ``CommandError`` — and annotates the current audited
scope via :func:`activity.audit.log_activity`, which no-ops when there is none.
"""

import base64
import hashlib
import logging

from django.db import IntegrityError, transaction
from django.utils import timezone

from activity.audit import log_activity
from epicurrents.models import AccessRight
from federation.auth import fetch_peer_public_key
from federation.models import FederatedPeer

logger = logging.getLogger(__name__)


class FederationServiceError(Exception):
    """Domain-level failure carrying an HTTP-style status code and a message.

    The code lets the API layer raise a matching ``HttpError`` while the CLI
    layer surfaces the message as a ``CommandError``; neither layer has to know
    the other's error convention.
    """

    def __init__(self, code: int, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


def key_fingerprint(public_key_b64: str) -> str:
    """SHA-256 hex fingerprint of an Ed25519 public key given as URL-safe base64url.

    The fingerprint is what a peer administrator reads out-of-band to confirm a
    key before it is trusted; comparing full keys would work too, but a hash is
    shorter to transcribe and matches the TOFU-verification workflow.
    """
    padded = public_key_b64 + "=" * (-len(public_key_b64) % 4)
    raw = base64.urlsafe_b64decode(padded.encode())
    return hashlib.sha256(raw).hexdigest()


def _normalize_fingerprint(fp: str) -> str:
    return fp.strip().lower().replace(":", "").replace(" ", "")


def get_peer_by_ref(ref: str) -> FederatedPeer:
    """Resolve a peer by numeric id or exact URL. Raises if none matches."""
    ref = ref.strip()
    peer = None
    if ref.isdigit():
        peer = FederatedPeer.objects.filter(pk=int(ref)).first()
    if peer is None:
        peer = FederatedPeer.objects.filter(url=ref.rstrip("/")).first()
    if peer is None:
        raise FederationServiceError(404, f"Federated peer not found: {ref}")
    return peer


def register_peer(*, url: str, display_name: str = "", added_by=None) -> FederatedPeer:
    """Register a peer by URL, fetching its public key. Created untrusted.

    The key fetch runs through the SSRF guard and strict-TLS context in
    ``fetch_peer_public_key``, so a URL that resolves to a private / special-use
    address is refused here. Trust is a separate, explicit step
    (:func:`set_peer_trust`).
    """
    url = url.strip().rstrip("/")
    if not url.startswith("https://"):
        raise FederationServiceError(400, "Peer URL must use HTTPS.")
    if FederatedPeer.objects.filter(url=url).exists():
        raise FederationServiceError(409, "A peer with this URL already exists.")

    try:
        key_b64, next_key_b64 = fetch_peer_public_key(url)
    except ValueError as exc:
        raise FederationServiceError(502, str(exc))

    with transaction.atomic():
        peer = FederatedPeer.objects.create(
            url=url,
            display_name=display_name,
            public_key=key_b64,
            public_key_next=next_key_b64,
            public_key_fetched_at=timezone.now(),
            is_trusted=False,
            added_by=added_by,
        )
        log_activity(verb="federation.peer.create", target=peer)
    return peer


def set_peer_trust(peer: FederatedPeer, *, trusted: bool, expected_fingerprint: str | None = None) -> FederatedPeer:
    """Set a peer's trust flag. When trusting, an ``expected_fingerprint`` is verified first.

    Passing the fingerprint the peer administrator confirmed out-of-band turns
    the TOFU verification from a documented manual step into an enforced one —
    the trust flip fails if the stored key does not match.
    """
    if trusted and expected_fingerprint:
        actual = key_fingerprint(peer.public_key)
        if _normalize_fingerprint(expected_fingerprint) != actual:
            raise FederationServiceError(
                400,
                f"Fingerprint mismatch: peer key fingerprint is {actual}. Refusing to trust.",
            )
    peer.is_trusted = trusted
    with transaction.atomic():
        peer.save()
        log_activity(
            verb="federation.peer.update",
            target=peer,
            metadata={"fields_updated": ["is_trusted"]},
        )
    return peer


def set_peer_display_name(peer: FederatedPeer, display_name: str) -> FederatedPeer:
    """Update a peer's human label."""
    peer.display_name = display_name
    with transaction.atomic():
        peer.save()
        log_activity(
            verb="federation.peer.update",
            target=peer,
            metadata={"fields_updated": ["display_name"]},
        )
    return peer


def refresh_peer_key(peer: FederatedPeer) -> tuple[FederatedPeer, bool]:
    """Re-fetch the peer's public key. Returns ``(peer, key_changed)``.

    A changed key is the normal outcome of a peer rotation, but it is also how a
    man-in-the-middle would manifest, so a change is logged at WARNING with both
    fingerprints regardless of whether we proceed with the overwrite.
    """
    try:
        key_b64, next_key_b64 = fetch_peer_public_key(peer.url)
    except ValueError as exc:
        raise FederationServiceError(502, str(exc))

    key_changed = bool(peer.public_key) and key_b64 != peer.public_key
    if key_changed:
        logger.warning(
            "Federation peer key changed on refresh: peer=%s (id=%d) old=%s... new=%s...",
            peer.url,
            peer.pk,
            peer.public_key[:12],
            key_b64[:12],
        )
    if next_key_b64 != peer.public_key_next:
        logger.info(
            "Federation peer next-key updated: peer=%s (id=%d) value=%s",
            peer.url,
            peer.pk,
            next_key_b64[:12] + "..." if next_key_b64 else "(cleared)",
        )

    peer.public_key = key_b64
    peer.public_key_next = next_key_b64
    peer.public_key_fetched_at = timezone.now()
    with transaction.atomic():
        peer.save()
        log_activity(
            verb="federation.peer.refresh_key",
            target=peer,
            metadata={"key_changed": key_changed},
        )
    return peer, key_changed


def delete_peer(peer: FederatedPeer) -> None:
    """Remove a peer, cascading to every grant that targets it."""
    with transaction.atomic():
        log_activity(verb="federation.peer.delete", target=peer)
        peer.delete()


def _resolve_object(content_type, object_id):
    model_class = content_type.model_class()
    if model_class is None:
        raise FederationServiceError(404, "Content type model not available.")
    obj = model_class.objects.filter(pk=object_id).first()
    if obj is None:
        raise FederationServiceError(404, "Object not found.")
    return obj


def create_grant(
    *,
    giver,
    peer: FederatedPeer,
    content_type,
    object_id,
    remote_user_id: str = "",
    can_read: bool = True,
    can_write: bool = False,
    can_share: bool = False,
    apply_middleware: bool | None = None,
    expires_at=None,
) -> AccessRight:
    """Create a federation grant on an object for a peer (optionally a specific remote user).

    ``giver`` must be the object's author, a superuser, or hold ``can_share`` on
    it — the same share authority the local grant surface enforces. An empty
    ``remote_user_id`` is the wildcard "any authenticated user from that peer".
    ``apply_middleware`` left as ``None`` applies the fail-safe default for
    cross-instance sharing: ``True``, so EDF/BDF bytes served under the grant
    pass through the de-identification pipeline (anonymized header, stripped
    annotation text). Pass ``False`` explicitly to serve raw bytes to the
    peer — a deliberate cross-controller PHI disclosure.
    """
    obj = _resolve_object(content_type, object_id)

    is_author = getattr(obj, "author_id", None) == giver.pk
    if not is_author and not getattr(giver, "is_superuser", False):
        has_share = AccessRight.objects.active().for_object(obj).for_target(giver).filter(can_share=True).exists()
        if not has_share:
            raise FederationServiceError(403, "The giver does not have permission to share this object.")

    if not (can_read or can_write or can_share):
        raise FederationServiceError(400, "At least one permission flag must be set.")

    fields = {
        "content_type": content_type,
        "object_id": str(object_id),
        "access_giver": giver,
        "federated_peer": peer,
        "remote_user_id": remote_user_id,
        "can_read": can_read,
        "can_write": can_write,
        "can_share": can_share,
        "expires_at": expires_at,
    }
    # The AccessRight model default (False) is tuned for local grants, where
    # access stays within this controller. A federated grant crosses to
    # another controller, so de-identification is on unless explicitly
    # declined.
    fields["apply_middleware"] = True if apply_middleware is None else apply_middleware

    if AccessRight.objects.filter(
        content_type=content_type,
        object_id=str(object_id),
        federated_peer=peer,
        remote_user_id=remote_user_id,
    ).exists():
        raise FederationServiceError(
            409, "A grant for this peer and remote user already exists on the object. Revoke it first."
        )

    try:
        with transaction.atomic():
            grant = AccessRight.objects.create(**fields)
            log_activity(verb="federation.grant.create", target=grant)
    except IntegrityError as exc:
        # Race backstop: the per-target uniqueness constraint catches a
        # concurrent duplicate the pre-check above ran too early to see.
        raise FederationServiceError(
            409, "A grant for this peer and remote user already exists on the object. Revoke it first."
        ) from exc
    return grant


def get_grant(grant_id: int) -> AccessRight:
    """Resolve a federation grant (an AccessRight with a peer target) by id."""
    grant = AccessRight.objects.filter(pk=grant_id, federated_peer__isnull=False).first()
    if grant is None:
        raise FederationServiceError(404, "Grant not found.")
    return grant


def _require_grant_control(grant: AccessRight, actor):
    # ``actor=None`` is a trusted system/operator caller (a management command run
    # with shell access); the authority check applies only to request-layer users.
    if actor is None:
        return
    if grant.access_giver_id != actor.pk and not getattr(actor, "is_superuser", False):
        raise FederationServiceError(403, "You do not have permission to modify this grant.")


def revoke_grant(*, grant: AccessRight, actor) -> None:
    """Revoke a grant. Only the original giver or a superuser may."""
    _require_grant_control(grant, actor)
    with transaction.atomic():
        log_activity(verb="federation.grant.revoke", target=grant)
        grant.delete()


def renew_grant(*, grant: AccessRight, actor, expires_at) -> AccessRight:
    """Set a grant's expiry. Only the original giver or a superuser may.

    ``expires_at=None`` makes the grant non-expiring. This is the only update
    path for ``AccessRight.expires_at`` on the federation surface.
    """
    _require_grant_control(grant, actor)
    grant.expires_at = expires_at
    with transaction.atomic():
        grant.save(update_fields=["expires_at"])
        # No metadata: AccessRight is tracked, so this save records the
        # expires_at from/to in the linked ObjectChangeLog already.
        log_activity(verb="federation.grant.renew", target=grant)
    return grant
