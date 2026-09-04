"""Database models for the *example* project.

Models here extend the core schema with project-specific data.  A few
conventions to follow:

FK / OneToOne to core models
    Use string references (``"recordings.Recording"``) rather than direct
    imports to avoid circular dependencies.  Django resolves these lazily.

related_name
    Always supply a ``related_name`` so the reverse accessor on the core model
    is explicit (e.g. ``recording.example_note`` instead of the auto-generated
    ``recordingnote_set``).  Use the project name as a prefix to avoid
    clashes if multiple projects add reverse relations to the same model.

De-identification
    Never expose integer PKs in API responses.  See the de-identification
    section of the README for the full policy.

Table naming
    Django generates table names from the app label and model name:
    ``example_recordingnote``.  The ``deactivate_project`` management command
    uses this prefix to discover and archive tables, so do not override
    ``db_table`` in Meta unless you have a good reason.
"""

from django.conf import settings
from django.db import models


class RecordingNote(models.Model):
    """Project-specific clinical note attached to a single recording.

    Demonstrates a OneToOneField linking a project model to a core model.
    The ``example_note`` related name means you can traverse the relation as
    ``recording.example_note`` from any place that holds a Recording instance.
    """

    # OneToOne ensures one note per recording.  Use ForeignKey instead if
    # you need multiple project rows per recording (e.g. audit log entries).
    recording = models.OneToOneField(
        "recordings.Recording",
        on_delete=models.CASCADE,
        related_name="example_note",
    )

    # A site-specific identifier used by the institution's own systems.
    # Stored as text; the format is institution-defined.
    site_id = models.CharField(max_length=64, blank=True, default="")

    # Free-text clinical notes.  Length enforced at the API level
    # (see EXAMPLE_NOTE_MAX_LENGTH in settings.py).
    notes = models.TextField(blank=True, default="")

    # Optional reviewer — a FK to the user model so the recording can be
    # marked as reviewed by a specific clinician.
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="example_reviewed_recordings",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    modified_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["site_id"]),
        ]

    def __str__(self):
        return f"RecordingNote(recording={self.recording_id}, site_id={self.site_id!r})"
