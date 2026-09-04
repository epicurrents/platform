"""Tests for the Code field-shape changes: TextField value and JSON-typed meta.

Two regressions pinned here. ``Code.value`` was ``CharField(max_length=128)``, too narrow for a realistic
HED annotation, and is now ``TextField``. ``CodeIn.meta`` / ``CodePatch.meta`` were typed ``str | None``
against a ``JSONField``, so the API could only ever write a JSON *string* into a JSON field — the object
round-trip below could not have passed before the fix. The PATCH tests pin the absent-vs-explicit-null
distinction ``exclude_unset`` provides: null clears the field, an absent key leaves it unchanged.
"""

import pytest
from django.contrib.contenttypes.models import ContentType
from model_bakery import baker

from annotations.models import Code, Event
from conftest import patch_json, post_json

CODES_URL = "/annotations/api/v1/codes/"


@pytest.fixture
def event(db, user):
    recording = baker.make("recordings.Recording", author=user)
    return Event.objects.create(
        author=user,
        target_content_type=ContentType.objects.get_for_model(recording, for_concrete_model=False),
        target_object_id=str(recording.pk),
        object_hash="A" * 32,
        name="test-event",
        timestamp=1.0,
    )


def _payload(event, **kwargs):
    defaults = {
        "content_type_id": ContentType.objects.get_for_model(Event).pk,
        "object_id": str(event.pk),
        "standard": "hed",
        "value": "Seizure",
    }
    defaults.update(kwargs)
    return defaults


@pytest.mark.django_db
class TestCodeValueWidth:
    def test_long_value_round_trips_through_the_api(self, auth_client, event):
        c, _user = auth_client
        # A grouped HED seizure description runs several hundred characters —
        # well past the old CharField(max_length=128).
        long_value = "(" + ", ".join(f"Property/Sub-property-{i}/Value-{i}" for i in range(20)) + ")"
        assert len(long_value) > 128
        resp = post_json(c, CODES_URL, _payload(event, value=long_value))
        assert resp.status_code == 200, resp.content
        assert resp.json()["value"] == long_value
        assert Code.objects.get(pk=resp.json()["id"]).value == long_value


@pytest.mark.django_db
class TestCodeMetaJson:
    def test_meta_object_round_trips(self, auth_client, event):
        c, _user = auth_client
        meta = {"library": "score", "version": "1.1.0", "confidence": 0.9, "tags": ["a", "b"]}
        resp = post_json(c, CODES_URL, _payload(event, meta=meta))
        assert resp.status_code == 200, resp.content
        assert resp.json()["meta"] == meta
        assert Code.objects.get(pk=resp.json()["id"]).meta == meta

    def test_meta_list_round_trips(self, auth_client, event):
        c, _user = auth_client
        resp = post_json(c, CODES_URL, _payload(event, meta=[1, 2, 3]))
        assert resp.status_code == 200, resp.content
        assert Code.objects.get(pk=resp.json()["id"]).meta == [1, 2, 3]

    def test_meta_string_still_accepted(self, auth_client, event):
        c, _user = auth_client
        resp = post_json(c, CODES_URL, _payload(event, meta="free text"))
        assert resp.status_code == 200, resp.content
        assert Code.objects.get(pk=resp.json()["id"]).meta == "free text"

    def test_patch_meta_object(self, auth_client, event):
        c, _user = auth_client
        code = Code.objects.create(
            content_type=ContentType.objects.get_for_model(Event),
            object_id=str(event.pk),
            standard="hed",
            value="Seizure",
            meta="old",
        )
        resp = patch_json(c, f"{CODES_URL}{code.pk}", {"meta": {"new": True}})
        assert resp.status_code == 200, resp.content
        code.refresh_from_db()
        assert code.meta == {"new": True}

    def test_patch_explicit_null_clears_meta(self, auth_client, event):
        c, _user = auth_client
        code = Code.objects.create(
            content_type=ContentType.objects.get_for_model(Event),
            object_id=str(event.pk),
            standard="hed",
            value="Seizure",
            meta={"keep": False},
        )
        resp = patch_json(c, f"{CODES_URL}{code.pk}", {"meta": None})
        assert resp.status_code == 200, resp.content
        code.refresh_from_db()
        assert code.meta is None

    def test_patch_explicit_null_value_returns_422(self, auth_client, event):
        # value and standard are non-nullable; an explicit null must be a 422,
        # not a None handed to the validator and the database.
        c, _user = auth_client
        code = Code.objects.create(
            content_type=ContentType.objects.get_for_model(Event),
            object_id=str(event.pk),
            standard="hed",
            value="Seizure",
        )
        resp = patch_json(c, f"{CODES_URL}{code.pk}", {"value": None})
        assert resp.status_code == 422
        code.refresh_from_db()
        assert code.value == "Seizure"

    def test_patch_explicit_null_standard_returns_422(self, auth_client, event):
        c, _user = auth_client
        code = Code.objects.create(
            content_type=ContentType.objects.get_for_model(Event),
            object_id=str(event.pk),
            standard="hed",
            value="Seizure",
        )
        resp = patch_json(c, f"{CODES_URL}{code.pk}", {"standard": None})
        assert resp.status_code == 422
        code.refresh_from_db()
        assert code.standard == "hed"

    def test_patch_without_meta_leaves_it_unchanged(self, auth_client, event):
        c, _user = auth_client
        code = Code.objects.create(
            content_type=ContentType.objects.get_for_model(Event),
            object_id=str(event.pk),
            standard="hed",
            value="Seizure",
            meta={"keep": True},
        )
        resp = patch_json(c, f"{CODES_URL}{code.pk}", {"value": "Seizure-cluster"})
        assert resp.status_code == 200, resp.content
        code.refresh_from_db()
        assert code.meta == {"keep": True}
        assert code.value == "Seizure-cluster"
