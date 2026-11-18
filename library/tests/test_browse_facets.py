"""Filters, facets and small-count suppression in the dataset browser, and the catalogue of released datasets.

Covers each filter and how they combine, the refusal of a filter on a field the caller is not shown, the facet counts
under the other dimensions' filters, suppression below k on a release-gated dataset to a reader who does not manage
it (counts, rows and the second count of a partition), where k comes from, and ``GET /catalogue/``.
"""

from __future__ import annotations

from datetime import date

import pytest
from django.contrib.contenttypes.models import ContentType
from django.test import Client
from model_bakery import baker

from activity.models import Activity
from library.browse import FALLBACK_SUPPRESSION_K
from library.models import Dataset, DatasetItemSubject, DatasetMeta, Tag, TaggedItem
from library.tests.test_release import _add, _grant, _release
from recordings.models import Recording

pytestmark = pytest.mark.django_db

DATASETS = "/api/v1/library/datasets"
CATALOGUE = "/api/v1/library/catalogue/"
AGE = {"key": "age", "label": "Age band", "choices": ["0-17", "18-64", "65+"], "displayed": True}
SITE = {"key": "site", "label": "Site", "choices": ["a", "b"]}


def _client(user=None) -> Client:
    client = Client()
    if user is not None:
        client.force_login(user)
    return client


def _recording(author, index, **fields):
    defaults = {
        "author": author,
        "stored_name": f"{0xF00 + index:032X}.edf",
        "file_extension": ".edf",
        "file_size": 1,
        "status": Recording.Status.READY,
        "display_name": f"rec {index:02d}",
        "modality": "eeg",
    }
    defaults.update(fields)
    return baker.make(Recording, **defaults)


def _member(dataset, recording, **values):
    item = _add(dataset, recording)
    if values:
        DatasetItemSubject.objects.create(item=item, values=values)
    return item


def _tag(recording, tag):
    ct = ContentType.objects.get_for_model(Recording, for_concrete_model=False)
    TaggedItem.objects.create(tag=tag, content_type=ct, object_id=str(recording.pk))


def _browse(client, dataset, query=""):
    response = client.get(f"{DATASETS}/{dataset.object_hash}/browse/{query}")
    assert response.status_code == 200, response.content
    return response.json()


def _names(page):
    return [row["display_name"] for row in page["items"]]


def _counts(facet):
    return {entry["value"]: entry["count"] for entry in facet}


@pytest.fixture
def ungated(user):
    """An ungated dataset of six recordings: modality, age and site values, and one tag on three of them."""
    dataset = Dataset.objects.create(author=user, name="open")
    DatasetMeta.objects.create(dataset=dataset, subject_schema=[AGE, SITE])
    tag = baker.make(Tag, author=user, name="sleep", curated=True)
    specs = [
        ("eeg", "0-17", "a", True),
        ("eeg", "0-17", "b", True),
        ("eeg", "18-64", "a", False),
        ("ecg", "18-64", "b", True),
        ("ecg", "65+", "a", False),
        ("emg", None, None, False),
    ]
    for index, (modality, age, site, tagged) in enumerate(specs):
        recording = _recording(user, index, modality=modality)
        values = {key: value for key, value in (("age", age), ("site", site)) if value}
        _member(dataset, recording, **values)
        if tagged:
            _tag(recording, tag)
    return dataset, tag


