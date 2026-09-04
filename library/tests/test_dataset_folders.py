"""Contract tests for dataset folders, item placement, and the Collection → Dataset export.

Folders are presentation only, and the tests pin the invariants that keep them that way:
folder operations never touch membership (deleting a folder drops items back to the dataset
root), write access is the dataset's, and the export copies — never moves — a collection's
readable items, capped by the caller's own read access on each one.
"""

import pytest
from django.contrib.contenttypes.models import ContentType
from django.test import Client
from model_bakery import baker

from conftest import patch_json, post_json
from epicurrents.models import AccessRight
from library.models import Collection, CollectionItem, Dataset, DatasetFolder, DatasetItem

DATASETS_URL = "/api/v1/library/datasets/"
COLLECTIONS_URL = "/api/v1/library/collections/"


def _grant(obj, giver, target, *, can_write=False):
    return AccessRight.objects.create(
        content_type=ContentType.objects.get_for_model(obj, for_concrete_model=False),
        object_id=str(obj.pk),
        access_giver=giver,
        access_target=target,
        can_read=True,
        can_write=can_write,
    )


def _make_recording(author, *, status="ready", deleted_at=None, content_hash="0" * 64):
    return baker.make(
        "recordings.Recording",
        author=author,
        status=status,
        deleted_at=deleted_at,
        content_hash=content_hash,
    )


def _recording_ct():
    from recordings.models import Recording

    return ContentType.objects.get_for_model(Recording, for_concrete_model=False)


@pytest.fixture
def dataset(db, user):
    dataset = Dataset.objects.create(author=user, name="organised-set")
    _grant(dataset, user, user, can_write=True)
    return dataset


@pytest.fixture
def author_client(user):
    client = Client()
    client.force_login(user)
    return client


