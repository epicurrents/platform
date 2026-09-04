"""API integration tests for the library app (Collections and Datasets)."""

import json

import pytest
from django.contrib.contenttypes.models import ContentType
from model_bakery import baker

from epicurrents.models import AccessRight
from library.models import (
    Collection,
    CollectionItem,
    Dataset,
    DatasetItem,
    Tag,
    TaggedItem,
)

BASE = "/api/v1/library/collections/"


def _url(collection_id=None, suffix=""):
    if collection_id is None:
        return BASE
    return f"{BASE}{collection_id}/{suffix}"


def post_json(client, url, data):
    return client.post(url, json.dumps(data), content_type="application/json")


def patch_json(client, url, data):
    return client.patch(url, json.dumps(data), content_type="application/json")


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCreateCollection:
    def test_unauthenticated_returns_401(self, client):
        resp = post_json(client, BASE, {"name": "X"})
        assert resp.status_code == 401

    def test_creates_collection(self, auth_client):
        c, user = auth_client
        resp = post_json(c, BASE, {"name": "My Collection", "description": "desc"})
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "My Collection"
        assert data["description"] == "desc"
        assert data["author_id"] == user.pk
        assert data["parent_id"] is None

    def test_creation_makes_no_access_right_row(self, auth_client):
        c, user = auth_client
        resp = post_json(c, BASE, {"name": "Col"})
        col_id = resp.json()["id"]
        ct = ContentType.objects.get_for_model(Collection, for_concrete_model=False)
        # Author access is implicit; no AccessRight may target a collection.
        assert not AccessRight.objects.filter(content_type=ct, object_id=str(col_id)).exists()

    def test_create_with_parent(self, auth_client):
        c, user = auth_client
        parent = baker.make(Collection, author=user)
        ct = ContentType.objects.get_for_model(Collection, for_concrete_model=False)
        AccessRight.objects.create(
            content_type=ct,
            object_id=str(parent.pk),
            access_giver=user,
            access_target=user,
            can_read=True,
        )
        resp = post_json(c, BASE, {"name": "Child", "parent_id": parent.pk})
        assert resp.status_code == 201
        assert resp.json()["parent_id"] == parent.pk

    def test_create_with_nonexistent_parent_returns_404(self, auth_client):
        c, _ = auth_client
        resp = post_json(c, BASE, {"name": "X", "parent_id": 99999})
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestListCollections:
    def test_unauthenticated_returns_401(self, client):
        assert client.get(BASE).status_code == 401

    def test_author_sees_own_without_access_right(self, auth_client):
        c, user = auth_client
        baker.make(Collection, author=user, _quantity=2)
        resp = c.get(BASE)
        assert resp.status_code == 200
        assert len(resp.json()) == 2

    def test_does_not_show_others_private_collections(self, auth_client, make_user):
        c, _ = auth_client
        other = make_user(username="other")
        baker.make(Collection, author=other, _quantity=2)
        resp = c.get(BASE)
        assert resp.status_code == 200
        assert len(resp.json()) == 0

    def test_stale_share_row_does_not_surface_collection(self, auth_client, make_user):
        c, user = auth_client
        other = make_user(username="other")
        col = baker.make(Collection, author=other)
        ct = ContentType.objects.get_for_model(Collection, for_concrete_model=False)
        AccessRight.objects.create(
            content_type=ct,
            object_id=str(col.pk),
            access_giver=other,
            access_target=user,
            can_read=True,
        )
        resp = c.get(BASE)
        assert resp.status_code == 200
        ids = [d["id"] for d in resp.json()]
        assert col.pk not in ids

    def test_list_children(self, auth_client):
        c, user = auth_client
        parent = baker.make(Collection, author=user)
        baker.make(Collection, author=user, parent=parent, _quantity=4)
        ct = ContentType.objects.get_for_model(Collection, for_concrete_model=False)
        AccessRight.objects.create(
            content_type=ct,
            object_id=str(parent.pk),
            access_giver=user,
            access_target=user,
            can_read=True,
        )
        resp = c.get(BASE, {"parent_id": parent.pk})
        assert resp.status_code == 200
        assert len(resp.json()) == 4

    def test_list_children_requires_read_on_parent(self, auth_client, make_user):
        c, user = auth_client
        other = make_user(username="other")
        parent = baker.make(Collection, author=other)
        resp = c.get(BASE, {"parent_id": parent.pk})
        assert resp.status_code == 403

    def test_trash_lists_own_deleted(self, auth_client):
        from django.utils import timezone

        c, user = auth_client
        col = baker.make(Collection, author=user)
        Collection.objects.filter(pk=col.pk).update(deleted_at=timezone.now())
        resp = c.get(BASE, {"trash": "true"})
        assert resp.status_code == 200
        assert any(d["id"] == col.pk for d in resp.json())


# ---------------------------------------------------------------------------
# Detail / Update / Delete
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestGetCollection:
    def test_get_own(self, auth_client):
        c, user = auth_client
        col = baker.make(Collection, author=user, name="MyCol")
        resp = c.get(_url(col.pk))
        assert resp.status_code == 200
        assert resp.json()["name"] == "MyCol"

    def test_stale_share_row_does_not_open_detail(self, auth_client, make_user):
        c, user = auth_client
        other = make_user(username="other")
        col = baker.make(Collection, author=other)
        ct = ContentType.objects.get_for_model(Collection, for_concrete_model=False)
        AccessRight.objects.create(
            content_type=ct,
            object_id=str(col.pk),
            access_giver=other,
            access_target=user,
            can_read=True,
        )
        assert c.get(_url(col.pk)).status_code == 403

    def test_get_unshared_returns_403(self, auth_client, make_user):
        c, _ = auth_client
        other = make_user(username="other")
        col = baker.make(Collection, author=other)
        assert c.get(_url(col.pk)).status_code == 403

    def test_get_deleted_returns_404(self, auth_client):
        from django.utils import timezone

        c, user = auth_client
        col = baker.make(Collection, author=user)
        Collection.objects.filter(pk=col.pk).update(deleted_at=timezone.now())
        assert c.get(_url(col.pk)).status_code == 404


@pytest.mark.django_db
class TestUpdateCollection:
    def test_author_can_update(self, auth_client):
        c, user = auth_client
        col = baker.make(Collection, author=user, name="Old")
        resp = patch_json(c, _url(col.pk), {"name": "New"})
        assert resp.status_code == 200
        assert resp.json()["name"] == "New"

    def test_non_author_without_write_returns_403(self, auth_client, make_user):
        c, user = auth_client
        other = make_user(username="other")
        col = baker.make(Collection, author=other)
        ct = ContentType.objects.get_for_model(Collection, for_concrete_model=False)
        AccessRight.objects.create(
            content_type=ct,
            object_id=str(col.pk),
            access_giver=other,
            access_target=user,
            can_read=True,
            can_write=False,
        )
        resp = patch_json(c, _url(col.pk), {"name": "X"})
        assert resp.status_code == 403

    def test_cycle_detection(self, auth_client):
        c, user = auth_client
        root = baker.make(Collection, author=user, name="Root")
        child = baker.make(Collection, author=user, name="Child", parent=root)
        resp = patch_json(c, _url(root.pk), {"parent_id": child.pk})
        assert resp.status_code == 400


@pytest.mark.django_db
class TestDeleteCollection:
    def test_author_can_soft_delete(self, auth_client):
        c, user = auth_client
        col = baker.make(Collection, author=user)
        resp = c.delete(_url(col.pk))
        assert resp.status_code == 200
        col.refresh_from_db()
        assert col.deleted_at is not None

    def test_non_author_without_write_returns_403(self, auth_client, make_user):
        c, user = auth_client
        other = make_user(username="other")
        col = baker.make(Collection, author=other)
        ct = ContentType.objects.get_for_model(Collection, for_concrete_model=False)
        AccessRight.objects.create(
            content_type=ct,
            object_id=str(col.pk),
            access_giver=other,
            access_target=user,
            can_read=True,
        )
        assert c.delete(_url(col.pk)).status_code == 403

    def test_soft_delete_cascades_to_children(self, auth_client):
        c, user = auth_client
        parent = baker.make(Collection, author=user)
        child = baker.make(Collection, author=user, parent=parent)
        c.delete(_url(parent.pk))
        child.refresh_from_db()
        # Recursive trash: the child is trashed too, but keeps its parent link so
        # a restore re-nests it.
        assert child.deleted_at is not None
        assert child.parent_id == parent.pk


