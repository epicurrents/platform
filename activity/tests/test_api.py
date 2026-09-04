"""Tests for activity API — change log listing and rollback endpoint."""

import pytest
from django.contrib.contenttypes.models import ContentType
from django.utils import timezone

from activity.audit import compute_audit_hash, hash_payload_state, serialize_instance
from activity.models import ObjectChangeLog
from conftest import post_json
from epicurrents.models import AccessRight

CHANGES_URL = "/api/v1/activity/changes/"
ROLLBACK_URL = "/api/v1/activity/rollback/{change_id}"


def _grant_write(user, recording):
    """Mirror the AccessRight rows that real upload flows create.

    ``baker.make`` does not auto-create AccessRight rows, so tests that need
    a non-superuser to be able to read/rollback their own changes must add
    one explicitly — same shape as production, where the upload endpoint
    creates Recording + AccessRight together inside transaction.atomic().
    """
    ct = ContentType.objects.get_for_model(recording, for_concrete_model=False)
    return AccessRight.objects.create(
        content_type=ct,
        object_id=str(recording.pk),
        access_giver=user,
        access_target=user,
        can_read=True,
        can_write=True,
    )


def _create_change(
    user,
    recording,
    action=ObjectChangeLog.ACTION_MODIFY,
    before_state=None,
    changes=None,
):
    """Build a self-consistent ObjectChangeLog row for tests.

    Hash payload is reconstructed via ``hash_payload_state`` so the stored
    ``after_hash`` matches what ``verify_change_hash`` will recompute. Tests
    that need a specific ``before_state`` (e.g. to set up a known rollback
    target) pass it here rather than mutating the row after creation.
    """
    ct = ContentType.objects.get_for_model(recording, for_concrete_model=False)
    before = before_state if before_state is not None else serialize_instance(recording)
    now = timezone.now()
    return ObjectChangeLog.objects.create(
        content_type=ct,
        object_id=str(recording.pk),
        action=action,
        performed_by=user,
        before_state=before,
        changes=changes,
        after_hash=compute_audit_hash(
            hash_payload_state(action, before, changes),
            performed_by_id=user.pk,
            content_type_id=ct.pk,
            object_id=str(recording.pk),
            action=action,
            timestamp=now.isoformat(),
        ),
        created_at=now,
    )


@pytest.mark.django_db
class TestListChangeLogs:
    def test_unauthenticated_returns_401(self, client):
        assert client.get(CHANGES_URL).status_code == 401

    def test_superuser_sees_all_changes(self, superuser_client, user):
        from model_bakery import baker

        c, su = superuser_client
        recording = baker.make("recordings.Recording", author=user)
        _create_change(user, recording)
        resp = c.get(CHANGES_URL)
        assert resp.status_code == 200
        assert len(resp.json()) >= 1

    def test_regular_user_sees_own_changes(self, auth_client):
        from model_bakery import baker

        c, user = auth_client
        recording = baker.make("recordings.Recording", author=user)
        _grant_write(user, recording)
        _create_change(user, recording)
        resp = c.get(CHANGES_URL)
        assert resp.status_code == 200
        assert len(resp.json()) >= 1

    def test_filter_by_action(self, superuser_client, user):
        from model_bakery import baker

        c, su = superuser_client
        recording = baker.make("recordings.Recording", author=user)
        _create_change(user, recording, action=ObjectChangeLog.ACTION_MODIFY)
        _create_change(user, recording, action=ObjectChangeLog.ACTION_DELETE)
        resp = c.get(f"{CHANGES_URL}?action=modify")
        assert resp.status_code == 200
        assert all(entry["action"] == "modify" for entry in resp.json())

    def test_invalid_action_filter_returns_400(self, auth_client):
        c, _ = auth_client
        resp = c.get(f"{CHANGES_URL}?action=invalid")
        assert resp.status_code == 400

    def test_filter_by_model(self, superuser_client, user):
        from model_bakery import baker

        c, su = superuser_client
        recording = baker.make("recordings.Recording", author=user)
        _create_change(user, recording)
        resp = c.get(f"{CHANGES_URL}?model=recording")
        assert resp.status_code == 200
        assert all(entry["model"] == "recording" for entry in resp.json())

    def test_response_includes_verified_field(self, superuser_client, user):
        from model_bakery import baker

        c, su = superuser_client
        recording = baker.make("recordings.Recording", author=user)
        _create_change(user, recording)
        resp = c.get(CHANGES_URL)
        assert resp.status_code == 200
        rows = resp.json()
        assert rows
        for row in rows:
            assert "verified" in row
            assert row["verified"] is True  # untampered

    def test_tampered_row_surfaces_as_unverified(self, superuser_client, user):
        from model_bakery import baker

        c, su = superuser_client
        recording = baker.make("recordings.Recording", author=user)
        change = _create_change(user, recording)
        # Tamper after creation: phantom field that survives reconstruction.
        change.before_state = {**change.before_state, "injected_field": "x"}
        change.save(update_fields=["before_state"])

        resp = c.get(CHANGES_URL)
        rows = {row["id"]: row for row in resp.json()}
        assert rows[change.pk]["verified"] is False


