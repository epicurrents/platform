"""Tests for project-registered stored channel names (``channel_labels.register_channel_names``).

A registered name extends what a channel may be stored under, so the properties pinned here are the
ones a shipped name has: it resolves from the spellings a 10-10 electrode resolves from, it reads
back as itself from the stored file, it sorts deterministically, and the recording's record seals
which names were in force. The names are invented; the platform ships none.
"""

import json
import tempfile
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest
from django.apps import apps
from django.core.management import call_command
from django.test import override_settings

from recordings.checks import check_registered_channel_names
from recordings.deidentification_record import DEIDENTIFICATION_RECORD_KEY
from recordings.processors import channel_labels
from recordings.processors.channel_labels import ChannelRuleError, classify_channel, parse_channel_rules
from recordings.processors.edf import extract_signal_type, parse_edf_header, parse_signal_infos, process_edf_file
from recordings.tests.test_edf_processor import _make_edf_data, _make_edf_header

_TABLES = ("_REGISTERED_NAMES", "_SHIPPED_TYPES", "_SELF_RESOLVING", "_CANONICAL_BY_UPPER")


@pytest.fixture
def register(monkeypatch):
    """Register names into copies of the tables, restored after the test, as ``ready()`` would."""
    for attr in _TABLES:
        monkeypatch.setattr(channel_labels, attr, dict(getattr(channel_labels, attr)))

    def _register(names):
        with patch.object(apps, "ready", False):
            channel_labels.register_channel_names(names)

    return _register


@pytest.fixture
def registered(register):
    register({"Xq1": "eeg", "Xq2": "eeg", "Zqmark": "trig"})


def _signal(label, unit="uV"):
    return {
        "label": label,
        "unit": unit,
        "phys_min": -100.0,
        "phys_max": 100.0,
        "dig_min": -32768,
        "dig_max": 32767,
        "sample_count": 16,
    }


def _stored_labels(signals):
    """Process an EDF with *signals* as ingest does, and return the labels its header now carries."""
    content = _make_edf_header(signals=signals) + _make_edf_data(signals)
    with tempfile.NamedTemporaryFile(suffix=".edf", delete=False) as tmp:
        tmp.write(content)
        path = Path(tmp.name)
    try:
        process_edf_file(path)
        on_disk = path.read_bytes()
    finally:
        path.unlink(missing_ok=True)
    return [info.label for info in parse_signal_infos(on_disk, parse_edf_header(on_disk))]


class TestResolution:
    def test_an_unregistered_name_does_not_resolve(self):
        assert classify_channel("Xq1")[1] == ""

    @pytest.mark.usefixtures("registered")
    @pytest.mark.parametrize(
        ("label", "prior", "expected"),
        [
            ("Xq1", "", ("eeg", "Xq1")),
            ("xq1", "", ("eeg", "Xq1")),
            ("EEG Xq1-Ref", "eeg", ("eeg", "Xq1")),
            ("Xq1 Ref", "", ("eeg", "Xq1")),
            ("F7-Xq1", "", ("eeg", "F7-Xq1")),
            ("Xq1-Xq2", "eeg", ("eeg", "Xq1-Xq2")),
            ("Xq1_orig", "", ("misc", "Xq1_orig")),
            ("Zqmark", "", ("trig", "Zqmark")),
            ("EEG Zqmark-Ref", "eeg", ("trig", "Zqmark")),
        ],
    )
    def test_a_registered_name_resolves_as_a_shipped_one_does(self, label, prior, expected):
        assert classify_channel(label, prior) == expected

    @pytest.mark.usefixtures("registered")
    @pytest.mark.parametrize("name", ["Xq1", "Xq2", "F7-Xq1", "Zqmark", "Xq1_orig"])
    def test_a_stored_name_reads_back_as_itself(self, name):
        # Typed the way ingest and the serving middleware type a label: from its text.
        signal_type, canonical = classify_channel(name, extract_signal_type(name))
        assert classify_channel(canonical, extract_signal_type(canonical)) == (signal_type, canonical)

    @pytest.mark.usefixtures("registered")
    def test_the_shipped_vocabulary_is_unchanged(self):
        assert "Xq1" not in channel_labels._TEN_TEN_ELECTRODES
        assert classify_channel("Fp1") == ("eeg", "Fp1")

    def test_a_site_table_may_name_a_registered_name(self, register):
        register({"Xq1": "eeg"})
        (rule,) = parse_channel_rules({"rules": [{"canonical": "Xq1", "label": ["Xqone"]}]}, "site", site=True)
        assert (rule.signal_type, rule.canonical) == ("eeg", "Xq1")

    def test_a_site_table_read_before_registration_is_read_again(self, register, tmp_path):
        path = tmp_path / "site.json"
        path.write_text(json.dumps({"rules": [{"canonical": "Xq1", "label": ["Xqone"]}]}), encoding="utf-8")
        with override_settings(RECORDING_CHANNEL_RULES=[str(path)]):
            assert classify_channel("Xqone")[1] == ""
            register({"Xq1": "eeg"})
            assert classify_channel("Xqone") == ("eeg", "Xq1")
        channel_labels._cached_site_tables.cache_clear()


