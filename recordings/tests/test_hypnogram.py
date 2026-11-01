"""A hypnogram file beside a study: read once, aligned on the raw headers, kept only as coded events."""

import io
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.test import override_settings

from annotations.models import Annotation, Code, Event
from recordings.hypnogram import (
    MAX_HYPNOGRAM_BYTES,
    HypnogramError,
    is_annotation_file,
    pair_hypnograms,
    read_header_block,
    read_hypnogram,
)
from recordings.models import ImportJobFile, Recording
from recordings.processors.edf import EdfParseError, parse_edf_header
from recordings.tasks import process_recording
from recordings.testing import make_edf_bytes
from recordings.tests.test_edf_processor import _make_edfplus_file

UPLOAD_URL = "/recordings/api/v1/upload"
HASH = "d" * 32
SUBJECT = "X F X Female_33yr"
OTHER_SUBJECT = "X M X Male_60yr"
START = ("24.04.89", "16.13.00")

W, N2, REM, UNKNOWN = "Sleep stage W", "Sleep stage 2", "Sleep stage R", "Sleep stage ?"


def _field(text, width):
    return text.ljust(width).encode("ascii")


def _hypnogram(
    tals=((0, 60, W), (60, 120, N2), (180, 180, REM)),
    *,
    start=START,
    subject=SUBJECT,
    format_name="EDF",
    record_duration="0",
    cut=0,
):
    """An annotation-only file laid out as Sleep-EDF ships one: one record, of duration 0."""
    body = b"+0\x14\x14\x00"
    for onset, duration, label in tals:
        span = b"" if duration is None else b"\x15" + str(duration).encode()
        body += f"+{onset}".encode() + span + b"\x14" + label.encode() + b"\x14\x00"
    width = 3 if format_name == "BDF" else 2
    samples = (len(body) + width) // width
    data_block = body.ljust(samples * width, b"\x00")
    header = b"".join(
        (
            b"\xffBIOSEMI" if format_name == "BDF" else _field("0", 8),
            _field(subject, 80),
            _field("Startdate 24-APR-1989 X X X", 80),
            _field(start[0], 8),
            _field(start[1], 8),
            _field("512", 8),
            _field(f"{format_name}+C", 44),
            _field("1", 8),
            _field(record_duration, 8),
            _field("1", 4),
        )
    )
    signal = b"".join(
        (
            _field(f"{format_name} Annotations", 16),
            _field("", 80),
            _field("", 8),
            _field("-1", 8),
            _field("1", 8),
            _field("-8388608" if format_name == "BDF" else "-32768", 8),
            _field("8388607" if format_name == "BDF" else "32767", 8),
            _field("", 80),
            _field(str(samples), 8),
            _field("", 32),
        )
    )
    data = header + signal + data_block
    return data[:-cut] if cut else data


def _study(*, start=START, subject=SUBJECT, tals_per_record=None, records=12):
    """A continuous six-minute study with the given identification in its raw header."""
    content = bytearray(_make_edfplus_file(n_records=records, rec_duration=30.0, tals_per_record=tals_per_record))
    content[8:88] = _field(subject, 80)
    content[168:176] = _field(start[0], 8)
    content[176:184] = _field(start[1], 8)
    return bytes(content)


def _block(content):
    return read_header_block(io.BytesIO(content))


class TestRecognisingTheFile:
    def test_a_zero_duration_file_is_an_annotation_file(self):
        assert is_annotation_file(_block(_hypnogram()))

    def test_a_recording_is_not(self):
        assert not is_annotation_file(_block(_study()))

    @pytest.mark.parametrize("content", [b"", b"fake edf data", b"0" * 256])
    def test_something_else_is_not(self, content):
        assert not is_annotation_file(_block(content))

    def test_the_parser_stays_strict_for_everything_else(self):
        with pytest.raises(EdfParseError, match="data_record_duration"):
            parse_edf_header(_hypnogram())


