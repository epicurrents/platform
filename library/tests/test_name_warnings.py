"""Free-text warnings beside the library's create and rename responses."""

import json

import pytest
from django.contrib.contenttypes.models import ContentType
from model_bakery import baker

from library.models import Collection, Dataset, DatasetFolder, Tag
from recordings.models import Recording

COLLECTIONS = "/api/v1/library/collections/"
DATASETS = "/api/v1/library/datasets/"
TAGS = "/api/v1/library/tags/"


def _post(client, url, body):
    return client.post(url, json.dumps(body), content_type="application/json")


def _patch(client, url, body):
    return client.patch(url, json.dumps(body), content_type="application/json")


def _kinds(payload) -> list[tuple[str, str]]:
    return [(row["field"], row["kind"]) for row in payload["warnings"]]


@pytest.mark.django_db
class TestCollections:
    def test_create_warns_and_still_creates(self, auth_client):
        c, _ = auth_client
        resp = _post(c, COLLECTIONS, {"name": "Doe, Jane", "description": "admitted 2024-03-05"})
        assert resp.status_code == 201
        assert _kinds(resp.json()) == [("name", "person_name"), ("description", "date")]
        assert Collection.objects.filter(name="Doe, Jane").exists()
        assert resp.json()["warnings"][0]["message"].startswith("The name reads as a person's name")

    def test_create_clean_name_has_no_warnings(self, auth_client):
        c, _ = auth_client
        resp = _post(c, COLLECTIONS, {"name": "Sleep study"})
        assert resp.status_code == 201
        assert resp.json()["warnings"] == []

    def test_update_warns_on_the_changed_field_only(self, auth_client):
        c, user = auth_client
        col = baker.make(Collection, author=user, name="Doe, Jane")
        resp = _patch(c, f"{COLLECTIONS}{col.pk}/", {"description": "MRN 123456789"})
        assert resp.status_code == 200
        assert _kinds(resp.json()) == [("description", "digit_run")]

    def test_bulk_rename_warns_on_the_prefix(self, auth_client):
        c, user = auth_client
        col = baker.make(Collection, author=user)
        resp = _post(c, f"{COLLECTIONS}{col.pk}/recordings/bulk-rename", {"prefix": "Jane Doe"})
        assert resp.status_code == 200
        assert _kinds(resp.json()) == [("prefix", "person_name")]


@pytest.mark.django_db
class TestDatasets:
    def test_create_warns(self, auth_client):
        c, _ = auth_client
        resp = _post(c, DATASETS, {"name": "Patient 20240305", "description": ""})
        assert resp.status_code == 201
        assert _kinds(resp.json()) == [("name", "digit_run")]
        assert resp.json()["object_hash"]
        assert Dataset.objects.filter(name="Patient 20240305").exists()

    def test_update_warns(self, auth_client):
        c, user = auth_client
        dataset = Dataset.objects.create(author=user, name="Study")
        resp = _patch(c, f"{DATASETS}{dataset.object_hash}/", {"name": "Doe, J."})
        assert resp.status_code == 200
        assert _kinds(resp.json()) == [("name", "person_name")]

    def test_folder_create_and_rename_warn(self, auth_client):
        c, user = auth_client
        dataset = Dataset.objects.create(author=user, name="Study")
        resp = _post(c, f"{DATASETS}{dataset.object_hash}/folders/", {"name": "5 March 2024"})
        assert resp.status_code == 201
        assert _kinds(resp.json()) == [("name", "date")]
        folder = DatasetFolder.objects.get(pk=resp.json()["id"])
        resp = _patch(c, f"{DATASETS}{dataset.object_hash}/folders/{folder.pk}/", {"name": "Week 1"})
        assert resp.status_code == 200
        assert resp.json()["warnings"] == []
        resp = _patch(c, f"{DATASETS}{dataset.object_hash}/folders/{folder.pk}/", {"position": 2})
        assert resp.status_code == 200
        assert resp.json()["warnings"] == []


@pytest.mark.django_db
class TestTags:
    def test_create_and_update_warn(self, superuser_client):
        c, su = superuser_client
        resp = _post(c, TAGS, {"name": "Jane Doe"})
        assert resp.status_code == 201
        assert _kinds(resp.json()) == [("name", "person_name")]
        tag = Tag.objects.get(pk=resp.json()["id"])
        resp = _patch(c, f"{TAGS}{tag.pk}/", {"description": "born 1985-03-12"})
        assert resp.status_code == 200
        assert _kinds(resp.json()) == [("description", "date")]

    def test_tag_read_surfaces_no_warnings_field(self, superuser_client):
        c, su = superuser_client
        tag = baker.make(Tag, author=su, name="Jane Doe", curated=True)
        resp = c.get(f"{TAGS}{tag.pk}/")
        assert resp.status_code == 200
        assert "warnings" not in resp.json()


@pytest.mark.django_db
def test_recording_ct_is_available_for_bulk_rename(auth_client):
    """Guard for the bulk-rename fixture shape used above: the content type resolves."""
    assert ContentType.objects.get_for_model(Recording, for_concrete_model=False)
