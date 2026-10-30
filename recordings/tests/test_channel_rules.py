"""The channel rule tables: what names the sensors of a sleep study, and what keeps the names controlled.

The de-identification pass writes the name a rule gives into the stored file and replaces anything unnamed with
``MISC_<n>``, so a rule decides both what a reader can tell a channel is and what leaves the source's naming behind.
"""

import json
import logging
import struct

import pytest
from django.core.management import call_command
from django.core.management.base import SystemCheckError
from django.test import override_settings

from recordings.processors import channel_labels
from recordings.processors.channel_labels import (
    _SHIPPED_RULES,
    _SHIPPED_TYPES,
    AUXILIARY_TYPES,
    ChannelRuleError,
    classify_channel,
    derivation_label,
    parse_channel_rules,
    site_rules_digest,
)
from recordings.processors.edf import (
    _SW_LABEL,
    deidentify_signal_infos,
    extract_signal_type,
    parse_edf_header,
    parse_signal_infos,
    process_edf_file,
)
from recordings.tasks import _determine_modality


def _classified(label, **fields):
    return classify_channel(label, extract_signal_type(label), **fields)


# Spellings of one sensor as exports write them, with the name each must reach.
SENSOR_CASES = [
    ("Resp oro-nasal", ("resp", "Resp oronasal")),
    ("Thermistor", ("resp", "Resp oronasal")),
    ("Oral Therm", ("resp", "Resp oronasal")),
    ("Nasal Pressure", ("resp", "Resp nasal")),
    ("NasalP", ("resp", "Resp nasal")),
    ("PTAF", ("resp", "Resp nasal")),
    ("Pres", ("resp", "Resp nasal")),
    ("Flow Patient", ("resp", "Resp")),
    ("AIRFLOW", ("resp", "Resp")),
    ("CFlow", ("resp", "Resp PAP")),
    ("CPAP", ("resp", "PAP pressure")),
    ("THOR RES", ("resp", "Thorax")),
    ("Effort THO", ("resp", "Thorax")),
    ("Effort-THO", ("resp", "Thorax")),
    ("RIP Thora", ("resp", "Thorax")),
    ("Resp thorax", ("resp", "Thorax")),
    ("ABDO RES", ("resp", "Abdomen")),
    ("Effort ABD", ("resp", "Abdomen")),
    ("Resp abdomen", ("resp", "Abdomen")),
    ("RIP Sum", ("resp", "Resp sum")),
    ("EtCO2", ("resp", "EtCO2")),
    ("SaO2 finger", ("spo2", "SpO2")),
    ("SpO 2", ("spo2", "SpO2")),
    ("OX stat", ("misc", "SpO2 status")),
    ("Pleth", ("ppg", "PPG")),
    ("H.R.", ("misc", "HR")),
    ("Snore", ("misc", "Snore")),
    ("Mic", ("misc", "Sound")),
    ("POSITION", ("misc", "Position")),
    ("Temp rectal", ("temp", "Temp")),
    ("Flusso", ("resp", "Resp")),
    ("TERMISTORE", ("resp", "Resp oronasal")),
    ("TORACE", ("resp", "Thorax")),
    ("ADDOME", ("resp", "Abdomen")),
    ("ADDDOME", ("resp", "Abdomen")),
    ("Posizione", ("misc", "Position")),
    ("Light sternum", ("misc", "Light")),
    ("Event button", ("trig", "Event")),
]

# The electrophysiological channels of a study, in the spellings exports write them.
POLYGRAPHIC_CASES = [
    ("C4:M1", ("eeg", "C4")),
    ("F4:M1", ("eeg", "F4")),
    ("EOG(L)", ("eog", "LOC")),
    ("EOG(R)", ("eog", "ROC")),
    ("EOGl:M2", ("eog", "LOC")),
    ("Left Eye", ("eog", "LOC")),
    ("Right Eye", ("eog", "ROC")),
    ("Chin EMG", ("emg", "EMG/Chin")),
    ("Chin3", ("emg", "EMG/Chin")),
    ("L-Leg", ("emg", "EMG/LegL")),
    ("R-Leg", ("emg", "EMG/LegR")),
    ("LLeg1", ("emg", "EMG/LegL")),
    ("RLeg1", ("emg", "EMG/LegR")),
    ("LLeg1-LLeg2", ("emg", "EMG/LegL")),
    ("TIB Dx", ("emg", "EMG/LegR")),
    ("TIB Sx", ("emg", "EMG/LegL")),
]


@pytest.mark.parametrize(("label", "expected"), SENSOR_CASES + POLYGRAPHIC_CASES)
def test_a_spelling_reaches_its_name(label, expected):
    assert _classified(label) == expected


