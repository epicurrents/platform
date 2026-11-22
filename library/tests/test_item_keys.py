"""Who receives the sequential keys of an item row.

``id`` is a membership's or a tagging's primary key, which counts the deployment's memberships in add order. A
dataset serves it to its managers alone (its author, superusers and the holders of a write grant), gated or not, and
a tag to a superuser and to the author of the tagged object. Everyone else is listed the same rows with it null and
addresses a row by ``object_hash``. The member's own primary key is on no row for any caller, so no row has an
``object_id``. Pinned per reader class on every item-row surface, for a recording and for a member of a registered type
(``Dataset`` registered as one, as in ``test_item_types``).
"""

from __future__ import annotations

import pytest
from django.contrib.contenttypes.models import ContentType
from django.test import Client
from model_bakery import baker

from conftest import post_json
from library import item_types
from library.item_types import ItemType, register_item_type
from library.models import Dataset, DatasetFolder, DatasetItem, Tag, TaggedItem
from library.tests.test_release import HASHES, _add, _grant, _recording
from recordings.models import Recording

pytestmark = pytest.mark.django_db

DATASETS = "/api/v1/library/datasets"
TAGS = "/api/v1/library/tags"
TOKEN = "T" * 32


@pytest.fixture
def casefile(monkeypatch):
    """``Dataset`` registered as a member type that a share token is served."""
    monkeypatch.setattr(item_types, "_TYPES", {})
    register_item_type(
        ItemType(
            object_type="casefile",
            model_label="library.dataset",
            hash_field="object_hash",
            display_name=lambda obj: obj.name,
            normalise_hash=lambda value: value.strip().upper(),
        )
    )
    return ContentType.objects.get_for_model(Dataset, for_concrete_model=False)


def _client(user=None) -> Client:
    client = Client()
    if user is not None:
        client.force_login(user)
    return client


def _recording_ct():
    return ContentType.objects.get_for_model(Recording, for_concrete_model=False)


@pytest.fixture
def shared(make_user, make_superuser, casefile):
    """An ungated dataset holding a recording and a casefile, with one reader of every class."""
    author = make_user()
    holder = Dataset.objects.create(author=author, name="holder")
    _grant(holder, author, target=author, can_write=True, can_share=True)
    recording = _recording(author, index=0)
    member = Dataset.objects.create(author=author, name="member")
    items = {"recording": _add(holder, recording), "casefile": _add(holder, member)}
    reader, writer, stranger = make_user(), make_user(), make_user()
    _grant(holder, author, target=reader)
    _grant(holder, author, target=writer, can_write=True)
    _grant(holder, author, token=TOKEN)
    return {
        "holder": holder,
        "items": items,
        "hashes": {"recording": HASHES[0], "casefile": member.object_hash},
        "objects": {"recording": recording, "casefile": member},
        "author": author,
        "superuser": make_superuser(),
        "reader": reader,
        "writer": writer,
        "stranger": stranger,
    }


def _keys(rows) -> dict:
    """Each row's ``id`` by its object type. No row carries the member's own key, whoever reads it."""
    assert all("object_id" not in row for row in rows)
    return {row["object_type"]: row["id"] for row in rows}


def _expected(shared, *, served: bool) -> dict:
    return {kind: item.pk if served else None for kind, item in shared["items"].items()}


