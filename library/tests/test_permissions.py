"""Unit tests for the library permission layer.

Covers:
- can_read_via_dataset: dataset-membership inheritance with apply_middleware propagation
- The author-only collection gate, and the contract that collection-targeted AccessRight rows grant nothing
"""

import pytest
from django.contrib.contenttypes.models import ContentType
from model_bakery import baker

from epicurrents.models import AccessRight
from epicurrents.permissions import ReadAccessTerms, can_read_object, get_read_access_result
from library.models import Collection, CollectionItem, Dataset, DatasetItem
from library.permissions import can_read_via_dataset


def _dataset_right(dataset, giver, **kwargs):
    """Create an AccessRight on a Dataset."""
    ct = ContentType.objects.get_for_model(Dataset, for_concrete_model=False)
    return AccessRight.objects.create(
        content_type=ct,
        object_id=str(dataset.pk),
        access_giver=giver,
        **kwargs,
    )


def _add_to_dataset(dataset, obj):
    ct = ContentType.objects.get_for_model(obj, for_concrete_model=False)
    return DatasetItem.objects.create(
        dataset=dataset,
        content_type=ct,
        object_id=str(obj.pk),
    )


def _collection_right(collection, giver, **kwargs):
    """Create an AccessRight on a Collection."""
    ct = ContentType.objects.get_for_model(Collection, for_concrete_model=False)
    return AccessRight.objects.create(
        content_type=ct,
        object_id=str(collection.pk),
        access_giver=giver,
        **kwargs,
    )


def _recording_right(recording, giver, **kwargs):
    """Create an AccessRight on a Recording."""
    from recordings.models import Recording

    ct = ContentType.objects.get_for_model(Recording, for_concrete_model=False)
    return AccessRight.objects.create(
        content_type=ct,
        object_id=str(recording.pk),
        access_giver=giver,
        **kwargs,
    )


def _add_to_collection(collection, obj):
    ct = ContentType.objects.get_for_model(obj, for_concrete_model=False)
    return CollectionItem.objects.create(
        collection=collection,
        content_type=ct,
        object_id=str(obj.pk),
    )


@pytest.mark.django_db
class TestCanReadViaDataset:
    def test_no_dataset_membership_returns_false(self, make_user):
        owner = make_user()
        reader = make_user()
        recording = baker.make("recordings.Recording", author=owner)
        result = can_read_via_dataset(reader, recording)
        assert isinstance(result, ReadAccessTerms)
        assert result.granted is False

    def test_no_dataset_right_returns_false(self, make_user):
        owner = make_user()
        reader = make_user()
        dataset = baker.make(Dataset, author=owner)
        recording = baker.make("recordings.Recording", author=owner)
        _add_to_dataset(dataset, recording)
        result = can_read_via_dataset(reader, recording)
        assert result.granted is False

    def test_grants_access_when_dataset_shared(self, make_user):
        owner = make_user()
        reader = make_user()
        dataset = baker.make(Dataset, author=owner)
        recording = baker.make("recordings.Recording", author=owner)
        _add_to_dataset(dataset, recording)
        _dataset_right(dataset, owner, access_target=reader, can_read=True)
        result = can_read_via_dataset(reader, recording)
        assert isinstance(result, ReadAccessTerms)
        assert result.granted is True

    def test_propagates_apply_middleware_false(self, make_user):
        owner = make_user()
        reader = make_user()
        dataset = baker.make(Dataset, author=owner)
        recording = baker.make("recordings.Recording", author=owner)
        _add_to_dataset(dataset, recording)
        _dataset_right(dataset, owner, access_target=reader, can_read=True, apply_middleware=False)
        result = can_read_via_dataset(reader, recording)
        assert result.apply_middleware is False

    def test_propagates_apply_middleware_true(self, make_user):
        owner = make_user()
        reader = make_user()
        dataset = baker.make(Dataset, author=owner)
        recording = baker.make("recordings.Recording", author=owner)
        _add_to_dataset(dataset, recording)
        _dataset_right(dataset, owner, access_target=reader, can_read=True, apply_middleware=True)
        result = can_read_via_dataset(reader, recording)
        assert result.apply_middleware is True

    def test_deleted_dataset_is_excluded(self, make_user):
        from django.utils import timezone

        owner = make_user()
        reader = make_user()
        dataset = baker.make(Dataset, author=owner, deleted_at=timezone.now())
        recording = baker.make("recordings.Recording", author=owner)
        _add_to_dataset(dataset, recording)
        _dataset_right(dataset, owner, access_target=reader, can_read=True)
        assert can_read_via_dataset(reader, recording).granted is False

    def test_expired_access_right_is_excluded(self, make_user):
        from django.utils import timezone

        owner = make_user()
        reader = make_user()
        dataset = baker.make(Dataset, author=owner)
        recording = baker.make("recordings.Recording", author=owner)
        _add_to_dataset(dataset, recording)
        past = timezone.now() - timezone.timedelta(seconds=1)
        _dataset_right(dataset, owner, access_target=reader, can_read=True, expires_at=past)
        assert can_read_via_dataset(reader, recording).granted is False

    def test_group_target_grants_access(self, make_user):
        from django.contrib.auth.models import Group

        owner = make_user()
        reader = make_user()
        group = baker.make(Group)
        reader.groups.add(group)
        dataset = baker.make(Dataset, author=owner)
        recording = baker.make("recordings.Recording", author=owner)
        _add_to_dataset(dataset, recording)
        _dataset_right(dataset, owner, access_target_group=group, can_read=True)
        result = can_read_via_dataset(reader, recording)
        assert result.granted is True

    def test_share_token_grants_access(self, make_user):
        owner = make_user()
        dataset = baker.make(Dataset, author=owner)
        recording = baker.make("recordings.Recording", author=owner)
        _add_to_dataset(dataset, recording)
        _dataset_right(dataset, owner, public_share_token="tok-ds1", can_read=True)
        result = can_read_via_dataset(None, recording, share_token="tok-ds1")
        assert result.granted is True

    def test_can_read_object_grants_via_dataset(self, make_user):
        owner = make_user()
        reader = make_user()
        dataset = baker.make(Dataset, author=owner)
        recording = baker.make("recordings.Recording", author=owner)
        _add_to_dataset(dataset, recording)
        _dataset_right(dataset, owner, access_target=reader, can_read=True)
        assert can_read_object(reader, recording) is True

    def test_direct_access_right_takes_precedence_over_dataset(self, make_user):
        """A direct AccessRight on the recording is used before the extension."""
        owner = make_user()
        reader = make_user()
        dataset = baker.make(Dataset, author=owner)
        recording = baker.make("recordings.Recording", author=owner)
        _add_to_dataset(dataset, recording)
        # Direct right: apply_middleware=False
        _recording_right(
            recording,
            owner,
            access_target=reader,
            can_read=True,
            apply_middleware=False,
        )
        # Dataset share: apply_middleware=True (would override if extension ran)
        _dataset_right(dataset, owner, access_target=reader, can_read=True, apply_middleware=True)
        result = get_read_access_result(reader, recording)
        assert result.granted is True
        assert result.apply_middleware is False