class TestReading:
    def test_the_stages_become_codes_and_times_and_nothing_else(self):
        assert read_hypnogram(_hypnogram(), _block(_study())) == [
            {"onset": 0.0, "duration": 60.0, "code": "PSG_STAGE_W"},
            {"onset": 60.0, "duration": 120.0, "code": "PSG_STAGE_N2"},
            {"onset": 180.0, "duration": 180.0, "code": "PSG_STAGE_REM"},
        ]

    def test_nothing_from_either_header_survives(self):
        answer = json.dumps(read_hypnogram(_hypnogram(), _block(_study())))
        assert "Female" not in answer
        assert "1989" not in answer

    def test_a_bdf_hypnogram_reads(self):
        events = read_hypnogram(_hypnogram(format_name="BDF"), _block(_study()))
        assert [event["code"] for event in events] == ["PSG_STAGE_W", "PSG_STAGE_N2", "PSG_STAGE_REM"]

    def test_a_record_duration_other_than_zero_is_also_a_hypnogram(self):
        assert read_hypnogram(_hypnogram(record_duration="30"), _block(_study()))

    def test_a_hypnogram_starting_later_moves_onto_the_study(self):
        events = read_hypnogram(_hypnogram(((0, 30, W),), start=("24.04.89", "16.14.00")), _block(_study()))
        assert events == [{"onset": 60.0, "duration": 30.0, "code": "PSG_STAGE_W"}]

    def test_a_hypnogram_starting_earlier_loses_what_precedes_the_study(self):
        events = read_hypnogram(
            _hypnogram(((0, 90, W), (90, 30, N2)), start=("24.04.89", "16.12.00")), _block(_study())
        )
        assert events == [
            {"onset": 0.0, "duration": 30.0, "code": "PSG_STAGE_W"},
            {"onset": 30.0, "duration": 30.0, "code": "PSG_STAGE_N2"},
        ]

    def test_a_stage_running_past_the_end_is_cut_to_it(self):
        # Sleep-EDF's last event is often an hours-long unscored span.
        events = read_hypnogram(_hypnogram(((0, 300, N2), (300, 3600, UNKNOWN))), _block(_study()))
        assert events[-1] == {"onset": 300.0, "duration": 60.0, "code": "PSG_STAGE_UNSCORED"}

    def test_a_stage_wholly_past_the_end_is_dropped(self):
        events = read_hypnogram(_hypnogram(((0, 30, N2), (400, 30, N2))), _block(_study()))
        assert len(events) == 1

    def test_a_stage_with_no_duration_runs_to_the_next(self):
        events = read_hypnogram(_hypnogram(((0, None, W), (90, None, N2))), _block(_study()))
        assert [(event["onset"], event["duration"]) for event in events] == [(0.0, 90.0), (90.0, 30.0)]

    def test_a_run_of_epochs_is_one_event(self):
        # Every event is two audited rows written in the request, and a night is some nine hundred epochs.
        tals = [(epoch * 30, 30, N2) for epoch in range(10)]
        assert read_hypnogram(_hypnogram(tals), _block(_study())) == [
            {"onset": 0.0, "duration": 300.0, "code": "PSG_STAGE_N2"}
        ]

    def test_a_note_is_dropped_without_a_trace(self):
        events = read_hypnogram(_hypnogram(((0, 30, W), (5, 0, "Mrs Virtanen coughed"))), _block(_study()))
        assert [event["code"] for event in events] == ["PSG_STAGE_W"]


