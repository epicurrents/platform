"""Database models for the *dicom* plugin.

The data model mirrors the DICOM information hierarchy:

  DicomStudy   — one examination (patient + date + accession number).
  DicomSeries  — one acquisition run within a study (one modality / protocol).
  DicomInstance — one SOP instance, i.e. one DICOM file.

A ``DicomInstance`` row is created for every uploaded DICOM file that passes
header parsing. The study/series hierarchy is discovered from the file's
embedded UIDs at upload time, so uploading files in any order — including files
from different studies in a single batch — always produces the correct tree.

Studies are **per-author**: the same ``StudyInstanceUID`` uploaded by two
different users produces two independent ``DicomStudy`` rows (and two file
copies), exactly as two users uploading the same EDF produce two independent
``Recording`` rows. This is what keeps one user's upload from attaching to —
or probing for the existence of — another user's study.

Files are stored under ``DICOM_UPLOAD_PATH`` with UUID-derived names (never
derived from user input). ``DicomStudy.content_hash`` is a SHA-256 over the
author PK and the ``StudyInstanceUID`` salted with a server secret and is used
as the public URL identifier, in the same way that ``Recording.content_hash``
is used by the recordings app.

Lifecycle
---------
``DicomStudy`` follows the media-app soft-delete pattern: ``deleted_at`` set →
hidden from every read surface → hard-deleted (with files) by the scheduled
``dicom.purge_deleted_dicom_studies`` task after ``DICOM_TRASH_RETENTION_DAYS``.
Series and instances have no independent lifecycle; they die with the study.
The ``pre_delete`` receiver in ``plugins/dicom/signals.py`` unlinks each
instance's stored file whenever a hard delete cascades, so the purge task,
study deletion, and ``erase_user``'s account cascade all clean the filesystem.

Access control
--------------
``DicomStudy`` declares a ``GenericRelation`` to ``AccessRight`` so studies can
be shared using the same permission model as recordings, plus the library
reference-row relations (collections / datasets / tags) per the GenericFK
target cascade pattern in AGENTS.md. A study may also be *attached* to a
parent object (typically a ``Recording``) via a GenericForeignKey, in which
case it inherits the parent's read access through the
``can_read_via_attachment`` extension in ``plugins/dicom/permissions.py`` —
the same mode of operation as attached media. Series and instances are not
independently shareable; access always flows from the study.
"""

import hashlib

from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey, GenericRelation
from django.contrib.contenttypes.models import ContentType
from django.db import models

# ---------------------------------------------------------------------------
# Study
# ---------------------------------------------------------------------------


class DicomStudy(models.Model):
    """One DICOM study — a single examination, scoped to its uploading author."""

    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="dicom_studies",
    )

    # DICOM study-level tags (0020,000D) etc. Unique per author, not globally —
    # see the module docstring for why.
    study_instance_uid = models.CharField(max_length=64, db_index=True)
    study_date = models.CharField(max_length=8, blank=True, default="")
    study_time = models.CharField(max_length=16, blank=True, default="")
    study_description = models.CharField(max_length=256, blank=True, default="")

    # Patient demographics — stored as parsed from the file. Masked out of
    # audit payloads (see apps.py); ingest de-identification is a tracked
    # roadmap item (plugins/dicom/README.md → Roadmap).
    patient_name = models.CharField(max_length=256, blank=True, default="")
    patient_id = models.CharField(max_length=64, blank=True, default="")
    patient_birth_date = models.CharField(max_length=8, blank=True, default="")
    patient_sex = models.CharField(max_length=16, blank=True, default="")
    patient_age = models.CharField(max_length=4, blank=True, default="")
    accession_number = models.CharField(max_length=64, blank=True, default="")

    # Derived / cached fields refreshed by ingest.refresh_study_aggregates.
    num_instances = models.IntegerField(default=0)
    modalities = models.CharField(max_length=256, blank=True, default="")  # comma-joined

    # Opaque public identifier — see make_content_hash. Use this in URLs
    # instead of the integer PK.
    content_hash = models.CharField(max_length=64, unique=True, db_index=True)

    # Optional generic attachment to a parent object (typically a Recording).
    # Mirrors media.MediaFile: the target deliberately declares no reverse
    # GenericRelation, so a purged parent orphans the study rather than
    # cascade-deleting it. Stale pairs surface as attachment=None at read time.
    attachment_content_type = models.ForeignKey(
        ContentType,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="dicom_study_attachments_by_type",
    )
    attachment_object_id = models.CharField(max_length=255, blank=True, default="")
    attachment = GenericForeignKey("attachment_content_type", "attachment_object_id")

    created_at = models.DateTimeField(auto_now_add=True)
    modified_at = models.DateTimeField(auto_now=True)
    # Soft delete: null = active, non-null = trashed. Hard-deleted with files
    # by the scheduled purge task after the retention window.
    deleted_at = models.DateTimeField(null=True, blank=True, default=None, db_index=True)

    # Reverse GenericRelations so hard-delete cascades cleanly through every
    # reference row targeting this study (AGENTS.md → GenericFK target cascade
    # pattern). Soft-delete is unaffected.
    access_rights = GenericRelation("epicurrents.AccessRight")
    collection_memberships = GenericRelation("library.CollectionItem")
    dataset_memberships = GenericRelation("library.DatasetItem")
    tagged_items = GenericRelation("library.TaggedItem")

    class Meta:
        verbose_name_plural = "DICOM studies"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["author", "study_instance_uid"],
                name="dicomstudy_unique_uid_per_author",
            ),
        ]
        indexes = [
            models.Index(
                fields=["attachment_content_type", "attachment_object_id"],
                name="dicom_attachment_lookup_idx",
            ),
        ]

    def __str__(self):
        return f"DicomStudy({self.study_instance_uid!r}, author={self.author_id})"

    @classmethod
    def make_content_hash(cls, author_id: int, study_instance_uid: str) -> str:
        """Return a stable public hash for one author's copy of a study.

        Salted with ``SECRET_KEY`` so UIDs cannot be enumerated from outside
        the instance (even for well-known public DICOM test sets), and keyed
        on the author PK so per-author copies of the same study get distinct
        public identifiers.
        """
        secret = getattr(settings, "SECRET_KEY", "")
        raw = f"{secret}:{author_id}:{study_instance_uid}"
        return hashlib.sha256(raw.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Series
# ---------------------------------------------------------------------------


class DicomSeries(models.Model):
    """One DICOM series within a study."""

    study = models.ForeignKey(
        DicomStudy,
        on_delete=models.CASCADE,
        related_name="series",
    )

    # DICOM series-level tags.
    series_instance_uid = models.CharField(max_length=64, db_index=True)
    series_description = models.CharField(max_length=256, blank=True, default="")
    series_number = models.CharField(max_length=12, blank=True, default="")
    series_date = models.CharField(max_length=8, blank=True, default="")
    modality = models.CharField(max_length=16, blank=True, default="")
    slice_thickness = models.CharField(max_length=16, blank=True, default="")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["study", "series_instance_uid"],
                name="dicomseries_unique_uid_per_study",
            ),
        ]
        ordering = ["series_number", "series_instance_uid"]

    def __str__(self):
        return f"DicomSeries({self.series_instance_uid!r}, modality={self.modality!r})"


