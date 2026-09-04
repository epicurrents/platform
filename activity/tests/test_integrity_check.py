"""Tests for activity.integrity_check.run_integrity_check.

Covers the four anomaly classes the chain phase detects (content
tamper, gap, genesis lift, missing key) plus the derived-state phase
mismatch / no_digester verdicts. Also asserts the summary shape and
the clean-run path.
"""

import pytest
from django.contrib.contenttypes.models import ContentType
from django.utils import timezone

from activity.audit import (
    create_chained_change_log,
    genesis_sentinel,
    record_modify_change,
)
from activity.derived_state import register_derived_state_digester
from activity.integrity_check import run_integrity_check
from activity.models import ObjectChangeLog


def _ct(instance):
    return ContentType.objects.get_for_model(instance, for_concrete_model=False)


def _make_v3_row(*, recording, before_state=None, changes=None, hash_payload=None):
    """Write one v3 chain row against the recording's content_type."""
    return create_chained_change_log(
        content_type=_ct(recording),
        object_id=str(recording.pk),
        action=ObjectChangeLog.ACTION_MODIFY,
        performed_by=None,
        before_state=before_state or {"x": 0},
        changes=changes or {"x": {"from": 0, "to": 1}},
        hash_payload=hash_payload or {"x": 1},
        timestamp=timezone.now(),
    )


@pytest.fixture
def keyed_settings(settings):
    """Configure a v3 HMAC key so writes go through the chain path."""
    settings.ACTIVITY_HASH_KEYS = {1: b"k" * 32}
    settings.ACTIVITY_HASH_KEY_CURRENT = 1
    return settings


def _security_events(caplog, event_type):
    return [rec for rec in caplog.records if getattr(rec, "security_event_type", None) == event_type]


@pytest.mark.django_db
class TestRunIntegrityCheckCleanRun:
    def test_clean_chain_produces_no_events(self, user, keyed_settings, caplog):
        from model_bakery import baker

        recording = baker.make("recordings.Recording", author=user)
        for _ in range(3):
            _make_v3_row(recording=recording)

        with caplog.at_level("WARNING", logger="epicurrents.security"):
            summary = run_integrity_check(derived_window_days=0)

        assert summary["chain_breaks"] == 0
        assert summary["chain_gaps"] == []
        assert summary["genesis_invalid"] == 0
        assert summary["key_missing"] == 0
        assert summary["chains_checked"] >= 1
        assert summary["chain_rows_checked"] >= 3
        assert not any(getattr(r, "security_event_type", "").startswith("audit.") for r in caplog.records)

    def test_summary_shape_has_all_keys(self, user, keyed_settings):
        from model_bakery import baker

        baker.make("recordings.Recording", author=user)
        summary = run_integrity_check(derived_window_days=0)
        assert set(summary.keys()) == {
            "chains_checked",
            "chain_rows_checked",
            "chain_breaks",
            "chain_gaps",
            "genesis_invalid",
            "key_missing",
            "derived_rows_checked",
            "derived_mismatches",
            "derived_no_digester",
        }