@pytest.mark.django_db
class TestFolderCrud:
    def test_create_folder_returns_row(self, author_client, dataset):
        response = post_json(author_client, f"{DATASETS_URL}{dataset.pk}/folders/", {"name": "Controls"})
        assert response.status_code == 201
        body = response.json()
        assert body["name"] == "Controls"
        assert body["parent_id"] is None
        assert body["dataset_id"] == dataset.pk
        assert DatasetFolder.objects.filter(dataset=dataset, name="Controls").exists()

    def test_create_nested_folder(self, author_client, dataset):
        parent = DatasetFolder.objects.create(dataset=dataset, name="Sessions")
        response = post_json(
            author_client,
            f"{DATASETS_URL}{dataset.pk}/folders/",
            {"name": "Night", "parent_id": parent.pk, "position": 2},
        )
        assert response.status_code == 201
        body = response.json()
        assert body["parent_id"] == parent.pk
        assert body["position"] == 2

    def test_create_folder_blank_name_rejected(self, author_client, dataset):
        response = post_json(author_client, f"{DATASETS_URL}{dataset.pk}/folders/", {"name": "   "})
        assert response.status_code == 422

    def test_create_folder_parent_from_other_dataset_404(self, author_client, dataset, user):
        other = Dataset.objects.create(author=user, name="other")
        stray = DatasetFolder.objects.create(dataset=other, name="Elsewhere")
        response = post_json(
            author_client,
            f"{DATASETS_URL}{dataset.pk}/folders/",
            {"name": "Child", "parent_id": stray.pk},
        )
        assert response.status_code == 404

    def test_create_requires_dataset_write(self, dataset, user, make_user):
        reader = make_user()
        _grant(dataset, user, reader, can_write=False)
        client = Client()
        client.force_login(reader)
        response = post_json(client, f"{DATASETS_URL}{dataset.pk}/folders/", {"name": "Nope"})
        assert response.status_code == 403
        assert not DatasetFolder.objects.exists()

    def test_list_orders_siblings_by_position_then_name(self, author_client, dataset):
        DatasetFolder.objects.create(dataset=dataset, name="Beta", position=1)
        DatasetFolder.objects.create(dataset=dataset, name="Alpha", position=1)
        DatasetFolder.objects.create(dataset=dataset, name="First", position=0)
        response = author_client.get(f"{DATASETS_URL}{dataset.pk}/folders/")
        assert response.status_code == 200
        assert [f["name"] for f in response.json()] == ["First", "Alpha", "Beta"]

    def test_list_readable_by_grantee(self, dataset, user, make_user):
        grantee = make_user()
        _grant(dataset, user, grantee)
        DatasetFolder.objects.create(dataset=dataset, name="Visible")
        client = Client()
        client.force_login(grantee)
        response = client.get(f"{DATASETS_URL}{dataset.pk}/folders/")
        assert response.status_code == 200
        assert [f["name"] for f in response.json()] == ["Visible"]

    def test_list_denied_without_grant(self, dataset, make_user):
        outsider = make_user()
        client = Client()
        client.force_login(outsider)
        response = client.get(f"{DATASETS_URL}{dataset.pk}/folders/")
        assert response.status_code == 403

    def test_rename_folder(self, author_client, dataset):
        folder = DatasetFolder.objects.create(dataset=dataset, name="Old")
        response = patch_json(author_client, f"{DATASETS_URL}{dataset.pk}/folders/{folder.pk}/", {"name": "New"})
        assert response.status_code == 200
        folder.refresh_from_db()
        assert folder.name == "New"

    def test_explicit_null_parent_moves_to_root(self, author_client, dataset):
        parent = DatasetFolder.objects.create(dataset=dataset, name="Parent")
        child = DatasetFolder.objects.create(dataset=dataset, name="Child", parent=parent)
        response = patch_json(author_client, f"{DATASETS_URL}{dataset.pk}/folders/{child.pk}/", {"parent_id": None})
        assert response.status_code == 200
        child.refresh_from_db()
        assert child.parent_id is None

    def test_absent_parent_left_unchanged(self, author_client, dataset):
        parent = DatasetFolder.objects.create(dataset=dataset, name="Parent")
        child = DatasetFolder.objects.create(dataset=dataset, name="Child", parent=parent)
        response = patch_json(author_client, f"{DATASETS_URL}{dataset.pk}/folders/{child.pk}/", {"name": "Kid"})
        assert response.status_code == 200
        child.refresh_from_db()
        assert child.parent_id == parent.pk

    def test_move_into_own_subtree_rejected(self, author_client, dataset):
        top = DatasetFolder.objects.create(dataset=dataset, name="Top")
        mid = DatasetFolder.objects.create(dataset=dataset, name="Mid", parent=top)
        response = patch_json(author_client, f"{DATASETS_URL}{dataset.pk}/folders/{top.pk}/", {"parent_id": mid.pk})
        assert response.status_code == 400
        top.refresh_from_db()
        assert top.parent_id is None

    def test_delete_cascades_subtree_and_items_fall_to_root(self, author_client, dataset, user):
        top = DatasetFolder.objects.create(dataset=dataset, name="Top")
        mid = DatasetFolder.objects.create(dataset=dataset, name="Mid", parent=top)
        sibling = DatasetFolder.objects.create(dataset=dataset, name="Sibling")
        recording = _make_recording(user)
        item = DatasetItem.objects.create(
            dataset=dataset, content_type=_recording_ct(), object_id=str(recording.pk), folder=mid
        )
        response = author_client.delete(f"{DATASETS_URL}{dataset.pk}/folders/{top.pk}/")
        assert response.status_code == 200
        assert set(DatasetFolder.objects.values_list("pk", flat=True)) == {sibling.pk}
        item.refresh_from_db()
        assert item.folder_id is None

    def test_delete_requires_write(self, dataset, user, make_user):
        reader = make_user()
        _grant(dataset, user, reader, can_write=False)
        folder = DatasetFolder.objects.create(dataset=dataset, name="Keep")
        client = Client()
        client.force_login(reader)
        response = client.delete(f"{DATASETS_URL}{dataset.pk}/folders/{folder.pk}/")
        assert response.status_code == 403
        assert DatasetFolder.objects.filter(pk=folder.pk).exists()


