"""Recording models — file metadata, format metadata, signal metadata, and import jobs.

``Recording``
    Core model: stores file identity (hash, path, size), processing status,
    modality, and soft-delete timestamp.

``RecordingMeta``
    Format-level metadata extracted from the EDF/BDF header (duration, data
    records, signal count).  Linked via generic FK so the meta system can be
    extended to other content types without schema changes.

``SignalInfo``
    Per-channel (signal) metadata: label, physical/digital ranges, sampling
    rate, filter settings, annotation-channel flag.

``ImportJob`` / ``ImportJobFile``
    Progress tracking for the ``import_recordings`` management command.
    One job per import run; one file row per discovered file.
"""

from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey, GenericRelation
from django.contrib.contenttypes.models import ContentType
from django.db import models
from django.utils import timezone


def stored_original_name(uploaded_name: str, extension: str) -> str:
    """Return the value to write into :attr:`Recording.original_name`.

    Normally the name the file arrived with. When
    ``RECORDINGS_DISCARD_ORIGINAL_NAME`` is on, an upload timestamp instead:
    clinical exports are routinely named after the patient, which makes the
    filename a direct identifier arriving through a field nobody classifies as
    one.

    Every route that creates a ``Recording`` must obtain the value here rather
    than assigning the filename directly. That is not a style preference. The
    setting first shipped enforced only in the upload endpoint, so a deployment
    that had turned it on — and documented it as load-bearing for its
    data-protection position — still wrote real patient filenames to the
    database for every recording brought in by ``import_recordings``. A gate
    that covers one route of three is a default wearing a prohibition's name.
    """
    if getattr(settings, "RECORDINGS_DISCARD_ORIGINAL_NAME", False):
        return f"upload-{timezone.now():%Y%m%dT%H%M%SZ}{extension}"
    return uploaded_name


