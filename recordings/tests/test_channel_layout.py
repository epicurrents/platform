"""Contract tests for the ingest-time montage-shape assessment.

Detection keys on the parsed channel structure, never raw label strings — the platform's own
``<label>_orig`` derived-copy convention (a project's signal repair) and re-uploaded platform-processed
files must not read as duplicate electrodes or vendor junk. The assessment is re-derivable from a
cleaned file, so ``refresh_signal_metadata`` keeps it in sync with the bytes on disk. Design in
docs/engineering-notes/channel-deidentification-plan.md (Phase 1b).
"""

import pytest

from recordings.models import RecordingMeta
from recordings.processors.channel_labels import (
    DERIVED_COPY_SUFFIX,
    assess_channel_layout,
    classify_channel,
)
from recordings.processors.edf import parse_edf_header, parse_signal_infos
from recordings.tests.test_channel_deidentification import (
    DETAIL_URL,
    _ingest_fixture,
    _parse_fixture,
)
from recordings.tests.test_edf_processor import _make_edf_data, _make_edf_header


class TestDerivedCopyClassification:
    def test_resolved_base_keeps_suffixed_canonical(self):
        assert classify_channel("Fp1_orig") == ("misc", "Fp1_orig")

    def test_referential_base_collapses_before_suffixing(self):
        assert classify_channel("EEG Fp1-Cz_orig") == ("misc", "Fp1_orig")

    def test_unresolved_base_stays_fail_closed(self):
        assert classify_channel("XYZ99_orig") == ("misc", "")

    def test_classification_is_idempotent(self):
        first = classify_channel("Fp1_orig")
        assert classify_channel(first[1], first[0]) == first

    def test_suffix_constant_matches_repair_convention(self):
        assert DERIVED_COPY_SUFFIX == "_orig"


class TestAssessChannelLayout:
    def _assess(self, signals):
        return assess_channel_layout(_parse_fixture(signals))

    def test_referential(self):
        layout, unresolved = self._assess(
            [
                {"label": "EEG Fp1-Cz", "sample_count": 16},
                {"label": "EEG Fp2-Cz", "sample_count": 16},
            ]
        )
        assert (layout, unresolved) == ("referential", 0)

    def test_bipolar(self):
        layout, unresolved = self._assess(
            [
                {"label": "EEG Fp1-F7", "sample_count": 16},
                {"label": "EEG F7-T7", "sample_count": 16},
            ]
        )
        assert (layout, unresolved) == ("bipolar", 0)

    def test_mixed_reference_export_via_duplicate_bares(self):
        layout, unresolved = self._assess(
            [
                {"label": "EEG Fp1-A1", "sample_count": 16},
                {"label": "EEG Fp1-A2", "sample_count": 16},
            ]
        )
        assert (layout, unresolved) == ("mixed", 0)

    def test_mixed_bare_and_pair_forms(self):
        layout, unresolved = self._assess(
            [
                {"label": "Fp1", "sample_count": 16},
                {"label": "EEG F7-T7", "sample_count": 16},
            ]
        )
        assert (layout, unresolved) == ("mixed", 0)

    def test_unknown_when_no_resolved_eeg(self):
        layout, unresolved = self._assess(
            [
                {"label": "SpO2", "sample_count": 16},
                {"label": "XYZ99", "sample_count": 16},
            ]
        )
        assert (layout, unresolved) == ("unknown", 1)

    def test_unresolved_counts_exclude_annotation_channel(self):
        layout, unresolved = self._assess(
            [
                {"label": "Fp1", "sample_count": 16},
                {"label": "XYZ99", "sample_count": 16},
                {"label": "EDF Annotations", "sample_count": 16},
            ]
        )
        assert (layout, unresolved) == ("referential", 1)

    def test_derived_copies_do_not_distort_layout(self):
        """Fp1 + Fp1_orig is a repaired referential recording, not a duplicate-electrode export."""
        layout, unresolved = self._assess(
            [
                {"label": "Fp1", "sample_count": 16},
                {"label": "Fp1_orig", "sample_count": 16},
            ]
        )
        assert (layout, unresolved) == ("referential", 0)


@pytest.mark.django_db
class TestLayoutPersistence:
    """Uses the shared ingest fixture: Fp1 (resolved) + XYZ99 (unresolved)."""

    def test_ingest_writes_assessment(self, user, tmp_path):
        recording, _ = _ingest_fixture(user, tmp_path)
        meta = RecordingMeta.objects.get()
        assert meta.channel_layout == "referential"
        assert meta.unresolved_channel_count == 1

    def test_refresh_rederives_assessment(self, user, tmp_path):
        recording, _ = _ingest_fixture(user, tmp_path)
        RecordingMeta.objects.update(channel_layout="unknown", unresolved_channel_count=0, signal_count=99)

        from recordings.metadata import refresh_signal_metadata

        result = refresh_signal_metadata(recording)
        assert result.changed
        meta = RecordingMeta.objects.get()
        assert meta.channel_layout == "referential"
        assert meta.unresolved_channel_count == 1

    def test_derived_copy_label_survives_cleaning_on_disk(self, user, tmp_path):
        """A re-uploaded platform-processed file keeps its _orig pairing through ingest."""
        from recordings.processors.edf import process_edf_file

        signals = [
            {"label": "Fp1", "sample_count": 16},
            {"label": "Fp1_orig", "sample_count": 16},
        ]
        path = tmp_path / "repaired.edf"
        path.write_bytes(_make_edf_header(signals=signals) + _make_edf_data(signals))
        process_edf_file(path)
        on_disk = path.read_bytes()
        disk_infos = parse_signal_infos(on_disk, parse_edf_header(on_disk))
        assert [s.label for s in disk_infos] == ["Fp1", "Fp1_orig"]

    def test_assessment_served_to_grantee(self, client, user, make_user):
        import json

        from django.contrib.contenttypes.models import ContentType

        from epicurrents.models import AccessRight
        from recordings.models import Recording

        recording = Recording.objects.create(
            author=user,
            original_name="rec.edf",
            stored_name="FACE0000FACE0000FACE0000FACE0000.edf",
            file_path="/tmp/none.edf",
            file_extension=".edf",
            file_size=1024,
            status=Recording.Status.READY,
        )
        ct = ContentType.objects.get_for_model(recording, for_concrete_model=False)
        RecordingMeta.objects.create(
            content_type=ct,
            object_id=str(recording.pk),
            format="edf",
            duration=1.0,
            data_record_count=1,
            data_record_duration=1.0,
            signal_count=2,
            discontinuous=False,
            channel_layout="mixed",
            unresolved_channel_count=3,
        )
        reader = make_user()
        AccessRight.objects.create(
            content_type=ct,
            object_id=str(recording.pk),
            access_giver=user,
            access_target=reader,
            can_read=True,
        )
        client.force_login(reader)
        resp = client.get(DETAIL_URL.format(hash=recording.stored_name.split(".")[0]))
        assert resp.status_code == 200
        meta = json.loads(resp.content)["meta"]
        assert meta["channel_layout"] == "mixed"
        assert meta["unresolved_channel_count"] == 3
