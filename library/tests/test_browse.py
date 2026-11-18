"""The dataset browser endpoint, ``GET /datasets/{id}/browse/``.

Covers the columns and how they are derived, the sort allowlist and that no key orders by arrival, the
membership the item listing shares (gated, trashed, FAILED and media members), the date each caller is
served, access, paging and the audit row. The per-reader key set is pinned in
``recordings/tests/test_grantee_visible_shape.py`` with the other grantee-visible shapes.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from django.contrib.contenttypes.models import ContentType
from django.test import Client
from django.utils import timezone
from model_bakery import baker

from activity.models import Activity
from library.models import Dataset
from library.tests.test_release import _add, _gated_dataset, _grant, _recording, _release
from recordings.models import Recording, RecordingMeta, SignalInfo

pytestmark = pytest.mark.django_db

DATASETS = "/api/v1/library/datasets"
TOKEN = "browse-share-token"


def _client(user=None) -> Client:
    client = Client()
    if user is not None:
        client.force_login(user)
    return client


def _meta(recording, *, duration=10.0, rates=(256.0,), annotation=True, layout="referential", version=5):
    """Metadata rows for *recording*: one signal channel per entry of *rates*, plus an annotation channel."""
    meta = baker.make(
        RecordingMeta,
        content_type=ContentType.objects.get_for_model(Recording, for_concrete_model=False),
        object_id=str(recording.pk),
        duration=duration,
        signal_count=len(rates) + int(annotation),
        channel_layout=layout,
        deidentification_version=version,
    )
    for index, rate in enumerate(rates):
        baker.make(SignalInfo, meta=meta, index=index, sampling_rate=rate, is_annotation_channel=False)
    if annotation:
        baker.make(SignalInfo, meta=meta, index=len(rates), sampling_rate=0.0, is_annotation_channel=True)
    return meta


def _browse(client, dataset, query=""):
    return client.get(f"{DATASETS}/{dataset.object_hash}/browse/{query}")


def _hashes(response):
    assert response.status_code == 200, response.content
    return [row["hash"] for row in response.json()["items"]]


def _hash(recording):
    return recording.stored_name.split(".")[0]


class TestColumns:
    def test_ingest_metadata_per_row(self, user):
        recording = _recording(user, modality="eeg", public_source="https://example.org/ds")
        _meta(recording, duration=12.5, rates=(500.0, 256.0, 500.0), layout="bipolar", version=6)
        dataset = Dataset.objects.create(author=user, name="d")
        _add(dataset, recording)
        (row,) = _browse(_client(user), dataset).json()["items"]
        assert row["modality"] == "eeg"
        assert row["public_source"] == "https://example.org/ds"
        assert row["duration"] == 12.5
        assert row["channel_count"] == 3, "the annotation channel is not a signal channel"
        assert row["sampling_rate_max"] == 500.0
        assert row["sampling_rate_min"] == 256.0
        assert row["channel_layout"] == "bipolar"
        assert row["deidentification_version"] == 6

    def test_a_recording_without_metadata_has_null_columns(self, user):
        dataset = Dataset.objects.create(author=user, name="d")
        _add(dataset, _recording(user, status=Recording.Status.PROCESSING))
        (row,) = _browse(_client(user), dataset).json()["items"]
        for key in ("duration", "channel_count", "sampling_rate_max", "channel_layout", "deidentification_version"):
            assert row[key] is None

    def test_the_display_name_is_the_grantee_visible_one_for_the_author_too(self, user):
        recording = _recording(user, original_name="MRN_12345_routine.edf")
        dataset = Dataset.objects.create(author=user, name="d")
        _add(dataset, recording)
        (row,) = _browse(_client(user), dataset).json()["items"]
        assert row["display_name"] == _hash(recording)[:8]


class TestSort:
    @pytest.fixture
    def three(self, user):
        dataset = Dataset.objects.create(author=user, name="d")
        recordings = [
            _recording(user, index=0, display_name="b"),
            _recording(user, index=1, display_name="a"),
            _recording(user, index=2, display_name="c"),
        ]
        _meta(recordings[0], duration=30.0)
        _meta(recordings[2], duration=10.0)
        for recording in recordings:
            _add(dataset, recording)
        return dataset, recordings

    def test_name_is_the_default(self, user, three):
        dataset, (b, a, c) = three
        assert _hashes(_browse(_client(user), dataset)) == [_hash(a), _hash(b), _hash(c)]

    def test_a_column_ascending_and_descending_with_missing_values_last(self, user, three):
        dataset, (b, a, c) = three
        assert _hashes(_browse(_client(user), dataset, "?sort=duration")) == [_hash(c), _hash(b), _hash(a)]
        assert _hashes(_browse(_client(user), dataset, "?sort=-duration")) == [_hash(b), _hash(c), _hash(a)]

    @pytest.mark.parametrize("sort", ["created_at", "id", "-added_at", "stored_hash", ""])
    def test_a_key_outside_the_allowlist_is_refused(self, user, three, sort):
        dataset, _ = three
        if sort == "":
            assert _browse(_client(user), dataset, "?sort=").status_code == 200
            return
        assert _browse(_client(user), dataset, f"?sort={sort}").status_code == 400

    def test_date_sorts_by_month_then_name_never_by_arrival(self, user):
        dataset = Dataset.objects.create(author=user, name="d")
        later = _recording(user, index=0, display_name="a", created_at=datetime(2026, 5, 20, tzinfo=UTC))
        earlier = _recording(user, index=1, display_name="b", created_at=datetime(2026, 5, 2, tzinfo=UTC))
        previous = _recording(user, index=2, display_name="c", created_at=datetime(2026, 4, 28, tzinfo=UTC))
        for recording in (later, earlier, previous):
            _add(dataset, recording)
        hashes = _hashes(_browse(_client(user), dataset, "?sort=date"))
        assert hashes == [_hash(previous), _hash(later), _hash(earlier)]


class TestMembership:
    def test_media_trashed_and_failed_members_are_left_out(self, user):
        from media.models import MediaFile

        dataset = Dataset.objects.create(author=user, name="d")
        kept = _recording(user, index=0)
        _add(dataset, kept)
        _add(dataset, _recording(user, index=1, deleted_at=timezone.now()))
        _add(dataset, _recording(user, index=2, status=Recording.Status.FAILED))
        _add(dataset, baker.make(MediaFile, author=user))
        body = _browse(_client(user), dataset).json()
        assert [row["hash"] for row in body["items"]] == [_hash(kept)]
        assert body["count"] == 1

    def test_a_gated_dataset_shows_a_reader_released_members_dated_by_release(self, user, make_user):
        reader = make_user()
        released = _recording(user, index=0, created_at=datetime(2026, 3, 14, 9, tzinfo=UTC))
        unreleased = _recording(user, index=1)
        dataset, (item, _other) = _gated_dataset(user, released, unreleased)
        _release(dataset, item)
        _grant(dataset, user, target=reader)
        body = _browse(_client(reader), dataset).json()
        assert body["count"] == 1
        (row,) = body["items"]
        assert row["hash"] == _hash(released)
        assert datetime.fromisoformat(row["created_at"]) == dataset.releases.get().release_month

    def test_a_manager_sees_unreleased_members(self, user, make_user):
        manager = make_user()
        dataset, _items = _gated_dataset(user, _recording(user, index=0), _recording(user, index=1))
        _grant(dataset, user, target=manager, can_write=True)
        assert _browse(_client(manager), dataset).json()["count"] == 2

    def test_paging_with_the_count_of_every_page(self, user):
        dataset = Dataset.objects.create(author=user, name="d")
        recordings = [_recording(user, index=i, display_name=f"r{i}") for i in range(5)]
        for recording in recordings:
            _add(dataset, recording)
        body = _browse(_client(user), dataset, "?limit=2&offset=2").json()
        assert body["count"] == 5
        assert [row["hash"] for row in body["items"]] == [_hash(recordings[2]), _hash(recordings[3])]


class TestDates:
    def test_exact_for_the_author_and_month_truncated_for_a_reader(self, user, make_user):
        reader = make_user()
        uploaded = datetime(2026, 6, 17, 13, 45, tzinfo=UTC)
        recording = _recording(user, created_at=uploaded)
        dataset = Dataset.objects.create(author=user, name="d")
        _add(dataset, recording)
        _grant(dataset, user, target=reader)
        own = _browse(_client(user), dataset).json()["items"][0]["created_at"]
        theirs = _browse(_client(reader), dataset).json()["items"][0]["created_at"]
        assert datetime.fromisoformat(own) == uploaded
        assert datetime.fromisoformat(theirs) == datetime(2026, 6, 1, tzinfo=UTC)


class TestAccess:
    def test_unauthenticated_without_a_token_is_401(self, user):
        dataset = Dataset.objects.create(author=user, name="d")
        assert _browse(_client(), dataset).status_code == 401

    def test_a_caller_without_read_access_is_403(self, user, make_user):
        dataset = Dataset.objects.create(author=user, name="d")
        assert _browse(_client(make_user()), dataset).status_code == 403

    def test_a_share_token_reads_an_ungated_dataset(self, user):
        dataset = Dataset.objects.create(author=user, name="d")
        recording = _recording(user)
        _add(dataset, recording)
        _grant(dataset, user, token=TOKEN)
        assert _hashes(_browse(_client(), dataset, f"?share_token={TOKEN}")) == [_hash(recording)]

    def test_a_share_token_is_refused_on_a_gated_dataset(self, user):
        recording = _recording(user)
        dataset, (item,) = _gated_dataset(user, recording)
        _release(dataset, item)
        _grant(dataset, user, token=TOKEN)
        assert _browse(_client(), dataset, f"?share_token={TOKEN}").status_code == 403


class TestAudit:
    def test_a_browse_writes_an_activity_row_naming_no_one(self, user):
        dataset = Dataset.objects.create(author=user, name="d")
        _add(dataset, _recording(user))
        _browse(_client(user), dataset, "?sort=-duration&limit=10")
        activity = Activity.objects.filter(verb="library.dataset.recording.list").latest("created_at")
        assert activity.target_object_id == str(dataset.pk)
        assert activity.metadata == {
            "sort": "duration",
            "descending": True,
            "limit": 10,
            "offset": 0,
            "filters": [],
            "returned_count": 1,
            "share_token_used": False,
        }


class TestRowTags:
    def test_three_tags_at_most_in_display_order_with_the_count(self, user, make_user):
        from library.models import Tag, TaggedItem

        staff = make_user()
        recording = _recording(user)
        dataset = Dataset.objects.create(author=user, name="d")
        _add(dataset, recording)
        ct = ContentType.objects.get_for_model(Recording, for_concrete_model=False)
        for author, name, curated in [(user, "own b", False), (user, "own a", False), (staff, "curated", True)]:
            tag = baker.make(Tag, author=author, name=name, curated=curated)
            TaggedItem.objects.create(tag=tag, content_type=ct, object_id=str(recording.pk))
        tag = baker.make(Tag, author=staff, name="other", curated=False)
        TaggedItem.objects.create(tag=tag, content_type=ct, object_id=str(recording.pk))
        (row,) = _browse(_client(user), dataset).json()["items"]
        assert [tag["name"] for tag in row["tags"]] == ["curated", "own a", "own b"]
        assert row["tag_count"] == 4

    def test_a_share_token_caller_gets_no_tags(self, user):
        from library.models import Tag, TaggedItem

        recording = _recording(user)
        dataset = Dataset.objects.create(author=user, name="d")
        _add(dataset, recording)
        _grant(dataset, user, token=TOKEN)
        ct = ContentType.objects.get_for_model(Recording, for_concrete_model=False)
        tag = baker.make(Tag, author=user, name="curated", curated=True)
        TaggedItem.objects.create(tag=tag, content_type=ct, object_id=str(recording.pk))
        (row,) = _browse(Client(), dataset, f"?share_token={TOKEN}").json()["items"]
        assert row["tags"] == [] and row["tag_count"] == 0
