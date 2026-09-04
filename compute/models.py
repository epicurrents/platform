"""Compute app models — cached results of expensive server-side computations.

``LeadFieldCache``
    Stores a pre-computed EEG lead field matrix (forward solution) for a
    given standard electrode montage and spherical head model.  Once
    computed it is served to the browser so the Pyodide source-localisation
    script can run the inverse step (sLORETA / eLORETA / dipole) entirely
    client-side without repeating the expensive forward computation.

    The lead field matrix and source-position grid are stored as raw
    little-endian float64 bytes (C-contiguous / row-major order) in
    ``BinaryField`` columns.  The caller is responsible for interpreting
    them with the shape information in ``n_channels``, ``n_sources``, and
    ``n_orient`` (lead field shape = ``(n_channels, n_sources * n_orient)``,
    source positions shape = ``(n_sources, 3)``).

Storage note
------------
For typical clinical EEG montages (19–64 channels, 200–2 000 source points)
each row is well under 1 MB.  High-density caps (256 channels, ~5 000 source
points, free orientation) can reach ~10 MB; still within PostgreSQL bytea
limits.  If storage becomes a concern, migrate lead_field / src_pos to
``FileField`` backed by a configurable storage backend.
"""

import numpy as np
from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db import models
from django.utils import timezone


class LeadFieldCache(models.Model):
    """Cached EEG lead field matrix for a standard montage + sphere model.

    Uniqueness is enforced on ``(montage_name, n_orient, grid_resolution_mm)``
    so that different resolution or orientation choices for the same montage
    are stored as separate rows while preventing duplicates.
    """

    # --- Identity -----------------------------------------------------------

    montage_name = models.CharField(
        max_length=128,
        help_text=(
            "MNE standard montage name, e.g. 'standard_1020', 'biosemi64'. "
            "Must be accepted by mne.channels.make_standard_montage()."
        ),
    )

    # --- Shape --------------------------------------------------------------

    n_channels = models.PositiveIntegerField(
        help_text="Number of EEG channels (rows of the lead field matrix).",
    )
    n_sources = models.PositiveIntegerField(
        help_text="Number of source grid points (columns ÷ n_orient).",
    )
    n_orient = models.PositiveSmallIntegerField(
        default=1,
        help_text="Orientations per source: 1 = fixed (surface normal), 3 = free (x/y/z).",
    )

    # --- Head model ---------------------------------------------------------

    grid_resolution_mm = models.FloatField(
        default=7.5,
        help_text="Source grid spacing in millimetres.",
    )
    sphere_radius_m = models.FloatField(
        default=0.09,
        help_text="Radius of the spherical head model in metres.",
    )
    sphere_center_m = models.JSONField(
        default=list,
        help_text="[x, y, z] centre of the sphere in metres (head coordinates).",
    )

    # --- Channel list -------------------------------------------------------

    channel_names = models.JSONField(
        help_text="Ordered list of channel name strings matching the matrix rows.",
    )

    # --- Binary data --------------------------------------------------------

    lead_field = models.BinaryField(
        help_text=(
            "Raw little-endian float64 bytes of the lead field matrix, "
            "shape (n_channels, n_sources * n_orient), C order."
        ),
    )
    src_pos = models.BinaryField(
        help_text=(
            "Raw little-endian float64 bytes of the source positions, shape (n_sources, 3), C order, in metres."
        ),
    )

    # --- Bookkeeping --------------------------------------------------------

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["montage_name", "n_orient", "grid_resolution_mm"],
                name="compute_leadfieldcache_unique_montage_orient_res",
            ),
        ]
        indexes = [
            models.Index(fields=["montage_name"], name="compute_lfc_montage_idx"),
        ]

    def __str__(self) -> str:
        orient = "fixed" if self.n_orient == 1 else "free"
        return (
            f"LeadFieldCache({self.montage_name}, {self.n_channels}ch, "
            f"{self.n_sources}src, {orient}, {self.grid_resolution_mm}mm)"
        )

    @classmethod
    def upsert_from_compute(
        cls,
        *,
        montage_name: str,
        n_orient: int,
        grid_resolution_mm: float,
        sphere_radius_m: float,
        sphere_center_m: tuple[float, float, float],
        lead_field: np.ndarray,
        src_pos: np.ndarray,
        channel_names: list[str],
    ) -> tuple["LeadFieldCache", bool]:
        """Atomically upsert a freshly-computed lead field into the cache.

        Looks up by the unique key ``(montage_name, n_orient,
        grid_resolution_mm)`` and either creates a new row or replaces the
        existing one in a single ``update_or_create()`` call. Closes the
        TOCTOU window that an explicit ``filter().first() + if/else`` pair
        would leave open. Used by both the API trigger endpoint and the
        ``compute_leadfield`` management command so the two paths cannot
        drift apart.

        Returns ``(row, created)`` where ``created`` is ``True`` for a
        fresh insert and ``False`` for an in-place replacement. ``created_at``
        is preserved on replacement; ``updated_at`` is bumped by ``auto_now``.
        """
        n_ch, _ = lead_field.shape
        n_src = src_pos.shape[0]
        return cls.objects.update_or_create(
            montage_name=montage_name,
            n_orient=n_orient,
            grid_resolution_mm=grid_resolution_mm,
            defaults={
                "n_channels": n_ch,
                "n_sources": n_src,
                "sphere_radius_m": sphere_radius_m,
                "sphere_center_m": list(sphere_center_m),
                "channel_names": channel_names,
                "lead_field": lead_field.tobytes(),
                "src_pos": src_pos.tobytes(),
            },
        )


