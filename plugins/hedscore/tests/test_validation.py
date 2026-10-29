"""The ``hed`` validator: what a code's value may carry under the rules that keep it a confined vocabulary."""

import pytest

from plugins.hedscore.schema import SCHEMA_PIN, load_schema
from plugins.hedscore.validation import (
    MAX_DEPTH,
    MAX_LENGTH,
    MAX_NUMBERS,
    MAX_TAGS,
    NUMERIC_CLASS,
    NUMERIC_DOMAINS,
    canonicalise_hed,
    validate_hed,
)

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
            "Intermittent-photic-stimulation/2.5 Hz",
            "Feature-amplitude/150 uV",
            "Feature-amplitude/0 uV",
            "Feature-frequency/0.25 Hz",
            "Episode-event-count/3",
            "Number-of-RPP-phases/2",
            "Oxygen-saturation/88",
            "ECG-QT-period/420 ms",
            "Time-interval/1.5 s",
            "Percentage/30",
        ],
    )
    def test_a_numeric_value_in_its_domain_is_accepted(self, value):
        accepts(value)

    def test_a_number_is_required(self):
        refuses("Intermittent-photic-stimulation/fast", "written plainly")

    @pytest.mark.parametrize(
        ("value", "match"),
        [
            ("Intermittent-photic-stimulation/10", "written in Hz"),
            ("Intermittent-photic-stimulation/10 s", "written in Hz"),
            ("Feature-amplitude/0.15 mV", "written in uV"),
            ("ECG-QT-period/0.42 s", "written in ms"),
            ("Oxygen-saturation/88 Hz", "without a unit"),
        ],
    )
    def test_a_value_is_written_in_its_one_unit(self, value, match):
        refuses(value, match)

    @pytest.mark.parametrize(
        "number",
        ["0150", "150.0", "1.50", "+150", "-150", "1.5e2", ".5", "150.", "1 500", "150,5"],
    )
    def test_a_number_has_one_spelling(self, number):
        refuses(f"Feature-amplitude/{number} uV", "written plainly|written in")

    @pytest.mark.parametrize(
        ("value", "match"),
        [
            ("Feature-amplitude/10001 uV", "from 0 to 10000"),
            ("Feature-amplitude/19850312 uV", "from 0 to 10000"),
            ("Number-of-RPP-phases/4", "from 1 to 3"),
            ("Number-of-RPP-phases/0", "from 1 to 3"),
            ("Oxygen-saturation/101", "from 0 to 100"),
        ],
    )
    def test_a_value_outside_its_range_is_refused(self, value, match):
        refuses(value, match)

    @pytest.mark.parametrize(
        "value",
        ["Feature-amplitude/150.5 uV", "Feature-frequency/2.125 Hz", "Intermittent-photic-stimulation/10.25 Hz"],
    )
    def test_a_value_finer_than_its_decimals_is_refused(self, value):
        refuses(value, "decimals")

    @pytest.mark.parametrize("value", ["Age/67 year", "Weight/70 kg", "Item-count/3", "Temperature/37 oC"])
    def test_a_numeric_placeholder_without_a_domain_is_refused(self, value):
        refuses(value, "not accepted")

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


class TestSize:
    def test_the_tag_cap(self):
        accepts(", ".join(["Spike"] * MAX_TAGS))
        refuses(", ".join(["Spike"] * (MAX_TAGS + 1)), f"at most {MAX_TAGS} tags")

    def test_the_number_cap(self):
        accepts(", ".join(["Feature-amplitude/100 uV"] * MAX_NUMBERS))
        refuses(", ".join(["Feature-amplitude/100 uV"] * (MAX_NUMBERS + 1)), f"at most {MAX_NUMBERS} numeric")

    def test_a_sensor_list_names_a_channel_once(self):
        refuses("Sensor-list/F7;T7;F7", "each channel once")


