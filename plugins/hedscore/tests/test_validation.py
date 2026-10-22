"""The ``hed`` validator: what a code's value may carry under the rules that keep it a closed vocabulary."""

import pytest

from plugins.hedscore.schema import SCHEMA_PIN
from plugins.hedscore.validation import MAX_DEPTH, MAX_LENGTH, validate_hed

PIN = SCHEMA_PIN.as_meta()


def accepts(value):
    validate_hed(value, PIN)


def refuses(value, match):
    with pytest.raises(ValueError, match=match):
        validate_hed(value, PIN)


class TestTags:
    @pytest.mark.parametrize(
        "value",
        [
            "Spike",
            "Spike-and-slow-wave, Left",
            "Spike, (Sensor-list/F7;T7), Feature-frequency/3 Hz",
            "Hyperventilation-good-effort",
            "Speak",
        ],
    )
    def test_short_forms_of_schema_terms_are_accepted(self, value):
        accepts(value)

    def test_an_unknown_tag_is_refused(self):
        refuses("Spikey", "not a tag")

    def test_case_is_exact(self):
        refuses("spike", "not a tag")

    def test_a_long_form_is_refused(self):
        refuses("Feature-property/Signal-morphology-property/Spike", "short form")

    def test_an_extension_is_refused(self):
        refuses("Interictal-activity/My-own-pattern", "short form")

    def test_a_reserved_tag_is_refused(self):
        refuses("Duration/2 s", "reserved")

    def test_a_deprecated_tag_is_refused(self):
        refuses("Clock-face/3", "deprecated")

    def test_a_tag_that_requires_a_value_is_refused_bare(self):
        refuses("Sensor-list", "needs a value")


class TestPlaceholderValues:
    @pytest.mark.parametrize(
        "value",
        [
            "Intermittent-photic-stimulation/10 Hz",
            "Intermittent-photic-stimulation/10",
            "Feature-amplitude/150 uV",
            "Episode-event-count/3",
            "Number-of-RPP-phases/2",
            "Oxygen-saturation/88.5",
        ],
    )
    def test_a_numeric_value_is_accepted(self, value):
        accepts(value)

    def test_a_number_is_required(self):
        refuses("Intermittent-photic-stimulation/fast", "takes a number")

    def test_a_unit_outside_the_class_is_refused(self):
        refuses("Intermittent-photic-stimulation/10 s", "not a unit")

    @pytest.mark.parametrize(
        "value",
        [
            "Description/Patient said her name",
            "Label/Anna",
            "Other-episode/fell off chair",
            "Spike/left temporal",
            "Subject-identifier/12345",
            "Sleep-stage-N2/brief",
        ],
    )
    def test_a_text_value_is_refused_whatever_it_says(self, value):
        refuses(value, "text value")


class TestComparisons:
    """The forms the viewer's finding picker writes a bound, an approximation, a range or a dominance in."""

    @pytest.mark.parametrize(
        "value",
        [
            "(Epileptiform-interictal-activity, (Greater-than-or-equal-to, Feature-frequency/2 Hz))",
            "(Epileptiform-interictal-activity, (Less-than-or-equal-to, Feature-frequency/4 Hz))",
            "(Epileptiform-interictal-activity, (Approximately-equal-to, Feature-frequency/3.5 Hz))",
            (
                "(Epileptiform-interictal-activity, (Greater-than-or-equal-to, Feature-frequency/2 Hz),"
                " (Less-than-or-equal-to, Feature-frequency/3 Hz))"
            ),
            "(Posterior-dominant-rhythm, (Approximately-equal-to, Feature-amplitude/40 uV))",
            "(Epileptiform-interictal-activity, Left, Right, Asymmetrical, (Left, (Greater-than, Right)))",
            "(Epileptiform-interictal-activity, Left, Right, Symmetrical)",
            (
                "(Epileptiform-interictal-activity, Sensor-list/F7;T7;TP7,"
                " (Sensor-list/T7, (Greater-than, Sensor-list/F7;TP7)))"
            ),
        ],
    )
    def test_a_compared_number_is_accepted(self, value):
        accepts(value)

    def test_a_compared_text_value_is_still_refused(self):
        refuses("(Spike, (Approximately-equal-to, Feature-stopped-by/sleep))", "text value")


class TestSensorList:
    @pytest.mark.parametrize("value", ["Sensor-list/Fp1", "Sensor-list/F7;T7;P7", "Sensor-list/Fp1-F7"])
    def test_canonical_labels_are_accepted(self, value):
        accepts(value)

    @pytest.mark.parametrize(
        "label",
        ["EEG Fp1-REF", "Fp1-A1", "T3", "fp1", "MISC_3", "Cz'", "LOC", "patient"],
    )
    def test_a_raw_or_unresolved_label_is_refused(self, label):
        refuses(f"Sensor-list/{label}", "not a canonical channel label")

    def test_an_empty_entry_is_refused(self):
        refuses("Sensor-list/F7;;T7", "not a canonical channel label")


class TestStructure:
    @pytest.mark.parametrize("value", ["Spike,", ", Spike", "Spike,,Left", "()", "Spike, ()"])
    def test_empty_tags_and_groups_are_refused(self, value):
        refuses(value, "empty")

    @pytest.mark.parametrize("value", ["(Spike", "Spike)", "(Spike))", "Spike (Left)"])
    def test_malformed_groups_are_refused(self, value):
        refuses(value, "parentheses|malformed")

    def test_nested_groups_are_read(self):
        accepts("Spike, (Left, (Sensor-list/F7))")

    def test_nesting_up_to_the_cap_is_read(self):
        accepts("(" * MAX_DEPTH + "Spike" + ")" * MAX_DEPTH)

    def test_deeper_nesting_is_refused_rather_than_recursed(self):
        depth = (MAX_LENGTH - len("Spike")) // 2
        refuses("(" * depth + "Spike" + ")" * depth, "nest at most")

    def test_the_length_is_capped(self):
        refuses(", ".join(["Spike"] * (MAX_LENGTH // 5)), "at most")

    def test_the_value_must_be_a_string(self):
        refuses(3, "non-empty string")


class TestMeta:
    @pytest.mark.parametrize(
        "meta",
        [
            None,
            "free text",
            {"schema": {"library": "score", "version": "1.0.0", "with_standard": "8.2.0"}},
            {**PIN, "note": "the patient's name"},
        ],
    )
    def test_meta_must_be_the_pin_exactly(self, meta):
        with pytest.raises(ValueError, match="schema pin"):
            validate_hed("Spike", meta)


class TestAllowedBranches:
    def test_a_branch_outside_the_allowlist_is_refused(self, settings):
        settings.HEDSCORE_ALLOWED_BRANCHES = ["Modulator"]
        refuses("Spike", "does not accept")
        accepts("Hyperventilation-good-effort, Intermittent-photic-stimulation/10 Hz")

    def test_an_empty_allowlist_accepts_every_branch(self, settings):
        settings.HEDSCORE_ALLOWED_BRANCHES = []
        accepts("Spike, Speak")