# ---------------------------------------------------------------------------
# Item membership (generic)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestItemMembership:
    def _make_recording(self, user):
        from recordings.models import Recording

        return baker.make(Recording, author=user, file_size=1, status=Recording.Status.READY)

    def _recording_ct(self):
        from recordings.models import Recording

        return ContentType.objects.get_for_model(Recording, for_concrete_model=False)

    def test_add_item(self, auth_client):
        c, user = auth_client
        col = baker.make(Collection, author=user)
        rec = self._make_recording(user)
        ct = self._recording_ct()
        resp = post_json(
            c,
            _url(col.pk, "items/"),
            {
                "content_type_id": ct.pk,
                "object_id": str(rec.pk),
            },
        )
        assert resp.status_code == 201
        assert resp.json()["content_type_id"] == ct.pk
        assert resp.json()["object_id"] == str(rec.pk)
        assert CollectionItem.objects.filter(collection=col, content_type=ct, object_id=str(rec.pk)).exists()

    def test_add_duplicate_item_returns_409(self, auth_client):
        c, user = auth_client
        col = baker.make(Collection, author=user)
        rec = self._make_recording(user)
        ct = self._recording_ct()
        payload = {"content_type_id": ct.pk, "object_id": str(rec.pk)}
        post_json(c, _url(col.pk, "items/"), payload)
        resp = post_json(c, _url(col.pk, "items/"), payload)
        assert resp.status_code == 409

    def test_add_item_already_in_another_collection_returns_409(self, auth_client):
        """A recording can belong to at most one collection (global unique constraint)."""
        c, user = auth_client
        col_a = baker.make(Collection, author=user)
        col_b = baker.make(Collection, author=user)
        rec = self._make_recording(user)
        ct = self._recording_ct()
        payload = {"content_type_id": ct.pk, "object_id": str(rec.pk)}
        first_resp = post_json(c, _url(col_a.pk, "items/"), payload)
        assert first_resp.status_code == 201
        resp = post_json(c, _url(col_b.pk, "items/"), payload)
        assert resp.status_code == 409

    def test_remove_item(self, auth_client):
        c, user = auth_client
        col = baker.make(Collection, author=user)
        rec = self._make_recording(user)
        ct = self._recording_ct()
        item = CollectionItem.objects.create(collection=col, content_type=ct, object_id=str(rec.pk))
        resp = c.delete(_url(col.pk, f"items/{item.pk}/"))
        assert resp.status_code == 200
        assert not CollectionItem.objects.filter(pk=item.pk).exists()

    def test_add_item_requires_write_on_collection(self, auth_client, make_user):
        c, user = auth_client
        other = make_user(username="other")
        col = baker.make(Collection, author=other)
        rec = self._make_recording(user)
        ct = self._recording_ct()
        col_ct = ContentType.objects.get_for_model(Collection, for_concrete_model=False)
        AccessRight.objects.create(
            content_type=col_ct,
            object_id=str(col.pk),
            access_giver=other,
            access_target=user,
            can_read=True,
            can_write=False,
        )
        resp = post_json(
            c,
            _url(col.pk, "items/"),
            {
                "content_type_id": ct.pk,
                "object_id": str(rec.pk),
            },
        )
        assert resp.status_code == 403

    def test_add_item_requires_read_on_object(self, auth_client, make_user):
        c, user = auth_client
        other = make_user(username="other")
        col = baker.make(Collection, author=user)
        rec = self._make_recording(other)  # owned by other, not shared
        ct = self._recording_ct()
        resp = post_json(
            c,
            _url(col.pk, "items/"),
            {
                "content_type_id": ct.pk,
                "object_id": str(rec.pk),
            },
        )
        assert resp.status_code == 403

    def test_add_item_nonexistent_object_returns_404(self, auth_client):
        c, user = auth_client
        col = baker.make(Collection, author=user)
        ct = self._recording_ct()
        resp = post_json(
            c,
            _url(col.pk, "items/"),
            {
                "content_type_id": ct.pk,
                "object_id": "99999",
            },
        )
        assert resp.status_code == 404

    def test_list_items(self, auth_client):
        c, user = auth_client
        col = baker.make(Collection, author=user)
        rec = self._make_recording(user)
        ct = self._recording_ct()
        CollectionItem.objects.create(collection=col, content_type=ct, object_id=str(rec.pk))
        resp = c.get(_url(col.pk, "items/"))
        assert resp.status_code == 200
        assert len(resp.json()) == 1
        assert resp.json()[0]["object_id"] == str(rec.pk)

    def test_list_items_author_sees_original_name(self, auth_client):
        c, user = auth_client
        col = baker.make(Collection, author=user)
        rec = self._make_recording(user)
        rec.original_name = "patient_x.edf"
        rec.display_name = None
        rec.stored_name = "B2B2B2B2B2B2B2B2B2B2B2B2B2B2B2B2.edf"
        rec.save()
        CollectionItem.objects.create(collection=col, content_type=self._recording_ct(), object_id=str(rec.pk))
        resp = c.get(_url(col.pk, "items/"))
        assert resp.status_code == 200
        # Unlabeled recording, author view: the original filename, not the hash prefix.
        assert resp.json()[0]["object_name"] == "patient_x.edf"

    def test_list_items_grantee_sees_display_name_not_original(self, auth_client, make_user):
        c, user = auth_client
        owner = make_user(username="owner_objname")
        col = baker.make(Collection, author=user)
        ct = self._recording_ct()
        rec = self._make_recording(owner)
        rec.original_name = "patient_y.edf"
        rec.display_name = None
        rec.stored_name = "C3C3C3C3C3C3C3C3C3C3C3C3C3C3C3C3.edf"
        rec.save()
        AccessRight.objects.create(
            content_type=ct,
            object_id=str(rec.pk),
            access_giver=owner,
            access_target=user,
            can_read=True,
        )
        CollectionItem.objects.create(collection=col, content_type=ct, object_id=str(rec.pk))
        resp = c.get(_url(col.pk, "items/"))
        assert resp.status_code == 200
        row = next(i for i in resp.json() if i["object_id"] == str(rec.pk))
        # Grantee never sees the filename — only the hash-prefix fallback.
        assert row["object_name"] == "C3C3C3C3"

    def test_list_items_filter_by_content_type(self, auth_client):
        c, user = auth_client
        col = baker.make(Collection, author=user)
        rec = self._make_recording(user)
        rec_ct = self._recording_ct()
        # Add a recording item and a nested collection item
        col_ct = ContentType.objects.get_for_model(Collection, for_concrete_model=False)
        nested = baker.make(Collection, author=user)
        CollectionItem.objects.create(collection=col, content_type=rec_ct, object_id=str(rec.pk))
        CollectionItem.objects.create(collection=col, content_type=col_ct, object_id=str(nested.pk))

        resp = c.get(_url(col.pk, "items/"), {"content_type_id": rec_ct.pk})
        assert resp.status_code == 200
        assert len(resp.json()) == 1
        assert resp.json()[0]["content_type_id"] == rec_ct.pk

    def test_remove_item_wrong_collection_returns_404(self, auth_client):
        c, user = auth_client
        col_a = baker.make(Collection, author=user)
        col_b = baker.make(Collection, author=user)
        rec = self._make_recording(user)
        ct = self._recording_ct()
        item = CollectionItem.objects.create(collection=col_b, content_type=ct, object_id=str(rec.pk))
        resp = c.delete(_url(col_a.pk, f"items/{item.pk}/"))
        assert resp.status_code == 404

    def test_move_item(self, auth_client):
        c, user = auth_client
        source = baker.make(Collection, author=user)
        target = baker.make(Collection, author=user)
        rec = self._make_recording(user)
        ct = self._recording_ct()
        item = CollectionItem.objects.create(collection=source, content_type=ct, object_id=str(rec.pk))
        resp = post_json(
            c,
            _url(source.pk, f"items/{item.pk}/move"),
            {"target_collection_id": target.pk},
        )
        assert resp.status_code == 200
        item.refresh_from_db()
        assert item.collection_id == target.pk
        # The one-collection-per-recording invariant: exactly one row remains.
        assert CollectionItem.objects.filter(content_type=ct, object_id=str(rec.pk)).count() == 1

    def test_move_item_to_same_collection_is_idempotent(self, auth_client):
        c, user = auth_client
        col = baker.make(Collection, author=user)
        rec = self._make_recording(user)
        ct = self._recording_ct()
        item = CollectionItem.objects.create(collection=col, content_type=ct, object_id=str(rec.pk))
        resp = post_json(
            c,
            _url(col.pk, f"items/{item.pk}/move"),
            {"target_collection_id": col.pk},
        )
        assert resp.status_code == 200
        item.refresh_from_db()
        assert item.collection_id == col.pk

    def test_move_item_requires_write_on_source(self, auth_client, make_user):
        c, user = auth_client
        other = make_user(username="other")
        source = baker.make(Collection, author=other)
        target = baker.make(Collection, author=user)
        rec = self._make_recording(other)
        ct = self._recording_ct()
        item = CollectionItem.objects.create(collection=source, content_type=ct, object_id=str(rec.pk))
        # Read-only share on the source collection — caller cannot move out.
        col_ct = ContentType.objects.get_for_model(Collection, for_concrete_model=False)
        AccessRight.objects.create(
            content_type=col_ct,
            object_id=str(source.pk),
            access_giver=other,
            access_target=user,
            can_read=True,
            can_write=False,
        )
        resp = post_json(
            c,
            _url(source.pk, f"items/{item.pk}/move"),
            {"target_collection_id": target.pk},
        )
        assert resp.status_code == 403
        item.refresh_from_db()
        assert item.collection_id == source.pk

    def test_move_item_requires_write_on_target(self, auth_client, make_user):
        c, user = auth_client
        other = make_user(username="other")
        source = baker.make(Collection, author=user)
        target = baker.make(Collection, author=other)
        rec = self._make_recording(user)
        ct = self._recording_ct()
        item = CollectionItem.objects.create(collection=source, content_type=ct, object_id=str(rec.pk))
        resp = post_json(
            c,
            _url(source.pk, f"items/{item.pk}/move"),
            {"target_collection_id": target.pk},
        )
        assert resp.status_code == 403
        item.refresh_from_db()
        assert item.collection_id == source.pk

    def test_move_item_nonexistent_target_returns_404(self, auth_client):
        c, user = auth_client
        source = baker.make(Collection, author=user)
        rec = self._make_recording(user)
        ct = self._recording_ct()
        item = CollectionItem.objects.create(collection=source, content_type=ct, object_id=str(rec.pk))
        resp = post_json(
            c,
            _url(source.pk, f"items/{item.pk}/move"),
            {"target_collection_id": 99999},
        )
        assert resp.status_code == 404

    def test_move_item_wrong_source_collection_returns_404(self, auth_client):
        c, user = auth_client
        col_a = baker.make(Collection, author=user)
        col_b = baker.make(Collection, author=user)
        target = baker.make(Collection, author=user)
        rec = self._make_recording(user)
        ct = self._recording_ct()
        # The item lives in col_b, but the move call addresses col_a.
        item = CollectionItem.objects.create(collection=col_b, content_type=ct, object_id=str(rec.pk))
        resp = post_json(
            c,
            _url(col_a.pk, f"items/{item.pk}/move"),
            {"target_collection_id": target.pk},
        )
        assert resp.status_code == 404

    def test_list_items_hides_unreadable_items(self, auth_client, make_user):
        """Items the caller cannot read are excluded from the collection listing."""
        c, user = auth_client
        owner = make_user(username="owner")
        col = baker.make(Collection, author=user)
        ct = self._recording_ct()

        readable_rec = self._make_recording(user)  # user owns it → readable
        private_rec = self._make_recording(owner)  # user has no access → hidden

        CollectionItem.objects.create(collection=col, content_type=ct, object_id=str(readable_rec.pk))
        CollectionItem.objects.create(collection=col, content_type=ct, object_id=str(private_rec.pk))

        resp = c.get(_url(col.pk, "items/"))
        assert resp.status_code == 200
        obj_ids = {item["object_id"] for item in resp.json()}
        assert str(readable_rec.pk) in obj_ids
        assert str(private_rec.pk) not in obj_ids

    def test_list_items_shows_shared_item(self, auth_client, make_user):
        """Items shared via AccessRight are included in the listing."""
        c, user = auth_client
        owner = make_user(username="owner")
        col = baker.make(Collection, author=user)
        ct = self._recording_ct()
        rec = self._make_recording(owner)

        AccessRight.objects.create(
            content_type=ct,
            object_id=str(rec.pk),
            access_giver=owner,
            access_target=user,
            can_read=True,
        )
        CollectionItem.objects.create(collection=col, content_type=ct, object_id=str(rec.pk))

        resp = c.get(_url(col.pk, "items/"))
        assert resp.status_code == 200
        assert any(item["object_id"] == str(rec.pk) for item in resp.json())

    def test_list_items_hides_failed_recordings(self, auth_client):
        """FAILED recordings are dropped from the collection listing for every viewer.

        Matches the soft-delete pattern already enforced in
        ``_enrich_collection_items``: items pointing at a recording the
        platform cannot serve are silently omitted.  The author retains
        visibility of the failure via the main recordings list.
        """
        from recordings.models import Recording

        c, user = auth_client
        col = baker.make(Collection, author=user)
        ct = self._recording_ct()
        ready_rec = self._make_recording(user)
        failed_rec = baker.make(Recording, author=user, file_size=1, status=Recording.Status.FAILED)

        CollectionItem.objects.create(collection=col, content_type=ct, object_id=str(ready_rec.pk))
        CollectionItem.objects.create(collection=col, content_type=ct, object_id=str(failed_rec.pk))

        resp = c.get(_url(col.pk, "items/"))
        assert resp.status_code == 200
        obj_ids = {item["object_id"] for item in resp.json()}
        assert str(ready_rec.pk) in obj_ids
        assert str(failed_rec.pk) not in obj_ids

    # ─── MediaFile in collection listings ─────────────────────────────────

    def _media_ct(self):
        from media.models import MediaFile

        return ContentType.objects.get_for_model(MediaFile, for_concrete_model=False)

    def _make_media(self, user, file_extension=".pdf", **kwargs):
        from media.models import MediaFile

        kwargs.setdefault("media_type", MediaFile.MediaType.DOCUMENT)
        kwargs.setdefault("file_size", 1024)
        kwargs.setdefault("file_extension", file_extension)
        kwargs.setdefault("file_path", "/tmp/notreal" + file_extension)
        kwargs.setdefault("original_name", "doc" + file_extension)
        kwargs.setdefault("display_name", None)
        # Caller can override; the helpers below set a deterministic content_hash.
        kwargs.setdefault("content_hash", "A" * 32)
        kwargs.setdefault("stored_name", "A" * 32 + file_extension)
        return baker.make(MediaFile, author=user, **kwargs)

    def test_list_items_surfaces_media_with_support_flag(self, auth_client):
        """A media item in a collection lists with object_type=mediafile and
        media-specific fields (media_type, file_extension, is_supported)."""
        from django.test import override_settings

        c, user = auth_client
        col = baker.make(Collection, author=user)
        media = self._make_media(user, file_extension=".pdf")
        CollectionItem.objects.create(
            collection=col,
            content_type=self._media_ct(),
            object_id=str(media.pk),
        )
        with override_settings(MEDIA_ALLOWED_UPLOAD_EXTENSIONS=[".pdf"]):
            resp = c.get(_url(col.pk, "items/"))
        assert resp.status_code == 200
        row = next(r for r in resp.json() if r["object_id"] == str(media.pk))
        assert row["object_type"] == "mediafile"
        assert row["object_hash"] == media.content_hash
        assert row["media_type"] == "document"
        assert row["file_extension"] == ".pdf"
        assert row["is_supported"] is True

    def test_list_items_keeps_unsupported_media_with_flag_false(self, auth_client):
        """The user explicitly asked for unsupported rows to stay listed
        (greyed by the frontend) — only the live allowlist toggles
        ``is_supported``, never the membership row's presence."""
        from django.test import override_settings

        c, user = auth_client
        col = baker.make(Collection, author=user)
        media = self._make_media(user, file_extension=".pdf")
        CollectionItem.objects.create(
            collection=col,
            content_type=self._media_ct(),
            object_id=str(media.pk),
        )
        with override_settings(MEDIA_ALLOWED_UPLOAD_EXTENSIONS=[".md"]):
            resp = c.get(_url(col.pk, "items/"))
        assert resp.status_code == 200
        row = next(r for r in resp.json() if r["object_id"] == str(media.pk))
        assert row["is_supported"] is False
        assert row["object_type"] == "mediafile"

    def test_add_item_resolves_media_content_hash_to_pk(self, auth_client):
        """The add-item endpoint accepts a media ``content_hash`` for
        ``object_id`` and resolves it server-side, mirroring the recording
        hash resolver. The frontend Add-media flow depends on this."""
        c, user = auth_client
        col = baker.make(Collection, author=user)
        media = self._make_media(user)
        resp = post_json(
            c,
            _url(col.pk, "items/"),
            {
                "content_type_id": self._media_ct().pk,
                "object_id": media.content_hash,
            },
        )
        assert resp.status_code == 201, resp.content
        assert CollectionItem.objects.filter(
            collection=col,
            content_type=self._media_ct(),
            object_id=str(media.pk),
        ).exists()

    def test_list_items_drops_soft_deleted_media(self, auth_client):
        """Soft-deleted media files are silently omitted from collection
        listings — same rule as soft-deleted recordings."""
        from django.utils import timezone

        c, user = auth_client
        col = baker.make(Collection, author=user)
        media = self._make_media(user)
        media.deleted_at = timezone.now()
        media.save(update_fields=["deleted_at"])
        CollectionItem.objects.create(
            collection=col,
            content_type=self._media_ct(),
            object_id=str(media.pk),
        )
        resp = c.get(_url(col.pk, "items/"))
        assert resp.status_code == 200
        assert all(row["object_id"] != str(media.pk) for row in resp.json())


