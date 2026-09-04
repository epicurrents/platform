"""Tests for re-deriving stored signal metadata from the file on disk.

The drift these cover is silent by nature: nothing raises when ``RecordingMeta`` describes a file
that has since been rewritten, and the damage only appears as bytes served at the wrong offsets. So
the assertions are about the stored rows matching the file, and about the audit trail carrying a
digest that still verifies afterwards.
"""

import pytest
from django.contrib.contenttypes.models import ContentType

from activity.derived_state import verify_derived_state
from activity.models import Activity, ObjectChangeLog
from activity.system_activity import with_system_activity
from recordings.metadata import MetadataRefreshError, refresh_signal_metadata
from recordings.models import Recording, RecordingMeta, SignalInfo
from recordings.testing import make_edf_bytes


def _make_recording(user, tmp_path, *, n_channels: int, stored_signal_count: int | None = None):
    """Write an EDF with *n_channels* and register metadata claiming *stored_signal_count*.

    When ``stored_signal_count`` differs from ``n_channels`` the recording is in exactly the state a
    reprocessing stage leaves behind: a correct file, and rows describing an older one.
    """
    data = make_edf_bytes(n_channels=n_channels, n_records=2)
    path = tmp_path / f"rec{n_channels}.edf"
    path.write_bytes(data)
    recording = Recording.objects.create(
        author=user,
        stored_name=path.name,
        file_path=str(path),
        file_extension=".edf",
        file_size=len(data),
        status=Recording.Status.READY,
    )
    # Rows are derived from the file rather than invented, so "in sync" in these tests means the
    # database genuinely describes the bytes on disk. Hand-written values would drift from the
    # builder's output and make the no-op case pass for the wrong reason.
    from recordings.processors.edf import parse_edf_header, parse_signal_infos

    parsed_header = parse_edf_header(data)
    parsed = parse_signal_infos(data, parsed_header)

    claimed = n_channels if stored_signal_count is None else stored_signal_count
    meta = RecordingMeta.objects.create(
        content_type=ContentType.objects.get_for_model(recording, for_concrete_model=False),
        object_id=str(recording.pk),
        format="edf",
        duration=2.0,
        data_record_count=2,
        data_record_duration=1.0,
        signal_count=claimed,
        discontinuous=False,
    )
    SignalInfo.objects.bulk_create(
        [
            SignalInfo(
                meta=meta,
                index=i,
                label=s.label,
                signal_type=s.signal_type,
                canonical_label=s.canonical_label,
                physical_unit=s.physical_unit,
                transducer_type=s.transducer_type,
                prefiltering=s.prefiltering,
                physical_min=s.physical_min,
                physical_max=s.physical_max,
                digital_min=s.digital_min,
                digital_max=s.digital_max,
                units_per_bit=s.units_per_bit,
                digital_offset=s.digital_offset,
                sample_count=s.sample_count,
                sampling_rate=s.sampling_rate,
                highpass=s.highpass,
                lowpass=s.lowpass,
                notch=s.notch,
                is_annotation_channel=s.is_annotation_channel,
            )
            for i, s in enumerate(parsed[:claimed])
        ]
    )
    return recording, meta


