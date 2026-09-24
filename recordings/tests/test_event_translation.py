"""Tests for the ingest event translation: the registry, the tables, the fail-closed writer and the two seams.

The property under test throughout is the one the translation exists for: a vendor's event string reaches no
``Event`` row and no ``Code`` row, whether or not anything translates it, and a translated event reaches a
de-identifying reader as a term of the platform's own vocabulary.
"""

from __future__ import annotations

import json
import logging

import pytest
from django.contrib.contenttypes.models import ContentType
from django.test import override_settings
from model_bakery import baker

from annotations.core_vocabularies import find_acquisition_term, load_vocabulary
from annotations.models import Annotation, Code, Event
from epicurrents.models import AccessRight
from recordings.checks import check_event_translation_tables
from recordings.event_translation import (
    PLACEHOLDER_ANNOTATION_NAME,
    PLACEHOLDER_EVENT_NAME,
    EventTranslationError,
    SourceEvent,
    Translation,
    _cached_tables,
    load_table,
    register_event_translation,
    registered_event_translations,
    translate_source_event,
    unregister_event_translation,
    write_source_events,
)
from recordings.tests.test_ingest_privacy_overrides import _Anno, _Result

VENDOR_TYPE = "Øyne lukkes"
VENDOR_LABEL = "Vendor review marker 7"
MAPPER = "test-mapper"


@pytest.fixture(autouse=True)
def _clean_registry():
    yield
    for name in registered_event_translations():
        unregister_event_translation(name)
    _cached_tables.cache_clear()


@pytest.fixture
def table(tmp_path):
    """A table file and the settings that name it, relative to a ``BASE_DIR`` of ``tmp_path``."""

    def make(rules, *, name="Vendor X"):
        path = tmp_path / "events.json"
        path.write_text(json.dumps({"name": name, "rules": rules}), encoding="utf-8")
        return override_settings(BASE_DIR=tmp_path, RECORDING_EVENT_TRANSLATIONS=["events.json"])

    return make


@pytest.fixture
def recording(db, user):
    return baker.make("recordings.Recording", author=user, status="READY")


def _events(recording):
    return list(Event.objects.filter(target_object_id=str(recording.pk)).order_by("timestamp"))


def _codes(event):
    return list(Code.objects.filter(content_type=ContentType.objects.get_for_model(Event), object_id=str(event.pk)))


def _row_text(recording) -> str:
    """Everything an Event or Code row on *recording* carries, so a vendor string can be asserted absent."""
    parts = []
    for event in _events(recording):
        parts += [event.name, event.event_class, json.dumps(event.value)]
        parts += [f"{code.standard}{code.value}{json.dumps(code.meta)}" for code in _codes(event)]
    return " ".join(parts)


class TestTermLookup:
    def test_a_shared_term_is_owned_by_the_biosignal_standard(self):
        term = find_acquisition_term("BIO_TECH_PAUSE")
        assert (term.standard, term.name, term.event_class) == (
            "epicurrents.biosignal",
            "Recording paused",
            "technical",
        )

    def test_an_activation_term_is_owned_by_the_eeg_standard(self):
        term = find_acquisition_term("EEG_ACT_EC")
        assert (term.standard, term.name, term.event_class) == ("epicurrents.eeg", "Eyes closed", "activation")

    def test_a_finding_term_is_not_an_acquisition_term(self):
        categories = load_vocabulary("epicurrents.eeg")["categories"]
        finding = next(c for c in categories.values() if c["scope"] == "finding")
        code = next(iter(finding["events"].values()))["code"]
        assert find_acquisition_term(code) is None

    def test_an_unknown_code_answers_none(self):
        assert find_acquisition_term("BIO_TECH_NOT_A_TERM") is None