# ---------------------------------------------------------------------------
# Recursive trash + restore lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCollectionTrashLifecycle:
    def _make_recording(self, user):
        from recordings.models import Recording

        return baker.make(Recording, author=user, file_size=1, status=Recording.Status.READY)

    def _recording_ct(self):
        from recordings.models import Recording

        return ContentType.objects.get_for_model(Recording, for_concrete_model=False)

    def _add(self, c, col_pk, rec, ct):
        return post_json(c, _url(col_pk, "items/"), {"content_type_id": ct.pk, "object_id": str(rec.pk)})

    def test_delete_cascades_to_subtree_and_memberships(self, auth_client):
        c, user = auth_client
        parent = baker.make(Collection, author=user)
        child = baker.make(Collection, author=user, parent=parent)
        ct = self._recording_ct()
        rec = self._make_recording(user)
        assert self._add(c, child.pk, rec, ct).status_code == 201

        assert c.delete(_url(parent.pk)).status_code == 200
        parent.refresh_from_db()
        child.refresh_from_db()
        item = CollectionItem.objects.get(collection=child, object_id=str(rec.pk))
        # The whole subtree is trashed under one shared timestamp.
        assert parent.deleted_at is not None
        assert parent.deleted_at == child.deleted_at == item.deleted_at

    def test_can_refile_recording_from_trashed_collection(self, auth_client):
        """A recording in a trashed collection can be filed elsewhere."""
        c, user = auth_client
        col_a = baker.make(Collection, author=user)
        col_b = baker.make(Collection, author=user)
        ct = self._recording_ct()
        rec = self._make_recording(user)
        assert self._add(c, col_a.pk, rec, ct).status_code == 201
        assert c.delete(_url(col_a.pk)).status_code == 200

        assert self._add(c, col_b.pk, rec, ct).status_code == 201
        assert CollectionItem.objects.filter(collection=col_b, deleted_at__isnull=True, object_id=str(rec.pk)).exists()

    def test_add_still_409_when_in_another_live_collection(self, auth_client):
        c, user = auth_client
        col_a = baker.make(Collection, author=user)
        col_b = baker.make(Collection, author=user)
        ct = self._recording_ct()
        rec = self._make_recording(user)
        assert self._add(c, col_a.pk, rec, ct).status_code == 201
        assert self._add(c, col_b.pk, rec, ct).status_code == 409

    def test_restore_brings_back_subtree_and_membership(self, auth_client):
        c, user = auth_client
        parent = baker.make(Collection, author=user)
        child = baker.make(Collection, author=user, parent=parent)
        ct = self._recording_ct()
        rec = self._make_recording(user)
        assert self._add(c, child.pk, rec, ct).status_code == 201
        assert c.delete(_url(parent.pk)).status_code == 200

        resp = post_json(c, _url(parent.pk, "restore"), {})
        assert resp.status_code == 200
        parent.refresh_from_db()
        child.refresh_from_db()
        item = CollectionItem.objects.get(collection=child, object_id=str(rec.pk))
        assert parent.deleted_at is None
        assert child.deleted_at is None
        assert item.deleted_at is None

    def test_restore_skips_recording_refiled_elsewhere(self, auth_client):
        """Re-filing wins: a recording moved out while trashed is not pulled back."""
        c, user = auth_client
        col_a = baker.make(Collection, author=user)
        col_b = baker.make(Collection, author=user)
        ct = self._recording_ct()
        rec = self._make_recording(user)
        assert self._add(c, col_a.pk, rec, ct).status_code == 201
        assert c.delete(_url(col_a.pk)).status_code == 200
        assert self._add(c, col_b.pk, rec, ct).status_code == 201

        resp = post_json(c, _url(col_a.pk, "restore"), {})
        assert resp.status_code == 200
        assert resp.json()["items_skipped"] == 1
        # The recording stays in B; A's membership is left trashed.
        assert CollectionItem.objects.filter(collection=col_b, deleted_at__isnull=True, object_id=str(rec.pk)).exists()
        a_item = CollectionItem.objects.get(collection=col_a, object_id=str(rec.pk))
        assert a_item.deleted_at is not None


# ---------------------------------------------------------------------------
# Bulk-rename recordings (Phase 2)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestBulkRenameRecordings:
    """POST /collections/{id}/recordings/bulk-rename — sequential display_name assignment.

    Iterates writable recordings in ``added_at`` order and sets each to
    ``"{prefix} {n}"``.  Non-writable recordings are skipped without
    advancing the counter, so the result is always 1..N contiguous on the
    rows actually renamed.
    """

    def _make_recording(self, user, **kwargs):
        from recordings.models import Recording

        kwargs.setdefault("status", Recording.Status.READY)
        kwargs.setdefault("file_size", 1)
        return baker.make(Recording, author=user, **kwargs)

    def _recording_ct(self):
        from recordings.models import Recording

        return ContentType.objects.get_for_model(Recording, for_concrete_model=False)

    def _url_bulk_rename(self, collection_id):
        return _url(collection_id, "recordings/bulk-rename")

    def test_renames_recordings_sequentially(self, auth_client):
        c, user = auth_client
        col = baker.make(Collection, author=user)
        ct = self._recording_ct()
        recs = [self._make_recording(user, original_name=f"r{i}.edf") for i in range(3)]
        for rec in recs:
            CollectionItem.objects.create(collection=col, content_type=ct, object_id=str(rec.pk))

        resp = post_json(c, self._url_bulk_rename(col.pk), {"prefix": "Subject"})
        assert resp.status_code == 200
        assert resp.json() == {"renamed": 3, "skipped": 0}

        for rec, expected_n in zip(recs, [1, 2, 3]):
            rec.refresh_from_db()
            assert rec.display_name == f"Subject {expected_n}"

    def test_default_prefix_is_recording(self, auth_client):
        c, user = auth_client
        col = baker.make(Collection, author=user)
        ct = self._recording_ct()
        rec = self._make_recording(user)
        CollectionItem.objects.create(collection=col, content_type=ct, object_id=str(rec.pk))

        resp = post_json(c, self._url_bulk_rename(col.pk), {})
        assert resp.status_code == 200
        rec.refresh_from_db()
        assert rec.display_name == "Recording 1"

    def test_skips_non_writable_recordings_without_renumbering(self, auth_client, make_user):
        """A recording the caller cannot write is skipped; the counter does not advance."""
        c, user = auth_client
        other = make_user(username="other_owner")
        col = baker.make(Collection, author=user)
        ct = self._recording_ct()

        own = self._make_recording(user)
        shared = self._make_recording(other)
        AccessRight.objects.create(
            content_type=ct,
            object_id=str(shared.pk),
            access_giver=other,
            access_target=user,
            can_read=True,  # read-only — not writable
        )
        own2 = self._make_recording(user)

        # Add in this order: own, shared (read-only), own2.
        for rec in [own, shared, own2]:
            CollectionItem.objects.create(collection=col, content_type=ct, object_id=str(rec.pk))

        resp = post_json(c, self._url_bulk_rename(col.pk), {"prefix": "S"})
        assert resp.status_code == 200
        assert resp.json() == {"renamed": 2, "skipped": 1}

        own.refresh_from_db()
        shared.refresh_from_db()
        own2.refresh_from_db()
        # Counter is contiguous over writable rows only.
        assert own.display_name == "S 1"
        assert shared.display_name is None
        assert own2.display_name == "S 2"

    def test_ignores_failed_and_deleted_recordings(self, auth_client):
        """FAILED and soft-deleted rows are invisible — not counted in skipped.

        Counting them would disclose how many hidden items the collection
        contains; the bulk-rename response includes ``skipped`` only for
        rows the caller *can* see but cannot write.
        """
        from django.utils import timezone

        from recordings.models import Recording

        c, user = auth_client
        col = baker.make(Collection, author=user)
        ct = self._recording_ct()

        ready = self._make_recording(user)
        failed = baker.make(Recording, author=user, file_size=1, status=Recording.Status.FAILED)
        deleted = baker.make(
            Recording,
            author=user,
            file_size=1,
            status=Recording.Status.READY,
            deleted_at=timezone.now(),
        )

        for rec in [ready, failed, deleted]:
            CollectionItem.objects.create(collection=col, content_type=ct, object_id=str(rec.pk))

        resp = post_json(c, self._url_bulk_rename(col.pk), {})
        assert resp.status_code == 200
        assert resp.json() == {"renamed": 1, "skipped": 0}

    def test_ignores_non_recording_items(self, auth_client):
        """Other content-type items in the collection are not touched."""
        c, user = auth_client
        col = baker.make(Collection, author=user)
        col_ct = ContentType.objects.get_for_model(Collection, for_concrete_model=False)
        nested = baker.make(Collection, author=user, name="Nested")
        CollectionItem.objects.create(collection=col, content_type=col_ct, object_id=str(nested.pk))

        resp = post_json(c, self._url_bulk_rename(col.pk), {})
        assert resp.status_code == 200
        assert resp.json() == {"renamed": 0, "skipped": 0}

    def test_empty_collection_returns_zero(self, auth_client):
        c, user = auth_client
        col = baker.make(Collection, author=user)
        resp = post_json(c, self._url_bulk_rename(col.pk), {})
        assert resp.status_code == 200
        assert resp.json() == {"renamed": 0, "skipped": 0}

    def test_requires_read_on_collection(self, auth_client, make_user):
        """A user without read access to the collection is rejected with 403."""
        c, user = auth_client
        owner = make_user(username="col_owner")
        col = baker.make(Collection, author=owner)
        resp = post_json(c, self._url_bulk_rename(col.pk), {})
        assert resp.status_code == 403

    def test_unauthenticated_returns_401(self, client):
        col = baker.make(Collection, author=baker.make("user.User"))
        resp = post_json(client, _url(col.pk, "recordings/bulk-rename"), {})
        assert resp.status_code == 401

    def test_empty_prefix_falls_back_to_default(self, auth_client):
        c, user = auth_client
        col = baker.make(Collection, author=user)
        ct = self._recording_ct()
        rec = self._make_recording(user)
        CollectionItem.objects.create(collection=col, content_type=ct, object_id=str(rec.pk))

        resp = post_json(c, self._url_bulk_rename(col.pk), {"prefix": "   "})
        assert resp.status_code == 200
        rec.refresh_from_db()
        assert rec.display_name == "Recording 1"

    def test_overlong_prefix_returns_400(self, auth_client):
        """A prefix that would overflow ``Recording.display_name`` is rejected.

        Pre-fix the rename would proceed and the DB raise IntegrityError
        on the first row, producing a 500 instead of a clear 400.
        """
        c, user = auth_client
        col = baker.make(Collection, author=user)
        ct = self._recording_ct()
        rec = self._make_recording(user)
        CollectionItem.objects.create(collection=col, content_type=ct, object_id=str(rec.pk))

        long_prefix = "X" * 300
        resp = post_json(c, self._url_bulk_rename(col.pk), {"prefix": long_prefix})
        assert resp.status_code == 400
        rec.refresh_from_db()
        assert rec.display_name is None

    def test_writes_activity_row(self, auth_client):
        """Bulk-rename is a mutation; it must produce an Activity audit row."""
        from activity.models import Activity

        c, user = auth_client
        col = baker.make(Collection, author=user)
        ct = self._recording_ct()
        rec = self._make_recording(user)
        CollectionItem.objects.create(collection=col, content_type=ct, object_id=str(rec.pk))

        resp = post_json(c, self._url_bulk_rename(col.pk), {"prefix": "Subject"})
        assert resp.status_code == 200

        row = Activity.objects.filter(verb="library.collection.recordings.bulk_rename").first()
        assert row is not None
        assert row.metadata["renamed_count"] == 1
        assert row.metadata["skipped_count"] == 0
        assert row.metadata["prefix"] == "Subject"
        assert row.metadata["renamed_recording_pks"] == [rec.pk]


