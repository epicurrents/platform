"""The channel rule tables: what names the sensors of a sleep study, and what keeps the names controlled.

The de-identification pass writes the name a rule gives into the stored file and replaces anything unnamed with
``MISC_<n>``, so a rule decides both what a reader can tell a channel is and what leaves the source's naming behind.
"""

import hashlib
import io
import json
import logging
import struct
from pathlib import Path
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.core.management.base import SystemCheckError
from django.test import TestCase, override_settings

from recordings.models import Recording
from recordings.processors import channel_labels
from recordings.processors.channel_labels import (
    _SHIPPED_RULES,
    _SHIPPED_TYPES,
    AUXILIARY_TYPES,
    ChannelMap,
    ChannelRuleError,
    classify_channel,
    derivation_label,
    get_channel_map,
    parse_channel_map,
    parse_channel_rules,
    site_rules_digest,
)
from recordings.processors.edf import (
    _SW_LABEL,
    _ascii_clean,
    _read_field,
    deidentify_signal_infos,
    extract_signal_type,
    parse_edf_header,
    parse_signal_infos,
    process_edf_file,
)
from recordings.tasks import _determine_modality
from recordings.tests.test_edf_processor import _make_edfplus_file


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
    ("Saturation", ("spo2", "SpO2")),
    ("Pulse Waveform", ("ppg", "PPG")),
    ("Audio", ("misc", "Sound")),
    ("CO2", ("resp", "Resp CO2")),
    ("Capnography", ("resp", "Resp CO2")),
    ("Resp Imp", ("resp", "Resp imp")),
    ("Respiratory Rate", ("resp", "Resp rate")),
    ("RIP Flow", ("resp", "Resp RIP flow")),
    ("Flow Limitation", ("resp", "Resp limit")),
    ("Thorax Raw", ("resp", "Thorax raw")),
    ("PTT", ("misc", "PTT")),
    ("X Axis", ("misc", "Accel X")),
    ("BCG", ("misc", "BCG")),
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
    ("Left Leg", ("emg", "EMG/LegL")),
    ("Right Leg", ("emg", "EMG/LegR")),
    ("EMG Leg", ("emg", "EMG/Leg")),
    ("Chin-Ref", ("emg", "EMG/Chin")),
    ("EMG/ChinRef", ("emg", "EMG/ChinRef")),
    ("Diaphragm", ("emg", "EMG/Diaph")),
    ("EMG", ("emg", "EMG")),
]


@pytest.mark.parametrize(("label", "expected"), SENSOR_CASES + POLYGRAPHIC_CASES)
def test_a_spelling_reaches_its_name(label, expected):
    assert _classified(label) == expected


@pytest.mark.parametrize("label", ["Sensor 7", "EEG1", "Flow X", "LAT", "Leg", "PR", "DX1-DX2", "SX1-SX2"])
def test_an_unknown_or_ambiguous_label_stays_unnamed(label):
    assert _classified(label)[1] == ""


@pytest.mark.parametrize(
    "label",
    ["Flusso", "Termistore", "Torace", "Addome", "Adddome", "Posizione", "TIB Sx", "TIB Dx", "Leuka", "Jalka/L"],
)
def test_a_local_spelling_is_left_to_a_channel_map(label):
    assert _classified(label)[1] == ""


def test_a_bare_pressure_is_not_called_nasal():
    # Therapy and raw transducer pressures are written the same way, and the difference decides how an event is scored.
    assert _classified("Pressure")[1] == ""


class TestConditions:
    def test_a_thermal_transducer_names_a_flow_channel(self):
        assert _classified("Flow", transducer="Thermistor") == ("resp", "Resp oronasal")

    def test_a_pressure_transducer_names_a_flow_channel(self):
        assert _classified("Flow", transducer="Nasal cannula") == ("resp", "Resp nasal")
        assert _classified("Airflow", transducer="PRESSURE TRANSDUCER") == ("resp", "Resp nasal")

    def test_a_pressure_unit_names_a_flow_channel(self):
        assert _classified("Flow", unit="cmH2O") == ("resp", "Resp nasal")

    def test_a_transducer_written_in_another_language_still_says_pressure(self):
        assert _classified("Flow", transducer="Pressione") == ("resp", "Resp nasal")

    def test_an_oximeter_status_line_is_told_from_a_trigger_by_its_transducer(self):
        assert _classified("STAT", transducer="Oximeter") == ("misc", "SpO2 status")
        assert _classified("STAT")[1] == ""

    def test_a_flow_channel_with_no_evidence_stays_generic(self):
        assert _classified("Flow") == ("resp", "Resp")
        assert _classified("Flow", unit="uV", transducer="AgAgCl") == ("resp", "Resp")


