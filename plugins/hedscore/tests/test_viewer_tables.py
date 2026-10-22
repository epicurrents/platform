"""The tables the viewer's finding picker copies agree with the platform, so what the picker writes this plugin accepts.

Three tables live in the interface beside the SCORE term index. The canonical electrode table fills `Sensor-list`, and
the validator accepts a sensor only when `canonicalise_label` reads it back as itself, so the copy has to agree with
the table the platform canonicalises by. The standard terms are the HED standard's side and symmetry tags and the ID a
finding group is numbered by, which the index does not hold, so their copy has to name tags of the pinned schema. The
numeric domains are the unit, range and precision the validator accepts a number in, which the picker writes a number
to, so their copy has to be the validator's table.
These tests check both wherever the viewer checkout is present; a failure means regenerating the copy from the
platform's source.
"""

import json
from pathlib import Path

import pytest

from plugins.hedscore.schema import SCHEMA_PIN, load_schema
from plugins.hedscore.validation import FINDING_GROUP, FINDING_GROUP_NUMBERS, NUMERIC_DOMAINS, validate_hed
from recordings.processors import channel_labels
from recordings.processors.channel_labels import canonicalise_label

REPO_ROOT = Path(__file__).resolve().parents[3]
VIEWER_EEG = REPO_ROOT / "frontend/viewer/interface/src/app/modules/eeg"
VIEWER_ELECTRODES = VIEWER_EEG / "canonical-electrodes.json"
VIEWER_STANDARD_TERMS = VIEWER_EEG / "standard-terms.json"
VIEWER_NUMERIC_DOMAINS = VIEWER_EEG / "numeric-domains.json"


@pytest.fixture(scope="module")
def viewer_electrodes():
    if not VIEWER_ELECTRODES.exists():
        pytest.skip("viewer checkout not present")
    return json.loads(VIEWER_ELECTRODES.read_text(encoding="utf-8"))


def test_table_is_the_platform_table(viewer_electrodes):
    assert viewer_electrodes["electrodes"] == list(channel_labels._TEN_TEN_ELECTRODES)
    assert viewer_electrodes["renames"] == channel_labels._OLD_TO_NEW


def test_every_label_reads_back_as_itself(viewer_electrodes):
    labels = viewer_electrodes["electrodes"] + list(viewer_electrodes["renames"].values())
    assert [label for label in labels if canonicalise_label(label) != label] == []


def test_a_location_of_every_label_validates(viewer_electrodes):
    sensors = ";".join(viewer_electrodes["electrodes"])
    validate_hed(f"(Epileptiform-interictal-activity, Sensor-list/{sensors})", SCHEMA_PIN.as_meta())


@pytest.fixture(scope="module")
def viewer_standard_terms():
    if not VIEWER_STANDARD_TERMS.exists():
        pytest.skip("viewer checkout not present")
    return json.loads(VIEWER_STANDARD_TERMS.read_text(encoding="utf-8"))


def test_standard_terms_are_tags_of_the_pinned_standard(viewer_standard_terms):
    schema = load_schema()
    assert viewer_standard_terms["withStandard"] == SCHEMA_PIN.with_standard
    for entry in viewer_standard_terms["terms"]:
        term = schema.terms[entry["name"]]
        assert not term.in_library
        assert (entry["branch"], entry["description"], entry["hedId"]) == (term.branch, term.description, term.hed_id)
        assert not (term.require_child or term.deprecated or term.reserved)


def test_a_finding_with_each_standard_term_validates(viewer_standard_terms):
    for entry in viewer_standard_terms["terms"]:
        # The one standard tag the picker writes with a value is a finding group's number.
        tag = f"{FINDING_GROUP}/{FINDING_GROUP_NUMBERS.start}" if entry["name"] == FINDING_GROUP else entry["name"]
        validate_hed(f"(Posterior-dominant-rhythm, {tag})", SCHEMA_PIN.as_meta())


def test_numeric_domains_are_the_validators_table():
    if not VIEWER_NUMERIC_DOMAINS.exists():
        pytest.skip("viewer checkout not present")
    copy = json.loads(VIEWER_NUMERIC_DOMAINS.read_text(encoding="utf-8"))
    assert copy["version"] == SCHEMA_PIN.version
    assert copy["domains"] == {
        name: {"unit": d.unit, "low": int(d.low), "high": int(d.high), "decimals": d.decimals}
        for name, d in NUMERIC_DOMAINS.items()
    }
