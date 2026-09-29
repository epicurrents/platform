"""The release gate acts only on memberships the gating party controls, and nothing sequential reaches a reader.

Covers the scope rule in ``library.release.controls_membership`` (a gated dataset hides its author's own members
and, in a pool, the pooled recordings; a third party gating someone else's recording hides nothing), the refusals
that keep uncontrolled members out of gated datasets, media members under the same gate, gated members in ungated
listings, the null ``id``/``object_id`` for readers, and pooled recordings dated and withheld by what they are
rather than by where they currently sit.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from django.contrib.contenttypes.models import ContentType
from django.test import Client
from model_bakery import baker

from conftest import patch_json, post_json
from epicurrents.permissions import can_read_object
from epicurrents.system_user import get_system_user
from library.models import Dataset, Tag, TaggedItem
from library.release import WITHHELD_DATE, member_hidden_from_reader, stored_digest_withheld
from library.tests.test_release import HASHES, _add, _gated_dataset, _grant, _recording, _release
from recordings.models import Recording

pytestmark = pytest.mark.django_db

DATASETS = "/api/v1/library/datasets"
ITEM_KEYS = {
    "id",
    "content_type_id",
    "object_id",
    "added_at",
    "folder_id",
    "release_month",
    "object_name",
    "object_hash",
    "object_type",
    "media_type",
    "file_extension",
    "is_supported",
}


def _client(user) -> Client:
    client = Client()
    client.force_login(user)
    return client


def _pool(author, *members):
    dataset, items = _gated_dataset(author, *members)
    Dataset.objects.filter(pk=dataset.pk).update(submission_profile="test.pool")
    dataset.refresh_from_db()
    return dataset, items


def _pooled(index=0, **fields):
    return _recording(get_system_user(), index=index, **fields)


def _recording_ct():
    return ContentType.objects.get_for_model(Recording, for_concrete_model=False)


class TestThirdPartyGate:
    def test_a_reader_cannot_add_a_pool_member_to_a_gated_dataset_or_gate_one_holding_it(self, make_user):
        """The review reproduction: a reader of a released pool member tries to hide it by gating their own dataset."""
        curator, reader, other = make_user(), make_user(), make_user()
        recording = _pooled()
        pool, items = _pool(curator, recording)
        _release(pool, *items)
        _grant(pool, curator, target=reader)
        _grant(pool, curator, target=other)
        client = _client(reader)
        mine = post_json(client, f"{DATASETS}/", {"name": "mine"}).json()["object_hash"]
        item = {"content_type_id": _recording_ct().pk, "object_id": str(recording.pk)}
        assert post_json(client, f"{DATASETS}/{mine}/items/", item).status_code == 201
        assert patch_json(client, f"{DATASETS}/{mine}/", {"release_gated": True}).status_code == 409
        gated = post_json(client, f"{DATASETS}/", {"name": "gated"}).json()["object_hash"]
        assert patch_json(client, f"{DATASETS}/{gated}/", {"release_gated": True}).status_code == 200
        assert post_json(client, f"{DATASETS}/{gated}/items/", item).status_code == 409
        assert can_read_object(user=other, obj=recording) is True

    def test_a_third_partys_gated_dataset_hides_nothing_it_does_not_own(self, make_user):
        """Defence in depth: a gated membership set up past the endpoints hides nothing of someone else's recording."""
        owner, reader, meddler = make_user(), make_user(), make_user()
        recording = _recording(owner)
        _grant(recording, owner, target=reader)
        meddling, _ = _gated_dataset(meddler, recording)
        assert member_hidden_from_reader(reader, recording) is False
        assert can_read_object(user=reader, obj=recording) is True
        assert stored_digest_withheld(reader, recording) is False
        # A pooled recording in a gated dataset that is not its pool is not hidden by it either.
        pooled = _pooled(index=1)
        pool, items = _pool(owner, pooled)
        _release(pool, *items)
        _grant(pool, owner, target=reader)
        _add(meddling, pooled)
        assert can_read_object(user=reader, obj=pooled) is True

    def test_even_a_superuser_adds_only_the_dataset_authors_objects(self, make_user, superuser):
        author, other = make_user(), make_user()
        dataset, _ = _gated_dataset(author)
        foreign = _recording(other, index=0)
        own = _recording(author, index=1)
        client = _client(superuser)
        url = f"{DATASETS}/{dataset.object_hash}/items/"
        ct_id = _recording_ct().pk
        assert post_json(client, url, {"content_type_id": ct_id, "object_id": str(foreign.pk)}).status_code == 409
        assert post_json(client, url, {"content_type_id": ct_id, "object_id": str(own.pk)}).status_code == 201

    def test_the_author_gates_a_dataset_of_their_own_recordings(self, make_user):
        author = make_user()
        dataset = Dataset.objects.create(author=author, name="own")
        _add(dataset, _recording(author))
        response = patch_json(_client(author), f"{DATASETS}/{dataset.object_hash}/", {"release_gated": True})
        assert response.status_code == 200 and response.json()["release_gated"] is True