@pytest.mark.django_db
class TestItemPlacement:
    def test_move_item_into_folder_and_back(self, author_client, dataset, user):
        folder = DatasetFolder.objects.create(dataset=dataset, name="Bucket")
        recording = _make_recording(user)
        item = DatasetItem.objects.create(dataset=dataset, content_type=_recording_ct(), object_id=str(recording.pk))
        response = post_json(
            author_client, f"{DATASETS_URL}{dataset.pk}/items/{item.pk}/move", {"folder_id": folder.pk}
        )
        assert response.status_code == 200
        assert response.json()["folder_id"] == folder.pk
        item.refresh_from_db()
        assert item.folder_id == folder.pk

        response = post_json(author_client, f"{DATASETS_URL}{dataset.pk}/items/{item.pk}/move", {"folder_id": None})
        assert response.status_code == 200
        item.refresh_from_db()
        assert item.folder_id is None

    def test_move_item_requires_write(self, dataset, user, make_user):
        reader = make_user()
        _grant(dataset, user, reader, can_write=False)
        folder = DatasetFolder.objects.create(dataset=dataset, name="Bucket")
        recording = _make_recording(user)
        item = DatasetItem.objects.create(dataset=dataset, content_type=_recording_ct(), object_id=str(recording.pk))
        client = Client()
        client.force_login(reader)
        response = post_json(client, f"{DATASETS_URL}{dataset.pk}/items/{item.pk}/move", {"folder_id": folder.pk})
        assert response.status_code == 403
        item.refresh_from_db()
        assert item.folder_id is None

    def test_move_item_to_other_datasets_folder_404(self, author_client, dataset, user):
        other = Dataset.objects.create(author=user, name="other")
        stray = DatasetFolder.objects.create(dataset=other, name="Elsewhere")
        recording = _make_recording(user)
        item = DatasetItem.objects.create(dataset=dataset, content_type=_recording_ct(), object_id=str(recording.pk))
        response = post_json(author_client, f"{DATASETS_URL}{dataset.pk}/items/{item.pk}/move", {"folder_id": stray.pk})
        assert response.status_code == 404
        item.refresh_from_db()
        assert item.folder_id is None

    def test_items_listing_reports_folder_id(self, author_client, dataset, user):
        folder = DatasetFolder.objects.create(dataset=dataset, name="Bucket")
        recording = _make_recording(user)
        DatasetItem.objects.create(
            dataset=dataset, content_type=_recording_ct(), object_id=str(recording.pk), folder=folder
        )
        response = author_client.get(f"{DATASETS_URL}{dataset.pk}/items/")
        assert response.status_code == 200
        assert response.json()[0]["folder_id"] == folder.pk


