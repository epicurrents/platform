"""Activity app models — per-request audit log and field-level change tracking.

``Activity`` records every API interaction (actor, method, path, status code,
optional target object).  Rows older than ``ACTIVITY_ARCHIVE_AFTER_DAYS`` are
soft-archived by the ``archive_old_activity`` Celery task.

``ObjectChangeLog`` captures the before-state and a field-level diff for every
create/modify/delete event on tracked models. Each entry carries an integrity
hash (``after_hash``) over the after-state plus change metadata; under the v3
algorithm the hash is HMAC-keyed and chained to the previous row in the same
per-content_type shard (see ``activity/audit.py`` and ``activity/README.md`` →
*Threat model*). Entries are never archived or deleted — they are the
permanent audit trail used by the rollback API. The one sanctioned mutation
is GDPR Art. 17 subject erasure (``activity/erasure.py``), which scrubs
personal data from the payload fields and tombstones the row with a re-sealed
``erased_hash`` while leaving ``after_hash`` untouched so chain links keep
verifying.
"""

from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db import models


class ActiveActivityManager(models.Manager):
    """Default manager — excludes archived rows from all standard queries."""

    def get_queryset(self):
        return super().get_queryset().filter(archived_at__isnull=True)


class Activity(models.Model):
    """Audit trail record for one auditable interaction."""

    class Interface(models.TextChoices):
        """Caller interface that produced this row.

        ``api`` rows come from ``ApiActivityLoggingMiddleware`` and represent
        HTTP requests against the public API surface.  ``celery`` and
        ``command`` rows come from ``activity.system_activity`` blocks
        opened explicitly inside a Celery task or a management command.
        Indexed so SIEM rules and audit views can filter by interface
        without scanning the full table.
        """

        API = "api", "API"
        CELERY = "celery", "Celery"
        COMMAND = "command", "Command"

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="activities",
        null=True,
        blank=True,
    )
    interface = models.CharField(
        max_length=16,
        choices=Interface.choices,
        default=Interface.API,
        db_index=True,
    )
    verb = models.CharField(max_length=128)
    method = models.CharField(max_length=12, blank=True, default="")
    path = models.TextField(blank=True, default="")
    status_code = models.PositiveSmallIntegerField(null=True, blank=True)

    target_content_type = models.ForeignKey(
        ContentType,
        on_delete=models.SET_NULL,
        related_name="activity_targets",
        null=True,
        blank=True,
    )
    target_object_id = models.CharField(max_length=255, blank=True, default="")
    target_identifier = models.TextField(blank=True, default="")

    # Populated by ApiActivityLoggingMiddleware when the request targets a
    # project-specific endpoint (path contains /project/api/).  Empty string
    # for core API requests.
    project = models.CharField(max_length=64, blank=True, default="")

    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    archived_at = models.DateTimeField(null=True, blank=True, default=None, db_index=True)

    # Default manager hides archived rows; use Activity.including_archived for historical queries.
    objects = ActiveActivityManager()
    including_archived = models.Manager()

    def __str__(self) -> str:
        actor_str = str(self.actor) if self.actor_id is not None else "anonymous"
        return f"Activity({self.method} {self.path} by {actor_str} [{self.status_code}])"

    class Meta:
        indexes = [
            models.Index(fields=["created_at"]),
            models.Index(fields=["verb", "created_at"]),
            models.Index(fields=["target_content_type", "target_object_id"]),
        ]