@pytest.mark.django_db
class TestRollbackEndpoint:
    def test_unauthenticated_returns_401(self, client):
        resp = post_json(client, "/api/v1/activity/rollback/1", {})
        assert resp.status_code == 401

    def test_missing_change_returns_404(self, auth_client):
        c, _ = auth_client
        resp = post_json(c, "/api/v1/activity/rollback/999999", {})
        assert resp.status_code == 404

    def test_author_can_rollback_own_change(self, auth_client):
        from model_bakery import baker

        c, user = auth_client
        recording = baker.make("recordings.Recording", author=user, original_name="v2.edf")
        before = serialize_instance(recording)
        before["original_name"] = "v1.edf"
        change = _create_change(user, recording, before_state=before)

        resp = post_json(c, f"/api/v1/activity/rollback/{change.pk}", {})
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "rolled_back"
        assert data["change_id"] == change.pk

    def test_non_author_cannot_rollback(self, client, make_user):
        from model_bakery import baker

        owner = make_user(username="owner")
        other = make_user(username="other")
        recording = baker.make("recordings.Recording", author=owner, original_name="v2.edf")
        before = serialize_instance(recording)
        change = _create_change(owner, recording, before_state=before)

        client.force_login(other)
        resp = post_json(client, f"/api/v1/activity/rollback/{change.pk}", {})
        assert resp.status_code == 403

    def test_superuser_can_rollback_any_change(self, superuser_client, user):
        from model_bakery import baker

        c, su = superuser_client
        recording = baker.make("recordings.Recording", author=user, original_name="v2.edf")
        before = serialize_instance(recording)
        before["original_name"] = "v1.edf"
        change = _create_change(user, recording, before_state=before)

        resp = post_json(c, f"/api/v1/activity/rollback/{change.pk}", {})
        assert resp.status_code == 200

    def test_write_grantee_cannot_rollback_create(self, client, make_user):
        """A can_write grant must not unlock destruction via CREATE rollback.

        Rolling back a CREATE entry deletes the object; a grantee holding
        only a collaboration grant on a shared object must not gain a
        deletion path its owner never delegated.
        """
        from model_bakery import baker

        owner = make_user(username="owner")
        grantee = make_user(username="grantee")
        recording = baker.make("recordings.Recording", author=owner, original_name="shared.edf")
        ct = ContentType.objects.get_for_model(recording, for_concrete_model=False)
        AccessRight.objects.create(
            content_type=ct,
            object_id=str(recording.pk),
            access_giver=owner,
            access_target=grantee,
            can_read=True,
            can_write=True,
        )
        change = _create_change(owner, recording, action=ObjectChangeLog.ACTION_CREATE)

        client.force_login(grantee)
        resp = post_json(client, f"/api/v1/activity/rollback/{change.pk}", {})
        assert resp.status_code == 403
        recording.refresh_from_db()
        assert recording.deleted_at is None

    def test_write_grantee_can_still_rollback_modify(self, client, make_user):
        """The CREATE restriction must not narrow MODIFY-rollback access."""
        from model_bakery import baker

        owner = make_user(username="owner")
        grantee = make_user(username="grantee")
        recording = baker.make("recordings.Recording", author=owner, original_name="v2.edf")
        ct = ContentType.objects.get_for_model(recording, for_concrete_model=False)
        AccessRight.objects.create(
            content_type=ct,
            object_id=str(recording.pk),
            access_giver=owner,
            access_target=grantee,
            can_read=True,
            can_write=True,
        )
        before = serialize_instance(recording)
        before["original_name"] = "v1.edf"
        change = _create_change(owner, recording, before_state=before)

        client.force_login(grantee)
        resp = post_json(client, f"/api/v1/activity/rollback/{change.pk}", {})
        assert resp.status_code == 200

    def test_author_create_rollback_soft_deletes(self, auth_client):
        """CREATE rollback on a soft-deletable model sets deleted_at.

        Hard delete would bypass the trash / retention pipeline the normal
        delete endpoints enforce.
        """
        from model_bakery import baker

        c, user = auth_client
        recording = baker.make("recordings.Recording", author=user, original_name="mine.edf")
        change = _create_change(user, recording, action=ObjectChangeLog.ACTION_CREATE)

        resp = post_json(c, f"/api/v1/activity/rollback/{change.pk}", {})
        assert resp.status_code == 200
        recording.refresh_from_db()  # row still exists — not hard-deleted
        assert recording.deleted_at is not None

    def test_create_rollback_is_undoable(self, auth_client):
        """Rolling back the CREATE-rollback entry clears deleted_at again."""
        from model_bakery import baker

        c, user = auth_client
        recording = baker.make("recordings.Recording", author=user, original_name="mine.edf")
        change = _create_change(user, recording, action=ObjectChangeLog.ACTION_CREATE)
        resp = post_json(c, f"/api/v1/activity/rollback/{change.pk}", {})
        assert resp.status_code == 200

        rollback_entry = (
            ObjectChangeLog.objects.filter(
                action=ObjectChangeLog.ACTION_ROLLBACK,
                object_id=str(recording.pk),
            )
            .order_by("-pk")
            .first()
        )
        assert rollback_entry is not None
        resp = post_json(c, f"/api/v1/activity/rollback/{rollback_entry.pk}", {})
        assert resp.status_code == 200
        recording.refresh_from_db()
        assert recording.deleted_at is None

    def test_create_rollback_hard_deletes_models_without_soft_delete(self, superuser_client):
        """Models without deleted_at keep the original hard-delete semantics."""
        from django.contrib.auth.models import Group

        c, su = superuser_client
        group = Group.objects.create(name="rollback-target")
        change = _create_change(su, group, action=ObjectChangeLog.ACTION_CREATE)

        resp = post_json(c, f"/api/v1/activity/rollback/{change.pk}", {})
        assert resp.status_code == 200
        assert not Group.objects.filter(pk=group.pk).exists()

    def test_tampered_row_returns_409(self, auth_client):
        from model_bakery import baker

        c, user = auth_client
        recording = baker.make("recordings.Recording", author=user, original_name="current.edf")
        change = _create_change(user, recording)
        # Phantom-field tamper survives reconstruction; see TestRollbackVerifiesHash.
        change.before_state = {**change.before_state, "injected_field": "x"}
        change.save(update_fields=["before_state"])

        resp = post_json(c, f"/api/v1/activity/rollback/{change.pk}", {})
        assert resp.status_code == 409
        recording.refresh_from_db()
        assert recording.original_name == "current.edf"  # rollback did not apply

    def test_bulk_with_tampered_row_returns_409_atomically(self, superuser_client, user):
        from model_bakery import baker

        c, su = superuser_client
        recording = baker.make("recordings.Recording", author=user, original_name="current.edf")
        clean = _create_change(user, recording)
        tampered = _create_change(user, recording)
        tampered.before_state = {
            **tampered.before_state,
            "injected_field": "x",
        }
        tampered.save(update_fields=["before_state"])

        resp = post_json(
            c,
            "/api/v1/activity/rollback/bulk",
            {"change_ids": [clean.pk, tampered.pk]},
        )
        assert resp.status_code == 409
        recording.refresh_from_db()
        # Bulk runs inside a single transaction; tampered row aborts the
        # whole batch, so the clean row's rollback must not apply either.
        assert recording.original_name == "current.edf"


