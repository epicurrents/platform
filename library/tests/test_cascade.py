"""Cascade tests — hard-deleting a Collection or Dataset removes all rows that target it via GenericFK.

Companion to ``recordings/tests/test_cascade.py``. The Collection / Dataset
targets here are themselves containers, so the cascade scope is narrower:
- ``AccessRight`` rows pointing at a Dataset (epicurrents app). Collections
  declare no AccessRight relation — they are author-private and no row may
  target them, so there is nothing to cascade (``TestCollectionShareRemoval``
  in test_api.py pins the inertness of a hand-inserted row).
- ``TaggedItem`` rows pointing at the target (library app).
- For Datasets: ``CollectionItem`` rows pointing at the Dataset (a Dataset
  can be a member of a Collection).

Every test captures ``str(obj.pk)`` before the delete — ``Model.delete()``
sets ``pk`` to ``None``, so a post-delete filter built from ``obj.pk`` matches
nothing and passes vacuously whether or not the cascade ran.

Audit-trail rows are intentionally not covered (they must outlive their
targets).
"""

import pytest
from django.contrib.contenttypes.models import ContentType
from model_bakery import baker

from annotations.models import Annotation, Event, Interruption, Label
from epicurrents.models import AccessRight
from library.models import Collection, CollectionItem, Dataset, Tag, TaggedItem


def _annotation_kwargs(user, target):
    ct = ContentType.objects.get_for_model(target, for_concrete_model=False)
    return {
        "author": user,
        "target_content_type": ct,
        "target_object_id": str(target.pk),
        "object_hash": "A" * 32,
    }


@pytest.mark.django_db
class TestCollectionHardDeleteCascade:
    def test_tagged_item_cascade(self, user):
        collection = baker.make(Collection, author=user)
        tag = baker.make(Tag, author=user)
        ct = ContentType.objects.get_for_model(Collection)
        object_id = str(collection.pk)
        TaggedItem.objects.create(tag=tag, content_type=ct, object_id=object_id)
        assert TaggedItem.objects.filter(content_type=ct, object_id=object_id).exists()

        collection.delete()

        assert not TaggedItem.objects.filter(content_type=ct, object_id=object_id).exists()
        assert Tag.objects.filter(pk=tag.pk).exists()

    def test_annotation_cascade(self, user):
        """Collections aren't typically annotated by the UI but the API allows it."""
        collection = baker.make(Collection, author=user)
        object_id = str(collection.pk)
        Annotation(name="note", content={}, **_annotation_kwargs(user, collection)).save()
        Event(name="spike", timestamp=1.0, **_annotation_kwargs(user, collection)).save()
        Interruption(timestamp=2.0, duration=1.0, **_annotation_kwargs(user, collection)).save()
        Label(name="quality", **_annotation_kwargs(user, collection)).save()

        collection.delete()

        assert not Annotation.objects.filter(target_object_id=object_id).exists()
        assert not Event.objects.filter(target_object_id=object_id).exists()
        assert not Interruption.objects.filter(target_object_id=object_id).exists()
        assert not Label.objects.filter(target_object_id=object_id).exists()


@pytest.mark.django_db
class TestDatasetHardDeleteCascade:
    def test_access_right_cascade(self, user):
        dataset = baker.make(Dataset, author=user)
        ct = ContentType.objects.get_for_model(Dataset)
        object_id = str(dataset.pk)
        AccessRight.objects.create(
            content_type=ct,
            object_id=object_id,
            access_giver=user,
            access_target=user,
            can_read=True,
        )
        assert AccessRight.objects.filter(content_type=ct, object_id=object_id).exists()

        dataset.delete()

        assert not AccessRight.objects.filter(content_type=ct, object_id=object_id).exists()

    def test_collection_item_cascade(self, user):
        """A Dataset that appears as a CollectionItem must drop the membership row when hard-deleted."""
        dataset = baker.make(Dataset, author=user)
        collection = baker.make(Collection, author=user)
        ct = ContentType.objects.get_for_model(Dataset)
        object_id = str(dataset.pk)
        CollectionItem.objects.create(collection=collection, content_type=ct, object_id=object_id)
        assert CollectionItem.objects.filter(content_type=ct, object_id=object_id).exists()

        dataset.delete()

        assert not CollectionItem.objects.filter(content_type=ct, object_id=object_id).exists()
        assert Collection.objects.filter(pk=collection.pk).exists()

    def test_tagged_item_cascade(self, user):
        dataset = baker.make(Dataset, author=user)
        tag = baker.make(Tag, author=user)
        ct = ContentType.objects.get_for_model(Dataset)
        object_id = str(dataset.pk)
        TaggedItem.objects.create(tag=tag, content_type=ct, object_id=object_id)
        assert TaggedItem.objects.filter(content_type=ct, object_id=object_id).exists()

        dataset.delete()

        assert not TaggedItem.objects.filter(content_type=ct, object_id=object_id).exists()
        assert Tag.objects.filter(pk=tag.pk).exists()

    def test_annotation_cascade(self, user):
        """Datasets aren't typically annotated by the UI but the API allows it."""
        dataset = baker.make(Dataset, author=user)
        object_id = str(dataset.pk)
        Annotation(name="note", content={}, **_annotation_kwargs(user, dataset)).save()
        Event(name="spike", timestamp=1.0, **_annotation_kwargs(user, dataset)).save()
        Interruption(timestamp=2.0, duration=1.0, **_annotation_kwargs(user, dataset)).save()
        Label(name="quality", **_annotation_kwargs(user, dataset)).save()

        dataset.delete()

        assert not Annotation.objects.filter(target_object_id=object_id).exists()
        assert not Event.objects.filter(target_object_id=object_id).exists()
        assert not Interruption.objects.filter(target_object_id=object_id).exists()
        assert not Label.objects.filter(target_object_id=object_id).exists()
