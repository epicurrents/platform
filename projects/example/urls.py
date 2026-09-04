"""API endpoints for the *example* project.

This module is optional. When present, it is mounted at ``/project/api/v1/``
by the URL loader in ``epicurrents/urls.py``. Full path examples:

    GET  /project/api/v1/notes/{recording_hash}
    PUT  /project/api/v1/notes/{recording_hash}

Structure
---------
Follow the same patterns used in core app APIs:

- One ``NinjaAPI`` instance per project (``urls_namespace`` must be unique).
- Input / output separated into ``*In`` / ``*Out`` schemas.
- Permission checks via ``epicurrents.permissions`` helpers.
- Write operations wrapped in ``transaction.atomic()`` when they touch more
  than one table.
- Audit trail: call ``activity.audit.log_activity(verb=..., target=...,
  metadata=...)`` after the endpoint resolves its work. Verb follows
  ``<project>.<resource>.<action>``; ``target`` is the model instance the
  endpoint operated on; ``metadata`` carries derived insights, filter
  parameters, or bulk identifiers that the linked ``ObjectChangeLog`` does
  NOT already capture. Delete endpoints call ``log_activity`` BEFORE the
  ``.delete()`` inside the atomic block so ``target.pk`` is preserved.
  See [.review/agents/audit-trail-completeness.md](../../.review/agents/audit-trail-completeness.md)
  for the full invariant.

Authentication
--------------
All endpoints below use Django Ninja's default session-cookie auth, which is
configured globally. To add token or API-key auth for project endpoints only,
pass an ``auth=`` argument to ``NinjaAPI`` or individual ``@api.*`` decorators.
"""

from datetime import datetime

from django.conf import settings
from django.db import transaction
from django.urls import path
from ninja import NinjaAPI, Schema
from ninja.errors import HttpError

from activity.audit import log_activity
from epicurrents.auth import enforce_session_csrf
from epicurrents.permissions import can_read_object, ensure_can_write_object
from projects.example.models import RecordingNote
from recordings.models import Recording

api = NinjaAPI(
    title="Example Project API",
    version="1",
    # Must be unique across all mounted NinjaAPI instances.
    urls_namespace="example-api-v1",
)


# ---------------------------------------------------------------------------
# Auth helpers — staff vs superuser tier guards.
#
# Use these at the top of any endpoint that requires elevated access:
#
#   _require_auth        any authenticated user (no specific tier).
#   _require_staff       staff-level access — admin dashboards, batch
#                        operations, anything that requires visibility
#                        across all users' data.
#   _require_superuser   destructive or irreversible actions (data
#                        deletion, irreversible bulk operations).
#
# Treat superuser as a strict subset of staff: anything a superuser can do,
# a staff user should also be able to do or see in read-only form. Do NOT
# invent project-specific role models (e.g. UserProfile.role == 'admin')
# for tiers that map cleanly onto staff vs. superuser — use the Django
# flags directly. Project-specific roles are only appropriate when the
# distinction cannot be expressed as staff vs. superuser (e.g. a per-session
# student identity that has nothing to do with admin access).
#
# Example use::
#
#     @api.get("/notes/", response=list[NoteOut])
#     def list_all_notes(request):
#         _require_staff(request)        # staff-only listing
#         return list(RecordingNote.objects.all())
# ---------------------------------------------------------------------------


def _require_auth(request):
    """Return the authenticated user or raise 401.

    ``enforce_session_csrf`` is the platform's single CSRF chokepoint: the
    Ninja mounts are csrf-exempt, so a session-authenticated write that does
    not pass through here is unprotected. Resolve the caller through this
    helper (never by reading ``request.user`` directly in an endpoint) and
    the chokepoint covers every unsafe method automatically.
    """
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated:
        raise HttpError(401, "Authentication required.")
    enforce_session_csrf(request)
    return user


def _require_staff(request):
    """Return authenticated staff (or superuser) user or raise 403."""
    user = _require_auth(request)
    if not (user.is_staff or user.is_superuser):
        raise HttpError(403, "Staff access required.")
    return user


def _require_superuser(request):
    """Return authenticated superuser or raise 403."""
    user = _require_auth(request)
    if not user.is_superuser:
        raise HttpError(403, "Superuser access required.")
    return user


# ---------------------------------------------------------------------------
# Recording resolution
# ---------------------------------------------------------------------------


