"""Configuring a submission pool from the dataset page, the rules that lock it, and its dedicated group.

Covers the ``/datasets/{id}/pool/`` endpoints (configure on an empty dataset, read, intake, profile change, dissolve,
all the author's or a superuser's), the locks a filling pool puts on the dataset surface (the gate, membership,
deletion), and the dedicated-group guards the pool registers: no grant, no role, no delete, left out of the
grant-target listing, and ``library.W001`` for a grant or role acquired another way.
"""

from __future__ import annotations

import io
from urllib.parse import urlencode

import pytest
from django.contrib.auth.models import Group
from django.contrib.contenttypes.models import ContentType
from django.test import Client, override_settings

from activity.models import Activity
from conftest import delete_json, patch_json, post_json
from epicurrents.models import AccessRight
from library.checks import check_pool_groups_grant_nothing
from library.models import Dataset, DatasetItem
from recordings.models import Recording, SubmissionFile, SubmissionLedger
from recordings.submissions import IngestProfile, register_ingest_profile, reset_ingest_profiles

pytestmark = pytest.mark.django_db

DATASETS = "/api/v1/library/datasets"
GROUPS = "/api/v1/user/admin/groups"


@pytest.fixture(autouse=True)
def _profiles():
    reset_ingest_profiles()
    register_ingest_profile(IngestProfile(key="test.pool", channels=("Fp1", "Fp2"), durations_seconds=(2.0,)))
    register_ingest_profile(IngestProfile(key="test.other"))
    yield
    reset_ingest_profiles()


def _client(user) -> Client:
    client = Client()
    client.force_login(user)
    return client


def _pool_url(dataset) -> str:
    return f"{DATASETS}/{dataset.object_hash}/pool/"


def _configure(client, dataset, profile="test.pool"):
    response = post_json(client, _pool_url(dataset), {"profile": profile})
    assert response.status_code == 201, response.content
    dataset.refresh_from_db()
    return response.json()


def _fill(dataset, contributor, *, files=0):
    ledger = SubmissionLedger.objects.create(dataset=dataset, contributor=contributor)
    for n in range(files):
        SubmissionFile.objects.create(
            ledger=ledger,
            stored_name=f"{ledger.pk:016X}{n:016X}.edf",
            file_extension=".edf",
            file_path="/nonexistent",
            file_size=1,
            file_hash="0" * 64,
            sidecar_hash="0" * 64,
        )
    return ledger


def _member(dataset, author):
    recording = Recording.objects.create(
        author=author,
        original_name="x.edf",
        stored_name=f"{Recording.objects.count():032X}.edf",
        file_extension=".edf",
        file_size=1,
        file_path="/nonexistent",
        file_hash="a" * 64,
        content_hash="b" * 64,
        status=Recording.Status.READY,
    )
    ct = ContentType.objects.get_for_model(Recording)
    return DatasetItem.objects.create(dataset=dataset, content_type=ct, object_id=str(recording.pk))


@pytest.fixture
def author(make_user):
    return make_user()


@pytest.fixture
def dataset(author):
    return Dataset.objects.create(author=author, name="Teaching pool")


