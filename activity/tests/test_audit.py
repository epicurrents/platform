"""Tests for activity.audit — serialization, diffing, hashing, and rollback."""

import itertools

import pytest
from django.contrib.contenttypes.models import ContentType
from django.utils import timezone

from activity.audit import (
    ActivityHashKeyMissing,
    ChangeHashMismatch,
    compute_audit_hash,
    create_chained_change_log,
    current_write_hash_version,
    diff_states,
    genesis_sentinel,
    hash_payload_state,
    rollback_change,
    serialize_instance,
    verify_chain,
    verify_change_hash,
)
from activity.models import ObjectChangeLog


@pytest.mark.django_db
class TestSerializeInstance:
    def test_returns_dict_of_concrete_fields(self, user):
        from model_bakery import baker

        recording = baker.make("recordings.Recording", author=user, original_name="test.edf")
        data = serialize_instance(recording)
        assert isinstance(data, dict)
        # original_name is a registered masked field (patient-side PHI) —
        # the serialized state carries the opaque digest, never the raw name.
        assert data["original_name"].startswith("<masked:")
        assert "test.edf" not in str(data)
        assert "id" in data
        assert "author_id" in data

    def test_datetime_fields_serialized_as_isoformat(self, user):
        from model_bakery import baker

        recording = baker.make("recordings.Recording", author=user)
        data = serialize_instance(recording)
        # created_at is a datetime — should be a string after serialization
        assert isinstance(data["created_at"], str)

    def test_binary_fields_serialized_as_length_sentinel(self):
        import json

        from compute.models import LeadFieldCache

        instance = LeadFieldCache(
            montage_name="standard_1020",
            n_orient=1,
            n_channels=4,
            n_sources=3,
            grid_resolution_mm=7.5,
            sphere_radius_m=0.09,
            sphere_center_m=[0.0, 0.0, 0.04],
            channel_names=["A", "B", "C", "D"],
            lead_field=b"\x00\x01" * 16,
            src_pos=b"\xff" * 24,
        )
        data = serialize_instance(instance)
        assert data["lead_field"] == "<bytes:len=32>"
        assert data["src_pos"] == "<bytes:len=24>"
        # The whole row round-trips through json.dumps without raising.
        json.dumps(data)


class TestDiffStates:
    def test_detects_changed_field(self):
        before = {"name": "old", "size": 100}
        after = {"name": "new", "size": 100}
        changes = diff_states(before, after)
        assert "name" in changes
        assert changes["name"] == {"from": "old", "to": "new"}

    def test_unchanged_fields_omitted(self):
        state = {"name": "same", "size": 100}
        assert diff_states(state, state) == {}

    def test_added_field_appears_in_diff(self):
        changes = diff_states({"a": 1}, {"a": 1, "b": 2})
        assert "b" in changes
        assert changes["b"] == {"from": None, "to": 2}

    def test_removed_field_appears_in_diff(self):
        changes = diff_states({"a": 1, "b": 2}, {"a": 1})
        assert "b" in changes
        assert changes["b"] == {"from": 2, "to": None}


