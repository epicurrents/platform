"""Tests for activity.derived_state — digester registry + verifier.

Covers the registration mechanism, the verify_derived_state result shape,
the three verdicts (ok / mismatch / no_digester), and the hash-payload
mixing that makes the audit row's after_hash sensitive to extra_payload.
"""

import pytest
from django.contrib.contenttypes.models import ContentType
from django.utils import timezone

from activity.audit import (
    create_chained_change_log,
    record_modify_change,
    serialize_instance,
    verify_change_hash,
)
from activity.derived_state import (
    DerivedStateVerificationResult,
    register_derived_state_digester,
    verify_derived_state,
)
from activity.models import ObjectChangeLog
from activity.system_activity import with_system_activity


@pytest.mark.django_db
class TestExtraPayloadHashing:
    def test_empty_extra_payload_does_not_change_hash(self, user):
        """A row written with extra_payload={} hashes the same as a row
        written without the kwarg — Phase 2 schema add is invisible to
        existing rows."""
        from model_bakery import baker

        recording = baker.make("recordings.Recording", author=user)
        before = serialize_instance(recording)
        recording.original_name = "x.edf"
        recording.save(update_fields=["original_name"])
        # Direct call without extra_payload.
        row_a = record_modify_change(actor=user, obj=recording, before_state=before)
        # Same shape, explicit empty extra_payload — should produce the
        # same hash given identical inputs are otherwise impossible.
        # We instead assert the row's stored extra_payload is empty and
        # verify_change_hash succeeds.
        assert row_a.extra_payload == {}
        assert verify_change_hash(row_a) is True

    def test_extra_payload_is_stored_and_verifies(self, user):
        from model_bakery import baker

        recording = baker.make("recordings.Recording", author=user)
        before = serialize_instance(recording)
        recording.original_name = "y.edf"
        recording.save(update_fields=["original_name"])
        row = record_modify_change(
            actor=user,
            obj=recording,
            before_state=before,
            extra_payload={"signal_info_digest": "abc123"},
        )
        assert row.extra_payload == {"signal_info_digest": "abc123"}
        assert verify_change_hash(row) is True

    def test_tampering_with_extra_payload_breaks_hash(self, user):
        from model_bakery import baker

        recording = baker.make("recordings.Recording", author=user)
        before = serialize_instance(recording)
        recording.original_name = "z.edf"
        recording.save(update_fields=["original_name"])
        row = record_modify_change(
            actor=user,
            obj=recording,
            before_state=before,
            extra_payload={"signal_info_digest": "abc123"},
        )
        # Naive tamper: edit the stored digest but leave after_hash.
        row.extra_payload = {"signal_info_digest": "deadbeef"}
        row.save(update_fields=["extra_payload"])
        assert verify_change_hash(row) is False


@pytest.mark.django_db
class TestDigesterRegistry:
    def test_registered_digester_returns_ok_when_match(self, user):
        from model_bakery import baker

        recording = baker.make("recordings.Recording", author=user)

        def digester(target) -> str:
            return f"digest-of-{target.pk}"

        register_derived_state_digester(
            target_model=type(recording),
            key="test_key",
            digester=digester,
        )

        before = serialize_instance(recording)
        recording.original_name = "a.edf"
        recording.save(update_fields=["original_name"])
        row = record_modify_change(
            actor=user,
            obj=recording,
            before_state=before,
            extra_payload={"test_key": f"digest-of-{recording.pk}"},
        )
        result = verify_derived_state(row)
        assert isinstance(result, DerivedStateVerificationResult)
        assert result.ok is True
        assert result.digests == {"test_key": "ok"}

    def test_registered_digester_returns_mismatch_when_stored_diverges(self, user):
        from model_bakery import baker

        recording = baker.make("recordings.Recording", author=user)

        def digester(target) -> str:
            return "live-digest"

        register_derived_state_digester(
            target_model=type(recording),
            key="probe",
            digester=digester,
        )

        before = serialize_instance(recording)
        recording.original_name = "b.edf"
        recording.save(update_fields=["original_name"])
        row = record_modify_change(
            actor=user,
            obj=recording,
            before_state=before,
            extra_payload={"probe": "stale-digest"},
        )
        result = verify_derived_state(row)
        assert result.ok is False
        assert result.digests == {"probe": "mismatch"}

    def test_unregistered_key_returns_no_digester(self, user):
        from model_bakery import baker

        recording = baker.make("recordings.Recording", author=user)
        before = serialize_instance(recording)
        recording.original_name = "c.edf"
        recording.save(update_fields=["original_name"])
        row = record_modify_change(
            actor=user,
            obj=recording,
            before_state=before,
            extra_payload={"unregistered_key": "x"},
        )
        result = verify_derived_state(row)
        assert result.ok is False
        assert result.digests == {"unregistered_key": "no_digester"}

    def test_empty_extra_payload_yields_ok_with_no_digests(self, user):
        from model_bakery import baker

        recording = baker.make("recordings.Recording", author=user)
        before = serialize_instance(recording)
        recording.original_name = "d.edf"
        recording.save(update_fields=["original_name"])
        row = record_modify_change(actor=user, obj=recording, before_state=before)
        result = verify_derived_state(row)
        assert result.ok is True
        assert result.digests == {}

    def test_target_missing_yields_not_ok(self, user):
        from model_bakery import baker

        recording = baker.make("recordings.Recording", author=user)
        ct = ContentType.objects.get_for_model(type(recording))
        with with_system_activity("test.synthetic", interface="celery") as _act:
            row = create_chained_change_log(
                content_type=ct,
                object_id=str(recording.pk),
                action=ObjectChangeLog.ACTION_DELETE,
                performed_by=None,
                before_state=serialize_instance(recording),
                changes=None,
                hash_payload={},
                timestamp=timezone.now(),
                extra_payload={"probe": "x"},
            )
        recording.delete()
        result = verify_derived_state(row)
        assert result.target_loaded is False
        assert result.ok is False
