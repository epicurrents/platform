"""Tags addressed by hash, and the dataset batch tag.

Covers tagging a recording and a media file by its hash, the ``object_hash`` on the tagged-item responses, untagging
by content type and hash with the permission checked before the lookup, and tagging and untagging the members a
browser query matches: the filters, the members skipped for want of write access, the tag author's untag, the counts,
one audit row per tagged item and the refusals.
"""

from __future__ import annotations

import pytest
from django.contrib.contenttypes.models import ContentType
from model_bakery import baker

from activity.models import Activity, ObjectChangeLog
from library.models import Dataset, DatasetMeta, Tag, TaggedItem
from library.tests.test_browse_facets import _client, _member, _recording
from library.tests.test_release import _grant
from recordings.models import Recording

pytestmark = pytest.mark.django_db

DATASETS = "/api/v1/library/datasets"
TAGS = "/api/v1/library/tags"
SEX = {"key": "sex", "label": "Sex", "choices": ["female", "male"]}


def _recording_ct():
    return ContentType.objects.get_for_model(Recording, for_concrete_model=False)


def _hash(recording):
    return recording.stored_name.split(".", 1)[0]


def _tagged(tag):
    return {int(object_id) for object_id in TaggedItem.objects.filter(tag=tag).values_list("object_id", flat=True)}


def _tag_by_hash(client, tag, recording):
    return client.post(
        f"{TAGS}/{tag.pk}/items/",
        {"content_type_id": _recording_ct().pk, "object_id": _hash(recording)},
        content_type="application/json",
    )


class TestTagByHash:
    def test_a_recording_is_tagged_by_its_hash(self, user):
        recording = _recording(user, 0)
        tag = baker.make(Tag, author=user, name="normal")
        response = _tag_by_hash(_client(user), tag, recording)
        assert response.status_code == 201, response.content
        assert response.json()["object_hash"] == _hash(recording)
        assert _tagged(tag) == {recording.pk}

    def test_the_hash_is_case_insensitive(self, user):
        recording = _recording(user, 0)
        tag = baker.make(Tag, author=user)
        response = _client(user).post(
            f"{TAGS}/{tag.pk}/items/",
            {"content_type_id": _recording_ct().pk, "object_id": _hash(recording).lower()},
            content_type="application/json",
        )
        assert response.status_code == 201

    def test_a_media_file_is_tagged_by_its_content_hash(self, user):
        from media.models import MediaFile

        media = baker.make(MediaFile, author=user, content_hash="AB" * 32, file_size=1)
        tag = baker.make(Tag, author=user)
        ct = ContentType.objects.get_for_model(MediaFile, for_concrete_model=False)
        response = _client(user).post(
            f"{TAGS}/{tag.pk}/items/",
            {"content_type_id": ct.pk, "object_id": media.content_hash},
            content_type="application/json",
        )
        assert response.status_code == 201, response.content
        assert response.json()["object_hash"] == media.content_hash
        assert TaggedItem.objects.get(tag=tag).object_id == str(media.pk)

    def test_the_hash_and_the_key_name_the_same_tagging(self, user):
        recording = _recording(user, 0)
        tag = baker.make(Tag, author=user)
        client = _client(user)
        assert _tag_by_hash(client, tag, recording).status_code == 201
        response = client.post(
            f"{TAGS}/{tag.pk}/items/",
            {"content_type_id": _recording_ct().pk, "object_id": str(recording.pk)},
            content_type="application/json",
        )
        assert response.status_code == 409

    @pytest.mark.parametrize(("object_id", "status"), [("F" * 32, 404), ("not-a-hash", 400)])
    def test_a_hash_naming_nothing_is_refused(self, user, object_id, status):
        tag = baker.make(Tag, author=user)
        response = _client(user).post(
            f"{TAGS}/{tag.pk}/items/",
            {"content_type_id": _recording_ct().pk, "object_id": object_id},
            content_type="application/json",
        )
        assert response.status_code == status

    def test_the_listing_names_each_item_by_its_hash(self, user):
        recording = _recording(user, 0)
        tag = baker.make(Tag, author=user)
        TaggedItem.objects.create(tag=tag, content_type=_recording_ct(), object_id=str(recording.pk))
        rows = _client(user).get(f"{TAGS}/{tag.pk}/items/").json()
        assert [row["object_hash"] for row in rows] == [_hash(recording)]