@pytest.mark.django_db
class TestAddItemRejectsFailedRecording:
    """``_check_item_readable`` must reject FAILED recordings explicitly.

    Pre-fix ``add_item`` returned 500 — the read check let the FAILED row
    through, then ``_enrich_collection_items([item])[0]`` raised
    IndexError when the FAILED filter dropped the item from the result
    list, leaking the existence of the FAILED row as a crash.
    """

    def _recording_ct(self):
        from recordings.models import Recording

        return ContentType.objects.get_for_model(Recording, for_concrete_model=False)

    def test_add_failed_recording_to_collection_returns_422(self, auth_client):
        from recordings.models import Recording

        c, user = auth_client
        col = baker.make(Collection, author=user)
        failed = baker.make(Recording, author=user, file_size=1, status=Recording.Status.FAILED)
        ct = self._recording_ct()

        resp = post_json(
            c,
            _url(col.pk, "items/"),
            {"content_type_id": ct.pk, "object_id": str(failed.pk)},
        )
        assert resp.status_code == 422

    def test_add_failed_recording_to_dataset_returns_422(self, auth_client):
        from recordings.models import Recording

        c, user = auth_client
        ds = baker.make(Dataset, author=user)
        failed = baker.make(Recording, author=user, file_size=1, status=Recording.Status.FAILED)
        ct = self._recording_ct()

        resp = post_json(
            c,
            f"/api/v1/library/datasets/{ds.pk}/items/",
            {"content_type_id": ct.pk, "object_id": str(failed.pk)},
        )
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Access rights management
# ---------------------------------------------------------------------------


# ===========================================================================
# Dataset API tests
# ===========================================================================

DS_BASE = "/api/v1/library/datasets/"


def _ds_url(dataset_id=None, suffix=""):
    if dataset_id is None:
        return DS_BASE
    return f"{DS_BASE}{dataset_id}/{suffix}"


def _recording_ct():
    from recordings.models import Recording

    return ContentType.objects.get_for_model(Recording, for_concrete_model=False)


def _make_recording(user):
    from recordings.models import Recording

    return baker.make(Recording, author=user, file_size=1, status=Recording.Status.READY)


def _dataset_ct():
    return ContentType.objects.get_for_model(Dataset, for_concrete_model=False)


def _grant_dataset_write(owner, user, dataset):
    ct = _dataset_ct()
    return AccessRight.objects.create(
        content_type=ct,
        object_id=str(dataset.pk),
        access_giver=owner,
        access_target=user,
        can_read=True,
        can_write=True,
        can_share=True,
    )


@pytest.mark.django_db
class TestCreateDataset:
    def test_unauthenticated_returns_401(self, client):
        assert post_json(client, DS_BASE, {"name": "X"}).status_code == 401

    def test_creates_dataset(self, auth_client):
        c, user = auth_client
        resp = post_json(c, DS_BASE, {"name": "DS1", "description": "d"})
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "DS1"
        assert data["author_id"] == user.pk
        assert data["parent_id"] is None  # always flat

    def test_author_gets_full_access_right(self, auth_client):
        c, user = auth_client
        resp = post_json(c, DS_BASE, {"name": "DS"})
        ds_id = resp.json()["id"]
        ct = _dataset_ct()
        assert AccessRight.objects.filter(
            content_type=ct,
            object_id=str(ds_id),
            access_target=user,
            can_read=True,
            can_write=True,
            can_share=True,
        ).exists()

    def test_create_with_recording_hashes_adds_items(self, auth_client):
        c, user = auth_client
        rec1 = _make_recording(user)
        rec2 = _make_recording(user)
        resp = post_json(
            c,
            DS_BASE,
            {
                "name": "DS",
                "recording_hashes": [str(rec1.pk), str(rec2.pk)],
            },
        )
        assert resp.status_code == 201
        ds_id = resp.json()["id"]
        ct = _recording_ct()
        assert DatasetItem.objects.filter(dataset_id=ds_id, content_type=ct, object_id=str(rec1.pk)).exists()
        assert DatasetItem.objects.filter(dataset_id=ds_id, content_type=ct, object_id=str(rec2.pk)).exists()

    def test_invalid_hash_rolls_back_entire_creation(self, auth_client):
        """If any identifier is invalid the whole transaction rolls back — no orphaned dataset."""
        c, user = auth_client
        rec = _make_recording(user)
        resp = post_json(
            c,
            DS_BASE,
            {
                "name": "ShouldNotExist",
                "recording_hashes": [str(rec.pk), "99999999"],
            },
        )
        assert resp.status_code == 404
        assert not Dataset.objects.filter(name="ShouldNotExist").exists()

    def test_inaccessible_recording_rolls_back_entire_creation(self, auth_client, make_user):
        """If a recording is unreadable the whole transaction rolls back."""
        c, user = auth_client
        owner = make_user()
        rec = _make_recording(owner)  # not shared with user
        resp = post_json(
            c,
            DS_BASE,
            {
                "name": "ShouldNotExist",
                "recording_hashes": [str(rec.pk)],
            },
        )
        assert resp.status_code == 403
        assert not Dataset.objects.filter(name="ShouldNotExist").exists()

    def test_duplicate_hash_in_list_is_idempotent(self, auth_client):
        """Passing the same identifier twice results in a single item, not a 409."""
        c, user = auth_client
        rec = _make_recording(user)
        resp = post_json(
            c,
            DS_BASE,
            {
                "name": "DS",
                "recording_hashes": [str(rec.pk), str(rec.pk)],
            },
        )
        assert resp.status_code == 201
        ds_id = resp.json()["id"]
        assert DatasetItem.objects.filter(dataset_id=ds_id).count() == 1


@pytest.mark.django_db
class TestListDatasets:
    def test_unauthenticated_returns_401(self, client):
        assert client.get(DS_BASE).status_code == 401

    def test_author_sees_own(self, auth_client):
        c, user = auth_client
        baker.make(Dataset, author=user, _quantity=3)
        resp = c.get(DS_BASE)
        assert resp.status_code == 200
        assert len(resp.json()) == 3

    def test_shared_dataset_visible(self, auth_client, make_user):
        c, user = auth_client
        other = make_user(username="other")
        ds = baker.make(Dataset, author=other)
        ct = _dataset_ct()
        AccessRight.objects.create(
            content_type=ct,
            object_id=str(ds.pk),
            access_giver=other,
            access_target=user,
            can_read=True,
        )
        ids = [d["id"] for d in c.get(DS_BASE).json()]
        assert ds.pk in ids

    def test_trash_returns_own_deleted(self, auth_client):
        from django.utils import timezone

        c, user = auth_client
        ds = baker.make(Dataset, author=user)
        Dataset.objects.filter(pk=ds.pk).update(deleted_at=timezone.now())
        resp = c.get(DS_BASE, {"trash": "true"})
        assert any(d["id"] == ds.pk for d in resp.json())


@pytest.mark.django_db
class TestGetUpdateDeleteDataset:
    def test_get_own(self, auth_client):
        c, user = auth_client
        ds = baker.make(Dataset, author=user, name="MyDS")
        resp = c.get(_ds_url(ds.pk))
        assert resp.status_code == 200
        assert resp.json()["name"] == "MyDS"

    def test_get_unshared_returns_403(self, auth_client, make_user):
        c, _ = auth_client
        other = make_user(username="other")
        ds = baker.make(Dataset, author=other)
        assert c.get(_ds_url(ds.pk)).status_code == 403

    def test_author_can_update(self, auth_client):
        c, user = auth_client
        ds = baker.make(Dataset, author=user, name="Old")
        resp = patch_json(c, _ds_url(ds.pk), {"name": "New"})
        assert resp.status_code == 200
        assert resp.json()["name"] == "New"

    def test_non_author_without_write_cannot_update(self, auth_client, make_user):
        c, user = auth_client
        other = make_user(username="other")
        ds = baker.make(Dataset, author=other)
        ct = _dataset_ct()
        AccessRight.objects.create(
            content_type=ct,
            object_id=str(ds.pk),
            access_giver=other,
            access_target=user,
            can_read=True,
        )
        assert patch_json(c, _ds_url(ds.pk), {"name": "X"}).status_code == 403

    def test_get_returns_viewer_config(self, auth_client):
        c, user = auth_client
        ds = baker.make(Dataset, author=user, viewer_config={"eeg.defaultMontage": "lon"})
        resp = c.get(_ds_url(ds.pk))
        assert resp.status_code == 200
        assert resp.json()["viewer_config"] == {"eeg.defaultMontage": "lon"}

    def test_get_returns_empty_viewer_config_by_default(self, auth_client):
        c, user = auth_client
        ds = baker.make(Dataset, author=user)
        assert c.get(_ds_url(ds.pk)).json()["viewer_config"] == {}

    def test_author_can_set_viewer_config(self, auth_client):
        c, user = auth_client
        ds = baker.make(Dataset, author=user)
        resp = patch_json(c, _ds_url(ds.pk), {"viewer_config": {"eeg.defaultMontage": "lon"}})
        assert resp.status_code == 200
        assert resp.json()["viewer_config"] == {"eeg.defaultMontage": "lon"}
        ds.refresh_from_db()
        assert ds.viewer_config == {"eeg.defaultMontage": "lon"}

    def test_setting_viewer_config_leaves_name_untouched(self, auth_client):
        c, user = auth_client
        ds = baker.make(Dataset, author=user, name="Keep")
        patch_json(c, _ds_url(ds.pk), {"viewer_config": {"a": 1}})
        ds.refresh_from_db()
        assert ds.name == "Keep"

    def test_non_author_without_write_cannot_set_viewer_config(self, auth_client, make_user):
        c, user = auth_client
        other = make_user(username="other")
        ds = baker.make(Dataset, author=other)
        AccessRight.objects.create(
            content_type=_dataset_ct(),
            object_id=str(ds.pk),
            access_giver=other,
            access_target=user,
            can_read=True,
        )
        resp = patch_json(c, _ds_url(ds.pk), {"viewer_config": {"a": 1}})
        assert resp.status_code == 403
        ds.refresh_from_db()
        assert ds.viewer_config == {}

    def test_non_object_viewer_config_rejected(self, auth_client):
        c, user = auth_client
        ds = baker.make(Dataset, author=user)
        resp = patch_json(c, _ds_url(ds.pk), {"viewer_config": ["not", "a", "map"]})
        assert resp.status_code == 422

    def test_author_can_soft_delete(self, auth_client):
        c, user = auth_client
        ds = baker.make(Dataset, author=user)
        resp = c.delete(_ds_url(ds.pk))
        assert resp.status_code == 200
        ds.refresh_from_db()
        assert ds.deleted_at is not None

    def test_deleted_dataset_not_found(self, auth_client):
        from django.utils import timezone

        c, user = auth_client
        ds = baker.make(Dataset, author=user)
        Dataset.objects.filter(pk=ds.pk).update(deleted_at=timezone.now())
        assert c.get(_ds_url(ds.pk)).status_code == 404


