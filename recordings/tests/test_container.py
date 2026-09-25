"""Tests for the viewer's container: the marker, the detached footer, and the rows written from it.

The properties under test: an ordinary EDF is never touched; a container is stored as the EDF alone, with a
standard reserved field; the footer's events reach ``Event`` rows through the code each declares, without any
mapper or table; and a container that is not what its marker says is refused rather than stored.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import patch

import pytest
from django.contrib.contenttypes.models import ContentType
from django.test import override_settings
from model_bakery import baker

from annotations.models import Annotation, Code, Event, Interruption
from recordings.container import (
    ContainerError,
    detach_footer,
    footer_carries_events,
    footer_events,
    footer_interruptions,
    read_container_marker,
    save_footer_events,
)
from recordings.event_translation import (
    PLACEHOLDER_ANNOTATION_NAME,
    PLACEHOLDER_EVENT_NAME,
    _cached_tables,
    register_event_translation,
    registered_event_translations,
    unregister_event_translation,
)
from recordings.processors.edf import parse_edf_header
from recordings.tests.test_edf_processor import _make_edf_data, _make_edf_header

SIGNALS = [{"label": "EEG Fp1", "sample_count": 4, "phys_min": -100, "phys_max": 100}]


def _plain_edf(n_records: int = 2) -> bytes:
    return _make_edf_header(n_records=n_records, signals=SIGNALS) + _make_edf_data(SIGNALS, n_records)


def _make_container(footer, *, plus: str = "", n_records: int = 2, kib: int | None = None, body: bytes | None = None):
    """A container as the viewer's encoder writes one: the EDF, the marker in the reserved field, the padded footer."""
    edf = _plain_edf(n_records)
    body = json.dumps(footer).encode("utf-8") if body is None else body
    kib = -(-len(body) // 1024) if kib is None else kib
    marker = f"EDF{plus} EC:{len(edf)}:{kib}".ljust(44).encode("ascii")
    return edf[:192] + marker + edf[236:] + body.ljust(kib * 1024, b"\x00")


def _event(**fields):
    template = {"class": "event", "duration": 0, "priority": 400, "start": 1.0, "value": "", "channels": []}
    template.update(fields)
    return template


@pytest.fixture(autouse=True)
def _clean_registry():
    yield
    for name in registered_event_translations():
        unregister_event_translation(name)
    _cached_tables.cache_clear()


@pytest.fixture
def recording(db, user):
    return baker.make("recordings.Recording", author=user, status="READY")


def _events(recording):
    return list(Event.objects.filter(target_object_id=str(recording.pk)).order_by("timestamp"))


def _codes(event):
    return list(Code.objects.filter(content_type=ContentType.objects.get_for_model(Event), object_id=str(event.pk)))


class TestMarker:
    def test_an_ordinary_reserved_field_is_no_marker(self):
        assert read_container_marker(_plain_edf()) is None
        assert read_container_marker(_make_edf_header(reserved="EDF+C", signals=SIGNALS)) is None

    def test_the_marker_names_the_format_the_edf_size_and_the_footer_size(self):
        assert read_container_marker(_make_container({}, n_records=3)) == ("EDF", 256 + 256 + 3 * 8, 1)
        assert read_container_marker(_make_container({}, plus="+D")) == ("EDF+D", 256 + 256 + 2 * 8, 1)

    def test_less_than_a_header_is_no_marker(self):
        assert read_container_marker(b"0" * 100) is None


class TestDetach:
    def test_an_ordinary_edf_is_left_untouched(self, tmp_path):
        path = tmp_path / "plain.edf"
        path.write_bytes(_plain_edf())
        assert detach_footer(path) is None
        assert path.read_bytes() == _plain_edf()

    def test_the_footer_comes_off_and_a_plain_edf_with_a_blank_reserved_field_remains(self, tmp_path):
        footer = {"version": "1.0", "events": [_event(value="Eyes closed")], "interruptions": []}
        path = tmp_path / "container.edf"
        path.write_bytes(_make_container(footer))

        assert detach_footer(path) == footer

        stored = path.read_bytes()
        assert stored == _plain_edf()
        header = parse_edf_header(stored)
        assert (header.reserved, header.is_plus) == ("", False)

    def test_an_edf_plus_marker_keeps_its_continuity_marker(self, tmp_path):
        path = tmp_path / "container.edf"
        path.write_bytes(_make_container({}, plus="+D"))
        detach_footer(path)
        header = parse_edf_header(path.read_bytes())
        assert (header.reserved, header.is_plus, header.discontinuous) == ("EDF+D", True, True)

    def test_a_footer_the_file_does_not_hold_is_refused(self, tmp_path):
        path = tmp_path / "short.edf"
        path.write_bytes(_make_container({})[:-100])
        with pytest.raises(ContainerError, match="names"):
            detach_footer(path)

    def test_bytes_after_the_footer_are_refused(self, tmp_path):
        path = tmp_path / "trailing.edf"
        path.write_bytes(_make_container({}) + b"\x00" * 1024)
        with pytest.raises(ContainerError, match="names"):
            detach_footer(path)

    def test_a_zero_length_footer_is_refused(self, tmp_path):
        path = tmp_path / "empty.edf"
        path.write_bytes(_make_container(None, kib=0, body=b""))
        with pytest.raises(ContainerError, match="zero-length"):
            detach_footer(path)

    def test_a_marker_that_disagrees_with_the_header_geometry_is_refused(self, tmp_path):
        container = _make_container({})
        edf_bytes = len(_plain_edf())
        # A marker naming one record less, with the file shortened to match, so only the geometry disagrees: the
        # truncation would cut into the data records.
        short = container.replace(f"EC:{edf_bytes}:".encode(), f"EC:{edf_bytes - 8}:".encode())
        path = tmp_path / "geometry.edf"
        path.write_bytes(short[:-8])
        with pytest.raises(ContainerError, match="header describes"):
            detach_footer(path)

    def test_a_footer_that_is_not_utf8_is_refused_as_a_container(self, tmp_path):
        path = tmp_path / "bytes.edf"
        path.write_bytes(_make_container(None, body=b"\xff\xfe{}"))
        with pytest.raises(ContainerError, match="not JSON"):
            detach_footer(path)

    def test_is_container_reads_the_marker_only(self, tmp_path):
        from recordings.container import is_container

        plain = tmp_path / "plain.edf"
        plain.write_bytes(_plain_edf())
        container = tmp_path / "container.edf"
        container.write_bytes(_make_container({}))
        assert not is_container(plain)
        assert is_container(container)

    @pytest.mark.parametrize("body", [b"not json", b"[1, 2]", b'"a string"'])
    def test_a_footer_that_is_not_a_json_object_is_refused(self, tmp_path, body):
        path = tmp_path / "bad.edf"
        path.write_bytes(_make_container(None, body=body))
        with pytest.raises(ContainerError, match="footer"):
            detach_footer(path)

    def test_a_refused_container_is_left_as_it_was(self, tmp_path):
        path = tmp_path / "bad.edf"
        original = _make_container(None, body=b"not json")
        path.write_bytes(original)
        with pytest.raises(ContainerError):
            detach_footer(path)
        assert path.read_bytes() == original


class TestFooterEvents:
    def test_each_event_carries_its_timing_text_kind_and_declared_code(self):
        sources = footer_events(
            {
                "events": [
                    _event(start=2.5, duration=1.5, value="HV", codes={"epicurrents.eeg": "EEG_ACT_HV"}),
                    _event(start=4, value="the patient coughed") | {"class": "comment"},
                    _event(start=6, label="Pause", value=3, codes={"epicurrents.biosignal": "BIO_TECH_PAUSE"}),
                ]
            }
        )
        assert [(s.onset, s.duration, s.label, s.type, s.code) for s in sources] == [
            (2.5, 1.5, "HV", "event", "EEG_ACT_HV"),
            (4.0, None, "the patient coughed", "", ""),
            (6.0, None, "Pause", "event", "BIO_TECH_PAUSE"),
        ]

    def test_a_code_that_is_not_a_string_is_no_code(self):
        (source,) = footer_events({"events": [_event(codes={"epicurrents.eeg": 7, "dicom": "SCT/1"})]})
        assert source.code == ""

    def test_no_events_key_is_no_events(self):
        assert footer_events({}) == []

    @pytest.mark.parametrize(
        ("footer", "message"),
        [
            ({"events": {}}, "not a list"),
            ({"events": ["x"]}, "not an object"),
            ({"events": [{"value": "no start"}]}, "no numeric start"),
            ({"events": [{"start": True}]}, "no numeric start"),
        ],
    )
    def test_a_malformed_event_is_named(self, footer, message):
        with pytest.raises(ValueError, match=message):
            footer_events(footer)

    def test_interruptions_are_pairs_of_a_start_and_a_positive_duration(self):
        assert footer_interruptions({"interruptions": [[10, 5], [40.5, 2]]}) == [(10.0, 5.0), (40.5, 2.0)]
        assert footer_interruptions({}) == []

    @pytest.mark.parametrize("items", [{}, [[1]], [[1, 2, 3]], [["a", 1]], [[1, 0]], [[-1, 2]], [[True, 1]]])
    def test_a_malformed_interruption_is_refused(self, items):
        with pytest.raises(ValueError, match="interruptions"):
            footer_interruptions({"interruptions": items})

    def test_a_footer_carries_events_when_it_has_a_well_formed_one(self):
        assert footer_carries_events({"events": [_event()]})
        assert not footer_carries_events({"events": []})
        assert not footer_carries_events({"events": ["x"]})
        assert not footer_carries_events({"interruptions": [[1, 2]]})
        assert not footer_carries_events(None)


@pytest.mark.django_db
class TestSave:
    def test_a_declared_code_reaches_the_row_with_no_mapper_or_table(self, recording):
        save_footer_events(
            recording,
            {
                "events": [
                    _event(start=3, duration=2, value="Hyperventilation", codes={"epicurrents.eeg": "EEG_ACT_HV"}),
                    _event(start=1, value="Recording paused", codes={"epicurrents.biosignal": "BIO_TECH_PAUSE"}),
                ]
            },
        )
        pause, hv = _events(recording)
        assert (pause.name, pause.event_class, pause.timestamp, pause.duration) == (
            "Recording paused",
            "technical",
            1.0,
            None,
        )
        assert (hv.name, hv.event_class, hv.timestamp, hv.duration) == ("Hyperventilation", "activation", 3.0, 2.0)
        assert [(c.standard, c.value) for c in _codes(pause)] == [("epicurrents.biosignal", "BIO_TECH_PAUSE")]
        assert [(c.standard, c.value) for c in _codes(hv)] == [("epicurrents.eeg", "EEG_ACT_HV")]

    def test_a_declared_code_no_vocabulary_has_gives_a_placeholder_and_is_logged_without_its_value(
        self, recording, caplog
    ):
        with caplog.at_level(logging.WARNING, logger="recordings.event_translation"):
            save_footer_events(
                recording, {"events": [_event(value="Site marker", codes={"epicurrents.eeg": "PATIENT_SMITH"})]}
            )
        (event,) = _events(recording)
        assert (event.name, _codes(event)) == (PLACEHOLDER_EVENT_NAME, [])
        assert "declared a code" in caplog.text
        assert "PATIENT_SMITH" not in caplog.text
        assert "Site marker" not in caplog.text

    def test_an_event_with_no_code_is_translated_by_its_text_like_any_other(self, recording):
        register_event_translation(lambda s: "EEG_ACT_EC" if s.label == "Eyes closed" else None, name="test")
        save_footer_events(
            recording,
            {"events": [_event(value="Eyes closed"), _event(start=2, value="a note") | {"class": "comment"}]},
        )
        closed, note = _events(recording)
        assert (closed.name, [c.value for c in _codes(closed)]) == ("Eyes closed", ["EEG_ACT_EC"])
        assert (note.name, _codes(note)) == (PLACEHOLDER_ANNOTATION_NAME, [])

    def test_interruptions_become_rows_and_the_raw_record_holds_both(self, recording):
        save_footer_events(
            recording,
            {"events": [_event(start=5, duration=1, value="Photic 10 Hz")], "interruptions": [[10, 5], [40, 2]]},
        )
        gaps = list(Interruption.objects.filter(target_object_id=str(recording.pk)).order_by("timestamp"))
        assert [(g.timestamp, g.duration) for g in gaps] == [(10.0, 5.0), (40.0, 2.0)]
        assert len({g.object_hash for g in gaps}) == 2
        raw = Annotation.objects.get(target_object_id=str(recording.pk), name="Source events")
        assert raw.content == {
            "events": [{"onset": 5.0, "duration": 1.0, "label": "event: Photic 10 Hz"}],
            "interruptions": [{"onset": 10.0, "duration": 5.0}, {"onset": 40.0, "duration": 2.0}],
        }

    def test_an_empty_footer_writes_nothing(self, recording):
        save_footer_events(recording, {"version": "1.0", "events": [], "interruptions": []})
        assert not Event.objects.filter(target_object_id=str(recording.pk)).exists()
        assert not Annotation.objects.filter(target_object_id=str(recording.pk)).exists()

    def test_a_malformed_footer_writes_nothing(self, recording):
        with pytest.raises(ValueError):
            save_footer_events(recording, {"events": [_event()], "interruptions": [[1]]})
        assert not Event.objects.filter(target_object_id=str(recording.pk)).exists()
        assert not Interruption.objects.filter(target_object_id=str(recording.pk)).exists()

    @override_settings(RECORDINGS_DISCARD_EMBEDDED_ANNOTATIONS=True)
    def test_under_the_discard_setting_the_term_and_the_gap_survive_and_the_rest_does_not(self, recording):
        save_footer_events(
            recording,
            {
                "events": [_event(value="HV", codes={"epicurrents.eeg": "EEG_ACT_HV"}), _event(start=2, value="note")],
                "interruptions": [[10, 5]],
            },
        )
        assert [e.name for e in _events(recording)] == ["Hyperventilation"]
        assert Interruption.objects.filter(target_object_id=str(recording.pk)).count() == 1
        assert not Annotation.objects.filter(target_object_id=str(recording.pk)).exists()


@pytest.mark.django_db
class TestUpload:
    """The upload path end to end: a container in, a plain EDF and its rows out."""

    def _process(self, user, tmp_path, content: bytes):
        from recordings.tasks import process_recording
        from recordings.tests.test_convert_hooks import _make_pending_recording

        staging = tmp_path / "staging"
        uploads = tmp_path / "uploads"
        staging.mkdir()
        uploads.mkdir()
        recording = _make_pending_recording(user, staging, content, ext=".edf")
        with (
            override_settings(RECORDINGS_STAGING_PATH=str(staging), RECORDINGS_UPLOAD_PATH=str(uploads)),
            patch("notifications.tasks.send_push_to_user.delay"),
        ):
            process_recording(recording.pk)
        recording.refresh_from_db()
        return recording

    def test_a_container_is_stored_as_the_edf_alone_with_its_events_and_gaps_as_rows(self, user, tmp_path):
        footer = {
            "version": "1.0",
            "events": [
                _event(start=1, duration=0.5, value="Eyes closed", codes={"epicurrents.eeg": "EEG_ACT_EC"}),
                _event(start=1.5, value="a bedside note") | {"class": "comment"},
            ],
            "interruptions": [[1, 3]],
            "labels": [],
            "subject": {"patientId": None, "recordingDate": None, "recordingId": None},
        }
        recording = self._process(user, tmp_path, _make_container(footer))

        assert recording.status == recording.Status.READY, recording.processing_error
        stored = Path(recording.file_path).read_bytes()
        assert len(stored) == len(_plain_edf())
        assert read_container_marker(stored) is None
        assert recording.file_size == len(stored)
        closed, note = _events(recording)
        assert (closed.name, [c.value for c in _codes(closed)]) == ("Eyes closed", ["EEG_ACT_EC"])
        assert (note.name, _codes(note)) == (PLACEHOLDER_ANNOTATION_NAME, [])
        assert [(g.timestamp, g.duration) for g in Interruption.objects.filter(target_object_id=str(recording.pk))] == [
            (1.0, 3.0)
        ]
        names = set(Annotation.objects.filter(target_object_id=str(recording.pk)).values_list("name", flat=True))
        assert names == {"Source events"}

    def test_an_ordinary_edf_upload_is_unchanged(self, user, tmp_path):
        recording = self._process(user, tmp_path, _plain_edf())
        assert recording.status == recording.Status.READY, recording.processing_error
        assert Path(recording.file_path).stat().st_size == len(_plain_edf())
        assert not Event.objects.filter(target_object_id=str(recording.pk)).exists()

    def test_a_container_that_is_not_one_fails_the_recording(self, user, tmp_path):
        recording = self._process(user, tmp_path, _make_container(None, body=b"not json"))
        assert recording.status == recording.Status.FAILED
        assert "footer" in recording.processing_error

    def test_a_failure_after_the_detach_preserves_the_container_as_uploaded(self, user, tmp_path):
        from recordings.preservation import MODE_FAILED, REASON_FAILED
        from recordings.processors.edf import EdfParseError

        originals = tmp_path / "originals"
        originals.mkdir()
        container = _make_container({"events": [_event(value="HV")]})
        with (
            override_settings(RECORDINGS_ORIGINALS_PATH=str(originals), RECORDINGS_PRESERVE_MODE=MODE_FAILED),
            patch("recordings.processors.edf.process_edf_file", side_effect=EdfParseError("refused")),
        ):
            recording = self._process(user, tmp_path, container)

        assert recording.status == recording.Status.FAILED
        target_dir = originals / recording.stored_name.split(".")[0]
        manifest = json.loads((target_dir / "manifest.json").read_text())
        assert manifest["preservation_reason"] == REASON_FAILED
        (preserved,) = [p for p in target_dir.iterdir() if p.name != "manifest.json"]
        assert preserved.read_bytes() == container


@pytest.mark.django_db
class TestImport:
    def test_a_container_in_the_import_tree_is_stored_the_same_way(self, user, tmp_path):
        from recordings.tests.test_import import _call_import

        src = tmp_path / "src"
        src.mkdir()
        (src / "rec.edf").write_bytes(
            _make_container(
                {"events": [_event(value="HV", codes={"epicurrents.eeg": "EEG_ACT_HV"})], "interruptions": [[1, 2]]}
            )
        )
        with override_settings(RECORDINGS_UPLOAD_PATH=str(tmp_path / "uploads")):
            _call_import(src, user.username, structure="flat")

        from recordings.models import Recording

        recording = Recording.objects.get(author=user)
        assert recording.status == recording.Status.READY
        assert read_container_marker(Path(recording.file_path).read_bytes()) is None
        assert recording.file_size == len(_plain_edf())
        (event,) = _events(recording)
        assert (event.name, [c.value for c in _codes(event)]) == ("Hyperventilation", ["EEG_ACT_HV"])
        assert Interruption.objects.filter(target_object_id=str(recording.pk)).count() == 1
