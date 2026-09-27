"""Curator review of a release-gated dataset's members: approvals, the veto, and what a release run takes from them.

Covers the ``/datasets/{id}/reviews/`` endpoints (managers only, counts never names, approve, withdraw, veto with a
closed reason), the helpers a project's selector builds on (``approved_items``, ``approvers_of``, ``veto_member``) and
the run's sign-off as the union of the selector's approvers and the command line.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime

import pytest
from django.test import Client

from activity.models import Activity, ObjectChangeLog
from conftest import delete_json, post_json
from epicurrents.system_user import get_system_user
from library.models import Dataset, DatasetItem, DatasetRelease, MemberApproval
from library.release import (
    ReleaseDecision,
    approved_items,
    approvers_of,
    register_release_selector,
    run_release,
    veto_member,
)
from library.tests.test_release import HASHES, _add, _gated_dataset, _grant, _recording, _release
from recordings.models import Recording
from recordings.submissions import IngestProfile, register_ingest_profile, reset_ingest_profiles

pytestmark = pytest.mark.django_db

DATASETS = "/api/v1/library/datasets"


@pytest.fixture(autouse=True)
def _clean_registries():
    register_release_selector(None)
    reset_ingest_profiles()
    yield
    register_release_selector(None)
    reset_ingest_profiles()


def _client(user) -> Client:
    client = Client()
    client.force_login(user)
    return client


def _reviews_url(dataset) -> str:
    return f"{DATASETS}/{dataset.object_hash}/reviews/"


def _member_url(dataset, index: int, action: str) -> str:
    return f"{_reviews_url(dataset)}{HASHES[index]}/{action}"


@pytest.fixture
def pool(make_user, tmp_path):
    """A release-gated dataset with two pooled recording members whose files exist; returns (dataset, author, items).

    The recordings belong to the system user, as every pooled submission does; the dataset to *author*.
    """
    author = make_user()
    members = []
    for index in (0, 1):
        path = tmp_path / f"{index}.edf"
        path.write_bytes(b"0" * 16)
        members.append(_recording(get_system_user(), index=index, file_path=str(path)))
    dataset, items = _gated_dataset(author, *members)
    return dataset, author, items


class TestWhoReviews:
    def test_a_write_grantee_is_a_curator(self, pool, make_user):
        dataset, author, _items = pool
        curator = make_user()
        _grant(dataset, author, target=curator, can_write=True)
        assert _client(curator).get(_reviews_url(dataset)).status_code == 200

    def test_a_reader_is_not(self, pool, make_user):
        dataset, author, _items = pool
        reader = make_user()
        _grant(dataset, author, target=reader)
        client = _client(reader)
        assert client.get(_reviews_url(dataset)).status_code == 403
        assert post_json(client, _member_url(dataset, 0, "approval"), {}).status_code == 403
        assert post_json(client, _member_url(dataset, 0, "veto"), {"reason": "device"}).status_code == 403
        assert Recording.objects.count() == 2

    def test_a_dataset_that_is_not_gated_has_no_review(self, pool):
        dataset, author, _items = pool
        dataset.release_gated = False
        dataset.save(update_fields=["release_gated"])
        assert _client(author).get(_reviews_url(dataset)).status_code == 409

    def test_a_failed_member_is_hidden_from_a_curator_who_is_not_a_superuser(self, pool, make_user, superuser):
        dataset, author, items = pool
        Recording.objects.filter(pk=int(items[0].object_id)).update(status=Recording.Status.FAILED)
        curator = make_user()
        _grant(dataset, author, target=curator, can_write=True)
        client = _client(curator)
        assert HASHES[0] not in [member["hash"] for member in client.get(_reviews_url(dataset)).json()["members"]]
        assert post_json(client, _member_url(dataset, 0, "approval"), {}).status_code == 404
        assert post_json(client, _member_url(dataset, 0, "veto"), {"reason": "device"}).status_code == 404
        listed = _client(superuser).get(_reviews_url(dataset)).json()["members"]
        assert HASHES[0] in [member["hash"] for member in listed]

    def test_a_recording_outside_the_dataset_is_not_found(self, pool, make_user):
        dataset, author, _items = pool
        _recording(author, index=5)
        assert post_json(_client(author), _member_url(dataset, 5, "approval"), {}).status_code == 404


class TestApproval:
    def test_approvals_are_counted_never_named(self, pool, make_user):
        dataset, author, _items = pool
        curator = make_user()
        _grant(dataset, author, target=curator, can_write=True)
        assert post_json(_client(author), _member_url(dataset, 0, "approval"), {}).status_code == 201
        body = _client(curator).get(_reviews_url(dataset)).json()
        assert body["approvals_required"] is None
        first = next(member for member in body["members"] if member["hash"] == HASHES[0])
        assert first == {"hash": HASHES[0], "released": False, "approvals": 1, "approved_by_me": False}
        assert "reviewer" not in json.dumps(body)

    def test_approving_twice_is_one_approval(self, pool):
        dataset, author, items = pool
        client = _client(author)
        post_json(client, _member_url(dataset, 0, "approval"), {})
        response = post_json(client, _member_url(dataset, 0, "approval"), {})
        assert response.json()["approvals"] == 1
        assert MemberApproval.objects.filter(item=items[0]).count() == 1
        activity = Activity.objects.filter(verb="library.dataset.member.approval.create").latest("pk")
        assert activity.target_object_id == str(items[0].pk)
        assert activity.target_content_type.model == "datasetitem"

    def test_withdrawing_removes_only_the_callers_approval(self, pool, make_user):
        dataset, author, items = pool
        curator = make_user()
        _grant(dataset, author, target=curator, can_write=True)
        post_json(_client(author), _member_url(dataset, 0, "approval"), {})
        post_json(_client(curator), _member_url(dataset, 0, "approval"), {})
        response = delete_json(_client(curator), _member_url(dataset, 0, "approval"))
        assert response.json() == {"hash": HASHES[0], "released": False, "approvals": 1, "approved_by_me": False}
        activity = Activity.objects.get(verb="library.dataset.member.approval.delete")
        assert activity.target_object_id == str(items[0].pk)
        assert list(MemberApproval.objects.filter(item=items[0]).values_list("reviewer_id", flat=True)) == [author.pk]

    def test_a_released_member_is_not_approved_or_withdrawn(self, pool):
        dataset, author, items = pool
        _release(dataset, items[0])
        client = _client(author)
        assert post_json(client, _member_url(dataset, 0, "approval"), {}).status_code == 409
        assert delete_json(client, _member_url(dataset, 0, "approval")).status_code == 409

    def test_the_pool_profile_sets_the_approvals_required(self, pool):
        dataset, author, _items = pool
        register_ingest_profile(IngestProfile(key="test.pool", approvals=2))
        dataset.submission_profile = "test.pool"
        dataset.save(update_fields=["submission_profile"])
        assert _client(author).get(_reviews_url(dataset)).json()["approvals_required"] == 2
        register_ingest_profile(IngestProfile(key="test.pool"))
        assert _client(author).get(_reviews_url(dataset)).json()["approvals_required"] == 1


class TestVeto:
    def test_a_veto_removes_the_recording_its_file_and_its_membership(self, pool, tmp_path):
        dataset, author, items = pool
        post_json(_client(author), _member_url(dataset, 0, "approval"), {})
        response = post_json(_client(author), _member_url(dataset, 0, "veto"), {"reason": "skull_defect"})
        assert response.status_code == 204
        assert not Recording.objects.filter(pk=int(items[0].object_id)).exists()
        assert not DatasetItem.objects.filter(pk=items[0].pk).exists()
        assert not MemberApproval.objects.exists()
        assert not (tmp_path / "0.edf").exists()
        assert (tmp_path / "1.edf").exists()
        activity = Activity.objects.get(verb="library.dataset.member.veto")
        assert activity.metadata == {"reason": "skull_defect", "released": False, "removed": "recording"}
        assert (activity.target_content_type.model, activity.target_object_id) == ("datasetitem", str(items[0].pk))
        assert ObjectChangeLog.objects.filter(activity=activity, action=ObjectChangeLog.ACTION_DELETE).exists()

    def test_a_released_member_can_be_vetoed(self, pool):
        dataset, author, items = pool
        _release(dataset, items[0])
        assert post_json(_client(author), _member_url(dataset, 0, "veto"), {"reason": "device"}).status_code == 204
        assert Activity.objects.get(verb="library.dataset.member.veto").metadata["released"] is True

    @pytest.mark.parametrize("reason", ["", "Patient is the mayor", "DEVICE"])
    def test_a_reason_outside_the_list_is_refused_and_nothing_removed(self, pool, reason):
        dataset, author, _items = pool
        response = post_json(_client(author), _member_url(dataset, 0, "veto"), {"reason": reason})
        assert response.status_code == 400
        assert Recording.objects.count() == 2

    def test_an_unlink_failure_removes_nothing(self, pool, monkeypatch):
        dataset, author, items = pool

        def refuse(self, missing_ok=False):
            raise PermissionError("read-only")

        monkeypatch.setattr("pathlib.Path.unlink", refuse)
        response = post_json(_client(author), _member_url(dataset, 0, "veto"), {"reason": "device"})
        assert response.status_code == 500
        assert Recording.objects.filter(pk=int(items[0].object_id)).exists()

    def test_a_users_own_recording_only_leaves_the_dataset(self, make_user, tmp_path):
        author = make_user()
        uploader = make_user()
        path = tmp_path / "own.edf"
        path.write_bytes(b"0" * 16)
        own = _recording(uploader, index=3, file_path=str(path))
        dataset, items = _gated_dataset(author, own)
        response = post_json(_client(author), _member_url(dataset, 3, "veto"), {"reason": "device"})
        assert response.status_code == 204
        assert Recording.objects.filter(pk=own.pk).exists()
        assert path.exists()
        assert not DatasetItem.objects.filter(pk=items[0].pk).exists()
        assert Activity.objects.get(verb="library.dataset.member.veto").metadata["removed"] == "membership"

    def test_veto_member_refuses_a_non_recording(self, make_user):
        author = make_user()
        dataset, _ = _gated_dataset(author)
        other = Dataset.objects.create(author=author, name="nested")
        with pytest.raises(ValueError):
            veto_member(_add(dataset, other))


class TestSelectorHelpers:
    def test_approved_items_counts_distinct_curators(self, pool, make_user):
        _dataset, author, items = pool
        other = make_user()
        MemberApproval.objects.create(item=items[0], reviewer=author)
        MemberApproval.objects.create(item=items[0], reviewer=other)
        MemberApproval.objects.create(item=items[1], reviewer=author)
        assert approved_items(items, required=2) == [items[0]]
        assert approved_items(items, required=1) == items

    def test_an_approval_outlives_its_curators_account(self, pool, make_user):
        _dataset, _author, items = pool
        gone = make_user()
        MemberApproval.objects.create(item=items[0], reviewer=gone)
        gone.delete()
        assert approved_items(items, required=1) == [items[0]]
        assert approvers_of(items) == []

    def test_approvers_of_is_the_distinct_sorted_curators(self, pool, make_user):
        _dataset, author, items = pool
        other = make_user()
        MemberApproval.objects.create(item=items[0], reviewer=other)
        MemberApproval.objects.create(item=items[1], reviewer=other)
        MemberApproval.objects.create(item=items[1], reviewer=author)
        assert approvers_of(items) == sorted({author.pk, other.pk})

    def test_the_run_signs_off_with_the_selectors_approvers_and_the_command_line(self, make_user):
        author = make_user()
        officer = make_user()
        curator = make_user()
        member = _recording(author, index=0, created_at=datetime(2026, 7, 5, tzinfo=UTC))
        dataset, _items = _gated_dataset(author, member)
        register_release_selector(
            lambda _dataset, eligible, as_of: ReleaseDecision(items=eligible, sign_off_user_ids=[curator.pk])
        )
        release, _eligible, _released = run_release(dataset, as_of=date(2026, 10, 1), sign_off_user_ids=[officer.pk])
        assert DatasetRelease.objects.get(pk=release.pk).sign_off_user_ids == sorted({curator.pk, officer.pk})