class TestMediaMembers:
    def _media(self, author, **fields):
        from media.models import MediaFile

        return baker.make(MediaFile, author=author, stored_name="M" * 32, file_size=1, **fields)

    def test_an_unreleased_media_member_is_hidden_from_a_dataset_grantee(self, make_user):
        curator, viewer = make_user(), make_user()
        media = self._media(curator)
        dataset, items = _gated_dataset(curator, media)
        _grant(dataset, curator, target=viewer)
        assert can_read_object(user=viewer, obj=media) is False
        _release(dataset, *items)
        assert can_read_object(user=viewer, obj=media) is True

    def test_a_released_media_member_does_not_resolve_for_a_share_token(self, make_user):
        curator = make_user()
        media = self._media(curator)
        dataset, items = _gated_dataset(curator, media)
        _release(dataset, *items)
        _grant(dataset, curator, token="Q" * 32)
        assert can_read_object(user=None, obj=media, share_token="Q" * 32) is False

    def test_the_media_gate_is_registered(self):
        from epicurrents.permissions import _READ_VISIBILITY_GATES

        assert member_hidden_from_reader in _READ_VISIBILITY_GATES["media.mediafile"]


class TestGatedMembersInOtherListings:
    def test_an_unreleased_member_is_not_listed_through_an_ungated_dataset(self, make_user):
        """The review reproduction: a manager files an unreleased pooled recording in an ungated, shared dataset."""
        curator, viewer = make_user(), make_user()
        recording = _pooled(index=1)
        _pool(curator, recording)
        side = Dataset.objects.create(author=curator, name="side")
        _add(side, recording)
        _add(side, _recording(curator, index=2))
        _grant(side, curator, target=viewer)
        _grant(side, curator, token="T" * 32)
        hashes = [row["object_hash"] for row in _client(viewer).get(f"{DATASETS}/{side.object_hash}/items/").json()]
        assert hashes == [HASHES[2]]
        rows = Client().get(f"{DATASETS}/{side.object_hash}/items/", {"share_token": "T" * 32}).json()
        assert [row["object_hash"] for row in rows] == [HASHES[2]]
        managed = [row["object_hash"] for row in _client(curator).get(f"{DATASETS}/{side.object_hash}/items/").json()]
        assert sorted(managed) == [HASHES[1], HASHES[2]]

    def test_a_released_member_is_not_listed_to_a_share_token_through_an_ungated_dataset(self, make_user):
        curator = make_user()
        recording = _recording(curator, index=1)
        pool, items = _gated_dataset(curator, recording)
        _release(pool, *items)
        side = Dataset.objects.create(author=curator, name="side")
        _add(side, recording)
        _grant(side, curator, token="T" * 32)
        assert Client().get(f"{DATASETS}/{side.object_hash}/items/", {"share_token": "T" * 32}).json() == []