# ===========================================================================
# Pipeline persistence
# ---------------------------------------------------------------------------
# The durable/operational models behind the reconstruction and analysis
# pipelines (see ``pipeline-persistence-models.md``). ``compute`` owns them all;
# they FK outward to ``recordings.Recording`` and ``annotations.Event`` and never
# the reverse. The run tables reference each other only by ``(recording,
# version_id)`` *strings* — never by ForeignKey — because a version's identity is
# the manifest hash in the ``activity.ObjectChangeLog`` chain (not a row) and
# because ``ArtifactCacheEntry`` is the evictable GC unit: an FK from a durable
# run record to the cache entry would force eviction to either cascade-delete the
# record or null its provenance. String linkage lets the record keep naming the
# version it scored after the bytes are evicted (see §E of the design note).
#
# Field-width conventions: content-addressed version ids / digests are full
# sha256 hex (64 chars); the audit chain hashes on ``PipelineRunAudit`` are 32
# chars to match ``activity.audit``'s truncated-hash primitives, which the chain
# reuses.
# ===========================================================================


class RecordingJob(models.Model):
    """Reconstruction operational ledger — one row per RECONSTRUCT stage on a
    recording (signal-pipeline-plan §5).

    Live run state, rebuildable from the stage registry + the manifest, so it
    holds no irreplaceable data and is deliberately **not** part of any integrity
    chain. The durable fact that a stage produced a version is recorded once, on
    completion, as a CREATE event in :class:`PipelineRunAudit` — never a MODIFY of
    the immutable source.
    """

    recording = models.ForeignKey(
        "recordings.Recording",
        on_delete=models.CASCADE,
        related_name="reconstruction_jobs",
    )
    stage_name = models.CharField(
        max_length=64,
        help_text="Reconstruction stage identifier; matches the stage registry.",
    )
    order = models.PositiveSmallIntegerField(
        help_text="Resolved topological position in the reconstruction chain.",
    )
    enabled = models.BooleanField(
        default=True,
        help_text="Per-recording override to skip this stage.",
    )

    class State(models.TextChoices):
        QUEUED = "queued", "Queued"
        RUNNING = "running", "Running"
        DONE = "done", "Done"
        FAILED = "failed", "Failed"
        SKIPPED = "skipped", "Skipped"

    state = models.CharField(max_length=8, choices=State.choices, default=State.QUEUED, db_index=True)

    input_digest = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text="Hash of the bytes this stage consumed (signal-pipeline-plan §3.2 input_hash).",
    )
    output_version_id = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text="Manifest hash of the version this stage produced; '' until DONE.",
    )

    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    error = models.TextField(blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["recording", "stage_name"],
                name="compute_recordingjob_unique_stage_per_recording",
            ),
        ]
        indexes = [
            models.Index(
                fields=["recording", "order"],
                name="compute_recjob_rec_order_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"RecordingJob({self.recording_id}/{self.stage_name} [{self.state}])"


class ArtifactCacheEntry(models.Model):
    """Materialised version bytes in the cache tier (signal-pipeline-plan §3.5).

    The **GC unit**: reference-counted (count is computed, not stored),
    evictable, and the row a subject-erasure must purge. Keyed by ``(recording,
    version_id)`` — the same content-addressed string the run tables join on.
    ``recording`` is part of the key (not redundant) because the ``'source'``
    sentinel is the same string for every recording and so is not globally
    unique on its own.
    """

    recording = models.ForeignKey(
        "recordings.Recording",
        on_delete=models.CASCADE,
        related_name="artifact_cache_entries",
    )
    version_id = models.CharField(
        max_length=64,
        help_text="Manifest hash of the materialised version, or 'source' for the base version.",
    )
    reproducible = models.BooleanField(
        default=True,
        help_text="Whether the bytes can be rebuilt from source + manifest; governs eviction cost, not identity.",
    )
    storage_path = models.CharField(
        max_length=1024,
        blank=True,
        default="",
        help_text="Location of the materialised bytes; '' once evicted/purged.",
    )
    size_bytes = models.BigIntegerField(default=0)

    class Disposition(models.TextChoices):
        MATERIALISED = "materialised", "Materialised"
        EVICTED = "evicted", "Evicted"  # reproducible: gone, rebuild on miss
        TOMBSTONED = "tombstoned", "Tombstoned"  # non-reproducible referenced eviction
        PURGED = "purged", "Purged"  # subject erasure

    disposition = models.CharField(
        max_length=12,
        choices=Disposition.choices,
        default=Disposition.MATERIALISED,
        db_index=True,
    )

    pinned = models.BooleanField(
        default=False,
        help_text="Explicit retention pin (retention §2.2); protects from GC.",
    )
    last_accessed_at = models.DateTimeField(null=True, blank=True, help_text="LRU timestamp for cache eviction.")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["recording", "version_id"],
                name="compute_artifactcache_unique_version_per_recording",
            ),
        ]

    def __str__(self) -> str:
        return f"ArtifactCacheEntry({self.recording_id}/{self.version_id} [{self.disposition}])"


