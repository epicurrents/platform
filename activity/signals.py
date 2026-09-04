"""Auto-attribution layer for the audit trail.

⚠️ LOAD-BEARING — audit-trail auto-attribution.
``_track_sender`` is the gate that decides which ORM ``post_save`` /
``pre_delete`` signals produce ``ObjectChangeLog`` rows.  Any narrowing
here silently strips audit coverage from whatever model class falls out
of the gate; the failure mode mirrors the path-recognition bug in
``epicurrents/middleware.py``.  See AGENTS.md → *Load-bearing files*
before modifying; the contract test is ``activity/tests/test_audit.py``
(see also ``epicurrents/tests/test_middleware_audit_trail.py`` for the
end-to-end coverage path).
"""

from django.contrib.contenttypes.models import ContentType
from django.contrib.sessions.models import Session
from django.db.models.signals import post_save, pre_delete, pre_save
from django.dispatch import receiver
from django.utils import timezone

from .audit import (
    create_chained_change_log,
    diff_states,
    serialize_instance,
)
from .models import Activity, ObjectChangeLog
from .request_context import (
    get_current_activity,
    get_current_user,
    is_audited_context,
    is_change_logging_suppressed,
)

# Session rows are excluded because auditing them writes the session_key —
# a live bearer credential — into the permanent audit trail, turning every
# login into a session-hijack vector for anyone with audit-table or backup
# read access. Session lifecycle is infrastructure state, not user-data
# mutation; login events are already recorded via the `user.login` Activity
# verb. Contract test: activity/tests/test_erasure.py::TestSessionExclusion.
EXCLUDED_MODELS = {Activity, ObjectChangeLog, Session}


def _track_sender(sender) -> bool:
    """Return True when sender should be audited for destructive changes."""

    return is_audited_context() and not is_change_logging_suppressed() and sender not in EXCLUDED_MODELS


@receiver(pre_save)
def capture_previous_state(sender, instance, **kwargs):
    """Capture pre-save object state for later modify diff logging."""

    if not _track_sender(sender):
        return
    if not getattr(instance, "pk", None):
        return

    previous = sender._default_manager.filter(pk=instance.pk).first()
    if previous is None:
        return

    instance._audit_before_state = serialize_instance(previous)


@receiver(post_save)
def log_create_change(sender, instance, created, **kwargs):
    """Write ObjectChangeLog entries for API-driven object creations.

    Complements ``log_modify_change`` and ``log_delete_change`` so the full
    create/modify/delete lifecycle is covered by the auto-signal layer.
    Rolling back a create entry deletes the created object; ``rollback_change``
    in ``activity/audit.py`` handles this case.
    """

    if not _track_sender(sender):
        return
    if not created:
        return

    created_state = serialize_instance(instance)
    content_type = ContentType.objects.get_for_model(instance, for_concrete_model=False)
    performed_by = get_current_user()
    current_act = get_current_activity()
    now = timezone.now()
    create_chained_change_log(
        content_type=content_type,
        object_id=str(instance.pk),
        action=ObjectChangeLog.ACTION_CREATE,
        performed_by=performed_by,
        before_state=created_state,
        changes=None,
        hash_payload=created_state,
        timestamp=now,
        activity=current_act,
        project=current_act.project if current_act is not None else "",
    )


@receiver(post_save)
def log_modify_change(sender, instance, created, **kwargs):
    """Write ObjectChangeLog entries for API-driven object modifications."""

    if not _track_sender(sender):
        return
    if created:
        return

    # Check that pre_save managed to capture a previous state to diff against.
    before_state = getattr(instance, "_audit_before_state", None)
    if not before_state:
        return

    after_state = serialize_instance(instance)
    changes = diff_states(before_state, after_state)
    if not changes:
        return

    content_type = ContentType.objects.get_for_model(instance, for_concrete_model=False)
    performed_by = get_current_user()
    current_act = get_current_activity()
    now = timezone.now()
    create_chained_change_log(
        content_type=content_type,
        object_id=str(instance.pk),
        action=ObjectChangeLog.ACTION_MODIFY,
        performed_by=performed_by,
        before_state=before_state,
        changes=changes,
        hash_payload=after_state,
        timestamp=now,
        activity=current_act,
        project=current_act.project if current_act is not None else "",
    )


@receiver(pre_delete)
def log_delete_change(sender, instance, **kwargs):
    """Write ObjectChangeLog entries for API-driven object deletions."""

    if not _track_sender(sender):
        return

    before_state = serialize_instance(instance)
    content_type = ContentType.objects.get_for_model(instance, for_concrete_model=False)
    performed_by = get_current_user()
    current_act = get_current_activity()
    now = timezone.now()
    create_chained_change_log(
        content_type=content_type,
        object_id=str(instance.pk),
        action=ObjectChangeLog.ACTION_DELETE,
        performed_by=performed_by,
        before_state=before_state,
        changes=None,
        hash_payload={},
        timestamp=now,
        activity=current_act,
        project=current_act.project if current_act is not None else "",
    )