class TestConfigure:
    def test_an_empty_dataset_becomes_a_closed_gated_pool_with_a_new_group(self, author, dataset):
        client = _client(author)
        assert client.get(_pool_url(dataset)).json() == {
            "profile": None,
            "group_id": None,
            "group_name": None,
            "open": False,
            "filling": False,
            "configurable": True,
            "pending_count": 0,
            "failed_count": 0,
            "ingested_count": 0,
            "contributor_count": 0,
            "contributors_required": None,
        }
        body = _configure(client, dataset)
        group = Group.objects.get(pk=body["group_id"])
        assert body["profile"] == "test.pool"
        assert body["group_name"] == group.name == "Teaching pool — contributors"
        assert body["open"] is False
        assert body["filling"] is False
        assert body["configurable"] is False
        assert dataset.release_gated is True
        assert dataset.submission_group == group
        activity = Activity.objects.filter(verb="library.dataset.pool.create").latest("pk")
        assert activity.target_object_id == str(dataset.pk)
        assert activity.metadata == {}

    def test_group_name_collision_takes_a_hash_tag(self, author, dataset):
        Group.objects.create(name="Teaching pool — contributors")
        body = _configure(_client(author), dataset)
        assert body["group_name"] == f"Teaching pool — contributors ({dataset.object_hash[:8].lower()})"

    def test_refused_on_a_dataset_with_members_or_an_unknown_profile(self, author, dataset):
        client = _client(author)
        assert post_json(client, _pool_url(dataset), {"profile": "nope"}).status_code == 400
        _member(dataset, author)
        assert client.get(_pool_url(dataset)).json()["configurable"] is False
        assert post_json(client, _pool_url(dataset), {"profile": "test.pool"}).status_code == 409
        assert Group.objects.count() == 0
        dataset.refresh_from_db()
        assert dataset.submission_profile == ""

    def test_refused_twice(self, author, dataset):
        client = _client(author)
        _configure(client, dataset)
        assert post_json(client, _pool_url(dataset), {"profile": "test.other"}).status_code == 409
        assert Group.objects.count() == 1

    def test_only_the_author_or_a_superuser(self, author, dataset, make_user, superuser):
        grantee = make_user()
        AccessRight.objects.create(
            content_type=ContentType.objects.get_for_model(Dataset),
            object_id=str(dataset.pk),
            access_giver=author,
            access_target=grantee,
            can_read=True,
            can_write=True,
        )
        grantee_client = _client(grantee)
        assert grantee_client.get(_pool_url(dataset)).status_code == 403
        assert post_json(grantee_client, _pool_url(dataset), {"profile": "test.pool"}).status_code == 403
        assert Client().get(_pool_url(dataset)).status_code == 401
        _configure(_client(superuser), dataset)


class TestIntakeAndProfile:
    def test_intake_opens_and_closes_in_every_state(self, author, dataset, make_user):
        client = _client(author)
        _configure(client, dataset)
        assert patch_json(client, _pool_url(dataset), {"open": True}).json()["open"] is True
        _fill(dataset, make_user())
        assert patch_json(client, _pool_url(dataset), {"open": False}).json()["open"] is False
        assert patch_json(client, _pool_url(dataset), {"open": True}).json()["open"] is True
        activity = Activity.objects.filter(verb="library.dataset.pool.update").latest("pk")
        assert activity.metadata == {"fields_updated": ["submissions_open"]}

    def test_profile_changes_until_the_pool_fills(self, author, dataset, make_user):
        client = _client(author)
        _configure(client, dataset)
        assert patch_json(client, _pool_url(dataset), {"profile": "test.other"}).json()["profile"] == "test.other"
        assert patch_json(client, _pool_url(dataset), {"profile": "nope"}).status_code == 400
        _fill(dataset, make_user())
        assert patch_json(client, _pool_url(dataset), {"profile": "test.pool"}).status_code == 409
        dataset.refresh_from_db()
        assert dataset.submission_profile == "test.other"

    def test_not_a_pool(self, author, dataset):
        assert patch_json(_client(author), _pool_url(dataset), {"open": True}).status_code == 409

    def test_intake_opens_only_once_the_group_has_m_active_members(self, author, dataset, make_user):
        register_ingest_profile(IngestProfile(key="test.pool", m=2))
        client = _client(author)
        _configure(client, dataset)
        state = client.get(_pool_url(dataset)).json()
        assert (state["contributor_count"], state["contributors_required"]) == (0, 2)
        dataset.submission_group.user_set.add(make_user(), make_user(is_active=False))
        response = patch_json(client, _pool_url(dataset), {"open": True})
        assert response.status_code == 409
        assert "at least 2" in response.json()["detail"]
        dataset.submission_group.user_set.add(make_user())
        assert patch_json(client, _pool_url(dataset), {"open": True}).json()["open"] is True
        assert patch_json(client, _pool_url(dataset), {"open": False}).json()["open"] is False

    def test_closing_is_never_refused(self, author, dataset, make_user):
        client = _client(author)
        _configure(client, dataset)
        assert patch_json(client, _pool_url(dataset), {"open": True}).json()["open"] is True
        register_ingest_profile(IngestProfile(key="test.pool", m=5))
        assert patch_json(client, _pool_url(dataset), {"open": False}).json()["open"] is False