class TestSequentialIds:
    def test_a_reader_of_a_gated_dataset_gets_no_item_or_object_id(self, make_user):
        curator, reader = make_user(), make_user()
        pool, items = _pool(curator, _pooled(index=0), _pooled(index=1))
        _release(pool, *items)
        _grant(pool, curator, target=reader)
        rows = _client(reader).get(f"{DATASETS}/{pool.object_hash}/items/").json()
        assert len(rows) == 2
        for row in rows:
            assert set(row) == ITEM_KEYS
            assert row["id"] is None and row["object_id"] is None
            assert row["object_hash"] in HASHES

    def test_a_manager_keeps_both(self, make_user):
        curator, manager = make_user(), make_user()
        pool, items = _pool(curator, _pooled(index=0))
        _grant(pool, curator, target=manager, can_write=True)
        for user in (curator, manager):
            rows = _client(user).get(f"{DATASETS}/{pool.object_hash}/items/").json()
            assert [(row["id"], row["object_id"]) for row in rows] == [(items[0].pk, items[0].object_id)]

    def test_a_pooled_recording_in_an_ungated_dataset_gets_no_ids(self, make_user):
        curator, viewer = make_user(), make_user()
        recording = _pooled(index=0)
        pool, items = _pool(curator, recording)
        _release(pool, *items)
        side = Dataset.objects.create(author=curator, name="side")
        _add(side, recording)
        _grant(side, curator, target=viewer)
        _grant(pool, curator, target=viewer)
        rows = _client(viewer).get(f"{DATASETS}/{side.object_hash}/items/").json()
        assert [(row["id"], row["object_id"], row["object_hash"]) for row in rows] == [(None, None, HASHES[0])]

    def test_tag_listings_null_the_ids_of_pooled_recordings(self, make_user, superuser):
        curator, viewer = make_user(), make_user()
        pooled = _pooled(index=0)
        own = _recording(viewer, index=1)
        pool, items = _pool(curator, pooled)
        _release(pool, *items)
        _grant(pool, curator, target=viewer)
        tag = Tag.objects.create(author=superuser, name="spikes", curated=True)
        for recording in (pooled, own):
            TaggedItem.objects.create(tag=tag, content_type=_recording_ct(), object_id=str(recording.pk))
        rows = _client(viewer).get(f"/api/v1/library/tags/{tag.pk}/items/").json()
        by_object = {row["object_id"]: row for row in rows}
        assert set(by_object) == {None, str(own.pk)}
        assert by_object[None]["id"] is None
        rows = _client(superuser).get(f"/api/v1/library/tags/{tag.pk}/items/").json()
        assert {row["object_id"] for row in rows} == {str(pooled.pk), str(own.pk)}


class TestPooledRecordingsStayPooled:
    DIGEST = "c" * 64

    def _detail(self, user, index=0):
        return _client(user).get(f"/recordings/api/v1/{HASHES[index]}").json()

    def test_trashing_the_pool_keeps_the_digest_withheld_and_the_release_month(self, make_user):
        curator, reader = make_user(), make_user()
        recording = _pooled(created_at=datetime(2026, 7, 14, 9, tzinfo=UTC), stored_hash=self.DIGEST)
        _grant(recording, get_system_user(), target=reader)
        pool, items = _pool(curator, recording)
        _release(pool, *items, on=date(2026, 9, 3))
        Dataset.objects.filter(pk=pool.pk).update(deleted_at=datetime(2026, 9, 20, tzinfo=UTC))
        body = self._detail(reader)
        assert body["stored_hash"] == ""
        assert body["created_at"].startswith("2026-09-01T00:00:00")

    def test_ungating_the_pool_keeps_them_too(self, make_user):
        curator, reader = make_user(), make_user()
        recording = _pooled(created_at=datetime(2026, 7, 14, 9, tzinfo=UTC), stored_hash=self.DIGEST)
        _grant(recording, get_system_user(), target=reader)
        pool, items = _pool(curator, recording)
        _release(pool, *items, on=date(2026, 9, 3))
        Dataset.objects.filter(pk=pool.pk).update(release_gated=False)
        body = self._detail(reader)
        assert body["stored_hash"] == ""
        assert body["created_at"].startswith("2026-09-01T00:00:00")

    def test_a_pooled_recording_never_released_is_not_dated_by_its_ingest(self, make_user):
        curator, reader = make_user(), make_user()
        recording = _pooled(created_at=datetime(2026, 7, 14, 9, tzinfo=UTC))
        _grant(recording, get_system_user(), target=reader)
        pool, _items = _pool(curator, recording)
        Dataset.objects.filter(pk=pool.pk).update(deleted_at=datetime(2026, 9, 20, tzinfo=UTC))
        assert self._detail(reader)["created_at"].startswith(WITHHELD_DATE.date().isoformat())
