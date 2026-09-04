"""Models for inter-instance federation.

``FederatedPeer`` represents a trusted remote instance.  ``FederationAuditLog``
records every inbound access by a federated peer for compliance reconstruction
("which peer accessed which object on whose behalf at what time").
"""

from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db import models


class FederatedPeer(models.Model):
    """A remote app instance that this instance may exchange data with.

    The ``url`` is the canonical HTTPS base URL of the peer instance and acts
    as its unique identity (same value published in the ``iss``/``aud`` JWT
    claims).

    ``is_trusted`` must be set to True by a superuser before any inbound
    requests from the peer are accepted.  Auto-discovered peers (e.g. via
    ``fetch_peer_public_key``) are created with ``is_trusted=False``.
    """

    url = models.URLField(max_length=512, unique=True)
    display_name = models.CharField(max_length=255, blank=True)

    # Ed25519 public key as URL-safe base64url (43 chars, no padding).
    public_key = models.CharField(max_length=64)
    # Announced-next public key during a rotation overlap window.  Empty when
    # the peer is not advertising a forthcoming rotation.  See
    # federation/README.md → "Key rotation" for the overlap protocol.
    public_key_next = models.CharField(max_length=64, blank=True, default="")
    public_key_fetched_at = models.DateTimeField(null=True, blank=True)

    is_trusted = models.BooleanField(default=False, db_index=True)

    added_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="added_federated_peers",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    modified_at = models.DateTimeField(auto_now=True)

    class Meta:
        # No extra indexes needed — the unique=True on `url` already creates a
        # B-tree index covering every lookup on that field.
        pass

    def __str__(self) -> str:
        return self.display_name or self.url


class FederationAuditLog(models.Model):
    """Append-only audit trail for inbound federation access.

    One row per inbound federation request that reached the access-decision
    stage: every successful access and every access denial.  Auth failures
    (bad token, untrusted peer) are intentionally **not** logged here — they
    surface in Django's WARNING log via ``federation.api.v1.ninja`` and are
    noisier than the compliance question warrants.  The compliance question
    is "which peer, acting on whose behalf, touched which object when, with
    what outcome", and that is what this table answers.

    Operational policy (enforced by convention, not by DB constraints):

    * **Append-only.** Application code never updates or deletes rows; the
      retention pruning job is the only intentional deletion path.
    * **Retention.** The deployment's regulatory minimum.  HIPAA-style
      deployments must keep at least 6 years; lower thresholds are
      configuration choices, not code choices.
    * **Export.** Rows are exportable as a flat CSV for SAR / breach response
      via a management command (planned follow-up; not in this initial commit).

    What this table does *not* try to answer:

    * Whether the matching grant was an exact-match (``AccessRight.remote_user_id == sub``)
      or a wildcard (``remote_user_id == ""``).  Query the ``AccessRight``
      table for the recording at the audit timestamp if forensics needs it.
    """

    peer = models.ForeignKey(
        FederatedPeer,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="audit_logs",
    )
    # Denormalised peer URL so the audit row survives FederatedPeer deletion.
    # If you need to know which peer made a request after the peer record is
    # gone, this is the field — peer_id will be NULL.
    peer_url = models.CharField(max_length=512)

    # JWT ``sub`` claim — the user identity on the remote peer.  Always
    # populated for a row that reached the access-decision stage (a valid
    # JWT carries ``sub``).  Distinct from ``AccessRight.remote_user_id``,
    # which may be empty to denote a wildcard grant.
    remote_user_id = models.CharField(max_length=512)

    # Endpoint name; matches the request handler symbol so it groups cleanly
    # in audit reports ("download_recording", "inbound_check_object", ...).
    action = models.CharField(max_length=64)

    target_content_type = models.ForeignKey(
        ContentType,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="federation_audit_logs",
    )
    target_object_id = models.CharField(max_length=255, blank=True, default="")
    target = GenericForeignKey("target_content_type", "target_object_id")

    status_code = models.IntegerField()

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["peer", "-created_at"]),
            models.Index(fields=["target_content_type", "target_object_id", "-created_at"]),
        ]

    def __str__(self) -> str:
        return f"FedAudit({self.peer_url}/{self.remote_user_id} → {self.action} = {self.status_code})"