@pytest.mark.parametrize("label", ["Sensor 7", "EEG1", "Flow X", "LAT", "Leg", "PR", "DX1-DX2", "SX1-SX2"])
def test_an_unknown_or_ambiguous_label_stays_unnamed(label):
    assert _classified(label)[1] == ""


class TestConditions:
    def test_a_thermal_transducer_names_a_flow_channel(self):
        assert _classified("Flow", transducer="Thermistor") == ("resp", "Resp oronasal")

    def test_a_pressure_transducer_names_a_flow_channel(self):
        assert _classified("Flow", transducer="Nasal cannula") == ("resp", "Resp nasal")
        assert _classified("Airflow", transducer="PRESSURE TRANSDUCER") == ("resp", "Resp nasal")

    def test_a_pressure_unit_names_a_flow_channel(self):
        assert _classified("Flow", unit="cmH2O") == ("resp", "Resp nasal")

    def test_a_transducer_written_in_another_language_still_says_pressure(self):
        assert _classified("Flusso", transducer="Pressione") == ("resp", "Resp nasal")

    def test_an_oximeter_status_line_is_told_from_a_trigger_by_its_transducer(self):
        assert _classified("STAT", transducer="Oximeter") == ("misc", "SpO2 status")
        assert _classified("STAT")[1] == ""

    def test_a_flow_channel_with_no_evidence_stays_generic(self):
        assert _classified("Flow") == ("resp", "Resp")
        assert _classified("Flow", unit="uV", transducer="AgAgCl") == ("resp", "Resp")


class TestTable:
    def test_every_rule_names_an_auxiliary_type(self):
        assert {rule.signal_type for rule in _SHIPPED_RULES} <= AUXILIARY_TYPES

    def test_one_name_has_one_type(self):
        for rule in _SHIPPED_RULES:
            assert _SHIPPED_TYPES[rule.canonical] == rule.signal_type

    @pytest.mark.parametrize("canonical", sorted(_SHIPPED_TYPES))
    def test_a_stored_name_reads_back_as_itself_with_nothing_else_known(self, canonical):
        # The pass blanks the transducer, and the serving middleware classifies the stored label on every download.
        assert _classified(canonical) == (_SHIPPED_TYPES[canonical], canonical)

    @pytest.mark.parametrize("canonical", sorted(_SHIPPED_TYPES))
    def test_a_name_fits_the_label_field(self, canonical):
        assert len(canonical) <= _SW_LABEL

    def test_a_derived_copy_keeps_its_name_where_the_name_fits(self):
        assert _classified("Thorax_orig") == ("misc", "Thorax_orig")

    @pytest.mark.parametrize("label", ["Thermistor_orig", "PAP pressure_orig"])
    def test_a_derived_copy_too_long_to_store_stays_unnamed(self, label):
        # Stored cut short, the name would come back as something else on the next pass.
        assert _classified(label) == ("misc", "")

    def test_a_conditional_rule_precedes_the_unconditional_one_for_its_label(self):
        seen: set[str] = set()
        for rule in _SHIPPED_RULES:
            if rule.units or rule.transducers:
                assert not rule.labels & seen, f"{rule.canonical} can never fire"
            else:
                seen |= rule.labels

    @pytest.mark.parametrize(
        "rule",
        [
            {"type": "resp", "canonical": "X", "label": []},
            {"type": "eeg", "canonical": "X", "label": ["X"]},
            {"type": "resp", "canonical": "", "label": ["X"]},
            {"type": "resp", "canonical": "X", "label": ["X"], "units": ["uV"]},
            {"type": "resp", "canonical": "X", "label": ["X"], "transducer": "therm"},
        ],
    )
    def test_a_malformed_rule_is_refused(self, rule):
        with pytest.raises(ChannelRuleError):
            parse_channel_rules({"rules": [rule]}, "test")


class TestAuxiliaryTypesStayOutOfTheVote:
    class _Signal:
        is_annotation_channel = False

        def __init__(self, signal_type):
            self.signal_type = signal_type

    def test_a_study_is_not_voted_a_respiration_recording(self):
        signals = [self._Signal(t) for t in ["eeg", "eeg", "resp", "resp", "resp", "resp", "spo2", "ppg", "temp"]]
        assert _determine_modality(signals) == "eeg"


@pytest.fixture
def site_table(tmp_path):
    def write(rules):
        path = tmp_path / "site.json"
        path.write_text(json.dumps({"rules": rules}), encoding="utf-8")
        return path

    channel_labels._cached_site_tables.cache_clear()
    yield write
    channel_labels._cached_site_tables.cache_clear()