# ---------------------------------------------------------------------------
# Dataset item membership
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestDatasetItemMembership:
    def test_add_item(self, auth_client):
        c, user = auth_client
        ds = baker.make(Dataset, author=user)
        rec = _make_recording(user)
        ct = _recording_ct()
        resp = post_json(
            c,
            _ds_url(ds.pk, "items/"),
            {
                "content_type_id": ct.pk,
                "object_id": str(rec.pk),
            },
        )
        assert resp.status_code == 201
        assert DatasetItem.objects.filter(dataset=ds, content_type=ct, object_id=str(rec.pk)).exists()

    def test_add_duplicate_returns_409(self, auth_client):
        c, user = auth_client
        ds = baker.make(Dataset, author=user)
        rec = _make_recording(user)
        ct = _recording_ct()
        payload = {"content_type_id": ct.pk, "object_id": str(rec.pk)}
        post_json(c, _ds_url(ds.pk, "items/"), payload)
        assert post_json(c, _ds_url(ds.pk, "items/"), payload).status_code == 409

    def test_remove_item(self, auth_client):
        c, user = auth_client
        ds = baker.make(Dataset, author=user)
        rec = _make_recording(user)
        ct = _recording_ct()
        item = DatasetItem.objects.create(dataset=ds, content_type=ct, object_id=str(rec.pk))
        resp = c.delete(_ds_url(ds.pk, f"items/{item.pk}/"))
        assert resp.status_code == 200
        assert not DatasetItem.objects.filter(pk=item.pk).exists()

    def test_add_item_requires_write_on_dataset(self, auth_client, make_user):
        c, user = auth_client
        other = make_user(username="other")
        ds = baker.make(Dataset, author=other)
        rec = _make_recording(user)
        ct_col = _dataset_ct()
        AccessRight.objects.create(
            content_type=ct_col,
            object_id=str(ds.pk),
            access_giver=other,
            access_target=user,
            can_read=True,
            can_write=False,
        )
        resp = post_json(
            c,
            _ds_url(ds.pk, "items/"),
            {
                "content_type_id": _recording_ct().pk,
                "object_id": str(rec.pk),
            },
        )
        assert resp.status_code == 403

    def test_add_item_requires_read_on_object(self, auth_client, make_user):
        c, user = auth_client
        other = make_user(username="other")
        ds = baker.make(Dataset, author=user)
        rec = _make_recording(other)  # not shared with user
        resp = post_json(
            c,
            _ds_url(ds.pk, "items/"),
            {
                "content_type_id": _recording_ct().pk,
                "object_id": str(rec.pk),
            },
        )
        assert resp.status_code == 403

    def test_list_items(self, auth_client):
        c, user = auth_client
        ds = baker.make(Dataset, author=user)
        rec = _make_recording(user)
        ct = _recording_ct()
        DatasetItem.objects.create(dataset=ds, content_type=ct, object_id=str(rec.pk))
        resp = c.get(_ds_url(ds.pk, "items/"))
        assert resp.status_code == 200
        assert len(resp.json()) == 1

    def test_remove_wrong_dataset_returns_404(self, auth_client):
        c, user = auth_client
        ds_a = baker.make(Dataset, author=user)
        ds_b = baker.make(Dataset, author=user)
        rec = _make_recording(user)
        ct = _recording_ct()
        item = DatasetItem.objects.create(dataset=ds_b, content_type=ct, object_id=str(rec.pk))
        assert c.delete(_ds_url(ds_a.pk, f"items/{item.pk}/")).status_code == 404

    def test_list_items_shows_all_including_otherwise_inaccessible(self, auth_client, make_user):
        """Dataset read access grants access to all items — no per-item filtering.

        A user who can read a Dataset sees all its items regardless of whether
        they have a direct AccessRight on each object. This is the core purpose
        of the Dataset model.
        """
        c, user = auth_client
        owner = make_user(username="owner")
        ds = baker.make(Dataset, author=owner)
        ct = _recording_ct()
        ds_ct = _dataset_ct()

        # Item owned by owner — user has no direct AccessRight on it
        private_rec = _make_recording(owner)
        DatasetItem.objects.create(dataset=ds, content_type=ct, object_id=str(private_rec.pk))

        # Grant dataset-level read to user
        AccessRight.objects.create(
            content_type=ds_ct,
            object_id=str(ds.pk),
            access_giver=owner,
            access_target=user,
            can_read=True,
        )

        resp = c.get(_ds_url(ds.pk, "items/"))
        assert resp.status_code == 200
        assert any(item["object_id"] == str(private_rec.pk) for item in resp.json())


# ---------------------------------------------------------------------------
# Dataset → item permission inheritance (via recordings API)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestDatasetPermissionInheritanceViaAPI:
    """Verify that the permission extension works transparently through the recordings API."""

    def test_user_sees_recording_in_list_via_dataset(self, auth_client, make_user):
        c, user = auth_client
        owner = make_user(username="owner")
        rec = _make_recording(owner)

        ds = baker.make(Dataset, author=owner)
        rec_ct = _recording_ct()
        DatasetItem.objects.create(dataset=ds, content_type=rec_ct, object_id=str(rec.pk))

        ct = _dataset_ct()
        AccessRight.objects.create(
            content_type=ct,
            object_id=str(ds.pk),
            access_giver=owner,
            access_target=user,
            can_read=True,
        )

        # The recording should now appear in the user's recording listing
        resp = c.get("/recordings/api/v1/")
        assert resp.status_code == 200
        hashes = [r["hash"] for r in resp.json()]
        assert rec.stored_name.split(".")[0] in hashes

    def test_user_blocked_from_recording_without_dataset_right(self, auth_client, make_user):
        c, user = auth_client
        owner = make_user(username="owner")
        rec = _make_recording(owner)

        ds = baker.make(Dataset, author=owner)
        rec_ct = _recording_ct()
        DatasetItem.objects.create(dataset=ds, content_type=rec_ct, object_id=str(rec.pk))
        # No AccessRight on the dataset for user

        resp = c.get("/recordings/api/v1/")
        ids = [r["id"] for r in resp.json()]
        assert rec.pk not in ids

    def test_deleted_dataset_removes_item_from_listing(self, auth_client, make_user):
        from django.utils import timezone

        c, user = auth_client
        owner = make_user(username="owner")
        rec = _make_recording(owner)

        ds = baker.make(Dataset, author=owner)
        rec_ct = _recording_ct()
        DatasetItem.objects.create(dataset=ds, content_type=rec_ct, object_id=str(rec.pk))
        ct = _dataset_ct()
        AccessRight.objects.create(
            content_type=ct,
            object_id=str(ds.pk),
            access_giver=owner,
            access_target=user,
            can_read=True,
        )

        # Soft-delete the dataset
        Dataset.objects.filter(pk=ds.pk).update(deleted_at=timezone.now())

        resp = c.get("/recordings/api/v1/")
        ids = [r["id"] for r in resp.json()]
        assert rec.pk not in ids


# ---------------------------------------------------------------------------
# Dataset access rights management
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestDatasetAccessRights:
    def _make_ds_with_right(self, user):
        ds = baker.make(Dataset, author=user)
        ct = _dataset_ct()
        right = AccessRight.objects.create(
            content_type=ct,
            object_id=str(ds.pk),
            access_giver=user,
            access_target=user,
            can_read=True,
            can_write=True,
            can_share=True,
        )
        return ds, ct, right

    def test_author_can_list_access(self, auth_client, make_user):
        c, user = auth_client
        ds, ct, _ = self._make_ds_with_right(user)
        # Create a right for a different user — the author's own right is excluded
        # from the list by design (the author always has implicit full access).
        grantee = make_user()
        right = AccessRight.objects.create(
            content_type=ct,
            object_id=str(ds.pk),
            access_giver=user,
            access_target=grantee,
            can_read=True,
        )
        resp = c.get(_ds_url(ds.pk, "access/"))
        assert resp.status_code == 200
        assert any(d["id"] == right.pk for d in resp.json())

    def test_list_access_requires_write(self, auth_client, make_user):
        c, user = auth_client
        other = make_user(username="other")
        ds = baker.make(Dataset, author=other)
        ct = _dataset_ct()
        AccessRight.objects.create(
            content_type=ct,
            object_id=str(ds.pk),
            access_giver=other,
            access_target=user,
            can_read=True,
        )
        assert c.get(_ds_url(ds.pk, "access/")).status_code == 403

    def test_grant_access(self, auth_client, make_user):
        c, user = auth_client
        ds, _, _ = self._make_ds_with_right(user)
        other = make_user(username="other")
        resp = post_json(
            c,
            _ds_url(ds.pk, "access/"),
            {
                "access_target_id": other.pk,
                "can_read": True,
            },
        )
        assert resp.status_code == 201
        assert resp.json()["access_target_id"] == other.pk

    def test_grant_access_duplicate_token_returns_409(self, auth_client):
        c, user = auth_client
        ds, ct, _ = self._make_ds_with_right(user)
        AccessRight.objects.create(
            content_type=ct,
            object_id=str(ds.pk),
            access_giver=user,
            public_share_token="dstoken",
            can_read=True,
        )
        resp = post_json(
            c,
            _ds_url(ds.pk, "access/"),
            {
                "public_share_token": "dstoken",
                "can_read": True,
            },
        )
        assert resp.status_code == 409

    def test_grant_access_duplicate_user_target_returns_409(self, auth_client, make_user):
        c, user = auth_client
        ds, ct, _ = self._make_ds_with_right(user)
        other = make_user(username="dupuser")
        AccessRight.objects.create(
            content_type=ct,
            object_id=str(ds.pk),
            access_giver=user,
            access_target=other,
            can_read=True,
        )
        resp = post_json(
            c,
            _ds_url(ds.pk, "access/"),
            {
                "access_target_id": other.pk,
                "can_read": True,
                "can_write": True,
            },
        )
        assert resp.status_code == 409
        assert "already has an access right" in resp.json()["detail"]

    def test_grant_access_duplicate_group_target_returns_409(self, auth_client):
        from django.contrib.auth.models import Group

        c, user = auth_client
        ds, ct, _ = self._make_ds_with_right(user)
        group = Group.objects.create(name="dupgroup")
        AccessRight.objects.create(
            content_type=ct,
            object_id=str(ds.pk),
            access_giver=user,
            access_target_group=group,
            can_read=True,
        )
        resp = post_json(
            c,
            _ds_url(ds.pk, "access/"),
            {
                "access_target_group_id": group.pk,
                "can_read": True,
            },
        )
        assert resp.status_code == 409

    def test_revoke_access(self, auth_client, make_user):
        c, user = auth_client
        other = make_user(username="other")
        ds, ct, _ = self._make_ds_with_right(user)
        right = AccessRight.objects.create(
            content_type=ct,
            object_id=str(ds.pk),
            access_giver=user,
            access_target=other,
            can_read=True,
        )
        resp = c.delete(_ds_url(ds.pk, f"access/{right.pk}/"))
        assert resp.status_code == 200
        assert not AccessRight.objects.filter(pk=right.pk).exists()

    def test_revoke_wrong_dataset_returns_404(self, auth_client):
        c, user = auth_client
        ds_a, ct, _ = self._make_ds_with_right(user)
        ds_b = baker.make(Dataset, author=user)
        right_b = AccessRight.objects.create(
            content_type=ct,
            object_id=str(ds_b.pk),
            access_giver=user,
            access_target=user,
            can_read=True,
        )
        assert c.delete(_ds_url(ds_a.pk, f"access/{right_b.pk}/")).status_code == 404


