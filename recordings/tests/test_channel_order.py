"""Contract tests for the canonical channel order written at ingest.

The permutation moves sample bytes verbatim — the assertions here are bit-exact per channel,
matched through ``source_index`` / ``source_label``, across EDF and BDF sample widths and mixed
sampling rates. The annotation channel must land last with its TAL bytes untouched, and a file
already in canonical order must pass through unchanged. Design in
docs/engineering-notes/channel-deidentification-plan.md (Phase 3).
"""

import json

import pytest
from django.contrib.contenttypes.models import ContentType

from epicurrents.models import AccessRight
from recordings.models import RecordingMeta, SignalInfo
from recordings.processors.channel_labels import CHANNEL_ORDER_VERSION
from recordings.processors.edf import (
    compute_channel_order,
    parse_edf_header,
    parse_signal_infos,
    process_edf_file,
)
from recordings.tests.test_channel_deidentification import DETAIL_URL, _parse_fixture
from recordings.tests.test_edf_processor import _make_edf_header

# A deliberately jumbled channel set: vendor junk first, the annotation channel
# mid-file, EEG scattered and out of pair order, an EKG lead between them.
# Distinct sample counts exercise the mixed-rate offset math.
JUMBLED_SIGNALS = [
    {"label": "XYZ99", "sample_count": 4},
    {"label": "EEG T4", "sample_count": 8},
    {"label": "EDF Annotations", "sample_count": 32},
    {"label": "EEG Fp2", "sample_count": 8},
    {"label": "ECG", "sample_count": 2},
    {"label": "EEG Fp1", "sample_count": 8},
]

# Cleaned labels in the canonical order the spec must produce: EEG homologous
# pairs first (Fp1, Fp2, then T8), EKG next, unresolved MISC after, annotations last.
EXPECTED_ORDER = ["Fp1", "Fp2", "T8", "ECG", "MISC_1", "EDF Annotations"]


def _patterned_record(signals, bytes_per_sample=2, marker_base=0x10):
    """One data record with a distinct constant byte pattern per channel."""
    record = b""
    for i, s in enumerate(signals):
        if s["label"] in ("EDF Annotations", "BDF Annotations"):
            content = b"+0\x14\x14\x00"
            record += content.ljust(s["sample_count"] * bytes_per_sample, b"\x00")
        else:
            record += bytes([marker_base + i]) * (s["sample_count"] * bytes_per_sample)
    return record


def _write_jumbled_file(tmp_path, *, bdf=False, n_records=3):
    bytes_per_sample = 3 if bdf else 2
    version = (bytes([0xFF]) + b"BIOSEMI") if bdf else b"0       "
    reserved = "BDF+C" if bdf else "EDF+C"
    signals = [dict(s) for s in JUMBLED_SIGNALS]
    if bdf:
        # BDF names its annotation channel differently; the fixture must match
        # or the channel reads as an ordinary (unresolvable) signal.
        signals[2]["label"] = "BDF Annotations"
    header = _make_edf_header(version=version, reserved=reserved, n_records=n_records, signals=signals)
    record = _patterned_record(signals, bytes_per_sample)
    path = tmp_path / ("rec.bdf" if bdf else "rec.edf")
    path.write_bytes(header + record * n_records)
    return path


def _channel_bytes(file_bytes, header, infos, bytes_per_sample):
    """Map each channel's cleaned label to its concatenated sample bytes across all records."""
    record_size = sum(si.sample_count * bytes_per_sample for si in infos)
    out = {}
    offset = 0
    for si in infos:
        length = si.sample_count * bytes_per_sample
        chunks = []
        for rec in range(header.data_record_count):
            start = header.header_record_bytes + rec * record_size + offset
            chunks.append(file_bytes[start : start + length])
        out[si.label] = b"".join(chunks)
        offset += length
    return out