class AnalysisRun(models.Model):
    """One annotation-producing analysis run against a signal version
    (retention-and-lifecycle-plan §1, analysis-execution-plan).

    Durable record — the run *record* is never rebuildable, even though its
    cached outputs are. Identity is the compute run key ``(input_digest,
    image_digest, params)``. References the exact version it scored by string,
    not FK (see the module note on §E).
    """

    recording = models.ForeignKey(
        "recordings.Recording",
        on_delete=models.CASCADE,
        related_name="analysis_runs",
    )
    input_version_id = models.CharField(
        max_length=64,
        help_text="Signal version scored (a RecordingJob.output_version_id or 'source'). String, not FK.",
    )
    produces_kind = models.CharField(
        max_length=64,
        help_text="Annotation kind this run emits (a token in the annotations AnnotationKind vocabulary).",
    )

    input_digest = models.CharField(
        max_length=64,
        help_text="Run-identity digest: folds in the version digest and the digests of any annotation sets consumed (retention §1.1).",
    )
    image_digest = models.CharField(max_length=128, help_text="Detector container/model identity.")
    params = models.JSONField(default=dict, help_text="Run parameters; folded into input_digest for identity.")

    grid_s = models.FloatField(
        help_text="Segmentation grid size in seconds (analysis-execution §1). Not part of identity."
    )
    halo_s = models.FloatField(default=0.0, help_text="Context halo around each segment, seconds.")
    max_event_span_s = models.FloatField(default=0.0, help_text="Longest event the detector may emit, seconds.")

    class Locality(models.TextChoices):
        WINDOW_INDEPENDENT = "window_independent", "Window-independent"
        LOCAL_CONTEXT = "local_context", "Local context"
        GLOBAL = "global", "Global"

    locality = models.CharField(max_length=20, choices=Locality.choices)

    reproducible = models.BooleanField(
        default=False,
        help_text="Earned via the conformance oracle (analysis-execution §5), never declared.",
    )
    conformance = models.JSONField(
        default=dict,
        blank=True,
        help_text="Conformance verdict: {mode, pass, corpus}.",
    )

    class State(models.TextChoices):
        QUEUED = "queued", "Queued"
        RUNNING = "running", "Running"
        DONE = "done", "Done"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"

    state = models.CharField(max_length=10, choices=State.choices, default=State.QUEUED, db_index=True)

    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["input_digest", "image_digest"],  # params folded into input_digest
                name="compute_analysisrun_unique_run_key",
            ),
        ]
        indexes = [
            models.Index(
                fields=["recording", "input_version_id", "produces_kind"],
                name="compute_arun_rec_ver_kind_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"AnalysisRun({self.recording_id}/{self.produces_kind} @{self.input_version_id} [{self.state}])"


class AnalysisSegment(models.Model):
    """Per-segment coverage of an :class:`AnalysisRun` — unioned on read, never a
    single mutable coverage row (analysis-execution §2, the contention-free
    commit path)."""

    run = models.ForeignKey(AnalysisRun, on_delete=models.CASCADE, related_name="segments")
    index = models.PositiveIntegerField(help_text="Segment position; contract-stable across re-fan-out.")
    start_s = models.FloatField()
    end_s = models.FloatField()

    class State(models.TextChoices):
        PENDING = "pending", "Pending"
        DONE = "done", "Done"
        FAILED = "failed", "Failed"
        SKIPPED = "skipped", "Skipped"

    state = models.CharField(max_length=8, choices=State.choices, default=State.PENDING, db_index=True)
    skip_reason = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text="Why SKIPPED (gap/flat/out-of-coverage); must be resegmentation-invariant (analysis-execution §5).",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["run", "index"],
                name="compute_analysissegment_unique_index_per_run",
            ),
        ]

    def __str__(self) -> str:
        return f"AnalysisSegment(run={self.run_id} #{self.index} [{self.state}])"


