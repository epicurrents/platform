"""The plugin through the platform: ``hed`` codes written through the annotations API, the policy endpoint, the check."""

import pytest
from django.contrib.contenttypes.models import ContentType
from model_bakery import baker

from activity.models import Activity
from annotations.models import Code, Event
from conftest import post_json
from plugins.hedscore.checks import check_allowed_branches
from plugins.hedscore.schema import SCHEMA_PIN, load_schema

CODES_URL = "/annotations/api/v1/codes/"
VOCABULARIES_URL = "/annotations/api/v1/vocabularies"
POLICY_URL = "/plugin/hedscore/api/v1/policy"


@pytest.fixture
def event(db, user):
    recording = baker.make("recordings.Recording", author=user)
    return Event.objects.create(
        author=user,
        target_content_type=ContentType.objects.get_for_model(recording, for_concrete_model=False),
        target_object_id=str(recording.pk),
        object_hash="C" * 32,
        name="finding",
        timestamp=1.0,
    )


def _payload(event, value, meta=None):
    return {
        "content_type_id": ContentType.objects.get_for_model(Event).pk,
        "object_id": str(event.pk),
        "standard": "hed",
        "value": value,
        "meta": SCHEMA_PIN.as_meta() if meta is None else meta,
    }


@pytest.mark.django_db
class TestCodeWrites:
    def test_a_valid_string_is_stored_with_its_pin(self, auth_client, event):
        c, _user = auth_client
        resp = post_json(c, CODES_URL, _payload(event, "Spike, (Sensor-list/F7;T7)"))
        assert resp.status_code == 200, resp.content
        code = Code.objects.get(pk=resp.json()["id"])
        assert code.standard == "hed"
        assert code.meta == SCHEMA_PIN.as_meta()

    def test_a_text_placeholder_is_refused_and_nothing_is_written(self, auth_client, event):
        c, _user = auth_client
        resp = post_json(c, CODES_URL, _payload(event, "Spike, Description/seen in the waiting room"))
        assert resp.status_code == 422
        assert not Code.objects.exists()

    def test_a_raw_channel_label_is_refused(self, auth_client, event):
        c, _user = auth_client
        resp = post_json(c, CODES_URL, _payload(event, "Spike, Sensor-list/EEG Fp1-REF"))
        assert resp.status_code == 422

    def test_text_in_meta_is_refused(self, auth_client, event):
        c, _user = auth_client
        resp = post_json(c, CODES_URL, _payload(event, "Spike", meta={**SCHEMA_PIN.as_meta(), "note": "free text"}))
        assert resp.status_code == 422

    def test_the_branch_allowlist_holds_at_the_api(self, auth_client, event, settings):
        settings.HEDSCORE_ALLOWED_BRANCHES = ["Modulator"]
        c, _user = auth_client
        assert post_json(c, CODES_URL, _payload(event, "Spike")).status_code == 422
        assert post_json(c, CODES_URL, _payload(event, "Manual-eye-closure")).status_code == 200


@pytest.mark.django_db
class TestVocabularyListing:
    def test_hed_is_listed_at_the_pinned_version_and_open(self, auth_client):
        c, _user = auth_client
        listed = {row["standard"]: row for row in c.get(VOCABULARIES_URL).json()}
        assert listed["hed"]["version"] == SCHEMA_PIN.version
        assert listed["hed"]["closed"] is False


@pytest.mark.django_db
class TestPolicy:
    def test_every_branch_when_none_is_configured(self, auth_client, settings):
        settings.HEDSCORE_ALLOWED_BRANCHES = []
        c, _user = auth_client
        resp = c.get(POLICY_URL)
        assert resp.status_code == 200
        assert resp.json() == {
            "library": SCHEMA_PIN.library,
            "version": SCHEMA_PIN.version,
            "with_standard": SCHEMA_PIN.with_standard,
            "sha256": SCHEMA_PIN.sha256,
            "allowed_branches": list(load_schema().branches),
        }

    def test_the_configured_branches_when_restricted(self, auth_client, settings):
        settings.HEDSCORE_ALLOWED_BRANCHES = ["Modulator"]
        c, _user = auth_client
        assert c.get(POLICY_URL).json()["allowed_branches"] == ["Modulator"]

    def test_requires_authentication(self, client):
        assert client.get(POLICY_URL).status_code == 401

    def test_is_audited(self, auth_client, settings):
        settings.HEDSCORE_ALLOWED_BRANCHES = ["Modulator"]
        c, _user = auth_client
        c.get(POLICY_URL)
        row = Activity.objects.filter(verb="hedscore.policy.read").latest("pk")
        assert row.metadata == {"restricted": True}


class TestAllowedBranchesCheck:
    def test_known_branches_pass(self, settings):
        settings.HEDSCORE_ALLOWED_BRANCHES = ["Modulator", "Property"]
        assert check_allowed_branches() == []

    def test_an_unknown_branch_is_an_error(self, settings):
        settings.HEDSCORE_ALLOWED_BRANCHES = ["Modulators"]
        errors = check_allowed_branches()
        assert [error.id for error in errors] == ["hedscore.E001"]
        assert "Modulators" in errors[0].msg
