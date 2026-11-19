"""Paged item listings: a page is short only at the end, whatever the listing leaves out.

A client reads a collection's or a dataset's items a page at a time until a short page, sending the rows it holds as
the next offset. Trashed and FAILED recordings and trashed media are left out before the page is sliced, so one of
them among the first page neither ends the read early nor shifts the next page onto rows already read.
"""

from __future__ import annotations

import pytest
from django.contrib.contenttypes.models import ContentType
from django.utils import timezone
from model_bakery import baker

from library.models import Collection, CollectionItem, Dataset, DatasetItem
from library.tests.test_browse_facets import _client, _recording
from media.models import MediaFile
from recordings.models import Recording

pytestmark = pytest.mark.django_db

PAGE = 5


def _recording_ct():
    return ContentType.objects.get_for_model(Recording, for_concrete_model=False)


def _members(author):
    """Eight live recordings, a trashed and a FAILED one among them early, and a trashed media file."""
    recordings = [_recording(author, index) for index in range(10)]
    Recording.objects.filter(pk=recordings[1].pk).update(deleted_at=timezone.now())
    Recording.objects.filter(pk=recordings[3].pk).update(status=Recording.Status.FAILED)
    media = baker.make(MediaFile, author=author, deleted_at=timezone.now())
    live = [rec for index, rec in enumerate(recordings) if index not in (1, 3)]
    return recordings, media, live


def _read_all(client, url):
    """Every row of a listing, read as the SPA's ``readAllPages`` reads it."""
    rows = []
    while True:
        response = client.get(url, {"limit": PAGE, "offset": len(rows)})
        assert response.status_code == 200, response.content
        page = response.json()
        rows.extend(page)
        if len(page) < PAGE:
            return rows


def _hashes(rows):
    return [row["object_hash"] for row in rows]


def _hash(recording):
    return recording.stored_name.split(".")[0]


class TestCollectionItems:
    @pytest.fixture
    def collection(self, user):
        recordings, media, live = _members(user)
        collection = baker.make(Collection, author=user)
        for recording in recordings:
            CollectionItem.objects.create(
                collection=collection, content_type=_recording_ct(), object_id=str(recording.pk)
            )
        CollectionItem.objects.create(
            collection=collection,
            content_type=ContentType.objects.get_for_model(MediaFile, for_concrete_model=False),
            object_id=str(media.pk),
        )
        return collection, live

    def test_the_first_page_is_full(self, user, collection):
        collection, _live = collection
        page = _client(user).get(f"/api/v1/library/collections/{collection.pk}/items/", {"limit": PAGE})
        assert len(page.json()) == PAGE

    @pytest.mark.parametrize("as_superuser", [False, True])
    def test_reading_to_a_short_page_returns_every_live_item_once(self, user, make_superuser, collection, as_superuser):
        collection, live = collection
        client = _client(make_superuser() if as_superuser else user)
        rows = _read_all(client, f"/api/v1/library/collections/{collection.pk}/items/")
        assert sorted(_hashes(rows)) == sorted(_hash(rec) for rec in live)


class TestDatasetItems:
    def test_reading_to_a_short_page_returns_every_live_member_once(self, user):
        recordings, _media, live = _members(user)
        dataset = Dataset.objects.create(author=user, name="paged")
        for recording in recordings:
            DatasetItem.objects.create(dataset=dataset, content_type=_recording_ct(), object_id=str(recording.pk))
        client = _client(user)
        first = client.get(f"/api/v1/library/datasets/{dataset.object_hash}/items/", {"limit": PAGE}).json()
        assert len(first) == PAGE
        rows = _read_all(client, f"/api/v1/library/datasets/{dataset.object_hash}/items/")
        assert sorted(_hashes(rows)) == sorted(_hash(rec) for rec in live)