# ---------------------------------------------------------------------------
# Instance
# ---------------------------------------------------------------------------


class DicomInstance(models.Model):
    """One DICOM SOP instance — one physical file.

    Pixel-level metadata (dimensions, bit depth, photometric interpretation,
    etc.) is stored here so the DICOMweb JSON required by OHIF can be built
    without re-reading the file.

    ``status`` semantics: ``READY`` means the row *and* its file are in final
    storage under ``DICOM_UPLOAD_PATH``. ``PENDING`` exists only inside the
    upload request between the DB commit and the staging→final move; a row
    stranded in ``PENDING`` (crash between commit and move) is reaped by the
    purge task's orphan branch. ``FAILED`` marks a post-commit move failure;
    re-uploading the same instance replaces it in place.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        READY = "ready", "Ready"
        FAILED = "failed", "Failed"

    series = models.ForeignKey(
        DicomSeries,
        on_delete=models.CASCADE,
        related_name="instances",
    )

    # Identity tags. Unique per series, not globally — per-author study copies
    # legitimately repeat the same SOPInstanceUID.
    sop_instance_uid = models.CharField(max_length=64, db_index=True)
    sop_class_uid = models.CharField(max_length=64, blank=True, default="")
    instance_number = models.CharField(max_length=12, blank=True, default="")

    # Image geometry / pixel encoding — all nullable because non-image
    # objects (SR, RT, PR) may not carry pixel data.
    columns = models.IntegerField(null=True, blank=True)
    rows = models.IntegerField(null=True, blank=True)
    photometric_interpretation = models.CharField(max_length=16, blank=True, default="")
    bits_allocated = models.IntegerField(null=True, blank=True)
    bits_stored = models.IntegerField(null=True, blank=True)
    pixel_representation = models.IntegerField(null=True, blank=True)
    samples_per_pixel = models.IntegerField(null=True, blank=True)
    high_bit = models.IntegerField(null=True, blank=True)
    number_of_frames = models.IntegerField(null=True, blank=True)

    # Multi-value tags stored as JSON arrays (strings, as DICOM DS/CS VRs are
    # character strings even when they represent numbers).
    pixel_spacing = models.JSONField(null=True, blank=True)
    image_orientation_patient = models.JSONField(null=True, blank=True)
    image_position_patient = models.JSONField(null=True, blank=True)
    image_type = models.JSONField(null=True, blank=True)

    # Frame-of-reference and windowing.
    frame_of_reference_uid = models.CharField(max_length=64, blank=True, default="")
    window_center = models.CharField(max_length=32, blank=True, default="")
    window_width = models.CharField(max_length=32, blank=True, default="")

    # File storage — stored_name is the filename under DICOM_UPLOAD_PATH.
    # Never derived from user input; generated at upload time.
    stored_name = models.CharField(max_length=255, unique=True)
    file_size = models.BigIntegerField()
    file_hash = models.CharField(max_length=64, blank=True, default="")

    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )
    error_message = models.TextField(blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    modified_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["instance_number", "sop_instance_uid"]
        constraints = [
            models.UniqueConstraint(
                fields=["series", "sop_instance_uid"],
                name="dicominstance_unique_sop_per_series",
            ),
        ]

    def __str__(self):
        return f"DicomInstance({self.sop_instance_uid!r}, status={self.status!r})"
