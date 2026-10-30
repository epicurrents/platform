"""Declaring a recording's modality, and the continuity a polysomnography must have.

Each route that sets the modality is covered, because the failure they exist to prevent is a quiet one: a recording
stored as something other than what its author said, or a study with a broken timeline opened as though it had none.
"""

import io
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from django.core.management import CommandError, call_command
from django.test import override_settings
from model_bakery import baker

from annotations.models import Code
from recordings.modalities import (
    PSG,
    RECORDING_MODALITIES,
    ModalityError,
    count_file_gaps,
    ensure_continuous_file,
    normalise_modality,
)
from recordings.models import ImportJob, ImportJobFile, Recording
from recordings.tasks import process_recording
from recordings.testing import make_edf_bytes
from recordings.tests.test_edf_processor import _make_edfplus_file

UPLOAD_URL = "/recordings/api/v1/upload"
HASH = "c" * 32


def _continuous():
    return _make_edfplus_file(n_records=4)


def _interrupted():
    return _make_edfplus_file(n_records=4, discontinuous=True, onsets=[0.0, 1.0, 30.0, 31.0])


def _marked_discontinuous_without_a_gap():
    return _make_edfplus_file(n_records=4, discontinuous=True)


def _spliced_under_a_continuous_marker():
    return _make_edfplus_file(n_records=4, onsets=[0.0, 1.0, 30.0, 31.0])


def _written(tmp_path, content, name="study.edf"):
    path = tmp_path / name
    path.write_bytes(content)
    return path


class TestNormalise:
    def test_a_value_is_trimmed_and_lower_cased(self):
        assert normalise_modality("  PSG ") == PSG

    @pytest.mark.parametrize("value", [None, "", "   "])
    def test_nothing_declared_is_empty(self, value):
        assert normalise_modality(value) == ""

    def test_an_unknown_value_is_refused(self):
        with pytest.raises(ModalityError, match="Unknown modality"):
            normalise_modality("polygraph")

    def test_every_type_the_channel_vote_returns_is_accepted(self):
        assert {"eeg", "eog", "emg", "ekg", "trig", "misc"} <= RECORDING_MODALITIES


class TestContinuity:
    def test_a_continuous_file_stands(self, tmp_path):
        ensure_continuous_file(_written(tmp_path, _continuous()))

    def test_a_plain_edf_has_no_timeline_to_break(self, tmp_path):
        ensure_continuous_file(_written(tmp_path, make_edf_bytes(n_channels=2, n_records=3)))

    def test_the_marker_is_not_the_test(self, tmp_path):
        ensure_continuous_file(_written(tmp_path, _marked_discontinuous_without_a_gap()))

    def test_a_gap_refuses_and_the_message_counts_it(self, tmp_path):
        path = _written(tmp_path, _interrupted())
        assert count_file_gaps(path) == 1
        with pytest.raises(ModalityError, match=r"1 interruption"):
            ensure_continuous_file(path)

    def test_a_splice_under_a_continuous_marker_refuses(self, tmp_path):
        with pytest.raises(ModalityError, match="continuous"):
            ensure_continuous_file(_written(tmp_path, _spliced_under_a_continuous_marker()))

    def test_an_unreadable_timeline_is_not_taken_for_continuous(self, tmp_path):
        path = _written(tmp_path, _continuous()[:-100])
        with pytest.raises(ModalityError, match="cannot be read"):
            ensure_continuous_file(path)

    def test_a_signal_block_too_short_to_parse_is_not_taken_for_continuous(self, tmp_path):
        path = _written(tmp_path, _continuous()[:300])
        with pytest.raises(ModalityError, match="cannot be read"):
            ensure_continuous_file(path)

    def test_a_missing_file_refuses(self, tmp_path):
        with pytest.raises(ModalityError, match="cannot be read"):
            ensure_continuous_file(tmp_path / "absent.edf")

    def test_another_format_refuses(self, tmp_path):
        with pytest.raises(ModalityError, match="EDF or BDF"):
            ensure_continuous_file(_written(tmp_path, b"not a recording", "study.csv"))


def _stage(user, tmp_path, content, *, modality=""):
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
        status=Recording.Status.PENDING,
    )
    return recording, staging, uploads


def _ingest(user, tmp_path, content, *, modality=""):
    recording, staging, uploads = _stage(user, tmp_path, content, modality=modality)
    with (
        override_settings(RECORDINGS_STAGING_PATH=str(staging), RECORDINGS_UPLOAD_PATH=str(uploads)),
        patch("notifications.tasks.send_push_to_user.delay"),
    ):
        process_recording(recording.pk)
    recording.refresh_from_db()
    return recording


