"""Annotation models — generic annotation types that attach to any platform object.

All concrete types (``Annotation``, ``Event``, ``Interruption``, ``Label``) extend
``AnnotationBase``, which provides the generic FK target pair, a 32-character
``object_hash`` (unique per target), and a 64-character ``content_hash`` that
covers all type-specific fields plus any attached ``Code`` objects.

``Code`` stores standardised classification codes (ICD-10, SNOMED, LOINC, etc.)
and attaches via a generic FK to ``Event``, ``Interruption``, or ``Label``.
Code mutations trigger ``recompute_content_hash`` on the parent via a signal in
``annotations/signals.py``.
"""

import hashlib
import json

from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey, GenericRelation
from django.contrib.contenttypes.models import ContentType
from django.core.validators import RegexValidator
from django.db import models


class AnnotationBase(models.Model):
    """Abstract base shared by all annotation types."""

    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="%(class)ss",
    )
    target_content_type = models.ForeignKey(
        ContentType,
        on_delete=models.CASCADE,
        related_name="%(class)s_targeted",
    )
    target_object_id = models.CharField(max_length=255)
    target_object = GenericForeignKey("target_content_type", "target_object_id")

    # Binds the annotation to the exact signal version it was made/scored on
    # (pipeline-persistence-models §F). 'source' is the base, unprocessed version
    # — the platform's canonical name for "no manifest applied" (SOURCE_VERSION_ID
    # in recordings.pipeline.manifest); kept as a plain string here so annotations
    # stays decoupled from the reconstruction pipeline. Existing rows default to
    # 'source'.
    version_id = models.CharField(
        max_length=64,
        default="source",
        # A database-level default too, not only the Python one: the column is NOT NULL, and an
        # insert that omits it (a worker running code from before this field existed, a raw
        # insert, a bulk path) must still get the base version rather than fail the constraint.
        db_default="source",
        help_text="Signal version this annotation is bound to; 'source' (base version) by default.",
    )

    object_hash = models.CharField(
        max_length=32,
        validators=[
            RegexValidator(
                regex=r"^[A-Za-z0-9]{32}$",
                message="object_hash must be 32 alphanumeric characters",
            )
        ],
    )
    content_hash = models.CharField(max_length=64, blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    modified_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True
        indexes = [
            models.Index(fields=["author", "created_at"]),
            models.Index(
                fields=[
                    "target_content_type",
                    "target_object_id",
                    "author",
                    "created_at",
                ]
            ),
            models.Index(fields=["content_hash"]),
            models.Index(fields=["object_hash"]),
        ]
        constraints = [
            # version_id is part of the identity: the same object_hash may exist
            # on different signal versions of the same target (an annotation made
            # on 'source' and its counterpart on a derived version are distinct
            # rows). Ordered so the (target, version_id) prefix also serves
            # "annotations on version V of this target" queries.
            models.UniqueConstraint(
                fields=[
                    "target_content_type",
                    "target_object_id",
                    "version_id",
                    "object_hash",
                ],
                name="%(class)s_unique_hash_per_target",
            )
        ]

    def _hash_fields(self) -> dict:
        """Override in subclasses to contribute type-specific fields to the hash."""
        return {}

    def build_content_hash(self, *, extra: dict | None = None) -> str:
        payload = {
            "model": self.__class__.__name__,
            "author_id": self.author_id,
            "target_content_type_id": self.target_content_type_id,
            "target_object_id": str(self.target_object_id or ""),
            "object_hash": (self.object_hash or "").upper(),
            **self._hash_fields(),
            **(extra or {}),
        }
        # Bind the hash to the signal version — but only when it is not the base
        # version. 'source' is the *absence* of a binding (mirroring the empty
        # manifest → SOURCE_VERSION_ID rule), so it contributes nothing and
        # every existing source-bound annotation hashes exactly as it did before
        # this field existed. A non-source binding is a distinguishing factor and
        # enters the hash, giving the version binding the same integrity coverage
        # the target already has.
        version_id = getattr(self, "version_id", "source")
        if version_id not in ("", "source"):
            payload["version_id"] = version_id
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _codes_hash_extra(self) -> dict:
        """Return a stable sorted codes payload for hash inclusion if this type has codes.

        ``meta`` is included so that mutations to it are integrity-checked
        alongside ``standard`` and ``value``. Sort key stays (standard, value)
        because the canonical Code-creation idiom (``update_or_create`` keyed
        on ``content_type + object_id + standard``) guarantees at most one
        Code per parent per standard — so the sort key is unique in practice.
        Internal dict-key ordering within ``meta`` does not affect the hash:
        ``build_content_hash`` calls ``json.dumps(..., sort_keys=True)`` which
        recursively canonicalises object keys at serialisation time.
        """
        if not self.pk or not hasattr(self, "codes"):
            return {}
        codes_data = sorted(
            [{"standard": c.standard, "value": c.value, "meta": c.meta} for c in self.codes.all()],
            key=lambda x: (x["standard"], x["value"]),
        )
        return {"codes": codes_data}

    def recompute_content_hash(self) -> None:
        """Recompute and persist content_hash including current codes.

        Called by the Code post_save/post_delete signal after every code mutation.
        Uses UPDATE to avoid triggering pre_save/post_save on the parent.
        """
        new_hash = self.build_content_hash(extra=self._codes_hash_extra())
        type(self).objects.filter(pk=self.pk).update(content_hash=new_hash)
        self.content_hash = new_hash

    def save(self, *args, **kwargs):
        if self.object_hash:
            self.object_hash = self.object_hash.upper()
        self.content_hash = self.build_content_hash(extra=self._codes_hash_extra())
        super().save(*args, **kwargs)


class Annotation(AnnotationBase):
    """Free-form structured note attached to an object snapshot (formerly Bundle)."""

    name = models.CharField(max_length=255, blank=True, default="")
    content = models.JSONField()

    def __str__(self) -> str:
        return f"Annotation({self.object_hash} by {self.author_id} on {self.target_content_type_id}/{self.target_object_id})"

    def _hash_fields(self) -> dict:
        return {"name": self.name, "content": self.content}


class Code(models.Model):
    """Standardised classification code attached to an Event, Interruption, or Label."""

    def __str__(self) -> str:
        return f"Code({self.standard}:{self.value})"

    content_type = models.ForeignKey(
        ContentType,
        on_delete=models.CASCADE,
        related_name="annotation_codes",
    )
    object_id = models.CharField(max_length=255)
    annotation = GenericForeignKey("content_type", "object_id")

    standard = models.CharField(max_length=64)
    value = models.TextField()
    meta = models.JSONField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    modified_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["content_type", "object_id"]),
            models.Index(fields=["standard"]),
        ]
        constraints = [
            # The canonical write pattern is update_or_create keyed on these
            # three fields (see annotations/README.md). Without DB-level
            # enforcement, two concurrent requests can both decide to INSERT
            # and leave duplicate rows.
            models.UniqueConstraint(
                fields=["content_type", "object_id", "standard"],
                name="annotations_code_unique_per_target_standard",
            ),
        ]


