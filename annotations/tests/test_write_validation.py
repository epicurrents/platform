"""Tests for the model validation of event, interruption and label writes.

A write the model refuses is answered with 422 and the model's message rather than a 500, and an event may have no
name of its own, as one named by its codes does.
"""

import pytest
from django.contrib.contenttypes.models import ContentType
from model_bakery import baker

from annotations.models import Event, Label
from conftest import patch_json, post_json

EVENTS_URL = "/annotations/api/v1/events/"
LABELS_URL = "/annotations/api/v1/labels/"


@pytest.fixture
def recording(db, user):
    return baker.make("recordings.Recording", author=user)


def _target(recording, **kwargs):
    ct = ContentType.objects.get_for_model(recording, for_concrete_model=False)
    return {"target_content_type_id": ct.pk, "target_object_id": str(recording.pk), "object_hash": "C" * 32, **kwargs}


@pytest.mark.django_db
class TestEventName:
    def test_an_event_is_created_without_a_name(self, auth_client, recording):
        c, _user = auth_client
        resp = post_json(c, EVENTS_URL, _target(recording, name="", timestamp=1.0))
        assert resp.status_code == 200, resp.content
        assert Event.objects.get(object_hash="C" * 32).name == ""

    def test_a_patch_clears_an_events_name(self, auth_client, recording):
        c, _user = auth_client
        post_json(c, EVENTS_URL, _target(recording, name="event", timestamp=1.0))
        resp = patch_json(c, f"{EVENTS_URL}{'C' * 32}", {"name": ""})
        assert resp.status_code == 200, resp.content
        assert Event.objects.get(object_hash="C" * 32).name == ""


@pytest.mark.django_db
class TestRefusal:
    def test_a_create_the_model_refuses_is_a_422_naming_the_field(self, auth_client, recording):
        c, _user = auth_client
        resp = post_json(c, EVENTS_URL, _target(recording, name="x" * 256, timestamp=1.0))
        assert resp.status_code == 422
        assert resp.json()["detail"].startswith("name:")
        assert not Event.objects.exists()

    def test_a_patch_the_model_refuses_is_a_422_and_changes_nothing(self, auth_client, recording):
        c, _user = auth_client
        post_json(c, LABELS_URL, _target(recording, name="finding"))
        resp = patch_json(c, f"{LABELS_URL}{'C' * 32}", {"name": ""})
        assert resp.status_code == 422
        assert "name" in resp.json()["detail"]
        assert Label.objects.get(object_hash="C" * 32).name == "finding"