class TestFilters:
    def test_modality_matches_any_of_its_values(self, user, ungated):
        dataset, _ = ungated
        page = _browse(_client(user), dataset, "?modality=ecg&modality=emg")
        assert page["count"] == 3
        assert _names(page) == ["rec 03", "rec 04", "rec 05"]

    def test_a_subject_field_matches_any_of_its_choices(self, user, ungated):
        dataset, _ = ungated
        page = _browse(_client(user), dataset, "?subject.age=0-17&subject.age=65%2B")
        assert _names(page) == ["rec 00", "rec 01", "rec 04"]

    def test_dimensions_combine_with_and(self, user, ungated):
        dataset, tag = ungated
        page = _browse(_client(user), dataset, f"?modality=eeg&subject.site=b&tag={tag.pk}")
        assert _names(page) == ["rec 01"]

    def test_every_tag_must_be_present(self, user, ungated):
        dataset, tag = ungated
        other = baker.make(Tag, author=user, name="other")
        page = _browse(_client(user), dataset, f"?tag={tag.pk}&tag={other.pk}")
        assert page["count"] == 0 and page["items"] == []

    def test_sorting_and_paging_apply_to_the_matching_rows(self, user, ungated):
        dataset, _ = ungated
        page = _browse(_client(user), dataset, "?modality=eeg&sort=-name&limit=2&offset=1")
        assert page["count"] == 3
        assert _names(page) == ["rec 01", "rec 00"]

    @pytest.mark.parametrize(
        "query", ["?subject.unknown=a", "?subject.age=99", "?tag=abc"], ids=["field", "choice", "tag-id"]
    )
    def test_an_unknown_filter_is_refused(self, user, ungated, query):
        dataset, _ = ungated
        response = _client(user).get(f"{DATASETS}/{dataset.object_hash}/browse/{query}")
        assert response.status_code == 400

    def test_a_reader_cannot_filter_on_a_field_they_are_not_shown(self, user, make_user):
        reader = make_user()
        dataset = Dataset.objects.create(author=user, name="pool", release_gated=True)
        DatasetMeta.objects.create(dataset=dataset, subject_schema=[AGE, SITE])
        items = [_member(dataset, _recording(user, index), site="a") for index in range(3)]
        _release(dataset, *items)
        _grant(dataset, user, target=reader)
        response = _client(reader).get(f"{DATASETS}/{dataset.object_hash}/browse/?subject.site=a")
        assert response.status_code == 400, "site is not displayed, so its counts would describe hidden values"
        assert "site" not in _browse(_client(reader), dataset)["facets"]["subject"]

    def test_a_share_token_caller_cannot_filter_by_tag(self, user, ungated):
        dataset, tag = ungated
        _grant(dataset, user, token="facet-token")
        response = Client().get(f"{DATASETS}/{dataset.object_hash}/browse/?share_token=facet-token&tag={tag.pk}")
        assert response.status_code == 400
        page = _browse(Client(), dataset, "?share_token=facet-token")
        assert page["facets"]["tags"] == []


class TestFacets:
    def test_unfiltered_counts(self, user, ungated):
        dataset, tag = ungated
        facets = _browse(_client(user), dataset)["facets"]
        assert _counts(facets["modality"]) == {"ecg": 2, "eeg": 3, "emg": 1}
        assert _counts(facets["subject"]["age"]) == {"0-17": 2, "18-64": 2, "65+": 1}
        assert [entry["value"] for entry in facets["subject"]["age"]] == AGE["choices"], "in declared order"
        assert facets["tags"] == [{"id": tag.pk, "name": "sleep", "curated": True, "count": 3}]

    def test_a_dimension_is_counted_under_the_other_filters_only(self, user, ungated):
        dataset, _ = ungated
        facets = _browse(_client(user), dataset, "?modality=eeg")["facets"]
        assert _counts(facets["modality"]) == {"ecg": 2, "eeg": 3, "emg": 1}, "its own filter is left out"
        assert _counts(facets["subject"]["age"]) == {"0-17": 2, "18-64": 1, "65+": 0}

    def test_tags_are_counted_under_every_filter(self, user, ungated):
        dataset, tag = ungated
        facets = _browse(_client(user), dataset, "?modality=ecg")["facets"]
        assert facets["tags"][0]["count"] == 1

    def test_an_ungated_dataset_suppresses_nothing(self, user, make_user, ungated):
        dataset, _ = ungated
        reader = make_user()
        _grant(dataset, user, target=reader)
        page = _browse(_client(reader), dataset, "?modality=emg")
        assert page["suppression_k"] is None
        assert page["count"] == 1 and _names(page) == ["rec 05"]


