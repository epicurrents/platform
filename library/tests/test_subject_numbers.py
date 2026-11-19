"""Number subject fields: the schema form, the values, where a number field is refused, and the browser's range filter.

Covers the field's attributes, the values a member may carry, the refusal on a release-gated dataset, a pool and a
dataset with a release, gating and pooling refused while a number field is declared, a type change refused while
values are in use, the numeric sort, the range filter and its facet, and the subject-table import's number mapping.
"""

from __future__ import annotations

import json

import pytest

from library import pools
from library.models import Dataset, DatasetItemSubject, DatasetMeta
from library.subject_import import ImportRefused, load_mapping
from library.tests.test_browse_facets import _browse, _client, _member, _names, _recording
from library.tests.test_release import _add, _gated_dataset, _grant, _release
from library.tests.test_subject_import import _files, _named, _run, _values
from library.tests.test_subjects import _patch_values, _put_schema

pytestmark = pytest.mark.django_db

DATASETS = "/api/v1/library/datasets"
AGE = {"key": "age", "label": "Age (years)", "type": "number"}
SEX = {"key": "sex", "label": "Sex", "choices": ["female", "male"]}


def _dataset(author, schema=(AGE, SEX), **fields):
    dataset = Dataset.objects.create(author=author, name="published", **fields)
    DatasetMeta.objects.create(dataset=dataset, subject_schema=list(schema))
    return dataset


@pytest.fixture
def ages(user, make_user):
    """An ungated dataset of five recordings aged 10, 9, 2.5 and 16 years and one without an age, and a reader."""
    reader = make_user()
    dataset = _dataset(user)
    for index, (age, sex) in enumerate([(10, "male"), (9, "female"), (2.5, "female"), (16, "male"), (None, "male")]):
        values = {"sex": sex, **({"age": age} if age is not None else {})}
        _member(dataset, _recording(user, index), **values)
    _grant(dataset, user, target=reader)
    return dataset, reader


class TestSchema:
    def test_a_number_field_is_stored_with_its_type(self, user):
        dataset = _dataset(user, schema=())
        response = _put_schema(_client(user), dataset, [AGE, SEX])
        assert response.status_code == 200, response.content
        assert DatasetMeta.objects.get(dataset=dataset).subject_schema == [AGE, SEX]
        assert response.json()["fields"][0] == {**AGE, "choices": [], "displayed": False}

    @pytest.mark.parametrize(
        "field",
        [
            {**AGE, "type": "integer"},
            {**AGE, "type": "choice"},
            {**AGE, "choices": ["1"]},
            {**AGE, "displayed": True},
            {**AGE, "unit": "years"},
        ],
    )
    def test_a_malformed_number_field_is_refused(self, user, field):
        dataset = _dataset(user, schema=())
        assert _put_schema(_client(user), dataset, [field]).status_code == 400


class TestValues:
    def test_a_member_carries_a_number(self, user):
        dataset = _dataset(user)
        recording = _recording(user, 0)
        _add(dataset, recording)
        response = _patch_values(_client(user), dataset, recording, {"age": 1.487123288, "sex": "female"})
        assert response.status_code == 200, response.content
        assert response.json()["values"] == {"age": 1.487123288, "sex": "female"}

    @pytest.mark.parametrize("value", ["1.5", True, [1], {"v": 1}, 1e16])
    def test_anything_but_a_finite_number_is_refused(self, user, value):
        dataset = _dataset(user)
        recording = _recording(user, 0)
        _add(dataset, recording)
        assert _patch_values(_client(user), dataset, recording, {"age": value}).status_code == 400

    def test_infinity_is_refused(self, user):
        dataset = _dataset(user)
        recording = _recording(user, 0)
        _add(dataset, recording)
        response = _client(user).patch(
            f"{DATASETS}/{dataset.object_hash}/recordings/{recording.stored_name.split('.')[0]}/subject/",
            '{"values": {"age": Infinity}}',
            content_type="application/json",
        )
        assert response.status_code in (400, 422)
        assert not DatasetItemSubject.objects.exists()

    def test_a_field_holding_numbers_cannot_become_a_field_of_choices(self, user):
        dataset = _dataset(user)
        _member(dataset, _recording(user, 0), age=4)
        bands = {"key": "age", "label": "Age", "choices": ["0-9", "10+"]}
        assert _put_schema(_client(user), dataset, [bands, SEX]).status_code == 409

    def test_a_field_holding_choices_cannot_become_a_number_field(self, user):
        dataset = _dataset(user, schema=(SEX,))
        _member(dataset, _recording(user, 0), sex="male")
        number = {"key": "sex", "label": "Sex", "type": "number"}
        assert _put_schema(_client(user), dataset, [number]).status_code == 409