class ObjectChangeLog(models.Model):
    """Destructive-change audit record with before-state and integrity hash."""

    ACTION_CREATE = "create"
    ACTION_MODIFY = "modify"
    ACTION_DELETE = "delete"
    ACTION_ROLLBACK = "rollback"
    ACTION_ERASE = "erase"

    ACTION_CHOICES = (
        (ACTION_CREATE, "Create"),
        (ACTION_MODIFY, "Modify"),
        (ACTION_DELETE, "Delete"),
        (ACTION_ROLLBACK, "Rollback"),
        (ACTION_ERASE, "Erase"),
    )

    activity = models.ForeignKey(
        Activity,
        on_delete=models.SET_NULL,
        related_name="change_logs",
        null=True,
        blank=True,
    )

    # PROTECT, not CASCADE: change-log rows must outlive everything they
    # reference, including the ContentType row itself. A historical erasure
    # registration (see activity/erasure.py) scrubs a dropped model's rows
    # through its surviving ContentType, and `remove_stale_contenttypes`
    # offers to delete exactly that row — under CASCADE a confirmed run would
    # destroy the whole shard outside the sanctioned tombstoning path and
    # silently unhook the scrub. Failing loudly is the append-only answer.
    content_type = models.ForeignKey(ContentType, on_delete=models.PROTECT, related_name="object_change_logs")
    object_id = models.CharField(max_length=255)
    content_object = GenericForeignKey("content_type", "object_id")

    action = models.CharField(max_length=16, choices=ACTION_CHOICES)

    # Mirrors Activity.project.  Set on API-driven changes by the signal
    # handlers (copied from the current Activity) and passed explicitly for
    # changes written outside a request context (e.g. management commands).
    project = models.CharField(max_length=64, blank=True, default="")

    performed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="performed_object_changes",
        null=True,
        blank=True,
    )

    before_state = models.JSONField(default=dict)
    changes = models.JSONField(null=True, blank=True)
    after_hash = models.CharField(max_length=32)

    # Algorithm version + HMAC key version used to compute after_hash. The
    # writer captures the values from `current_write_hash_version()` once
    # and stores them alongside the hash so a later verify recomputes under
    # the same algorithm even after the current write version has rolled
    # forward. `hash_key_version` is NULL for unkeyed algorithms (v1).
    hash_algorithm = models.CharField(max_length=8, default="v1")
    hash_key_version = models.IntegerField(null=True, blank=True)

    # Chain pointer + monotonic position for the v3 algorithm. Each row's
    # after_hash includes the previous row's after_hash in the same per-
    # content_type shard, so tampering with row N invalidates every row
    # N+1, N+2, … in the same shard. `prev_hash` is the empty string and
    # `sequence_no` is NULL for non-chain rows (v1/v2 and pre-chain v3
    # deployments that haven't seen a write yet on this shard).
    prev_hash = models.CharField(max_length=32, blank=True, default="")
    sequence_no = models.BigIntegerField(null=True, blank=True)

    # Caller-supplied derived-state digests mixed into the audit hash. Used
    # when a single audit row represents a state transition that also covers
    # high-fanout dependent rows (e.g. SignalInfo under Recording) that
    # would inflate the chain if recorded individually. Each entry's value
    # is a hex digest the writer computed over the dependent state; the
    # verifier re-derives the same digest from current DB state and compares
    # via `verify_derived_state`. Empty dict for rows that have no
    # dependent-state coverage (the default for signal-driven writes).
    extra_payload = models.JSONField(default=dict, blank=True)

    # Subject-erasure tombstone (GDPR Art. 17; see activity/erasure.py).
    # A non-null `erased_at` marks the row's payload fields as scrubbed of
    # personal data. `after_hash` is deliberately left at its original value
    # so downstream chain links keep verifying; content integrity of the
    # scrubbed payload is instead sealed by `erased_hash`, computed at
    # erasure time under `erased_hash_algorithm` / `erased_hash_key_version`
    # and bound to the original `after_hash`. Erased rows refuse rollback.
    erased_at = models.DateTimeField(null=True, blank=True, default=None)
    erased_hash = models.CharField(max_length=32, blank=True, default="")
    erased_hash_algorithm = models.CharField(max_length=8, blank=True, default="")
    erased_hash_key_version = models.IntegerField(null=True, blank=True)

    # Not auto_now_add: the same timestamp must be fed into compute_audit_hash,
    # so the writer captures `now` once and uses it for both fields.
    created_at = models.DateTimeField()

    def __str__(self) -> str:
        performer = str(self.performed_by) if self.performed_by_id is not None else "unknown"
        return f"ObjectChangeLog({self.action} {self.content_type}/{self.object_id} by {performer})"

    class Meta:
        indexes = [
            models.Index(fields=["created_at"]),
            models.Index(fields=["content_type", "object_id", "created_at"]),
            models.Index(fields=["action", "created_at"]),
            # Verification walks the chain per content_type ordered by
            # sequence_no. The composite index keeps the walk O(N) over the
            # shard rather than O(N log N) over the full table.
            models.Index(fields=["content_type", "sequence_no"]),
        ]