class TestComputeAuditHash:
    _kwargs = {
        "performed_by_id": 1,
        "content_type_id": 2,
        "object_id": "42",
        "action": "modify",
        "timestamp": "2026-01-01T00:00:00+00:00",
    }

    def test_returns_32_character_string(self):
        h = compute_audit_hash({"key": "value"}, **self._kwargs)
        assert isinstance(h, str)
        assert len(h) == 32

    def test_same_inputs_produce_same_hash(self):
        h1 = compute_audit_hash({"key": "value"}, **self._kwargs)
        h2 = compute_audit_hash({"key": "value"}, **self._kwargs)
        assert h1 == h2

    def test_different_state_produces_different_hash(self):
        h1 = compute_audit_hash({"key": "value"}, **self._kwargs)
        h2 = compute_audit_hash({"key": "other"}, **self._kwargs)
        assert h1 != h2

    def test_different_actor_produces_different_hash(self):
        h1 = compute_audit_hash({}, **self._kwargs)
        h2 = compute_audit_hash({}, **{**self._kwargs, "performed_by_id": 99})
        assert h1 != h2

    def test_different_timestamp_produces_different_hash(self):
        h1 = compute_audit_hash({}, **self._kwargs)
        h2 = compute_audit_hash({}, **{**self._kwargs, "timestamp": "2026-06-01T00:00:00+00:00"})
        assert h1 != h2

    def test_different_action_produces_different_hash(self):
        h1 = compute_audit_hash({}, **self._kwargs)
        h2 = compute_audit_hash({}, **{**self._kwargs, "action": "delete"})
        assert h1 != h2

    def test_explicit_v1_matches_default(self):
        """The dispatcher defaults to v1; passing it explicitly produces
        the same hash. Pins the seam so additional algorithm versions can
        be added without retroactively shifting v1 hashes."""
        h_default = compute_audit_hash({"k": "v"}, **self._kwargs)
        h_v1 = compute_audit_hash({"k": "v"}, **self._kwargs, algorithm="v1")
        assert h_default == h_v1

    def test_unknown_algorithm_raises(self):
        """Unknown algorithm names raise — defensive against typos and
        future-format rows being read by old code that doesn't know the
        new version yet."""
        with pytest.raises(ValueError, match="Unknown audit-hash algorithm"):
            compute_audit_hash({}, **self._kwargs, algorithm="v999")

    def test_v2_requires_hash_key_version(self):
        """v2 is HMAC-keyed; the dispatcher refuses to silently pick a key
        when the caller forgot to name one."""
        with pytest.raises(ValueError, match="hash_key_version is required"):
            compute_audit_hash({}, **self._kwargs, algorithm="v2")

    def test_v2_hash_differs_from_v1_for_same_payload(self, settings):
        """The keyed algorithm produces a different hash than the unkeyed
        one for the same payload — the algorithm-version field on rows is
        load-bearing because the bytes diverge by key only, not by content."""
        settings.ACTIVITY_HASH_KEYS = {1: b"k" * 32}
        h_v1 = compute_audit_hash({"x": 1}, **self._kwargs, algorithm="v1")
        h_v2 = compute_audit_hash({"x": 1}, **self._kwargs, algorithm="v2", hash_key_version=1)
        assert h_v1 != h_v2

    def test_v2_hash_differs_when_key_changes(self, settings):
        """Different keys at the same version slot produce different hashes
        — pins the forgery-resistance property the keyed algorithm exists
        to provide."""
        settings.ACTIVITY_HASH_KEYS = {1: b"k" * 32}
        h_key_a = compute_audit_hash({"x": 1}, **self._kwargs, algorithm="v2", hash_key_version=1)
        settings.ACTIVITY_HASH_KEYS = {1: b"different-key-bytes-32-chars-x"}
        h_key_b = compute_audit_hash({"x": 1}, **self._kwargs, algorithm="v2", hash_key_version=1)
        assert h_key_a != h_key_b

    def test_v2_missing_key_raises(self, settings):
        """A row references a key version, the version isn't in settings →
        ActivityHashKeyMissing. Distinct from ChangeHashMismatch so the
        operator can tell a configuration error apart from a tamper signal."""
        settings.ACTIVITY_HASH_KEYS = {1: b"k" * 32}
        with pytest.raises(ActivityHashKeyMissing, match="ACTIVITY_HASH_KEY_V99"):
            compute_audit_hash({}, **self._kwargs, algorithm="v2", hash_key_version=99)

    def test_current_write_hash_version_uses_settings(self, settings):
        """The write-side helper reads ACTIVITY_HASH_KEY_CURRENT and returns
        the corresponding (algorithm, version) pair. Signal handlers use
        this to write into the new hash_algorithm / hash_key_version row
        fields without each callsite reaching into settings itself. With a
        key configured the algorithm is v3 (HMAC + chain); the unkeyed
        fallback returns v1 / None."""
        settings.ACTIVITY_HASH_KEYS = {1: b"k" * 32, 2: b"q" * 32}
        settings.ACTIVITY_HASH_KEY_CURRENT = 2
        assert current_write_hash_version() == ("v3", 2)

    def test_current_write_hash_version_falls_back_to_v1_without_keys(self, settings):
        """No keys configured (dev mode without init_env, or production
        misconfig the apps.py boot guard catches) → v1 / None. The audit
        trail keeps recording; production refuses to boot, dev silently
        runs on the legacy algorithm."""
        settings.ACTIVITY_HASH_KEYS = {}
        assert current_write_hash_version() == ("v1", None)


