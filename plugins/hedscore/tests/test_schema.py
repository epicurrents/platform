"""The vendored schema: its pin, the index parsed from it, and the loud failure when the file is wrong."""

import hashlib

import pytest
from django.core.exceptions import ImproperlyConfigured

from plugins.hedscore.schema import SCHEMA_DIR, SCHEMA_PIN, load_schema, read_pinned_schema

SCORE_BRANCHES = {
    "Modulator",
    "Background-activity",
    "Critically-ill-patient-patterns",
    "Episode",
    "Feature-property",
    "Interictal-activity",
    "Physiologic-pattern",
    "Polygraphic-channel-feature",
    "Sleep-and-drowsiness",
    "Uncertain-significant-pattern",
}
STANDARD_BRANCHES = {"Event", "Agent", "Action", "Item", "Property", "Relation"}


class TestPin:
    def test_vendored_file_matches_its_pin(self):
        data = (SCHEMA_DIR / SCHEMA_PIN.file).read_bytes()
        assert hashlib.sha256(data).hexdigest() == SCHEMA_PIN.sha256

    def test_the_file_declares_the_pinned_release(self):
        head = (SCHEMA_DIR / SCHEMA_PIN.file).read_bytes()[:200].decode()
        assert f'version="{SCHEMA_PIN.version}" library="{SCHEMA_PIN.library}"' in head
        assert f'withStandard="{SCHEMA_PIN.with_standard}"' in head

    def test_the_licence_travels_with_the_schema(self):
        assert "Attribution 4.0 International" in (SCHEMA_DIR / "LICENSE").read_text()

    def test_a_missing_file_stops_the_boot(self, tmp_path):
        with pytest.raises(ImproperlyConfigured, match="missing"):
            read_pinned_schema(directory=tmp_path)

    def test_an_altered_file_stops_the_boot(self, tmp_path):
        data = (SCHEMA_DIR / SCHEMA_PIN.file).read_bytes()
        (tmp_path / SCHEMA_PIN.file).write_bytes(data.replace(b"Spike", b"Spoke", 1))
        with pytest.raises(ImproperlyConfigured, match="SHA-256"):
            read_pinned_schema(directory=tmp_path)


class TestIndex:
    def test_term_counts_match_the_pinned_file(self):
        index = load_schema()
        # 1743 nodes carry a HED id, 216 of them value placeholders; SCORE's 510 nodes hold 396 named terms.
        assert len(index.terms) == 1527
        assert index.library_term_count() == 396

    def test_branches_are_the_library_and_the_partnered_standard(self):
        assert set(load_schema().branches) == SCORE_BRANCHES | STANDARD_BRANCHES

    def test_a_term_records_its_branch_and_its_placeholder(self):
        photic = load_schema().terms["Intermittent-photic-stimulation"]
        assert photic.branch == "Modulator"
        assert photic.in_library
        assert photic.takes_value
        assert photic.value_classes == {"numericClass"}
        assert photic.unit_classes == {"frequencyUnits"}

    def test_standard_terms_are_not_library_terms(self):
        assert not load_schema().terms["Speak"].in_library

    def test_si_units_take_their_prefixes(self):
        frequency = load_schema().units["frequencyUnits"]
        assert {"Hz", "kHz", "hertz", "kilohertz"} <= frequency
        assert "kHertz" not in frequency