class TestHeaderEncoding:
    @pytest.mark.parametrize(
        ("raw", "expected"), [(b"\xc2\xb0", "deg"), (b"\xb0", "deg"), (b"\xc2\xb5V", "uV"), (b"\xb5V", "uV")]
    )
    def test_a_unit_survives_either_encoding(self, raw, expected):
        assert _ascii_clean(_read_field(raw.ljust(8), 0, 8)) == expected


class TestImpedance:
    @pytest.mark.parametrize("label", ["E1-M2 (Imp)", "EKG Impedance", "C3-M2 (Imp)", "1-2 (Imp)", "Left Leg Impedan"])
    def test_a_unit_of_resistance_names_an_impedance_trace(self, label):
        assert _classified(label, unit="Ohm") == ("misc", "Impedance")
        assert _classified(label, unit="kOhm") == ("misc", "Impedance")

    @pytest.mark.parametrize(("label", "expected"), [("LOC", ("eog", "LOC")), ("MISC_4", ("", ""))])
    def test_a_stored_name_stays_what_an_earlier_pass_wrote(self, label, expected):
        # The serving middleware classifies the stored file on every download, with its unit intact.
        assert _classified(label, unit="Ohm") == expected

    def test_a_respiration_signal_measured_by_impedance_is_a_signal(self):
        assert _classified("Resp Imp", unit="V") == ("resp", "Resp imp")


class TestEmgTransducer:
    @pytest.mark.parametrize("label", ["Leuka", "Jalka/L", "Pallea pysty", "EEG Muscle", "EMG Tibialis"])
    def test_a_label_naming_nothing_takes_the_modality_the_transducer_states(self, label):
        assert _classified(label, transducer="EMG") == ("emg", "EMG")

    @pytest.mark.parametrize(
        ("label", "expected"), [("Chin", "EMG/Chin"), ("C3", "C3"), ("Snore", "Snore"), ("EKG", "ECG")]
    )
    def test_a_label_that_names_the_channel_outweighs_the_field(self, label, expected):
        assert _classified(label, transducer="EMG")[1] == expected

    def test_the_word_must_stand_alone(self):
        assert _classified("Leuka", transducer="Temgard probe") == ("", "")


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


def _channel_map(rules, key="source"):
    return parse_channel_map({"rules": rules}, "test", key=key, digest="d" * 64)