@pytest.mark.django_db
class TestRefreshSignalMetadata:
    def test_in_sync_recording_is_left_alone(self, user, tmp_path):
        recording, meta = _make_recording(user, tmp_path, n_channels=3)
        result = refresh_signal_metadata(recording)
        assert result.changed is False
        meta.refresh_from_db()
        assert meta.signal_count == 3

    def test_drifted_signal_count_is_corrected_from_the_file(self, user, tmp_path):
        recording, meta = _make_recording(user, tmp_path, n_channels=6, stored_signal_count=3)
        result = refresh_signal_metadata(recording)
        assert result.changed is True
        assert (result.stored_signal_count, result.file_signal_count) == (3, 6)
        meta.refresh_from_db()
        assert meta.signal_count == 6
        assert SignalInfo.objects.filter(meta=meta).count() == 6
        assert list(SignalInfo.objects.filter(meta=meta).values_list("index", flat=True)) == [0, 1, 2, 3, 4, 5]

    def test_corrected_metadata_describes_the_real_byte_layout(self, user, tmp_path):
        """The property the whole module exists for: header size derived from the stored count has
        to land on a record boundary, or every byte offset computed from it is wrong."""
        recording, meta = _make_recording(user, tmp_path, n_channels=6, stored_signal_count=3)
        refresh_signal_metadata(recording)
        meta.refresh_from_db()
        header_size = 256 * (1 + meta.signal_count)
        signal_bytes = recording.file_size - header_size
        assert signal_bytes > 0
        assert signal_bytes % meta.data_record_count == 0

    def test_dry_run_reports_drift_without_writing(self, user, tmp_path):
        recording, meta = _make_recording(user, tmp_path, n_channels=6, stored_signal_count=3)
        result = refresh_signal_metadata(recording, dry_run=True)
        assert result.changed is True
        meta.refresh_from_db()
        assert meta.signal_count == 3
        assert SignalInfo.objects.filter(meta=meta).count() == 3

    def test_repeated_refresh_is_idempotent(self, user, tmp_path):
        recording, _ = _make_recording(user, tmp_path, n_channels=6, stored_signal_count=3)
        assert refresh_signal_metadata(recording).changed is True
        assert refresh_signal_metadata(recording).changed is False

    def test_missing_file_raises_rather_than_clearing_metadata(self, user, tmp_path):
        recording, meta = _make_recording(user, tmp_path, n_channels=3)
        (tmp_path / "rec3.edf").unlink()
        with pytest.raises(MetadataRefreshError):
            refresh_signal_metadata(recording)
        meta.refresh_from_db()
        assert meta.signal_count == 3

    def test_channel_descriptor_change_is_detected_without_a_count_change(self, user, tmp_path):
        """Same channel count, different contents. Comparing counts alone would call this in sync
        and leave the stored descriptors describing filter settings the file no longer has."""
        recording, meta = _make_recording(user, tmp_path, n_channels=3)
        row = SignalInfo.objects.filter(meta=meta).order_by("index").first()
        row.prefiltering = "HP:0.5Hz LP:70Hz"
        row.save(update_fields=["prefiltering"])
        assert refresh_signal_metadata(recording).changed is True
        # The rows are replaced wholesale, so the corrected descriptor is on a new row.
        rewritten = SignalInfo.objects.filter(meta=meta).order_by("index").first()
        assert rewritten.prefiltering != "HP:0.5Hz LP:70Hz"

    def test_recording_without_metadata_is_refused_rather_than_backfilled(self, user, tmp_path):
        """A recording with no metadata never completed ingest — FAILED recordings are the normal
        case. Writing rows here would make an unprocessed file look processed."""
        recording, meta = _make_recording(user, tmp_path, n_channels=3)
        SignalInfo.objects.filter(meta=meta).delete()
        meta.delete()
        with pytest.raises(MetadataRefreshError, match="not been processed"):
            refresh_signal_metadata(recording)
        assert not RecordingMeta.objects.filter(object_id=str(recording.pk)).exists()

    def test_self_inconsistent_header_is_refused(self, user, tmp_path):
        """The stated header length and the channel count both come from the file, so a file where
        they disagree is malformed and gets nothing written from it."""
        recording, meta = _make_recording(user, tmp_path, n_channels=3)
        path = tmp_path / "rec3.edf"
        data = bytearray(path.read_bytes())
        data[184:192] = b"99999   "  # states a header length its 3 channels do not imply
        path.write_bytes(bytes(data))
        with pytest.raises(MetadataRefreshError, match="self-inconsistent"):
            refresh_signal_metadata(recording)

    def test_negative_header_length_does_not_reach_read(self, user, tmp_path):
        """`parse_edf_header` swallows a malformed length field, and a negative one reaching
        `read()` means "read to EOF" — four bytes of file content driving an unbounded allocation."""
        recording, meta = _make_recording(user, tmp_path, n_channels=3)
        path = tmp_path / "rec3.edf"
        data = bytearray(path.read_bytes())
        data[184:192] = b"-1      "
        path.write_bytes(bytes(data))
        with pytest.raises(MetadataRefreshError, match="self-inconsistent"):
            refresh_signal_metadata(recording)

    def test_truncated_file_is_refused(self, user, tmp_path):
        recording, meta = _make_recording(user, tmp_path, n_channels=3)
        path = tmp_path / "rec3.edf"
        path.write_bytes(path.read_bytes()[:200])
        with pytest.raises(MetadataRefreshError):
            refresh_signal_metadata(recording)

    def test_non_edf_recording_is_rejected(self, user, tmp_path):
        recording, _ = _make_recording(user, tmp_path, n_channels=3)
        recording.file_extension = ".csv"
        recording.save(update_fields=["file_extension"])
        with pytest.raises(MetadataRefreshError):
            refresh_signal_metadata(recording)


