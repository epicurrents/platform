"""The viewer's HED-SCORE term index was generated from the schema this plugin pins, and agrees with its parse.

The picker offers the terms of an index eeg-module generates at build time; this plugin validates what the picker
writes. Neither side authors terms, so the two agree exactly when both read the same release the same way, and these
tests check that wherever the viewer checkout is present. A failure here means a term or a unit the picker offers
and the validator refuses, or the reverse: regenerate the index from the vendored file rather than editing either.
"""

import json
from pathlib import Path

import pytest

from plugins.hedscore.schema import SCHEMA_PIN, load_schema
from plugins.hedscore.validation import NUMERIC_CLASS, validate_hed

REPO_ROOT = Path(__file__).resolve().parents[3]
VIEWER_INDEX = REPO_ROOT / "frontend/viewer/epicurrents/eeg-module/src/components/vocabulary/hed-score.json"


@pytest.fixture(scope="module")
def viewer_index():
    if not VIEWER_INDEX.exists():
        pytest.skip("viewer checkout not present")
    return json.loads(VIEWER_INDEX.read_text(encoding="utf-8"))


def test_index_names_the_pinned_release(viewer_index):
    assert viewer_index["schema"] == {
        "library": SCHEMA_PIN.library,
        "version": SCHEMA_PIN.version,
        "withStandard": SCHEMA_PIN.with_standard,
        "sha256": SCHEMA_PIN.sha256,
    }


def test_index_holds_exactly_the_library_terms(viewer_index):
    library = {name for name, term in load_schema().terms.items() if term.in_library}
    assert {term["name"] for term in viewer_index["terms"]} == library


def test_every_term_agrees_with_the_plugin_parse(viewer_index):
    schema = load_schema()
    for entry in viewer_index["terms"]:
        term = schema.terms[entry["name"]]
        assert entry["parent"] == (term.path[-2] if len(term.path) > 1 else None), term.name
        assert entry["hedId"] == term.hed_id, term.name
        assert set(entry.get("valueClasses", [])) == term.value_classes, term.name
        assert ("valueClasses" in entry) == term.takes_value, term.name
        assert set(entry.get("unitClasses", [])) == term.unit_classes, term.name
        assert entry.get("requireChild", False) == term.require_child, term.name
        assert entry.get("deprecated", False) == term.deprecated, term.name


def test_units_agree_with_the_validator(viewer_index):
    schema = load_schema()
    named = {unit_class for term in schema.terms.values() if term.in_library for unit_class in term.unit_classes}
    assert set(viewer_index["units"]) == named
    for unit_class, units in viewer_index["units"].items():
        assert set(units["units"]) == schema.units[unit_class], unit_class


def test_every_numeric_term_written_in_its_default_unit_is_accepted(viewer_index, settings):
    settings.HEDSCORE_ALLOWED_BRANCHES = []
    numeric = [term for term in viewer_index["terms"] if NUMERIC_CLASS in term.get("valueClasses", [])]
    assert numeric
    for term in numeric:
        defaults = [viewer_index["units"][unit_class]["default"] for unit_class in term.get("unitClasses", [])]
        value = f"{term['name']}/1 {defaults[0]}" if defaults else f"{term['name']}/1"
        validate_hed(value, SCHEMA_PIN.as_meta())
