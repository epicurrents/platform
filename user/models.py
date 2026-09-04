"""User model and external-identity link rows.

``User`` is a thin ``AbstractUser`` subclass that acts as ``AUTH_USER_MODEL``;
no extra fields are defined on it so that future schema additions (avatar,
preferences) can land as a normal migration rather than a model swap.

``TwoFactorCredential`` holds a user's TOTP secret and recovery codes for the
optional second login factor.

``UserPreference`` stores a client's per-user settings as an opaque JSON
blob, keyed by a scope string so unrelated clients cannot collide.

``ExternalIdentity`` links a local ``User`` to an account at an external
OpenID Connect provider (Microsoft Entra ID today). It is keyed on the
provider-issued ``subject`` claim — an opaque, pairwise pseudonymous
identifier that carries no embedded personal information — so the link
survives email / display-name changes at the provider. See
[user/oidc.py](oidc.py) and user/README.md → *External login (OIDC)*.
"""

from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    """Custom user model. Extends AbstractUser to allow future additions."""

    class Meta(AbstractUser.Meta):
        swappable = "AUTH_USER_MODEL"


class ExternalIdentity(models.Model):
    """A link between a local ``User`` and an external OIDC account.

    Uniqueness is on the ``(provider, issuer, subject)`` triple: ``subject``
    (the OIDC ``sub`` claim) is only guaranteed unique within an issuer, and a
    deployment may in principle accept more than one provider. ``email`` is
    cached for display and for the verified-email linking policy; it is PII and
    is not required for the identity to function.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="external_identities",
    )
    provider = models.CharField(max_length=64)
    issuer = models.CharField(max_length=255)
    subject = models.CharField(max_length=255)
    email = models.EmailField(blank=True)
    email_verified = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    last_login_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name_plural = "external identities"
        constraints = [
            models.UniqueConstraint(
                fields=["provider", "issuer", "subject"],
                name="uniq_external_identity_provider_issuer_subject",
            ),
        ]

    def __str__(self):
        return f"{self.provider}:{self.subject} -> {self.user_id}"


class TwoFactorCredential(models.Model):
    """A user's TOTP second factor: the shared secret and its recovery codes.

    Its own model rather than fields on ``User``, for two reasons. Every write
    to ``User`` serialises all of that model's concrete fields into
    ``ObjectChangeLog``, so a secret living there would ride along on every
    unrelated profile edit; and keeping it separate leaves it out of ``UserOut``
    and ``AccountOut`` by construction instead of by remembering to exclude it.
    Both fields are additionally masked out of audit payloads and registered for
    subject erasure in ``user.apps.UserConfig.ready``.

    ``confirmed_at`` is what makes the credential live. Enrolment writes a
    secret first and confirms it only once the user proves the authenticator
    holds the same one, so an abandoned enrolment leaves a row that must never
    gate a login — every read on the login path filters on this being set.

    ``last_counter`` is the most recently spent TOTP time step, and the reason a
    captured code cannot be replayed inside its validity window. See
    user/two_factor.py for how it is claimed.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="two_factor",
    )
    secret = models.CharField(max_length=64)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    last_counter = models.BigIntegerField(default=0)
    backup_codes = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "two-factor credential"

    def __str__(self):
        state = "confirmed" if self.confirmed_at else "pending"
        return f"two-factor ({state}) -> {self.user_id}"

    @property
    def is_active(self) -> bool:
        """Whether this credential gates login."""
        return self.confirmed_at is not None


class UserPreference(models.Model):
    """Client-owned settings for a user, stored as an opaque JSON blob.

    The platform does not interpret ``values``; it is a flat map of client
    setting names to primitive values whose meaning belongs entirely to the
    client that wrote it (today: the viewer's user-definable settings). Keeping
    it opaque means a new client setting needs no migration here. ``scope``
    separates one client's map from another's — a user has at most one row per
    scope, enforced by the uniqueness constraint.

    The blob is registered for GDPR Art. 17 erasure in
    ``user.apps.UserConfig.ready``. It should never carry personal data — the
    write endpoint rejects anything but a flat map of primitives under
    setting-shaped keys — but a client is free to name a setting badly, and the
    audit trail keeps every version of the blob forever, so registering it is
    the cheaper side of the trade.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="preferences",
    )
    scope = models.CharField(max_length=64)
    values = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    modified_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "scope"],
                name="uniq_user_preference_user_scope",
            ),
        ]

    def __str__(self):
        return f"{self.scope} preferences -> {self.user_id}"
