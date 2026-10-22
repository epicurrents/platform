"""Tests for codes written inline with an event, interruption or label.

A client addresses an annotation by its hash and never learns its primary key, which the ``/codes`` endpoints name
their parent by. The inline ``codes`` of a write is how a client attaches codes: keyed by standard, each validated
against its registered vocabulary before anything is written, and on a patch replacing the set.
"""

import secrets

import pytest
from django.contrib.contenttypes.models import ContentType
from django.test import override_settings
from model_bakery import baker

from annotations.models import Code, Event, Interruption, Label
from annotations.vocabularies import register_vocabulary, unregister_vocabulary
from conftest import patch_json, post_json

EVENTS_URL = "/annotations/api/v1/events/"
INTERRUPTIONS_URL = "/annotations/api/v1/interruptions/"
LABELS_URL = "/annotations/api/v1/labels/"
PIN = {"schema": "1"}


def _validator(value, meta):
    if value not in {"Spike", "Seizure"}:
        raise ValueError(f"{value!r} is not a test term")
    if meta != PIN:
        raise ValueError("meta must be the pin")


@pytest.fixture
def pinned_vocabulary():
    register_vocabulary("test-pinned", label="Pinned", validator=_validator, confined=True)
    yield
    unregister_vocabulary("test-pinned")


@pytest.fixture
def recording(db, user):
    return baker.make("recordings.Recording", author=user)


def _target(recording, **kwargs):
    ct = ContentType.objects.get_for_model(recording, for_concrete_model=False)
    return {"target_content_type_id": ct.pk, "target_object_id": str(recording.pk), "object_hash": "C" * 32, **kwargs}


def _codes(row):
    ct = ContentType.objects.get_for_model(type(row))
    return {c.standard: (c.value, c.meta) for c in Code.objects.filter(content_type=ct, object_id=str(row.pk))}


WRITES = [
    (EVENTS_URL, Event, {"name": "spike", "timestamp": 1.0}),
    (INTERRUPTIONS_URL, Interruption, {"timestamp": 1.0, "duration": 2.0}),
    (LABELS_URL, Label, {"name": "finding"}),
]


@pytest.mark.django_db
@pytest.mark.parametrize(("url", "model", "fields"), WRITES)
class TestCreate:
    def test_codes_are_written_with_the_row_and_served_back(
        self, auth_client, recording, pinned_vocabulary, url, model, fields
    ):
        c, _user = auth_client
        payload = _target(recording, **fields, codes={"test-pinned": {"value": "Spike", "meta": PIN}})
        resp = post_json(c, url, payload)
        assert resp.status_code == 200, resp.content
        row = model.objects.get(object_hash="C" * 32)
        assert _codes(row) == {"test-pinned": ("Spike", PIN)}
        assert [(code["standard"], code["value"]) for code in resp.json()["codes"]] == [("test-pinned", "Spike")]

    def test_a_refused_code_writes_nothing(self, auth_client, recording, pinned_vocabulary, url, model, fields):
        c, _user = auth_client
        payload = _target(recording, **fields, codes={"test-pinned": {"value": "Spike", "meta": None}})
        resp = post_json(c, url, payload)
        assert resp.status_code == 422
        assert "test-pinned" in resp.json()["detail"]
        assert not model.objects.exists()
        assert not Code.objects.exists()

    @override_settings(ANNOTATION_CODE_STRICT_VOCABULARY=True)
    def test_strict_mode_refuses_an_unregistered_standard(self, auth_client, recording, url, model, fields):
        c, _user = auth_client
        payload = _target(recording, **fields, codes={"nobody-registered-this": {"value": "x"}})
        assert post_json(c, url, payload).status_code == 422
        assert not model.objects.exists()

    def test_a_standard_longer_than_the_column_is_refused(self, auth_client, recording, url, model, fields):
        c, _user = auth_client
        payload = _target(recording, **fields, codes={"x" * 65: {"value": "x"}})
        assert post_json(c, url, payload).status_code == 422


@pytest.mark.django_db
@pytest.mark.parametrize(("url", "model", "fields"), WRITES)
class TestPatch:
    def _create(self, client, recording, url, fields, codes):
        resp = post_json(client, url, _target(recording, **fields, codes=codes))
        assert resp.status_code == 200, resp.content

    def test_codes_replace_the_set(self, auth_client, recording, pinned_vocabulary, url, model, fields):
        c, _user = auth_client
        self._create(
            c, recording, url, fields, {"test-pinned": {"value": "Spike", "meta": PIN}, "other": {"value": "a"}}
        )
        resp = patch_json(c, f"{url}{'C' * 32}", {"codes": {"test-pinned": {"value": "Seizure", "meta": PIN}}})
        assert resp.status_code == 200, resp.content
        row = model.objects.get(object_hash="C" * 32)
        assert _codes(row) == {"test-pinned": ("Seizure", PIN)}
        assert [code["value"] for code in resp.json()["codes"]] == ["Seizure"]

    def test_an_empty_set_clears_and_an_absent_one_leaves(
        self, auth_client, recording, pinned_vocabulary, url, model, fields
    ):
        c, _user = auth_client
        self._create(c, recording, url, fields, {"test-pinned": {"value": "Spike", "meta": PIN}})
        row = model.objects.get(object_hash="C" * 32)
        assert (
            patch_json(
                c, f"{url}{'C' * 32}", {"timestamp": 3.0} if "timestamp" in fields else {"name": "n"}
            ).status_code
            == 200
        )
        assert _codes(row) == {"test-pinned": ("Spike", PIN)}
        assert patch_json(c, f"{url}{'C' * 32}", {"codes": {}}).status_code == 200
        assert _codes(row) == {}

    def test_null_is_refused(self, auth_client, recording, url, model, fields):
        c, _user = auth_client
        self._create(c, recording, url, fields, None)
        assert patch_json(c, f"{url}{'C' * 32}", {"codes": None}).status_code == 422

    def test_a_refused_code_changes_nothing(self, auth_client, recording, pinned_vocabulary, url, model, fields):
        c, _user = auth_client
        self._create(c, recording, url, fields, {"test-pinned": {"value": "Spike", "meta": PIN}})
        resp = patch_json(c, f"{url}{'C' * 32}", {"codes": {"test-pinned": {"value": "Nope", "meta": PIN}}})
        assert resp.status_code == 422
        assert _codes(model.objects.get(object_hash="C" * 32)) == {"test-pinned": ("Spike", PIN)}

    def test_the_audit_row_names_codes_as_updated(self, auth_client, recording, pinned_vocabulary, url, model, fields):
        from activity.models import Activity

        c, _user = auth_client
        self._create(c, recording, url, fields, None)
        patch_json(c, f"{url}{'C' * 32}", {"codes": {"test-pinned": {"value": "Spike", "meta": PIN}}})
        row = Activity.objects.filter(verb__endswith=".update").latest("pk")
        assert row.metadata["fields_updated"] == ["codes"]

    def test_another_users_row_is_refused(self, client, make_user, recording, pinned_vocabulary, url, model, fields):
        owner = recording.author
        client.force_login(owner)
        self._create(client, recording, url, fields, None)
        client.force_login(make_user())
        resp = patch_json(client, f"{url}{'C' * 32}", {"codes": {"test-pinned": {"value": "Spike", "meta": PIN}}})
        assert resp.status_code in (403, 404)
        assert not Code.objects.exists()