class TestChannelMaps:
    def test_a_label_is_matched_whole_and_in_either_case(self):
        mapped = _channel_map([{"canonical": "EMG/Chin", "label": ["Leuka"]}])
        assert classify_channel("LEUKA", channel_map=mapped) == ("emg", "EMG/Chin")
        assert classify_channel("Leuka ref", channel_map=mapped) == ("", "")

    def test_a_map_decides_before_every_inference(self):
        mapped = _channel_map([{"canonical": "EMG/Chin", "label": ["1", "F3"]}])
        assert classify_channel("F3", "eeg", channel_map=mapped) == ("emg", "EMG/Chin")
        assert classify_channel("1", channel_map=mapped) == ("emg", "EMG/Chin")

    def test_separators_are_significant(self):
        mapped = _channel_map([{"canonical": "EMG/Chin", "label": ["1-2"]}])
        assert classify_channel("1 2", channel_map=mapped) == ("", "")

    def test_a_condition_narrows_the_rule_and_a_miss_falls_through(self):
        mapped = _channel_map([{"canonical": "Resp nasal", "label": ["Virta"], "unit": ["cmH2O"]}])
        assert classify_channel("Virta", unit="cmH2O", channel_map=mapped) == ("resp", "Resp nasal")
        assert classify_channel("Virta", unit="V", channel_map=mapped) == ("", "")

    @pytest.mark.parametrize("encoding", ["latin-1", "utf-8"])
    def test_an_accented_label_matches_in_either_header_encoding(self, encoding):
        mapped = _channel_map([{"canonical": "Position", "label": ["Asento pää"]}])
        field = "Asento pää".encode(encoding).ljust(_SW_LABEL)
        assert classify_channel(_read_field(field, 0, _SW_LABEL), channel_map=mapped) == ("misc", "Position")

    @pytest.mark.parametrize(
        "canonical",
        [
            "EMG/Chin",
            "EMG/Leg",
            "EMG/Diaph",
            "LOC",
            "ECG",
            "II",
            "C4",
            "A1",
            "Resp CO2",
            "Position manual",
            "Impedance",
        ],
    )
    def test_every_kind_of_shipped_name_is_a_target(self, canonical):
        mapped = _channel_map([{"canonical": canonical, "label": ["Sensor"]}])
        assert classify_channel("Sensor", channel_map=mapped) == (_SHIPPED_TYPES[canonical], canonical)

    @pytest.mark.parametrize(
        "rule",
        [
            {"canonical": "Ward 7 belt", "label": ["Belt"]},
            {"type": "eeg", "canonical": "Thorax", "label": ["Belt"]},
            {"canonical": "Thorax", "label": ["A label longer than the field"]},
            {"canonical": "Thorax", "label": ["   "]},
            {"canonical": "Thorax", "label": []},
        ],
    )
    def test_an_unusable_rule_is_refused(self, rule):
        with pytest.raises(ChannelRuleError):
            _channel_map([rule])

    @pytest.mark.parametrize("raw", [[], {"rule": []}, "rules"])
    def test_an_unusable_table_is_refused(self, raw):
        with pytest.raises(ChannelRuleError):
            parse_channel_map(raw, "test", key="source")

    def test_a_key_that_is_not_configured_is_refused(self):
        with pytest.raises(ChannelRuleError, match="No channel map"):
            get_channel_map("absent")

    def test_the_digest_is_of_the_bytes_read(self, tmp_path):
        path = tmp_path / "source.json"
        path.write_text(json.dumps({"rules": [{"canonical": "Thorax", "label": ["Rinta"]}]}), encoding="utf-8")
        channel_labels._read_channel_map.cache_clear()
        with override_settings(RECORDING_CHANNEL_MAPS={"source": str(path)}):
            assert get_channel_map("source").digest == hashlib.sha256(path.read_bytes()).hexdigest()
        channel_labels._read_channel_map.cache_clear()

    @pytest.mark.parametrize("table", ["{", json.dumps({"rules": [{"canonical": "Ward 7 belt", "label": ["Belt"]}]})])
    def test_the_check_refuses_a_map_it_cannot_use(self, tmp_path, table):
        path = tmp_path / "source.json"
        path.write_text(table, encoding="utf-8")
        channel_labels._read_channel_map.cache_clear()
        with override_settings(RECORDING_CHANNEL_MAPS={"source": str(path)}):
            with pytest.raises(SystemCheckError, match="recordings.E004"):
                call_command("check")
        channel_labels._read_channel_map.cache_clear()

    def test_the_check_refuses_a_key_it_would_seal(self, tmp_path):
        path = tmp_path / "source.json"
        path.write_text(json.dumps({"rules": []}), encoding="utf-8")
        channel_labels._read_channel_map.cache_clear()
        with override_settings(RECORDING_CHANNEL_MAPS={"Ward 7": str(path)}):
            with pytest.raises(SystemCheckError, match="recordings.E004"):
                call_command("check")
        channel_labels._read_channel_map.cache_clear()

    @pytest.mark.parametrize("key", ["italian", "nox", "finnish"])
    def test_a_shipped_example_parses(self, key):
        path = Path(channel_labels.__file__).resolve().parents[1] / "channel_maps" / f"{key}.json"
        assert isinstance(parse_channel_map(json.loads(path.read_bytes()), key, key=key), ChannelMap)

    def test_the_italian_labels_are_reachable_through_their_map(self):
        path = Path(channel_labels.__file__).resolve().parents[1] / "channel_maps" / "italian.json"
        mapped = parse_channel_map(json.loads(path.read_bytes()), "italian", key="italian")
        for label, expected in [
            ("FLUSSO", "Resp"),
            ("TERMISTORE", "Resp oronasal"),
            ("TORACE", "Thorax"),
            ("ADDDOME", "Abdomen"),
            ("Posizione", "Position"),
            ("TIB Sx", "EMG/LegL"),
        ]:
            assert classify_channel(label, channel_map=mapped)[1] == expected