@pytest.mark.django_db
class TestCollectionExport:
    def _collection_with_item(self, user, *, name="Sources"):
        collection = Collection.objects.create(author=user, name=name, description="original")
        recording = _make_recording(user)
        CollectionItem.objects.create(collection=collection, content_type=_recording_ct(), object_id=str(recording.pk))
        return collection, recording

    def test_export_copies_items_into_new_dataset(self, author_client, user):
        collection, recording = self._collection_with_item(user)
        response = post_json(author_client, f"{COLLECTIONS_URL}{collection.pk}/export/", {})
        assert response.status_code == 201
        body = response.json()
        assert body["exported_count"] == 1
        assert body["skipped_count"] == 0
        assert body["dataset"]["name"] == "Sources"
        dataset = Dataset.objects.get(pk=body["dataset"]["id"])
        assert dataset.author_id == user.pk
        item = DatasetItem.objects.get(dataset=dataset)
        assert item.object_id == str(recording.pk)
        # The author's own full-access row exists on the new dataset.
        right = AccessRight.objects.get(
            content_type=ContentType.objects.get_for_model(dataset, for_concrete_model=False),
            object_id=str(dataset.pk),
        )
        assert right.can_read and right.can_write and right.can_share

    def test_export_materialises_hierarchy(self, author_client, user):
        root = Collection.objects.create(author=user, name="Study")
        sub = Collection.objects.create(author=user, name="Visit 1", parent=root)
        deep = Collection.objects.create(author=user, name="EEG", parent=sub)
        root_rec = _make_recording(user, content_hash="a" * 64)
        deep_rec = _make_recording(user, content_hash="b" * 64)
        CollectionItem.objects.create(collection=root, content_type=_recording_ct(), object_id=str(root_rec.pk))
        CollectionItem.objects.create(collection=deep, content_type=_recording_ct(), object_id=str(deep_rec.pk))

        response = post_json(author_client, f"{COLLECTIONS_URL}{root.pk}/export/", {})
        assert response.status_code == 201
        body = response.json()
        assert body["folder_count"] == 2
        dataset = Dataset.objects.get(pk=body["dataset"]["id"])
        sub_folder = DatasetFolder.objects.get(dataset=dataset, name="Visit 1")
        deep_folder = DatasetFolder.objects.get(dataset=dataset, name="EEG")
        assert sub_folder.parent_id is None
        assert deep_folder.parent_id == sub_folder.pk
        assert DatasetItem.objects.get(object_id=str(root_rec.pk), dataset=dataset).folder_id is None
        assert DatasetItem.objects.get(object_id=str(deep_rec.pk), dataset=dataset).folder_id == deep_folder.pk

    def test_export_flat_when_materialise_false(self, author_client, user):
        root = Collection.objects.create(author=user, name="Study")
        sub = Collection.objects.create(author=user, name="Visit 1", parent=root)
        recording = _make_recording(user)
        CollectionItem.objects.create(collection=sub, content_type=_recording_ct(), object_id=str(recording.pk))
        response = post_json(author_client, f"{COLLECTIONS_URL}{root.pk}/export/", {"materialise_hierarchy": False})
        assert response.status_code == 201
        body = response.json()
        assert body["folder_count"] == 0
        assert not DatasetFolder.objects.exists()
        dataset = Dataset.objects.get(pk=body["dataset"]["id"])
        assert DatasetItem.objects.get(dataset=dataset).folder_id is None

    def test_export_skips_failed_trashed_and_unreadable(self, author_client, user, make_user):
        from django.utils import timezone

        stranger = make_user()
        collection = Collection.objects.create(author=user, name="Mixed")
        readable = _make_recording(user, content_hash="a" * 64)
        failed = _make_recording(user, status="failed", content_hash="b" * 64)
        trashed = _make_recording(user, deleted_at=timezone.now(), content_hash="c" * 64)
        unreadable = _make_recording(stranger, content_hash="d" * 64)
        for recording in (readable, failed, trashed, unreadable):
            CollectionItem.objects.create(
                collection=collection, content_type=_recording_ct(), object_id=str(recording.pk)
            )

        response = post_json(author_client, f"{COLLECTIONS_URL}{collection.pk}/export/", {})
        assert response.status_code == 201
        body = response.json()
        assert body["exported_count"] == 1
        assert body["skipped_count"] == 3
        dataset = Dataset.objects.get(pk=body["dataset"]["id"])
        assert [item.object_id for item in DatasetItem.objects.filter(dataset=dataset)] == [str(readable.pk)]

    def test_export_requires_collection_read(self, user, make_user):
        collection, _ = self._collection_with_item(user)
        outsider = make_user()
        client = Client()
        client.force_login(outsider)
        response = post_json(client, f"{COLLECTIONS_URL}{collection.pk}/export/", {})
        assert response.status_code == 403
        assert not Dataset.objects.exists()

    def test_export_leaves_collection_untouched(self, author_client, user):
        collection, recording = self._collection_with_item(user)
        response = post_json(
            author_client, f"{COLLECTIONS_URL}{collection.pk}/export/", {"name": "Copied", "description": "new"}
        )
        assert response.status_code == 201
        collection.refresh_from_db()
        assert collection.deleted_at is None
        membership = CollectionItem.objects.get(collection=collection)
        assert membership.deleted_at is None
        assert membership.object_id == str(recording.pk)
        body = response.json()
        assert body["dataset"]["name"] == "Copied"
        assert body["dataset"]["description"] == "new"

    def test_export_skips_trashed_subtree_branch(self, author_client, user):
        from django.utils import timezone

        root = Collection.objects.create(author=user, name="Study")
        live_sub = Collection.objects.create(author=user, name="Live", parent=root)
        dead_sub = Collection.objects.create(author=user, name="Dead", parent=root, deleted_at=timezone.now())
        live_rec = _make_recording(user, content_hash="a" * 64)
        dead_rec = _make_recording(user, content_hash="b" * 64)
        CollectionItem.objects.create(collection=live_sub, content_type=_recording_ct(), object_id=str(live_rec.pk))
        CollectionItem.objects.create(collection=dead_sub, content_type=_recording_ct(), object_id=str(dead_rec.pk))

        response = post_json(author_client, f"{COLLECTIONS_URL}{root.pk}/export/", {})
        assert response.status_code == 201
        body = response.json()
        assert body["exported_count"] == 1
        assert body["folder_count"] == 1
        dataset = Dataset.objects.get(pk=body["dataset"]["id"])
        assert not DatasetFolder.objects.filter(dataset=dataset, name="Dead").exists()
        assert not DatasetItem.objects.filter(dataset=dataset, object_id=str(dead_rec.pk)).exists()