class TestRefusing:
    def test_a_recording_is_not_a_hypnogram(self):
        with pytest.raises(HypnogramError, match="not a hypnogram"):
            read_hypnogram(_study(), _block(_study()))

    def test_a_truncated_file_is_not_a_shorter_night(self):
        with pytest.raises(HypnogramError, match="truncated"):
            read_hypnogram(_hypnogram(cut=20), _block(_study()))

    def test_annotations_without_a_stage_are_not_scoring(self):
        with pytest.raises(HypnogramError, match="No sleep stages"):
            read_hypnogram(_hypnogram(((0, 30, "Lights off"),)), _block(_study()))

    def test_another_nights_hypnogram_is_refused(self):
        with pytest.raises(HypnogramError, match="another night"):
            read_hypnogram(_hypnogram(start=("25.04.89", "16.13.00")), _block(_study()))

    def test_one_blanked_date_does_not_read_as_a_match(self):
        with pytest.raises(HypnogramError, match="another night"):
            read_hypnogram(_hypnogram(start=("01.01.85", "00.00.00")), _block(_study()))

    def test_an_unreadable_date_refuses(self):
        with pytest.raises(HypnogramError, match="start times"):
            read_hypnogram(_hypnogram(start=("xx.xx.xx", "16.13.00")), _block(_study()))

    def test_stages_that_all_miss_the_study_are_refused(self):
        with pytest.raises(HypnogramError, match="fall within"):
            read_hypnogram(_hypnogram(((400, 30, N2),)), _block(_study()))

    def test_records_of_no_size_are_refused_rather_than_walked(self):
        content = bytearray(_hypnogram())
        content[236:244] = _field("99999999", 8)
        content[256 + 216 : 256 + 224] = _field("0", 8)
        with pytest.raises(HypnogramError, match="no samples"):
            read_hypnogram(bytes(content), _block(_study()))

    @pytest.mark.parametrize("onset", ["nan", "inf"])
    def test_a_time_that_is_not_a_number_is_refused(self, onset):
        content = _hypnogram().replace(b"+0\x1560\x14", f"+{onset}\x1560\x14".encode())
        with pytest.raises(HypnogramError, match="not a number"):
            read_hypnogram(content, _block(_study()))

    def test_an_oversized_file_is_refused_unread(self):
        with pytest.raises(HypnogramError, match="too large"):
            read_hypnogram(b"0" * (MAX_HYPNOGRAM_BYTES + 1), _block(_study()))

    def test_no_refusal_quotes_the_file(self):
        with pytest.raises(HypnogramError) as refused:
            read_hypnogram(_hypnogram(((0, 30, "Mrs Virtanen"),)), _block(_study()))
        assert "Virtanen" not in str(refused.value)


class TestPairing:
    def _write(self, directory, name, content):
        directory.mkdir(exist_ok=True)
        path = directory / name
        path.write_bytes(content)
        return path

    def test_a_study_pairs_by_header_though_the_names_share_no_stem(self, tmp_path):
        study = self._write(tmp_path, "SC4001E0-PSG.edf", _study())
        hypnogram = self._write(tmp_path, "SC4001EC-Hypnogram.edf", _hypnogram())
        assert pair_hypnograms([study, hypnogram]) == ([study], {study: hypnogram}, [])

    def test_each_night_takes_its_own(self, tmp_path):
        first = self._write(tmp_path, "a.edf", _study())
        second = self._write(tmp_path, "b.edf", _study(start=("25.04.89", "16.13.00")))
        first_scoring = self._write(tmp_path, "c.edf", _hypnogram())
        second_scoring = self._write(tmp_path, "d.edf", _hypnogram(start=("25.04.89", "16.13.00")))
        _, pairs, unpaired = pair_hypnograms([first, second, first_scoring, second_scoring])
        assert pairs == {first: first_scoring, second: second_scoring}
        assert unpaired == []

    def test_another_subject_does_not_pair(self, tmp_path):
        study = self._write(tmp_path, "a.edf", _study())
        hypnogram = self._write(tmp_path, "b.edf", _hypnogram(subject=OTHER_SUBJECT))
        assert pair_hypnograms([study, hypnogram]) == ([study], {}, [hypnogram])

    def test_two_studies_matching_one_hypnogram_are_not_guessed_at(self, tmp_path):
        # What a dataset with blanked headers looks like.
        studies = [self._write(tmp_path, name, _study()) for name in ("a.edf", "b.edf")]
        hypnogram = self._write(tmp_path, "c.edf", _hypnogram())
        assert pair_hypnograms([*studies, hypnogram]) == (studies, {}, [hypnogram])

    def test_two_hypnograms_claiming_one_study_are_not_guessed_at(self, tmp_path):
        # Two scorers of one night: choosing one is the author's call.
        study = self._write(tmp_path, "a.edf", _study())
        hypnograms = [self._write(tmp_path, name, _hypnogram()) for name in ("b.edf", "c.edf")]
        assert pair_hypnograms([study, *hypnograms]) == ([study], {}, hypnograms)

    def test_pairing_does_not_cross_directories(self, tmp_path):
        study = self._write(tmp_path / "one", "a.edf", _study())
        hypnogram = self._write(tmp_path / "two", "b.edf", _hypnogram())
        assert pair_hypnograms([study, hypnogram]) == ([study], {}, [hypnogram])

    def test_a_file_that_cannot_be_read_is_left_for_the_import_to_fail(self, tmp_path):
        gone = tmp_path / "gone.edf"
        assert pair_hypnograms([gone]) == ([gone], {}, [])

    def test_a_file_of_another_format_stays_a_study_unread(self, tmp_path):
        other = self._write(tmp_path, "a.csv", b"time,value\n")
        assert pair_hypnograms([other]) == ([other], {}, [])


