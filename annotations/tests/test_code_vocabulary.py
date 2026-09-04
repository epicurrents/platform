"""Contract tests for the annotation vocabulary registry.

Core ships the mechanism with zero vocabularies, so the vocabulary registered here — inside the test
suite, never in production code — is what proves the contract: a registered validator gates API writes
for its standard, an unregistered standard passes untouched by default, and the strict setting flips that
default. Enforcement is at the API layer only; the server-side ORM bypass at the end is deliberate
behaviour, not a gap.
"""

import pytest
from django.contrib.contenttypes.models import ContentType
from django.test import override_settings
from model_bakery import baker

from annotations.models import Code, Event
from annotations.vocabularies import (
    register_vocabulary,
    registered_vocabularies,
    unregister_vocabulary,
    validate_code,
)
from conftest import patch_json, post_json

CODES_URL = "/annotations/api/v1/codes/"


def _terms_validator(value, meta):
    """Toy vocabulary: three terms; meta, when a dict, must not claim confidence > 1."""
    if value not in {"Seizure", "Spike", "Artifact"}:
        raise ValueError(f"term {value!r} is not in the test vocabulary")
    if isinstance(meta, dict) and meta.get("confidence", 0) > 1:
        raise ValueError("confidence must be at most 1")


@pytest.fixture
def test_vocabulary():
    register_vocabulary("test-vocab", label="Test vocabulary", validator=_terms_validator, version="1.0")
    yield
    unregister_vocabulary("test-vocab")


@pytest.fixture
def event(db, user):
    recording = baker.make("recordings.Recording", author=user)
    return Event.objects.create(
        author=user,
        target_content_type=ContentType.objects.get_for_model(recording, for_concrete_model=False),
        target_object_id=str(recording.pk),
        object_hash="B" * 32,
        name="test-event",
        timestamp=1.0,
    )


def _payload(event, **kwargs):
    defaults = {
        "content_type_id": ContentType.objects.get_for_model(Event).pk,
        "object_id": str(event.pk),
        "standard": "test-vocab",
        "value": "Seizure",
    }
    defaults.update(kwargs)
    return defaults


class TestRegistry:
    def test_registration_is_listed_and_removal_unlists(self):
        register_vocabulary("tmp-vocab", label="Temp", validator=_terms_validator)
        try:
            standards = [v.standard for v in registered_vocabularies()]
            assert "tmp-vocab" in standards
        finally:
            unregister_vocabulary("tmp-vocab")
        assert "tmp-vocab" not in [v.standard for v in registered_vocabularies()]

    def test_validate_code_passes_valid_term(self, test_vocabulary):
        validate_code("test-vocab", "Spike", None)

    def test_validate_code_raises_on_invalid_term(self, test_vocabulary):
        with pytest.raises(ValueError, match="not in the test vocabulary"):
            validate_code("test-vocab", "NotATerm", None)

    def test_validator_sees_meta(self, test_vocabulary):
        with pytest.raises(ValueError, match="confidence"):
            validate_code("test-vocab", "Seizure", {"confidence": 2})

    def test_unregistered_standard_passes_by_default(self):
        validate_code("nobody-registered-this", "anything", None)

    @override_settings(ANNOTATION_CODE_STRICT_VOCABULARY=True)
    def test_strict_mode_rejects_unregistered_standard(self):
        with pytest.raises(ValueError, match="unknown coding standard"):
            validate_code("nobody-registered-this", "anything", None)

    @override_settings(ANNOTATION_CODE_STRICT_VOCABULARY=True)
    def test_strict_mode_still_accepts_registered_standard(self, test_vocabulary):
        validate_code("test-vocab", "Artifact", None)


@pytest.mark.django_db
class TestCreateCodeValidation:
    def test_valid_term_is_created(self, auth_client, event, test_vocabulary):
        c, _user = auth_client
        resp = post_json(c, CODES_URL, _payload(event))
        assert resp.status_code == 200, resp.content
        assert Code.objects.filter(pk=resp.json()["id"]).exists()

    def test_invalid_term_returns_422_and_writes_nothing(self, auth_client, event, test_vocabulary):
        c, _user = auth_client
        resp = post_json(c, CODES_URL, _payload(event, value="NotATerm"))
        assert resp.status_code == 422
        assert "not in the test vocabulary" in resp.json()["detail"]
        assert not Code.objects.exists()

    def test_invalid_meta_returns_422(self, auth_client, event, test_vocabulary):
        c, _user = auth_client
        resp = post_json(c, CODES_URL, _payload(event, meta={"confidence": 5}))
        assert resp.status_code == 422
        assert not Code.objects.exists()

    def test_unregistered_standard_is_accepted_by_default(self, auth_client, event):
        c, _user = auth_client
        resp = post_json(c, CODES_URL, _payload(event, standard="epicurrents.example.mark", value="anything"))
        assert resp.status_code == 200, resp.content

    @override_settings(ANNOTATION_CODE_STRICT_VOCABULARY=True)
    def test_strict_mode_rejects_unregistered_standard_at_the_api(self, auth_client, event):
        c, _user = auth_client
        resp = post_json(c, CODES_URL, _payload(event, standard="epicurrents.example.mark", value="anything"))
        assert resp.status_code == 422


@pytest.mark.django_db
class TestUpdateCodeValidation:
    def _code(self, event, **kwargs):
        defaults = {
            "content_type": ContentType.objects.get_for_model(Event),
            "object_id": str(event.pk),
            "standard": "test-vocab",
            "value": "Seizure",
        }
        defaults.update(kwargs)
        return Code.objects.create(**defaults)

    def test_patch_to_valid_term_succeeds(self, auth_client, event, test_vocabulary):
        c, _user = auth_client
        code = self._code(event)
        resp = patch_json(c, f"{CODES_URL}{code.pk}", {"value": "Spike"})
        assert resp.status_code == 200, resp.content
        code.refresh_from_db()
        assert code.value == "Spike"

    def test_patch_to_invalid_term_returns_422_and_saves_nothing(self, auth_client, event, test_vocabulary):
        c, _user = auth_client
        code = self._code(event)
        resp = patch_json(c, f"{CODES_URL}{code.pk}", {"value": "NotATerm"})
        assert resp.status_code == 422
        code.refresh_from_db()
        assert code.value == "Seizure"

    def test_patch_validates_the_combined_row_state(self, auth_client, event, test_vocabulary):
        # Patching only meta must still be validated against the standard and
        # value already on the row, not against defaults.
        c, _user = auth_client
        code = self._code(event)
        resp = patch_json(c, f"{CODES_URL}{code.pk}", {"meta": {"confidence": 5}})
        assert resp.status_code == 422
        code.refresh_from_db()
        assert code.meta is None

    def test_patch_into_a_registered_standard_is_validated(self, auth_client, event, test_vocabulary):
        c, _user = auth_client
        code = self._code(event, standard="epicurrents.example.mark", value="NotATerm")
        resp = patch_json(c, f"{CODES_URL}{code.pk}", {"standard": "test-vocab"})
        assert resp.status_code == 422


@pytest.mark.django_db
class TestServerSideBypassIsDeliberate:
    def test_orm_writes_are_not_validated(self, event, test_vocabulary):
        # Ingest, management commands, and fixtures are the platform's own
        # code; the registry gates the API surface only (see the module
        # docstring in annotations/vocabularies.py).
        code = Code.objects.create(
            content_type=ContentType.objects.get_for_model(Event),
            object_id=str(event.pk),
            standard="test-vocab",
            value="NotATerm",
        )
        assert Code.objects.filter(pk=code.pk).exists()