class TestMappers:
    def test_a_mapper_answers_a_code_and_the_owning_standard_is_resolved(self):
        register_event_translation(lambda s: "EEG_ACT_EC" if s.type == VENDOR_TYPE else None, name=MAPPER)
        term = translate_source_event(SourceEvent(1.0, None, type=VENDOR_TYPE))
        assert (term.standard, term.code, term.name) == ("epicurrents.eeg", "EEG_ACT_EC", "Eyes closed")
        assert translate_source_event(SourceEvent(1.0, None, type="something else")) is None

    def test_a_translation_carries_its_meta(self):
        register_event_translation(lambda s: Translation("BIO_TECH_TRIGGER", {"number": "3"}), name=MAPPER)
        term = translate_source_event(SourceEvent(1.0, None, label="TRIG 3"))
        assert (term.code, term.meta) == ("BIO_TECH_TRIGGER", {"number": "3"})

    def test_a_code_no_vocabulary_has_is_refused_and_logged(self, caplog):
        register_event_translation(lambda s: "BIO_TECH_NOT_A_TERM", name=MAPPER)
        with caplog.at_level(logging.WARNING, logger="recordings.event_translation"):
            assert translate_source_event(SourceEvent(1.0, None, label=VENDOR_LABEL)) is None
        assert "BIO_TECH_NOT_A_TERM" in caplog.text
        assert VENDOR_LABEL not in caplog.text

    def test_a_finding_term_is_refused(self):
        categories = load_vocabulary("epicurrents.eeg")["categories"]
        finding = next(c for c in categories.values() if c["scope"] == "finding")
        code = next(iter(finding["events"].values()))["code"]
        register_event_translation(lambda s: code, name=MAPPER)
        assert translate_source_event(SourceEvent(1.0, None, label="x")) is None

    def test_a_raising_mapper_leaves_the_event_untranslated_and_names_no_text(self, caplog):
        def boom(source):
            raise RuntimeError("mapper bug")

        register_event_translation(boom, name=MAPPER)
        with caplog.at_level(logging.WARNING, logger="recordings.event_translation"):
            assert translate_source_event(SourceEvent(1.0, None, label=VENDOR_LABEL)) is None
        assert MAPPER in caplog.text
        assert VENDOR_LABEL not in caplog.text

    def test_mappers_are_asked_in_registration_order_and_a_name_is_replaced(self):
        register_event_translation(lambda s: "BIO_TECH_PAUSE", name="first")
        register_event_translation(lambda s: "BIO_TECH_RESUME", name="second")
        assert translate_source_event(SourceEvent(1.0, None, label="x")).code == "BIO_TECH_PAUSE"
        register_event_translation(lambda s: None, name="first")
        assert registered_event_translations() == ["second", "first"]
        assert translate_source_event(SourceEvent(1.0, None, label="x")).code == "BIO_TECH_RESUME"

    def test_a_mapper_answering_the_wrong_type_is_ignored_and_not_echoed(self, caplog):
        register_event_translation(lambda s: s, name=MAPPER)
        with caplog.at_level(logging.WARNING, logger="recordings.event_translation"):
            assert translate_source_event(SourceEvent(1.0, None, label=VENDOR_LABEL)) is None
        assert MAPPER in caplog.text
        assert VENDOR_LABEL not in caplog.text

    def test_a_mapper_echoing_the_label_as_its_code_does_not_reach_the_log(self, caplog):
        register_event_translation(lambda s: s.label, name=MAPPER)
        with caplog.at_level(logging.WARNING, logger="recordings.event_translation"):
            assert translate_source_event(SourceEvent(1.0, None, label=VENDOR_LABEL)) is None
        assert "not shaped like a code" in caplog.text
        assert VENDOR_LABEL not in caplog.text