@pytest.mark.django_db
class TestHashTamperDetection:
    """Initial integrity-hash tests.

    These cover what the current ``compute_audit_hash`` (v1) is meant to
    guarantee: that a recomputed hash from a freshly-loaded row matches the
    stored value when no tampering has occurred, and diverges if any field-
    level value or metadata changes. They do **not** test resistance against
    an attacker who can run ``compute_audit_hash`` themselves — that
    requires the HMAC / chain hardening tracked in the ROADMAP entry
    "Activity — strengthen ObjectChangeLog integrity hash beyond
    fingerprinting".
    """

    def test_hash_round_trips_through_jsonfield(self, user):
        """Recomputing the hash from a freshly-loaded row reproduces the stored value.

        This is the JSON round-trip stability guarantee documented in
        ``_json_safe`` — values like Decimal / datetime are pre-converted to
        canonical strings so that a state serialized → stored → reloaded
        produces the same hash as the original in-memory state.
        """
        from model_bakery import baker

        recording = baker.make("recordings.Recording", author=user)
        state = serialize_instance(recording)
        ct = ContentType.objects.get_for_model(recording, for_concrete_model=False)
        now = timezone.now()
        change = ObjectChangeLog.objects.create(
            content_type=ct,
            object_id=str(recording.pk),
            action=ObjectChangeLog.ACTION_MODIFY,
            performed_by=user,
            before_state=state,
            changes=None,
            after_hash=compute_audit_hash(
                state,
                performed_by_id=user.pk,
                content_type_id=ct.pk,
                object_id=str(recording.pk),
                action=ObjectChangeLog.ACTION_MODIFY,
                timestamp=now.isoformat(),
            ),
            created_at=now,
        )

        reloaded = ObjectChangeLog.objects.get(pk=change.pk)
        recomputed = compute_audit_hash(
            reloaded.before_state,
            performed_by_id=reloaded.performed_by_id,
            content_type_id=reloaded.content_type_id,
            object_id=reloaded.object_id,
            action=reloaded.action,
            timestamp=reloaded.created_at.isoformat(),
        )
        assert recomputed == reloaded.after_hash

    def test_tampered_before_state_yields_different_hash(self, user):
        """Editing stored before_state in the DB makes the recomputed hash diverge.

        Demonstrates that the hash detects content tampering by an actor who
        does not also recompute the hash — the limitation being that an actor
        with code-level access can recompute, which is what the ROADMAP
        hardening item addresses.
        """
        from model_bakery import baker

        recording = baker.make("recordings.Recording", author=user)
        state = serialize_instance(recording)
        ct = ContentType.objects.get_for_model(recording, for_concrete_model=False)
        now = timezone.now()
        change = ObjectChangeLog.objects.create(
            content_type=ct,
            object_id=str(recording.pk),
            action=ObjectChangeLog.ACTION_MODIFY,
            performed_by=user,
            before_state=state,
            changes=None,
            after_hash=compute_audit_hash(
                state,
                performed_by_id=user.pk,
                content_type_id=ct.pk,
                object_id=str(recording.pk),
                action=ObjectChangeLog.ACTION_MODIFY,
                timestamp=now.isoformat(),
            ),
            created_at=now,
        )

        tampered = dict(change.before_state)
        tampered["original_name"] = "tampered.edf"
        ObjectChangeLog.objects.filter(pk=change.pk).update(before_state=tampered)

        reloaded = ObjectChangeLog.objects.get(pk=change.pk)
        recomputed = compute_audit_hash(
            reloaded.before_state,
            performed_by_id=reloaded.performed_by_id,
            content_type_id=reloaded.content_type_id,
            object_id=reloaded.object_id,
            action=reloaded.action,
            timestamp=reloaded.created_at.isoformat(),
        )
        assert recomputed != reloaded.after_hash