@pytest.mark.django_db
class TestActivityAuditTrail:
    """Activity-row annotation contract for the activity API.

    Companion to the platform-wide audit-trail backfill.  One
    representative test per endpoint, locking the verb + target +
    metadata shape so a future regression that drops the annotation
    surfaces here rather than in a SIEM rule months later.
    """

    def test_rollback_single_records_activity_rollback(self, superuser_client, user):
        from model_bakery import baker

        from activity.models import Activity

        c, su = superuser_client
        recording = baker.make("recordings.Recording", author=user, original_name="v2.edf")
        before = serialize_instance(recording)
        before["original_name"] = "v1.edf"
        change = _create_change(user, recording, before_state=before)

        resp = post_json(c, f"/api/v1/activity/rollback/{change.pk}", {})
        assert resp.status_code == 200

        # The endpoint annotates the middleware row; the rollback_change
        # helper additionally inserts its own internal row with the legacy
        # ``"rollback"`` verb.  Filter on the taxonomy verb to grab the
        # endpoint's annotated row specifically.
        activity = Activity.objects.filter(verb="activity.rollback").latest("created_at")
        change_ct = ContentType.objects.get_for_model(change, for_concrete_model=False)
        assert activity.target_content_type_id == change_ct.pk
        assert activity.target_object_id == str(change.pk)

    def test_rollback_bulk_records_activity_rollback_bulk(self, superuser_client, user):
        from model_bakery import baker

        from activity.models import Activity

        c, su = superuser_client
        recording = baker.make("recordings.Recording", author=user, original_name="v2.edf")
        before = serialize_instance(recording)
        before["original_name"] = "v1.edf"
        change_a = _create_change(user, recording, before_state=before)
        change_b = _create_change(user, recording, before_state=before)

        resp = post_json(
            c,
            "/api/v1/activity/rollback/bulk",
            {"change_ids": [change_a.pk, change_b.pk]},
        )
        assert resp.status_code == 200

        activity = Activity.objects.filter(verb="activity.rollback.bulk").latest("created_at")
        assert activity.metadata["change_ids"] == [change_a.pk, change_b.pk]
        assert activity.metadata["rolled_back_count"] == 2

    def test_changelog_list_records_activity_changelog_list(self, auth_client):
        from activity.models import Activity

        c, _ = auth_client
        resp = c.get(f"{CHANGES_URL}?action=modify&limit=10")
        assert resp.status_code == 200

        activity = Activity.objects.filter(verb="activity.changelog.list").latest("created_at")
        assert activity.metadata["limit"] == 10
        assert activity.metadata["action_filter"] == "modify"
        assert activity.metadata["model_filter"] is None
        assert activity.metadata["activity_id_filter"] is None
        # ``returned_count`` is informative; exact value depends on what's
        # in the test DB at that moment, just assert it was recorded.
        assert "returned_count" in activity.metadata