class TestSiteRules:
    def test_a_site_teaches_a_new_spelling(self, site_table):
        path = site_table([{"canonical": "Resp nasal", "label": ["Sensor 7"]}])
        with override_settings(RECORDING_CHANNEL_RULES=[str(path)]):
            assert _classified("Sensor 7") == ("resp", "Resp nasal")
        assert _classified("Sensor 7")[1] == ""

    def test_a_site_rule_precedes_the_shipped_ones(self, site_table):
        path = site_table([{"canonical": "Resp oronasal", "label": ["Flow"]}])
        with override_settings(RECORDING_CHANNEL_RULES=[str(path)]):
            assert _classified("Flow") == ("resp", "Resp oronasal")

    def test_a_site_cannot_name_a_channel_its_own_way(self, site_table):
        path = site_table([{"canonical": "Ward 7 belt", "label": ["Belt"]}])
        with override_settings(RECORDING_CHANNEL_RULES=[str(path)]):
            with pytest.raises(SystemCheckError, match="recordings.E003"):
                call_command("check")

    def test_a_site_rule_cannot_set_a_type(self, site_table):
        path = site_table([{"type": "eeg", "canonical": "Resp nasal", "label": ["Belt"]}])
        with override_settings(RECORDING_CHANNEL_RULES=[str(path)]):
            with pytest.raises(SystemCheckError, match="recordings.E003"):
                call_command("check")

    def test_a_missing_table_stops_the_check(self, tmp_path):
        with override_settings(RECORDING_CHANNEL_RULES=[str(tmp_path / "absent.json")]):
            with pytest.raises(SystemCheckError, match="recordings.E003"):
                call_command("check")

    def test_a_table_broken_under_a_running_worker_fails_closed(self, site_table, caplog):
        path = site_table([{"canonical": "Ward 7 belt", "label": ["Sensor 7"]}])
        with override_settings(RECORDING_CHANNEL_RULES=[str(path)]), caplog.at_level(logging.ERROR):
            assert _classified("Sensor 7")[1] == ""
            assert _classified("Thorax") == ("resp", "Thorax")
        assert "site table refused" in caplog.text

    def test_the_digest_names_the_tables_in_force(self, site_table):
        assert site_rules_digest() == ""
        path = site_table([{"canonical": "Resp nasal", "label": ["Sensor 7"]}])
        with override_settings(RECORDING_CHANNEL_RULES=[str(path)]):
            assert len(site_rules_digest()) == 64

    def test_an_edit_under_a_running_worker_does_not_move_the_digest(self, site_table):
        # The record says what named the channels, and that is what the process has loaded.
        path = site_table([{"canonical": "Resp nasal", "label": ["Sensor 7"]}])
        with override_settings(RECORDING_CHANNEL_RULES=[str(path)]):
            before = site_rules_digest()
            site_table([{"canonical": "Resp nasal", "label": ["Sensor 8"]}])
            assert site_rules_digest() == before
            assert _classified("Sensor 7") == ("resp", "Resp nasal")

    def test_a_refused_table_is_not_sealed(self, site_table):
        path = site_table([{"canonical": "Ward 7 belt", "label": ["Sensor 7"]}])
        with override_settings(RECORDING_CHANNEL_RULES=[str(path)]):
            assert site_rules_digest() == ""


def _psg_file(path, signals):
    """Write a plain EDF of one second with one ``(label, unit, transducer, samples)`` per signal."""
    fields = [
        (16, [s[0] for s in signals]),
        (80, [s[2] for s in signals]),
        (8, [s[1] for s in signals]),
        (8, ["-1000"] * len(signals)),
        (8, ["1000"] * len(signals)),
        (8, ["-32768"] * len(signals)),
        (8, ["32767"] * len(signals)),
        (80, [""] * len(signals)),
        (8, [str(s[3]) for s in signals]),
        (32, [""] * len(signals)),
    ]
    head = (
        f"{'0':<8}{'Subject':<80}{'Startdate':<80}01.01.2601.00.00"
        f"{256 + 256 * len(signals):<8}{'':<44}{'1':<8}{'1':<8}{len(signals):<4}"
    )
    block = "".join(f"{value:<{width}}" for width, values in fields for value in values)
    samples = b"".join(struct.pack("<h", 0) * s[3] for s in signals)
    path.write_bytes(head.encode("ascii") + block.encode("ascii") + samples)