@pytest.fixture
def gated(user, make_user):
    """A released gated dataset displaying the age band: five members 0-17, four 18-64, one 65+, k = 3 recorded."""
    reader = make_user()
    dataset = Dataset.objects.create(author=user, name="pool", release_gated=True)
    DatasetMeta.objects.create(dataset=dataset, subject_schema=[AGE])
    tag = baker.make(Tag, author=user, name="sleep", curated=True)
    bands = ["0-17"] * 5 + ["18-64"] * 4 + ["65+"]
    items = []
    for index, band in enumerate(bands):
        recording = _recording(user, index, modality="ecg" if index == 9 else "eeg")
        items.append(_member(dataset, recording, age=band))
        if index in (0, 5):
            _tag(recording, tag)
    release = _release(dataset, *items)
    release.k = 3
    release.save(update_fields=["k"])
    _grant(dataset, user, target=reader)
    return dataset, reader, tag


class TestSuppression:
    def test_counts_below_k_are_withheld_from_a_reader(self, gated):
        dataset, reader, tag = gated
        page = _browse(_client(reader), dataset)
        assert page["suppression_k"] == 3
        assert page["count"] == 10, "the unfiltered count is the listing's, which the reader pages through anyway"
        assert _counts(page["facets"]["modality"]) == {"ecg": None, "eeg": None}
        assert page["facets"]["tags"][0]["count"] is None

    def test_a_single_suppressed_value_takes_the_smallest_shown_one_with_it(self, gated):
        dataset, reader, _ = gated
        ages = _counts(_browse(_client(reader), dataset)["facets"]["subject"]["age"])
        assert ages == {"0-17": 5, "18-64": None, "65+": None}, "10 less 5 less 4 would name the 65+ count"

    def test_zero_is_not_suppressed(self, gated):
        dataset, reader, _ = gated
        ages = _counts(_browse(_client(reader), dataset, "?modality=ecg")["facets"]["subject"]["age"])
        assert ages["0-17"] == 0 and ages["18-64"] == 0

    def test_a_filtered_page_matching_fewer_than_k_serves_no_rows(self, gated):
        dataset, reader, tag = gated
        page = _browse(_client(reader), dataset, f"?tag={tag.pk}")
        assert page["count"] is None and page["items"] == []
        activity = Activity.objects.filter(verb="library.dataset.recording.list").latest("created_at")
        assert activity.metadata["count_suppressed"] is True
        assert activity.metadata["filters"] == ["tag"]

    def test_a_filtered_page_matching_k_or_more_is_served(self, gated):
        dataset, reader, _ = gated
        page = _browse(_client(reader), dataset, "?subject.age=18-64")
        assert page["count"] == 4 and len(page["items"]) == 4

    def test_managers_see_every_count(self, user, gated):
        dataset, _, tag = gated
        page = _browse(_client(user), dataset, f"?tag={tag.pk}")
        assert page["suppression_k"] is None
        assert page["count"] == 2 and len(page["items"]) == 2


class TestSuppressionK:
    def _dataset(self, user, **fields):
        return Dataset.objects.create(author=user, name="pool", release_gated=True, **fields)

    def test_the_largest_k_any_release_recorded_class_k_included(self, user):
        from library.browse import suppression_k

        dataset = self._dataset(user)
        baker.make("library.DatasetRelease", dataset=dataset, released_on=date(2026, 9, 1), k=3)
        baker.make("library.DatasetRelease", dataset=dataset, released_on=date(2026, 10, 1), k=4, class_k={"x": 7})
        assert suppression_k(dataset, manager=False) == 7

    def test_the_pool_profile_without_a_recorded_k(self, user, monkeypatch):
        from library.browse import suppression_k
        from recordings import submissions

        monkeypatch.setitem(submissions._PROFILES, "facet-test", submissions.IngestProfile(key="facet-test", k=6))
        dataset = self._dataset(user, submission_profile="facet-test")
        assert suppression_k(dataset, manager=False) == 6

    def test_the_fallback_without_either(self, user):
        from library.browse import suppression_k

        assert suppression_k(self._dataset(user), manager=False) == FALLBACK_SUPPRESSION_K

    def test_none_for_a_manager_or_an_ungated_dataset(self, user):
        from library.browse import suppression_k

        assert suppression_k(self._dataset(user), manager=True) is None
        assert suppression_k(Dataset.objects.create(author=user, name="open"), manager=False) is None