class TestNumericDomains:
    """The domain table against the pinned schema, so a release that moves a placeholder fails here."""

    def test_every_domain_names_a_numeric_placeholder_in_its_own_unit_class(self):
        index = load_schema()
        for name, domain in NUMERIC_DOMAINS.items():
            term = index.terms[name]
            assert term.takes_value and NUMERIC_CLASS in term.value_classes, name
            assert not term.deprecated and not term.reserved, name
            if term.unit_classes:
                units = set().union(*(index.units[unit_class] for unit_class in term.unit_classes))
                assert domain.unit in units, name
            else:
                assert domain.unit is None, name
            assert domain.low <= domain.high, name

    def test_every_score_numeric_placeholder_has_a_domain(self):
        index = load_schema()
        score = {
            term.name
            for term in index.terms.values()
            if term.in_library and term.takes_value and NUMERIC_CLASS in term.value_classes and not term.deprecated
        }
        assert score <= set(NUMERIC_DOMAINS)

    def test_the_bounds_and_decimals_are_accepted_exactly(self):
        for name, domain in NUMERIC_DOMAINS.items():
            unit = f" {domain.unit}" if domain.unit else ""
            for bound in (domain.low, domain.high):
                accepts(f"{name}/{bound.normalize():f}{unit}")
            if domain.decimals:
                accepts(f"{name}/{domain.low + 1}.{'1' * domain.decimals}{unit}")


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


class TestFindingGroup:
    @pytest.mark.parametrize(
        "value", ["ID/100", "ID/512", "ID/999", "(Epileptiform-interictal-activity, Left, ID/104)"]
    )
    def test_a_group_number_is_accepted(self, value):
        accepts(value)

    @pytest.mark.parametrize(
        "number", ["0", "7", "99", "099", "0100", "1000", "-100", "100.0", "1e2", "١٠٠", "Anna", ""]
    )
    def test_any_other_value_is_refused(self, number):
        refuses(f"ID/{number}", "finding group's number")


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


class TestCanonicalForm:
    @pytest.mark.parametrize(
        ("written", "canonical"),
        [
            ("Spike", "Spike"),
            ("(Spike, Left)", "(Spike, Left)"),
            (" Spike ,Left,  Sensor-list/F7;T7 ", "Spike, Left, Sensor-list/F7;T7"),
            ("(Spike,\tLeft\n, Feature-frequency/10   Hz)", "(Spike, Left, Feature-frequency/10 Hz)"),
            ("(Spike, Spike, Left, Spike)", "(Spike, Left)"),
            ("(Spike, (Left, Right), (Left, Right))", "(Spike, (Left, Right))"),
            ("((Spike))", "Spike"),
            ("(Spike, (Left), ((Sensor-list/F7)))", "(Spike, Left, Sensor-list/F7)"),
            ("(Spike, (Left, (Greater-than, Right)))", "(Spike, (Left, (Greater-than, Right)))"),
        ],
    )
    def test_one_spelling_per_meaning(self, written, canonical):
        assert canonicalise_hed(written) == canonical

    def test_the_order_of_the_tags_is_kept(self):
        assert canonicalise_hed("(Left, Spike)") == "(Left, Spike)"

    def test_the_canonical_form_is_a_fixed_point(self):
        value = "(Spike, Left, (Left, (Greater-than, Right)), Sensor-list/F7;T7, ID/104)"
        assert canonicalise_hed(value) == value
        assert canonicalise_hed(canonicalise_hed(" ( Spike ,Left , (Left) ) ")) == canonicalise_hed(
            " ( Spike ,Left , (Left) ) "
        )

    @pytest.mark.parametrize("value", ["(Spike", "Spike, ()", "Spike (Left)", "", "  ", 3])
    def test_what_cannot_be_read_is_refused(self, value):
        with pytest.raises(ValueError):
            canonicalise_hed(value)

    def test_the_length_is_capped_before_the_string_is_read(self):
        with pytest.raises(ValueError, match="at most"):
            canonicalise_hed("(" * (MAX_LENGTH + 1))

    def test_the_registered_vocabulary_stores_the_canonical_form(self):
        from annotations.vocabularies import validate_code

        assert validate_code("hed", " (Spike,  Spike, (Left)) ", PIN) == "(Spike, Left)"


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
