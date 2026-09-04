"""Shared helpers for the federation management commands.

Underscore-prefixed so Django's command discovery skips it. Keeps user/peer
resolution and error translation out of every command's ``handle``.
"""

from django.contrib.auth import get_user_model
from django.core.management.base import CommandError

from federation import services
from federation.services import FederationServiceError


def resolve_user(username: str | None):
    """Return the ``User`` for ``username``, or ``None`` when unset. Raises if not found."""
    if not username:
        return None
    user = get_user_model().objects.filter(username=username).first()
    if user is None:
        raise CommandError(f"User not found: {username}")
    return user


def resolve_peer(ref: str):
    """Resolve a peer by id or URL, translating the service error to ``CommandError``."""
    try:
        return services.get_peer_by_ref(ref)
    except FederationServiceError as exc:
        raise CommandError(exc.message)
