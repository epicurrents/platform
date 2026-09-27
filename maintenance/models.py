"""The packages a deployment holds and the maintenance jobs run against it.

``MaintenanceJob`` is the audited request plus a projection of what the executor
reported. For the celery tier the row is the whole record; for the host tier the
spool under ``MAINTENANCE_SPOOL_PATH`` is authoritative, because a rollback
restores the database dump and erases every row written after it — the spool
survives, and ``maintenance.spool.sync`` re-creates the rows from it.

State changes go through :meth:`MaintenanceJob.transition`, a compare-and-set
``QuerySet.update`` followed by an explicit audit recorder, because a bulk update
fires no signal and the concurrent writers (the API, the Celery executor, the
spool sync) must not overwrite each other's transitions.
"""

import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone


class MaintenancePackage(models.Model):
    """A distribution package uploaded through the API and held in the spool.

    ``sha256`` is the identifier: the package directory under the spool is named
    after it and the path is derived, never stored. Rows are written by the
    upload endpoint, which has verified the package, and by the reconciliation,
    which finds a directory with no row and verifies it before a request may
    name it: ``unverified`` until the tarball has been hashed against the
    manifest, ``invalid`` when the signature or the hash does not hold.
    """

    class State(models.TextChoices):
        AVAILABLE = "available", "Available"
        UNVERIFIED = "unverified", "Unverified"
        INVALID = "invalid", "Invalid"
        APPLIED = "applied", "Applied"
        PRUNED = "pruned", "Pruned"

    sha256 = models.CharField(max_length=64, unique=True)
    version = models.CharField(max_length=32)
    project = models.CharField(max_length=64, blank=True)
    plugins = models.JSONField(default=list, blank=True)
    platform_compatible = models.CharField(max_length=64, blank=True)
    built_at = models.DateTimeField(null=True, blank=True)
    size = models.BigIntegerField(default=0)
    key_id = models.CharField(max_length=16, blank=True)
    manifest = models.JSONField(default=dict, blank=True)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="maintenance_packages",
    )
    uploaded_at = models.DateTimeField(auto_now_add=True)
    state = models.CharField(max_length=16, choices=State.choices, default=State.AVAILABLE)

    class Meta:
        ordering = ["-uploaded_at"]

    def __str__(self):
        return f"{self.version} ({self.sha256[:12]})"


class MaintenanceJob(models.Model):
    """One requested operation, on either executor tier.

    ``job_id`` is the identifier on the wire and in the spool; the integer
    primary key stays internal. ``in_flight`` mirrors whether ``state`` is one of
    :attr:`IN_FLIGHT_STATES`, and the partial unique constraint on it allows one
    such row across both tiers: ``compose stop celery`` during an update would
    kill a celery-tier job, so the tiers share the limit.
    """

    class Executor(models.TextChoices):
        CELERY = "celery", "Celery"
        HOST = "host", "Host agent"

    class State(models.TextChoices):
        REQUESTED = "requested", "Requested"
        ACCEPTED = "accepted", "Accepted"
        RUNNING = "running", "Running"
        AWAITING_VERIFICATION = "awaiting_verification", "Awaiting verification"
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"
        ROLLING_BACK = "rolling_back", "Rolling back"
        ROLLED_BACK = "rolled_back", "Rolled back"
        ROLLBACK_FAILED = "rollback_failed", "Rollback failed"

    IN_FLIGHT_STATES = frozenset(
        {
            State.REQUESTED,
            State.ACCEPTED,
            State.RUNNING,
            State.AWAITING_VERIFICATION,
            State.ROLLING_BACK,
        }
    )

    job_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    operation = models.CharField(max_length=100)
    executor = models.CharField(max_length=8, choices=Executor.choices)
    state = models.CharField(max_length=32, choices=State.choices, default=State.REQUESTED)
    reason = models.CharField(max_length=64, blank=True)
    step = models.CharField(max_length=64, blank=True)
    in_flight = models.BooleanField(default=True)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="maintenance_jobs",
    )
    package = models.ForeignKey(
        MaintenancePackage,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="jobs",
    )
    # Identifiers and hashes only, never a name or a path: the row targets a
    # job, which no erasure path reaches. See AGENTS.md → Activity metadata.
    args = models.JSONField(default=dict, blank=True)
    # A default rather than auto_now_add, so a row re-created from the spool
    # after a restore keeps the time the request was made.
    created_at = models.DateTimeField(default=timezone.now)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    verify_deadline = models.DateTimeField(null=True, blank=True)
    verify_requested_at = models.DateTimeField(null=True, blank=True)
    rollback_requested_at = models.DateTimeField(null=True, blank=True)
    spool_updated_at = models.DateTimeField(null=True, blank=True)
    # Celery tier only: the command's captured output, tail-bounded by
    # MAINTENANCE_JOB_OUTPUT_LIMIT and masked out of the audit trail.
    output = models.TextField(blank=True)
    agent_version = models.CharField(max_length=32, blank=True)
    installed_version_before = models.CharField(max_length=32, blank=True)
    target_version = models.CharField(max_length=32, blank=True)
    running_version = models.CharField(max_length=32, blank=True)
    snapshot = models.CharField(max_length=128, blank=True)
    post_snapshot = models.CharField(max_length=128, blank=True)
    # Whether the update applied a migration; null until the agent has run
    # migrate. False is what lets a rollback keep the database.
    migrations_applied = models.BooleanField(null=True, blank=True)
    last_notified_state = models.CharField(max_length=32, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["in_flight"],
                condition=models.Q(in_flight=True),
                name="maintenance_one_job_in_flight",
            ),
        ]

    def __str__(self):
        return f"{self.operation} {self.job_id} ({self.state})"

    @property
    def is_terminal(self) -> bool:
        """Whether no executor will write to this job again."""
        return self.state not in self.IN_FLIGHT_STATES

    def transition(self, *, expect, actor=None, unset=(), **fields) -> bool:
        """Move to a new state only if the row is still in one of ``expect``.

        A compare-and-set ``update`` keyed on the current state, so two writers
        racing for the same row cannot both win, and ``in_flight`` is derived
        from the new state rather than trusted from the caller. ``unset`` names
        fields that must still be null for the update to apply, which is how the
        verify and rollback stamps exclude each other and themselves. Returns
        whether this call made the change. The bulk update fires no signal, so
        the audit row is recorded explicitly, with ``actor`` when a request made
        the change.
        """
        from activity.audit import record_modify_change, serialize_instance

        expect = {str(state) for state in expect}
        if "state" in fields:
            fields["in_flight"] = fields["state"] in self.IN_FLIGHT_STATES
        before = serialize_instance(self)
        guards = {f"{name}__isnull": True for name in unset}
        updated = type(self).objects.filter(pk=self.pk, state__in=expect, **guards).update(**fields)
        if not updated:
            return False
        for name, value in fields.items():
            setattr(self, name, value)
        record_modify_change(actor=actor, obj=self, before_state=before)
        return True

    def mark_finished(self, *, state: str, reason: str = "", actor=None, expect=None, **fields) -> bool:
        """Terminal transition with ``finished_at`` stamped, via :meth:`transition`."""
        return self.transition(
            expect=expect if expect is not None else self.IN_FLIGHT_STATES,
            actor=actor,
            state=state,
            reason=reason,
            finished_at=timezone.now(),
            **fields,
        )
