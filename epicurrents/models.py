"""Core data models: ``AccessRight`` (object-level access control) and ``ViewerConfigOverride`` (editable viewer-config overrides).

``AccessRight`` is the storage layer for object-level access control. Each row is a single
grant: ``(content_type, object_id)`` × one target × permission booleans.
``epicurrents/permissions.py`` is the function-level API every endpoint
should call; this module is the data shape behind those calls.

The four target types (local user, local group, public share token,
federated peer) are enforced as mutually exclusive by
``access_right_exactly_one_target``; the at-least-one-permission
invariant by ``access_right_requires_some_permission``.  Both are
``CheckConstraint`` rows; the README (*Permissions* → *AccessRight
model*) walks through the cases.

``ViewerConfigOverride`` holds the editable, per-project overrides layered on
top of the active project's ``viewer-config.json`` seed; the seed loader and
effective-config merge live in ``epicurrents/viewer_config.py``.
"""

from django.conf import settings
from django.contrib.auth.models import Group
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db import models
from django.db.models import Q
from django.utils import timezone


class AccessRightQuerySet(models.QuerySet):
    """Query helpers for resolving active object-level access rights."""

    def active(self, at=None):
        now = at or timezone.now()
        return self.filter(Q(expires_at__isnull=True) | Q(expires_at__gt=now))

    def for_object(self, obj):
        """Filter rights that target *obj* (by content type + object_id)."""
        content_type = ContentType.objects.get_for_model(obj, for_concrete_model=False)
        return self.filter(content_type=content_type, object_id=str(obj.pk))

    def for_target(self, user):
        """Filter rights matching authenticated user or one of their groups."""

        if user and getattr(user, "is_authenticated", False):
            group_ids = list(user.groups.values_list("id", flat=True))
            target_filter = Q(access_target=user)
            if group_ids:
                target_filter |= Q(access_target_group_id__in=group_ids)
            return self.filter(target_filter)
        return self.none()

    def has_permission_for_ref(self, user, content_type, object_id, permission_field: str) -> bool:
        """Check permission by generic object reference for user/group targets."""

        if not (user and getattr(user, "is_authenticated", False)):
            return False

        group_ids = list(user.groups.values_list("id", flat=True))
        target_filter = Q(access_target=user)
        if group_ids:
            target_filter |= Q(access_target_group_id__in=group_ids)

        return (
            self.active()
            .filter(content_type=content_type, object_id=str(object_id))
            .filter(target_filter)
            .filter(**{permission_field: True})
            .exists()
        )

    def has_permission_for_token(self, token: str, content_type, object_id, permission_field: str) -> bool:
        """Check permission by explicit public share token target."""

        if not token:
            return False

        return (
            self.active()
            .filter(
                content_type=content_type,
                object_id=str(object_id),
                public_share_token=token,
                **{permission_field: True},
            )
            .exists()
        )

    def has_federated_permission(
        self,
        peer,
        remote_user_id: str,
        content_type,
        object_id,
        permission_field: str,
    ) -> bool:
        """Check permission for a federated peer + remote user pair.

        Matches rights where ``federated_peer`` equals ``peer`` and either
        ``remote_user_id`` matches exactly or is blank (wildcard — grants
        access to any authenticated user from that peer).
        """

        if peer is None:
            return False

        qs = self.active().filter(
            content_type=content_type,
            object_id=str(object_id),
            federated_peer=peer,
            **{permission_field: True},
        )
        return qs.filter(Q(remote_user_id="") | Q(remote_user_id=remote_user_id)).exists()