@pytest.mark.django_db
class TestVerifyChangeHash:
    """Contract for ``verify_change_hash`` — the read-side integrity gate.

    The helper is pure: it recomputes the row's hash and returns the bool. No
    side effects, no logging. Consumers (rollback path, changelog read API,
    future periodic sweep) decide what to do on False.
    """

    def _make_row(
        self,
        user,
        action,
        before_state,
        changes,
        algorithm="v1",
        hash_key_version=None,
    ):
        from model_bakery import baker

        recording = baker.make("recordings.Recording", author=user)
        ct = ContentType.objects.get_for_model(recording, for_concrete_model=False)
        now = timezone.now()
        return ObjectChangeLog.objects.create(
            content_type=ct,
            object_id=str(recording.pk),
            action=action,
            performed_by=user,
            before_state=before_state,
            changes=changes,
            hash_algorithm=algorithm,
            hash_key_version=hash_key_version,
            after_hash=compute_audit_hash(
                hash_payload_state(action, before_state, changes),
                performed_by_id=user.pk,
                content_type_id=ct.pk,
                object_id=str(recording.pk),
                action=action,
                timestamp=now.isoformat(),
                algorithm=algorithm,
                hash_key_version=hash_key_version,
            ),
            created_at=now,
        )

    def test_returns_true_for_untampered_create(self, user):
        row = self._make_row(user, ObjectChangeLog.ACTION_CREATE, {"name": "v1"}, None)
        assert verify_change_hash(row) is True

    def test_returns_true_for_untampered_modify(self, user):
        row = self._make_row(
            user,
            ObjectChangeLog.ACTION_MODIFY,
            {"name": "v1"},
            {"name": {"from": "v1", "to": "v2"}},
        )
        assert verify_change_hash(row) is True

    def test_returns_true_for_untampered_delete(self, user):
        row = self._make_row(user, ObjectChangeLog.ACTION_DELETE, {"name": "v1"}, None)
        assert verify_change_hash(row) is True

    def test_returns_false_when_before_state_edited(self, user):
        row = self._make_row(user, ObjectChangeLog.ACTION_CREATE, {"name": "v1"}, None)
        row.before_state = {"name": "tampered"}
        row.save(update_fields=["before_state"])
        assert verify_change_hash(row) is False

    def test_returns_false_when_changes_edited(self, user):
        row = self._make_row(
            user,
            ObjectChangeLog.ACTION_MODIFY,
            {"name": "v1"},
            {"name": {"from": "v1", "to": "v2"}},
        )
        row.changes = {"name": {"from": "v1", "to": "INJECTED"}}
        row.save(update_fields=["changes"])
        assert verify_change_hash(row) is False

    def test_returns_false_when_action_edited(self, user):
        row = self._make_row(user, ObjectChangeLog.ACTION_CREATE, {"name": "v1"}, None)
        # Re-label CREATE as MODIFY without recomputing the hash — the
        # reconstruction path branches on action, so the hash payload
        # diverges and verification fails.
        row.action = ObjectChangeLog.ACTION_MODIFY
        row.save(update_fields=["action"])
        assert verify_change_hash(row) is False

    def test_returns_false_when_after_hash_edited(self, user):
        row = self._make_row(user, ObjectChangeLog.ACTION_CREATE, {"name": "v1"}, None)
        row.after_hash = "00000000000000000000000000000000"
        row.save(update_fields=["after_hash"])
        assert verify_change_hash(row) is False

    def test_returns_true_for_untampered_v2_row(self, user, settings):
        """A row written under v2 (HMAC + key version 1) verifies under
        the same algorithm + key. Pins the dispatcher-by-row-field path."""
        settings.ACTIVITY_HASH_KEYS = {1: b"k" * 32}
        row = self._make_row(
            user,
            ObjectChangeLog.ACTION_CREATE,
            {"name": "v1"},
            None,
            algorithm="v2",
            hash_key_version=1,
        )
        assert row.hash_algorithm == "v2"
        assert row.hash_key_version == 1
        assert verify_change_hash(row) is True

    def test_returns_false_for_tampered_v2_row(self, user, settings):
        settings.ACTIVITY_HASH_KEYS = {1: b"k" * 32}
        row = self._make_row(
            user,
            ObjectChangeLog.ACTION_CREATE,
            {"name": "v1"},
            None,
            algorithm="v2",
            hash_key_version=1,
        )
        row.before_state = {"name": "tampered"}
        row.save(update_fields=["before_state"])
        assert verify_change_hash(row) is False

    def test_v2_row_with_missing_key_raises(self, user, settings):
        """A row written under key version 1 then read back after the
        operator dropped that key from settings → ActivityHashKeyMissing.
        Distinct from ChangeHashMismatch because the row may not be
        tampered at all; the key just isn't reachable."""
        settings.ACTIVITY_HASH_KEYS = {1: b"k" * 32}
        row = self._make_row(
            user,
            ObjectChangeLog.ACTION_CREATE,
            {"name": "v1"},
            None,
            algorithm="v2",
            hash_key_version=1,
        )
        # Operator decommissioned key v1.
        settings.ACTIVITY_HASH_KEYS = {}
        with pytest.raises(ActivityHashKeyMissing):
            verify_change_hash(row)

    def test_v2_row_verifies_independently_of_current_setting(self, user, settings):
        """Rolling the current write version forward to v2 → v3 (or any
        future bump) must not invalidate already-written v2 rows so long
        as their key version is still present. The row's stored
        ``hash_key_version`` is what verification keys off, not the
        current setting."""
        settings.ACTIVITY_HASH_KEYS = {1: b"k" * 32, 2: b"q" * 32}
        settings.ACTIVITY_HASH_KEY_CURRENT = 1
        row = self._make_row(
            user,
            ObjectChangeLog.ACTION_CREATE,
            {"name": "v1"},
            None,
            algorithm="v2",
            hash_key_version=1,
        )
        # Operator rotates to key version 2.
        settings.ACTIVITY_HASH_KEY_CURRENT = 2
        assert verify_change_hash(row) is True


@pytest.mark.django_db
class TestRollbackVerifiesHash:
    """Contract: rollback refuses tampered rows.

    The rollback path is the highest-value consumer of ``verify_change_hash``
    — applying ``before_state`` from a tampered row would silently restore
    the attacker's chosen state. The contract pinned here is "tampered row
    → ChangeHashMismatch, no restore applied, security event emitted".
    """

    def test_rollback_raises_on_tampered_before_state(self, user, caplog):
        import logging

        from model_bakery import baker

        recording = baker.make("recordings.Recording", author=user, original_name="current.edf")
        before = serialize_instance(recording)
        before["original_name"] = "would-restore-to.edf"
        ct = ContentType.objects.get_for_model(recording, for_concrete_model=False)
        now = timezone.now()
        changes = {
            "original_name": {
                "from": before["original_name"],
                "to": "current.edf",
            }
        }
        change = ObjectChangeLog.objects.create(
            content_type=ct,
            object_id=str(recording.pk),
            action=ObjectChangeLog.ACTION_MODIFY,
            performed_by=user,
            before_state=before,
            changes=changes,
            after_hash=compute_audit_hash(
                hash_payload_state(ObjectChangeLog.ACTION_MODIFY, before, changes),
                performed_by_id=user.pk,
                content_type_id=ct.pk,
                object_id=str(recording.pk),
                action=ObjectChangeLog.ACTION_MODIFY,
                timestamp=now.isoformat(),
            ),
            created_at=now,
        )

        # Tamper: inject a phantom field into before_state without
        # recomputing the hash. The reconstruction copies before_state and
        # then overlays changes' ``to`` values — the phantom field is not
        # in changes, so it survives to alter the recomputed payload.
        # (Tampering original_name itself would be invisible because
        # changes[original_name]["to"] overwrites it during reconstruction.)
        change.before_state = {**before, "injected_field": "attacker"}
        change.save(update_fields=["before_state"])

        with caplog.at_level(logging.WARNING, logger="epicurrents.security"), pytest.raises(ChangeHashMismatch):
            rollback_change(user=user, change_id=change.pk)

        recording.refresh_from_db()
        assert recording.original_name == "current.edf"  # untouched

        events = [
            r for r in caplog.records if r.__dict__.get("security_event_type") == "audit.hash_verification_failed"
        ]
        assert len(events) == 1
        assert events[0].__dict__["change_id"] == change.pk