class TestDissolve:
    def test_before_filling_removes_the_configuration_and_the_group(self, author, dataset, make_user):
        client = _client(author)
        group_id = _configure(client, dataset)["group_id"]
        make_user().groups.add(group_id)
        response = delete_json(client, _pool_url(dataset))
        assert response.status_code == 200, response.content
        assert response.json()["profile"] is None
        assert response.json()["configurable"] is True
        dataset.refresh_from_db()
        assert (dataset.submission_profile, dataset.submission_group, dataset.release_gated) == ("", None, False)
        assert not Group.objects.filter(pk=group_id).exists()
        activity = Activity.objects.filter(verb="library.dataset.pool.delete").latest("pk")
        assert activity.metadata == {"member_count": 1}

    def test_refused_once_filling(self, author, dataset, make_user):
        client = _client(author)
        _configure(client, dataset)
        _fill(dataset, make_user())
        assert delete_json(client, _pool_url(dataset)).status_code == 409
        dataset.refresh_from_db()
        assert dataset.submission_profile == "test.pool"
        assert dataset.submission_group is not None


class TestFillingPool:
    def test_totals_over_every_ledger_never_per_contributor(self, author, dataset, make_user):
        client = _client(author)
        _configure(client, dataset)
        first = _fill(dataset, make_user(), files=2)
        _fill(dataset, make_user(), files=1)
        SubmissionFile.objects.filter(pk=first.files.first().pk).update(status=SubmissionFile.Status.FAILED)
        SubmissionLedger.objects.filter(pk=first.pk).update(ingested_count=4)
        body = client.get(_pool_url(dataset)).json()
        assert body["filling"] is True
        assert (body["pending_count"], body["failed_count"], body["ingested_count"]) == (2, 1, 4)
        assert "ledgers" not in body and "contributors" not in body

    def test_the_gate_is_locked_on_a_pool(self, author, dataset):
        client = _client(author)
        _configure(client, dataset)
        url = f"{DATASETS}/{dataset.object_hash}/"
        assert patch_json(client, url, {"release_gated": False}).status_code == 409
        assert patch_json(client, url, {"release_gated": True}).status_code == 200
        dataset.refresh_from_db()
        assert dataset.release_gated is True

    def test_members_are_neither_added_nor_removed_by_hand(self, author, dataset, make_user):
        client = _client(author)
        _configure(client, dataset)
        item = _member(dataset, author)
        response = post_json(
            client,
            f"{DATASETS}/{dataset.object_hash}/items/",
            {"content_type_id": ContentType.objects.get_for_model(Recording).pk, "object_id": item.object_id},
        )
        assert response.status_code == 409
        assert client.delete(f"{DATASETS}/{dataset.object_hash}/items/{item.pk}/").status_code == 409
        assert DatasetItem.objects.filter(pk=item.pk).exists()

    def test_deletion(self, author, dataset, make_user):
        client = _client(author)
        url = f"{DATASETS}/{dataset.object_hash}/"
        _configure(client, dataset)
        assert client.delete(url).status_code == 409
        ledger = _fill(dataset, make_user(), files=1)
        assert client.delete(url).status_code == 409
        ledger.files.all().delete()
        item = _member(dataset, author)
        assert client.delete(url).status_code == 409
        item.delete()
        assert client.delete(url).status_code == 200
        dataset.refresh_from_db()
        assert dataset.deleted_at is not None

    def test_a_plain_gated_dataset_keeps_its_switch(self, author, dataset):
        url = f"{DATASETS}/{dataset.object_hash}/"
        client = _client(author)
        assert patch_json(client, url, {"release_gated": True}).status_code == 200
        assert patch_json(client, url, {"release_gated": False}).status_code == 200
        assert "submission_group_id" not in client.get(url).json()