@pytest.mark.django_db
class TestIngest:
    def test_an_undeclared_recording_is_eeg(self, user, tmp_path):
        assert _ingest(user, tmp_path, _continuous()).modality == "eeg"

    def test_a_declared_modality_survives_processing(self, user, tmp_path):
        recording = _ingest(user, tmp_path, _continuous(), modality=PSG)
        assert recording.status == Recording.Status.READY
        assert recording.modality == PSG

    def test_a_study_keeps_its_reference_through_the_real_ingest(self, user, tmp_path):
        content = bytearray(_continuous())
        content[256:272] = b"EEG C4-M1".ljust(16)
        recording = _ingest(user, tmp_path, bytes(content), modality=PSG)
        assert Path(recording.file_path).read_bytes()[256:272].rstrip() == b"C4-M1"

    def test_an_eeg_loses_it_as_it_always_did(self, user, tmp_path):
        content = bytearray(_continuous())
        content[256:272] = b"EEG C4-M1".ljust(16)
        recording = _ingest(user, tmp_path, bytes(content), modality="eeg")
        assert Path(recording.file_path).read_bytes()[256:272].rstrip() == b"C4"

    def test_an_interrupted_study_fails_with_the_reason(self, user, tmp_path):
        recording = _ingest(user, tmp_path, _interrupted(), modality=PSG)
        assert recording.status == Recording.Status.FAILED
        assert "continuous" in recording.processing_error

    def test_a_refused_study_keeps_the_bytes_as_uploaded(self, user, tmp_path):
        # A pass that ran first would leave failed-mode preservation holding rewritten bytes.
        content = _interrupted()
        recording = _ingest(user, tmp_path, content, modality=PSG)
        assert Path(recording.file_path).read_bytes() == content
        assert recording.stored_hash == ""

    def test_an_interrupted_eeg_is_unaffected(self, user, tmp_path):
        recording = _ingest(user, tmp_path, _interrupted(), modality="eeg")
        assert recording.status == Recording.Status.READY


@pytest.mark.django_db
class TestUpload:
    def _upload(self, client, tmp_path, query=""):
        (tmp_path / "staging").mkdir(exist_ok=True)
        (tmp_path / "uploads").mkdir(exist_ok=True)
        upload = io.BytesIO(b"fake edf data")
        upload.name = "study.edf"
        with (
            override_settings(
                RECORDINGS_STAGING_PATH=str(tmp_path / "staging"),
                RECORDINGS_UPLOAD_PATH=str(tmp_path / "uploads"),
            ),
            patch("recordings.tasks.process_recording.delay"),
        ):
            return client.post(f"{UPLOAD_URL}{query}", {"file": upload}, format="multipart")

    def test_the_declaration_is_stored_with_the_row(self, auth_client, tmp_path):
        client, _ = auth_client
        assert self._upload(client, tmp_path, "?modality=PSG").status_code == 202
        assert Recording.objects.get().modality == PSG

    def test_nothing_declared_leaves_the_row_for_ingest_to_default(self, auth_client, tmp_path):
        client, _ = auth_client
        assert self._upload(client, tmp_path).status_code == 202
        assert Recording.objects.get().modality == ""

    def test_an_unknown_modality_writes_nothing(self, auth_client, tmp_path):
        client, _ = auth_client
        assert self._upload(client, tmp_path, "?modality=polygraph").status_code == 400
        assert not Recording.objects.exists()
        # A staged file with no row is the upload as it arrived, out of reach of every purge and erasure path.
        assert list((tmp_path / "staging").iterdir()) == []