@pytest.mark.django_db
class TestCollectionRowsGrantNothing:
    """A collection-targeted AccessRight must grant nothing.

    No API path creates these rows and library/0004 purges pre-existing ones, but a stale or
    hand-inserted row must still be inert — the permission layer consults no collection-targeted
    rows, and the assertion here is the contract that keeps collections author-private.
    """

    def test_collection_right_grants_no_item_access(self, make_user):
        owner = make_user()
        reader = make_user()
        collection = baker.make(Collection, author=owner)
        recording = baker.make("recordings.Recording", author=owner)
        _add_to_collection(collection, recording)
        _collection_right(collection, owner, access_target=reader, can_read=True)
        assert can_read_object(reader, recording) is False

    def test_collection_right_grants_no_collection_access(self, make_user):
        from library.permissions import can_read_collection

        owner = make_user()
        reader = make_user()
        collection = baker.make(Collection, author=owner)
        _collection_right(collection, owner, access_target=reader, can_read=True)
        assert can_read_collection(reader, collection) is False

    def test_collection_share_token_grants_nothing(self, make_user):
        owner = make_user()
        collection = baker.make(Collection, author=owner)
        recording = baker.make("recordings.Recording", author=owner)
        _add_to_collection(collection, recording)
        _collection_right(collection, owner, public_share_token="tok-abc", can_read=True)
        assert can_read_object(None, recording, share_token="tok-abc") is False

    def test_author_reads_own_collection_without_a_row(self, make_user):
        from library.permissions import can_read_collection, can_write_collection

        owner = make_user()
        collection = baker.make(Collection, author=owner)
        assert can_read_collection(owner, collection) is True
        assert can_write_collection(owner, collection) is True

    def test_superuser_reads_any_collection(self, make_user, make_superuser):
        from library.permissions import can_read_collection

        owner = make_user()
        admin = make_superuser()
        collection = baker.make(Collection, author=owner)
        assert can_read_collection(admin, collection) is True

    def test_non_author_cannot_read_or_write(self, make_user):
        from library.permissions import can_read_collection, can_write_collection

        owner = make_user()
        other = make_user()
        collection = baker.make(Collection, author=owner)
        assert can_read_collection(other, collection) is False
        assert can_write_collection(other, collection) is False
