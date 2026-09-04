"""Model-layer tests for the library app."""

import pytest
from django.contrib.contenttypes.models import ContentType
from model_bakery import baker

from epicurrents.models import AccessRight
from library.models import Collection, Dataset, DatasetItem, Tag, TaggedItem
from library.permissions import can_read_collection, can_write_collection


@pytest.mark.django_db
class TestCollectionStr:
    def test_str_returns_name(self, user):
        col = baker.make(Collection, author=user, name="My Collection")
        assert str(col) == "My Collection"


@pytest.mark.django_db
class TestCollectionParent:
    def test_children_become_root_when_parent_soft_deleted(self, user):
        """Children's parent FK is SET_NULL when parent is hard-deleted."""
        parent = baker.make(Collection, author=user, name="Parent")
        child = baker.make(Collection, author=user, name="Child", parent=parent)

        parent.delete()  # hard delete to trigger SET_NULL
        child.refresh_from_db()
        assert child.parent_id is None

    def test_nested_collections(self, user):
        root = baker.make(Collection, author=user, name="Root")
        mid = baker.make(Collection, author=user, name="Mid", parent=root)
        leaf = baker.make(Collection, author=user, name="Leaf", parent=mid)

        assert leaf.parent_id == mid.pk
        assert mid.parent_id == root.pk
        assert root.parent_id is None


@pytest.mark.django_db
class TestCollectionAuthorGate:
    """Collections are author-private: reads and writes gate on the author and superusers only."""

    def test_author_reads_and_writes(self, user):
        col = baker.make(Collection, author=user, name="Mine")
        assert can_read_collection(user=user, collection=col)
        assert can_write_collection(user=user, collection=col)

    def test_non_author_denied(self, user, make_user):
        other = make_user(username="other")
        col = baker.make(Collection, author=user, name="Private")
        assert not can_read_collection(user=other, collection=col)
        assert not can_write_collection(user=other, collection=col)

    def test_superuser_allowed(self, user, make_superuser):
        admin = make_superuser()
        col = baker.make(Collection, author=user, name="Anyone's")
        assert can_read_collection(user=admin, collection=col)
        assert can_write_collection(user=admin, collection=col)

    def test_stale_access_right_row_is_inert(self, user, make_user):
        from django.contrib.contenttypes.models import ContentType

        from epicurrents.models import AccessRight

        other = make_user(username="other")
        col = baker.make(Collection, author=user, name="Shared once")
        ct = ContentType.objects.get_for_model(Collection, for_concrete_model=False)
        AccessRight.objects.create(
            content_type=ct,
            object_id=str(col.pk),
            access_giver=user,
            access_target=other,
            can_read=True,
            can_write=True,
        )
        assert not can_read_collection(user=other, collection=col)
        assert not can_write_collection(user=other, collection=col)