@pytest.mark.django_db
class TestPatch:
    def _recording(self, user, tmp_path, content, *, modality="eeg"):
        path = _written(tmp_path, content, f"{HASH}.edf")
        return baker.make(
            Recording,
            author=user,
            stored_name=f"{HASH}.edf",
            file_extension=".edf",
            file_path=str(path),
            file_size=len(content),
            modality=modality,
            status=Recording.Status.READY,
        )

    def _patch(self, client, modality):
        return client.patch(
            f"/recordings/api/v1/{HASH}", json.dumps({"modality": modality}), content_type="application/json"
        )

    def test_a_continuous_recording_can_become_a_study(self, auth_client, tmp_path):
        client, user = auth_client
        recording = self._recording(user, tmp_path, _continuous())
        assert self._patch(client, "psg").status_code == 200
        recording.refresh_from_db()
        assert recording.modality == PSG

    def test_an_interrupted_recording_cannot(self, auth_client, tmp_path):
        client, user = auth_client
        recording = self._recording(user, tmp_path, _interrupted())
        response = self._patch(client, "psg")
        assert response.status_code == 400
        assert "continuous" in response.json()["detail"]
        recording.refresh_from_db()
        assert recording.modality == "eeg"

    def test_an_unknown_modality_is_refused(self, auth_client, tmp_path):
        client, user = auth_client
        self._recording(user, tmp_path, _continuous())
        assert self._patch(client, "polygraph").status_code == 400

    def test_a_linked_file_that_changed_is_not_read(self, auth_client, tmp_path):
        client, user = auth_client
        self._recording(user, tmp_path, _continuous())
        with patch("recordings.api.v1.ninja.linked_file_change", return_value="the linked file has changed"):
            response = self._patch(client, "psg")
        assert response.status_code == 400
        assert "changed" in response.json()["detail"]

    @pytest.mark.parametrize(("stored", "declared"), [("eeg", "psg"), ("psg", "eeg"), ("psg", "")])
    def test_a_linked_recording_keeps_its_side_of_the_line(self, auth_client, tmp_path, stored, declared):
        # Its labels were worked out from this value, and serving reads it.
        client, user = auth_client
        recording = self._recording(user, tmp_path, _continuous(), modality=stored)
        Recording.objects.filter(pk=recording.pk).update(storage=Recording.Storage.LINKED)
        assert self._patch(client, declared).status_code == 400
        recording.refresh_from_db()
        assert recording.modality == stored

    def test_a_linked_recording_can_change_between_other_modalities(self, auth_client, tmp_path):
        client, user = auth_client
        recording = self._recording(user, tmp_path, _continuous(), modality="eeg")
        Recording.objects.filter(pk=recording.pk).update(storage=Recording.Storage.LINKED)
        assert self._patch(client, "emg").status_code == 200

    def test_a_stored_value_outside_the_list_can_be_sent_back(self, auth_client, tmp_path):
        # The edit dialog sends the field whether or not it was touched.
        client, user = auth_client
        self._recording(user, tmp_path, _continuous(), modality="resp")
        assert self._patch(client, "resp").status_code == 200


@pytest.mark.django_db
class TestImport:
    def _import(self, user, tmp_path, source, **options):
        uploads = tmp_path / "uploads"
        uploads.mkdir(exist_ok=True)
        with override_settings(RECORDINGS_UPLOAD_PATH=str(uploads)):
            call_command(
                "import_recordings", str(source), username=user.get_username(), stdout=io.StringIO(), **options
            )

    def _source(self, tmp_path, content):
        source = tmp_path / "source"
        source.mkdir(exist_ok=True)
        _written(source, content)
        return source

    def test_the_channel_vote_answers_with_nothing_declared(self, user, tmp_path):
        self._import(user, tmp_path, self._source(tmp_path, _continuous()))
        assert Recording.objects.get().modality == "eeg"

    def test_a_declared_study_is_declared_before_its_rows_are_written(self, user, tmp_path):
        # A scoring term is written only on a study, and the rows are written before the final transition.
        content = _make_edfplus_file(n_records=4, tals_per_record=[[b"+0.0\x14Sleep stage 2\x14\x00"]])
        self._import(user, tmp_path, self._source(tmp_path, content), modality="psg")
        assert Code.objects.filter(standard="epicurrents.psg", value="PSG_STAGE_N2").count() == 1

    def test_a_declaration_replaces_the_vote(self, user, tmp_path):
        self._import(user, tmp_path, self._source(tmp_path, _continuous()), modality="psg")
        assert Recording.objects.get().modality == PSG

    def test_an_interrupted_study_is_a_failed_file_and_no_recording(self, user, tmp_path):
        self._import(user, tmp_path, self._source(tmp_path, _interrupted()), modality="psg")
        assert not Recording.objects.exists()
        assert ImportJobFile.objects.get().status == ImportJobFile.Status.FAILED

    def test_an_unknown_modality_stops_the_command(self, user, tmp_path):
        with pytest.raises(CommandError, match="Unknown modality"):
            self._import(user, tmp_path, self._source(tmp_path, _continuous()), modality="polygraph")

    def _leave_job_in_progress(self, user, tmp_path, *, modality):
        job = ImportJob.objects.create(
            owner=user,
            source_path=str(self._source(tmp_path, _continuous())),
            pipeline_label="import",
            structure=ImportJob.Structure.RECURSIVE,
            modality=modality,
        )
        return job

    def test_a_declared_job_records_its_modality(self, user, tmp_path):
        self._import(user, tmp_path, self._source(tmp_path, _continuous()), modality="psg")
        assert ImportJob.objects.get().modality == PSG

    def test_a_resumed_job_keeps_its_modality_without_the_flag(self, user, tmp_path):
        job = self._leave_job_in_progress(user, tmp_path, modality="psg")
        self._import(user, tmp_path, tmp_path / "source", resume=True)
        assert Recording.objects.get().modality == PSG
        assert job.pk == ImportJob.objects.get().pk

    def test_a_resumed_job_refuses_another_modality(self, user, tmp_path):
        self._leave_job_in_progress(user, tmp_path, modality="psg")
        with pytest.raises(CommandError, match="keeps it"):
            self._import(user, tmp_path, tmp_path / "source", resume=True, modality="eeg")