class TestTables:
    def test_type_and_label_match_casefolded_with_whitespace_collapsed(self, table):
        with table(
            [{"type": "Eyes  Closed", "code": "EEG_ACT_EC"}, {"label": "recording paused", "code": "BIO_TECH_PAUSE"}]
        ):
            assert translate_source_event(SourceEvent(1.0, None, type="eyes closed ")).code == "EEG_ACT_EC"
            assert translate_source_event(SourceEvent(1.0, None, label="Recording   Paused")).code == "BIO_TECH_PAUSE"
            assert translate_source_event(SourceEvent(1.0, None, label="Recording Paused by tech")) is None

    def test_a_rule_with_type_and_label_needs_both(self, table):
        with table([{"type": "Photic", "label": "10 Hz", "code": "EEG_ACT_PHOTIC_10HZ"}]):
            assert (
                translate_source_event(SourceEvent(1.0, None, type="Photic", label="10 Hz")).code
                == "EEG_ACT_PHOTIC_10HZ"
            )
            assert translate_source_event(SourceEvent(1.0, None, type="Photic", label="12 Hz")) is None
            assert translate_source_event(SourceEvent(1.0, None, type="Flash", label="10 Hz")) is None

    def test_a_pattern_fills_the_code_and_the_meta_from_its_groups(self, table):
        rules = [
            {"type": "Photic", "pattern": r"(\d+) ?hz", "code": "EEG_ACT_PHOTIC_{1}HZ"},
            {"pattern": r"trig(?:ger)? (\d+)", "code": "BIO_TECH_TRIGGER", "meta": {"number": "{1}", "kind": "tal"}},
        ]
        with table(rules):
            term = translate_source_event(SourceEvent(1.0, None, type="Photic", label="15Hz"))
            assert (term.code, term.standard) == ("EEG_ACT_PHOTIC_15HZ", "epicurrents.eeg")
            term = translate_source_event(SourceEvent(1.0, None, label="Trigger 42"))
            assert (term.code, term.meta) == ("BIO_TECH_TRIGGER", {"number": "42", "kind": "tal"})

    def test_a_code_assembled_from_a_pattern_fails_closed_when_no_term_has_it(self, table, caplog):
        with (
            table([{"type": "Photic", "pattern": r"(\d+) ?hz", "code": "EEG_ACT_PHOTIC_{1}HZ"}]),
            caplog.at_level(logging.WARNING, logger="recordings.event_translation"),
        ):
            assert translate_source_event(SourceEvent(1.0, None, type="Photic", label="11 Hz")) is None
        assert "EEG_ACT_PHOTIC_11HZ" in caplog.text

    def test_the_first_matching_rule_wins(self, table):
        with table(
            [
                {"type": "Photic", "label": "1 Hz", "code": "EEG_ACT_PHOTIC_1HZ"},
                {"type": "Photic", "code": "EEG_ACT_PHOTIC"},
            ]
        ):
            assert (
                translate_source_event(SourceEvent(1.0, None, type="Photic", label="1 Hz")).code == "EEG_ACT_PHOTIC_1HZ"
            )
            assert translate_source_event(SourceEvent(1.0, None, type="Photic", label="fast")).code == "EEG_ACT_PHOTIC"

    def test_a_registered_mapper_is_asked_before_the_tables(self, table):
        register_event_translation(lambda s: "BIO_TECH_RESUME", name=MAPPER)
        with table([{"type": "x", "code": "BIO_TECH_PAUSE"}]):
            assert translate_source_event(SourceEvent(1.0, None, type="x")).code == "BIO_TECH_RESUME"

    def test_an_absolute_path_is_taken_as_given(self, tmp_path):
        path = tmp_path / "abs.json"
        path.write_text(json.dumps({"rules": [{"type": "x", "code": "BIO_TECH_PAUSE"}]}), encoding="utf-8")
        with override_settings(BASE_DIR=tmp_path / "elsewhere", RECORDING_EVENT_TRANSLATIONS=[str(path)]):
            assert translate_source_event(SourceEvent(1.0, None, type="x")).code == "BIO_TECH_PAUSE"

    def test_a_malformed_table_under_a_running_process_is_refused_and_logged(self, tmp_path, caplog):
        (tmp_path / "events.json").write_text("{not json", encoding="utf-8")
        with (
            override_settings(BASE_DIR=tmp_path, RECORDING_EVENT_TRANSLATIONS=["events.json"]),
            caplog.at_level(logging.ERROR, logger="recordings.event_translation"),
        ):
            assert translate_source_event(SourceEvent(1.0, None, type="x")) is None
        assert "table refused" in caplog.text

    @pytest.mark.parametrize(
        ("rule", "message"),
        [
            ("not an object", "is not an object"),
            ({"type": "x", "code": "C", "colour": "red"}, "unknown keys"),
            ({"type": "x"}, "non-empty string code"),
            ({"type": "x", "code": ""}, "non-empty string code"),
            ({"code": "C"}, "matches nothing"),
            ({"label": "x", "pattern": "x", "code": "C"}, "both label and pattern"),
            ({"type": "", "code": "C"}, "'type' must be a non-empty string"),
            ({"label": 3, "code": "C"}, "'label' must be a non-empty string"),
            ({"pattern": "(", "code": "C"}, "does not compile"),
            ({"type": "x", "code": "C", "meta": "text"}, "'meta' must be an object"),
        ],
    )
    def test_each_rule_malformation_is_named(self, tmp_path, rule, message):
        path = tmp_path / "events.json"
        path.write_text(json.dumps({"rules": [rule]}), encoding="utf-8")
        with pytest.raises(EventTranslationError, match=message):
            load_table(path)

    def test_a_file_malformation_is_named(self, tmp_path):
        path = tmp_path / "events.json"
        with pytest.raises(EventTranslationError, match="cannot be read"):
            load_table(path)
        path.write_text("[]", encoding="utf-8")
        with pytest.raises(EventTranslationError, match="object with a rules list"):
            load_table(path)
        path.write_text(json.dumps({"name": 1, "rules": []}), encoding="utf-8")
        with pytest.raises(EventTranslationError, match="name must be a string"):
            load_table(path)