class RunAnnotation(models.Model):
    """Provenance edge: an :class:`AnalysisRun` (at a segment) produced an
    ``annotations.Event``.

    Lives in ``compute`` so the ``annotations`` app stays producer-agnostic
    (retention §1.1) — only machine-produced events get a link row. Idempotency
    of the fan-out is the :class:`AnalysisSegment` coverage row's job, not this
    edge's; this records which ``(run, segment)`` produced which event and is the
    forward index for re-fan-out invalidation. Distinct from the version
    reference (the annotation's own ``version_id`` keeps *bytes* alive); this
    keeps *provenance*.
    """

    run = models.ForeignKey(AnalysisRun, on_delete=models.CASCADE, related_name="annotation_links")
    event = models.ForeignKey("annotations.Event", on_delete=models.CASCADE, related_name="+")
    segment_index = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Producing segment; null for run-global (GLOBAL locality) output.",
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["run", "event"],
                name="compute_runannotation_unique_event_per_run",
            ),
        ]
        indexes = [
            models.Index(
                fields=["run", "segment_index"],
                name="compute_runann_run_segment_idx",
            ),
            models.Index(fields=["event"], name="compute_runann_event_idx"),
        ]

    def __str__(self) -> str:
        return f"RunAnnotation(run={self.run_id} → event={self.event_id})"


class PipelineRunAudit(models.Model):
    """Durable, append-only run-event audit — one dedicated, hash-chained table
    for both the reconstruction and analysis pipelines
    (pipeline-persistence-models §D).

    Each pipeline serialises its own structure into ``meta`` rather than a table
    full of the other pipeline's null columns. Recording a completed run is a
    CREATE of a new asset (a version, an annotation set), never a MODIFY of the
    immutable source. Chained with the same primitives as
    ``activity.ObjectChangeLog`` (``activity.audit``) so there is one verifiable
    integrity mechanism.
    """

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="pipeline_run_audits",
    )
    # target via GenericFK — a Recording, ArtifactCacheEntry, or AnalysisRun.
    target_content_type = models.ForeignKey(
        ContentType,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="pipeline_run_audits",
    )
    target_object_id = models.CharField(max_length=255, blank=True, default="")
    target = GenericForeignKey("target_content_type", "target_object_id")

    class Action(models.TextChoices):
        RECONSTRUCT_RAN = "reconstruct.ran", "Reconstruct ran"  # a version CREATED
        ANALYSIS_STARTED = "analysis.started", "Analysis started"  # PHI materialised / began
        ANALYSIS_FINISHED = "analysis.finished", "Analysis finished"  # annotation set CREATED
        DETECTOR_EGRESS = "detector.egress", "Detector egress"  # external-container exposure
        ARTIFACT_EVICTED = "artifact.evicted", "Artifact evicted"  # retention §2.4 tombstone trail

    action = models.CharField(max_length=32, choices=Action.choices, db_index=True)

    meta = models.JSONField(
        default=dict,
        blank=True,
        help_text="Per-pipeline payload. NEVER clinical findings; NEVER unmasked PII.",
    )

    # Append-only insertion time. Writer-settable (like
    # activity.ObjectChangeLog.created_at, which is fed into the hash) so the
    # chain writer can hash the value it inserts; the default preserves the
    # auto_now_add behaviour for every current caller.
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    # Chain pointers — reuse activity.audit's 32-char truncated-hash primitives.
    prev_hash = models.CharField(max_length=32, blank=True, default="")
    content_hash = models.CharField(max_length=32, blank=True, default="")

    class Meta:
        indexes = [
            models.Index(
                fields=["target_content_type", "target_object_id"],
                name="compute_paudit_target_idx",
            ),
            models.Index(
                fields=["action", "created_at"],
                name="compute_paudit_action_dt_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"PipelineRunAudit({self.action} → {self.target_content_type_id}/{self.target_object_id})"
