"""Tests for a dataset's preload flag, which has the viewer load every member when the dataset is opened.

The viewer otherwise lists members and loads each when it is opened, because every loaded recording holds a reader
and a browser tab gives out at a few hundred. The flag is for a dataset whose workflow spans its members, and the
size limit is what keeps it from bringing that failure back.
"""

import pytest
from django.contrib.contenttypes.models import ContentType
from django.test import override_settings
from model_bakery import baker

from activity.models import ObjectChangeLog
from conftest import patch_json
from library.models import Dataset, DatasetItem

DATASETS_URL = "/api/v1/library/datasets/"


def _add_members(dataset, author, count):
    content_type = None
    for _ in range(count):
        recording = baker.make("recordings.Recording", author=author, status="READY")
        content_type = content_type or ContentType.objects.get_for_model(recording, for_concrete_model=False)
        DatasetItem.objects.create(dataset=dataset, content_type=content_type, object_id=str(recording.pk))


@pytest.fixture
def dataset(db, user):
    return Dataset.objects.create(author=user, name="teaching-set")


@pytest.mark.django_db
class TestPreloadFlag:
    def test_off_by_default_and_served(self, auth_client, dataset):
        c, _user = auth_client
        resp = c.get(f"{DATASETS_URL}{dataset.object_hash}/")
        assert resp.status_code == 200, resp.content
        assert resp.json()["preload_members"] is False

    def test_turned_on_and_off(self, auth_client, dataset):
        c, user = auth_client
        _add_members(dataset, user, 3)
        resp = patch_json(c, f"{DATASETS_URL}{dataset.object_hash}/", {"preload_members": True})
        assert resp.status_code == 200, resp.content
        assert resp.json()["preload_members"] is True
        dataset.refresh_from_db()
        assert dataset.preload_members is True
        resp = patch_json(c, f"{DATASETS_URL}{dataset.object_hash}/", {"preload_members": False})
        assert resp.status_code == 200, resp.content
        dataset.refresh_from_db()
        assert dataset.preload_members is False

    @override_settings(LIBRARY_DATASET_PRELOAD_LIMIT=2)
    def test_refused_above_the_limit(self, auth_client, dataset):
        c, user = auth_client
        _add_members(dataset, user, 3)
        resp = patch_json(c, f"{DATASETS_URL}{dataset.object_hash}/", {"preload_members": True, "name": "renamed"})
        assert resp.status_code == 409, resp.content
        assert "more than 2 members" in resp.json()["detail"]
        dataset.refresh_from_db()
        assert dataset.preload_members is False
        assert dataset.name == "teaching-set"

    @override_settings(LIBRARY_DATASET_PRELOAD_LIMIT=2)
    def test_allowed_at_the_limit(self, auth_client, dataset):
        c, user = auth_client
        _add_members(dataset, user, 2)
        resp = patch_json(c, f"{DATASETS_URL}{dataset.object_hash}/", {"preload_members": True})
        assert resp.status_code == 200, resp.content

    @override_settings(LIBRARY_DATASET_PRELOAD_LIMIT=2)
    def test_turning_off_is_never_refused(self, auth_client, dataset):
        c, user = auth_client
        Dataset.objects.filter(pk=dataset.pk).update(preload_members=True)
        _add_members(dataset, user, 3)
        resp = patch_json(c, f"{DATASETS_URL}{dataset.object_hash}/", {"preload_members": False})
        assert resp.status_code == 200, resp.content

    def test_audited(self, auth_client, dataset):
        c, _user = auth_client
        patch_json(c, f"{DATASETS_URL}{dataset.object_hash}/", {"preload_members": True})
        change = ObjectChangeLog.objects.filter(
            content_type=ContentType.objects.get_for_model(Dataset), object_id=str(dataset.pk)
        ).latest("pk")
        assert "preload_members" in change.changes