class TestComputeChannelOrder:
    def test_full_grouping_and_pair_order(self):
        infos = _parse_fixture(JUMBLED_SIGNALS)
        # compute_channel_order operates on cleaned labels; emulate the cleaning
        # the ingest pass applies before it runs.
        from recordings.processors.edf import deidentify_signal_infos

        deidentify_signal_infos(infos)
        order = compute_channel_order(infos)
        assert [infos[i].label for i in order] == EXPECTED_ORDER

    def test_bare_electrode_sorts_before_its_derivations(self):
        signals = [
            {"label": "EEG Fp1-F7", "sample_count": 8},
            {"label": "Fp1", "sample_count": 8},
        ]
        infos = _parse_fixture(signals)
        from recordings.processors.edf import deidentify_signal_infos

        deidentify_signal_infos(infos)
        order = compute_channel_order(infos)
        assert [infos[i].label for i in order] == ["Fp1", "Fp1-F7"]

    def test_stable_within_unranked_group(self):
        signals = [
            {"label": "AAA1", "sample_count": 8},
            {"label": "BBB2", "sample_count": 8},
        ]
        infos = _parse_fixture(signals)
        from recordings.processors.edf import deidentify_signal_infos

        deidentify_signal_infos(infos)
        order = compute_channel_order(infos)
        assert [infos[i].label for i in order] == ["MISC_1", "MISC_2"]


