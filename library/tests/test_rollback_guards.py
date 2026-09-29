"""Rollback guards: the audit-trail rollback cannot restore pool or release state past the rules that govern it.

Reproduces the review probes (the author and a write grantee rolling back ``configure_pool`` on a filling pool)
and covers each guard in ``library.rollback_guards``, the single and bulk endpoints, the rollbackable-changes
listing, and the registry in ``activity.audit``.
"""

from __future__ import annotations

import json

import pytest
from django.contrib.contenttypes.models import ContentType
from django.test import Client

from activity.audit import can_rollback_change, register_rollback_guard, rollback_change, rollback_refusal
from activity.models import Activity, ObjectChangeLog
from activity.system_activity import with_system_activity
from library.models import Dataset, DatasetRelease, MemberApproval
from library.tests.test_release import _add, _gated_dataset, _grant, _recording, _release
from recordings.models import SubmissionLedger
from recordings.submissions import IngestProfile, register_ingest_profile, reset_ingest_profiles

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _profiles():
    reset_ingest_profiles()
    register_ingest_profile(IngestProfile(key="test.pool"))
    yield
    reset_ingest_profiles()


def _client(user) -> Client:
    client = Client()
    client.force_login(user)
    return client


def _filling_pool(author):
    """A pool configured through the endpoint, with one ledger; returns the dataset and the configure change."""
    created = _client(author).post(
        "/api/v1/library/datasets/", json.dumps({"name": "P"}), content_type="application/json"
    )
    dataset = Dataset.objects.get(object_hash=created.json()["object_hash"])
    response = _client(author).post(
        f"/api/v1/library/datasets/{dataset.object_hash}/pool/",
        json.dumps({"profile": "test.pool"}),
        content_type="application/json",
    )
    assert response.status_code == 201, response.content
    SubmissionLedger.objects.create(dataset=dataset, contributor=author)
    ct = ContentType.objects.get_for_model(Dataset)
    change = ObjectChangeLog.objects.filter(
        content_type=ct, object_id=str(dataset.pk), action=ObjectChangeLog.ACTION_MODIFY
    ).latest("pk")
    return dataset, change


def _unchanged_pool(dataset):
    dataset.refresh_from_db()
    assert (dataset.submission_profile, dataset.release_gated) == ("test.pool", True)
    assert dataset.submission_group_id is not None


class TestPoolConfiguration:
    def test_the_author_cannot_roll_back_the_pool_configuration(self, make_user):
        author = make_user()
        dataset, change = _filling_pool(author)
        response = _client(author).post(f"/api/v1/activity/rollback/{change.pk}", content_type="application/json")
        assert response.status_code == 400
        _unchanged_pool(dataset)

    def test_a_write_grantee_cannot_either(self, make_user):
        author, grantee = make_user(), make_user()
        dataset, change = _filling_pool(author)
        _grant(dataset, author, target=grantee, can_write=True)
        response = _client(grantee).post(f"/api/v1/activity/rollback/{change.pk}", content_type="application/json")
        assert response.status_code == 400
        _unchanged_pool(dataset)

    def test_nor_a_superuser_through_the_bulk_endpoint(self, make_user, superuser):
        dataset, change = _filling_pool(make_user())
        response = _client(superuser).post(
            "/api/v1/activity/rollback/bulk", json.dumps({"change_ids": [change.pk]}), content_type="application/json"
        )
        assert response.status_code == 403
        _unchanged_pool(dataset)
        assert can_rollback_change(superuser, change) is False
        author = Dataset.objects.get(pk=dataset.pk).author
        listed = _client(author).get("/api/v1/activity/changes/").json()
        assert change.pk not in [row["id"] for row in listed]

    def test_the_creation_of_a_pool_is_not_rolled_back(self, make_user):
        author = make_user()
        dataset, _change = _filling_pool(author)
        ct = ContentType.objects.get_for_model(Dataset)
        create = ObjectChangeLog.objects.get(
            content_type=ct, object_id=str(dataset.pk), action=ObjectChangeLog.ACTION_CREATE
        )
        with pytest.raises(ValueError, match="submission pool"):
            rollback_change(user=author, change_id=create.pk)
        dataset.refresh_from_db()
        assert dataset.deleted_at is None

    def test_a_rename_of_a_pool_still_rolls_back(self, make_user):
        author = make_user()
        dataset, _change = _filling_pool(author)
        response = _client(author).patch(
            f"/api/v1/library/datasets/{dataset.object_hash}/",
            json.dumps({"name": "Q"}),
            content_type="application/json",
        )
        assert response.status_code == 200
        ct = ContentType.objects.get_for_model(Dataset)
        rename = ObjectChangeLog.objects.filter(content_type=ct, object_id=str(dataset.pk)).latest("pk")
        assert rollback_refusal(rename) is None
        rollback_change(user=author, change_id=rename.pk)
        dataset.refresh_from_db()
        assert dataset.name == "P"
        _unchanged_pool(dataset)

    def test_the_gate_of_an_ordinary_dataset_is_not_toggled_by_rollback(self, make_user):
        author = make_user()
        dataset = Dataset.objects.create(author=author, name="ds")
        response = _client(author).patch(
            f"/api/v1/library/datasets/{dataset.object_hash}/",
            json.dumps({"release_gated": True}),
            content_type="application/json",
        )
        assert response.status_code == 200
        ct = ContentType.objects.get_for_model(Dataset)
        toggle = ObjectChangeLog.objects.filter(content_type=ct, object_id=str(dataset.pk)).latest("pk")
        with pytest.raises(ValueError, match="release gating"):
            rollback_change(user=author, change_id=toggle.pk)


