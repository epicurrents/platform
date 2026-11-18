"""Subject fields: the schema a dataset declares, the values its members carry, and how the browser shows them.

Covers schema validation, the schema and values endpoints with their access and refusals, the pool lock, the
audit trail withholding the values, the rollback guards, the cascade with a membership, and the browser's
subject column and sort keys, including what a reader of a release-gated dataset is not shown.
"""

from __future__ import annotations

import json

import pytest
from django.contrib.contenttypes.models import ContentType
from django.test import Client

from activity.audit import rollback_refusal
from activity.models import Activity, ObjectChangeLog
from library import subjects
from library.models import Dataset, DatasetItemSubject, DatasetMeta
from library.tests.test_release import _add, _gated_dataset, _grant, _recording, _release
from recordings.models import SubmissionLedger

pytestmark = pytest.mark.django_db

DATASETS = "/api/v1/library/datasets"
AGE = {"key": "age", "label": "Age", "choices": ["0-17", "18-64", "65+"]}
SEX = {"key": "sex", "label": "Sex", "choices": ["female", "male"]}
# Distinctive, so a search of the audit trail for it cannot match anything else.
MARKER = "band-seventy-plus"


def _client(user=None) -> Client:
    client = Client()
    if user is not None:
        client.force_login(user)
    return client


def _put_schema(client, dataset, fields):
    return client.put(
        f"{DATASETS}/{dataset.object_hash}/subject-schema/",
        json.dumps({"fields": fields}),
        content_type="application/json",
    )


def _patch_values(client, dataset, recording, values):
    return client.patch(
        f"{DATASETS}/{dataset.object_hash}/recordings/{_hash(recording)}/subject/",
        json.dumps({"values": values}),
        content_type="application/json",
    )


def _hash(recording):
    return recording.stored_name.split(".")[0]


def _with_schema(dataset, *fields):
    DatasetMeta.objects.update_or_create(dataset=dataset, defaults={"subject_schema": list(fields)})


def _set(item, **values):
    return DatasetItemSubject.objects.update_or_create(item=item, defaults={"values": values})[0]


class TestValidateSchema:
    def test_strings_are_stripped(self):
        (field,) = subjects.validate_schema([{"key": "age", "label": " Age ", "choices": [" 0-17 ", "18+"]}])
        assert field == {"key": "age", "label": "Age", "choices": ["0-17", "18+"]}

    @pytest.mark.parametrize(
        "fields",
        [
            {"key": "age"},
            [{"key": "age", "label": "Age"}],
            [{**AGE, "displayed": True}],
            [{**AGE, "key": "Age"}],
            [{**AGE, "key": "1age"}],
            [AGE, AGE],
            [{**AGE, "label": " "}],
            [{**AGE, "choices": []}],
            [{**AGE, "choices": ["a", "a"]}],
            [{**AGE, "choices": ["a", 3]}],
            [{**AGE, "choices": ["x" * 65]}],
            [{**AGE, "key": f"f{i}"} for i in range(subjects.MAX_FIELDS + 1)],
        ],
    )
    def test_a_faulty_schema_is_refused(self, fields):
        with pytest.raises(subjects.SubjectError) as exc:
            subjects.validate_schema(fields)
        assert exc.value.status == 400