@pytest.mark.django_db
class TestLogActivity:
    """Contract for the ``log_activity`` helper.

    The helper is the canonical replacement for the 12-line
    ``get_current_activity / set fields / save(update_fields=...)``
    boilerplate every API endpoint used to repeat. These tests pin the
    behaviour so a "refactor" that silently drops one of the field
    writes gets caught.
    """

    def _make_activity_with_context(self):
        """Create an Activity row and wire it into the request-context vars.

        Mimics what ``ApiActivityLoggingMiddleware`` does at request
        entry, so ``log_activity`` (which calls
        ``get_current_activity()``) can find it.
        """
        from activity.models import Activity
        from activity.request_context import set_request_context

        activity = Activity.objects.create(
            actor=None,
            verb="get",
            method="GET",
            path="/api/v1/example/",
            target_identifier="/api/v1/example/",
        )
        tokens = set_request_context(user=None, activity=activity, is_audited=True)
        return activity, tokens

    def _reset_context(self, tokens):
        from activity.request_context import reset_request_context

        reset_request_context(tokens)

    def test_no_active_activity_is_a_noop(self):
        """Outside a request context the helper must not raise."""
        from activity.audit import log_activity

        # No set_request_context call → get_current_activity returns None.
        # Helper should return silently.
        log_activity(verb="should.not.crash")

    def test_sets_verb(self):
        from activity.audit import log_activity

        activity, tokens = self._make_activity_with_context()
        try:
            log_activity(verb="library.collection.create")
        finally:
            self._reset_context(tokens)

        activity.refresh_from_db()
        assert activity.verb == "library.collection.create"

    def test_sets_target_from_instance(self, user):
        from activity.audit import log_activity

        activity, tokens = self._make_activity_with_context()
        try:
            log_activity(verb="user.profile.read", target=user)
        finally:
            self._reset_context(tokens)

        activity.refresh_from_db()
        assert activity.target_object_id == str(user.pk)
        assert activity.target_content_type is not None
        assert activity.target_content_type.app_label == "user"

    def test_merges_metadata_with_existing(self):
        from activity.audit import log_activity
        from activity.models import Activity

        activity, tokens = self._make_activity_with_context()
        # Simulate the middleware pre-populating something (we don't do
        # this today, but the helper must not drop keys it does not own).
        Activity.objects.filter(pk=activity.pk).update(metadata={"middleware_seeded": "yes"})
        activity.refresh_from_db()
        try:
            log_activity(
                verb="example.action",
                metadata={"endpoint_key": "value"},
            )
        finally:
            self._reset_context(tokens)

        activity.refresh_from_db()
        assert activity.metadata == {
            "middleware_seeded": "yes",
            "endpoint_key": "value",
        }

    def test_only_writes_mutated_fields(self):
        """Verify ``save(update_fields=...)`` is scoped to fields the
        helper actually changed — concurrent writes by the middleware to
        ``status_code`` (on exit) must survive."""
        from activity.audit import log_activity
        from activity.models import Activity

        activity, tokens = self._make_activity_with_context()
        try:
            # Pretend a concurrent write happened — set status_code through
            # a raw queryset update so it bypasses the cached instance.
            Activity.objects.filter(pk=activity.pk).update(status_code=200)
            # ``log_activity`` operates on the cached instance whose
            # ``status_code`` is still None; if it included status_code in
            # update_fields it would clobber the 200 back to None.
            log_activity(verb="example.action")
        finally:
            self._reset_context(tokens)

        activity.refresh_from_db()
        assert activity.verb == "example.action"
        assert activity.status_code == 200, (
            "log_activity wrote a field it did not modify, clobbering a concurrent middleware update."
        )

    def test_target_without_pk_skips_object_id(self, user):
        """An unsaved instance has ``pk is None`` — content type still set,
        object_id left empty so the row is queryable by content type but
        doesn't claim a fictional PK."""
        from activity.audit import log_activity
        from user.models import User

        # Unsaved instance — pk is None.
        unsaved = User(username="unsaved_for_test")
        assert unsaved.pk is None

        activity, tokens = self._make_activity_with_context()
        try:
            log_activity(verb="user.profile.read", target=unsaved)
        finally:
            self._reset_context(tokens)

        activity.refresh_from_db()
        assert activity.target_content_type is not None
        assert activity.target_content_type.app_label == "user"
        assert activity.target_object_id == ""


