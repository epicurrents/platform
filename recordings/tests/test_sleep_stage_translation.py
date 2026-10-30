"""The stages a scored EDF+ file arrives with reach the coded events a hypnogram reads."""

import pytest
from model_bakery import baker

from annotations.models import Code, Event
from recordings.event_translation import (
    PLACEHOLDER_ANNOTATION_NAME,
    SourceEvent,
    translate_source_event,
    write_source_events,
)
from recordings.models import Recording


def _code(label):
    term = translate_source_event(SourceEvent(onset=0.0, duration=30.0, label=label))
    return term.code if term else None


@pytest.mark.parametrize(
    ("label", "code"),
    [
        ("Sleep stage W", "PSG_STAGE_W"),
        ("Sleep stage 1", "PSG_STAGE_N1"),
        ("Sleep stage N2", "PSG_STAGE_N2"),
        ("Sleep stage 3", "PSG_STAGE_N3"),
        ("Sleep stage 4", "PSG_STAGE_N3"),
        ("Sleep stage R", "PSG_STAGE_REM"),
        ("SLEEP-REM", "PSG_STAGE_REM"),
        ("Sleep stage ?", "PSG_STAGE_UNSCORED"),
        ("Movement time", "PSG_STAGE_UNSCORED"),
    ],
)
def test_a_scored_stage_translates(label, code):
    assert _code(label) == code


@pytest.mark.parametrize("label", ["Wake", "Awake", "REM", "N2", "Sleep", "Stage 2 seizure"])
def test_a_note_that_only_sounds_like_a_stage_does_not(label):
    assert _code(label) is None


def test_a_translated_stage_is_written_under_the_scoring_standard():
    term = translate_source_event(SourceEvent(onset=0.0, duration=30.0, label="Sleep stage 2"))
    assert term.standard == "epicurrents.psg"
    assert term.name == "Sleep-stage-N2"


@pytest.mark.django_db
class TestOnlyAStudyTakesScoringTerms:
    SOURCES = [SourceEvent(onset=0.0, duration=30.0, label="Sleep stage 2")]

    def _recording(self, user, modality):
        return baker.make(Recording, author=user, modality=modality, stored_name="a" * 32 + ".edf")

    def test_a_study_takes_the_code(self, user):
        recording = self._recording(user, "psg")
        assert write_source_events(recording, self.SOURCES, hash_prefix="t") == 1
        assert Code.objects.get().value == "PSG_STAGE_N2"

    @pytest.mark.parametrize("modality", ["eeg", ""])
    def test_any_other_recording_gets_the_placeholder_it_always_did(self, user, modality):
        recording = self._recording(user, modality)
        assert write_source_events(recording, self.SOURCES, hash_prefix="t") == 0
        assert not Code.objects.exists()
        assert Event.objects.get().name == PLACEHOLDER_ANNOTATION_NAME

    def test_a_discarding_deployment_writes_nothing_for_it(self, user):
        recording = self._recording(user, "eeg")
        assert write_source_events(recording, self.SOURCES, hash_prefix="t", discard_text=True) == 0
        assert not Event.objects.exists()

    def test_a_declared_code_is_held_to_the_same_line(self, user):
        # The viewer's container declares its own codes, which is another way in for the same term.
        recording = self._recording(user, "eeg")
        sources = [SourceEvent(onset=0.0, duration=30.0, code="PSG_STAGE_N2")]
        assert write_source_events(recording, sources, hash_prefix="t") == 0
        assert not Code.objects.exists()

    def test_an_acquisition_term_is_unaffected(self, user):
        recording = self._recording(user, "eeg")
        sources = [SourceEvent(onset=0.0, duration=1.0, code="BIO_TECH_CALIBRATION")]
        assert write_source_events(recording, sources, hash_prefix="t") == 1