class TestReorderOnDisk:
    def _process(self, tmp_path, *, bdf=False):
        path = _write_jumbled_file(tmp_path, bdf=bdf)
        original = path.read_bytes()
        result = process_edf_file(path)
        reordered = path.read_bytes()
        return original, result, reordered, path

    def test_disk_order_is_canonical(self, tmp_path):
        _, result, reordered, _ = self._process(tmp_path)
        infos = parse_signal_infos(reordered, parse_edf_header(reordered))
        assert [si.label for si in infos] == EXPECTED_ORDER
        assert [si.label for si in result.signal_infos] == EXPECTED_ORDER

    def test_samples_move_bit_exactly(self, tmp_path):
        original, _, reordered, _ = self._process(tmp_path)
        orig_hdr = parse_edf_header(original)
        orig_infos = parse_signal_infos(original, orig_hdr)
        new_hdr = parse_edf_header(reordered)
        new_infos = parse_signal_infos(reordered, new_hdr)

        # Match channels by source label: original raw labels map onto the
        # cleaned labels the reordered file carries.
        raw_to_clean = dict(
            zip([s["label"] for s in JUMBLED_SIGNALS], ["MISC_1", "T8", "EDF Annotations", "Fp2", "ECG", "Fp1"])
        )
        orig_bytes = _channel_bytes(original, orig_hdr, orig_infos, 2)
        new_bytes = _channel_bytes(reordered, new_hdr, new_infos, 2)
        for raw_label, clean_label in raw_to_clean.items():
            assert new_bytes[clean_label] == orig_bytes[raw_label], raw_label

    def test_bdf_three_byte_samples_move_bit_exactly(self, tmp_path):
        original, _, reordered, _ = self._process(tmp_path, bdf=True)
        orig_hdr = parse_edf_header(original)
        orig_infos = parse_signal_infos(original, orig_hdr)
        new_hdr = parse_edf_header(reordered)
        new_infos = parse_signal_infos(reordered, new_hdr)
        orig_bytes = _channel_bytes(original, orig_hdr, orig_infos, 3)
        new_bytes = _channel_bytes(reordered, new_hdr, new_infos, 3)
        assert new_bytes["Fp1"] == orig_bytes["EEG Fp1"]
        assert new_bytes["MISC_1"] == orig_bytes["XYZ99"]
        assert new_bytes["BDF Annotations"] == orig_bytes["BDF Annotations"]

    def test_annotation_channel_lands_last_with_tals_intact(self, tmp_path):
        _, _, reordered, _ = self._process(tmp_path)
        hdr = parse_edf_header(reordered)
        infos = parse_signal_infos(reordered, hdr)
        assert infos[-1].is_annotation_channel
        record_size = sum(si.sample_count * 2 for si in infos)
        anno_offset = sum(si.sample_count * 2 for si in infos[:-1])
        first_record_anno = reordered[
            hdr.header_record_bytes + anno_offset : hdr.header_record_bytes + anno_offset + infos[-1].sample_count * 2
        ]
        assert first_record_anno.startswith(b"+0\x14\x14\x00")
        assert record_size == sum(s["sample_count"] * 2 for s in JUMBLED_SIGNALS)

    def test_source_index_records_the_permutation(self, tmp_path):
        _, result, _, _ = self._process(tmp_path)
        # EXPECTED_ORDER positions map back to the JUMBLED_SIGNALS positions.
        assert [si.source_index for si in result.signal_infos] == [5, 3, 1, 4, 0, 2]

    def test_unknown_record_count_is_resolved_from_file_size(self, tmp_path):
        """EDF's -1 record count must not skip the permutation while the header claims order."""
        signals = JUMBLED_SIGNALS
        header = _make_edf_header(reserved="EDF+C", n_records=-1, signals=signals)
        record = _patterned_record(signals)
        path = tmp_path / "stream.edf"
        path.write_bytes(header + record * 3)

        result = process_edf_file(path)
        reordered = path.read_bytes()
        hdr = parse_edf_header(reordered)
        infos = parse_signal_infos(reordered, hdr)
        assert hdr.data_record_count == 3
        assert [si.label for si in infos] == EXPECTED_ORDER
        # The permutation actually ran: the Fp1 pattern bytes moved to the front.
        first_channel = reordered[hdr.header_record_bytes : hdr.header_record_bytes + infos[0].sample_count * 2]
        assert first_channel == bytes([0x10 + 5]) * (8 * 2)
        assert result.header.data_record_count == 3

    def test_unknown_record_count_with_ragged_tail_fails(self, tmp_path):
        from recordings.processors.edf import EdfParseError

        signals = JUMBLED_SIGNALS
        header = _make_edf_header(reserved="EDF+C", n_records=-1, signals=signals)
        record = _patterned_record(signals)
        path = tmp_path / "ragged.edf"
        path.write_bytes(header + record * 2 + record[: len(record) // 2])

        with pytest.raises(EdfParseError):
            process_edf_file(path)

    def test_reprocess_is_a_no_op_on_ordered_file(self, tmp_path):
        _, _, reordered, path = self._process(tmp_path)
        result = process_edf_file(path)
        assert path.read_bytes() == reordered
        # An already-canonical file still gets source_index captured (identity).
        assert [si.source_index for si in result.signal_infos] == list(range(len(JUMBLED_SIGNALS)))


@pytest.mark.django_db
class TestOrderPersistence:
    def _ingest_jumbled(self, user, tmp_path):
        from recordings.models import Recording
        from recordings.tasks import _save_edf_results

        path = _write_jumbled_file(tmp_path)
        result = process_edf_file(path)
        recording = Recording.objects.create(
            author=user,
            original_name="rec.edf",
            stored_name="D00D0000D00D0000D00D0000D00D0000.edf",
            file_path=str(path),
            file_extension=".edf",
            file_size=path.stat().st_size,
            status=Recording.Status.READY,
        )
        _save_edf_results(recording, result)
        return recording

    def test_rows_describe_disk_order_and_source_positions(self, user, tmp_path):
        self._ingest_jumbled(user, tmp_path)
        rows = list(SignalInfo.objects.order_by("index"))
        assert [r.label for r in rows] == EXPECTED_ORDER
        assert [r.source_index for r in rows] == [5, 3, 1, 4, 0, 2]
        meta = RecordingMeta.objects.get()
        assert meta.channel_order_version == CHANNEL_ORDER_VERSION

    def test_refresh_preserves_source_index_and_version(self, user, tmp_path):
        from recordings.metadata import refresh_signal_metadata

        recording = self._ingest_jumbled(user, tmp_path)
        RecordingMeta.objects.update(signal_count=99)
        result = refresh_signal_metadata(recording)
        assert result.changed
        rows = list(SignalInfo.objects.order_by("index"))
        assert [r.source_index for r in rows] == [5, 3, 1, 4, 0, 2]
        assert RecordingMeta.objects.get().channel_order_version == CHANNEL_ORDER_VERSION

    def test_source_index_gated_and_version_public(self, client, user, make_user, tmp_path):
        recording = self._ingest_jumbled(user, tmp_path)
        reader = make_user()
        ct = ContentType.objects.get_for_model(recording, for_concrete_model=False)
        AccessRight.objects.create(
            content_type=ct,
            object_id=str(recording.pk),
            access_giver=user,
            access_target=reader,
            can_read=True,
        )
        hash_part = recording.stored_name.split(".")[0]

        client.force_login(user)
        author_meta = json.loads(client.get(DETAIL_URL.format(hash=hash_part)).content)["meta"]
        assert [s["source_index"] for s in author_meta["signals"]] == [5, 3, 1, 4, 0, 2]

        client.force_login(reader)
        grantee_meta = json.loads(client.get(DETAIL_URL.format(hash=hash_part)).content)["meta"]
        assert all(s["source_index"] is None for s in grantee_meta["signals"])
        assert grantee_meta["channel_order_version"] == CHANNEL_ORDER_VERSION