@pytest.mark.django_db
class TestRollbackChange:
    def _create_change_log(self, user, recording, before_state):
        ct = ContentType.objects.get_for_model(recording, for_concrete_model=False)
        now = timezone.now()
        changes = {
            "original_name": {
                "from": before_state["original_name"],
                "to": "changed.edf",
            }
        }
        return ObjectChangeLog.objects.create(
            content_type=ct,
            object_id=str(recording.pk),
            action=ObjectChangeLog.ACTION_MODIFY,
            performed_by=user,
            before_state=before_state,
            changes=changes,
            after_hash=compute_audit_hash(
                hash_payload_state(ObjectChangeLog.ACTION_MODIFY, before_state, changes),
                performed_by_id=user.pk,
                content_type_id=ct.pk,
                object_id=str(recording.pk),
                action=ObjectChangeLog.ACTION_MODIFY,
                timestamp=now.isoformat(),
            ),
            created_at=now,
        )

    def test_rollback_restores_modify(self, user):
        from model_bakery import baker

        recording = baker.make("recordings.Recording", author=user, original_name="changed.edf")
        before = serialize_instance(recording)
        before["original_name"] = "original.edf"
        change = self._create_change_log(user, recording, before)

        restored = rollback_change(user=user, change_id=change.pk)
        assert restored.original_name == "original.edf"

    def test_rollback_creates_rollback_change_log(self, user):
        from model_bakery import baker

        recording = baker.make("recordings.Recording", author=user, original_name="changed.edf")
        before = serialize_instance(recording)
        before["original_name"] = "original.edf"
        change = self._create_change_log(user, recording, before)

        rollback_change(user=user, change_id=change.pk)
        assert ObjectChangeLog.objects.filter(
            object_id=str(recording.pk), action=ObjectChangeLog.ACTION_ROLLBACK
        ).exists()

    def test_rollback_raises_for_missing_change(self, user):
        with pytest.raises(ObjectChangeLog.DoesNotExist):
            rollback_change(user=user, change_id=999999)

    def test_rollback_raises_permission_error_for_non_author(self, user, make_user):
        from model_bakery import baker

        other = make_user(username="other")
        recording = baker.make("recordings.Recording", author=user, original_name="changed.edf")
        before = serialize_instance(recording)
        change = self._create_change_log(user, recording, before)

        with pytest.raises(PermissionError):
            rollback_change(user=other, change_id=change.pk)

    def test_superuser_can_rollback_any_change(self, user, superuser):
        from model_bakery import baker

        recording = baker.make("recordings.Recording", author=user, original_name="changed.edf")
        before = serialize_instance(recording)
        before["original_name"] = "original.edf"
        change = self._create_change_log(user, recording, before)

        restored = rollback_change(user=superuser, change_id=change.pk)
        assert restored.original_name == "original.edf"