class TestSystemCheck:
    def test_a_good_table_passes(self, table):
        with table([{"type": "x", "code": "BIO_TECH_PAUSE"}, {"pattern": r"(\d+)", "code": "EEG_ACT_PHOTIC_{1}HZ"}]):
            assert check_event_translation_tables(None) == []

    def test_a_malformed_table_is_an_error(self, tmp_path):
        with override_settings(BASE_DIR=tmp_path, RECORDING_EVENT_TRANSLATIONS=["missing.json"]):
            issues = check_event_translation_tables(None)
        assert [issue.id for issue in issues] == ["recordings.E001"]
        assert "missing.json" in issues[0].msg

    def test_a_literal_code_no_vocabulary_has_is_an_error(self, table):
        with table([{"type": "x", "code": "BIO_TECH_PAUSE"}, {"type": "y", "code": "BIO_TECH_NOT_A_TERM"}]):
            issues = check_event_translation_tables(None)
        assert [issue.id for issue in issues] == ["recordings.E002"]
        assert "BIO_TECH_NOT_A_TERM" in issues[0].msg
        assert "BIO_TECH_PAUSE" not in issues[0].msg

    def test_no_tables_is_nothing_to_check(self):
        with override_settings(RECORDING_EVENT_TRANSLATIONS=[]):
            assert check_event_translation_tables(None) == []


@pytest.mark.django_db
class TestWriter:
    def test_a_translated_event_is_named_and_classed_by_its_term_and_coded_under_the_owning_standard(self, recording):
        register_event_translation(lambda s: Translation("EEG_ACT_EC", {"note": "from the file"}), name=MAPPER)
        assert write_source_events(recording, [SourceEvent(3.5, 2.0, type=VENDOR_TYPE)], hash_prefix="t") == 1
        (event,) = _events(recording)
        assert (event.name, event.event_class, event.timestamp, event.duration) == (
            "Eyes closed",
            "activation",
            3.5,
            2.0,
        )
        assert event.value is None
        (code,) = _codes(event)
        assert (code.standard, code.value, code.meta) == ("epicurrents.eeg", "EEG_ACT_EC", {"note": "from the file"})

    def test_an_untranslated_event_is_a_placeholder_named_by_its_kind(self, recording):
        sources = [SourceEvent(1.0, None, label=VENDOR_LABEL), SourceEvent(2.0, 0.5, type=VENDOR_TYPE, label="x")]
        assert write_source_events(recording, sources, hash_prefix="t") == 0
        annotation, event = _events(recording)
        assert (annotation.name, annotation.event_class, annotation.timestamp) == (PLACEHOLDER_ANNOTATION_NAME, "", 1.0)
        assert (event.name, event.timestamp, event.duration) == (PLACEHOLDER_EVENT_NAME, 2.0, 0.5)
        assert _codes(annotation) == [] and _codes(event) == []

    def test_no_vendor_string_reaches_any_event_or_code_row(self, recording):
        register_event_translation(lambda s: "BIO_TECH_PAUSE" if s.type == VENDOR_TYPE else None, name=MAPPER)
        sources = [
            SourceEvent(1.0, None, label=VENDOR_LABEL),
            SourceEvent(2.0, None, type=VENDOR_TYPE, label=VENDOR_LABEL),
        ]
        write_source_events(recording, sources, hash_prefix="t")
        assert len(_events(recording)) == 2
        assert VENDOR_LABEL not in _row_text(recording)
        assert VENDOR_TYPE not in _row_text(recording)

    def test_rows_carry_distinct_hashes_per_seam_and_index(self, recording):
        write_source_events(
            recording, [SourceEvent(1.0, None, label="a"), SourceEvent(1.0, None, label="a")], hash_prefix="one"
        )
        write_source_events(recording, [SourceEvent(1.0, None, label="a")], hash_prefix="two")
        hashes = {event.object_hash for event in _events(recording)}
        assert len(hashes) == 3

    def test_the_rows_are_the_system_users(self, recording):
        from epicurrents.system_user import get_system_user

        write_source_events(recording, [SourceEvent(1.0, None, label="a")], hash_prefix="t")
        assert _events(recording)[0].author == get_system_user()

    @override_settings(RECORDINGS_DISCARD_EMBEDDED_ANNOTATIONS=True)
    def test_under_the_discard_setting_translated_events_are_written_and_placeholders_are_not(self, recording):
        register_event_translation(lambda s: "BIO_TECH_PAUSE" if s.type == VENDOR_TYPE else None, name=MAPPER)
        sources = [SourceEvent(1.0, None, label=VENDOR_LABEL), SourceEvent(2.0, None, type=VENDOR_TYPE)]
        assert write_source_events(recording, sources, hash_prefix="t") == 1
        (event,) = _events(recording)
        assert (event.name, event.timestamp) == ("Recording paused", 2.0)