class Recording(models.Model):
    """Uploaded recording file metadata and integrity information."""

    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="recordings",
    )
    # Filename as uploaded.  Immutable after creation.  Visible only to the
    # author and superusers; never returned to grantees, share-token holders,
    # or federated peers (see ``_can_see_original_name`` in the v1 API).
    # The grantee-visible name is ``display_name``.
    original_name = models.CharField(max_length=255)
    # Grantee-visible label for the recording.  Nullable: when unset, list /
    # detail responses fall back to ``stored_name[:8].upper()`` (the public
    # hash prefix).  The author may set or clear this via PATCH; the
    # collection bulk-rename endpoint writes this field too.
    display_name = models.CharField(max_length=255, null=True, blank=True, default=None)
    stored_name = models.CharField(max_length=255, unique=True)
    file_extension = models.CharField(max_length=32, blank=True, default="")
    file_size = models.BigIntegerField()
    file_path = models.CharField(max_length=1024)
    file_hash = models.CharField(max_length=64, blank=True, default="")
    content_hash = models.CharField(max_length=64, blank=True, default="")

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        PROCESSING = "processing", "Processing"
        READY = "ready", "Ready"
        FAILED = "failed", "Failed"  # file preserved but format processing failed

    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )
    # Populated when status transitions to FAILED so the author has enough
    # information to act (re-upload, fix the source, etc.).  May contain
    # filesystem paths and library stack-trace fragments — surfaced only to
    # the author and superusers (see ``_can_see_original_name`` for the same
    # visibility pattern).  Empty for non-failed recordings.
    processing_error = models.TextField(blank=True, default="")
    # Dominant signal modality inferred during processing (e.g. 'eeg', 'emg').
    # Empty until processing completes successfully.
    modality = models.CharField(max_length=32, blank=True, default="")
    # Operator-set mains (power-line) frequency in Hz for this recording's
    # environment (50 EU / 60 US). NULL = inherit the deployment ``EEG_MAINS_HZ``
    # default. Lives here — not on ``RecordingMeta`` — because meta/SignalInfo are
    # rebuilt from the header on every reprocess and would wipe the override; the
    # recording site is a stable property. Not personal data. Feeds the detector
    # notch and BIDS ``PowerLineFrequency`` via
    # ``compute.mains.resolve_recording_notch_hz``.
    power_line_frequency = models.FloatField(null=True, blank=True, default=None)

    # Reverse GenericRelations so hard-delete (purge) cascades cleanly through
    # every reference row that targets this recording via a GenericForeignKey.
    # Without these, the orphans would persist silently because the ORM cannot
    # enforce referential integrity across generic FKs. Soft-delete is
    # unaffected — the cascade only runs when the Recording row is actually
    # removed by Django.
    annotations = GenericRelation(
        "annotations.Annotation",
        object_id_field="target_object_id",
        content_type_field="target_content_type",
    )
    events = GenericRelation(
        "annotations.Event",
        object_id_field="target_object_id",
        content_type_field="target_content_type",
    )
    interruptions = GenericRelation(
        "annotations.Interruption",
        object_id_field="target_object_id",
        content_type_field="target_content_type",
    )
    labels = GenericRelation(
        "annotations.Label",
        object_id_field="target_object_id",
        content_type_field="target_content_type",
    )
    access_rights = GenericRelation("epicurrents.AccessRight")
    collection_memberships = GenericRelation("library.CollectionItem")
    dataset_memberships = GenericRelation("library.DatasetItem")
    tagged_items = GenericRelation("library.TaggedItem")
    recording_metas = GenericRelation("recordings.RecordingMeta")

    created_at = models.DateTimeField(auto_now_add=True)
    modified_at = models.DateTimeField(auto_now=True)
    deleted_at = models.DateTimeField(null=True, blank=True, default=None, db_index=True)

    def __str__(self) -> str:
        return f"Recording({self.original_name!r} [{self.status}] by {self.author_id})"

    class Meta:
        indexes = [
            models.Index(fields=["author", "created_at"]),
            models.Index(fields=["created_at"]),
            models.Index(fields=["content_hash"]),
            # Serves the by-hash resolvers, which filter with
            # stored_name__startswith on every detail / download / status
            # request. The default-collation unique btree cannot serve a
            # LIKE 'prefix%' on PostgreSQL; this pattern-ops index can.
            # Ignored on backends without operator classes (SQLite).
            models.Index(
                fields=["stored_name"],
                name="recording_stored_name_like",
                opclasses=["varchar_pattern_ops"],
            ),
        ]


class RecordingMeta(models.Model):
    """Parsed format metadata for a recording, extracted during processing.

    Linked to the parent recording via a generic foreign key so the meta system
    can be extended to other content types in the future without schema changes.
    Currently only used for Recording objects.
    """

    content_type = models.ForeignKey(
        ContentType,
        on_delete=models.CASCADE,
        related_name="recording_metas",
    )
    object_id = models.CharField(max_length=255)
    content_object = GenericForeignKey("content_type", "object_id")

    # EDF/BDF format descriptor: 'edf', 'edf+', 'bdf', 'bdf+'
    format = models.CharField(max_length=8)
    # Total recording duration derived from data_record_count * data_record_duration.
    duration = models.FloatField()
    data_record_count = models.IntegerField()
    # Duration of each data record in seconds.
    data_record_duration = models.FloatField()
    signal_count = models.IntegerField()
    # True for EDF+D / BDF+D (discontinuous recordings with gaps between records).
    discontinuous = models.BooleanField(default=False)
    # Original recording datetime from header; null after de-identification.
    recording_date = models.DateTimeField(null=True, blank=True)

    # Montage shape assessed from the resolved EEG canonicals at ingest (see
    # processors.channel_labels.assess_channel_layout). Downstream consumers that
    # assume a referential montage (remontaging, trend computation, epoch
    # generation) gate on this instead of discovering the shape at failure time.
    # Content-free — served to every reader.
    class ChannelLayout(models.TextChoices):
        REFERENTIAL = "referential", "Referential"
        BIPOLAR = "bipolar", "Bipolar"
        MIXED = "mixed", "Mixed"
        UNKNOWN = "unknown", "Unknown"

    channel_layout = models.CharField(
        max_length=16,
        choices=ChannelLayout.choices,
        default=ChannelLayout.UNKNOWN,
    )
    # Non-annotation channels the canonicaliser could not resolve (written as
    # MISC<n> by the de-identification pass); 0 means a fully normalised
    # recording. Denormalised so cross-recording sweeps need no SignalInfo join.
    unresolved_channel_count = models.PositiveSmallIntegerField(default=0)
    # Version of the canonical channel-order spec the stored file was written
    # under (processors.channel_labels.CHANNEL_ORDER_VERSION); 0 means no
    # canonical ordering was applied. Stamped at ingest, never re-derived — a
    # refresh cannot know which spec wrote the bytes.
    channel_order_version = models.PositiveSmallIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    modified_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"RecordingMeta({self.format} {self.duration:.1f}s for {self.content_type_id}/{self.object_id})"

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["content_type", "object_id"],
                name="recordingmeta_unique_per_object",
            ),
        ]
        indexes = [
            models.Index(fields=["content_type", "object_id"]),
        ]