@pytest.mark.django_db
class TestChannelMapEndpoints:
    URL = "/recordings/api/v1"

    def _upload(self, client, tmp_path, query=""):
        (tmp_path / "staging").mkdir(exist_ok=True)
        upload = io.BytesIO(_make_edfplus_file())
        upload.name = "study.edf"
        with (
            override_settings(RECORDINGS_STAGING_PATH=str(tmp_path / "staging")),
            patch("recordings.tasks.process_recording.delay") as delay,
            TestCase.captureOnCommitCallbacks(execute=True),
        ):
            response = client.post(f"{self.URL}/upload{query}", {"file": upload}, format="multipart")
        return response, delay

    @override_settings(RECORDING_CHANNEL_MAPS={"source": "a.json", "other": "b.json"})
    def test_the_keys_are_listed_and_nothing_else(self, auth_client):
        client, _ = auth_client
        response = client.get(f"{self.URL}/channel-maps")
        assert response.status_code == 200
        assert response.json() == ["other", "source"]

    def test_the_list_is_not_public(self, client):
        assert client.get(f"{self.URL}/channel-maps").status_code == 401

    @override_settings(RECORDING_CHANNEL_MAPS={"source": "a.json"})
    def test_the_upload_hands_the_task_the_key(self, auth_client, tmp_path):
        client, _ = auth_client
        response, delay = self._upload(client, tmp_path, "?channel_map=source")
        assert response.status_code == 202
        assert delay.call_args.kwargs["channel_map"] == "source"

    def test_an_unknown_map_is_refused_before_anything_is_staged(self, auth_client, tmp_path):
        client, _ = auth_client
        response, delay = self._upload(client, tmp_path, "?channel_map=absent")
        assert response.status_code == 400
        delay.assert_not_called()
        assert not list((tmp_path / "staging").iterdir())
        assert not Recording.objects.exists()


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


class TestMappedFile:
    SIGNALS = [
        ("Leuka", "uV", "EMG", 256),
        ("Kapno", "", "", 32),
        ("E1-M2 (Imp)", "Ohm", "", 1),
        ("Jalka/L", "uV", "EMG", 256),
        ("Vendor spare 3", "", "", 1),
    ]

    def _stored(self, tmp_path, channel_map=None):
        path = tmp_path / "study.edf"
        _psg_file(path, self.SIGNALS)
        result = process_edf_file(path, channel_map=channel_map)
        return path.read_bytes(), result

    def test_the_stored_file_holds_the_mapped_names_and_nothing_of_the_source(self, tmp_path):
        mapped = _channel_map(
            [{"canonical": "EMG/Chin", "label": ["Leuka"]}, {"canonical": "Resp CO2", "label": ["Kapno"]}]
        )
        stored, result = self._stored(tmp_path, mapped)
        infos = parse_signal_infos(stored, parse_edf_header(stored))
        assert {i.label for i in infos} == {"EMG/Chin", "EMG", "Resp CO2", "Impedance", "MISC_1"}
        for word in (b"Leuka", b"Kapno", b"Jalka", b"Vendor spare"):
            assert word not in stored
        assert {i.source_label for i in result.signal_infos} == {s[0] for s in self.SIGNALS}

    def test_the_stored_file_needs_no_map_to_read_back(self, tmp_path):
        mapped = _channel_map(
            [{"canonical": "EMG/Chin", "label": ["Leuka"]}, {"canonical": "Resp CO2", "label": ["Kapno"]}]
        )
        stored, _ = self._stored(tmp_path, mapped)
        infos = parse_signal_infos(stored, parse_edf_header(stored))
        labels = [i.label for i in infos]
        types = [i.signal_type for i in infos]
        deidentify_signal_infos(infos)
        assert [i.label for i in infos] == labels
        assert [i.signal_type for i in infos] == types

    def test_a_map_outweighs_the_derivation_its_label_spells(self, tmp_path):
        path = tmp_path / "study.edf"
        _psg_file(path, [("E1-M2", "uV", "", 256), ("E2-M1", "uV", "", 256), ("O1-M2", "uV", "", 256)])
        swapped = _channel_map(
            [{"canonical": "ROC", "label": ["E1-M2"]}, {"canonical": "LOC", "label": ["E2-M1"]}],
        )
        process_edf_file(path, keep_reference=True, channel_map=swapped)
        stored = path.read_bytes()
        labels = [i.label for i in parse_signal_infos(stored, parse_edf_header(stored))]
        assert sorted(labels) == ["LOC", "O1-M2", "ROC"]

    def test_a_channel_mapped_onto_an_electrode_it_collides_with_stays_unique(self, tmp_path):
        path = tmp_path / "study.edf"
        _psg_file(path, [("C4", "uV", "", 256), ("Fp1-A1", "uV", "", 256)])
        process_edf_file(path, channel_map=_channel_map([{"canonical": "C4", "label": ["Fp1-A1"]}]))
        stored = path.read_bytes()
        labels = [i.label for i in parse_signal_infos(stored, parse_edf_header(stored))]
        assert sorted(labels) == ["C4", "MISC_1"]

    def test_a_file_without_a_map_keeps_what_the_field_states(self, tmp_path):
        stored, _ = self._stored(tmp_path)
        labels = [i.label for i in parse_signal_infos(stored, parse_edf_header(stored))]
        assert labels.count("EMG") == 2
        assert "Impedance" in labels


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
