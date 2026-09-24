"""The platform's own vocabularies: the pinned copies match their pins and the viewer, and the validators gate writes.

The pin is what makes a drift visible. A term added in the viewer and not copied here answers 422 to the client that
uses it, and a copy edited on its own names a value the viewer cannot; the viewer checkout is beside the platform in a
development tree, and the comparison runs whenever it is.
"""

import hashlib
import json
from pathlib import Path

import pytest
from django.contrib.contenttypes.models import ContentType
from django.test import override_settings
from model_bakery import baker

from annotations.core_vocabularies import (
    VOCABULARY_DIR,
    VOCABULARY_PINS,
    accepted_codes,
    acquisition_codes,
    load_vocabulary,
)
from annotations.models import Code, Event
from annotations.vocabularies import registered_vocabularies, validate_code
from conftest import post_json

CODES_URL = "/annotations/api/v1/codes/"
REPO_ROOT = Path(__file__).resolve().parents[2]
VIEWER_FILES = {
    "epicurrents.biosignal": REPO_ROOT / "frontend/viewer/epicurrents/core/src/assets/annotation/vocabulary/biosignal-events.json",
    "epicurrents.eeg": REPO_ROOT / "frontend/viewer/epicurrents/eeg-module/src/components/vocabulary/eeg-events.json",
}


@pytest.mark.parametrize("standard", sorted(VOCABULARY_PINS))
class TestPinnedCopies:
    def test_copy_names_its_standard_and_pinned_version(self, standard):
        vocabulary = load_vocabulary(standard)
        assert vocabulary["standard"] == standard
        assert vocabulary["version"] == VOCABULARY_PINS[standard].version

    def test_copy_carries_the_pinned_digest(self, standard):
        digest = hashlib.sha256((VOCABULARY_DIR / VOCABULARY_PINS[standard].file).read_bytes()).hexdigest()
        assert digest == VOCABULARY_PINS[standard].sha256, "the copy changed; move the pin with it, viewer first"

    def test_copy_is_the_viewer_file(self, standard):
        viewer_file = VIEWER_FILES[standard]
        if not viewer_file.exists():
            pytest.skip("viewer checkout not present")
        assert (VOCABULARY_DIR / VOCABULARY_PINS[standard].file).read_bytes() == viewer_file.read_bytes()

    def test_every_acquisition_term_has_a_code_a_name_and_a_class(self, standard):
        for name, category in load_vocabulary(standard)["categories"].items():
            if category["scope"] != "acquisition":
                continue
            for key, term in category["events"].items():
                assert term["code"] and term["name"], f"{name}.{key}"
                assert term["class"] in {"activation", "event", "technical", "trigger"}, f"{name}.{key}"


class TestSets:
    def test_biosignal_accepts_its_four_categories(self):
        vocabulary = load_vocabulary("epicurrents.biosignal")
        assert set(vocabulary["categories"]) == {"TECHNICAL", "INTERVENTION", "OBSERVATION", "ENVIRONMENT"}
        assert accepted_codes("epicurrents.biosignal") == acquisition_codes("epicurrents.biosignal")
        assert {"BIO_TECH_CALIBRATION", "BIO_INT_MEDICATION", "BIO_OBS_LOC_ALERT", "BIO_ENV_NOISE"} <= accepted_codes(
            "epicurrents.biosignal"
        )

    def test_eeg_accepts_activation_and_the_shared_set_but_no_finding(self):
        eeg = acquisition_codes("epicurrents.eeg")
        assert {"EEG_ACT_EC", "EEG_ACT_HV", "EEG_ACT_PHOTIC_10HZ", "EEG_ACT_STIM_NOXIOUS"} <= eeg
        assert "EEG_BKG_ALPHA" not in eeg
        assert "EEG_EPI_SPIKE" not in eeg
        assert accepted_codes("epicurrents.eeg") == eeg | accepted_codes("epicurrents.biosignal")

    def test_codes_are_unique_across_both_files(self):
        seen: dict[str, str] = {}
        for standard in VOCABULARY_PINS:
            for category in load_vocabulary(standard)["categories"].values():
                for term in category["events"].values():
                    assert term["code"] not in seen, f"{term['code']} in {standard} and {seen[term['code']]}"
                    seen[term["code"]] = standard


class TestRegistration:
    def test_both_standards_are_registered_at_boot(self):
        registered = {v.standard: v for v in registered_vocabularies()}
        for standard, pin in VOCABULARY_PINS.items():
            assert registered[standard].version == pin.version
            assert registered[standard].label == pin.label

    def test_validate_code_accepts_a_term_and_names_a_stranger(self):
        validate_code("epicurrents.biosignal", "BIO_OBS_POSITION_SUPINE", None)
        validate_code("epicurrents.eeg", "EEG_ACT_EO", {"anything": "goes"})
        validate_code("epicurrents.eeg", "BIO_TECH_TRIGGER", None)
        with pytest.raises(ValueError, match="'EEG_ACT_EO' is not in the epicurrents.biosignal"):
            validate_code("epicurrents.biosignal", "EEG_ACT_EO", None)
        with pytest.raises(ValueError, match="'EEG_BKG_ALPHA' is not in the epicurrents.eeg"):
            validate_code("epicurrents.eeg", "EEG_BKG_ALPHA", None)
        with pytest.raises(ValueError, match="not in the"):
            validate_code("epicurrents.biosignal", ["BIO_TECH_TRIGGER"], None)

    @override_settings(ANNOTATION_CODE_STRICT_VOCABULARY=True)
    def test_strict_mode_accepts_the_platform_vocabularies(self):
        validate_code("epicurrents.biosignal", "BIO_INT_CPR", None)


@pytest.fixture
def event(db, user):
    recording = baker.make("recordings.Recording", author=user)
    return Event.objects.create(
        author=user,
        target_content_type=ContentType.objects.get_for_model(recording, for_concrete_model=False),
        target_object_id=str(recording.pk),
        object_hash="C" * 32,
        name="test-event",
        timestamp=1.0,
    )


def _payload(event, **kwargs):
    defaults = {
        "content_type_id": ContentType.objects.get_for_model(Event).pk,
        "object_id": str(event.pk),
        "standard": "epicurrents.biosignal",
        "value": "BIO_TECH_CALIBRATION",
    }
    defaults.update(kwargs)
    return defaults


@pytest.mark.django_db
class TestApiWrites:
    def test_a_term_is_written(self, auth_client, event):
        c, _user = auth_client
        resp = post_json(c, CODES_URL, _payload(event, meta={"number": 3}))
        assert resp.status_code == 200, resp.content
        assert Code.objects.filter(pk=resp.json()["id"], value="BIO_TECH_CALIBRATION").exists()

    def test_a_stranger_answers_422_and_writes_nothing(self, auth_client, event):
        c, _user = auth_client
        resp = post_json(c, CODES_URL, _payload(event, value="Calibration"))
        assert resp.status_code == 422, resp.content
        assert "Calibration" in json.dumps(resp.json())
        assert not Code.objects.filter(object_id=str(event.pk)).exists()

    def test_an_eeg_event_may_carry_a_shared_term_under_the_eeg_standard(self, auth_client, event):
        c, _user = auth_client
        resp = post_json(c, CODES_URL, _payload(event, standard="epicurrents.eeg", value="BIO_OBS_LOC_ASLEEP"))
        assert resp.status_code == 200, resp.content