@pytest.mark.django_db
class TestChainAnomalies:
    def test_content_tamper_emits_chain_break(self, user, keyed_settings, caplog):
        from model_bakery import baker

        recording = baker.make("recordings.Recording", author=user)
        row = _make_v3_row(recording=recording)
        # Naive tamper: edit before_state, leave after_hash. verify_chain
        # recomputes and reports the row as broken.
        row.before_state = {"tampered": True}
        row.save(update_fields=["before_state"])

        with caplog.at_level("WARNING", logger="epicurrents.security"):
            summary = run_integrity_check(derived_window_days=0)

        assert summary["chain_breaks"] >= 1
        events = _security_events(caplog, "audit.chain_break")
        assert len(events) == 1

    def test_gap_emits_chain_gap_event(self, user, keyed_settings, caplog):
        from model_bakery import baker

        recording = baker.make("recordings.Recording", author=user)
        rows = [_make_v3_row(recording=recording) for _ in range(3)]
        # Delete the middle row — sequence_no=2 disappears.
        rows[1].delete()

        with caplog.at_level("WARNING", logger="epicurrents.security"):
            summary = run_integrity_check(derived_window_days=0)

        assert summary["chain_gaps"] == [rows[1].sequence_no]
        events = _security_events(caplog, "audit.chain_gap")
        assert len(events) == 1
        assert events[0].missing_sequence_nos == [rows[1].sequence_no]

    def test_genesis_lift_emits_event(self, user, keyed_settings, caplog):
        from model_bakery import baker

        recording = baker.make("recordings.Recording", author=user)
        row = _make_v3_row(recording=recording)
        # Replace the first row's prev_hash with a different shard's
        # sentinel.
        other_ct_id = ContentType.objects.get_for_model(ObjectChangeLog).pk
        row.prev_hash = genesis_sentinel(other_ct_id)
        row.save(update_fields=["prev_hash"])

        with caplog.at_level("WARNING", logger="epicurrents.security"):
            summary = run_integrity_check(derived_window_days=0)

        # Genesis mismatch also breaks the row's hash (prev_hash is in
        # the HMAC payload) — both events fire.
        assert summary["genesis_invalid"] >= 1
        assert _security_events(caplog, "audit.genesis_invalid")

    def test_missing_hash_key_emits_key_missing(self, user, keyed_settings, caplog):
        from model_bakery import baker

        recording = baker.make("recordings.Recording", author=user)
        _make_v3_row(recording=recording)
        # Clear the keys after the row was written so verification can't
        # recompute the HMAC.
        keyed_settings.ACTIVITY_HASH_KEYS = {}

        with caplog.at_level("WARNING", logger="epicurrents.security"):
            summary = run_integrity_check(derived_window_days=0)

        assert summary["key_missing"] >= 1
        events = _security_events(caplog, "audit.hash_key_missing")
        assert len(events) >= 1


@pytest.mark.django_db
class TestDerivedStatePhase:
    def test_mismatch_emits_derived_state_event(self, user, keyed_settings, caplog):
        from model_bakery import baker

        from activity.audit import serialize_instance

        recording = baker.make("recordings.Recording", author=user)

        def digester(target):
            return "live-digest"

        register_derived_state_digester(
            target_model=type(recording),
            key="test_key",
            digester=digester,
        )
        # Write a row whose stored digest diverges from the live recompute.
        before = serialize_instance(recording)
        recording.original_name = "renamed.edf"
        recording.save(update_fields=["original_name"])
        record_modify_change(
            actor=user,
            obj=recording,
            before_state=before,
            extra_payload={"test_key": "stale-digest"},
        )

        with caplog.at_level("WARNING", logger="epicurrents.security"):
            summary = run_integrity_check()

        assert summary["derived_mismatches"] >= 1
        events = _security_events(caplog, "audit.derived_state_mismatch")
        assert len(events) == 1
        assert events[0].digest_key == "test_key"

    def test_no_digester_emits_event(self, user, keyed_settings, caplog):
        from model_bakery import baker

        from activity.audit import serialize_instance

        recording = baker.make("recordings.Recording", author=user)
        before = serialize_instance(recording)
        recording.original_name = "x.edf"
        recording.save(update_fields=["original_name"])
        record_modify_change(
            actor=user,
            obj=recording,
            before_state=before,
            extra_payload={"unregistered_key_for_test": "abc"},
        )

        with caplog.at_level("WARNING", logger="epicurrents.security"):
            summary = run_integrity_check()

        assert summary["derived_no_digester"] >= 1
        events = _security_events(caplog, "audit.derived_state_no_digester")
        assert events

    def test_window_zero_skips_derived_phase(self, user, keyed_settings, caplog):
        from model_bakery import baker

        from activity.audit import serialize_instance

        recording = baker.make("recordings.Recording", author=user)
        before = serialize_instance(recording)
        recording.original_name = "y.edf"
        recording.save(update_fields=["original_name"])
        record_modify_change(
            actor=user,
            obj=recording,
            before_state=before,
            extra_payload={"unregistered_key_for_test": "abc"},
        )

        with caplog.at_level("WARNING", logger="epicurrents.security"):
            summary = run_integrity_check(derived_window_days=0)

        assert summary["derived_rows_checked"] == 0
        assert summary["derived_mismatches"] == 0
        assert not _security_events(caplog, "audit.derived_state_mismatch")