class TestThroughThePass:
    SIGNALS = [
        ("C4:M1", "uV", "AgAgCl", 256),
        ("EOG(L)", "uV", "", 256),
        ("EOG(R)", "uV", "", 256),
        ("Chin EMG", "uV", "", 256),
        ("Flow", "", "Thermistor", 32),
        ("Pres", "cmH2O", "", 32),
        ("Effort THO", "", "RIP belt", 32),
        ("SaO2", "%", "Site probe model 7", 1),
        ("Vendor spare 3", "", "", 1),
    ]

    def test_the_stored_file_says_what_each_channel_is_and_nothing_of_the_source(self, tmp_path):
        path = tmp_path / "study.edf"
        _psg_file(path, self.SIGNALS)
        process_edf_file(path)
        stored = path.read_bytes()
        header = parse_edf_header(stored)
        infos = parse_signal_infos(stored, header)
        assert {i.label for i in infos} == {
            "C4",
            "LOC",
            "ROC",
            "EMG/Chin",
            "Resp oronasal",
            "Resp nasal",
            "Thorax",
            "SpO2",
            "MISC_1",
        }
        assert b"Thermistor" not in stored
        assert b"model 7" not in stored
        assert b"Vendor spare" not in stored

    def test_a_stored_name_without_a_study_flag_is_unchanged(self, tmp_path):
        path = tmp_path / "study.edf"
        _psg_file(path, self.SIGNALS)
        process_edf_file(path)
        stored = path.read_bytes()
        assert "C4" in [i.label for i in parse_signal_infos(stored, parse_edf_header(stored))]

    def test_a_second_pass_reproduces_the_stored_header(self, tmp_path):
        # The stored transducer is blank, so a name a transducer decided has to survive without it.
        path = tmp_path / "study.edf"
        _psg_file(path, self.SIGNALS)
        process_edf_file(path)
        stored = path.read_bytes()
        header = parse_edf_header(stored)
        infos = parse_signal_infos(stored, header)
        labels = [i.label for i in infos]
        types = [i.signal_type for i in infos]
        deidentify_signal_infos(infos)
        assert [i.label for i in infos] == labels
        assert [i.signal_type for i in infos] == types


class TestKeepReference:
    SIGNALS = [
        ("C4-M1", "uV", "", 256),
        ("EEG C3-A2", "uV", "", 256),
        ("EEG Fpz-Cz", "uV", "", 256),
        ("O2-Ref", "uV", "", 256),
        ("F4", "uV", "", 256),
        ("E1-M2", "uV", "", 256),
        ("ROC-LOC", "uV", "", 256),
        ("Chin1-Chin2", "uV", "", 256),
        ("ECG1-ECG2", "mV", "", 256),
    ]

    @pytest.mark.parametrize(
        ("label", "signal_type", "expected"),
        [
            ("C4-M1", "eeg", "C4-M1"),
            ("EEG C3-A2", "eeg", "C3-A2"),
            ("C4:M1", "eeg", "C4-M1"),
            ("EEG Fpz-Cz", "eeg", "Fpz-Cz"),
            ("T3-M2", "eeg", "T7-M2"),
            ("C4-Ref", "eeg", ""),
            ("C4", "eeg", ""),
            ("Smith-M1", "eeg", ""),
            ("C4-Smith", "eeg", ""),
            ("E1-M2", "eog", "LOC-M2"),
            ("ROC-LOC", "eog", "ROC-LOC"),
            ("EOG-L", "eog", ""),
            ("Chin1-Chin2", "emg", ""),
        ],
    )
    def test_a_derivation_is_two_vocabulary_tokens(self, label, signal_type, expected):
        assert derivation_label(label, signal_type) == expected

    def _stored(self, tmp_path, *, keep_reference):
        path = tmp_path / "study.edf"
        _psg_file(path, self.SIGNALS)
        process_edf_file(path, keep_reference=keep_reference)
        stored = path.read_bytes()
        header = parse_edf_header(stored)
        return header, parse_signal_infos(stored, header)

    def test_a_study_stores_what_was_recorded(self, tmp_path):
        _, infos = self._stored(tmp_path, keep_reference=True)
        labels = [i.label for i in infos]
        assert labels.count("C4-M1") == 1
        for expected in ("C3-A2", "Fpz-Cz", "O2", "F4", "LOC-M2", "ROC-LOC", "EMG/Chin", "ECG1-ECG2"):
            assert expected in labels

    def test_the_name_a_consumer_matches_on_stays_the_electrode(self, tmp_path):
        _, infos = self._stored(tmp_path, keep_reference=True)
        by_label = {i.label: i.canonical_label for i in infos}
        assert by_label["C4-M1"] == "C4"

    def test_a_second_pass_told_the_same_reproduces_the_header(self, tmp_path):
        _, infos = self._stored(tmp_path, keep_reference=True)
        labels = [i.label for i in infos]
        deidentify_signal_infos(infos, keep_reference=True)
        assert [i.label for i in infos] == labels

    def test_the_same_channels_lose_their_reference_outside_a_study(self, tmp_path):
        _, infos = self._stored(tmp_path, keep_reference=False)
        labels = [i.label for i in infos]
        assert "C4" in labels
        assert "C4-M1" not in labels

    def test_order_follows_the_primary_electrode(self, tmp_path):
        _, infos = self._stored(tmp_path, keep_reference=True)
        eeg = [i.label for i in infos if i.signal_type == "eeg"]
        assert eeg.index("C3-A2") < eeg.index("C4-M1")
