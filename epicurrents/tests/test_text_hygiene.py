"""The identifier heuristic behind the free-text warnings, and the check on its deployment patterns."""

import pytest
from django.test import override_settings

from epicurrents.checks import check_text_hygiene_patterns
from epicurrents.text_hygiene import (
    KIND_DATE,
    KIND_DIGIT_RUN,
    KIND_PERSON_NAME,
    looks_like_identifier,
    name_warnings,
)


@pytest.mark.parametrize(
    ("text", "kinds"),
    [
        ("", []),
        (None, []),
        ("Recording 1", []),
        ("EEG Baseline", []),
        ("Sleep study night 2", []),
        ("Subject 12345", []),
        ("Subject 123456", [KIND_DIGIT_RUN]),
        ("010185-123X", [KIND_DIGIT_RUN]),
        ("2024-03-05", [KIND_DATE]),
        ("eeg 05.03.2024 morning", [KIND_DATE]),
        ("5/3/24", [KIND_DATE]),
        ("5 March 2024", [KIND_DATE]),
        ("March 5, 2024", [KIND_DATE]),
        ("v1.2.3", []),
        ("Jane Doe", [KIND_PERSON_NAME]),
        ("Jane M. Doe", [KIND_PERSON_NAME]),
        ("Doe, Jane", [KIND_PERSON_NAME]),
        ("Doe, Jane Mary", [KIND_PERSON_NAME]),
        ("Doe, J.", [KIND_PERSON_NAME]),
        ("Ääkkönen, Päivi", [KIND_PERSON_NAME]),
        ("O'Brien, Seán", [KIND_PERSON_NAME]),
        ("Jane Doe 19850312", [KIND_DIGIT_RUN]),
        ("jane doe", []),
        ("JANE DOE", []),
    ],
)
def test_built_in_shapes(text, kinds):
    assert looks_like_identifier(text) == kinds


@override_settings(TEXT_HYGIENE_PATTERNS={"study_code": r"\bSTUDY-\d{3}\b"})
def test_deployment_pattern_is_reported_by_its_kind():
    assert looks_like_identifier("STUDY-042 baseline") == ["study_code"]
    assert looks_like_identifier("study baseline") == []


@override_settings(TEXT_HYGIENE_PATTERNS={"study_code": r"\bSTUDY-\d{3}\b"})
def test_deployment_pattern_comes_after_the_built_in_kinds():
    assert looks_like_identifier("STUDY-042 2024-03-05") == [KIND_DATE, "study_code"]


def test_name_warnings_rows_name_the_field():
    rows = name_warnings(name="Doe, Jane", description="seen 2024-03-05", prefix=None)
    assert [(row["field"], row["kind"]) for row in rows] == [
        ("name", KIND_PERSON_NAME),
        ("description", KIND_DATE),
    ]
    assert rows[0]["message"].startswith("The name reads as a person's name")
    assert rows[1]["message"].endswith("recipients see it as typed.")


def test_name_warnings_empty_for_clean_text():
    assert name_warnings(display_name="Recording 1", description="") == []


def test_name_warnings_field_with_underscore_is_spelled_out():
    (row,) = name_warnings(display_name="Jane Doe")
    assert row["message"].startswith("The display name ")


class TestPatternCheck:
    @override_settings(TEXT_HYGIENE_PATTERNS={"study_code": r"\bSTUDY-\d{3}\b"})
    def test_valid_mapping_passes(self):
        assert check_text_hygiene_patterns(None) == []

    @override_settings(TEXT_HYGIENE_PATTERNS=[r"\d+"])
    def test_non_mapping_is_an_error(self):
        (issue,) = check_text_hygiene_patterns(None)
        assert issue.id == "epicurrents.E010"

    @override_settings(TEXT_HYGIENE_PATTERNS={"": r"\d+", "bad": 3})
    def test_bad_entries_are_errors(self):
        assert sorted(issue.id for issue in check_text_hygiene_patterns(None)) == [
            "epicurrents.E011",
            "epicurrents.E011",
        ]

    @override_settings(TEXT_HYGIENE_PATTERNS={"broken": r"(\d+"})
    def test_pattern_that_does_not_compile_is_an_error(self):
        (issue,) = check_text_hygiene_patterns(None)
        assert issue.id == "epicurrents.E012"
        assert "broken" in issue.msg