def _ingest(user, tmp_path, content, *, scoring=None, push_side_effect=None, modality="psg"):
    staging = tmp_path / "staging"
    uploads = tmp_path / "uploads"
    staging.mkdir(exist_ok=True)
    uploads.mkdir(exist_ok=True)
    stored_name = f"{HASH.upper()}.edf"
    (staging / stored_name).write_bytes(content)
    recording = Recording.objects.create(
        author=user,
        original_name="study.edf",
        stored_name=stored_name,
        file_extension=".edf",
        file_size=len(content),
        file_path=str(staging / stored_name),
        file_hash="a" * 64,
        content_hash="",
        modality=modality,
        pending_scoring=scoring,
        status=Recording.Status.PENDING,
    )
    with (
        override_settings(RECORDINGS_STAGING_PATH=str(staging), RECORDINGS_UPLOAD_PATH=str(uploads)),
        patch("notifications.tasks.send_push_to_user.delay", side_effect=push_side_effect),
    ):
        process_recording(recording.pk)
    recording.refresh_from_db()
    return recording


@pytest.mark.django_db
class TestIngest:
    def test_the_scoring_reaches_the_rows_a_hypnogram_is_read_from(self, user, tmp_path):
        events = read_hypnogram(_hypnogram(), _block(_study()))
        recording = _ingest(user, tmp_path, _study(), scoring=events)
        assert recording.status == Recording.Status.READY
        rows = Event.objects.order_by("timestamp")
        assert [(row.name, row.timestamp, row.duration) for row in rows] == [
            ("Sleep-stage-W", 0.0, 60.0),
            ("Sleep-stage-N2", 60.0, 120.0),
            ("Sleep-stage-REM", 180.0, 180.0),
        ]
        assert set(Code.objects.values_list("standard", flat=True)) == {"epicurrents.psg"}

    def test_it_replaces_stages_embedded_in_the_study(self, user, tmp_path):
        study = _study(tals_per_record=[[b"+0\x1530\x14Sleep stage 1\x14\x00"]])
        events = read_hypnogram(_hypnogram(), _block(study))
        _ingest(user, tmp_path, study, scoring=events)
        assert not Code.objects.filter(value="PSG_STAGE_N1").exists()
        assert Code.objects.filter(value="PSG_STAGE_W").count() == 1

    def test_embedded_stages_stand_without_one(self, user, tmp_path):
        _ingest(user, tmp_path, _study(tals_per_record=[[b"+0\x1530\x14Sleep stage 1\x14\x00"]]))
        assert Code.objects.filter(value="PSG_STAGE_N1").count() == 1

    def test_events_that_are_not_stages_stay_when_it_replaces_the_stages(self, user, tmp_path):
        study = _study(tals_per_record=[[b"+1\x14Lights off\x14\x00"]])
        _ingest(user, tmp_path, study, scoring=read_hypnogram(_hypnogram(), _block(study)))
        assert Event.objects.filter(name="Source annotation").count() == 1

    def test_a_discarding_deployment_still_gets_the_scoring(self, user, tmp_path):
        events = read_hypnogram(_hypnogram(), _block(_study()))
        with override_settings(RECORDINGS_DISCARD_EMBEDDED_ANNOTATIONS=True):
            _ingest(user, tmp_path, _study(), scoring=events)
        assert Code.objects.count() == 3

    def test_no_raw_record_is_written_from_it(self, user, tmp_path):
        _ingest(user, tmp_path, _study(), scoring=read_hypnogram(_hypnogram(), _block(_study())))
        assert not Annotation.objects.exists()

    def test_the_stages_go_with_the_recording(self, user, tmp_path):
        recording = _ingest(user, tmp_path, _study(), scoring=read_hypnogram(_hypnogram(), _block(_study())))
        recording.delete()
        assert not Event.objects.exists()
        assert not Code.objects.exists()

    def test_a_ready_study_holds_nothing_back(self, user, tmp_path):
        recording = _ingest(user, tmp_path, _study(), scoring=read_hypnogram(_hypnogram(), _block(_study())))
        assert recording.pending_scoring is None

    def test_a_study_the_worker_refuses_has_no_stages_and_keeps_none_back(self, user, tmp_path):
        spliced = _make_edfplus_file(n_records=12, rec_duration=30.0, onsets=[*range(0, 300, 30), 900.0, 930.0])
        recording = _ingest(user, tmp_path, spliced, scoring=[{"onset": 0.0, "duration": 30.0, "code": "PSG_STAGE_W"}])
        assert recording.status == Recording.Status.FAILED
        assert recording.pending_scoring is None
        assert not Event.objects.exists()

    def test_an_unexpected_failure_keeps_none_back_either(self, user, tmp_path):
        with (
            patch("recordings.metadata.stored_hash_of", side_effect=OSError("disk")),
            pytest.raises(OSError, match="disk"),
        ):
            _ingest(user, tmp_path, _study(), scoring=[{"onset": 0.0, "duration": 30.0, "code": "PSG_STAGE_W"}])
        recording = Recording.objects.get()
        assert recording.status == Recording.Status.FAILED
        assert recording.pending_scoring is None
        assert not Event.objects.exists()

    def test_the_stages_arrive_with_the_status(self, user, tmp_path):
        # A failed write must not leave a READY study without the scoring it was given.
        events = read_hypnogram(_hypnogram(), _block(_study()))
        with (
            patch("recordings.hypnogram.write_hypnogram", side_effect=ValueError("no")),
            pytest.raises(ValueError, match="no"),
        ):
            _ingest(user, tmp_path, _study(), scoring=events)
        assert Recording.objects.get().status == Recording.Status.FAILED

    @pytest.mark.parametrize("modality", ["eeg", ""])
    def test_scoring_survives_a_change_of_modality(self, user, tmp_path, client, modality):
        # A scorer's work is not derived from the declaration, and an overnight EEG may carry scoring too.
        recording = _ingest(user, tmp_path, _study(), scoring=read_hypnogram(_hypnogram(), _block(_study())))
        client.force_login(user)
        response = client.patch(
            f"/recordings/api/v1/{HASH.upper()}", json.dumps({"modality": modality}), content_type="application/json"
        )
        assert response.status_code == 200
        recording.refresh_from_db()
        assert recording.modality == modality
        assert Code.objects.filter(standard="epicurrents.psg").count() == 3
        assert Event.objects.filter(name="Sleep-stage-N2").count() == 1

    def test_scoring_survives_a_change_of_modality_while_it_waits(self, user, tmp_path):
        recording = _ingest(
            user, tmp_path, _study(), scoring=read_hypnogram(_hypnogram(), _block(_study())), modality="eeg"
        )
        assert recording.status == Recording.Status.READY
        assert Code.objects.filter(standard="epicurrents.psg").count() == 3

    def test_an_audit_failure_takes_the_stages_back_with_the_status(self, user, tmp_path):
        events = read_hypnogram(_hypnogram(), _block(_study()))
        with (
            patch("activity.audit.record_modify_change", side_effect=RuntimeError("chain")),
            pytest.raises(RuntimeError, match="chain"),
        ):
            _ingest(user, tmp_path, _study(), scoring=events)
        assert Recording.objects.get().status == Recording.Status.FAILED
        assert not Event.objects.exists()

    def test_a_notification_that_cannot_be_queued_costs_the_study_nothing(self, user, tmp_path):
        # The handler for a failed task unlinks the file, which is no answer to a broker outage.
        events = read_hypnogram(_hypnogram(), _block(_study()))
        recording = _ingest(user, tmp_path, _study(), scoring=events, push_side_effect=ConnectionError("broker"))
        assert recording.status == Recording.Status.READY
        assert Path(recording.file_path).exists()
        assert Code.objects.count() == 3

    def test_a_waiting_hypnogram_stays_out_of_the_audit_trail(self, user, tmp_path):
        from activity.models import ObjectChangeLog

        _ingest(user, tmp_path, _study(), scoring=read_hypnogram(_hypnogram(), _block(_study())))
        recording_rows = ObjectChangeLog.objects.filter(content_type__model="recording")
        assert recording_rows.exists()
        assert all("PSG_STAGE" not in json.dumps(row.changes, default=str) for row in recording_rows)
        assert all("PSG_STAGE" not in json.dumps(row.before_state, default=str) for row in recording_rows)