@pytest.mark.django_db
class TestChainWrites:
    """Per-content_type chain writes through ``create_chained_change_log``.

    Pins the chain invariants on the write side: every chained row's
    ``prev_hash`` references the previous row's ``after_hash`` (or the
    per-shard sentinel for the first row), ``sequence_no`` advances
    monotonically without gaps, and shards do not interfere with each
    other.
    """

    def _make_recording(self, user):
        from model_bakery import baker

        return baker.make("recordings.Recording", author=user)

    def _ct(self, instance):
        return ContentType.objects.get_for_model(instance, for_concrete_model=False)

    def test_first_row_references_genesis_sentinel(self, user, settings):
        settings.ACTIVITY_HASH_KEYS = {1: b"k" * 32}
        settings.ACTIVITY_HASH_KEY_CURRENT = 1
        recording = self._make_recording(user)
        ct = self._ct(recording)
        row = create_chained_change_log(
            content_type=ct,
            object_id=str(recording.pk),
            action=ObjectChangeLog.ACTION_CREATE,
            performed_by=user,
            before_state={"x": 1},
            changes=None,
            hash_payload={"x": 1},
            timestamp=timezone.now(),
        )
        assert row.hash_algorithm == "v3"
        assert row.sequence_no == 1
        assert row.prev_hash == genesis_sentinel(ct.pk)

    def test_sequence_advances_monotonically(self, user, settings):
        settings.ACTIVITY_HASH_KEYS = {1: b"k" * 32}
        settings.ACTIVITY_HASH_KEY_CURRENT = 1
        recording = self._make_recording(user)
        ct = self._ct(recording)
        rows = [
            create_chained_change_log(
                content_type=ct,
                object_id=str(recording.pk),
                action=ObjectChangeLog.ACTION_MODIFY,
                performed_by=user,
                before_state={"x": i},
                changes={"x": {"from": i, "to": i + 1}},
                hash_payload={"x": i + 1},
                timestamp=timezone.now(),
            )
            for i in range(5)
        ]
        assert [r.sequence_no for r in rows] == [1, 2, 3, 4, 5]
        # Each row's prev_hash references the predecessor's after_hash.
        for prev, curr in itertools.pairwise(rows):
            assert curr.prev_hash == prev.after_hash

    def test_shards_are_independent(self, user, settings):
        """A write to a different content_type starts a fresh chain at
        sequence_no=1 with its own genesis sentinel — shards don't share
        the global counter."""
        settings.ACTIVITY_HASH_KEYS = {1: b"k" * 32}
        settings.ACTIVITY_HASH_KEY_CURRENT = 1
        from model_bakery import baker

        rec = self._make_recording(user)
        ann = baker.make(
            "annotations.Annotation",
            target_object_id=rec.pk,
            target_content_type=self._ct(rec),
            author=user,
        )

        rec_row = create_chained_change_log(
            content_type=self._ct(rec),
            object_id=str(rec.pk),
            action=ObjectChangeLog.ACTION_MODIFY,
            performed_by=user,
            before_state={"x": 1},
            changes={"x": {"from": 1, "to": 2}},
            hash_payload={"x": 2},
            timestamp=timezone.now(),
        )
        ann_row = create_chained_change_log(
            content_type=self._ct(ann),
            object_id=str(ann.pk),
            action=ObjectChangeLog.ACTION_MODIFY,
            performed_by=user,
            before_state={"y": 1},
            changes={"y": {"from": 1, "to": 2}},
            hash_payload={"y": 2},
            timestamp=timezone.now(),
        )
        assert rec_row.sequence_no == 1
        assert ann_row.sequence_no == 1
        assert rec_row.prev_hash == genesis_sentinel(self._ct(rec).pk)
        assert ann_row.prev_hash == genesis_sentinel(self._ct(ann).pk)
        assert rec_row.prev_hash != ann_row.prev_hash  # sentinels differ by shard

    def test_falls_back_to_unchained_without_keys(self, user, settings):
        """When no HMAC key is configured, the helper writes a v1 row with
        sequence_no=None and prev_hash="" so a future chain walk skips it."""
        settings.ACTIVITY_HASH_KEYS = {}
        recording = self._make_recording(user)
        row = create_chained_change_log(
            content_type=self._ct(recording),
            object_id=str(recording.pk),
            action=ObjectChangeLog.ACTION_CREATE,
            performed_by=user,
            before_state={"x": 1},
            changes=None,
            hash_payload={"x": 1},
            timestamp=timezone.now(),
        )
        assert row.hash_algorithm == "v1"
        assert row.sequence_no is None
        assert row.prev_hash == ""


