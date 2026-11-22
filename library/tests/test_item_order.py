"""The default order of an item listing: by the name each row shows, never by addition.

A dataset's members and a tag's items are listed by name, gated or not, with ties broken by the member's public hash.
``added_at`` and the item id are both orders of arrival, so neither is an ordering term. The gated half of the
contract is in ``test_release``; the cases here are the ungated dataset and the tag listing, which share its
ordering through ``library.release.order_by_member_name``.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.contrib.contenttypes.models import ContentType
from django.test import Client
from django.utils import timezone

from library.models import Dataset, DatasetItem, Tag, TaggedItem
from library.tests.test_release import HASHES, _add, _grant, _recording
from recordings.models import Recording

pytestmark = pytest.mark.django_db

DATASETS = "/api/v1/library/datasets"
TAGS = "/api/v1/library/tags"


def _client(user) -> Client:
    client = Client()
    client.force_login(user)
    return client


def _names(rows) -> list:
    return [row["object_name"] for row in rows]


def _hashes(rows) -> list:
    return [row["object_hash"] for row in rows]


@pytest.fixture
def named(make_user):
    """An ungated dataset whose members were added in the reverse of their name order, and a reader of it."""
    author, reader = make_user(), make_user()
    dataset = Dataset.objects.create(author=author, name="ungated")
    _grant(dataset, author, target=reader)
    labels = ["Zeta", "middle", "delta", "Bravo", "alpha"]
    for index, label in enumerate(labels):
        item = _add(dataset, _recording(author, index=index, display_name=label))
        # Each added a day after the one before, so the order of addition is unmistakably the reverse.
        DatasetItem.objects.filter(pk=item.pk).update(added_at=timezone.now() + timedelta(days=index))
    return dataset, author, reader


class TestUngatedDatasetListing:
    def test_members_are_listed_by_name_whatever_order_they_were_added_in(self, named):
        dataset, author, reader = named
        for caller in (author, reader):
            rows = _client(caller).get(f"{DATASETS}/{dataset.object_hash}/items/").json()
            assert _names(rows) == ["alpha", "Bravo", "delta", "middle", "Zeta"]

    def test_the_order_holds_across_pages(self, named):
        dataset, _author, reader = named
        client = _client(reader)
        url = f"{DATASETS}/{dataset.object_hash}/items/"
        paged = []
        for offset in (0, 2, 4):
            paged += client.get(url, {"limit": 2, "offset": offset}).json()
        assert _names(paged) == ["alpha", "Bravo", "delta", "middle", "Zeta"]
        assert _hashes(paged) == _hashes(client.get(url).json())

    def test_a_tie_between_names_is_broken_by_the_hash_not_by_addition(self, make_user):
        author = make_user()
        dataset = Dataset.objects.create(author=author, name="ties")
        # Added highest hash first, so both the item id and added_at run against the hash order.
        for index in (3, 2, 1, 0):
            _add(dataset, _recording(author, index=index, display_name="Same"))
        client = _client(author)
        url = f"{DATASETS}/{dataset.object_hash}/items/"
        assert _hashes(client.get(url).json()) == HASHES[:4]
        paged = client.get(url, {"limit": 2}).json() + client.get(url, {"limit": 2, "offset": 2}).json()
        assert _hashes(paged) == HASHES[:4]

    def test_an_unnamed_recording_sorts_by_the_name_its_row_shows_the_caller(self, make_user):
        author, reader = make_user(), make_user()
        dataset = Dataset.objects.create(author=author, name="unnamed")
        _grant(dataset, author, target=reader)
        # The author is shown the original filename, a reader the handle cut from the stored name.
        _add(dataset, _recording(author, index=0, original_name="zulu.edf"))
        _add(dataset, _recording(author, index=1, original_name="alpha.edf"))
        url = f"{DATASETS}/{dataset.object_hash}/items/"
        assert _names(_client(author).get(url).json()) == ["alpha.edf", "zulu.edf"]
        assert _hashes(_client(reader).get(url).json()) == [HASHES[0], HASHES[1]]

    def test_a_reader_is_not_ordered_by_an_original_name_they_are_not_shown(self, make_user):
        author, reader = make_user(), make_user()
        dataset = Dataset.objects.create(author=author, name="private names")
        _grant(dataset, author, target=reader)
        _add(dataset, _recording(author, index=0, original_name="zulu.edf"))
        _add(dataset, _recording(author, index=1, display_name="mmm", original_name="alpha.edf"))
        rows = _client(reader).get(f"{DATASETS}/{dataset.object_hash}/items/").json()
        # "0000…1" before "mmm": the stored name, where the original filename would put it last.
        assert _hashes(rows) == [HASHES[0], HASHES[1]]


class TestTagItemListing:
    def test_tagged_objects_are_listed_by_name_then_hash(self, make_user):
        author = make_user()
        tag = Tag.objects.create(author=author, name="spikes", curated=True)
        recording_ct = ContentType.objects.get_for_model(Recording, for_concrete_model=False)
        labels = ["Zeta", "Same", "Same", "alpha"]
        for index in (3, 2, 1, 0):
            recording = _recording(author, index=index, display_name=labels[index])
            _grant(recording, author, target=author)
            TaggedItem.objects.create(tag=tag, content_type=recording_ct, object_id=str(recording.pk))
        client = _client(author)
        expected = [HASHES[3], HASHES[1], HASHES[2], HASHES[0]]
        assert _hashes(client.get(f"{TAGS}/{tag.pk}/items/").json()) == expected
        paged = []
        for offset in (0, 2):
            paged += client.get(f"{TAGS}/{tag.pk}/items/", {"limit": 2, "offset": offset}).json()
        assert _hashes(paged) == expected