class TestSchemaEndpoint:
    def test_a_manager_sets_the_schema_and_a_reader_reads_it(self, user, make_user):
        reader = make_user()
        dataset = Dataset.objects.create(author=user, name="d")
        _grant(dataset, user, target=reader)
        response = _put_schema(_client(user), dataset, [AGE, SEX])
        assert response.status_code == 200, response.content
        assert response.json()["fields"] == [AGE, SEX]
        read = _client(reader).get(f"{DATASETS}/{dataset.object_hash}/subject-schema/")
        assert read.json() == {"fields": [AGE, SEX]}

    def test_a_write_grantee_is_a_manager_and_a_reader_is_not(self, user, make_user):
        writer, reader = make_user(), make_user()
        dataset = Dataset.objects.create(author=user, name="d")
        _grant(dataset, user, target=writer, can_write=True)
        _grant(dataset, user, target=reader)
        assert _put_schema(_client(writer), dataset, [AGE]).status_code == 200
        assert _put_schema(_client(reader), dataset, [SEX]).status_code == 403
        assert _put_schema(_client(), dataset, [SEX]).status_code == 401
        assert subjects.schema_of(dataset) == [AGE]

    def test_a_faulty_schema_is_a_400(self, user):
        dataset = Dataset.objects.create(author=user, name="d")
        assert _put_schema(_client(user), dataset, [{**AGE, "key": "Age"}]).status_code == 400

    def test_an_identifier_like_label_is_warned_about_and_kept(self, user):
        dataset = Dataset.objects.create(author=user, name="d")
        response = _put_schema(_client(user), dataset, [{**AGE, "label": "Age at MRN 12345678"}])
        assert response.status_code == 200
        assert [row["field"] for row in response.json()["warnings"]] == ["age.label"]

    def test_dropping_a_choice_in_use_is_refused_and_an_unused_one_is_not(self, user):
        dataset = Dataset.objects.create(author=user, name="d")
        _with_schema(dataset, AGE, SEX)
        _set(_add(dataset, _recording(user)), age="18-64")
        assert _put_schema(_client(user), dataset, [{**AGE, "choices": ["0-17", "65+"]}, SEX]).status_code == 409
        assert _put_schema(_client(user), dataset, [AGE]).status_code == 200
        assert _put_schema(_client(user), dataset, [{**AGE, "choices": ["18-64"]}]).status_code == 200

    def test_a_filling_pool_keeps_its_schema(self, user):
        dataset = Dataset.objects.create(author=user, name="p", release_gated=True, submission_profile="test.pool")
        _with_schema(dataset, AGE)
        assert _put_schema(_client(user), dataset, [AGE, SEX]).status_code == 200
        SubmissionLedger.objects.create(dataset=dataset, contributor=user)
        assert _put_schema(_client(user), dataset, [AGE]).status_code == 409
        assert _put_schema(_client(user), dataset, [AGE, SEX]).status_code == 200, "an unchanged schema is no change"

    def test_a_gated_dataset_shows_a_reader_no_schema(self, user, make_user):
        reader = make_user()
        dataset, (item,) = _gated_dataset(user, _recording(user))
        _release(dataset, item)
        _with_schema(dataset, AGE)
        _grant(dataset, user, target=reader)
        assert _client(reader).get(f"{DATASETS}/{dataset.object_hash}/subject-schema/").json() == {"fields": []}
        assert _client(user).get(f"{DATASETS}/{dataset.object_hash}/subject-schema/").json() == {"fields": [AGE]}

    def test_the_audit_row_counts_and_names_nothing(self, user):
        dataset = Dataset.objects.create(author=user, name="d")
        _put_schema(_client(user), dataset, [AGE, SEX])
        activity = Activity.objects.filter(verb="library.dataset.subject_schema.update").latest("created_at")
        assert activity.metadata == {"field_count": 2, "choice_count": 5}


class TestValuesEndpoint:
    @pytest.fixture
    def member(self, user):
        dataset = Dataset.objects.create(author=user, name="d")
        _with_schema(dataset, AGE, SEX)
        recording = _recording(user)
        return dataset, _add(dataset, recording), recording

    def test_values_merge_and_clear(self, user, member):
        dataset, item, recording = member
        client = _client(user)
        assert _patch_values(client, dataset, recording, {"age": "65+"}).json() == {"values": {"age": "65+"}}
        assert _patch_values(client, dataset, recording, {"sex": "female"}).json()["values"] == {
            "age": "65+",
            "sex": "female",
        }
        assert _patch_values(client, dataset, recording, {"age": None}).json()["values"] == {"sex": "female"}
        _patch_values(client, dataset, recording, {"sex": None})
        assert not DatasetItemSubject.objects.filter(item=item).exists(), "no values is no row"

    @pytest.mark.parametrize("values", [{"weight": "70"}, {"age": "42"}, {"age": 42}])
    def test_an_undeclared_key_or_value_is_a_400(self, user, member, values):
        dataset, item, recording = member
        assert _patch_values(_client(user), dataset, recording, values).status_code == 400
        assert not DatasetItemSubject.objects.filter(item=item).exists()

    def test_a_reader_may_not_set_values(self, user, make_user, member):
        dataset, _item, recording = member
        reader = make_user()
        _grant(dataset, user, target=reader)
        assert _patch_values(_client(reader), dataset, recording, {"age": "65+"}).status_code == 403

    def test_a_recording_outside_the_dataset_is_a_404(self, user, member):
        dataset, _item, _recording_in = member
        outside = _recording(user, index=1)
        assert _patch_values(_client(user), dataset, outside, {"age": "65+"}).status_code == 404

    def test_a_released_member_of_a_gated_dataset_keeps_its_values(self, user):
        released, unreleased = _recording(user, index=0), _recording(user, index=1)
        dataset, (item, _other) = _gated_dataset(user, released, unreleased)
        _with_schema(dataset, AGE)
        _release(dataset, item)
        assert _patch_values(_client(user), dataset, released, {"age": "65+"}).status_code == 409
        assert _patch_values(_client(user), dataset, unreleased, {"age": "65+"}).status_code == 200

    def test_the_trail_withholds_the_values(self, user):
        dataset = Dataset.objects.create(author=user, name="d")
        _with_schema(dataset, {"key": "age", "label": "Age", "choices": [MARKER, "other"]})
        recording = _recording(user)
        _add(dataset, recording)
        client = _client(user)
        _patch_values(client, dataset, recording, {"age": MARKER})
        _patch_values(client, dataset, recording, {"age": "other"})
        _patch_values(client, dataset, recording, {"age": None})
        ct = ContentType.objects.get_for_model(DatasetItemSubject)
        changes = ObjectChangeLog.objects.filter(content_type=ct)
        assert {c.action for c in changes} == {
            ObjectChangeLog.ACTION_CREATE,
            ObjectChangeLog.ACTION_MODIFY,
            ObjectChangeLog.ACTION_DELETE,
        }
        trail = json.dumps([[c.before_state, c.changes] for c in changes])
        assert MARKER not in trail and '"other"' not in trail
        activity = Activity.objects.filter(verb="library.dataset.member.subject.update").latest("created_at")
        assert activity.metadata == {"set_count": 0, "cleared_count": 1}

    def test_a_values_rollback_is_refused(self, user, member):
        dataset, _item, recording = member
        client = _client(user)
        _patch_values(client, dataset, recording, {"age": "65+"})
        _patch_values(client, dataset, recording, {"age": "0-17"})
        ct = ContentType.objects.get_for_model(DatasetItemSubject)
        change = ObjectChangeLog.objects.filter(content_type=ct, action=ObjectChangeLog.ACTION_MODIFY).latest("pk")
        assert rollback_refusal(change) is not None
        response = client.post(f"/api/v1/activity/rollback/{change.pk}", content_type="application/json")
        assert response.status_code in (400, 403)
        assert DatasetItemSubject.objects.get().values == {"age": "0-17"}

    def test_values_go_with_the_membership(self, user, member):
        _dataset, item, _recording_in = member
        _set(item, age="65+")
        item.delete()
        assert not DatasetItemSubject.objects.exists()


