"""Tests for the public dataset listing, which names the datasets their authors list through a share token.

The listing publishes a token on the platform's own authority, so it is narrower than sharing by link: an ungated
dataset of recordings that each name the published dataset they came from, through a de-identifying token. The rule
is applied when a token is listed and again on every read, which is what these tests pin.
"""

import json
from datetime import timedelta

import pytest
from django.contrib.contenttypes.models import ContentType
from django.test import Client, override_settings
from django.utils import timezone
from model_bakery import baker

from activity.audit import rollback_change, rollback_refusal
from activity.models import Activity, ObjectChangeLog
from epicurrents.models import AccessRight
from library.models import Dataset, DatasetItem

DATASETS_URL = "/api/v1/library/datasets/"
PUBLIC_URL = "/api/v1/library/public-datasets/"
SOURCE = "10.1234/published-eeg"
TOKEN = "listed-token-0123456789"

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("listing_on")]


@pytest.fixture
def listing_on(settings):
    settings.LIBRARY_PUBLIC_DATASET_LISTING = True


def _recording(author, public_source=SOURCE, **kwargs):
    return baker.make(
        "recordings.Recording",
        author=author,
        status="READY",
        modality="eeg",
        public_source=public_source,
        **kwargs,
    )


def _add(dataset, obj):
    content_type = ContentType.objects.get_for_model(obj, for_concrete_model=False)
    DatasetItem.objects.create(dataset=dataset, content_type=content_type, object_id=str(obj.pk))


def _token(dataset, token=TOKEN, **kwargs):
    fields = {"can_read": True, "apply_middleware": True, **kwargs}
    return AccessRight.objects.create(
        content_type=ContentType.objects.get_for_model(Dataset, for_concrete_model=False),
        object_id=str(dataset.pk),
        access_giver=dataset.author,
        public_share_token=token,
        **fields,
    )


@pytest.fixture
def dataset(user):
    dataset = Dataset.objects.create(author=user, name="published-eeg")
    _add(dataset, _recording(user))
    _add(dataset, _recording(user))
    return dataset


def _put(client, dataset, right):
    return client.put(
        f"{DATASETS_URL}{dataset.object_hash}/public-listing/",
        json.dumps({"access_right_id": right.pk}),
        content_type="application/json",
    )


def _listed_hashes():
    resp = Client().get(PUBLIC_URL)
    assert resp.status_code == 200, resp.content
    return [entry["object_hash"] for entry in resp.json()]


def _codes(resp):
    return {problem["code"] for problem in resp.json()["problems"]}


class TestListing:
    def test_a_listed_dataset_is_named_to_anyone_with_its_token(self, auth_client, dataset):
        c, _user = auth_client
        right = _token(dataset)
        resp = _put(c, dataset, right)
        assert resp.status_code == 200, resp.content
        assert resp.json()["listed"] is True
        resp = Client().get(PUBLIC_URL)
        assert resp.status_code == 200, resp.content
        (entry,) = resp.json()
        assert entry["object_hash"] == dataset.object_hash
        assert entry["share_token"] == TOKEN
        assert entry["recording_count"] == 2
        assert entry["modalities"] == ["eeg"]
        assert entry["public_sources"] == [SOURCE]
        assert "id" not in entry and "author_id" not in entry

    def test_the_listed_token_reads_the_browser(self, auth_client, dataset):
        c, _user = auth_client
        _put(c, dataset, _token(dataset))
        resp = Client().get(f"{DATASETS_URL}{dataset.object_hash}/browse/?share_token={TOKEN}")
        assert resp.status_code == 200, resp.content
        assert resp.json()["count"] == 2

    def test_cleared_listing_names_nothing_and_keeps_the_token(self, auth_client, dataset):
        c, _user = auth_client
        right = _token(dataset)
        _put(c, dataset, right)
        resp = c.delete(f"{DATASETS_URL}{dataset.object_hash}/public-listing/")
        assert resp.status_code == 200, resp.content
        assert resp.json()["access_right_id"] is None
        assert _listed_hashes() == []
        assert AccessRight.objects.filter(pk=right.pk).exists()

    def test_revoking_the_token_unlists(self, auth_client, dataset):
        c, _user = auth_client
        right = _token(dataset)
        _put(c, dataset, right)
        resp = c.delete(f"{DATASETS_URL}{dataset.object_hash}/access/{right.pk}/")
        assert resp.status_code in (200, 204), resp.content
        dataset.refresh_from_db()
        assert dataset.public_listing is None
        assert _listed_hashes() == []
        change = ObjectChangeLog.objects.filter(
            content_type=ContentType.objects.get_for_model(Dataset), object_id=str(dataset.pk)
        ).latest("pk")
        assert change.changes["public_listing_id"] == {"from": right.pk, "to": None}

    def test_listing_through_another_token_replaces_the_first(self, auth_client, dataset):
        c, _user = auth_client
        _put(c, dataset, _token(dataset))
        second = _token(dataset, token="second-token-0123456789")
        assert _put(c, dataset, second).status_code == 200
        (entry,) = Client().get(PUBLIC_URL).json()
        assert entry["share_token"] == "second-token-0123456789"