# ---------------------------------------------------------------------------
# Tags
# ---------------------------------------------------------------------------

TAGS_BASE = "/api/v1/library/tags/"


def _tag_url(tag_id=None, suffix=""):
    if tag_id is None:
        return TAGS_BASE
    return f"{TAGS_BASE}{tag_id}/{suffix}"


def _make_recording(user):
    from recordings.models import Recording

    return baker.make(Recording, author=user, file_size=1, status=Recording.Status.READY)


@pytest.mark.django_db
class TestListTags:
    def test_unauthenticated_returns_401(self, client):
        assert client.get(TAGS_BASE).status_code == 401

    def test_lists_root_tags(self, auth_client):
        c, user = auth_client
        baker.make(Tag, author=user, _quantity=3)
        resp = c.get(TAGS_BASE)
        assert resp.status_code == 200
        assert len(resp.json()) == 3

    def test_lists_children_by_parent_id(self, auth_client):
        c, user = auth_client
        parent = baker.make(Tag, author=user, name="Parent")
        baker.make(Tag, author=user, parent=parent, _quantity=2)
        resp = c.get(TAGS_BASE, {"parent_id": parent.pk})
        assert resp.status_code == 200
        assert len(resp.json()) == 2

    def test_root_listing_excludes_children(self, auth_client):
        c, user = auth_client
        parent = baker.make(Tag, author=user, name="Root")
        baker.make(Tag, author=user, parent=parent, name="Child")
        resp = c.get(TAGS_BASE)
        assert resp.status_code == 200
        ids = [t["id"] for t in resp.json()]
        assert parent.pk in ids
        assert all(Tag.objects.get(pk=i).parent_id is None for i in ids)

    def test_parent_id_404_for_missing_tag(self, auth_client):
        c, _ = auth_client
        resp = c.get(TAGS_BASE, {"parent_id": 99999})
        assert resp.status_code == 404


@pytest.mark.django_db
class TestCreateTag:
    def test_unauthenticated_returns_401(self, client):
        assert post_json(client, TAGS_BASE, {"name": "X"}).status_code == 401

    def test_creates_root_tag(self, auth_client):
        c, user = auth_client
        resp = post_json(c, TAGS_BASE, {"name": "EEG", "description": "Brainwaves"})
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "EEG"
        assert data["description"] == "Brainwaves"
        assert data["parent_id"] is None
        assert data["author_id"] == user.pk

    def test_creates_nested_tag(self, auth_client):
        c, user = auth_client
        parent = baker.make(Tag, author=user, name="EEG")
        resp = post_json(c, TAGS_BASE, {"name": "Artifact", "parent_id": parent.pk})
        assert resp.status_code == 201
        assert resp.json()["parent_id"] == parent.pk

    def test_nonexistent_parent_returns_404(self, auth_client):
        c, _ = auth_client
        assert post_json(c, TAGS_BASE, {"name": "X", "parent_id": 99999}).status_code == 404


@pytest.mark.django_db
class TestGetTag:
    def test_unauthenticated_returns_401(self, client):
        assert client.get(_tag_url(1)).status_code == 401

    def test_retrieves_tag(self, auth_client):
        c, user = auth_client
        tag = baker.make(Tag, author=user, name="EEG")
        resp = c.get(_tag_url(tag.pk))
        assert resp.status_code == 200
        assert resp.json()["name"] == "EEG"

    def test_missing_tag_returns_404(self, auth_client):
        c, _ = auth_client
        assert c.get(_tag_url(99999)).status_code == 404


@pytest.mark.django_db
class TestUpdateTag:
    def test_author_can_update_name(self, auth_client):
        c, user = auth_client
        tag = baker.make(Tag, author=user, name="Old")
        resp = patch_json(c, _tag_url(tag.pk), {"name": "New"})
        assert resp.status_code == 200
        assert resp.json()["name"] == "New"

    def test_non_author_gets_403(self, auth_client, make_user):
        c, _ = auth_client
        other = make_user(username="other")
        tag = baker.make(Tag, author=other, name="Theirs")
        assert patch_json(c, _tag_url(tag.pk), {"name": "Mine"}).status_code == 403

    def test_superuser_can_update(self, superuser_client):
        c, su = superuser_client
        tag = baker.make(Tag, author=su, name="Old")
        resp = patch_json(c, _tag_url(tag.pk), {"name": "Updated"})
        assert resp.status_code == 200

    def test_reparent_tag(self, auth_client):
        c, user = auth_client
        root = baker.make(Tag, author=user, name="Root")
        child = baker.make(Tag, author=user, name="Child")
        resp = patch_json(c, _tag_url(child.pk), {"parent_id": root.pk})
        assert resp.status_code == 200
        assert resp.json()["parent_id"] == root.pk

    def test_cycle_detection(self, auth_client):
        """Cannot move a tag into one of its own descendants."""
        c, user = auth_client
        parent = baker.make(Tag, author=user, name="Parent")
        child = baker.make(Tag, author=user, name="Child", parent=parent)
        resp = patch_json(c, _tag_url(parent.pk), {"parent_id": child.pk})
        assert resp.status_code == 400

    def test_promote_to_root_with_null_parent(self, auth_client):
        c, user = auth_client
        root = baker.make(Tag, author=user, name="Root")
        child = baker.make(Tag, author=user, name="Child", parent=root)
        resp = patch_json(c, _tag_url(child.pk), {"parent_id": None})
        assert resp.status_code == 200
        assert resp.json()["parent_id"] is None


@pytest.mark.django_db
class TestDeleteTag:
    def test_author_can_delete(self, auth_client):
        c, user = auth_client
        tag = baker.make(Tag, author=user)
        assert c.delete(_tag_url(tag.pk)).status_code == 200
        assert not Tag.objects.filter(pk=tag.pk).exists()

    def test_non_author_gets_403(self, auth_client, make_user):
        c, _ = auth_client
        other = make_user(username="other")
        tag = baker.make(Tag, author=other)
        assert c.delete(_tag_url(tag.pk)).status_code == 403

    def test_children_become_root_on_delete(self, auth_client):
        c, user = auth_client
        parent = baker.make(Tag, author=user, name="Parent")
        child = baker.make(Tag, author=user, name="Child", parent=parent)
        c.delete(_tag_url(parent.pk))
        child.refresh_from_db()
        assert child.parent_id is None