class SignalInfo(models.Model):
    """Per-channel (signal) metadata extracted from a recording file header.

    One row per channel. Stored as a proper relational model so schema changes
    can be handled with standard Django migrations rather than JSON data migrations.
    """

    meta = models.ForeignKey(
        RecordingMeta,
        on_delete=models.CASCADE,
        related_name="signals",
    )
    # Zero-based channel index in the original file.
    index = models.PositiveSmallIntegerField()
    # Label as written in the stored (de-identified) file header. Canonical for
    # channels the ingest cleaner resolved, 'MISC<n>' for the rest; annotation
    # channels keep their spec-mandated label.
    label = models.CharField(max_length=64)
    # Author-private originals of the fields the ingest cleaner rewrites. Captured
    # from the uploaded file before the channel-block de-identification pass;
    # empty when the source value was empty. Serialized only to callers that pass
    # the author-fields check (mirroring Recording.original_name), and preserved
    # verbatim by refresh_signal_metadata — a re-derive from the cleaned file has
    # nothing to derive them from. See docs/engineering-notes/channel-deidentification-plan.md.
    source_label = models.CharField(max_length=64, blank=True, default="")
    source_transducer_type = models.CharField(max_length=160, blank=True, default="")
    source_prefiltering = models.CharField(max_length=160, blank=True, default="")
    # Zero-based position of this channel in the uploaded file, before the
    # canonical reorder; null when unknown (rows replaced by a refresh that
    # could not carry it over). Author-private like the other source_* fields —
    # the acquisition-template order is part of the site fingerprint.
    source_index = models.PositiveSmallIntegerField(null=True, blank=True, default=None)
    # Canonical 10-10 electrode name derived from ``label`` at ingest (e.g. 'T7',
    # 'Fp1', 'Fp1-F7'); '' for non-EEG channels or labels that do not resolve to a
    # known electrode. Non-destructive — ``label`` is never mutated. Recomputed on
    # every reprocess by ``processors.channel_labels.canonicalise_label``, so it
    # cannot drift from ``label``. Shared by to_bids, detectors, YASA, forward model.
    canonical_label = models.CharField(max_length=32, blank=True, default="")
    # Inferred signal type ('eeg', 'emg', 'eog', 'ekg', or '' if unknown).
    signal_type = models.CharField(max_length=32, blank=True, default="")
    physical_unit = models.CharField(max_length=32, blank=True, default="")
    transducer_type = models.CharField(max_length=160, blank=True, default="")
    # Raw prefiltering string from header (e.g. 'HP:0.1Hz LP:75Hz N:50Hz').
    prefiltering = models.CharField(max_length=160, blank=True, default="")
    physical_min = models.FloatField()
    physical_max = models.FloatField()
    digital_min = models.IntegerField()
    digital_max = models.IntegerField()
    # Scaling factor: (physMax - physMin) / (digMax - digMin).
    units_per_bit = models.FloatField()
    # Digital offset for physical-to-digital conversion.
    digital_offset = models.FloatField()
    # Number of samples per data record.
    sample_count = models.PositiveIntegerField()
    # Samples per second (sample_count / data_record_duration); 0 for annotation channels.
    sampling_rate = models.FloatField()
    # Parsed filter values (Hz); 0 means not specified.
    highpass = models.FloatField(default=0.0)
    lowpass = models.FloatField(default=0.0)
    notch = models.FloatField(default=0.0)
    # True for 'EDF Annotations' / 'BDF Annotations' channels.
    is_annotation_channel = models.BooleanField(default=False)

    def __str__(self) -> str:
        return f"SignalInfo({self.index}: {self.label!r} {self.sampling_rate}Hz)"

    class Meta:
        ordering = ["index"]
        constraints = [
            models.UniqueConstraint(fields=["meta", "index"], name="signalinfo_unique_meta_index"),
        ]