class TestRefusals:
    def test_an_unsourced_recording_is_refused_and_counted(self, auth_client, dataset, user):
        c, _user = auth_client
        _add(dataset, _recording(user, public_source=""))
        resp = _put(c, dataset, _token(dataset))
        assert resp.status_code == 409, resp.content
        dataset.refresh_from_db()
        assert dataset.public_listing is None
        status = c.get(f"{DATASETS_URL}{dataset.object_hash}/public-listing/").json()
        assert status["problems"] == [
            {"code": "unsourced_recordings", "detail": status["problems"][0]["detail"], "count": 1}
        ]

    def test_a_raw_token_is_refused(self, auth_client, dataset):
        c, _user = auth_client
        assert _put(c, dataset, _token(dataset, apply_middleware=False)).status_code == 409

    def test_an_expired_token_is_refused(self, auth_client, dataset):
        c, _user = auth_client
        right = _token(dataset, expires_at=timezone.now() - timedelta(days=1))
        assert _put(c, dataset, right).status_code == 409

    def test_a_user_grant_is_refused(self, auth_client, dataset, make_user):
        c, _user = auth_client
        right = AccessRight.objects.create(
            content_type=ContentType.objects.get_for_model(Dataset, for_concrete_model=False),
            object_id=str(dataset.pk),
            access_giver=dataset.author,
            access_target=make_user(),
        )
        assert _put(c, dataset, right).status_code == 409

    def test_another_datasets_token_is_404(self, auth_client, dataset, user):
        c, _user = auth_client
        other = Dataset.objects.create(author=user, name="other")
        assert _put(c, dataset, _token(other)).status_code == 404

    def test_a_non_recording_member_is_refused(self, auth_client, dataset, user):
        c, _user = auth_client
        _add(dataset, Dataset.objects.create(author=user, name="nested"))
        resp = _put(c, dataset, _token(dataset))
        assert resp.status_code == 409
        assert "other_members" in _codes(c.get(f"{DATASETS_URL}{dataset.object_hash}/public-listing/"))

    def test_a_recording_with_attached_media_is_refused_and_unlists(self, auth_client, dataset, user):
        from media.models import MediaFile
        from recordings.models import Recording

        c, _user = auth_client
        assert _put(c, dataset, _token(dataset)).status_code == 200
        assert _listed_hashes() == [dataset.object_hash]
        recording = Recording.objects.filter(author=user).first()
        baker.make(
            MediaFile,
            author=user,
            content_hash="AB" * 16,
            file_size=1,
            attachment_content_type=ContentType.objects.get_for_model(Recording, for_concrete_model=False),
            attachment_object_id=str(recording.pk),
        )
        # The token would read the attachment through the recording, as stored.
        assert _listed_hashes() == []
        resp = c.get(f"{DATASETS_URL}{dataset.object_hash}/public-listing/")
        assert {"code": "attached_media", "count": 1} in [
            {"code": problem["code"], "count": problem["count"]} for problem in resp.json()["problems"]
        ]

    def test_an_empty_dataset_is_refused(self, auth_client, user):
        c, _user = auth_client
        empty = Dataset.objects.create(author=user, name="empty")
        assert _put(c, empty, _token(empty)).status_code == 409

    def test_a_gated_dataset_is_refused(self, auth_client, dataset):
        c, _user = auth_client
        right = _token(dataset)
        Dataset.objects.filter(pk=dataset.pk).update(release_gated=True)
        assert _put(c, dataset, right).status_code == 409

    @override_settings(LIBRARY_PUBLIC_DATASET_LISTING=False)
    def test_refused_and_not_served_when_the_deployment_does_not_list(self, auth_client, dataset):
        c, _user = auth_client
        assert _put(c, dataset, _token(dataset)).status_code == 409
        assert Client().get(PUBLIC_URL).status_code == 404