class AccessRight(models.Model):
    """Generic object-level access control entry across apps and models."""

    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE, related_name="access_rights")
    object_id = models.CharField(max_length=255)
    content_object = GenericForeignKey("content_type", "object_id")

    access_giver = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="granted_access_rights",
    )
    access_target = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="targeted_access_rights",
        null=True,
        blank=True,
    )
    access_target_group = models.ForeignKey(
        Group,
        on_delete=models.CASCADE,
        related_name="targeted_access_rights",
        null=True,
        blank=True,
    )
    public_share_token = models.CharField(max_length=128, null=True, blank=True, unique=True)

    # Federation target: grants access to a specific remote user (or any user
    # from the peer when remote_user_id is blank).
    federated_peer = models.ForeignKey(
        "federation.FederatedPeer",
        # CASCADE is intentional and load-bearing.  SET_NULL would violate the
        # CheckConstraint (exactly one of access_target / access_target_group /
        # public_share_token / federated_peer must be set); PROTECT would force
        # operators to revoke grants explicitly before peer deletion, which is
        # user-hostile for the common case.  The audit trail is preserved via
        # the activity app's pre_delete signal — each cascaded grant produces
        # an ObjectChangeLog row.  See federation/README.md → "Peer deletion".
        on_delete=models.CASCADE,
        related_name="access_rights",
        null=True,
        blank=True,
    )
    remote_user_id = models.CharField(
        max_length=512,
        blank=True,
        help_text="Remote user identifier (JWT 'sub') on the federated peer. Blank = any user from that peer.",
    )

    expires_at = models.DateTimeField(null=True, blank=True)

    can_read = models.BooleanField(default=True)
    can_write = models.BooleanField(default=False)
    can_share = models.BooleanField(default=False)
    apply_middleware = models.BooleanField(
        default=False,
        help_text=(
            "Pipe EDF/BDF file content through the configured middleware pipeline "
            "when serving this access right. Has no effect on non-EDF files or when "
            "the caller is the recording author or a superuser."
        ),
    )

    created_at = models.DateTimeField(auto_now_add=True)
    modified_at = models.DateTimeField(auto_now=True)

    objects = AccessRightQuerySet.as_manager()

    def __str__(self) -> str:
        target = (
            str(self.access_target)
            if self.access_target_id is not None
            else str(self.access_target_group)
            if self.access_target_group_id is not None
            else f"peer:{self.federated_peer_id}"
            if self.federated_peer_id is not None
            else f"token:{(self.public_share_token or '')[:8]}…"
        )
        perms = "/".join(
            p
            for p, flag in [
                ("r", self.can_read),
                ("w", self.can_write),
                ("s", self.can_share),
            ]
            if flag
        )
        return f"AccessRight({self.content_type}/{self.object_id} → {target} [{perms}])"

    class Meta:
        indexes = [
            models.Index(fields=["content_type", "object_id"]),
            models.Index(fields=["access_target", "content_type", "object_id"]),
            models.Index(fields=["access_target_group", "content_type", "object_id"]),
            models.Index(fields=["public_share_token", "content_type", "object_id"]),
            models.Index(fields=["federated_peer", "content_type", "object_id"]),
            models.Index(fields=["expires_at"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(can_read=True) | Q(can_write=True) | Q(can_share=True),
                name="access_right_requires_some_permission",
            ),
            models.CheckConstraint(
                condition=(
                    # Local user target
                    Q(
                        access_target__isnull=False,
                        access_target_group__isnull=True,
                        public_share_token__isnull=True,
                        federated_peer__isnull=True,
                    )
                    # Local group target
                    | Q(
                        access_target__isnull=True,
                        access_target_group__isnull=False,
                        public_share_token__isnull=True,
                        federated_peer__isnull=True,
                    )
                    # Public share token target
                    | Q(
                        access_target__isnull=True,
                        access_target_group__isnull=True,
                        public_share_token__isnull=False,
                        federated_peer__isnull=True,
                    )
                    # Federated peer target (optionally scoped to a specific remote user)
                    | Q(
                        access_target__isnull=True,
                        access_target_group__isnull=True,
                        public_share_token__isnull=True,
                        federated_peer__isnull=False,
                    )
                ),
                name="access_right_exactly_one_target",
            ),
            # One row per (object, target) grant — the invariant the README
            # has always stated, enforced since migration 0002. Partial
            # indexes because each target column is null on rows using the
            # other target types. Share-token rows need no per-object
            # constraint: ``public_share_token`` is globally unique, so two
            # token rows on one object are two distinct grants by design.
            models.UniqueConstraint(
                fields=["content_type", "object_id", "access_target"],
                condition=Q(access_target__isnull=False),
                name="access_right_unique_user_target",
            ),
            models.UniqueConstraint(
                fields=["content_type", "object_id", "access_target_group"],
                condition=Q(access_target_group__isnull=False),
                name="access_right_unique_group_target",
            ),
            # remote_user_id is part of the key: a wildcard row ("") and an
            # exact-user row may coexist; the resolver prefers the exact one.
            models.UniqueConstraint(
                fields=["content_type", "object_id", "federated_peer", "remote_user_id"],
                condition=Q(federated_peer__isnull=False),
                name="access_right_unique_federated_target",
            ),
        ]

    @classmethod
    def can_read_with_token(cls, token: str, obj) -> bool:
        content_type = ContentType.objects.get_for_model(obj, for_concrete_model=False)
        return cls.objects.has_permission_for_token(
            token=token,
            content_type=content_type,
            object_id=getattr(obj, "pk", None),
            permission_field="can_read",
        )

    @classmethod
    def can_federated_peer_read(cls, peer, remote_user_id: str, obj) -> bool:
        """Return True when a federated peer + remote user may read ``obj``."""
        content_type = ContentType.objects.get_for_model(obj, for_concrete_model=False)
        return cls.objects.has_federated_permission(
            peer=peer,
            remote_user_id=remote_user_id,
            content_type=content_type,
            object_id=getattr(obj, "pk", None),
            permission_field="can_read",
        )


class ViewerConfigOverride(models.Model):
    """Editable viewer-config overrides layered on the active project's seed.

    The effective viewer configuration served to the frontend is the active
    project's ``viewer-config.json`` seed merged with this row's ``overrides``
    (the overrides win). Keyed by project name — one row per project — so a
    project switch never surfaces another project's overrides. ``overrides`` is
    a flat map of dotted-path settings field to value
    (``{"eeg.defaultMontage": "lon"}``), the same shape as the seed.
    """

    project = models.CharField(max_length=64, unique=True)
    overrides = models.JSONField(default=dict, blank=True)
    modified_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"ViewerConfigOverride({self.project})"