@pytest.mark.django_db
class TestTalSeam:
    def test_each_tal_is_a_source_annotation_at_its_data_position(self, recording, table):
        from recordings.tasks import _save_edf_results

        with table([{"label": "Eyes closed", "code": "EEG_ACT_EC"}]):
            _save_edf_results(
                recording,
                _Result(annotations=[_Anno(1.0, 0.5, "Eyes closed"), _Anno(7.0, None, "Pt. aura")], gaps={4.0: 2.0}),
            )
        closed, aura = _events(recording)
        assert (closed.name, closed.timestamp, closed.duration) == ("Eyes closed", 1.0, 0.5)
        assert [code.value for code in _codes(closed)] == ["EEG_ACT_EC"]
        # 7.0 on the wall clock is 5.0 in data positions after a two-second gap at 4.0.
        assert (aura.name, aura.timestamp) == (PLACEHOLDER_ANNOTATION_NAME, 5.0)
        assert "Pt. aura" not in _row_text(recording)

    def test_when_a_sidecar_carried_the_events_the_tal_seam_writes_no_event_rows(self, recording, table):
        from recordings.tasks import _save_edf_results

        with table([{"label": "Eyes closed", "code": "EEG_ACT_EC"}]):
            _save_edf_results(
                recording, _Result(annotations=[_Anno(1.0, 0.5, "Eyes closed")]), events_from_sidecar=True
            )
        assert _events(recording) == []
        # The raw record is the file's own account and is kept either way.
        assert Annotation.objects.filter(target_object_id=str(recording.pk), name="Original annotations").exists()

    def test_the_raw_record_still_holds_what_the_file_said(self, recording, table):
        from recordings.tasks import _save_edf_results

        with table([{"label": "Eyes closed", "code": "EEG_ACT_EC"}]):
            _save_edf_results(recording, _Result(annotations=[_Anno(1.0, 0.5, "Eyes closed")]))
        (blob,) = Annotation.objects.filter(target_object_id=str(recording.pk))
        assert blob.content["events"][0]["label"] == "Eyes closed"

    @override_settings(RECORDINGS_DISCARD_EMBEDDED_ANNOTATIONS=True)
    def test_under_the_discard_setting_the_translated_event_survives_and_the_raw_record_does_not(
        self, recording, table
    ):
        from recordings.tasks import _save_edf_results

        with table([{"label": "Eyes closed", "code": "EEG_ACT_EC"}]):
            _save_edf_results(
                recording, _Result(annotations=[_Anno(1.0, 0.5, "Eyes closed"), _Anno(2.0, None, "Pt. aura")])
            )
        assert [event.name for event in _events(recording)] == ["Eyes closed"]
        assert not Annotation.objects.filter(target_object_id=str(recording.pk)).exists()