class TestSchemaRollback:
    def test_a_rollback_that_changes_the_schema_is_refused(self, user):
        dataset = Dataset.objects.create(author=user, name="d")
        client = _client(user)
        _put_schema(client, dataset, [AGE])
        _put_schema(client, dataset, [AGE, SEX])
        ct = ContentType.objects.get_for_model(DatasetMeta)
        for change in ObjectChangeLog.objects.filter(content_type=ct):
            assert rollback_refusal(change) is not None
        assert subjects.schema_of(dataset) == [AGE, SEX]


class TestBrowse:
    @pytest.fixture
    def three(self, user):
        dataset = Dataset.objects.create(author=user, name="d")
        _with_schema(dataset, AGE)
        old = _recording(user, index=0, display_name="a")
        young = _recording(user, index=1, display_name="b")
        unknown = _recording(user, index=2, display_name="c")
        _set(_add(dataset, old), age="65+")
        _set(_add(dataset, young), age="0-17")
        _add(dataset, unknown)
        return dataset, old, young, unknown

    def _browse(self, client, dataset, query=""):
        response = client.get(f"{DATASETS}/{dataset.object_hash}/browse/{query}")
        assert response.status_code == 200, response.content
        return response.json()

    def test_rows_carry_the_values_and_the_page_the_fields(self, user, make_user, three):
        dataset, old, _young, unknown = three
        reader = make_user()
        _grant(dataset, user, target=reader)
        body = self._browse(_client(reader), dataset)
        assert body["subject_fields"] == [AGE]
        by_hash = {row["hash"]: row["subject"] for row in body["items"]}
        assert by_hash[_hash(old)] == {"age": "65+"}
        assert by_hash[_hash(unknown)] == {}

    def test_a_subject_sort_follows_the_declared_choices_with_missing_last(self, user, three):
        dataset, old, young, unknown = three
        ascending = self._browse(_client(user), dataset, "?sort=subject.age")
        assert [row["hash"] for row in ascending["items"]] == [_hash(young), _hash(old), _hash(unknown)]
        descending = self._browse(_client(user), dataset, "?sort=-subject.age")
        assert [row["hash"] for row in descending["items"]] == [_hash(old), _hash(young), _hash(unknown)]

    def test_an_undeclared_subject_key_is_a_400(self, user, three):
        dataset, *_ = three
        assert _client(user).get(f"{DATASETS}/{dataset.object_hash}/browse/?sort=subject.sex").status_code == 400

    def test_a_reader_of_a_gated_dataset_is_shown_no_subject_fields(self, user, make_user):
        reader = make_user()
        recording = _recording(user)
        dataset, (item,) = _gated_dataset(user, recording)
        _with_schema(dataset, AGE)
        _set(item, age="65+")
        _release(dataset, item)
        _grant(dataset, user, target=reader)
        body = self._browse(_client(reader), dataset)
        assert body["subject_fields"] == []
        assert body["items"][0]["subject"] == {}
        refused = _client(reader).get(f"{DATASETS}/{dataset.object_hash}/browse/?sort=subject.age")
        assert refused.status_code == 400
        assert self._browse(_client(user), dataset)["items"][0]["subject"] == {"age": "65+"}