def _resolve_recording_or_404(recording_hash: str) -> Recording:
    """Resolve the public recording hash, or raise 404.

    The public ``hash`` every recording response serves is the 32-character
    prefix of ``Recording.stored_name`` — not ``content_hash``, which is a
    content fingerprint the platform rewrites during anonymisation. Match the
    prefix the same way the core recordings API does.
    """
    normalized = (recording_hash or "").strip().upper()
    if len(normalized) != 32 or not normalized.isalnum():
        raise HttpError(404, "Recording not found.")
    recording = Recording.objects.filter(
        stored_name__startswith=f"{normalized}.",
        deleted_at__isnull=True,
    ).first()
    if recording is None:
        raise HttpError(404, "Recording not found.")
    return recording


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class NoteOut(Schema):
    """Response schema for a RecordingNote."""

    site_id: str
    notes: str
    reviewed_by_id: int | None
    reviewed_at: datetime | None
    created_at: datetime
    modified_at: datetime


class NoteIn(Schema):
    """Payload for creating or updating a RecordingNote.

    All fields are optional so the same schema can be used for both full
    creation (PUT) and partial updates. Apply your own validation in the
    endpoint if any field is required on creation.
    """

    site_id: str | None = None
    notes: str | None = None
    # Pass the user ID of the reviewer, or null to clear.
    reviewed_by_id: int | None = None
    reviewed_at: datetime | None = None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@api.get(
    "/notes/{recording_hash}",
    response=NoteOut,
    summary="Get the project note for a recording.",
)
def get_note(request, recording_hash: str):
    """Return the project-specific note for *recording_hash*.

    Requires read access to the recording (same permission check as the core
    recordings API). Returns 404 if the recording does not exist, the caller
    does not have access, or no note has been created yet.
    """
    user = _require_auth(request)
    recording = _resolve_recording_or_404(recording_hash)

    if not can_read_object(user, recording):
        raise HttpError(404, "Recording not found.")

    # FAILED-hidden rule (AGENTS.md): a failed upload is visible only to its
    # author and superusers, and every grantee-facing surface that resolves a
    # Recording — project endpoints included — must 404 it indistinguishably
    # from absence. A failed upload's existence, confirmed to a grantee,
    # would leak alongside its PHI-bearing original filename elsewhere.
    if recording.status == Recording.Status.FAILED and not (user == recording.author or user.is_superuser):
        raise HttpError(404, "Recording not found.")

    try:
        note = recording.example_note
    except RecordingNote.DoesNotExist:
        raise HttpError(404, "No note exists for this recording.")

    log_activity(verb="example.note.read", target=note)
    return note


@api.put(
    "/notes/{recording_hash}",
    response=NoteOut,
    summary="Create or update the project note for a recording.",
)
def upsert_note(request, recording_hash: str, payload: NoteIn):
    """Create or replace the project note for *recording_hash*.

    Requires write access to the recording. Uses ``update_or_create`` so the
    same endpoint handles both the initial creation and subsequent updates.

    ``notes`` length is validated against ``EXAMPLE_NOTE_MAX_LENGTH`` (set in
    ``projects/example/settings.py``).
    """
    user = _require_auth(request)
    recording = _resolve_recording_or_404(recording_hash)

    # ensure_can_write_object raises HttpError(403) if the caller lacks access.
    ensure_can_write_object(user, recording)

    # Validate note length using the project-specific setting.
    max_length = getattr(settings, "EXAMPLE_NOTE_MAX_LENGTH", 2000)
    notes_value = payload.notes or ""
    if len(notes_value) > max_length:
        raise HttpError(
            400,
            f"notes exceeds the maximum allowed length of {max_length} characters.",
        )

    with transaction.atomic():
        note, created = RecordingNote.objects.update_or_create(
            recording=recording,
            defaults={
                "site_id": payload.site_id or "",
                "notes": notes_value,
                "reviewed_by_id": payload.reviewed_by_id,
                "reviewed_at": payload.reviewed_at,
            },
        )
        log_activity(
            verb="example.note.update",
            target=note,
            metadata={"created": created},
        )

    return note


# The platform mounts the active project with ``include("projects.<name>.urls")``
# at ``/project/api/v1/``, so this module must expose ``urlpatterns``. Wrapping
# the NinjaAPI router this way is the convention every project follows.
urlpatterns = [path("", api.urls)]