@pytest.mark.django_db
class TestSidecarSeam:
    SIDECAR = {
        "annotations": [{"onset_seconds": 5.0, "duration_seconds": None, "text": "Pt. aura, ref. Dr Hansen"}],
        "events": [
            {"onset_seconds": 2.0, "duration_seconds": 0.0, "type": "Photic", "label": "10 Hz"},
            {"onset_seconds": 3.0, "duration_seconds": None, "type": VENDOR_TYPE, "label": None},
        ],
    }

    def test_typed_events_translate_by_type_and_label_and_text_is_a_source_annotation(self, recording, table):
        from recordings.converters.sidecar import save_sidecar_events

        rules = [
            {"type": "Photic", "pattern": r"(\d+) ?hz", "code": "EEG_ACT_PHOTIC_{1}HZ"},
            {"type": VENDOR_TYPE, "code": "EEG_ACT_EC"},
        ]
        with table(rules):
            save_sidecar_events(recording, self.SIDECAR)
        photic, closed, aura = _events(recording)
        assert (photic.name, photic.timestamp) == ("10 Hz photic stimulation", 2.0)
        assert [code.value for code in _codes(photic)] == ["EEG_ACT_PHOTIC_10HZ"]
        assert (closed.name, [code.standard for code in _codes(closed)]) == ("Eyes closed", ["epicurrents.eeg"])
        assert (aura.name, aura.timestamp, _codes(aura)) == (PLACEHOLDER_ANNOTATION_NAME, 5.0, [])
        assert "Hansen" not in _row_text(recording)
        assert VENDOR_TYPE not in _row_text(recording)

    def test_the_raw_record_keeps_the_vendor_strings(self, recording):
        from recordings.converters.sidecar import save_sidecar_events

        save_sidecar_events(recording, self.SIDECAR)
        (blob,) = Annotation.objects.filter(target_object_id=str(recording.pk))
        assert [event["label"] for event in blob.content["events"]] == [
            "Photic: 10 Hz",
            VENDOR_TYPE,
            "Pt. aura, ref. Dr Hansen",
        ]

    def test_a_malformed_sidecar_writes_no_event_row(self, recording):
        from recordings.converters.sidecar import save_sidecar_events

        with pytest.raises(ValueError, match="onset_seconds"):
            save_sidecar_events(recording, {"events": [{"type": "Photic"}]})
        assert _events(recording) == []

    def test_the_two_seams_never_collide_on_a_hash(self, recording):
        from recordings.converters.sidecar import save_sidecar_events
        from recordings.tasks import _save_edf_results

        _save_edf_results(recording, _Result(annotations=[_Anno(2.0, None, "x")]))
        save_sidecar_events(recording, {"events": [{"onset_seconds": 2.0, "type": "x"}]})
        assert len({event.object_hash for event in _events(recording)}) == 2


STORED_HASH = "0123456789ABCDEF0123456789ABCDEF"
EVENTS_URL = "/annotations/api/v1/events/"


@pytest.mark.django_db
class TestServing:
    """An ingest-coded event under the annotation-text rule: the term crosses a de-identifying grant, the name does not."""

    @pytest.fixture
    def scene(self, make_user, table):
        from recordings.models import Recording

        owner = make_user(username="ingest-owner")
        reader = make_user(username="ingest-reader")
        recording = baker.make(
            Recording,
            author=owner,
            file_size=1,
            file_extension=".edf",
            stored_name=f"{STORED_HASH}.edf",
            status=Recording.Status.READY,
        )
        with table([{"label": "Eyes closed", "code": "EEG_ACT_EC"}]):
            write_source_events(
                recording,
                [SourceEvent(1.0, None, label="Eyes closed"), SourceEvent(2.0, None, label="Pt. aura")],
                hash_prefix="t",
            )
        return {"owner": owner, "reader": reader, "recording": recording}

    def _grant(self, scene, *, apply_middleware):
        recording = scene["recording"]
        AccessRight.objects.create(
            content_type=ContentType.objects.get_for_model(recording, for_concrete_model=False),
            object_id=str(recording.pk),
            access_giver=scene["owner"],
            access_target=scene["reader"],
            can_read=True,
            apply_middleware=apply_middleware,
        )

    def _listing(self, client, scene):
        recording = scene["recording"]
        ct = ContentType.objects.get_for_model(recording, for_concrete_model=False)
        response = client.get(f"{EVENTS_URL}?target_content_type_id={ct.pk}&target_object_id={recording.pk}")
        assert response.status_code == 200, response.content
        return sorted(response.json(), key=lambda row: row["timestamp"])

    def test_a_de_identifying_reader_receives_the_term_and_no_name(self, client, scene):
        self._grant(scene, apply_middleware=True)
        client.force_login(scene["reader"])
        closed, placeholder = self._listing(client, scene)
        assert (closed["name"], closed["text_withheld"]) == ("", True)
        assert [(code["standard"], code["value"], code["meta"]) for code in closed["codes"]] == [
            ("epicurrents.eeg", "EEG_ACT_EC", None)
        ]
        assert (placeholder["name"], placeholder["codes"], placeholder["timestamp"]) == ("", [], 2.0)

    def test_a_raw_reader_receives_the_terms_name_and_the_placeholder_name(self, client, scene):
        self._grant(scene, apply_middleware=False)
        client.force_login(scene["reader"])
        closed, placeholder = self._listing(client, scene)
        assert (closed["name"], closed["text_withheld"]) == ("Eyes closed", False)
        assert placeholder["name"] == PLACEHOLDER_ANNOTATION_NAME


