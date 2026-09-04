"""Read-visibility gate that hides FAILED and trashed recordings from grant resolution.

This module is the centralised enforcement of the FAILED-hidden rule (AGENTS.md →
*FAILED recording hiding*) and its soft-delete counterpart: registered from
``RecordingsConfig.ready()`` via ``epicurrents.permissions.register_read_visibility_gate``,
the gate denies read resolution before any ``AccessRight`` row or extension grant is
consulted, so a surface that never heard of the rule — the generic annotations API, an
extension grant, a future endpoint — is safe by default. The endpoint-level
``_failed_hidden_for_caller`` checks in ``recordings/api/v1/ninja.py`` remain in place:
they decide the response shape (404 rather than 403) on the recording surfaces and stand
as defence-in-depth beneath this gate.
"""

from typing import Any


def recording_hidden_from_reader(user: Any, obj: Any, share_token: str | None = None) -> bool:
    """Return True when *obj* must not resolve as readable for this caller.

    Trashed recordings (non-null ``deleted_at``) are hidden from every caller the
    resolver's gates reach — trash management works through author-filtered endpoints,
    not read grants. FAILED recordings are hidden from everyone but the author and
    superusers. ``user=None`` (anonymous, share-token-only, or federated callers) is
    the fully unprivileged shape and sees neither state.
    """
    from recordings.models import Recording

    if obj.deleted_at is not None:
        return True
    if obj.status != Recording.Status.FAILED:
        return False
    if user is None or not getattr(user, "is_authenticated", False):
        return True
    if getattr(user, "is_superuser", False):
        return False
    return obj.author_id != user.pk