class TestRegistration:
    @pytest.mark.parametrize(
        ("name", "signal_type"),
        [
            ("Fp1", "eeg"),
            ("fp1", "eeg"),
            ("Photic", "eeg"),
            ("T3", "eeg"),
            ("Ref", "eeg"),
            ("E1", "eeg"),
            ("Chin", "eeg"),
            ("MISC_4", "eeg"),
            ("Xq1_orig", "eeg"),
            ("EEGx", "eeg"),
            ("Xq-1", "eeg"),
            ("Xq 1", "eeg"),
            ("1Xq", "eeg"),
            ("Xqabcdefghij", "eeg"),
            ("", "eeg"),
            ("Xq1", "eog"),
            ("Xq1", "ecg"),
            ("Xemg1", "eeg"),
            ("Xeog2", "trig"),
            ("Qecg", "eeg"),
            ("Pol_A", "eeg"),
            ("ECG_A", "eeg"),
            ("DC1", "eeg"),
            ("AUX3", "misc"),
        ],
    )
    def test_a_name_the_platform_could_not_read_back_is_refused(self, register, name, signal_type):
        with pytest.raises(ChannelRuleError):
            register({name: signal_type})
        assert name not in channel_labels.registered_channel_names()

    def test_the_longest_name_that_fits_with_the_copy_suffix_is_accepted(self, register):
        register({"Xqabcdefghi": "eeg"})
        assert classify_channel("Xqabcdefghi_orig") == ("misc", "Xqabcdefghi_orig")

    def test_registering_the_same_name_again_is_a_no_op(self, register):
        register({"Xq1": "eeg"})
        register({"Xq1": "eeg"})
        assert channel_labels.registered_channel_names() == {"Xq1": "eeg"}

    def test_a_name_registered_under_another_type_or_case_is_refused(self, register):
        register({"Xq1": "eeg"})
        with pytest.raises(ChannelRuleError):
            register({"Xq1": "misc"})
        with pytest.raises(ChannelRuleError):
            register({"XQ1": "eeg"})

    def test_registration_after_the_app_registry_is_ready_is_refused(self):
        with pytest.raises(RuntimeError):
            channel_labels.register_channel_names({"Xq1": "eeg"})
        assert channel_labels.registered_channel_names() == {}


class TestRegistry:
    def test_nothing_is_registered_by_default(self):
        assert channel_labels.registered_channel_names() == {}

    def test_the_registry_lists_the_names_and_their_types(self, register):
        register({"Xq1": "eeg", "Zqmark": "trig"})
        assert channel_labels.registered_channel_names() == {"Xq1": "eeg", "Zqmark": "trig"}