class TestCatalogue:
    def _released(self, author, name, count=2, **meta):
        dataset = Dataset.objects.create(author=author, name=name, description=f"{name} text", release_gated=True)
        DatasetMeta.objects.create(dataset=dataset, subject_schema=[AGE, SITE], **meta)
        items = [_member(dataset, _recording(author, len(name) * 10 + index)) for index in range(count)]
        _release(dataset, *items, on=date(2026, 10, 14))
        return dataset

    def test_lists_readable_released_datasets_by_name(self, user, make_user):
        reader = make_user()
        second = self._released(user, "b second", license_spdx="CC-BY-4.0")
        first = self._released(user, "a first")
        self._released(user, "c unshared")
        for dataset in (first, second):
            _grant(dataset, user, target=reader)
        entries = _client(reader).get(CATALOGUE).json()
        assert [entry["name"] for entry in entries] == ["a first", "b second"]
        entry = entries[1]
        assert set(entry) == {
            "object_hash",
            "name",
            "description",
            "license_spdx",
            "license_url",
            "recording_count",
            "modalities",
            "latest_release",
            "subject_fields",
        }
        assert entry["object_hash"] == second.object_hash
        assert entry["license_spdx"] == "CC-BY-4.0" and entry["license_url"] is None
        assert entry["recording_count"] == 2 and entry["modalities"] == ["eeg"]
        assert entry["latest_release"].startswith("2026-10-01")
        assert entry["subject_fields"] == ["Age band"], "the reader is shown the displayed field only"

    def test_the_author_is_shown_every_field_and_only_released_members_are_counted(self, user):
        dataset = self._released(user, "pool")
        _member(dataset, _recording(user, 90))
        (entry,) = _client(user).get(CATALOGUE).json()
        assert entry["recording_count"] == 2
        assert entry["subject_fields"] == ["Age band", "Site"]

    def test_unreleased_ungated_and_emptied_datasets_are_left_out(self, user):
        Dataset.objects.create(author=user, name="open")
        unreleased = Dataset.objects.create(author=user, name="waiting", release_gated=True)
        _member(unreleased, _recording(user, 80))
        withdrawn = self._released(user, "withdrawn", count=1)
        Recording.objects.filter(pk__in=[int(i.object_id) for i in withdrawn.items.all()]).update(
            status=Recording.Status.FAILED
        )
        assert _client(user).get(CATALOGUE).json() == []

    def test_requires_an_account_and_is_audited(self, user):
        assert Client().get(CATALOGUE).status_code == 401
        self._released(user, "pool")
        _client(user).get(CATALOGUE)
        activity = Activity.objects.filter(verb="library.catalogue.list").latest("created_at")
        assert activity.metadata == {"limit": 50, "offset": 0, "returned_count": 1}


class TestTagReach:
    def test_a_member_the_dataset_author_cannot_read_contributes_no_tag(self, user, make_user):
        owner = make_user()
        dataset = Dataset.objects.create(author=user, name="mine")
        theirs = _recording(owner, 1)
        _member(dataset, theirs)
        _member(dataset, _recording(user, 2))
        _tag(theirs, baker.make(Tag, author=owner, name="private label"))
        page = _browse(_client(user), dataset)
        assert page["facets"]["tags"] == [], "the row carries no tag of a recording the author cannot read"