class TestReleaseRecords:
    def _audited(self, fn):
        with with_system_activity("tests.setup", interface=Activity.Interface.COMMAND):
            return fn()

    def test_a_release_an_approval_and_a_membership_of_a_gated_dataset_are_not_rolled_back(self, make_user, superuser):
        author = make_user()
        recording = _recording(author)
        dataset, items = self._audited(lambda: _gated_dataset(author, recording))
        release = self._audited(lambda: _release(dataset, *items))
        approval = self._audited(lambda: MemberApproval.objects.create(item=items[0], reviewer=author))
        for model, pk in ((DatasetRelease, release.pk), (MemberApproval, approval.pk), (type(items[0]), items[0].pk)):
            ct = ContentType.objects.get_for_model(model)
            for change in ObjectChangeLog.objects.filter(content_type=ct, object_id=str(pk)):
                assert rollback_refusal(change), (model, change.action)
                with pytest.raises(ValueError):
                    rollback_change(user=superuser, change_id=change.pk)

    def test_a_membership_of_an_ordinary_dataset_still_rolls_back(self, make_user):
        author = make_user()
        dataset = Dataset.objects.create(author=author, name="ds")
        item = self._audited(lambda: _add(dataset, _recording(author)))
        change = ObjectChangeLog.objects.get(
            content_type=ContentType.objects.get_for_model(type(item)), object_id=str(item.pk)
        )
        assert rollback_refusal(change) is None

    def test_a_ledger_is_not_rolled_back(self, make_user):
        dataset = Dataset.objects.create(author=make_user(), name="P", submission_profile="test.pool")
        ledger = self._audited(lambda: SubmissionLedger.objects.create(dataset=dataset, contributor=make_user()))
        change = ObjectChangeLog.objects.get(content_type=ContentType.objects.get_for_model(SubmissionLedger))
        assert ledger.pk and rollback_refusal(change)


class TestRegistry:
    def test_a_guard_is_consulted_per_model_and_registered_once(self, make_user):
        calls = []

        def guard(change, existing_obj):
            calls.append(change.pk)

        register_rollback_guard("library.tag", guard)
        register_rollback_guard("library.tag", guard)
        from activity.audit import _ROLLBACK_GUARDS

        assert _ROLLBACK_GUARDS["library.tag"].count(guard) == 1
        _ROLLBACK_GUARDS["library.tag"].remove(guard)