class TestUntagByHash:
    def _url(self, tag, object_id):
        return f"{TAGS}/{tag.pk}/items/?content_type_id={_recording_ct().pk}&object_id={object_id}"

    def test_the_writer_untags_by_hash(self, user):
        recording = _recording(user, 0)
        tag = baker.make(Tag, author=user)
        TaggedItem.objects.create(tag=tag, content_type=_recording_ct(), object_id=str(recording.pk))
        assert _client(user).delete(self._url(tag, _hash(recording))).status_code == 200
        assert not TaggedItem.objects.exists()

    def test_the_tag_author_untags_an_object_of_someone_else(self, user, make_user):
        owner = make_user()
        recording = _recording(owner, 0)
        _grant(recording, owner, target=user)
        tag = baker.make(Tag, author=user)
        TaggedItem.objects.create(tag=tag, content_type=_recording_ct(), object_id=str(recording.pk))
        assert _client(user).delete(self._url(tag, _hash(recording))).status_code == 200

    def test_an_object_without_the_tag_is_404(self, user):
        recording = _recording(user, 0)
        tag = baker.make(Tag, author=user)
        assert _client(user).delete(self._url(tag, _hash(recording))).status_code == 404

    def test_whether_an_object_carries_the_tag_is_not_answered_to_whom_may_not_remove_it(self, user, make_user):
        owner = make_user()
        tagged, untagged = _recording(owner, 0), _recording(owner, 1)
        for recording in (tagged, untagged):
            _grant(recording, owner, target=user)
        tag = baker.make(Tag, author=owner, curated=True)
        TaggedItem.objects.create(tag=tag, content_type=_recording_ct(), object_id=str(tagged.pk))
        client = _client(user)
        assert client.delete(self._url(tag, _hash(tagged))).status_code == 403
        assert client.delete(self._url(tag, _hash(untagged))).status_code == 403
        assert TaggedItem.objects.count() == 1


class TestNoExistenceAnswer:
    """An object the caller cannot read is answered like one that does not exist, on every tag write."""

    @pytest.fixture
    def unreadable(self, make_user):
        owner = make_user()
        failed = _recording(owner, 1, status=Recording.Status.FAILED)
        return _recording(owner, 0), failed

    def _responses(self, client, tag, object_id):
        ct = _recording_ct().pk
        add = client.post(
            f"{TAGS}/{tag.pk}/items/", {"content_type_id": ct, "object_id": object_id}, content_type="application/json"
        )
        remove = client.delete(f"{TAGS}/{tag.pk}/items/?content_type_id={ct}&object_id={object_id}")
        return [(response.status_code, response.json()) for response in (add, remove)]

    def test_unreadable_failed_and_missing_answer_alike(self, user, unreadable):
        tag = baker.make(Tag, author=user)
        client = _client(user)
        missing = self._responses(client, tag, "E" * 32)
        assert missing == [(404, {"detail": "Object not found"})] * 2
        for recording in unreadable:
            assert self._responses(client, tag, _hash(recording)) == missing
            assert self._responses(client, tag, str(recording.pk)) == missing
        assert not TaggedItem.objects.exists()


@pytest.fixture
def members(user, make_user):
    """An ungated dataset of four recordings, two of them the caller's own and two another author's, and a tag."""
    other = make_user()
    dataset = Dataset.objects.create(author=user, name="published")
    DatasetMeta.objects.create(dataset=dataset, subject_schema=[SEX])
    recordings = [
        _recording(user, 0),
        _recording(user, 1),
        _recording(other, 2),
        _recording(other, 3),
    ]
    for recording, sex in zip(recordings, ["female", "male", "female", "male"], strict=True):
        _member(dataset, recording, sex=sex)
    for recording in recordings[2:]:
        _grant(recording, other, target=user)
    tag = baker.make(Tag, author=other, name="normal", curated=True)
    return dataset, recordings, tag


def _batch(client, dataset, tag, query="", *, remove=False):
    url = f"{DATASETS}/{dataset.object_hash}/tags/{tag.pk}/{query}"
    return client.delete(url) if remove else client.post(url)