class TestScope:
    def test_refused_on_a_release_gated_dataset(self, user):
        dataset, _items = _gated_dataset(user)
        assert _put_schema(_client(user), dataset, [AGE]).status_code == 409

    def test_refused_on_a_dataset_with_a_release(self, user):
        dataset = _dataset(user, schema=(SEX,))
        _release(dataset, _member(dataset, _recording(user, 0)))
        assert _put_schema(_client(user), dataset, [SEX, AGE]).status_code == 409

    def test_refused_on_a_pool(self, user):
        dataset = _dataset(user, schema=())
        Dataset.objects.filter(pk=dataset.pk).update(submission_profile="any")
        dataset.refresh_from_db()
        assert _put_schema(_client(user), dataset, [AGE]).status_code == 409

    def test_a_dataset_declaring_one_cannot_be_gated(self, user):
        dataset = _dataset(user)
        response = _client(user).patch(
            f"{DATASETS}/{dataset.object_hash}/", json.dumps({"release_gated": True}), content_type="application/json"
        )
        assert response.status_code == 409
        dataset.refresh_from_db()
        assert dataset.release_gated is False

    def test_a_dataset_declaring_one_cannot_be_made_a_pool(self, user):
        dataset = _dataset(user)
        with pytest.raises(pools.PoolError) as exc:
            pools.configure_pool(dataset, "any")
        assert exc.value.status == 409
        dataset.refresh_from_db()
        assert not dataset.submission_profile and not dataset.release_gated


class TestBrowser:
    def test_the_sort_is_numeric_with_the_blank_last(self, ages):
        dataset, reader = ages
        assert _names(_browse(_client(reader), dataset, "?sort=subject.age")) == [
            "rec 02",
            "rec 01",
            "rec 00",
            "rec 03",
            "rec 04",
        ]
        assert _names(_browse(_client(reader), dataset, "?sort=-subject.age"))[:4] == [
            "rec 03",
            "rec 00",
            "rec 01",
            "rec 02",
        ]

    def test_the_rows_carry_the_number(self, ages):
        dataset, reader = ages
        rows = {row["display_name"]: row["subject"] for row in _browse(_client(reader), dataset)["items"]}
        assert rows["rec 02"] == {"age": 2.5, "sex": "female"}
        assert rows["rec 04"] == {"sex": "male"}

    @pytest.mark.parametrize(
        ("query", "expected"),
        [
            ("?subject.age.min=9", ["rec 00", "rec 01", "rec 03"]),
            ("?subject.age.max=9", ["rec 01", "rec 02"]),
            ("?subject.age.min=3&subject.age.max=12", ["rec 00", "rec 01"]),
            ("?subject.age.min=&subject.age.max=", ["rec 00", "rec 01", "rec 02", "rec 03", "rec 04"]),
            ("?subject.age.min=12&subject.age.max=3", []),
        ],
    )
    def test_the_range_is_inclusive_and_open_at_an_empty_end(self, ages, query, expected):
        dataset, reader = ages
        page = _browse(_client(reader), dataset, query)
        assert _names(page) == expected
        assert page["count"] == len(expected)

    @pytest.mark.parametrize(
        "query",
        [
            "?subject.age.min=nine",
            "?subject.age.min=inf",
            "?subject.age.min=1&subject.age.min=2",
            "?subject.age=9",
            "?subject.age.mid=9",
            "?subject.sex.min=1",
            "?subject.height.min=1",
        ],
    )
    def test_a_malformed_range_is_refused(self, ages, query):
        dataset, reader = ages
        response = _client(reader).get(f"{DATASETS}/{dataset.object_hash}/browse/{query}")
        assert response.status_code == 400

    def test_the_facet_spans_the_values_under_the_other_filters(self, ages):
        dataset, reader = ages
        facets = _browse(_client(reader), dataset)["facets"]
        assert facets["ranges"] == {"age": {"min": 2.5, "max": 16.0, "count": 4}}
        assert "age" not in facets["subject"]
        under_sex = _browse(_client(reader), dataset, "?subject.sex=female")["facets"]["ranges"]
        assert under_sex == {"age": {"min": 2.5, "max": 9.0, "count": 2}}
        under_own = _browse(_client(reader), dataset, "?subject.age.min=9")["facets"]
        assert under_own["ranges"]["age"] == {"min": 2.5, "max": 16.0, "count": 4}
        assert {entry["value"]: entry["count"] for entry in under_own["subject"]["sex"]} == {"female": 1, "male": 2}

    def test_an_empty_facet_has_no_bounds(self, user, make_user):
        reader = make_user()
        dataset = _dataset(user)
        _member(dataset, _recording(user, 0), sex="male")
        _grant(dataset, user, target=reader)
        assert _browse(_client(reader), dataset)["facets"]["ranges"] == {"age": {"min": None, "max": None, "count": 0}}


class TestImport:
    MAPPING = {"match": {"column": "file", "on": "name"}, "fields": {"age": {"column": "age"}}}

    def test_a_cell_is_stored_as_its_number(self, user, tmp_path):
        recordings = [_named(user, index, f"{name}.edf") for index, name in enumerate(["a1", "b2", "c3"])]
        dataset = _dataset(user)
        items = [_add(dataset, recording) for recording in recordings]
        table, mapping = _files(
            tmp_path, [["a1.edf", "1.487123288"], ["b2.edf", "12"], ["c3.edf", "twelve"]], self.MAPPING, ["file", "age"]
        )
        _run(dataset, table, mapping)
        assert [_values(item) for item in items] == [{"age": 1.487123288}, {"age": 12.0}, None]

    @pytest.mark.parametrize(
        "spec",
        [
            {"column": "age", "values": {"1": "1"}},
            {"column": "age", "bands": [{"below": 1, "choice": "x"}]},
        ],
    )
    def test_a_number_field_maps_a_column_and_nothing_else(self, spec):
        mapping = {"match": {"column": "file"}, "fields": {"age": spec}}
        with pytest.raises(ImportRefused):
            load_mapping(mapping, [AGE], ["file", "age"])