@pytest.mark.django_db
class TestRefreshAuditTrail:
    """The refresh replaces audited derived rows, so it owes the trail a re-baselined digest."""

    def test_digest_is_rebaselined_and_verifies(self, user, tmp_path):
        recording, _ = _make_recording(user, tmp_path, n_channels=6, stored_signal_count=3)
        with with_system_activity("recordings.metadata.refresh", interface=Activity.Interface.COMMAND):
            refresh_signal_metadata(recording)
        activity = Activity.objects.filter(verb="recordings.metadata.refresh").latest("id")
        rows = ObjectChangeLog.objects.filter(activity=activity, content_type__model="recording")
        assert rows.count() == 1
        assert verify_derived_state(rows.first()).ok is True

    def test_channel_replacement_writes_no_per_row_entries(self, user, tmp_path):
        """The digest covers the SignalInfo set; a row per deleted channel would say nothing it
        does not already say, and would bury the parent transition on a 100-channel recording."""
        recording, _ = _make_recording(user, tmp_path, n_channels=6, stored_signal_count=3)
        with with_system_activity("recordings.metadata.refresh", interface=Activity.Interface.COMMAND):
            refresh_signal_metadata(recording)
        activity = Activity.objects.filter(verb="recordings.metadata.refresh").latest("id")
        assert not ObjectChangeLog.objects.filter(activity=activity, content_type__model="signalinfo").exists()

    def test_suppression_does_not_leak_past_the_refresh(self, user, tmp_path):
        """The flag is a ContextVar; leaving it set would silently disable change logging for
        everything the caller does afterwards."""
        from activity.request_context import is_change_logging_suppressed

        recording, _ = _make_recording(user, tmp_path, n_channels=6, stored_signal_count=3)
        with with_system_activity("recordings.metadata.refresh", interface=Activity.Interface.COMMAND):
            refresh_signal_metadata(recording)
            assert is_change_logging_suppressed() is False


@pytest.mark.django_db
class TestRefreshLeavesContentDerivedRowsAlone:
    def test_interruptions_and_annotations_survive(self, user, tmp_path):
        """Both are derived from signal *content*, which a header refresh cannot see. Recreating
        them would duplicate rows the user may have edited since."""
        from annotations.models import Annotation, Interruption

        recording, _ = _make_recording(user, tmp_path, n_channels=6, stored_signal_count=3)
        recording_ct = ContentType.objects.get_for_model(recording, for_concrete_model=False)
        Interruption.objects.create(
            author=user,
            target_content_type=recording_ct,
            target_object_id=str(recording.pk),
            object_hash="A" * 32,
            timestamp=1.0,
            duration=0.5,
        )
        Annotation.objects.create(
            author=user,
            name="Original annotations",
            target_content_type=recording_ct,
            target_object_id=str(recording.pk),
            object_hash="B" * 32,
            content={"events": []},
        )
        refresh_signal_metadata(recording)
        assert Interruption.objects.filter(target_object_id=str(recording.pk)).count() == 1
        assert Annotation.objects.filter(target_object_id=str(recording.pk)).count() == 1