# ---------------------------------------------------------------------------
# Dataset permission inheritance
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestDatasetPermissionInheritance:
    """Verify that can_read_object transparently honours Dataset membership."""

    def _make_recording(self, owner):
        from recordings.models import Recording

        return baker.make(Recording, author=owner, file_size=1, status=Recording.Status.READY)

    def _grant_dataset_read(self, dataset, giver, target_user):
        ct = ContentType.objects.get_for_model(Dataset, for_concrete_model=False)
        return AccessRight.objects.create(
            content_type=ct,
            object_id=str(dataset.pk),
            access_giver=giver,
            access_target=target_user,
            can_read=True,
        )

    def test_dataset_read_grants_item_read(self, user, make_user):
        from epicurrents.permissions import can_read_object
        from recordings.models import Recording

        owner = make_user(username="owner")
        rec = self._make_recording(owner)

        dataset = baker.make(Dataset, author=owner)
        rec_ct = ContentType.objects.get_for_model(Recording, for_concrete_model=False)
        DatasetItem.objects.create(dataset=dataset, content_type=rec_ct, object_id=str(rec.pk))
        self._grant_dataset_read(dataset, giver=owner, target_user=user)

        assert can_read_object(user=user, obj=rec)

    def test_dataset_read_does_not_grant_item_write(self, user, make_user):
        from epicurrents.permissions import can_write_object
        from recordings.models import Recording

        owner = make_user(username="owner")
        rec = self._make_recording(owner)

        dataset = baker.make(Dataset, author=owner)
        rec_ct = ContentType.objects.get_for_model(Recording, for_concrete_model=False)
        DatasetItem.objects.create(dataset=dataset, content_type=rec_ct, object_id=str(rec.pk))
        self._grant_dataset_read(dataset, giver=owner, target_user=user)

        assert not can_write_object(user=user, obj=rec)

    def test_no_dataset_right_denies_item_read(self, user, make_user):
        from epicurrents.permissions import can_read_object

        owner = make_user(username="owner")
        rec = self._make_recording(owner)

        dataset = baker.make(Dataset, author=owner)
        rec_ct = ContentType.objects.get_for_model(
            __import__("recordings.models", fromlist=["Recording"]).Recording,
            for_concrete_model=False,
        )
        DatasetItem.objects.create(dataset=dataset, content_type=rec_ct, object_id=str(rec.pk))
        # No AccessRight on the dataset for user → still denied
        assert not can_read_object(user=user, obj=rec)

    def test_deleted_dataset_breaks_item_access(self, user, make_user):
        from django.utils import timezone

        from epicurrents.permissions import can_read_object
        from recordings.models import Recording

        owner = make_user(username="owner")
        rec = self._make_recording(owner)

        dataset = baker.make(Dataset, author=owner)
        rec_ct = ContentType.objects.get_for_model(Recording, for_concrete_model=False)
        DatasetItem.objects.create(dataset=dataset, content_type=rec_ct, object_id=str(rec.pk))
        self._grant_dataset_read(dataset, giver=owner, target_user=user)

        assert can_read_object(user=user, obj=rec)  # sanity check

        dataset.deleted_at = timezone.now()
        dataset.save()

        assert not can_read_object(user=user, obj=rec)

    def test_item_in_multiple_datasets_sufficient_if_one_grants_access(self, user, make_user):
        from epicurrents.permissions import can_read_object
        from recordings.models import Recording

        owner = make_user(username="owner")
        rec = self._make_recording(owner)
        rec_ct = ContentType.objects.get_for_model(Recording, for_concrete_model=False)

        ds_no_access = baker.make(Dataset, author=owner)
        ds_with_access = baker.make(Dataset, author=owner)

        for ds in (ds_no_access, ds_with_access):
            DatasetItem.objects.create(dataset=ds, content_type=rec_ct, object_id=str(rec.pk))

        self._grant_dataset_read(ds_with_access, giver=owner, target_user=user)

        assert can_read_object(user=user, obj=rec)

    def test_dataset_share_token_grants_item_read(self, make_user):
        from epicurrents.permissions import can_read_object
        from recordings.models import Recording

        owner = make_user(username="owner")
        other = make_user(username="anon")  # authenticated but no group/user right
        rec = self._make_recording(owner)

        dataset = baker.make(Dataset, author=owner)
        rec_ct = ContentType.objects.get_for_model(Recording, for_concrete_model=False)
        DatasetItem.objects.create(dataset=dataset, content_type=rec_ct, object_id=str(rec.pk))

        ds_ct = ContentType.objects.get_for_model(Dataset, for_concrete_model=False)
        AccessRight.objects.create(
            content_type=ds_ct,
            object_id=str(dataset.pk),
            access_giver=owner,
            public_share_token="secret-token",
            can_read=True,
        )

        assert can_read_object(user=other, obj=rec, share_token="secret-token")
        assert not can_read_object(user=other, obj=rec)  # without token → denied


# ---------------------------------------------------------------------------
# Tag model
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestTagStr:
    def test_str_returns_name(self, user):
        tag = baker.make(Tag, author=user, name="EEG")
        assert str(tag) == "EEG"


@pytest.mark.django_db
class TestTagHierarchy:
    def test_nested_tags(self, user):
        root = baker.make(Tag, author=user, name="Root")
        mid = baker.make(Tag, author=user, name="Mid", parent=root)
        leaf = baker.make(Tag, author=user, name="Leaf", parent=mid)

        assert leaf.parent_id == mid.pk
        assert mid.parent_id == root.pk
        assert root.parent_id is None

    def test_children_become_root_on_parent_hard_delete(self, user):
        """SET_NULL: deleting a parent tag promotes its children to root."""
        parent = baker.make(Tag, author=user, name="Parent")
        child = baker.make(Tag, author=user, name="Child", parent=parent)

        parent.delete()
        child.refresh_from_db()
        assert child.parent_id is None

    def test_deleting_tag_removes_tagged_items(self, user):
        """Cascade: deleting a tag removes all its TaggedItem rows."""
        from recordings.models import Recording

        rec = baker.make(Recording, author=user, file_size=1, status=Recording.Status.READY)
        tag = baker.make(Tag, author=user, name="ToDelete")
        ct = ContentType.objects.get_for_model(Recording, for_concrete_model=False)
        TaggedItem.objects.create(tag=tag, content_type=ct, object_id=str(rec.pk))
        tag_id = tag.pk

        assert TaggedItem.objects.filter(tag_id=tag_id).count() == 1
        tag.delete()
        assert TaggedItem.objects.filter(tag_id=tag_id).count() == 0

    def test_unique_constraint_prevents_duplicate_tag_on_item(self, user):
        from django.db import IntegrityError

        from recordings.models import Recording

        rec = baker.make(Recording, author=user, file_size=1, status=Recording.Status.READY)
        tag = baker.make(Tag, author=user, name="Unique")
        ct = ContentType.objects.get_for_model(Recording, for_concrete_model=False)

        TaggedItem.objects.create(tag=tag, content_type=ct, object_id=str(rec.pk))
        with pytest.raises(IntegrityError):
            TaggedItem.objects.create(tag=tag, content_type=ct, object_id=str(rec.pk))
