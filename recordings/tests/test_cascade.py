"""Cascade tests — hard-deleting a Recording removes all rows that target it via GenericForeignKey.

These guard against the orphan-on-hard-delete problem caused by Django's
ORM not enforcing referential integrity across GenericFKs. The fix is to
declare reverse ``GenericRelation`` fields on the target model; these tests
confirm the cascade actually fires for every reference row type.

Every test captures ``str(recording.pk)`` before the delete —
``Model.delete()`` sets ``pk`` to ``None``, so a post-delete filter built from
``recording.pk`` matches nothing and passes vacuously whether or not the
cascade ran.

Audit-trail rows (``ObjectChangeLog``, ``FederationAuditLog``) intentionally
do **not** cascade — they must outlive their targets — so they are not
covered here.
"""

import pytest
from django.contrib.contenttypes.models import ContentType
from model_bakery import baker

from annotations.models import Annotation, Event, Interruption, Label
from epicurrents.models import AccessRight
from library.models import Collection, CollectionItem, Dataset, DatasetItem, Tag, TaggedItem
from recordings.models import Recording


@pytest.mark.django_db
class TestRecordingHardDeleteCascade:
    """Hard-deleting a Recording must remove every row that targets it."""

    def _annotation_kwargs(self, user, recording):
        ct = ContentType.objects.get_for_model(recording, for_concrete_model=False)
        return {
            "author": user,
            "target_content_type": ct,
            "target_object_id": str(recording.pk),
            "object_hash": "A" * 32,
        }

    def test_annotation_cascade(self, user):
        recording = baker.make(Recording, author=user)
        object_id = str(recording.pk)
        Annotation(name="note", content={}, **self._annotation_kwargs(user, recording)).save()
        assert Annotation.objects.filter(target_object_id=object_id).exists()

        recording.delete()

        assert not Annotation.objects.filter(target_object_id=object_id).exists()

    def test_event_cascade(self, user):
        recording = baker.make(Recording, author=user)
        object_id = str(recording.pk)
        Event(name="spike", timestamp=1.0, **self._annotation_kwargs(user, recording)).save()
        assert Event.objects.filter(target_object_id=object_id).exists()

        recording.delete()

        assert not Event.objects.filter(target_object_id=object_id).exists()

    def test_interruption_cascade(self, user):
        recording = baker.make(Recording, author=user)
        object_id = str(recording.pk)
        Interruption(timestamp=2.0, duration=1.0, **self._annotation_kwargs(user, recording)).save()
        assert Interruption.objects.filter(target_object_id=object_id).exists()

        recording.delete()

        assert not Interruption.objects.filter(target_object_id=object_id).exists()

    def test_label_cascade(self, user):
        recording = baker.make(Recording, author=user)
        object_id = str(recording.pk)
        Label(name="quality", **self._annotation_kwargs(user, recording)).save()
        assert Label.objects.filter(target_object_id=object_id).exists()

        recording.delete()

        assert not Label.objects.filter(target_object_id=object_id).exists()

    def test_access_right_cascade(self, user):
        recording = baker.make(Recording, author=user)
        object_id = str(recording.pk)
        ct = ContentType.objects.get_for_model(recording, for_concrete_model=False)
        AccessRight.objects.create(
            content_type=ct,
            object_id=object_id,
            access_giver=user,
            access_target=user,
            can_read=True,
        )
        assert AccessRight.objects.filter(content_type=ct, object_id=object_id).exists()

        recording.delete()

        assert not AccessRight.objects.filter(content_type=ct, object_id=object_id).exists()

    def test_collection_item_cascade(self, user):
        recording = baker.make(Recording, author=user)
        object_id = str(recording.pk)
        collection = baker.make(Collection, author=user)
        ct = ContentType.objects.get_for_model(recording, for_concrete_model=False)
        CollectionItem.objects.create(collection=collection, content_type=ct, object_id=object_id)
        assert CollectionItem.objects.filter(content_type=ct, object_id=object_id).exists()

        recording.delete()

        assert not CollectionItem.objects.filter(content_type=ct, object_id=object_id).exists()
        # Collection itself must NOT be cascaded — only the membership row.
        assert Collection.objects.filter(pk=collection.pk).exists()

    def test_dataset_item_cascade(self, user):
        recording = baker.make(Recording, author=user)
        object_id = str(recording.pk)
        dataset = baker.make(Dataset, author=user)
        ct = ContentType.objects.get_for_model(recording, for_concrete_model=False)
        DatasetItem.objects.create(dataset=dataset, content_type=ct, object_id=object_id)
        assert DatasetItem.objects.filter(content_type=ct, object_id=object_id).exists()

        recording.delete()

        assert not DatasetItem.objects.filter(content_type=ct, object_id=object_id).exists()
        assert Dataset.objects.filter(pk=dataset.pk).exists()

    def test_tagged_item_cascade(self, user):
        recording = baker.make(Recording, author=user)
        object_id = str(recording.pk)
        tag = baker.make(Tag, author=user)
        ct = ContentType.objects.get_for_model(recording, for_concrete_model=False)
        TaggedItem.objects.create(tag=tag, content_type=ct, object_id=object_id)
        assert TaggedItem.objects.filter(content_type=ct, object_id=object_id).exists()

        recording.delete()

        assert not TaggedItem.objects.filter(content_type=ct, object_id=object_id).exists()
        assert Tag.objects.filter(pk=tag.pk).exists()

    def test_recording_meta_cascade(self, user):
        from django.contrib.contenttypes.models import ContentType

        from recordings.models import RecordingMeta

        recording = baker.make(Recording, author=user)
        object_id = str(recording.pk)
        RecordingMeta.objects.create(
            content_type=ContentType.objects.get_for_model(Recording),
            object_id=object_id,
            format="edf",
            duration=10.0,
            data_record_count=10,
            data_record_duration=1.0,
            signal_count=2,
        )
        assert RecordingMeta.objects.filter(object_id=object_id).exists()

        recording.delete()

        assert not RecordingMeta.objects.filter(object_id=object_id).exists()

    def test_soft_delete_does_not_cascade(self, user):
        """Soft-delete (setting ``deleted_at``) keeps the row alive so reverse-GenericRelations should not fire."""
        recording = baker.make(Recording, author=user)
        object_id = str(recording.pk)
        ct = ContentType.objects.get_for_model(recording, for_concrete_model=False)
        AccessRight.objects.create(
            content_type=ct,
            object_id=object_id,
            access_giver=user,
            access_target=user,
            can_read=True,
        )
        Event(name="spike", timestamp=1.0, **self._annotation_kwargs(user, recording)).save()

        from django.utils import timezone

        recording.deleted_at = timezone.now()
        recording.save()

        # Both the soft-deleted recording and all attached rows must still exist.
        assert Recording.objects.filter(pk=recording.pk).exists()
        assert AccessRight.objects.filter(content_type=ct, object_id=object_id).exists()
        assert Event.objects.filter(target_object_id=object_id).exists()