class TestDatasetItemListing:
    @pytest.mark.parametrize(
        ("caller", "served"),
        [("author", True), ("superuser", True), ("writer", True), ("reader", False)],
    )
    def test_the_item_id_goes_to_the_managers_of_an_ungated_dataset_alone(self, shared, caller, served):
        rows = _client(shared[caller]).get(f"{DATASETS}/{shared['holder'].object_hash}/items/").json()
        assert _keys(rows) == _expected(shared, served=served)
        assert {row["object_type"]: row["object_hash"] for row in rows} == shared["hashes"]

    def test_a_share_token_reader_without_an_account_gets_none(self, shared):
        rows = _client().get(f"{DATASETS}/{shared['holder'].object_hash}/items/", {"share_token": TOKEN}).json()
        assert _keys(rows) == _expected(shared, served=False)

    def test_a_signed_in_reader_holding_only_the_token_gets_none(self, shared):
        client = _client(shared["stranger"])
        rows = client.get(f"{DATASETS}/{shared['holder'].object_hash}/items/", {"share_token": TOKEN}).json()
        assert _keys(rows) == _expected(shared, served=False)

    def test_a_manager_presenting_the_token_keeps_it(self, shared):
        client = _client(shared["writer"])
        rows = client.get(f"{DATASETS}/{shared['holder'].object_hash}/items/", {"share_token": TOKEN}).json()
        assert _keys(rows) == _expected(shared, served=True)

    def test_a_group_write_grant_makes_a_manager(self, shared, make_user):
        from django.contrib.auth.models import Group

        member = make_user()
        group = Group.objects.create(name="curators")
        member.groups.add(group)
        _grant(shared["holder"], shared["author"], group=group, can_write=True)
        rows = _client(member).get(f"{DATASETS}/{shared['holder'].object_hash}/items/").json()
        assert _keys(rows) == _expected(shared, served=True)

    def test_a_write_grant_on_another_dataset_does_not(self, shared, make_user):
        elsewhere = make_user()
        _grant(shared["holder"], shared["author"], target=elsewhere)
        other = Dataset.objects.create(author=shared["author"], name="other")
        _grant(other, shared["author"], target=elsewhere, can_write=True)
        rows = _client(elsewhere).get(f"{DATASETS}/{shared['holder'].object_hash}/items/").json()
        assert _keys(rows) == _expected(shared, served=False)

    def test_an_expired_write_grant_does_not(self, shared, make_user):
        from datetime import timedelta

        from django.contrib.auth.models import Group
        from django.utils import timezone

        lapsed = make_user()
        group = Group.objects.create(name="former curators")
        lapsed.groups.add(group)
        _grant(shared["holder"], shared["author"], target=lapsed)
        _grant(
            shared["holder"],
            shared["author"],
            group=group,
            can_write=True,
            expires_at=timezone.now() - timedelta(days=1),
        )
        rows = _client(lapsed).get(f"{DATASETS}/{shared['holder'].object_hash}/items/").json()
        assert _keys(rows) == _expected(shared, served=False)

    def test_a_media_member_follows_the_same_rule(self, shared):
        from media.models import MediaFile

        media = baker.make(MediaFile, author=shared["author"], content_hash="ab" * 32, file_extension=".pdf")
        item = _add(shared["holder"], media)
        url = f"{DATASETS}/{shared['holder'].object_hash}/items/"
        assert _keys(_client(shared["reader"]).get(url).json())["mediafile"] is None
        assert _keys(_client(shared["author"]).get(url).json())["mediafile"] == item.pk