def _hash(recording):
    """The recording's public hash, giving it a stored name of the form ingest writes."""
    if "." not in recording.stored_name:
        recording.stored_name = f"{secrets.token_hex(16).upper()}.edf"
        recording.save(update_fields=["stored_name"])
    return recording.stored_name.split(".")[0]


@pytest.mark.django_db
class TestTargetByHash:
    """A client names a recording by its hash, since no response carries its primary key."""

    def test_an_event_is_created_and_listed_against_the_recording_hash(self, auth_client, recording):
        c, _user = auth_client
        ct = ContentType.objects.get_for_model(recording, for_concrete_model=False)
        payload = _target(recording, name="spike", timestamp=1.0)
        payload["target_object_id"] = _hash(recording).lower()
        resp = post_json(c, EVENTS_URL, payload)
        assert resp.status_code == 200, resp.content
        event = Event.objects.get(object_hash="C" * 32)
        assert event.target_object_id == str(recording.pk)
        from epicurrents.models import AccessRight

        AccessRight.objects.create(
            content_type=ct,
            object_id=str(recording.pk),
            access_giver=recording.author,
            access_target=recording.author,
            can_read=True,
        )
        listed = c.get(f"{EVENTS_URL}?target_content_type_id={ct.pk}&target_object_id={_hash(recording)}")
        assert listed.status_code == 200
        assert [row["object_hash"] for row in listed.json()] == ["C" * 32]

    def test_the_primary_key_is_still_accepted(self, auth_client, recording):
        c, _user = auth_client
        resp = post_json(c, EVENTS_URL, _target(recording, name="spike", timestamp=1.0))
        assert resp.status_code == 200, resp.content
        assert Event.objects.get().target_object_id == str(recording.pk)

    @pytest.mark.parametrize("object_id", ["F" * 32, "not-a-pk", ""])
    def test_an_unknown_target_answers_404(self, auth_client, recording, object_id):
        c, _user = auth_client
        payload = _target(recording, name="spike", timestamp=1.0)
        payload["target_object_id"] = object_id
        assert post_json(c, EVENTS_URL, payload).status_code == 404

    def test_a_dataset_resolves_by_its_object_hash(self, user):
        from annotations.api.v1.ninja import _target_lookup
        from library.models import Dataset

        dataset = Dataset.objects.create(author=user, name="D")
        assert Dataset.objects.filter(**_target_lookup(Dataset, dataset.object_hash.lower())).get() == dataset


@pytest.mark.django_db
class TestHiddenTargets:
    """A recording hidden from the caller answers as one that does not exist, so its hash confirms nothing."""

    def _grantee(self, client, make_user, recording):
        from epicurrents.models import AccessRight

        reader = make_user()
        AccessRight.objects.create(
            content_type=ContentType.objects.get_for_model(recording, for_concrete_model=False),
            object_id=str(recording.pk),
            access_giver=recording.author,
            access_target=reader,
            can_read=True,
        )
        client.force_login(reader)
        return client

    def _list(self, client, recording):
        ct = ContentType.objects.get_for_model(recording, for_concrete_model=False)
        return client.get(f"{EVENTS_URL}?target_content_type_id={ct.pk}&target_object_id={_hash(recording)}")

    def test_a_failed_recording_answers_like_an_unknown_one(self, client, make_user, recording):
        from recordings.models import Recording

        Recording.objects.filter(pk=recording.pk).update(status=Recording.Status.FAILED)
        recording.refresh_from_db()
        response = self._list(self._grantee(client, make_user, recording), recording)
        assert response.status_code == 404
        assert response.json() == {"detail": "Target object not found"}

    def test_a_trashed_recording_answers_404_to_its_author_too(self, auth_client, recording):
        from django.utils import timezone

        c, _user = auth_client
        recording.deleted_at = timezone.now()
        recording.save(update_fields=["deleted_at"])
        assert self._list(c, recording).status_code == 404
        payload = _target(recording, name="spike", timestamp=1.0)
        payload["target_object_id"] = _hash(recording)
        assert post_json(c, EVENTS_URL, payload).status_code == 404
