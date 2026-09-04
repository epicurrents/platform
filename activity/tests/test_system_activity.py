"""Tests for activity.system_activity — non-request audited scope.

Covers the contract that ``with_system_activity`` creates a parent
``Activity`` row with the correct ``interface``, flips the audit-context
ContextVars so nested signal-driven writes attribute correctly, and
restores the prior context state on exit (including nesting).
"""

import pytest

from activity.models import Activity, ObjectChangeLog
from activity.request_context import (
    current_activity,
    current_is_audited_context,
    current_user,
    get_current_activity,
    is_audited_context,
)
from activity.system_activity import with_system_activity


@pytest.mark.django_db
class TestActivityRow:
    def test_creates_row_with_celery_interface(self):
        with with_system_activity(
            "recordings.process",
            interface=Activity.Interface.CELERY,
        ) as activity:
            assert activity.pk is not None
            assert activity.interface == Activity.Interface.CELERY
            assert activity.verb == "recordings.process"
            assert activity.actor is None

    def test_creates_row_with_command_interface(self):
        with with_system_activity(
            "recordings.import",
            interface=Activity.Interface.COMMAND,
        ) as activity:
            assert activity.interface == Activity.Interface.COMMAND

    def test_rejects_unknown_interface(self):
        with pytest.raises(ValueError, match="unknown interface"):
            with with_system_activity("foo.bar", interface="not-a-real-iface"):
                pass

    def test_rejects_unsaved_target_instance(self):
        from recordings.models import Recording

        unsaved = Recording(original_name="x.edf")
        assert unsaved.pk is None
        with (
            pytest.raises(ValueError, match="target.pk is None"),
            with_system_activity(
                "recordings.process",
                interface=Activity.Interface.CELERY,
                target=unsaved,
            ),
        ):
            pass

    def test_db_error_degrades_gracefully(self, caplog):
        """A failure to insert the Activity row (rolling deploy with a
        missing column, transient DB outage) must not crash the wrapped
        task. The body still runs; the audit gap is logged."""
        from unittest import mock

        from django.db import DatabaseError

        with (
            mock.patch.object(
                Activity.objects,
                "create",
                side_effect=DatabaseError("simulated outage"),
            ),
            caplog.at_level("WARNING", logger="activity.system_activity"),
            with_system_activity(
                "recordings.process",
                interface=Activity.Interface.CELERY,
            ) as scope_value,
        ):
            # Body still executes; scope yields None.
            body_ran = True
            assert scope_value is None
        assert body_ran
        assert any("failed to create Activity row" in record.message for record in caplog.records)

    def test_accepts_actor_when_command_supplies_one(self, user):
        with with_system_activity(
            "recordings.import",
            interface=Activity.Interface.COMMAND,
            actor=user,
        ) as activity:
            assert activity.actor == user

    def test_target_is_recorded_when_supplied(self, user):
        from model_bakery import baker

        recording = baker.make("recordings.Recording", author=user)
        with with_system_activity(
            "recordings.process",
            interface=Activity.Interface.CELERY,
            target=recording,
        ) as activity:
            assert activity.target_object_id == str(recording.pk)
            assert activity.target_content_type is not None
            assert activity.target_content_type.model == "recording"

    def test_metadata_is_recorded(self):
        meta = {"recording_id": 42, "preserve_annotations": True}
        with with_system_activity(
            "recordings.process",
            interface=Activity.Interface.CELERY,
            metadata=meta,
        ) as activity:
            assert activity.metadata == meta


@pytest.mark.django_db
class TestContextVars:
    def test_audited_flag_is_true_inside_scope(self):
        assert is_audited_context() is False
        with with_system_activity(
            "recordings.process",
            interface=Activity.Interface.CELERY,
        ):
            assert is_audited_context() is True
        assert is_audited_context() is False

    def test_activity_contextvar_points_to_created_row(self):
        with with_system_activity(
            "recordings.process",
            interface=Activity.Interface.CELERY,
        ) as activity:
            assert get_current_activity() == activity

    def test_actor_contextvar_is_set_when_actor_supplied(self, user):
        with with_system_activity(
            "recordings.import",
            interface=Activity.Interface.COMMAND,
            actor=user,
        ):
            assert current_user.get() == user

    def test_context_is_restored_on_exit(self):
        # Pre-state: nothing set.
        assert current_activity.get() is None
        assert current_user.get() is None
        assert current_is_audited_context.get() is False
        with with_system_activity(
            "recordings.process",
            interface=Activity.Interface.CELERY,
        ):
            pass
        assert current_activity.get() is None
        assert current_user.get() is None
        assert current_is_audited_context.get() is False

    def test_context_is_restored_after_exception(self):
        try:
            with with_system_activity(
                "recordings.process",
                interface=Activity.Interface.CELERY,
            ):
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        assert current_activity.get() is None
        assert is_audited_context() is False

    def test_nested_scopes_restore_outer_state(self):
        with with_system_activity(
            "outer.op",
            interface=Activity.Interface.COMMAND,
        ) as outer:
            assert get_current_activity() == outer
            with with_system_activity(
                "inner.op",
                interface=Activity.Interface.CELERY,
            ) as inner:
                assert get_current_activity() == inner
            assert get_current_activity() == outer


@pytest.mark.django_db
class TestSignalAttribution:
    def test_signal_driven_writes_attach_to_parent_activity(self, user):
        from model_bakery import baker

        with with_system_activity(
            "recordings.process",
            interface=Activity.Interface.CELERY,
        ) as parent:
            recording = baker.make("recordings.Recording", author=user)

        change = ObjectChangeLog.objects.filter(
            object_id=str(recording.pk),
            action=ObjectChangeLog.ACTION_CREATE,
        ).first()
        assert change is not None, (
            "signal-driven create row missing — with_system_activity scope did not flip the audit context flag"
        )
        assert change.activity_id == parent.pk

    def test_no_signal_writes_outside_scope(self, user):
        from model_bakery import baker

        recording = baker.make("recordings.Recording", author=user)
        assert (
            ObjectChangeLog.objects.filter(
                object_id=str(recording.pk),
                action=ObjectChangeLog.ACTION_CREATE,
            ).count()
            == 0
        ), "create outside any audited scope should not produce ObjectChangeLog"