class ImportJob(models.Model):
    """Tracks a bulk import operation started via the ``import_recordings`` command.

    Only one job may be ``IN_PROGRESS`` at a time.  The command enforces this
    and requires ``--resume`` or ``--discard`` when an unfinished job exists.
    """

    class Status(models.TextChoices):
        IN_PROGRESS = "in_progress", "In Progress"
        COMPLETED = "completed", "Completed"
        ABORTED = "aborted", "Aborted"

    class Structure(models.TextChoices):
        RECURSIVE = "recursive", "Recursive (mirror directory tree as Collections)"
        RECURSIVE_FLAT = (
            "recursive_flat",
            "Recursive flat (scan subdirectories, no Collections)",
        )
        FLAT = "flat", "Flat (top-level directory only)"

    # The user who will own all recordings created by this job.
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="import_jobs",
    )
    source_path = models.CharField(max_length=2048)
    pipeline_label = models.CharField(max_length=64, default="import")
    structure = models.CharField(
        max_length=16,
        choices=Structure.choices,
        default=Structure.RECURSIVE,
    )
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.IN_PROGRESS,
        db_index=True,
    )

    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    def __str__(self) -> str:
        return f"ImportJob({self.pk} [{self.status}] {self.source_path!r} by {self.owner_id})"

    class Meta:
        indexes = [
            models.Index(fields=["owner", "created_at"]),
        ]
        constraints = [
            # The command's check-then-act guard is racy; this backstops the
            # documented one-job-at-a-time invariant at the schema level.
            models.UniqueConstraint(
                fields=["status"],
                condition=models.Q(status="in_progress"),
                name="importjob_single_in_progress",
            ),
        ]


class ImportJobFile(models.Model):
    """Per-file progress record for a :class:`ImportJob`.

    Allows the import to be interrupted and resumed: files already marked
    ``DONE`` are skipped on resume (unless ``--reprocess`` is given).
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        DONE = "done", "Done"
        FAILED = "failed", "Failed"
        SKIPPED = "skipped", "Skipped"

    job = models.ForeignKey(ImportJob, on_delete=models.CASCADE, related_name="files")
    # Path relative to the job's source_path, using the OS path separator.
    relative_path = models.CharField(max_length=2048)
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
    )
    # Set once the file has been successfully imported.
    recording = models.ForeignKey(
        "Recording",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="import_job_files",
    )
    error = models.TextField(blank=True, default="")
    processed_at = models.DateTimeField(null=True, blank=True)

    def __str__(self) -> str:
        return f"ImportJobFile({self.relative_path!r} [{self.status}] job={self.job_id})"

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["job", "relative_path"],
                name="importjobfile_unique_path_per_job",
            )
        ]
        indexes = [
            models.Index(fields=["job", "status"]),
        ]