class TestRecheckedOnRead:
    """The rule is applied on every read, so a dataset stops qualifying without anyone having to notice."""

    def test_a_member_added_without_a_source_unlists_until_fixed(self, auth_client, dataset, user):
        c, _user = auth_client
        _put(c, dataset, _token(dataset))
        unsourced = _recording(user, public_source="")
        _add(dataset, unsourced)
        assert _listed_hashes() == []
        assert c.get(f"{DATASETS_URL}{dataset.object_hash}/public-listing/").json()["listed"] is False
        unsourced.public_source = SOURCE
        unsourced.save(update_fields=["public_source"])
        assert _listed_hashes() == [dataset.object_hash]

    def test_an_expired_token_unlists(self, auth_client, dataset):
        c, _user = auth_client
        right = _token(dataset)
        _put(c, dataset, right)
        AccessRight.objects.filter(pk=right.pk).update(expires_at=timezone.now() - timedelta(minutes=1))
        assert _listed_hashes() == []

    def test_a_trashed_dataset_unlists(self, auth_client, dataset):
        c, _user = auth_client
        _put(c, dataset, _token(dataset))
        Dataset.objects.filter(pk=dataset.pk).update(deleted_at=timezone.now())
        assert _listed_hashes() == []

    def test_a_value_set_around_the_endpoint_publishes_nothing_ineligible(self, dataset):
        right = _token(dataset, apply_middleware=False)
        Dataset.objects.filter(pk=dataset.pk).update(public_listing=right)
        assert _listed_hashes() == []


class TestAuthority:
    def test_a_write_grantee_may_not_list(self, dataset, make_user):
        grantee = make_user()
        AccessRight.objects.create(
            content_type=ContentType.objects.get_for_model(Dataset, for_concrete_model=False),
            object_id=str(dataset.pk),
            access_giver=dataset.author,
            access_target=grantee,
            can_write=True,
            can_share=True,
        )
        c = Client()
        c.force_login(grantee)
        assert _put(c, dataset, _token(dataset)).status_code == 403
        assert c.get(f"{DATASETS_URL}{dataset.object_hash}/public-listing/").status_code == 403

    def test_anonymous_may_not_list(self, dataset):
        assert _put(Client(), dataset, _token(dataset)).status_code == 401

    def test_a_superuser_may_list(self, superuser_client, dataset):
        c, _admin = superuser_client
        assert _put(c, dataset, _token(dataset)).status_code == 200


class TestTokenReach:
    """A listed token's reader is anyone, so what it reads beyond the listing entry names no account and no day."""

    def test_the_dataset_is_read_without_its_author_or_its_exact_times(self, auth_client, dataset):
        c, user = auth_client
        _put(c, dataset, _token(dataset))
        body = Client().get(f"{DATASETS_URL}{dataset.object_hash}/?share_token={TOKEN}").json()
        assert body["author_id"] is None
        for key in ("created_at", "modified_at"):
            assert body[key].startswith(timezone.now().strftime("%Y-%m-01T00:00:00")), body[key]
        signed_in = c.get(f"{DATASETS_URL}{dataset.object_hash}/").json()
        assert signed_in["author_id"] == user.pk
        assert not signed_in["created_at"].startswith(timezone.now().strftime("%Y-%m-01T00:00:00.000"))

    def test_the_members_are_listed_without_keys_or_the_day_they_were_added(self, auth_client, dataset):
        c, _user = auth_client
        _put(c, dataset, _token(dataset))
        rows = Client().get(f"{DATASETS_URL}{dataset.object_hash}/items/?share_token={TOKEN}").json()
        assert len(rows) == 2
        for row in rows:
            assert row["id"] is None and "object_id" not in row
            assert row["object_hash"]
            assert row["added_at"].startswith(timezone.now().strftime("%Y-%m-01T00:00:00")), row["added_at"]
        own = c.get(f"{DATASETS_URL}{dataset.object_hash}/items/").json()
        assert all(row["id"] is not None and "object_id" not in row for row in own)

    def test_folders_and_snapshots_follow_the_dataset(self, auth_client, dataset, user):
        from library.models import DatasetFolder

        c, _user = auth_client
        _put(c, dataset, _token(dataset))
        DatasetFolder.objects.create(dataset=dataset, name="folder")
        created = c.post(
            f"{DATASETS_URL}{dataset.object_hash}/snapshots/",
            json.dumps({"label": "v1"}),
            content_type="application/json",
        )
        assert created.status_code == 201, created.content
        month = timezone.now().strftime("%Y-%m-01T00:00:00")
        anyone = Client()
        (folder,) = anyone.get(f"{DATASETS_URL}{dataset.object_hash}/folders/?share_token={TOKEN}").json()
        assert folder["created_at"].startswith(month) and folder["modified_at"].startswith(month)
        (snapshot,) = anyone.get(f"{DATASETS_URL}{dataset.object_hash}/snapshots/?share_token={TOKEN}").json()
        assert snapshot["author_id"] is None and snapshot["created_at"].startswith(month)
        detail = anyone.get(f"{DATASETS_URL}snapshots/{snapshot['object_hash']}/?share_token={TOKEN}").json()
        assert detail["author_id"] is None and detail["created_at"].startswith(month)
        assert c.get(f"{DATASETS_URL}{dataset.object_hash}/snapshots/").json()[0]["author_id"] == user.pk

    def test_a_signed_in_reader_holding_the_token_keeps_the_keys(self, auth_client, dataset, make_user):
        c, _user = auth_client
        _put(c, dataset, _token(dataset))
        reader = Client()
        reader.force_login(make_user())
        body = reader.get(f"{DATASETS_URL}{dataset.object_hash}/?share_token={TOKEN}").json()
        assert body["author_id"] == dataset.author_id


