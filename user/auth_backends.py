"""Authentication backend for OpenID Connect logins.

The OIDC callback validates the provider token and resolves it to an
:class:`~user.models.ExternalIdentity` (see [user/oidc.py](oidc.py)); this
backend turns that already-trusted identity into the authenticated user that
``django.contrib.auth.login`` records on the session. It performs no
credential checking of its own — the trust decision lives entirely in the
token-validation path — and is reachable only by passing an ``oidc_identity``
keyword, so password logins continue to flow through ``ModelBackend``.
"""

from django.contrib.auth import get_user_model
from django.contrib.auth.backends import BaseBackend


class OIDCBackend(BaseBackend):
    """Resolve a validated external identity to its local user."""

    def authenticate(self, request, oidc_identity=None, **kwargs):
        """Return the user behind a validated ``ExternalIdentity``, or ``None``.

        Returns ``None`` when called without ``oidc_identity`` so that the
        normal username/password ``authenticate()`` path is unaffected.
        """
        if oidc_identity is None:
            return None
        user = oidc_identity.user
        return user if self.user_can_authenticate(user) else None

    def get_user(self, user_id):
        """Load the session user; mirrors ``ModelBackend.get_user``."""
        User = get_user_model()
        try:
            user = User.objects.get(pk=user_id)
        except User.DoesNotExist:
            return None
        return user if self.user_can_authenticate(user) else None

    def user_can_authenticate(self, user) -> bool:
        """Reject inactive users, matching Django's default backend semantics."""
        return getattr(user, "is_active", False)
