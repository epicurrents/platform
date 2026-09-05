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


def resolve_recording(ref: str):
    """Resolve a recording by the hash that identifies it everywhere else, or by content hash.

    The identifier a person has in hand is the 32-character ``stored_name`` prefix:
    it is what URLs, viewer links and the REST API use. ``content_hash`` is a
    fingerprint of the bytes, useful when two copies of the same recording need to
    be told apart, and it is accepted as a second form rather than as the only one.

    Raises:
        CommandError: no recording matches either form.
    """
    from recordings.models import Recording

    candidate = (ref or "").strip()
    normalized = candidate.upper()
    if len(normalized) == 32 and normalized.isalnum():
        recording = (
            Recording.objects.filter(stored_name__startswith=f"{normalized}.", deleted_at__isnull=True)
            .order_by("-created_at")
            .first()
        )
        if recording is not None:
            return recording

    recording = Recording.objects.filter(content_hash=candidate, deleted_at__isnull=True).first()
    if recording is not None:
        return recording

    raise CommandError(
        f"No recording matches '{candidate}'. Give the 32-character hash from the recording's URL, "
        "or its full content_hash."
    )