@pytest.mark.django_db
class TestChainVerification:
    """Read-side chain integrity via ``verify_chain``.

    Pins that tampering propagates downstream, gaps are detected, the
    genesis sentinel is checked, and pre-chain rows are excluded from the
    walk.
    """

    def _make_chain(self, user, ct, length=3):
        """Build a clean v3 chain of *length* rows."""
        rows = []
        for i in range(length):
            rows.append(
                create_chained_change_log(
                    content_type=ct,
                    object_id="42",
                    action=ObjectChangeLog.ACTION_MODIFY,
                    performed_by=user,
                    before_state={"x": i},
                    changes={"x": {"from": i, "to": i + 1}},
                    hash_payload={"x": i + 1},
                    timestamp=timezone.now(),
                )
            )
        return rows

    def test_clean_chain_verifies(self, user, settings):
        settings.ACTIVITY_HASH_KEYS = {1: b"k" * 32}
        settings.ACTIVITY_HASH_KEY_CURRENT = 1
        from model_bakery import baker

        rec = baker.make("recordings.Recording", author=user)
        ct = ContentType.objects.get_for_model(rec, for_concrete_model=False)
        self._make_chain(user, ct, length=5)

        result = verify_chain(ct)
        assert result.ok
        assert result.rows_checked == 5
        assert result.first_break_sequence_no is None
        assert result.downstream_break_count == 0
        assert result.gap_sequence_nos == []
        assert result.genesis_ok is True

    def test_naive_content_tamper_caught_at_target_row(self, user, settings):
        """Tampering with a row's content WITHOUT also recomputing its
        ``after_hash`` is the simplest attack — verify_change_hash on the
        target row catches it directly. Downstream rows still verify
        because their ``prev_hash`` references the target's *stored*
        ``after_hash``, which the attacker did not touch."""
        settings.ACTIVITY_HASH_KEYS = {1: b"k" * 32}
        settings.ACTIVITY_HASH_KEY_CURRENT = 1
        from model_bakery import baker

        rec = baker.make("recordings.Recording", author=user)
        ct = ContentType.objects.get_for_model(rec, for_concrete_model=False)
        rows = self._make_chain(user, ct, length=5)

        target = rows[2]  # sequence_no=3
        target.before_state = {**target.before_state, "injected": "tamper"}
        target.save(update_fields=["before_state"])

        result = verify_chain(ct)
        assert not result.ok
        assert result.first_break_sequence_no == 3
        assert result.downstream_break_count == 0

    def test_covered_track_tamper_caught_at_next_row(self, user, settings):
        """The chain's distinctive property: when an attacker with key
        access tampers with a row's content AND recomputes its
        ``after_hash`` so the per-row check passes, the row's link
        (``prev_hash`` on the NEXT row) still references the old
        ``after_hash``. The chain walk catches the break at row N+1, not
        row N. To hide further, the attacker would have to rewrite every
        subsequent row's ``after_hash`` — the forced full-tail rewrite is
        the cost the chain imposes."""
        settings.ACTIVITY_HASH_KEYS = {1: b"k" * 32}
        settings.ACTIVITY_HASH_KEY_CURRENT = 1
        from model_bakery import baker

        rec = baker.make("recordings.Recording", author=user)
        ct = ContentType.objects.get_for_model(rec, for_concrete_model=False)
        rows = self._make_chain(user, ct, length=5)

        target = rows[2]  # sequence_no=3
        target.before_state = {**target.before_state, "injected": "tamper"}
        target.after_hash = compute_audit_hash(
            hash_payload_state(target.action, target.before_state, target.changes),
            performed_by_id=target.performed_by_id,
            content_type_id=target.content_type_id,
            object_id=target.object_id,
            action=target.action,
            timestamp=target.created_at.isoformat(),
            algorithm="v3",
            hash_key_version=1,
            prev_hash=target.prev_hash,
        )
        target.save(update_fields=["before_state", "after_hash"])

        result = verify_chain(ct)
        assert not result.ok
        # row 3 (the tampered row) still self-verifies, but row 4's
        # stored prev_hash references row 3's OLD after_hash, so the link
        # check fails at row 4.
        assert result.first_break_sequence_no == 4

    def test_missing_middle_row_detected_as_gap(self, user, settings):
        """Deleting a row in the middle of the chain leaves a hole in the
        sequence_no progression. The chain walk reports the gap even if
        the remaining rows still verify against each other (which they
        don't, because the post-gap row's prev_hash references the
        deleted row's hash) — gap detection runs independently of the
        hash walk."""
        settings.ACTIVITY_HASH_KEYS = {1: b"k" * 32}
        settings.ACTIVITY_HASH_KEY_CURRENT = 1
        from model_bakery import baker

        rec = baker.make("recordings.Recording", author=user)
        ct = ContentType.objects.get_for_model(rec, for_concrete_model=False)
        rows = self._make_chain(user, ct, length=5)

        rows[2].delete()  # delete sequence_no=3

        result = verify_chain(ct)
        assert not result.ok
        assert 3 in result.gap_sequence_nos

    def test_lifted_genesis_detected(self, user, settings):
        """Replacing the first row's prev_hash with another shard's
        sentinel fails the genesis check — the chain looks self-consistent
        but can't have legitimately started on this content_type."""
        settings.ACTIVITY_HASH_KEYS = {1: b"k" * 32}
        settings.ACTIVITY_HASH_KEY_CURRENT = 1
        from model_bakery import baker

        rec = baker.make("recordings.Recording", author=user)
        ct = ContentType.objects.get_for_model(rec, for_concrete_model=False)
        rows = self._make_chain(user, ct, length=3)

        # Tamper: write a sentinel from a different shard onto the first
        # row's prev_hash field.
        rows[0].prev_hash = genesis_sentinel(99999)
        rows[0].save(update_fields=["prev_hash"])

        result = verify_chain(ct)
        assert not result.ok
        assert result.genesis_ok is False

    def test_pre_chain_rows_excluded_from_walk(self, user, settings):
        """v1/v2 rows (sequence_no=NULL) are not part of the chain. They
        verify individually under their own algorithm via
        ``verify_change_hash``; the chain walk simply ignores them."""
        settings.ACTIVITY_HASH_KEYS = {1: b"k" * 32}
        settings.ACTIVITY_HASH_KEY_CURRENT = 1
        from model_bakery import baker

        rec = baker.make("recordings.Recording", author=user)
        ct = ContentType.objects.get_for_model(rec, for_concrete_model=False)

        # Write a pre-chain row directly (e.g. legacy v1).
        now = timezone.now()
        ObjectChangeLog.objects.create(
            content_type=ct,
            object_id="42",
            action=ObjectChangeLog.ACTION_CREATE,
            performed_by=user,
            before_state={"legacy": True},
            changes=None,
            hash_algorithm="v1",
            hash_key_version=None,
            after_hash=compute_audit_hash(
                {"legacy": True},
                performed_by_id=user.pk,
                content_type_id=ct.pk,
                object_id="42",
                action=ObjectChangeLog.ACTION_CREATE,
                timestamp=now.isoformat(),
                algorithm="v1",
            ),
            created_at=now,
        )

        # Then chain three v3 rows.
        self._make_chain(user, ct, length=3)

        result = verify_chain(ct)
        assert result.ok
        assert result.rows_checked == 3  # pre-chain row excluded