@pytest.mark.django_db
class TestTagItem:
    def test_unauthenticated_returns_401(self, client, make_user):
        user = make_user(username="owner")
        rec = _make_recording(user)
        tag = baker.make(Tag, author=user)
        ct = ContentType.objects.get_for_model(rec, for_concrete_model=False)
        resp = post_json(
            client,
            _tag_url(tag.pk, "items/"),
            {
                "content_type_id": ct.pk,
                "object_id": str(rec.pk),
            },
        )
        assert resp.status_code == 401

    def test_author_can_tag_own_recording(self, auth_client):
        c, user = auth_client
        rec = _make_recording(user)
        tag = baker.make(Tag, author=user)
        ct = ContentType.objects.get_for_model(rec, for_concrete_model=False)
        resp = post_json(
            c,
            _tag_url(tag.pk, "items/"),
            {
                "content_type_id": ct.pk,
                "object_id": str(rec.pk),
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["tag_id"] == tag.pk
        assert data["object_id"] == str(rec.pk)

    def test_duplicate_tag_returns_409(self, auth_client):
        c, user = auth_client
        rec = _make_recording(user)
        tag = baker.make(Tag, author=user)
        ct = ContentType.objects.get_for_model(rec, for_concrete_model=False)
        payload = {"content_type_id": ct.pk, "object_id": str(rec.pk)}
        post_json(c, _tag_url(tag.pk, "items/"), payload)
        assert post_json(c, _tag_url(tag.pk, "items/"), payload).status_code == 409

    def test_no_write_access_to_object_returns_403(self, auth_client, make_user):
        c, user = auth_client
        owner = make_user(username="owner")
        rec = _make_recording(owner)
        tag = baker.make(Tag, author=user)
        ct = ContentType.objects.get_for_model(rec, for_concrete_model=False)
        resp = post_json(
            c,
            _tag_url(tag.pk, "items/"),
            {
                "content_type_id": ct.pk,
                "object_id": str(rec.pk),
            },
        )
        assert resp.status_code == 403

    def test_missing_tag_returns_404(self, auth_client, make_user):
        c, user = auth_client
        rec = _make_recording(user)
        ct = ContentType.objects.get_for_model(rec, for_concrete_model=False)
        resp = post_json(
            c,
            _tag_url(99999, "items/"),
            {
                "content_type_id": ct.pk,
                "object_id": str(rec.pk),
            },
        )
        assert resp.status_code == 404


@pytest.mark.django_db
class TestUntagItem:
    def _setup(self, user):
        rec = _make_recording(user)
        tag = baker.make(Tag, author=user, name="T")
        ct = ContentType.objects.get_for_model(rec, for_concrete_model=False)
        item = TaggedItem.objects.create(tag=tag, content_type=ct, object_id=str(rec.pk))
        return tag, item, rec

    def test_object_owner_can_untag(self, auth_client):
        c, user = auth_client
        tag, item, _ = self._setup(user)
        resp = c.delete(_tag_url(tag.pk, f"items/{item.pk}/"))
        assert resp.status_code == 200
        assert not TaggedItem.objects.filter(pk=item.pk).exists()

    def test_tag_author_can_untag_others_object(self, auth_client, make_user):
        """Tag author may remove their tag even from objects they don't own."""
        c, user = auth_client
        other = make_user(username="other")
        rec = _make_recording(other)
        tag = baker.make(Tag, author=user, name="MyTag")
        ct = ContentType.objects.get_for_model(rec, for_concrete_model=False)
        item = TaggedItem.objects.create(tag=tag, content_type=ct, object_id=str(rec.pk))
        resp = c.delete(_tag_url(tag.pk, f"items/{item.pk}/"))
        assert resp.status_code == 200

    def test_unrelated_user_gets_403(self, auth_client, make_user):
        c, user = auth_client
        owner = make_user(username="owner")
        tag_author = make_user(username="tagauthor")
        rec = _make_recording(owner)
        tag = baker.make(Tag, author=tag_author, name="TheirTag")
        ct = ContentType.objects.get_for_model(rec, for_concrete_model=False)
        item = TaggedItem.objects.create(tag=tag, content_type=ct, object_id=str(rec.pk))
        # user is neither the tag author nor the recording owner
        resp = c.delete(_tag_url(tag.pk, f"items/{item.pk}/"))
        assert resp.status_code == 403

    def test_missing_item_returns_404(self, auth_client):
        c, user = auth_client
        tag = baker.make(Tag, author=user)
        assert c.delete(_tag_url(tag.pk, "items/99999/")).status_code == 404


@pytest.mark.django_db
class TestListTaggedItems:
    def _setup_tree(self, user):
        """Root → mid → leaf tag tree, each with one recording tagged."""
        root = baker.make(Tag, author=user, name="Root")
        mid = baker.make(Tag, author=user, name="Mid", parent=root)
        leaf = baker.make(Tag, author=user, name="Leaf", parent=mid)

        ct = ContentType.objects.get_for_model(
            __import__("recordings.models", fromlist=["Recording"]).Recording,
            for_concrete_model=False,
        )

        def _tag(tag, rec):
            TaggedItem.objects.create(tag=tag, content_type=ct, object_id=str(rec.pk))

        rec_root = _make_recording(user)
        rec_mid = _make_recording(user)
        rec_leaf = _make_recording(user)
        _tag(root, rec_root)
        _tag(mid, rec_mid)
        _tag(leaf, rec_leaf)

        return root, mid, leaf, rec_root, rec_mid, rec_leaf

    def test_unauthenticated_returns_401(self, client, make_user):
        user = make_user(username="u")
        tag = baker.make(Tag, author=user)
        assert client.get(_tag_url(tag.pk, "items/")).status_code == 401

    def test_include_children_default_returns_full_subtree(self, auth_client):
        c, user = auth_client
        root, mid, leaf, rec_root, rec_mid, rec_leaf = self._setup_tree(user)
        resp = c.get(_tag_url(root.pk, "items/"))
        assert resp.status_code == 200
        obj_ids = {item["object_id"] for item in resp.json()}
        assert str(rec_root.pk) in obj_ids
        assert str(rec_mid.pk) in obj_ids
        assert str(rec_leaf.pk) in obj_ids

    def test_include_children_false_returns_exact_tag_only(self, auth_client):
        c, user = auth_client
        root, mid, leaf, rec_root, rec_mid, rec_leaf = self._setup_tree(user)
        resp = c.get(_tag_url(root.pk, "items/"), {"include_children": "false"})
        assert resp.status_code == 200
        obj_ids = {item["object_id"] for item in resp.json()}
        assert str(rec_root.pk) in obj_ids
        assert str(rec_mid.pk) not in obj_ids
        assert str(rec_leaf.pk) not in obj_ids

    def test_mid_level_subtree(self, auth_client):
        c, user = auth_client
        root, mid, leaf, rec_root, rec_mid, rec_leaf = self._setup_tree(user)
        resp = c.get(_tag_url(mid.pk, "items/"))
        assert resp.status_code == 200
        obj_ids = {item["object_id"] for item in resp.json()}
        assert str(rec_root.pk) not in obj_ids
        assert str(rec_mid.pk) in obj_ids
        assert str(rec_leaf.pk) in obj_ids

    def test_content_type_filter(self, auth_client):
        c, user = auth_client
        root, mid, leaf, rec_root, rec_mid, rec_leaf = self._setup_tree(user)
        from recordings.models import Recording

        ct = ContentType.objects.get_for_model(Recording, for_concrete_model=False)
        resp = c.get(_tag_url(root.pk, "items/"), {"content_type_id": ct.pk})
        assert resp.status_code == 200
        assert len(resp.json()) == 3

    def test_missing_tag_returns_404(self, auth_client):
        c, _ = auth_client
        assert c.get(_tag_url(99999, "items/")).status_code == 404

    def test_hides_unreadable_items(self, auth_client, make_user):
        """Items the caller cannot read are excluded from tag search results."""
        from recordings.models import Recording

        c, user = auth_client
        owner = make_user(username="owner")
        tag = baker.make(Tag, author=user, name="T")
        ct = ContentType.objects.get_for_model(Recording, for_concrete_model=False)

        readable_rec = _make_recording(user)  # user owns → readable
        private_rec = _make_recording(owner)  # no access → hidden

        TaggedItem.objects.create(tag=tag, content_type=ct, object_id=str(readable_rec.pk))
        TaggedItem.objects.create(tag=tag, content_type=ct, object_id=str(private_rec.pk))

        resp = c.get(_tag_url(tag.pk, "items/"))
        assert resp.status_code == 200
        obj_ids = {item["object_id"] for item in resp.json()}
        assert str(readable_rec.pk) in obj_ids
        assert str(private_rec.pk) not in obj_ids

    def test_hides_failed_recordings(self, auth_client):
        """FAILED recordings are dropped from tag-item listings for every viewer.

        Mirrors the rule applied to collection / dataset items: a tag's item
        list never surfaces a FAILED recording, since downstream endpoints
        would 404 on it anyway.
        """
        from recordings.models import Recording

        c, user = auth_client
        tag = baker.make(Tag, author=user, name="T")
        ct = ContentType.objects.get_for_model(Recording, for_concrete_model=False)

        ready_rec = _make_recording(user)
        failed_rec = baker.make(Recording, author=user, file_size=1, status=Recording.Status.FAILED)
        TaggedItem.objects.create(tag=tag, content_type=ct, object_id=str(ready_rec.pk))
        TaggedItem.objects.create(tag=tag, content_type=ct, object_id=str(failed_rec.pk))

        resp = c.get(_tag_url(tag.pk, "items/"))
        assert resp.status_code == 200
        obj_ids = {item["object_id"] for item in resp.json()}
        assert str(ready_rec.pk) in obj_ids
        assert str(failed_rec.pk) not in obj_ids

    def test_shows_item_readable_via_dataset(self, auth_client, make_user):
        """Items accessible through Dataset membership are included in tag results."""
        from library.models import Dataset, DatasetItem

        c, user = auth_client
        owner = make_user(username="owner")
        tag = baker.make(Tag, author=user, name="T")
        rec = _make_recording(owner)
        rec_ct = ContentType.objects.get_for_model(
            __import__("recordings.models", fromlist=["Recording"]).Recording,
            for_concrete_model=False,
        )

        # Tag the recording
        TaggedItem.objects.create(tag=tag, content_type=rec_ct, object_id=str(rec.pk))

        # Grant access via a Dataset
        ds = baker.make(Dataset, author=owner)
        DatasetItem.objects.create(dataset=ds, content_type=rec_ct, object_id=str(rec.pk))
        ds_ct = ContentType.objects.get_for_model(Dataset, for_concrete_model=False)
        AccessRight.objects.create(
            content_type=ds_ct,
            object_id=str(ds.pk),
            access_giver=owner,
            access_target=user,
            can_read=True,
        )

        resp = c.get(_tag_url(tag.pk, "items/"))
        assert resp.status_code == 200
        assert any(item["object_id"] == str(rec.pk) for item in resp.json())


# ---------------------------------------------------------------------------
# Collection access inheritance via parent
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCollectionShareRemoval:
    """The collection sharing surface is gone: access endpoints 404 and stale rows grant nothing."""

    def _stale_grant(self, user, col, *, can_read=True, can_write=False, giver=None):
        ct = ContentType.objects.get_for_model(Collection, for_concrete_model=False)
        return AccessRight.objects.create(
            content_type=ct,
            object_id=str(col.pk),
            access_giver=giver or user,
            access_target=user,
            can_read=can_read,
            can_write=can_write,
        )

    def test_access_endpoints_are_gone(self, auth_client):
        c, user = auth_client
        col = baker.make(Collection, author=user)
        assert c.get(_url(col.pk, "access/")).status_code == 404
        assert post_json(c, _url(col.pk, "access/"), {"access_target_id": user.pk}).status_code == 404
        assert c.delete(_url(col.pk, "access/1/")).status_code == 404

    def test_stale_row_on_parent_grants_nothing_on_child(self, auth_client, make_user):
        c, user = auth_client
        other = make_user(username="other")
        parent = baker.make(Collection, author=other)
        child = baker.make(Collection, author=other, parent=parent)
        self._stale_grant(user, parent, can_read=True, can_write=True, giver=other)
        assert c.get(_url(parent.pk)).status_code == 403
        assert c.get(_url(child.pk)).status_code == 403
        assert patch_json(c, _url(child.pk), {"name": "Nope"}).status_code == 403


@pytest.mark.django_db
class TestLibraryAuditTrail:
    """Activity-row annotation contract for the library API.

    One representative test per endpoint, locking the verb + target +
    metadata shape. Tests use minimal setup (no real recordings unless
    the endpoint requires one) and exercise the success path only —
    failure-path verb coverage is implicit since the middleware default
    captures denied requests with method/status.
    """

    def _collection_ct(self):
        return ContentType.objects.get_for_model(Collection, for_concrete_model=False)

    def _dataset_ct(self):
        return ContentType.objects.get_for_model(Dataset, for_concrete_model=False)

    def _tag_ct(self):
        return ContentType.objects.get_for_model(Tag, for_concrete_model=False)

    def _access_right_ct(self):
        return ContentType.objects.get_for_model(AccessRight, for_concrete_model=False)

    def _ci_ct(self):
        return ContentType.objects.get_for_model(CollectionItem, for_concrete_model=False)

    def _di_ct(self):
        return ContentType.objects.get_for_model(DatasetItem, for_concrete_model=False)

    def _ti_ct(self):
        return ContentType.objects.get_for_model(TaggedItem, for_concrete_model=False)

    def _make_recording(self, user):
        from recordings.models import Recording

        return Recording.objects.create(
            author=user,
            original_name="audit.edf",
            stored_name="A" * 32 + ".edf",
            file_extension=".edf",
            file_size=1024,
            file_path="/tmp/audit.edf",
            file_hash="a" * 64,
            content_hash="b" * 64,
            status=Recording.Status.READY,
        )

    def _grant_self_dataset(self, user, ds):
        AccessRight.objects.create(
            content_type=self._dataset_ct(),
            object_id=str(ds.pk),
            access_giver=user,
            access_target=user,
            can_read=True,
            can_write=True,
            can_share=True,
        )

    # ── Collections ─────────────────────────────────────────────────────────

    def test_collection_create_records_verb(self, auth_client):
        from activity.models import Activity

        c, user = auth_client
        resp = post_json(c, BASE, {"name": "AuditCol"})
        assert resp.status_code == 201

        col = Collection.objects.get(name="AuditCol")
        activity = Activity.objects.filter(verb="library.collection.create").latest("created_at")
        assert activity.target_content_type_id == self._collection_ct().pk
        assert activity.target_object_id == str(col.pk)

    def test_collection_list_records_verb(self, auth_client):
        from activity.models import Activity

        c, user = auth_client
        baker.make(Collection, author=user, name="seed")
        resp = c.get(f"{BASE}?limit=10")
        assert resp.status_code == 200

        activity = Activity.objects.filter(verb="library.collection.list").latest("created_at")
        assert activity.metadata["limit"] == 10
        assert activity.metadata["offset"] == 0
        assert activity.metadata["trash"] is False
        assert "returned_count" in activity.metadata

    def test_collection_read_records_verb(self, auth_client):
        from activity.models import Activity

        c, user = auth_client
        col = baker.make(Collection, author=user)
        resp = c.get(_url(col.pk))
        assert resp.status_code == 200

        activity = Activity.objects.filter(verb="library.collection.read").latest("created_at")
        assert activity.target_object_id == str(col.pk)

    def test_collection_update_records_verb(self, auth_client):
        from activity.models import Activity

        c, user = auth_client
        col = baker.make(Collection, author=user)
        resp = patch_json(c, _url(col.pk), {"name": "Renamed"})
        assert resp.status_code == 200

        activity = Activity.objects.filter(verb="library.collection.update").latest("created_at")
        assert activity.target_object_id == str(col.pk)
        assert activity.metadata["fields_updated"] == ["name"]

    def test_collection_trash_records_verb(self, auth_client):
        from activity.models import Activity

        c, user = auth_client
        col = baker.make(Collection, author=user)
        resp = c.delete(_url(col.pk))
        assert resp.status_code == 200

        activity = Activity.objects.filter(verb="library.collection.trash").latest("created_at")
        assert activity.target_object_id == str(col.pk)

    def test_collection_item_list_records_verb(self, auth_client):
        from activity.models import Activity

        c, user = auth_client
        col = baker.make(Collection, author=user)
        resp = c.get(_url(col.pk, "items/"))
        assert resp.status_code == 200

        activity = Activity.objects.filter(verb="library.collection.item.list").latest("created_at")
        assert activity.target_object_id == str(col.pk)
        assert "returned_count" in activity.metadata

    def test_collection_item_add_records_verb(self, auth_client):
        from activity.models import Activity

        c, user = auth_client
        col = baker.make(Collection, author=user)
        rec = self._make_recording(user)
        ct = ContentType.objects.get_for_model(rec, for_concrete_model=False)
        resp = post_json(
            c,
            _url(col.pk, "items/"),
            {"content_type_id": ct.pk, "object_id": str(rec.pk)},
        )
        assert resp.status_code == 201

        item = CollectionItem.objects.get(collection=col, object_id=str(rec.pk))
        activity = Activity.objects.filter(verb="library.collection.item.add").latest("created_at")
        assert activity.target_content_type_id == self._ci_ct().pk
        assert activity.target_object_id == str(item.pk)

    def test_collection_item_remove_records_verb(self, auth_client):
        from activity.models import Activity

        c, user = auth_client
        col = baker.make(Collection, author=user)
        rec = self._make_recording(user)
        ct = ContentType.objects.get_for_model(rec, for_concrete_model=False)
        item = CollectionItem.objects.create(collection=col, content_type=ct, object_id=str(rec.pk))
        item_pk = item.pk
        resp = c.delete(_url(col.pk, f"items/{item_pk}/"))
        assert resp.status_code == 200

        activity = Activity.objects.filter(verb="library.collection.item.remove").latest("created_at")
        assert activity.target_content_type_id == self._ci_ct().pk
        assert activity.target_object_id == str(item_pk)

    def test_collection_recordings_bulk_rename_records_verb(self, auth_client):
        from activity.models import Activity

        c, user = auth_client
        col = baker.make(Collection, author=user)
        rec = self._make_recording(user)
        rec_ct = ContentType.objects.get_for_model(rec, for_concrete_model=False)
        CollectionItem.objects.create(collection=col, content_type=rec_ct, object_id=str(rec.pk))
        resp = post_json(c, _url(col.pk, "recordings/bulk-rename"), {"prefix": "Subject"})
        assert resp.status_code == 200

        activity = Activity.objects.filter(verb="library.collection.recordings.bulk_rename").latest("created_at")
        assert activity.target_object_id == str(col.pk)
        assert activity.metadata["renamed_count"] == 1
        assert activity.metadata["prefix"] == "Subject"

    # ── Datasets ────────────────────────────────────────────────────────────

    def test_dataset_create_records_verb(self, auth_client):
        from activity.models import Activity

        c, user = auth_client
        resp = post_json(c, DS_BASE, {"name": "AuditDS"})
        assert resp.status_code == 201

        ds = Dataset.objects.get(name="AuditDS")
        activity = Activity.objects.filter(verb="library.dataset.create").latest("created_at")
        assert activity.target_content_type_id == self._dataset_ct().pk
        assert activity.target_object_id == str(ds.pk)
        assert activity.metadata["initial_item_count"] == 0

    def test_dataset_list_records_verb(self, auth_client):
        from activity.models import Activity

        c, user = auth_client
        baker.make(Dataset, author=user)
        resp = c.get(f"{DS_BASE}?limit=5")
        assert resp.status_code == 200

        activity = Activity.objects.filter(verb="library.dataset.list").latest("created_at")
        assert activity.metadata["limit"] == 5
        assert "returned_count" in activity.metadata

    def test_dataset_read_records_verb(self, auth_client):
        from activity.models import Activity

        c, user = auth_client
        ds = baker.make(Dataset, author=user)
        resp = c.get(f"{DS_BASE}{ds.pk}/")
        assert resp.status_code == 200

        activity = Activity.objects.filter(verb="library.dataset.read").latest("created_at")
        assert activity.target_object_id == str(ds.pk)
        assert activity.metadata["share_token_used"] is False

    def test_dataset_update_records_verb(self, auth_client):
        from activity.models import Activity

        c, user = auth_client
        ds = baker.make(Dataset, author=user)
        self._grant_self_dataset(user, ds)
        resp = patch_json(c, f"{DS_BASE}{ds.pk}/", {"name": "Renamed"})
        assert resp.status_code == 200

        activity = Activity.objects.filter(verb="library.dataset.update").latest("created_at")
        assert activity.target_object_id == str(ds.pk)
        assert activity.metadata["fields_updated"] == ["name"]

    def test_dataset_trash_records_verb(self, auth_client):
        from activity.models import Activity

        c, user = auth_client
        ds = baker.make(Dataset, author=user)
        self._grant_self_dataset(user, ds)
        resp = c.delete(f"{DS_BASE}{ds.pk}/")
        assert resp.status_code == 200

        activity = Activity.objects.filter(verb="library.dataset.trash").latest("created_at")
        assert activity.target_object_id == str(ds.pk)

    def test_dataset_item_list_records_verb(self, auth_client):
        from activity.models import Activity

        c, user = auth_client
        ds = baker.make(Dataset, author=user)
        resp = c.get(f"{DS_BASE}{ds.pk}/items/")
        assert resp.status_code == 200

        activity = Activity.objects.filter(verb="library.dataset.item.list").latest("created_at")
        assert activity.target_object_id == str(ds.pk)
        assert "returned_count" in activity.metadata

    def test_dataset_item_add_records_verb(self, auth_client):
        from activity.models import Activity

        c, user = auth_client
        ds = baker.make(Dataset, author=user)
        self._grant_self_dataset(user, ds)
        rec = self._make_recording(user)
        ct = ContentType.objects.get_for_model(rec, for_concrete_model=False)
        resp = post_json(
            c,
            f"{DS_BASE}{ds.pk}/items/",
            {"content_type_id": ct.pk, "object_id": str(rec.pk)},
        )
        assert resp.status_code == 201

        item = DatasetItem.objects.get(dataset=ds, object_id=str(rec.pk))
        activity = Activity.objects.filter(verb="library.dataset.item.add").latest("created_at")
        assert activity.target_content_type_id == self._di_ct().pk
        assert activity.target_object_id == str(item.pk)

    def test_dataset_item_remove_records_verb(self, auth_client):
        from activity.models import Activity

        c, user = auth_client
        ds = baker.make(Dataset, author=user)
        self._grant_self_dataset(user, ds)
        rec = self._make_recording(user)
        ct = ContentType.objects.get_for_model(rec, for_concrete_model=False)
        item = DatasetItem.objects.create(dataset=ds, content_type=ct, object_id=str(rec.pk))
        item_pk = item.pk
        resp = c.delete(f"{DS_BASE}{ds.pk}/items/{item_pk}/")
        assert resp.status_code == 200

        activity = Activity.objects.filter(verb="library.dataset.item.remove").latest("created_at")
        assert activity.target_content_type_id == self._di_ct().pk
        assert activity.target_object_id == str(item_pk)

    def test_dataset_access_list_records_verb(self, auth_client):
        from activity.models import Activity

        c, user = auth_client
        ds = baker.make(Dataset, author=user)
        self._grant_self_dataset(user, ds)
        resp = c.get(f"{DS_BASE}{ds.pk}/access/")
        assert resp.status_code == 200

        activity = Activity.objects.filter(verb="library.dataset.access.list").latest("created_at")
        assert activity.target_object_id == str(ds.pk)
        assert "returned_count" in activity.metadata

    def test_dataset_access_grant_records_verb(self, auth_client, make_user):
        from activity.models import Activity

        c, user = auth_client
        ds = baker.make(Dataset, author=user)
        self._grant_self_dataset(user, ds)
        target = make_user()
        resp = post_json(
            c,
            f"{DS_BASE}{ds.pk}/access/",
            {"access_target_id": target.pk, "can_read": True},
        )
        assert resp.status_code == 201

        right_pk = resp.json()["id"]
        activity = Activity.objects.filter(verb="library.dataset.access.grant").latest("created_at")
        assert activity.target_content_type_id == self._access_right_ct().pk
        assert activity.target_object_id == str(right_pk)

    def test_dataset_access_revoke_records_verb(self, auth_client, make_user):
        from activity.models import Activity

        c, user = auth_client
        ds = baker.make(Dataset, author=user)
        self._grant_self_dataset(user, ds)
        target = make_user()
        right = AccessRight.objects.create(
            content_type=self._dataset_ct(),
            object_id=str(ds.pk),
            access_giver=user,
            access_target=target,
            can_read=True,
        )
        right_pk = right.pk
        resp = c.delete(f"{DS_BASE}{ds.pk}/access/{right_pk}/")
        assert resp.status_code == 200

        activity = Activity.objects.filter(verb="library.dataset.access.revoke").latest("created_at")
        assert activity.target_content_type_id == self._access_right_ct().pk
        assert activity.target_object_id == str(right_pk)

    # ── Tags ────────────────────────────────────────────────────────────────

    def test_tag_list_records_verb(self, auth_client):
        from activity.models import Activity

        c, user = auth_client
        baker.make(Tag, author=user)
        resp = c.get(TAGS_BASE)
        assert resp.status_code == 200

        activity = Activity.objects.filter(verb="library.tag.list").latest("created_at")
        assert "returned_count" in activity.metadata

    def test_tag_create_records_verb(self, auth_client):
        from activity.models import Activity

        c, user = auth_client
        resp = post_json(c, TAGS_BASE, {"name": "AuditTag"})
        assert resp.status_code == 201

        tag = Tag.objects.get(name="AuditTag")
        activity = Activity.objects.filter(verb="library.tag.create").latest("created_at")
        assert activity.target_content_type_id == self._tag_ct().pk
        assert activity.target_object_id == str(tag.pk)

    def test_tag_read_records_verb(self, auth_client):
        from activity.models import Activity

        c, user = auth_client
        tag = baker.make(Tag, author=user)
        resp = c.get(f"{TAGS_BASE}{tag.pk}/")
        assert resp.status_code == 200

        activity = Activity.objects.filter(verb="library.tag.read").latest("created_at")
        assert activity.target_object_id == str(tag.pk)

    def test_tag_update_records_verb(self, auth_client):
        from activity.models import Activity

        c, user = auth_client
        tag = baker.make(Tag, author=user)
        resp = patch_json(c, f"{TAGS_BASE}{tag.pk}/", {"name": "Renamed"})
        assert resp.status_code == 200

        activity = Activity.objects.filter(verb="library.tag.update").latest("created_at")
        assert activity.target_object_id == str(tag.pk)
        assert activity.metadata["fields_updated"] == ["name"]

    def test_tag_delete_records_verb(self, auth_client):
        from activity.models import Activity

        c, user = auth_client
        tag = baker.make(Tag, author=user)
        tag_pk = tag.pk
        resp = c.delete(f"{TAGS_BASE}{tag_pk}/")
        assert resp.status_code == 200

        activity = Activity.objects.filter(verb="library.tag.delete").latest("created_at")
        assert activity.target_content_type_id == self._tag_ct().pk
        assert activity.target_object_id == str(tag_pk)

    def test_tag_item_list_records_verb(self, auth_client):
        from activity.models import Activity

        c, user = auth_client
        tag = baker.make(Tag, author=user)
        resp = c.get(f"{TAGS_BASE}{tag.pk}/items/")
        assert resp.status_code == 200

        activity = Activity.objects.filter(verb="library.tag.item.list").latest("created_at")
        assert activity.target_object_id == str(tag.pk)
        assert "returned_count" in activity.metadata

    def test_tag_item_add_records_verb(self, auth_client):
        from activity.models import Activity

        c, user = auth_client
        tag = baker.make(Tag, author=user)
        rec = self._make_recording(user)
        ct = ContentType.objects.get_for_model(rec, for_concrete_model=False)
        resp = post_json(
            c,
            f"{TAGS_BASE}{tag.pk}/items/",
            {"content_type_id": ct.pk, "object_id": str(rec.pk)},
        )
        assert resp.status_code == 201

        item = TaggedItem.objects.get(tag=tag, object_id=str(rec.pk))
        activity = Activity.objects.filter(verb="library.tag.item.add").latest("created_at")
        assert activity.target_content_type_id == self._ti_ct().pk
        assert activity.target_object_id == str(item.pk)

    def test_tag_item_remove_records_verb(self, auth_client):
        from activity.models import Activity

        c, user = auth_client
        tag = baker.make(Tag, author=user)
        rec = self._make_recording(user)
        ct = ContentType.objects.get_for_model(rec, for_concrete_model=False)
        item = TaggedItem.objects.create(tag=tag, content_type=ct, object_id=str(rec.pk))
        item_pk = item.pk
        resp = c.delete(f"{TAGS_BASE}{tag.pk}/items/{item_pk}/")
        assert resp.status_code == 200

        activity = Activity.objects.filter(verb="library.tag.item.remove").latest("created_at")
        assert activity.target_content_type_id == self._ti_ct().pk
        assert activity.target_object_id == str(item_pk)