class Event(AnnotationBase):
    """Time-stamped event annotation with optional name, duration, and value."""

    def __str__(self) -> str:
        return f"Event({self.object_hash} name={self.name!r} t={self.timestamp})"

    name = models.CharField(max_length=255)
    timestamp = models.FloatField()
    duration = models.FloatField(null=True, blank=True)
    value = models.JSONField(null=True, blank=True)
    event_class = models.CharField(max_length=64, blank=True, default="")

    codes = GenericRelation(Code, related_query_name="event")

    def _hash_fields(self) -> dict:
        return {
            "name": self.name,
            "timestamp": self.timestamp,
            "duration": self.duration,
            "value": self.value,
        }


class Interruption(AnnotationBase):
    """Signal interruption annotation with start timestamp and duration."""

    def __str__(self) -> str:
        return f"Interruption({self.object_hash} t={self.timestamp})"

    timestamp = models.FloatField()
    duration = models.FloatField()

    codes = GenericRelation(Code, related_query_name="interruption")

    def _hash_fields(self) -> dict:
        return {"timestamp": self.timestamp, "duration": self.duration}


class Label(AnnotationBase):
    """Named label annotation with optional structured value."""

    def __str__(self) -> str:
        return f"Label({self.object_hash} name={self.name!r})"

    name = models.CharField(max_length=255)
    value = models.JSONField(null=True, blank=True)

    codes = GenericRelation(Code, related_query_name="label")

    def _hash_fields(self) -> dict:
        return {"name": self.name, "value": self.value}


class AnnotationKind(models.Model):
    """Controlled vocabulary of annotation-kind tokens
    (retention-and-lifecycle-plan §1.1).

    A semantic token that analysis producers *advertise* (via
    ``compute.AnalysisRun.produces_kind``) and consumers *require*, so the
    compute dispatch DAG and the annotation layer resolve against one registry
    rather than each inventing strings. Examples: ``hypnogram``,
    ``spike_events``, ``artifact_spans``.

    The ``token`` — not the numeric pk — is the cross-app identity. ``compute``
    references it by string, deliberately *not* by ForeignKey, so the compute app
    stays decoupled from this table (the same loose-coupling rationale as the
    ``(recording, version_id)`` string links). ``max_length`` matches
    ``AnalysisRun.produces_kind`` (64) so a token round-trips between the two
    without truncation.
    """

    token = models.CharField(
        max_length=64,
        unique=True,
        validators=[
            RegexValidator(
                regex=r"^[a-z][a-z0-9_]*$",
                message="token must be lowercase snake_case, e.g. 'spike_events'",
            )
        ],
        help_text="Semantic kind token, e.g. 'hypnogram', 'spike_events', 'artifact_spans'.",
    )
    display_name = models.CharField(max_length=128, blank=True, default="")
    description = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["token"]

    def __str__(self) -> str:
        return self.token