class TestDatasetItemWrites:
    """Adding and placing take write access, so the caller is a manager and the row keeps its item id."""

    @pytest.mark.parametrize("caller", ["author", "superuser", "writer"])
    @pytest.mark.parametrize("kind", ["recording", "casefile"])
    def test_an_add_response_carries_the_item_id(self, shared, caller, kind, casefile):
        target = Dataset.objects.create(author=shared["author"], name="target")
        _grant(target, shared["author"], target=shared["writer"], can_write=True)
        # The writer reads the member through the dataset that already holds it.
        content_type = _recording_ct() if kind == "recording" else casefile
        response = post_json(
            _client(shared[caller]),
            f"{DATASETS}/{target.object_hash}/items/",
            {"content_type_id": content_type.pk, "object_id": shared["hashes"][kind]},
        )
        assert response.status_code == 201, response.content
        row = response.json()
        item = DatasetItem.objects.get(dataset=target)
        assert (row["id"], row["object_hash"]) == (item.pk, shared["hashes"][kind])
        assert "object_id" not in row

    @pytest.mark.parametrize("caller", ["author", "superuser", "writer"])
    @pytest.mark.parametrize("kind", ["recording", "casefile"])
    def test_a_move_response_carries_the_item_id(self, shared, caller, kind):
        folder = DatasetFolder.objects.create(dataset=shared["holder"], name="folder")
        item = shared["items"][kind]
        response = post_json(
            _client(shared[caller]),
            f"{DATASETS}/{shared['holder'].object_hash}/items/{item.pk}/move",
            {"folder_id": folder.pk},
        )
        assert response.status_code == 200, response.content
        row = response.json()
        assert (row["id"], row["folder_id"]) == (item.pk, folder.pk)
        assert "object_id" not in row

    @pytest.mark.parametrize("caller", ["reader", "stranger"])
    def test_a_reader_can_neither_add_nor_move(self, shared, caller):
        client = _client(shared[caller])
        base = f"{DATASETS}/{shared['holder'].object_hash}/items/"
        added = post_json(client, base, {"content_type_id": _recording_ct().pk, "object_id": HASHES[0]})
        moved = post_json(client, f"{base}{shared['items']['recording'].pk}/move", {"folder_id": None})
        assert (added.status_code, moved.status_code) == (403, 403)

    def test_a_moved_pooled_recording_keeps_its_item_id_from_a_manager_of_another_dataset(self, shared, make_user):
        from epicurrents.system_user import get_system_user

        curator = make_user()
        pooled = _recording(get_system_user(), index=1)
        pool = Dataset.objects.create(author=curator, name="pool", release_gated=True, submission_profile="test.pool")
        _add(pool, pooled)
        item = _add(shared["holder"], pooled)
        response = post_json(
            _client(shared["author"]),
            f"{DATASETS}/{shared['holder'].object_hash}/items/{item.pk}/move",
            {"folder_id": None},
        )
        row = response.json()
        assert (row["id"], row["object_hash"]) == (None, HASHES[1])


class TestTagItemRows:
    @pytest.fixture
    def tagged(self, shared, casefile):
        tag = Tag.objects.create(author=shared["reader"], name="cases", curated=True)
        rows = {
            "recording": TaggedItem.objects.create(
                tag=tag, content_type=_recording_ct(), object_id=str(shared["objects"]["recording"].pk)
            ),
            "casefile": TaggedItem.objects.create(
                tag=tag, content_type=casefile, object_id=str(shared["objects"]["casefile"].pk)
            ),
        }
        return tag, rows

    def _listed(self, user, tag) -> dict:
        rows = _client(user).get(f"{TAGS}/{tag.pk}/items/").json()
        assert all("object_id" not in row for row in rows)
        return {row["object_hash"]: row["id"] for row in rows}

    @pytest.mark.parametrize(
        ("caller", "served"),
        # The reader wrote the tag and reads both objects through the dataset; the writer manages the dataset.
        [("author", True), ("superuser", True), ("reader", False), ("writer", False)],
    )
    def test_the_item_id_goes_to_the_object_author_and_superusers_alone(self, shared, tagged, caller, served):
        tag, rows = tagged
        expected = {shared["hashes"][kind]: row.pk if served else None for kind, row in rows.items()}
        assert self._listed(shared[caller], tag) == expected

    def test_a_tagging_response_follows_the_same_rule(self, shared, make_user):
        tag = Tag.objects.create(author=shared["author"], name="mine", curated=True)
        writer = make_user()
        recording = shared["objects"]["recording"]
        _grant(recording, shared["author"], target=writer, can_write=True)
        body = {"content_type_id": _recording_ct().pk, "object_id": HASHES[0]}
        row = post_json(_client(writer), f"{TAGS}/{tag.pk}/items/", body).json()
        assert (row["id"], row["object_hash"]) == (None, HASHES[0])
        TaggedItem.objects.all().delete()
        row = post_json(_client(shared["author"]), f"{TAGS}/{tag.pk}/items/", body).json()
        tagging = TaggedItem.objects.get()
        assert (row["id"], row["object_hash"]) == (tagging.pk, HASHES[0])
        assert set(row) == {"id", "tag_id", "content_type_id", "object_hash", "tagged_at"}

    def test_a_row_without_keys_is_untagged_by_its_hash(self, shared, tagged):
        tag, rows = tagged
        response = _client(shared["reader"]).delete(
            f"{TAGS}/{tag.pk}/items/?content_type_id={_recording_ct().pk}&object_id={HASHES[0]}"
        )
        assert response.status_code == 200, response.content
        assert not TaggedItem.objects.filter(pk=rows["recording"].pk).exists()
