"""Contract tests for the annotation vocabulary registry.

The vocabulary registered here — inside the test suite, never in production code — is what proves the
contract independently of the two core registers in ``annotations.core_vocabularies``: a registered validator gates API writes
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
    get_vocabulary,
    register_vocabulary,
    registered_vocabularies,
    unregister_vocabulary,
    validate_code,
)
from conftest import patch_json, post_json

CODES_URL = "/annotations/api/v1/codes/"
VOCABULARIES_URL = "/annotations/api/v1/vocabularies"


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


def _spaced_validator(value, meta):
    if value not in {"Seizure", "Spike"}:
        raise ValueError(f"term {value!r} is not in the spaced vocabulary")


def _collapse(value):
    """The spaced vocabulary's one spelling: surrounding and repeated whitespace removed."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("a value is a non-empty string")
    return " ".join(value.split())


@pytest.fixture
def spaced_vocabulary():
    register_vocabulary("test-spaced", label="Spaced", validator=_spaced_validator, canonicalise=_collapse)
    yield
    unregister_vocabulary("test-spaced")


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

    def test_validate_code_returns_the_value_it_was_given_without_a_canonicaliser(self, test_vocabulary):
        assert validate_code("test-vocab", "Seizure", None) == "Seizure"
        assert validate_code("nobody-registered", " as typed ", None) == " as typed "

    def test_a_canonicaliser_runs_before_the_validator_and_its_result_is_returned(self, spaced_vocabulary):
        assert validate_code("test-spaced", "  Seizure ", None) == "Seizure"
        with pytest.raises(ValueError, match="not in the spaced vocabulary"):
            validate_code("test-spaced", " Sei zure ", None)
        with pytest.raises(ValueError, match="a value is a non-empty string"):
            validate_code("test-spaced", 3, None)

    def test_a_vocabulary_names_no_terms_unless_it_registers_a_lookup(self, test_vocabulary):
        assert get_vocabulary("test-vocab").term_name is None
        assert get_vocabulary("nobody-registered-this") is None
        register_vocabulary("named-vocab", label="Named", validator=_terms_validator, term_name=str.upper)
        try:
            assert get_vocabulary("named-vocab").term_name("spike") == "SPIKE"
        finally:
            unregister_vocabulary("named-vocab")

    def test_a_closed_vocabulary_is_confined_and_an_open_one_is_not_unless_declared(self):
        register_vocabulary("closed-vocab", label="Closed", validator=_terms_validator, closed=True)
        register_vocabulary("confined-vocab", label="Confined", validator=_terms_validator, confined=True)
        try:
            assert get_vocabulary("closed-vocab").confined is True
            assert get_vocabulary("confined-vocab").confined is True
            assert get_vocabulary("confined-vocab").closed is False
        finally:
            unregister_vocabulary("closed-vocab")
            unregister_vocabulary("confined-vocab")
        register_vocabulary("open-vocab", label="Open", validator=_terms_validator)
        try:
            assert get_vocabulary("open-vocab").confined is False
            assert get_vocabulary("open-vocab").write_meta is None
        finally:
            unregister_vocabulary("open-vocab")

    def test_the_core_vocabularies_name_their_terms(self):
        assert get_vocabulary("epicurrents.eeg").term_name("EEG_ACT_HV") == "Hyperventilation"
        assert get_vocabulary("epicurrents.eeg").term_name("NOT_A_TERM") is None


@pytest.mark.django_db
class TestCreateCodeValidation:
    def test_valid_term_is_created(self, auth_client, event, test_vocabulary):
        c, _user = auth_client
        resp = post_json(c, CODES_URL, _payload(event))
        assert resp.status_code == 200, resp.content
        assert Code.objects.filter(pk=resp.json()["id"]).exists()

    def test_the_canonical_spelling_is_what_is_written_and_served(self, auth_client, event, spaced_vocabulary):
        c, _user = auth_client
        resp = post_json(c, CODES_URL, _payload(event, standard="test-spaced", value="  Seizure\t"))
        assert resp.status_code == 200, resp.content
        assert resp.json()["value"] == "Seizure"
        assert Code.objects.get(pk=resp.json()["id"]).value == "Seizure"

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

    def test_a_patched_value_is_saved_in_its_canonical_spelling(self, auth_client, event, spaced_vocabulary):
        c, _user = auth_client
        code = self._code(event, standard="test-spaced")
        resp = patch_json(c, f"{CODES_URL}{code.pk}", {"value": " Spike  "})
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
class TestVocabularyListing:
    def test_lists_every_registered_standard(self, auth_client, test_vocabulary):
        c, _user = auth_client
        resp = c.get(VOCABULARIES_URL)
        assert resp.status_code == 200
        listed = {row["standard"]: row for row in resp.json()}
        assert listed["test-vocab"] == {
            "standard": "test-vocab",
            "label": "Test vocabulary",
            "version": "1.0",
            "closed": False,
            "confined": False,
        }
        assert set(listed) == {v.standard for v in registered_vocabularies()}

    def test_core_vocabularies_are_listed_as_closed(self, auth_client):
        c, _user = auth_client
        listed = {row["standard"]: row for row in c.get(VOCABULARIES_URL).json()}
        assert listed["epicurrents.biosignal"]["closed"] is True
        assert listed["epicurrents.biosignal"]["confined"] is True
        assert listed["epicurrents.eeg"]["version"] == "2.0"

    def test_requires_authentication(self, client):
        assert client.get(VOCABULARIES_URL).status_code == 401

    def test_listing_is_audited_with_a_count(self, auth_client):
        from activity.models import Activity

        c, _user = auth_client
        c.get(VOCABULARIES_URL)
        row = Activity.objects.filter(verb="annotations.vocabulary.list").latest("pk")
        assert row.metadata == {"vocabulary_count": len(registered_vocabularies())}


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