@pytest.mark.usefixtures("registered")
class TestStoredFile:
    def test_registered_names_are_stored_after_the_ranked_electrodes_in_source_order(self):
        signals = [_signal("EEG Xq2-Ref"), _signal("EEG Fp2-Ref"), _signal("EEG Xq1-Ref"), _signal("EEG Fp1-Ref")]
        assert _stored_labels(signals) == ["Fp1", "Fp2", "Xq2", "Xq1"]

    def test_a_stored_file_is_not_renamed_by_a_second_pass(self):
        signals = [_signal("EEG Fp1-Ref"), _signal("Xq1"), _signal("Zqmark"), _signal("Unknown7")]
        stored = _stored_labels(signals)
        assert stored == ["Fp1", "Xq1", "Zqmark", "MISC_1"]
        assert _stored_labels([_signal(label) for label in stored]) == stored

    def test_a_duplicated_registered_name_keeps_its_first_occurrence(self):
        assert _stored_labels([_signal("Xq1"), _signal("EEG Xq1")]) == ["Xq1", "MISC_1"]


@pytest.mark.django_db
class TestRecord:
    def test_the_ready_row_seals_the_registered_names(self, register, user, tmp_path):
        from recordings.tests.test_deidentification_record import _final_row, _ingest

        register({"Xq1": "eeg"})
        recording = _ingest(user, tmp_path)
        record = _final_row(recording).extra_payload[DEIDENTIFICATION_RECORD_KEY]
        assert record["project_channel_names"] == {"Xq1": "eeg"}

    def test_the_report_flags_a_recording_sealed_under_other_names(self, register, user, tmp_path):
        from recordings.tests.test_deidentification_record import _hash, _ingest

        register({"Xq1": "eeg"})
        sealed_now = _ingest(user, tmp_path / "now")
        with patch.object(channel_labels, "_REGISTERED_NAMES", {}):
            sealed_without = _ingest(user, tmp_path / "without")
        out = StringIO()
        call_command("deidentification_report", "--format", "json", stdout=out)
        report = json.loads(out.getvalue())
        assert report["current"]["project_channel_names"] == {"Xq1": "eeg"}
        flags = {row["hash"]: row["registered_names_differ"] for row in report["recordings"]}
        assert flags == {_hash(sealed_now): False, _hash(sealed_without): True}
        assert report["registered_names_differ_count"] == 1

    def test_a_record_sealed_before_the_registry_is_not_flagged(self, register, user, tmp_path):
        from recordings.management.commands.deidentification_report import report_row
        from recordings.tests.test_deidentification_record import _ingest, _meta

        recording = _ingest(user, tmp_path)
        current = {"deidentification_version": 99, "channel_order_version": 99, "project_channel_names": {"Xq1": "eeg"}}
        row = report_row(recording, _meta(recording), {"site_channel_rules": ""}, current)
        assert row["registered_names_differ"] is False


class TestCheck:
    def _check_with_table(self, tmp_path, rules):
        path = tmp_path / "site.json"
        path.write_text(json.dumps({"rules": rules}), encoding="utf-8")
        with override_settings(RECORDING_CHANNEL_RULES=[str(path)]):
            return check_registered_channel_names(None)

    def test_a_site_spelling_a_registered_name_resolves_first_is_an_error(self, register, tmp_path):
        register({"Xq1": "eeg"})
        errors = self._check_with_table(tmp_path, [{"canonical": "Photic", "label": ["Xq1", "Strobe"]}])
        assert [error.id for error in errors] == ["recordings.E005"]
        assert "Xq1" in errors[0].msg

    def test_a_site_rule_naming_the_registered_name_itself_is_fine(self, register, tmp_path):
        register({"Xq1": "eeg"})
        assert self._check_with_table(tmp_path, [{"canonical": "Xq1", "label": ["Xq1", "Xqone"]}]) == []

    def test_nothing_registered_is_fine(self, tmp_path):
        assert self._check_with_table(tmp_path, [{"canonical": "Photic", "label": ["Xq1"]}]) == []
