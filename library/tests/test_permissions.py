"""Unit tests for the library permission layer.

Covers:
- can_read_via_dataset: dataset-membership inheritance with apply_middleware propagation
- The same rule for a federated peer, per object and as a listing
- The author-only collection gate, and the contract that collection-targeted AccessRight rows grant nothing
"""

import pytest
from django.contrib.contenttypes.models import ContentType
from model_bakery import baker

from epicurrents.models import AccessRight
from epicurrents.permissions import ReadAccessTerms, can_read_object, get_read_access_result
from federation.models import FederatedPeer
from library.models import Collection, CollectionItem, Dataset, DatasetItem
from library.permissions import (
    can_read_via_dataset,
    can_read_via_dataset_federated,
    federated_dataset_visible_terms,
)


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


@pytest.fixture
def peer(db):
    return FederatedPeer.objects.create(url="https://peer.example.com", public_key="k" * 43, is_trusted=True)


class TestDatasetGrantsReachAFederatedPeer:
    """A dataset shared with a peer has to reach that peer's users.

    It did not: the federated resolver read direct rows only, so a dataset grant
    was accepted, reported as created, and conveyed nothing — the peer's listing
    was empty and a per-object request 404'd. Datasets are the platform's only
    sharing unit, so that was the whole sharing surface for federation.
    """

    def _shared_dataset(self, user, recording, peer, **right_kwargs):
        dataset = baker.make(Dataset, author=user)
        DatasetItem.objects.create(
            dataset=dataset,
            content_type=ContentType.objects.get_for_model(recording),
            object_id=str(recording.pk),
        )
        _dataset_right(dataset, user, federated_peer=peer, can_read=True, **right_kwargs)
        return dataset

    def test_peer_reaches_an_item_of_a_shared_dataset(self, user, peer):
        recording = baker.make("recordings.Recording", author=user)
        self._shared_dataset(user, recording, peer)
        terms = can_read_via_dataset_federated(peer, "", recording)
        assert terms is not None and terms.granted

    @pytest.mark.parametrize("creation_order", [(False, True), (True, False)])
    def test_de_identifying_share_wins_when_an_item_is_in_two(self, user, peer, creation_order):
        # The listing half already resolves this overlap toward de-identification.
        # This half decides what the byte-serving endpoints actually send, so the
        # two disagreeing means a peer is told the recording is de-identified and
        # handed the raw file. Both creation orders, because the defect this
        # covers was the absence of any tie-break at all: with none, the answer is
        # whichever row the database returns first, and one order passes by luck.
        recording = baker.make("recordings.Recording", author=user)
        for apply_middleware in creation_order:
            self._shared_dataset(user, recording, peer, apply_middleware=apply_middleware)
        terms = can_read_via_dataset_federated(peer, "", recording)
        assert terms is not None and terms.apply_middleware is True

    def test_an_exact_user_grant_still_outranks_a_wildcard(self, user, peer):
        # The de-identifying preference is a tie-break among equals, not an
        # override: a grant naming this user is the sharer deciding about them
        # specifically, and it wins whichever way it is set. Pinned because the
        # obvious fix for the case above — sort by apply_middleware first — would
        # silently reverse this precedence and pass every other test here.
        recording = baker.make("recordings.Recording", author=user)
        dataset = self._shared_dataset(user, recording, peer, apply_middleware=True)
        _dataset_right(
            dataset, user, federated_peer=peer, can_read=True, remote_user_id="u1", apply_middleware=False
        )
        terms = can_read_via_dataset_federated(peer, "u1", recording)
        assert terms is not None and terms.apply_middleware is False

    def test_de_identification_survives_the_inheritance(self, user, peer):
        # The sharer's choice lives on the dataset row. Losing it here serves raw
        # EDF — patient identification and clinical annotation text — to a peer
        # that was granted the de-identified form.
        recording = baker.make("recordings.Recording", author=user)
        self._shared_dataset(user, recording, peer, apply_middleware=True)
        assert can_read_via_dataset_federated(peer, "", recording).apply_middleware is True

    def test_a_wildcard_grant_covers_any_remote_user(self, user, peer):
        recording = baker.make("recordings.Recording", author=user)
        self._shared_dataset(user, recording, peer)
        assert can_read_via_dataset_federated(peer, "42", recording).granted

    def test_a_user_scoped_grant_covers_only_that_user(self, user, peer):
        recording = baker.make("recordings.Recording", author=user)
        self._shared_dataset(user, recording, peer, remote_user_id="42")
        assert can_read_via_dataset_federated(peer, "42", recording).granted
        assert can_read_via_dataset_federated(peer, "43", recording) is None

    def test_another_peer_reaches_nothing(self, user, peer):
        recording = baker.make("recordings.Recording", author=user)
        self._shared_dataset(user, recording, peer)
        other = FederatedPeer.objects.create(url="https://other.example.com", public_key="j" * 43)
        assert can_read_via_dataset_federated(other, "", recording) is None

    def test_an_unshared_dataset_reaches_nothing(self, user, peer):
        recording = baker.make("recordings.Recording", author=user)
        dataset = baker.make(Dataset, author=user)
        DatasetItem.objects.create(
            dataset=dataset,
            content_type=ContentType.objects.get_for_model(recording),
            object_id=str(recording.pk),
        )
        assert can_read_via_dataset_federated(peer, "", recording) is None

    def test_a_trashed_dataset_reaches_nothing(self, user, peer):
        from django.utils import timezone

        recording = baker.make("recordings.Recording", author=user)
        dataset = self._shared_dataset(user, recording, peer)
        dataset.deleted_at = timezone.now()
        dataset.save(update_fields=["deleted_at"])
        assert can_read_via_dataset_federated(peer, "", recording) is None