def _latest_change(dataset):
    content_type = ContentType.objects.get_for_model(Dataset)
    return ObjectChangeLog.objects.filter(content_type=content_type, object_id=str(dataset.pk)).latest("pk")


class TestRollback:
    """The rollback API restores a recorded state past the endpoints, so the listing has a rollback guard."""

    def _unlisted(self, client, dataset):
        """List and unlist *dataset*; the change that cleared the listing."""
        right = _token(dataset)
        assert _put(client, dataset, right).status_code == 200
        assert client.delete(f"{DATASETS_URL}{dataset.object_hash}/public-listing/").status_code == 200
        return right, _latest_change(dataset)

    def test_a_write_grantee_cannot_relist_by_rolling_back_the_unlisting(self, auth_client, dataset, make_user):
        c, _user = auth_client
        _right, cleared = self._unlisted(c, dataset)
        grantee = make_user()
        AccessRight.objects.create(
            content_type=ContentType.objects.get_for_model(Dataset, for_concrete_model=False),
            object_id=str(dataset.pk),
            access_giver=dataset.author,
            access_target=grantee,
            can_read=True,
            can_write=True,
        )
        assert cleared.before_state["public_listing_id"] is not None
        with pytest.raises(ValueError, match="public listing"):
            rollback_change(user=grantee, change_id=cleared.pk)
        dataset.refresh_from_db()
        assert dataset.public_listing_id is None
        assert _listed_hashes() == []

    def test_nor_the_author(self, auth_client, dataset):
        c, user = auth_client
        _right, cleared = self._unlisted(c, dataset)
        with pytest.raises(ValueError, match="public listing"):
            rollback_change(user=user, change_id=cleared.pk)

    def test_a_state_listed_through_a_revoked_token_is_refused_not_restored(self, auth_client, dataset):
        c, user = auth_client
        right, cleared = self._unlisted(c, dataset)
        right.delete()
        assert rollback_refusal(cleared) is not None
        with pytest.raises(ValueError, match="public listing"):
            rollback_change(user=user, change_id=cleared.pk)

    def test_a_rename_that_leaves_the_listing_alone_still_rolls_back(self, auth_client, dataset):
        c, user = auth_client
        assert _put(c, dataset, _token(dataset)).status_code == 200
        resp = c.patch(
            f"{DATASETS_URL}{dataset.object_hash}/", json.dumps({"name": "renamed"}), content_type="application/json"
        )
        assert resp.status_code == 200, resp.content
        rename = _latest_change(dataset)
        assert rollback_refusal(rename) is None
        rollback_change(user=user, change_id=rename.pk)
        dataset.refresh_from_db()
        assert dataset.name == "published-eeg"
        assert _listed_hashes() == [dataset.object_hash]


class TestAudit:
    def test_listing_is_recorded_on_the_dataset(self, auth_client, dataset):
        c, _user = auth_client
        right = _token(dataset)
        _put(c, dataset, right)
        assert Activity.objects.filter(verb="library.dataset.listing.set").exists()
        change = ObjectChangeLog.objects.filter(
            content_type=ContentType.objects.get_for_model(Dataset), object_id=str(dataset.pk)
        ).latest("pk")
        assert change.changes["public_listing_id"] == {"from": None, "to": right.pk}

    def test_an_anonymous_read_of_the_listing_is_recorded(self, auth_client, dataset):
        c, _user = auth_client
        _put(c, dataset, _token(dataset))
        Client().get(PUBLIC_URL)
        activity = Activity.objects.filter(verb="library.dataset.public.list").get()
        assert activity.actor is None
        assert activity.metadata == {"returned_count": 1}