class TestSeamOwnership:
    """A converter writes its events into the EDF and into the sidecar; the sidecar owns the ``Event`` rows."""

    def test_a_sidecar_with_an_item_in_either_list_carries_events(self):
        from recordings.converters.sidecar import sidecar_carries_events

        assert sidecar_carries_events({"events": [{"onset_seconds": 1.0, "type": "Photic"}]})
        assert sidecar_carries_events({"annotations": [{"onset_seconds": 1.0, "text": "x"}], "events": []})

    @pytest.mark.parametrize(
        "sidecar",
        [
            None,
            {},
            {"annotations": [], "events": []},
            {"channels": [{"index": 0}]},
            {"events": "not a list"},
            {"annotations": [], "events": "not a list"},
            {"events": [{"type": "Photic"}]},
            "text",
        ],
    )
    def test_nothing_else_does(self, sidecar):
        """Including a sidecar the schema refuses: its events are lost, so the TALs must not be too."""
        from recordings.converters.sidecar import sidecar_carries_events

        assert not sidecar_carries_events(sidecar)

    def test_a_converted_upload_writes_each_event_once_from_the_sidecar(self, user, tmp_path):
        from unittest.mock import patch

        from recordings.converters.sidecar import handle_post_convert
        from recordings.pipelines import register_post_convert
        from recordings.tasks import process_recording
        from recordings.tests.test_convert_hooks import _make_pending_recording

        staging = tmp_path / "staging"
        uploads = tmp_path / "uploads"
        staging.mkdir()
        uploads.mkdir()
        recording = _make_pending_recording(user, staging, b"<vendor bytes>", ext=".xyz")
        # Registered at boot; registration is idempotent, so this only guards against a test that reset it.
        register_post_convert(handle_post_convert)
        with (
            override_settings(
                RECORDINGS_STAGING_PATH=str(staging),
                RECORDINGS_UPLOAD_PATH=str(uploads),
                RECORDING_CONVERTERS={".xyz": "recordings.tests.test_event_translation._tals_and_sidecar_convert"},
            ),
            patch("notifications.tasks.send_push_to_user.delay"),
        ):
            process_recording(recording.pk)

        recording.refresh_from_db()
        assert recording.status == recording.Status.READY, recording.processing_error
        # Two events in the file, two rows: the sidecar's typed ones, not the TALs' untyped ones as well.
        assert [event.name for event in _events(recording)] == [PLACEHOLDER_EVENT_NAME, PLACEHOLDER_EVENT_NAME]
        names = set(Annotation.objects.filter(target_object_id=str(recording.pk)).values_list("name", flat=True))
        assert names == {"Original annotations", "Source events"}


def _tals_and_sidecar_convert(source_path, output_dir):
    """A converter that writes its two events into the EDF as TALs and into the sidecar with their types."""
    from recordings.tests.test_edf_processor import _make_edfplus_file, _make_tal

    converted = output_dir / "out.edf"
    converted.write_bytes(
        _make_edfplus_file(
            n_records=1, tals_per_record=[[_make_tal(0.2, "10 Hz", duration=0.3), _make_tal(0.6, "Eyes closed")]]
        )
    )
    sidecar = {
        "events": [
            {"onset_seconds": 0.2, "duration_seconds": 0.3, "type": "Photic", "label": "10 Hz"},
            {"onset_seconds": 0.6, "duration_seconds": None, "type": "Eyes closed", "label": None},
        ]
    }
    return converted, sidecar