class TestDedicatedGroup:
    @pytest.fixture
    def pool_group(self, author, dataset):
        return Group.objects.get(pk=_configure(_client(author), dataset)["group_id"])

    def test_no_access_grant_targets_it(self, author, dataset, pool_group, make_user):
        other = Dataset.objects.create(author=author, name="Other")
        response = post_json(
            _client(author),
            f"{DATASETS}/{other.object_hash}/access/",
            {"access_target_group_id": pool_group.pk, "can_read": True},
        )
        assert response.status_code == 400
        assert not AccessRight.objects.filter(access_target_group=pool_group).exists()

    def test_no_upload_grants_it_access(self, author, pool_group, tmp_path):
        staging = tmp_path / "staging"
        staging.mkdir()
        with override_settings(RECORDINGS_STAGING_PATH=str(staging), RECORDINGS_UPLOAD_PATH=str(tmp_path / "up")):
            f = io.BytesIO(b"data")
            f.name = "test.edf"
            qs = urlencode({"group_access": f"{pool_group.pk}:r"})
            response = _client(author).post(f"/recordings/api/v1/upload?{qs}", {"file": f}, format="multipart")
        assert response.status_code == 400
        assert Recording.objects.count() == 0

    def test_left_out_of_the_grant_target_listing(self, author, pool_group):
        Group.objects.create(name="Readers")
        names = [row["name"] for row in _client(author).get("/api/v1/user/groups").json()]
        assert names == ["Readers"]

    def test_admin_page_names_the_pool_refuses_roles_and_deletion(self, superuser, dataset, pool_group):
        from user.roles import RoleProvider, clear_role_providers, get_role_providers, register_role_provider

        saved = get_role_providers()
        store: dict[int, str] = {}
        register_role_provider(
            RoleProvider(
                key="test_role",
                label="Test role",
                choices=(("captain", "Captain"),),
                read_groups=lambda groups: {g.pk: store[g.pk] for g in groups if g.pk in store},
                write_group=lambda group, value: (
                    store.pop(group.pk, None) if value is None else store.__setitem__(group.pk, value)
                ),
            )
        )
        try:
            client = _client(superuser)
            (row,) = [row for row in client.get(GROUPS).json() if row["id"] == pool_group.pk]
            assert row["dedicated_to"] == {
                "kind": "submission_pool",
                "object_hash": dataset.object_hash,
                "name": "Teaching pool",
            }
            url = f"{GROUPS}/{pool_group.pk}"
            assert patch_json(client, url, {"roles": {"test_role": "captain"}}).status_code == 409
            assert store == {}
            assert patch_json(client, url, {"roles": {"test_role": None}}).status_code == 200
            assert patch_json(client, url, {"name": "Renamed"}).json()["dedicated_to"]["kind"] == "submission_pool"
            assert client.delete(url).status_code == 409
            assert Group.objects.filter(pk=pool_group.pk).exists()
        finally:
            clear_role_providers()
            for provider in saved:
                register_role_provider(provider)

    def test_a_hard_deleted_pool_takes_its_group(self, dataset, pool_group):
        dataset.delete()
        assert not Group.objects.filter(pk=pool_group.pk).exists()

    def test_check_reports_a_grant_acquired_another_way(self, author, dataset, pool_group):
        assert check_pool_groups_grant_nothing(None, databases=["default"]) == []
        assert check_pool_groups_grant_nothing(None) == []
        AccessRight.objects.create(
            content_type=ContentType.objects.get_for_model(Dataset),
            object_id=str(dataset.pk),
            access_giver=author,
            access_target_group=pool_group,
            can_read=True,
        )
        (warning,) = check_pool_groups_grant_nothing(None, databases=["default"])
        assert warning.id == "library.W001"
        assert "an access grant" in warning.msg