class TestBatchTag:
    def test_every_member_the_caller_may_write_is_tagged(self, user, members):
        dataset, recordings, tag = members
        response = _batch(_client(user), dataset, tag)
        assert response.status_code == 200, response.content
        assert response.json() == {"matched": 4, "changed": 2, "unchanged": 0, "skipped": 2}
        assert _tagged(tag) == {recordings[0].pk, recordings[1].pk}

    def test_the_filters_narrow_the_members(self, user, members):
        dataset, recordings, tag = members
        response = _batch(_client(user), dataset, tag, "?subject.sex=female")
        assert response.json() == {"matched": 2, "changed": 1, "unchanged": 0, "skipped": 1}
        assert _tagged(tag) == {recordings[0].pk}

    def test_a_second_run_changes_nothing(self, user, members):
        dataset, _recordings, tag = members
        client = _client(user)
        _batch(client, dataset, tag)
        assert _batch(client, dataset, tag).json() == {"matched": 4, "changed": 0, "unchanged": 2, "skipped": 2}

    def test_each_tagging_is_audited(self, user, members):
        dataset, _recordings, tag = members
        _batch(_client(user), dataset, tag)
        ct = ContentType.objects.get_for_model(TaggedItem)
        assert ObjectChangeLog.objects.filter(content_type=ct, action=ObjectChangeLog.ACTION_CREATE).count() == 2
        activity = Activity.objects.get(verb="library.dataset.tag.add")
        assert activity.metadata["tag_id"] == tag.pk
        assert activity.metadata["changed"] == 2

    def test_the_writer_untags(self, user, members):
        dataset, recordings, tag = members
        for recording in recordings:
            TaggedItem.objects.create(tag=tag, content_type=_recording_ct(), object_id=str(recording.pk))
        response = _batch(_client(user), dataset, tag, "?subject.sex=male", remove=True)
        assert response.json() == {"matched": 2, "changed": 1, "unchanged": 0, "skipped": 1}
        assert _tagged(tag) == {recordings[0].pk, recordings[2].pk, recordings[3].pk}
        ct = ContentType.objects.get_for_model(TaggedItem)
        assert ObjectChangeLog.objects.filter(content_type=ct, action=ObjectChangeLog.ACTION_DELETE).count() == 1
        assert Activity.objects.get(verb="library.dataset.tag.remove").metadata["changed"] == 1

    def test_the_tag_author_untags_every_member(self, make_user, members):
        dataset, recordings, tag = members
        for recording in recordings:
            TaggedItem.objects.create(tag=tag, content_type=_recording_ct(), object_id=str(recording.pk))
        _grant(dataset, dataset.author, target=tag.author)
        response = _batch(_client(tag.author), dataset, tag, remove=True)
        assert response.json() == {"matched": 4, "changed": 4, "unchanged": 0, "skipped": 0}

    def test_a_reader_without_access_to_the_dataset_is_refused(self, make_user, members):
        dataset, _recordings, tag = members
        assert _batch(_client(make_user()), dataset, tag).status_code == 403
        assert not TaggedItem.objects.exists()

    def test_a_tag_outside_reach_is_404(self, user, make_user, members):
        dataset, _recordings, _tag = members
        private = baker.make(Tag, author=make_user(), name="private")
        assert _batch(_client(user), dataset, private).status_code == 404

    def test_a_malformed_filter_is_refused(self, user, members):
        dataset, _recordings, tag = members
        assert _batch(_client(user), dataset, tag, "?subject.sex=other").status_code == 400
        assert not TaggedItem.objects.exists()

    def test_a_match_over_the_cap_is_refused_and_writes_nothing(self, user, members, monkeypatch):
        from library.api.v1 import ninja

        dataset, _recordings, tag = members
        monkeypatch.setattr(ninja, "MAX_BATCH_TAGGED", 3)
        assert _batch(_client(user), dataset, tag).status_code == 400
        assert _batch(_client(user), dataset, tag, "?subject.sex=female").status_code == 200
        assert len(_tagged(tag)) == 1

    def test_a_caller_without_an_account_is_refused(self, members):
        dataset, _recordings, tag = members
        assert _batch(_client(), dataset, tag).status_code == 401
