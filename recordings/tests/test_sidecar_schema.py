"""Tests for the pinned sidecar event schema in recordings.converters.sidecar.

The schema was previously defined implicitly by a vendor converter's output: ``save_sidecar_events`` read
every key with a bare ``.get()``, so a replacement converter emitting different key names would have
produced rows of null onsets and empty labels while every existing test stayed green. These tests pin the
contract: conforming sidecars persist exactly as before, and a shape violation raises ``ValueError`` naming
the offending list, index, and key instead of silently absorbing the mismatch.
"""

import pytest
from model_bakery import baker

from annotations.models import Annotation
from recordings.converters.sidecar import save_sidecar_events, validate_sidecar_events


@pytest.fixture
def recording(db, user):
    return baker.make("recordings.Recording", author=user, status="READY")


class TestValidateSidecarEvents:
    def test_conforming_sidecar_passes(self):
        validate_sidecar_events(
            {
                "annotations": [{"onset_seconds": 1.0, "duration_seconds": 0.5, "text": "aura"}],
                "events": [{"onset_seconds": 2, "duration_seconds": None, "type": "Photic", "label": "10 Hz"}],
            }
        )

    def test_optional_keys_may_be_absent(self):
        validate_sidecar_events(
            {
                "annotations": [{"onset_seconds": 1.0}],
                "events": [{"onset_seconds": 2.0}],
            }
        )

    def test_empty_and_missing_lists_pass(self):
        validate_sidecar_events({})
        validate_sidecar_events({"annotations": [], "events": []})

    def test_missing_onset_is_rejected(self):
        with pytest.raises(ValueError, match=r"annotations\[0\].*onset_seconds"):
            validate_sidecar_events({"annotations": [{"duration_seconds": 0.5, "text": "aura"}]})

    def test_renamed_onset_key_is_rejected_not_absorbed(self):
        # The exact regression the pin exists for: a converter emitting
        # "onset" instead of "onset_seconds" must fail, not write null onsets.
        with pytest.raises(ValueError, match=r"events\[0\].*onset_seconds"):
            validate_sidecar_events({"events": [{"onset": 2.0, "label": "10 Hz"}]})

    def test_non_numeric_onset_is_rejected(self):
        with pytest.raises(ValueError, match=r"annotations\[0\].*onset_seconds.*str"):
            validate_sidecar_events({"annotations": [{"onset_seconds": "1.0"}]})

    def test_boolean_onset_is_rejected(self):
        # bool is an int subclass; the schema excludes it explicitly.
        with pytest.raises(ValueError, match=r"onset_seconds.*bool"):
            validate_sidecar_events({"annotations": [{"onset_seconds": True}]})

    def test_non_string_text_is_rejected(self):
        with pytest.raises(ValueError, match=r"annotations\[0\].*'text'"):
            validate_sidecar_events({"annotations": [{"onset_seconds": 1.0, "text": 42}]})

    def test_non_dict_item_is_rejected(self):
        with pytest.raises(ValueError, match=r"events\[1\] is not an object"):
            validate_sidecar_events({"events": [{"onset_seconds": 1.0}, "not-a-dict"]})

    def test_non_dict_sidecar_is_rejected(self):
        # The import path hands the converter's sidecar over unfiltered, so a
        # non-dict must raise the same ValueError as any other shape violation
        # rather than an AttributeError.
        with pytest.raises(ValueError, match="not an object"):
            validate_sidecar_events(["not", "a", "dict"])

    def test_non_list_container_is_rejected(self):
        with pytest.raises(ValueError, match='"annotations" is not a list'):
            validate_sidecar_events({"annotations": {"onset_seconds": 1.0}})

    def test_violation_names_the_offending_index(self):
        with pytest.raises(ValueError, match=r"events\[2\]"):
            validate_sidecar_events(
                {
                    "events": [
                        {"onset_seconds": 1.0},
                        {"onset_seconds": 2.0},
                        {"duration_seconds": 3.0},
                    ]
                }
            )

    def test_unknown_extra_keys_are_ignored(self):
        # The pin constrains what this module reads, not the converter's whole output.
        validate_sidecar_events({"events": [{"onset_seconds": 1.0, "vendor_field": object()}]})


@pytest.mark.django_db
class TestSaveSidecarEventsValidation:
    def test_conforming_sidecar_persists(self, recording):
        save_sidecar_events(
            recording,
            {"annotations": [{"onset_seconds": 1.0, "duration_seconds": 0.5, "text": "aura"}]},
        )
        row = Annotation.objects.get(target_object_id=str(recording.pk))
        assert row.content["events"] == [{"onset": 1.0, "duration": 0.5, "label": "aura"}]

    def test_malformed_sidecar_writes_nothing(self, recording):
        with pytest.raises(ValueError):
            save_sidecar_events(
                recording,
                {
                    "annotations": [{"onset_seconds": 1.0, "text": "kept?"}],
                    "events": [{"onset": 2.0}],
                },
            )
        assert not Annotation.objects.filter(target_object_id=str(recording.pk)).exists()