@pytest.mark.django_db
class TestUpload:
    def _upload(self, client, tmp_path, *, study=None, hypnogram=None, query="?modality=psg", name="study.edf"):
        (tmp_path / "staging").mkdir(exist_ok=True)
        files = {"file": io.BytesIO(_study() if study is None else study)}
        files["file"].name = name
        if hypnogram is not None:
            files["hypnogram"] = io.BytesIO(hypnogram)
            files["hypnogram"].name = "scoring.edf"
        with (
            override_settings(RECORDINGS_STAGING_PATH=str(tmp_path / "staging")),
            patch("recordings.tasks.process_recording.delay") as delay,
        ):
            response = self._post(client, query, files)
        return response, delay

    def _post(self, client, query, files):
        # The commit callback is what hands the task its message.
        from django.test import TestCase

        with TestCase.captureOnCommitCallbacks(execute=True):
            return client.post(f"{UPLOAD_URL}{query}", files, format="multipart")

    def _refused(self, response, tmp_path, status=400):
        assert response.status_code == status
        assert not Recording.objects.exists()
        # A staged file with no row is out of reach of every purge and erasure path.
        assert list((tmp_path / "staging").iterdir()) == []

    def test_the_scoring_waits_on_the_row(self, auth_client, tmp_path):
        client, _ = auth_client
        response, _ = self._upload(client, tmp_path, hypnogram=_hypnogram())
        assert response.status_code == 202
        assert [event["code"] for event in Recording.objects.get().pending_scoring] == [
            "PSG_STAGE_W",
            "PSG_STAGE_N2",
            "PSG_STAGE_REM",
        ]

    def test_no_reader_can_hold_a_stage_before_the_study(self, auth_client, tmp_path):
        # Every surface reads these rows, so their absence is what makes the answer the same on all of them.
        client, _ = auth_client
        self._upload(client, tmp_path, hypnogram=_hypnogram())
        assert Recording.objects.get().status == Recording.Status.PENDING
        assert not Event.objects.exists()
        assert not Code.objects.exists()

    def test_no_stage_rides_the_task_message(self, auth_client, tmp_path):
        # A stage is a reading about the subject, and no erasure path reaches the broker's append-only file.
        client, _ = auth_client
        _, delay = self._upload(client, tmp_path, hypnogram=_hypnogram())
        assert delay.call_args.args == (Recording.objects.get().pk,)
        assert delay.call_args.kwargs == {"preserve_annotations": False, "channel_map": ""}

    def test_the_hypnogram_is_not_staged(self, auth_client, tmp_path):
        client, _ = auth_client
        self._upload(client, tmp_path, hypnogram=_hypnogram())
        assert len(list((tmp_path / "staging").iterdir())) == 1

    def test_without_one_nothing_is_scored(self, auth_client, tmp_path):
        client, _ = auth_client
        self._upload(client, tmp_path)
        assert Recording.objects.get().pending_scoring is None

    def test_the_study_is_staged_whole_after_its_header_was_read(self, auth_client, tmp_path):
        client, _ = auth_client
        study = _study()
        self._upload(client, tmp_path, study=study, hypnogram=_hypnogram())
        assert Path(Recording.objects.get().file_path).read_bytes() == study

    def test_a_bad_hypnogram_writes_nothing(self, auth_client, tmp_path):
        client, _ = auth_client
        response, _ = self._upload(client, tmp_path, hypnogram=_hypnogram(start=("25.04.89", "16.13.00")))
        self._refused(response, tmp_path)
        assert "another night" in response.json()["detail"]

    def test_a_hypnogram_for_anything_but_a_study_is_refused(self, auth_client, tmp_path):
        client, _ = auth_client
        response, _ = self._upload(client, tmp_path, hypnogram=_hypnogram(), query="?modality=eeg")
        self._refused(response, tmp_path)

    def test_a_hypnogram_needs_a_study_it_can_read(self, auth_client, tmp_path):
        client, _ = auth_client
        response, _ = self._upload(client, tmp_path, study=b"a,b\n", hypnogram=_hypnogram(), name="study.csv")
        self._refused(response, tmp_path)

    def test_a_hypnogram_uploaded_as_the_recording_says_so(self, auth_client, tmp_path):
        client, _ = auth_client
        response, _ = self._upload(client, tmp_path, study=_hypnogram())
        self._refused(response, tmp_path)
        assert "as the hypnogram" in response.json()["detail"]

    def test_an_annotation_file_with_a_real_record_duration_is_left_to_ingest(self, auth_client, tmp_path):
        # It parses, and has always been stored as a recording with no signals.
        client, _ = auth_client
        response, _ = self._upload(client, tmp_path, study=_hypnogram(record_duration="30"), query="")
        assert response.status_code == 202

    def test_the_upload_audit_says_one_was_given(self, auth_client, tmp_path):
        from activity.models import Activity

        client, _ = auth_client
        self._upload(client, tmp_path, hypnogram=_hypnogram())
        assert Activity.objects.get(verb="recordings.upload").metadata["hypnogram_given"] is True

    def test_an_oversized_hypnogram_is_refused(self, auth_client, tmp_path):
        client, _ = auth_client
        response, _ = self._upload(client, tmp_path, hypnogram=b"0" * (MAX_HYPNOGRAM_BYTES + 1))
        self._refused(response, tmp_path, status=413)

    def test_an_unparsable_recording_still_reaches_ingest(self, auth_client, tmp_path):
        client, _ = auth_client
        response, _ = self._upload(client, tmp_path, study=b"fake edf data")
        assert response.status_code == 202