@pytest.mark.django_db
class TestFolderPatchNullHandling:
    def test_explicit_null_position_rejected(self, author_client, dataset):
        folder = DatasetFolder.objects.create(dataset=dataset, name="Folder", position=3)
        response = patch_json(author_client, f"{DATASETS_URL}{dataset.pk}/folders/{folder.pk}/", {"position": None})
        assert response.status_code == 422
        folder.refresh_from_db()
        assert folder.position == 3


@pytest.mark.django_db
class TestDatasetHashAddressing:
    """Dataset routes resolve the public object_hash and keep the integer PK for internal callers."""

    def test_get_dataset_by_object_hash(self, author_client, dataset):
        response = author_client.get(f"{DATASETS_URL}{dataset.object_hash}/")
        assert response.status_code == 200
        assert response.json()["object_hash"] == dataset.object_hash

    def test_get_dataset_by_lowercase_hash(self, author_client, dataset):
        response = author_client.get(f"{DATASETS_URL}{dataset.object_hash.lower()}/")
        assert response.status_code == 200

    def test_get_dataset_by_pk_still_works(self, author_client, dataset):
        response = author_client.get(f"{DATASETS_URL}{dataset.pk}/")
        assert response.status_code == 200

    def test_malformed_identifier_is_a_plain_404(self, author_client, dataset):
        response = author_client.get(f"{DATASETS_URL}not-a-hash/")
        assert response.status_code == 404
        response = author_client.get(f"{DATASETS_URL}{'Z' * 32}/")
        assert response.status_code == 404

    def test_unknown_hash_404(self, author_client, dataset):
        response = author_client.get(f"{DATASETS_URL}{'0' * 32}/")
        assert response.status_code == 404

    def test_all_digit_hash_resolves_as_hash(self, author_client, user):
        # token_hex can produce a hash of nothing but digits; length decides
        # the form, so it must not be misparsed as a PK.
        digit_hashed = Dataset.objects.create(author=user, name="digits")
        Dataset.objects.filter(pk=digit_hashed.pk).update(object_hash="1" * 32)
        _grant(digit_hashed, user, user, can_write=True)
        response = author_client.get(f"{DATASETS_URL}{'1' * 32}/")
        assert response.status_code == 200
        assert response.json()["name"] == "digits"

    def test_items_and_folders_resolve_by_hash(self, author_client, dataset, user):
        folder = DatasetFolder.objects.create(dataset=dataset, name="Bucket")
        recording = _make_recording(user)
        DatasetItem.objects.create(
            dataset=dataset, content_type=_recording_ct(), object_id=str(recording.pk), folder=folder
        )
        items = author_client.get(f"{DATASETS_URL}{dataset.object_hash}/items/")
        assert items.status_code == 200
        assert items.json()[0]["folder_id"] == folder.pk
        folders = author_client.get(f"{DATASETS_URL}{dataset.object_hash}/folders/")
        assert folders.status_code == 200
        assert [f["name"] for f in folders.json()] == ["Bucket"]

    def test_write_routes_resolve_by_hash(self, author_client, dataset):
        response = post_json(author_client, f"{DATASETS_URL}{dataset.object_hash}/folders/", {"name": "Via hash"})
        assert response.status_code == 201
        assert DatasetFolder.objects.filter(dataset=dataset, name="Via hash").exists()

    def test_trashed_dataset_hash_404(self, author_client, dataset):
        from django.utils import timezone

        dataset.deleted_at = timezone.now()
        dataset.save(update_fields=["deleted_at"])
        response = author_client.get(f"{DATASETS_URL}{dataset.object_hash}/")
        assert response.status_code == 404