class TestFederatedDatasetListing:
    """The listing half has to answer the same question as the per-object half.

    A listing that disagrees is its own defect: it either advertises objects that
    then 404, or hides objects the peer can fetch by hash.
    """

    def _shared(self, user, peer, count=2):
        dataset = baker.make(Dataset, author=user)
        recordings = [baker.make("recordings.Recording", author=user) for _ in range(count)]
        ct = ContentType.objects.get_for_model(recordings[0])
        for recording in recordings:
            DatasetItem.objects.create(dataset=dataset, content_type=ct, object_id=str(recording.pk))
        _dataset_right(dataset, user, federated_peer=peer, can_read=True)
        return recordings, ct

    def test_lists_every_item_of_a_shared_dataset(self, user, peer):
        recordings, ct = self._shared(user, peer)
        assert set(federated_dataset_visible_terms(peer, "", ct)) == {str(r.pk) for r in recordings}

    def test_agrees_with_the_per_object_check(self, user, peer):
        recordings, ct = self._shared(user, peer)
        listed = federated_dataset_visible_terms(peer, "", ct)
        for recording in recordings:
            assert (str(recording.pk) in listed) is bool(can_read_via_dataset_federated(peer, "", recording))

    def test_carries_the_de_identification_choice(self, user, peer):
        # The federated listing advertises a download size that depends on this
        # flag, so a listing that reports the terms wrongly misstates the size.
        dataset = baker.make(Dataset, author=user)
        recording = baker.make("recordings.Recording", author=user)
        ct = ContentType.objects.get_for_model(recording)
        DatasetItem.objects.create(dataset=dataset, content_type=ct, object_id=str(recording.pk))
        _dataset_right(dataset, user, federated_peer=peer, can_read=True, apply_middleware=True)
        assert federated_dataset_visible_terms(peer, "", ct)[str(recording.pk)].apply_middleware is True

    def test_de_identifying_share_wins_when_an_item_is_in_two(self, user, peer):
        # Among equals the resolver prefers the de-identifying row; the safe
        # direction, since the reverse serves PHI.
        recording = baker.make("recordings.Recording", author=user)
        ct = ContentType.objects.get_for_model(recording)
        for apply_middleware in (False, True):
            dataset = baker.make(Dataset, author=user)
            DatasetItem.objects.create(dataset=dataset, content_type=ct, object_id=str(recording.pk))
            _dataset_right(dataset, user, federated_peer=peer, can_read=True, apply_middleware=apply_middleware)
        assert federated_dataset_visible_terms(peer, "", ct)[str(recording.pk)].apply_middleware is True

    def test_reports_the_terms_the_per_object_check_will_apply(self, user, peer):
        # Agreement on presence is not enough. The listing advertises a download
        # size computed from apply_middleware, and the per-object check decides
        # what the bytes are, so a disagreement here is a peer told one thing and
        # served another. Both overlap shapes: two datasets at the same
        # specificity, and one dataset where an exact-user row overrides the
        # wildcard.
        recording = baker.make("recordings.Recording", author=user)
        ct = ContentType.objects.get_for_model(recording)
        first = baker.make(Dataset, author=user)
        second = baker.make(Dataset, author=user)
        for dataset in (first, second):
            DatasetItem.objects.create(dataset=dataset, content_type=ct, object_id=str(recording.pk))
        _dataset_right(first, user, federated_peer=peer, can_read=True, apply_middleware=False)
        _dataset_right(second, user, federated_peer=peer, can_read=True, apply_middleware=True)
        _dataset_right(
            first, user, federated_peer=peer, can_read=True, remote_user_id="u1", apply_middleware=False
        )
        for remote_user_id in ("", "u1"):
            listed = federated_dataset_visible_terms(peer, remote_user_id, ct)[str(recording.pk)]
            per_object = can_read_via_dataset_federated(peer, remote_user_id, recording)
            assert per_object is not None
            assert listed.apply_middleware is per_object.apply_middleware

    def test_an_exact_user_grant_outranks_the_wildcard_on_the_same_dataset(self, user, peer):
        # Same precedence the per-object half applies, asserted on the listing so
        # the two cannot drift: an exact row is the sharer deciding about this
        # user, and folding it together with the wildcard loses that decision.
        recording = baker.make("recordings.Recording", author=user)
        ct = ContentType.objects.get_for_model(recording)
        dataset = baker.make(Dataset, author=user)
        DatasetItem.objects.create(dataset=dataset, content_type=ct, object_id=str(recording.pk))
        _dataset_right(dataset, user, federated_peer=peer, can_read=True, apply_middleware=True)
        _dataset_right(
            dataset, user, federated_peer=peer, can_read=True, remote_user_id="u1", apply_middleware=False
        )
        assert federated_dataset_visible_terms(peer, "u1", ct)[str(recording.pk)].apply_middleware is False
        assert federated_dataset_visible_terms(peer, "", ct)[str(recording.pk)].apply_middleware is True

    def test_lists_nothing_for_another_peer(self, user, peer):
        _, ct = self._shared(user, peer)
        other = FederatedPeer.objects.create(url="https://other2.example.com", public_key="h" * 43)
        assert federated_dataset_visible_terms(other, "", ct) == {}

    def test_lists_nothing_without_a_peer(self, user, peer):
        _, ct = self._shared(user, peer)
        assert federated_dataset_visible_terms(None, "", ct) == {}