@pytest.mark.django_db
class TestImport:
    def _import(self, user, tmp_path, source, **options):
        uploads = tmp_path / "uploads"
        uploads.mkdir(exist_ok=True)
        output = io.StringIO()
        with override_settings(RECORDINGS_UPLOAD_PATH=str(uploads)):
            call_command("import_recordings", str(source), username=user.get_username(), stdout=output, **options)
        return output.getvalue()

    def _source(self, tmp_path, *files):
        source = tmp_path / "source"
        source.mkdir(exist_ok=True)
        for name, content in files:
            (source / name).write_bytes(content)
        return source

    def test_a_dataset_imports_with_its_scoring(self, user, tmp_path):
        source = self._source(tmp_path, ("SC4001E0-PSG.edf", _study()), ("SC4001EC-Hypnogram.edf", _hypnogram()))
        self._import(user, tmp_path, source, modality="psg")
        assert Recording.objects.get().modality == "psg"
        assert Code.objects.filter(standard="epicurrents.psg").count() == 3
        assert ImportJobFile.objects.get().relative_path == "SC4001E0-PSG.edf"

    def test_the_hypnogram_is_not_copied(self, user, tmp_path):
        source = self._source(tmp_path, ("a.edf", _study()), ("b.edf", _hypnogram()))
        self._import(user, tmp_path, source, modality="psg")
        assert len(list((tmp_path / "uploads").iterdir())) == 1

    def test_an_unpaired_hypnogram_is_named_and_imports_nothing(self, user, tmp_path):
        source = self._source(tmp_path, ("a.edf", _study()), ("b.edf", _hypnogram(subject=OTHER_SUBJECT)))
        output = self._import(user, tmp_path, source, modality="psg")
        assert "UNPAIRED b.edf" in output
        assert Recording.objects.count() == 1
        assert not Code.objects.exists()

    def test_a_paired_hypnogram_that_cannot_stand_fails_its_study(self, user, tmp_path):
        source = self._source(tmp_path, ("a.edf", _study()), ("b.edf", _hypnogram(((0, 30, "Lights off"),))))
        self._import(user, tmp_path, source, modality="psg")
        assert not Recording.objects.exists()
        job_file = ImportJobFile.objects.get()
        assert job_file.status == ImportJobFile.Status.FAILED
        assert "No sleep stages" in job_file.error

    def test_a_run_not_declared_psg_treats_it_as_it_always_did(self, user, tmp_path):
        source = self._source(tmp_path, ("a.edf", make_edf_bytes(n_channels=1, n_records=2)), ("b.edf", _hypnogram()))
        self._import(user, tmp_path, source)
        assert ImportJobFile.objects.filter(status=ImportJobFile.Status.FAILED).count() == 1
        assert not Code.objects.exists()
