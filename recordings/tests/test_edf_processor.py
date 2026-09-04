"""Tests for the EDF/BDF format processor (recordings/processors/edf.py).

Binary fixtures are assembled inline so the test suite has no dependency on
real patient files.  All EDF spec field widths are respected so the fixtures
pass through the parser identically to a real-world file.
"""

import tempfile
from pathlib import Path

import pytest

from recordings.processors.edf import (
    GAP_TOLERANCE_S,
    AnnotationEntry,
    EdfChannel,
    EdfParseError,
    _ascii_clean,
    _compute_record_onsets,
    _encode_tal_entry,
    _encode_tal_record,
    _encode_timekeeping_tal,
    extract_signal_type,
    normalise_edf_records,
    parse_annotations,
    parse_edf_header,
    parse_prefiltering,
    parse_signal_infos,
    process_edf_file,
    read_record_gaps,
    read_splice_positions,
    rewrite_edf_header,
    wall_clock_to_data_position,
    write_edf,
)

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _field(text: str, width: int) -> bytes:
    """Encode *text*, space-padded or truncated to *width* bytes.

    Uses latin-1 so non-ASCII characters (e.g. µ, accented letters) can be
    written into the fixture bytes, simulating real-world EDF files that
    contain such characters despite the spec requiring ASCII.
    """
    return text[:width].ljust(width).encode("latin-1", errors="replace")


def _make_edf_header(
    *,
    version: bytes = b"0       ",
    patient: str = "Jane Doe",
    recording: str = "Startdate 01-JAN-2024 A B C",
    startdate: str = "01.01.24",
    starttime: str = "12.00.00",
    reserved: str = "",
    n_records: int = 1,
    rec_duration: float = 1.0,
    signals: list[dict] | None = None,
) -> bytes:
    """Build a minimal but valid EDF (or BDF) header bytestring.

    *signals* is a list of dicts with optional keys matching EdfSignalInfo
    fields.  Defaults to one 256 Hz EEG channel.
    """
    if signals is None:
        signals = [
            {
                "label": "EEG Fp1-Cz",
                "transducer": "AgAgCl",
                "unit": "uV",
                "phys_min": -100.0,
                "phys_max": 100.0,
                "dig_min": -32768,
                "dig_max": 32767,
                "prefiltering": "HP:0.1Hz LP:75Hz",
                "sample_count": 256,
            }
        ]

    ns = len(signals)
    hdr_bytes = 256 + ns * (16 + 80 + 8 + 8 + 8 + 8 + 8 + 80 + 8 + 32)

    general = (
        version
        + _field(patient, 80)
        + _field(recording, 80)
        + _field(startdate, 8)
        + _field(starttime, 8)
        + _field(str(hdr_bytes), 8)
        + _field(reserved, 44)
        + _field(str(n_records), 8)
        + _field(str(rec_duration), 8)
        + _field(str(ns), 4)
    )
    assert len(general) == 256

    def _sec(key: str, width: int, default: str = "") -> bytes:
        return b"".join(_field(str(s.get(key, default)), width) for s in signals)

    signal_hdr = (
        _sec("label", 16)
        + _sec("transducer", 80)
        + _sec("unit", 8)
        + _sec("phys_min", 8, "0")
        + _sec("phys_max", 8, "0")
        + _sec("dig_min", 8, "-32768")
        + _sec("dig_max", 8, "32767")
        + _sec("prefiltering", 80)
        + _sec("sample_count", 8, "256")
        + _sec("reserved", 32)
    )

    return general + signal_hdr


def _make_edf_data(signal_infos, n_records: int = 1) -> bytes:
    """Generate zeroed 16-bit data records for a list of signal info dicts."""
    record = b""
    for s in signal_infos:
        sc = s.get("sample_count", 256)
        record += b"\x00\x00" * sc
    return record * n_records


def _make_tal(onset: float, label: str = "", duration: float | None = None) -> bytes:
    """Build a single TAL bytestring."""
    onset_str = f"+{onset}" if onset >= 0 else str(onset)
    buf = onset_str.encode("ascii")
    if duration is not None:
        buf += b"\x15" + str(duration).encode("ascii")
    buf += b"\x14"  # first field separator
    buf += label.encode("utf-8")
    buf += b"\x14"  # label terminator
    buf += b"\x00"  # TAL end
    return buf


def _make_timekeeping_tal(onset: float) -> bytes:
    """Build the mandatory timekeeping TAL for an EDF+ annotation record."""
    onset_str = f"+{onset}" if onset >= 0 else str(onset)
    return onset_str.encode("ascii") + b"\x14\x14\x00"


def _make_anno_record(onset: float, tals: list[bytes], total_bytes: int) -> bytes:
    """Build one annotation channel data record with timekeeping TAL + extra TALs."""
    content = _make_timekeeping_tal(onset)
    for tal in tals:
        content += tal
    padded = content.ljust(total_bytes, b"\x00")
    return padded[:total_bytes]


# ---------------------------------------------------------------------------
# ASCII cleaning
# ---------------------------------------------------------------------------


class TestAsciiClean:
    def test_plain_ascii_unchanged(self):
        assert _ascii_clean("EEG Fp1-Cz") == "EEG Fp1-Cz"

    def test_micro_sign_replaced(self):
        assert _ascii_clean("µV") == "uV"

    def test_greek_mu_replaced(self):
        assert _ascii_clean("μV") == "uV"

    def test_ohm_replaced(self):
        assert _ascii_clean("Ω") == "Ohm"

    def test_degree_replaced(self):
        assert _ascii_clean("°C") == "degC"

    def test_accented_base_kept(self):
        # ä → a (NFKD: a + combining umlaut, umlaut dropped)
        assert _ascii_clean("Schäfer") == "Schafer"

    def test_truncation(self):
        assert _ascii_clean("Hello World", max_bytes=5) == "Hello"

    def test_symbol_then_truncate(self):
        # µV is 2 chars after replacement; max_bytes=1 should truncate to 'u'
        assert _ascii_clean("µV", max_bytes=1) == "u"


# ---------------------------------------------------------------------------
# Signal type extraction
# ---------------------------------------------------------------------------


class TestExtractSignalType:
    def test_eeg(self):
        assert extract_signal_type("EEG Fp1-Cz") == "eeg"

    def test_emg(self):
        assert extract_signal_type("EMG chin") == "emg"

    def test_eog(self):
        assert extract_signal_type("EOG left") == "eog"

    def test_ecg(self):
        assert extract_signal_type("ECG lead II") == "ekg"

    def test_ekg_alias(self):
        assert extract_signal_type("EKG") == "ekg"

    def test_emg_wins_over_eeg(self):
        # Label "EMG EEG" — EMG matcher is checked first.
        assert extract_signal_type("EMG EEG") == "emg"

    def test_unknown_returns_empty(self):
        assert extract_signal_type("Body temp") == ""

    def test_case_insensitive(self):
        assert extract_signal_type("eeg fp1") == "eeg"


# ---------------------------------------------------------------------------
# Prefiltering parser
# ---------------------------------------------------------------------------


class TestParsePrefiltering:
    def test_all_three(self):
        hp, lp, n = parse_prefiltering("HP:0.1Hz LP:75Hz N:50Hz")
        assert hp == pytest.approx(0.1)
        assert lp == pytest.approx(75.0)
        assert n == pytest.approx(50.0)

    def test_partial(self):
        hp, lp, n = parse_prefiltering("HP:1Hz LP:30Hz")
        assert hp == pytest.approx(1.0)
        assert lp == pytest.approx(30.0)
        assert n == 0.0

    def test_empty(self):
        assert parse_prefiltering("") == (0.0, 0.0, 0.0)

    def test_case_insensitive(self):
        hp, lp, _ = parse_prefiltering("hp:0.5hz lp:100hz")
        assert hp == pytest.approx(0.5)
        assert lp == pytest.approx(100.0)


# ---------------------------------------------------------------------------
# Header parsing
# ---------------------------------------------------------------------------


class TestParseEdfHeader:
    def test_basic_edf(self):
        data = _make_edf_header()
        hdr = parse_edf_header(data)
        assert hdr.data_format == "edf"
        assert not hdr.is_plus
        assert not hdr.discontinuous
        assert hdr.signal_count == 1
        assert hdr.data_record_count == 1
        assert hdr.data_record_duration == pytest.approx(1.0)

    def test_edf_plus_continuous(self):
        data = _make_edf_header(reserved="EDF+C")
        hdr = parse_edf_header(data)
        assert hdr.data_format == "edf+"
        assert hdr.is_plus
        assert not hdr.discontinuous

    def test_edf_plus_discontinuous(self):
        data = _make_edf_header(reserved="EDF+D")
        hdr = parse_edf_header(data)
        assert hdr.discontinuous

    def test_bdf_format_detected(self):
        bdf_version = bytes([0xFF]) + b"BIOSEMI"
        data = _make_edf_header(version=bdf_version)
        hdr = parse_edf_header(data)
        assert hdr.data_format == "bdf"

    def test_bdf_plus_discontinuous(self):
        bdf_version = bytes([0xFF]) + b"BIOSEMI"
        data = _make_edf_header(version=bdf_version, reserved="BDF+D")
        hdr = parse_edf_header(data)
        assert hdr.data_format == "bdf+"
        assert hdr.discontinuous

    def test_date_parsed(self):
        data = _make_edf_header(startdate="15.06.24", starttime="08.30.00")
        hdr = parse_edf_header(data)
        assert hdr.recording_date is not None
        assert hdr.recording_date.year == 2024
        assert hdr.recording_date.month == 6
        assert hdr.recording_date.day == 15

    def test_date_1985_breakpoint(self):
        # Year 85 → 1985 (≥ 85 breakpoint).
        data = _make_edf_header(startdate="01.01.85")
        hdr = parse_edf_header(data)
        assert hdr.recording_date.year == 1985

    def test_bad_format_raises(self):
        bad = b"INVALID " + b" " * (256 - 8)
        with pytest.raises(EdfParseError):
            parse_edf_header(bad)

    def test_patient_id_stored(self):
        data = _make_edf_header(patient="John Doe")
        hdr = parse_edf_header(data)
        assert "John Doe" in hdr.patient_id

    def test_multirecord(self):
        data = _make_edf_header(n_records=10, rec_duration=2.0)
        hdr = parse_edf_header(data)
        assert hdr.data_record_count == 10
        assert hdr.data_record_duration == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# Signal info parsing
# ---------------------------------------------------------------------------


class TestParseSignalInfos:
    def test_single_eeg_channel(self):
        data = _make_edf_header()
        hdr = parse_edf_header(data)
        sigs = parse_signal_infos(data, hdr)
        assert len(sigs) == 1
        s = sigs[0]
        assert s.label == "EEG Fp1-Cz"
        assert s.physical_unit == "uV"
        assert s.signal_type == "eeg"
        assert s.sampling_rate == pytest.approx(256.0)
        assert not s.is_annotation_channel

    def test_prefiltering_parsed(self):
        data = _make_edf_header()
        hdr = parse_edf_header(data)
        sigs = parse_signal_infos(data, hdr)
        assert sigs[0].highpass == pytest.approx(0.1)
        assert sigs[0].lowpass == pytest.approx(75.0)
        assert sigs[0].notch == 0.0

    def test_annotation_channel_detected(self):
        signals = [
            {
                "label": "EEG Fp1",
                "sample_count": 256,
                "phys_min": -100,
                "phys_max": 100,
                "dig_min": -32768,
                "dig_max": 32767,
            },
            {
                "label": "EDF Annotations",
                "sample_count": 60,
                "phys_min": -1,
                "phys_max": 1,
                "dig_min": -32768,
                "dig_max": 32767,
            },
        ]
        data = _make_edf_header(reserved="EDF+C", signals=signals)
        hdr = parse_edf_header(data)
        sigs = parse_signal_infos(data, hdr)
        assert not sigs[0].is_annotation_channel
        assert sigs[1].is_annotation_channel
        assert sigs[1].sampling_rate == 0.0

    def test_units_per_bit(self):
        sigs_def = [
            {
                "label": "EEG",
                "phys_min": -100.0,
                "phys_max": 100.0,
                "dig_min": -32768,
                "dig_max": 32767,
                "sample_count": 256,
            }
        ]
        data = _make_edf_header(signals=sigs_def)
        hdr = parse_edf_header(data)
        sigs = parse_signal_infos(data, hdr)
        expected_upb = 200.0 / 65535
        assert sigs[0].units_per_bit == pytest.approx(expected_upb, rel=1e-4)

    def test_record_byte_size_set(self):
        data = _make_edf_header()  # 1 channel, 256 samples, 2 bytes each
        hdr = parse_edf_header(data)
        parse_signal_infos(data, hdr)
        assert hdr.record_byte_size == 256 * 2


# ---------------------------------------------------------------------------
# Annotation / gap parsing
# ---------------------------------------------------------------------------


def _make_edfplus_file(
    *,
    n_records: int = 2,
    rec_duration: float = 1.0,
    discontinuous: bool = False,
    tals_per_record: list[list[bytes]] | None = None,
    anno_sample_count: int = 80,
    onsets: list[float] | None = None,
    declared_n_records: int | None = None,
    with_annotation_channel: bool = True,
) -> bytes:
    """Build a complete EDF+C or EDF+D file with one EEG + one annotation channel.

    *onsets* overrides the timekeeping TAL of each record, which is what makes a gap:
    by default record *r* opens at ``r * rec_duration`` (contiguous). *declared_n_records*
    overrides only the header's record count, so a file can claim ``-1`` (still being
    written) while carrying real records. *with_annotation_channel* drops the annotation
    signal entirely, giving a plain EDF with no timeline to read.
    """
    reserved = "EDF+D" if discontinuous else "EDF+C"
    signals = [
        {
            "label": "EEG Fp1",
            "sample_count": 256,
            "phys_min": -100,
            "phys_max": 100,
            "dig_min": -32768,
            "dig_max": 32767,
        },
        {
            "label": "EDF Annotations",
            "sample_count": anno_sample_count,
            "phys_min": -1,
            "phys_max": 1,
            "dig_min": -32768,
            "dig_max": 32767,
            "prefiltering": "",
        },
    ]
    if not with_annotation_channel:
        signals = signals[:1]
    header_bytes = _make_edf_header(
        reserved=reserved,
        n_records=n_records if declared_n_records is None else declared_n_records,
        rec_duration=rec_duration,
        signals=signals,
    )

    if tals_per_record is None:
        tals_per_record = [[] for _ in range(n_records)]

    anno_record_bytes = anno_sample_count * 2  # 16-bit samples

    data_records = b""
    for r in range(n_records):
        onset = r * rec_duration if onsets is None else onsets[r]
        eeg_data = b"\x00\x00" * 256
        if not with_annotation_channel:
            data_records += eeg_data
            continue
        tals = tals_per_record[r] if r < len(tals_per_record) else []
        anno_data = _make_anno_record(onset, tals, anno_record_bytes)
        data_records += eeg_data + anno_data  # signals in definition order

    return header_bytes + data_records


class TestParseAnnotations:
    def test_no_annotation_channels_returns_empty(self):
        # Plain EDF (no EDF+ reserved field) → no annotation channels.
        data = _make_edf_header()
        hdr = parse_edf_header(data)
        sigs = parse_signal_infos(data, hdr)
        # Append zeroed data record.
        data += _make_edf_data([{"sample_count": 256}])
        annos, gaps = parse_annotations(data, hdr, sigs)
        assert annos == []
        assert gaps == {}

    def test_text_annotation_extracted(self):
        tal = _make_tal(0.5, label="Stimulus", duration=0.1)
        file_bytes = _make_edfplus_file(n_records=1, tals_per_record=[[tal]])
        data = file_bytes
        hdr = parse_edf_header(data)
        sigs = parse_signal_infos(data, hdr)
        annos, gaps = parse_annotations(data, hdr, sigs)
        assert len(annos) == 1
        assert annos[0].label == "Stimulus"
        assert annos[0].onset == pytest.approx(0.5)
        assert annos[0].duration == pytest.approx(0.1)

    def test_multiple_annotations(self):
        tals = [
            _make_tal(0.2, "Blink"),
            _make_tal(0.8, "Artefact"),
        ]
        file_bytes = _make_edfplus_file(n_records=1, tals_per_record=[tals])
        hdr = parse_edf_header(file_bytes)
        sigs = parse_signal_infos(file_bytes, hdr)
        annos, _ = parse_annotations(file_bytes, hdr, sigs)
        labels = [a.label for a in annos]
        assert "Blink" in labels
        assert "Artefact" in labels

    def test_no_gaps_in_continuous_file(self):
        file_bytes = _make_edfplus_file(n_records=3, discontinuous=False)
        hdr = parse_edf_header(file_bytes)
        sigs = parse_signal_infos(file_bytes, hdr)
        _, gaps = parse_annotations(file_bytes, hdr, sigs)
        assert gaps == {}

    def test_gap_detected_in_discontinuous_file(self):
        """A data record whose timekeeping TAL onset exceeds the expected start
        should produce a gap entry.  We inject this by building the annotation
        record manually with a 2-second jump where only 1 second was expected.
        """
        anno_bytes = 80 * 2  # 80 samples × 2 bytes

        signals = [
            {
                "label": "EEG Fp1",
                "sample_count": 256,
                "phys_min": -100,
                "phys_max": 100,
                "dig_min": -32768,
                "dig_max": 32767,
            },
            {
                "label": "EDF Annotations",
                "sample_count": 80,
                "phys_min": -1,
                "phys_max": 1,
                "dig_min": -32768,
                "dig_max": 32767,
                "prefiltering": "",
            },
        ]
        header_bytes = _make_edf_header(reserved="EDF+D", n_records=2, rec_duration=1.0, signals=signals)

        # Record 0: starts at t=0 (no gap).
        rec0_anno = _make_anno_record(0.0, [], anno_bytes)
        rec0_eeg = b"\x00\x00" * 256
        # Record 1: expected t=1.0 but TAL says t=3.0 → 2-second gap.
        rec1_anno = _make_anno_record(3.0, [], anno_bytes)
        rec1_eeg = b"\x00\x00" * 256

        file_bytes = header_bytes + rec0_eeg + rec0_anno + rec1_eeg + rec1_anno

        hdr = parse_edf_header(file_bytes)
        sigs = parse_signal_infos(file_bytes, hdr)
        _, gaps = parse_annotations(file_bytes, hdr, sigs)

        assert len(gaps) == 1
        # Gap at data_pos = 1 * 1.0 = 1.0 second (position after record 0)
        assert 1.0 in gaps
        assert gaps[1.0] == pytest.approx(2.0)

    def test_gap_not_double_counted_with_single_anno_channel(self):
        """Regression: gap must be accumulated exactly once per record."""
        file_bytes = _make_edfplus_file(n_records=2, discontinuous=True)
        # Build manually with a gap in record 1.
        anno_bytes = 80 * 2
        signals = [
            {
                "label": "EEG Fp1",
                "sample_count": 256,
                "phys_min": -100,
                "phys_max": 100,
                "dig_min": -32768,
                "dig_max": 32767,
            },
            {
                "label": "EDF Annotations",
                "sample_count": 80,
                "phys_min": -1,
                "phys_max": 1,
                "dig_min": -32768,
                "dig_max": 32767,
                "prefiltering": "",
            },
        ]
        header_bytes = _make_edf_header(reserved="EDF+D", n_records=2, rec_duration=1.0, signals=signals)
        rec0_anno = _make_anno_record(0.0, [], anno_bytes)
        rec1_anno = _make_anno_record(5.0, [], anno_bytes)  # 4-second gap
        rec0_eeg = b"\x00\x00" * 256
        rec1_eeg = b"\x00\x00" * 256
        file_bytes = header_bytes + rec0_eeg + rec0_anno + rec1_eeg + rec1_anno

        hdr = parse_edf_header(file_bytes)
        sigs = parse_signal_infos(file_bytes, hdr)
        _, gaps = parse_annotations(file_bytes, hdr, sigs)

        assert len(gaps) == 1
        assert gaps[1.0] == pytest.approx(4.0)


# ---------------------------------------------------------------------------
# Record timeline (seek-based): read_record_gaps / read_splice_positions
# ---------------------------------------------------------------------------


class TestReadRecordGaps:
    """The seek-based record-onset scan the signal loader and the smoke checks share.

    Where :func:`parse_annotations` needs the whole file resident and accumulates an
    offset across records, this reads only the annotation bytes of the records asked
    for. The tests below pin the two properties that difference buys: that a restricted
    range returns *exactly* the gaps it covers (no state crosses a record boundary), and
    that an unreadable timeline raises rather than reporting "no gaps".
    """

    def _parsed(self, tmp_path, file_bytes: bytes, name: str = "rec.edf"):
        path = tmp_path / name
        path.write_bytes(file_bytes)
        header = parse_edf_header(file_bytes)
        infos = parse_signal_infos(file_bytes, header)
        return path, header, infos

    def test_continuous_file_has_no_gaps(self, tmp_path):
        path, hdr, infos = self._parsed(tmp_path, _make_edfplus_file(n_records=4))
        assert read_record_gaps(path, hdr, infos) == []
        assert read_splice_positions(path, hdr, infos) == []

    def test_single_gap_named_by_the_record_after_it(self, tmp_path):
        # Record 1 opens at 5 s instead of 1 s: four seconds of wall clock, no samples.
        path, hdr, infos = self._parsed(
            tmp_path,
            _make_edfplus_file(n_records=3, discontinuous=True, onsets=[0.0, 5.0, 6.0]),
        )
        gaps = read_record_gaps(path, hdr, infos)
        assert len(gaps) == 1
        record, extra = gaps[0]
        assert record == 1
        assert extra == pytest.approx(4.0)
        # In data position the same gap is a zero-width seam at the record boundary.
        assert read_splice_positions(path, hdr, infos) == [pytest.approx(1.0)]

    def test_multiple_gaps_in_order(self, tmp_path):
        path, hdr, infos = self._parsed(
            tmp_path,
            _make_edfplus_file(n_records=5, discontinuous=True, onsets=[0.0, 1.0, 11.0, 12.0, 30.0]),
        )
        assert [r for r, _extra in read_record_gaps(path, hdr, infos)] == [2, 4]
        assert read_splice_positions(path, hdr, infos) == [
            pytest.approx(2.0),
            pytest.approx(4.0),
        ]

    def test_record_duration_scales_the_splice_position(self, tmp_path):
        # A 10-second record duration puts the seam at 10 s, not 1 s.
        path, hdr, infos = self._parsed(
            tmp_path,
            _make_edfplus_file(
                n_records=3,
                rec_duration=10.0,
                discontinuous=True,
                onsets=[0.0, 100.0, 110.0],
            ),
        )
        assert read_splice_positions(path, hdr, infos) == [pytest.approx(10.0)]

    def test_range_restriction_is_exact_not_approximate(self, tmp_path):
        """Restricting the record range must neither invent nor lose a gap.

        Gaps at records 2 and 4. A gap is a difference between two *consecutive*
        onsets, so a range that includes record *r* also reads ``r - 1`` and returns
        the same verdict for it as a full scan would — the point of the primitive.
        """
        path, hdr, infos = self._parsed(
            tmp_path,
            _make_edfplus_file(
                n_records=6,
                discontinuous=True,
                onsets=[0.0, 1.0, 11.0, 12.0, 30.0, 31.0],
            ),
        )
        full = read_record_gaps(path, hdr, infos)
        assert [r for r, _e in full] == [2, 4]
        # Windowed views, each equal to the corresponding slice of the full scan.
        assert read_record_gaps(path, hdr, infos, first_record=4) == full[1:]
        assert read_record_gaps(path, hdr, infos, last_record=3) == full[:1]
        assert read_record_gaps(path, hdr, infos, first_record=2, last_record=2) == full[:1]
        # A range containing no gap is empty, not a fallback to the whole file.
        assert read_record_gaps(path, hdr, infos, first_record=5, last_record=5) == []

    def test_first_record_zero_and_one_are_equivalent(self, tmp_path):
        """Record 0 cannot carry a gap — there is no earlier onset to differ from."""
        path, hdr, infos = self._parsed(
            tmp_path,
            _make_edfplus_file(n_records=3, discontinuous=True, onsets=[0.0, 5.0, 6.0]),
        )
        assert read_record_gaps(path, hdr, infos, first_record=0) == read_record_gaps(path, hdr, infos, first_record=1)

    def test_last_record_beyond_the_file_is_clamped(self, tmp_path):
        path, hdr, infos = self._parsed(
            tmp_path,
            _make_edfplus_file(n_records=3, discontinuous=True, onsets=[0.0, 5.0, 6.0]),
        )
        # A loader asking about a window at the end of the recording overshoots by design.
        assert read_record_gaps(path, hdr, infos, last_record=9999) == [(1, pytest.approx(4.0))]

    def test_backwards_onset_reported_as_negative_extra(self, tmp_path):
        """A record starting before its predecessor ended is corruption, not a pause —
        but the samples either side are not contiguous either way, so it is reported."""
        path, hdr, infos = self._parsed(
            tmp_path,
            _make_edfplus_file(n_records=3, discontinuous=True, onsets=[0.0, 0.25, 1.25]),
        )
        gaps = read_record_gaps(path, hdr, infos)
        assert len(gaps) == 1
        assert gaps[0][0] == 1
        assert gaps[0][1] == pytest.approx(-0.75)

    def test_rounding_within_tolerance_is_not_a_gap(self, tmp_path):
        """EDF writes onsets as ASCII decimals, so exact equality is unavailable."""
        drift = GAP_TOLERANCE_S / 2
        path, hdr, infos = self._parsed(
            tmp_path,
            _make_edfplus_file(n_records=3, onsets=[0.0, 1.0 + drift, 2.0 + 2 * drift]),
        )
        assert read_record_gaps(path, hdr, infos) == []

    def test_plain_edf_without_annotation_channel_returns_empty(self, tmp_path):
        """No annotation channel means no timeline to compare — the correct answer for a
        plain EDF is "no gaps", not a failure."""
        path, hdr, infos = self._parsed(tmp_path, _make_edfplus_file(n_records=4, with_annotation_channel=False))
        assert read_record_gaps(path, hdr, infos) == []

    def test_single_record_returns_empty(self, tmp_path):
        path, hdr, infos = self._parsed(tmp_path, _make_edfplus_file(n_records=1))
        assert read_record_gaps(path, hdr, infos) == []

    def test_unknown_record_count_recovered_from_file_size(self, tmp_path):
        """A file still being written declares -1 records; the gap is still found."""
        path, hdr, infos = self._parsed(
            tmp_path,
            _make_edfplus_file(
                n_records=3,
                discontinuous=True,
                onsets=[0.0, 5.0, 6.0],
                declared_n_records=-1,
            ),
        )
        assert hdr.data_record_count == -1
        assert read_splice_positions(path, hdr, infos) == [pytest.approx(1.0)]

    def test_truncated_record_raises(self, tmp_path):
        """Refusing beats reporting "continuous": a caller placing splices must not be
        able to mistake an unreadable timeline for an unbroken one."""
        file_bytes = _make_edfplus_file(n_records=3, discontinuous=True)
        path, hdr, infos = self._parsed(tmp_path, file_bytes[:-40])
        with pytest.raises(EdfParseError, match="truncated"):
            read_record_gaps(path, hdr, infos)

    def test_unreadable_timekeeping_tal_raises(self, tmp_path):
        anno_bytes = 80 * 2
        broken = b"not-a-number\x14\x14\x00".ljust(anno_bytes, b"\x00")
        file_bytes = _make_edfplus_file(n_records=2, discontinuous=True)
        header = parse_edf_header(file_bytes)
        # Overwrite record 1's annotation channel in place: EEG (256 × 2 bytes) then anno.
        record_size = 256 * 2 + anno_bytes
        start = header.header_record_bytes + record_size + 256 * 2
        corrupt = bytearray(file_bytes)
        corrupt[start : start + anno_bytes] = broken
        path, hdr, infos = self._parsed(tmp_path, bytes(corrupt))
        with pytest.raises(EdfParseError, match="timekeeping"):
            read_record_gaps(path, hdr, infos)

    def test_edf_plus_c_marker_is_not_consulted(self, tmp_path):
        """The records are the authority. A file marked continuous whose onsets jump has
        a gap, and a caller that wants to trust the marker checks it itself and skips
        the call — which is what the signal loader does."""
        path, hdr, infos = self._parsed(
            tmp_path,
            _make_edfplus_file(n_records=3, discontinuous=False, onsets=[0.0, 5.0, 6.0]),
        )
        assert not hdr.discontinuous
        assert read_splice_positions(path, hdr, infos) == [pytest.approx(1.0)]

    def test_agrees_with_parse_annotations_on_the_same_file(self, tmp_path):
        """Two independent readers of the same timeline, one seeking and one whole-file:
        the splice positions must be the keys of the gap map."""
        file_bytes = _make_edfplus_file(n_records=5, discontinuous=True, onsets=[0.0, 1.0, 11.0, 12.0, 30.0])
        path, hdr, infos = self._parsed(tmp_path, file_bytes)
        _annos, gaps = parse_annotations(file_bytes, hdr, infos)
        assert sorted(gaps) == read_splice_positions(path, hdr, infos)
        assert [pytest.approx(v) for v in gaps.values()] == [extra for _r, extra in read_record_gaps(path, hdr, infos)]

    def test_reads_only_the_records_in_range(self, tmp_path):
        """The cost is the range asked for, not the recording's size — that is what
        makes this usable from a loader that wants the splices near one window."""
        path, hdr, infos = self._parsed(tmp_path, _make_edfplus_file(n_records=200, discontinuous=True))
        reads = []
        real_open = Path.open

        def counting_open(self_path, *args, **kwargs):
            handle = real_open(self_path, *args, **kwargs)
            real_read = handle.read

            def read(*a, **k):
                reads.append(1)
                return real_read(*a, **k)

            handle.read = read
            return handle

        original = Path.open
        try:
            Path.open = counting_open
            read_record_gaps(path, hdr, infos, first_record=100, last_record=102)
        finally:
            Path.open = original
        # Records 99..102 inclusive: one read each, and nothing for the other 196.
        assert len(reads) == 4


# ---------------------------------------------------------------------------
# Header rewriting
# ---------------------------------------------------------------------------


class TestRewriteEdfHeader:
    def _roundtrip(self, header_bytes: bytes, data_bytes: bytes = b"") -> bytes:
        """Write file to disk, rewrite header, return full file content."""
        with tempfile.NamedTemporaryFile(suffix=".edf", delete=False) as f:
            path = Path(f.name)
            f.write(header_bytes + data_bytes)
        hdr = parse_edf_header(header_bytes)
        sigs = parse_signal_infos(header_bytes, hdr)
        rewrite_edf_header(path, hdr, sigs)
        result = path.read_bytes()
        path.unlink(missing_ok=True)
        return result

    def test_patient_field_blanked(self):
        raw = _make_edf_header(patient="Jane Doe")
        result = self._roundtrip(raw)
        patient_field = result[8:88].decode("ascii").strip()
        assert patient_field == "X X X X"

    def test_recording_field_blanked(self):
        raw = _make_edf_header(recording="Startdate 01-JAN-2024 admin tech equip")
        result = self._roundtrip(raw)
        recording_field = result[88:168].decode("ascii").strip()
        assert recording_field == "Startdate X X X X"

    def test_start_date_anonymised(self):
        raw = _make_edf_header(startdate="15.06.24")
        result = self._roundtrip(raw)
        assert result[168:176] == b"01.01.85"

    def test_start_time_zeroed(self):
        raw = _make_edf_header(starttime="08.30.00")
        result = self._roundtrip(raw)
        assert result[176:184] == b"00.00.00"

    def test_reserved_set_to_edf_plus_c(self):
        raw = _make_edf_header(reserved="")  # plain EDF
        result = self._roundtrip(raw)
        # Reserved field: offset 192, width 44 (after version+patient+recording+date+time+hdr_bytes)
        reserved_field = result[192:236].decode("ascii").strip()
        assert reserved_field == "EDF+C"

    def test_reserved_preserves_edf_plus_d(self):
        raw = _make_edf_header(reserved="EDF+D")
        result = self._roundtrip(raw)
        reserved_field = result[192:236].decode("ascii").strip()
        assert reserved_field == "EDF+D"

    def test_bdf_version_byte_preserved(self):
        bdf_version = bytes([0xFF]) + b"BIOSEMI"
        raw = _make_edf_header(version=bdf_version)
        result = self._roundtrip(raw)
        assert result[0] == 0xFF
        assert result[1:8] == b"BIOSEMI"

    def test_non_ascii_signal_label_cleaned(self):
        signals = [
            {
                "label": "µV channel",
                "sample_count": 256,
                "phys_min": -100,
                "phys_max": 100,
                "dig_min": -32768,
                "dig_max": 32767,
            }
        ]
        raw = _make_edf_header(signals=signals)
        result = self._roundtrip(raw)
        # Signal labels start at byte 256 in the rewritten header.
        label_field = result[256:272].decode("ascii").strip()
        assert "µ" not in label_field
        assert "u" in label_field  # µ → u

    def test_data_records_untouched(self):
        raw = _make_edf_header()
        sentinel = b"\xde\xad\xbe\xef" * 128
        result = self._roundtrip(raw, data_bytes=sentinel)
        # Data starts after the header.
        hdr = parse_edf_header(raw)
        assert result[hdr.header_record_bytes :] == sentinel


# ---------------------------------------------------------------------------
# Full pipeline (process_edf_file)
# ---------------------------------------------------------------------------


class TestProcessEdfFile:
    def _write_file(self, content: bytes, suffix: str = ".edf") -> Path:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(content)
        return Path(tmp.name)

    def test_plain_edf_processed(self):
        raw = _make_edf_header()
        data = raw + _make_edf_data([{"sample_count": 256}])
        path = self._write_file(data)
        try:
            result = process_edf_file(path)
            assert result.header.data_format in ("edf", "edf+")
            assert result.header.signal_count == 1
            assert result.annotations == []
            assert result.gaps == {}
        finally:
            path.unlink(missing_ok=True)

    def test_edfplus_with_annotation(self):
        tal = _make_tal(0.3, "Spike", duration=0.05)
        file_bytes = _make_edfplus_file(n_records=1, tals_per_record=[[tal]])
        path = self._write_file(file_bytes)
        try:
            result = process_edf_file(path)
            assert len(result.annotations) == 1
            assert result.annotations[0].label == "Spike"
        finally:
            path.unlink(missing_ok=True)

    def test_header_rewritten_after_process(self):
        raw = _make_edf_header(patient="Real Patient Name")
        data = raw + _make_edf_data([{"sample_count": 256}])
        path = self._write_file(data)
        try:
            process_edf_file(path)
            rewritten = path.read_bytes()
            patient_field = rewritten[8:88].decode("ascii").strip()
            assert patient_field == "X X X X"
        finally:
            path.unlink(missing_ok=True)

    def test_corrupt_format_raises(self):
        bad_data = b"GARBAGE!" + b"\x00" * 512
        path = self._write_file(bad_data)
        try:
            with pytest.raises(EdfParseError):
                process_edf_file(path)
        finally:
            path.unlink(missing_ok=True)

    def test_bdf_file_processed(self):
        bdf_version = bytes([0xFF]) + b"BIOSEMI"
        raw = _make_edf_header(version=bdf_version)
        data = raw + b"\x00\x00\x00" * 256  # 24-bit zero samples for 1 channel × 256 samples
        path = self._write_file(data, suffix=".bdf")
        try:
            result = process_edf_file(path)
            # result.header reflects the original file format before rewriting;
            # plain BDF → 'bdf'.  The rewritten file on disk will have 'BDF+C' in reserved.
            assert result.header.data_format == "bdf"
            # Confirm the on-disk header was rewritten to BDF+C.
            rewritten = path.read_bytes()
            reserved_on_disk = rewritten[192:236].decode("ascii").strip()
            assert reserved_on_disk == "BDF+C"
        finally:
            path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Integration: _save_edf_results via process_recording
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestSaveEdfResults:
    """Test that _save_edf_results creates the expected DB rows."""

    def test_recording_meta_created(self, make_user):
        from recordings.models import Recording, RecordingMeta
        from recordings.processors.edf import (
            EdfHeader,
            EdfProcessingResult,
            EdfSignalInfo,
        )

        user = make_user()
        recording = Recording.objects.create(
            author=user,
            original_name="test.edf",
            stored_name="ABCD1234.edf",
            file_extension=".edf",
            file_size=1024,
            file_path="/tmp/ABCD1234.edf",
            file_hash="a" * 64,
        )

        header = EdfHeader(
            data_format="edf+",
            patient_id="X X X X",
            local_recording_id="Startdate X X X X",
            recording_date=None,
            header_record_bytes=512,
            reserved="EDF+C",
            data_record_count=10,
            data_record_duration=1.0,
            signal_count=1,
            is_plus=True,
            discontinuous=False,
        )
        sig = EdfSignalInfo(
            label="EEG Fp1",
            transducer_type="AgAgCl",
            physical_unit="uV",
            physical_min=-100.0,
            physical_max=100.0,
            digital_min=-32768,
            digital_max=32767,
            prefiltering="HP:0.1Hz LP:75Hz",
            sample_count=256,
            reserved="",
            units_per_bit=200.0 / 65535,
            digital_offset=0.0,
            sampling_rate=256.0,
            signal_type="eeg",
            is_annotation_channel=False,
            highpass=0.1,
            lowpass=75.0,
            notch=0.0,
        )
        result = EdfProcessingResult(
            header=header,
            signal_infos=[sig],
            annotations=[],
            gaps={},
        )

        from recordings.tasks import _save_edf_results

        _save_edf_results(recording, result)

        meta = RecordingMeta.objects.get(object_id=str(recording.pk))
        assert meta.format == "edf+"
        assert meta.data_record_count == 10
        assert meta.signal_count == 1
        assert meta.signals.count() == 1
        assert meta.signals.first().label == "EEG Fp1"

    def test_interruptions_created_for_gaps(self, make_user):
        from annotations.models import Interruption
        from recordings.models import Recording
        from recordings.processors.edf import EdfHeader, EdfProcessingResult

        user = make_user()
        recording = Recording.objects.create(
            author=user,
            original_name="disc.edf",
            stored_name="DISC1234.edf",
            file_extension=".edf",
            file_size=2048,
            file_path="/tmp/DISC1234.edf",
            file_hash="b" * 64,
        )
        header = EdfHeader(
            data_format="edf+",
            patient_id="X X X X",
            local_recording_id="Startdate X X X X",
            recording_date=None,
            header_record_bytes=512,
            reserved="EDF+D",
            data_record_count=3,
            data_record_duration=1.0,
            signal_count=1,
            is_plus=True,
            discontinuous=True,
        )
        result = EdfProcessingResult(
            header=header,
            signal_infos=[],
            annotations=[],
            gaps={1.0: 2.5},
        )

        from recordings.tasks import _save_edf_results

        _save_edf_results(recording, result)

        interruptions = Interruption.objects.filter(target_object_id=str(recording.pk))
        assert interruptions.count() == 1
        assert interruptions.first().timestamp == pytest.approx(1.0)
        assert interruptions.first().duration == pytest.approx(2.5)

    def test_original_annotations_created(self, make_user):
        from annotations.models import Annotation
        from recordings.models import Recording
        from recordings.processors.edf import (
            AnnotationEntry,
            EdfHeader,
            EdfProcessingResult,
        )

        user = make_user()
        recording = Recording.objects.create(
            author=user,
            original_name="annot.edf",
            stored_name="ANNO1234.edf",
            file_extension=".edf",
            file_size=1024,
            file_path="/tmp/ANNO1234.edf",
            file_hash="c" * 64,
        )
        header = EdfHeader(
            data_format="edf+",
            patient_id="X X X X",
            local_recording_id="Startdate X X X X",
            recording_date=None,
            header_record_bytes=512,
            reserved="EDF+C",
            data_record_count=1,
            data_record_duration=1.0,
            signal_count=1,
            is_plus=True,
            discontinuous=False,
        )
        result = EdfProcessingResult(
            header=header,
            signal_infos=[],
            annotations=[AnnotationEntry(onset=0.5, duration=0.1, label="Spike")],
            gaps={},
        )

        from recordings.tasks import _save_edf_results

        _save_edf_results(recording, result)

        ann = Annotation.objects.get(target_object_id=str(recording.pk))
        assert ann.name == "Original annotations"
        assert "events" in ann.content
        assert ann.content["events"][0]["label"] == "Spike"
        assert "interruptions" not in ann.content
        # No gaps, so the two timelines coincide and nothing is worth recording twice.
        assert ann.content["events"][0]["onset"] == pytest.approx(0.5)
        assert "wall_clock_onset" not in ann.content["events"][0]

    def test_annotation_onsets_are_translated_to_data_positions(self, make_user):
        """The bug this closes: TAL onsets are wall clock, Interruption rows are data
        positions, and storing the former beside the latter put one file's annotations
        and its gaps on two different timelines with nothing saying which was which.

        Data position is canonical (see ``recordings/continuity-and-timelines.md``), so
        ``events[*]["onset"]`` is translated at the ingest boundary and the file's own
        wall-clock value is retained only where it differs.
        """
        from annotations.models import Annotation, Interruption
        from recordings.models import Recording
        from recordings.processors.edf import (
            AnnotationEntry,
            EdfHeader,
            EdfProcessingResult,
        )

        user = make_user()
        recording = Recording.objects.create(
            author=user,
            original_name="disc-annot.edf",
            stored_name="DANN1234.edf",
            file_extension=".edf",
            file_size=4096,
            file_path="/tmp/DANN1234.edf",
            file_hash="d" * 64,
        )
        header = EdfHeader(
            data_format="edf+",
            patient_id="X X X X",
            local_recording_id="Startdate X X X X",
            recording_date=None,
            header_record_bytes=512,
            reserved="EDF+D",
            data_record_count=20,
            data_record_duration=1.0,
            signal_count=1,
            is_plus=True,
            discontinuous=True,
        )
        result = EdfProcessingResult(
            header=header,
            signal_infos=[],
            annotations=[
                # Before the gap: the timelines still agree.
                AnnotationEntry(onset=2.0, duration=0.0, label="Before"),
                # Inside the dead time (gap spans wall clock 5.0 → 8.0): no sample was
                # recorded, so it collapses onto the splice. Lossy, and deliberate.
                AnnotationEntry(onset=6.5, duration=0.0, label="During"),
                # After the gap: shifted back by the accumulated 3s.
                AnnotationEntry(onset=12.0, duration=0.25, label="After"),
            ],
            gaps={5.0: 3.0},
        )

        from recordings.tasks import _save_edf_results

        _save_edf_results(recording, result)

        events = {e["label"]: e for e in Annotation.objects.get(target_object_id=str(recording.pk)).content["events"]}

        assert events["Before"]["onset"] == pytest.approx(2.0)
        assert "wall_clock_onset" not in events["Before"]

        assert events["During"]["onset"] == pytest.approx(5.0)
        assert events["During"]["wall_clock_onset"] == pytest.approx(6.5)

        assert events["After"]["onset"] == pytest.approx(9.0)
        assert events["After"]["wall_clock_onset"] == pytest.approx(12.0)
        assert events["After"]["duration"] == pytest.approx(0.25)

        # And the whole point: the Interruption row is on the same timeline, so the
        # splice at 5.0 sits between "Before" at 2.0 and "After" at 9.0.
        interruption = Interruption.objects.get(target_object_id=str(recording.pk))
        assert interruption.timestamp == pytest.approx(5.0)
        assert events["Before"]["onset"] < interruption.timestamp < events["After"]["onset"]

    def test_no_annotation_when_nothing_to_store(self, make_user):
        from annotations.models import Annotation
        from recordings.models import Recording
        from recordings.processors.edf import EdfHeader, EdfProcessingResult

        user = make_user()
        recording = Recording.objects.create(
            author=user,
            original_name="empty.edf",
            stored_name="EMPT1234.edf",
            file_extension=".edf",
            file_size=512,
            file_path="/tmp/EMPT1234.edf",
            file_hash="d" * 64,
        )
        header = EdfHeader(
            data_format="edf",
            patient_id="X X X X",
            local_recording_id="Startdate X X X X",
            recording_date=None,
            header_record_bytes=256,
            reserved="",
            data_record_count=1,
            data_record_duration=1.0,
            signal_count=0,
            is_plus=False,
            discontinuous=False,
        )
        result = EdfProcessingResult(header=header, signal_infos=[], annotations=[], gaps={})

        from recordings.tasks import _save_edf_results

        _save_edf_results(recording, result)

        assert not Annotation.objects.filter(target_object_id=str(recording.pk)).exists()


# ---------------------------------------------------------------------------
# TAL encoding
# ---------------------------------------------------------------------------


class TestEncodeTal:
    def test_timekeeping_format(self):
        raw = _encode_timekeeping_tal(0.0)
        assert raw == b"+0\x14\x14\x00"

    def test_timekeeping_non_zero(self):
        raw = _encode_timekeeping_tal(3.5)
        assert raw == b"+3.5\x14\x14\x00"

    def test_text_entry_no_duration(self):
        raw = _encode_tal_entry(1.5, 0.0, "Spike")
        assert raw == b"+1.5\x14Spike\x14\x00"

    def test_text_entry_with_duration(self):
        raw = _encode_tal_entry(2.0, 0.5, "Blink")
        assert raw == b"+2\x15" + b"0.5\x14Blink\x14\x00"

    def test_record_timekeeping_only(self):
        buf = _encode_tal_record(5.0, [], 40)
        assert buf[:7] == b"+5\x14\x14\x00\x00\x00"
        assert len(buf) == 40

    def test_record_with_annotations(self):
        annos = [AnnotationEntry(onset=5.3, duration=0.0, label="Mark")]
        buf = _encode_tal_record(5.0, annos, 60)
        assert b"Mark" in buf
        assert len(buf) == 60
        assert buf[len(buf) - 1] == 0  # null-padded

    def test_record_strip_text(self):
        annos = [AnnotationEntry(onset=5.3, duration=0.0, label="Mark")]
        buf = _encode_tal_record(5.0, annos, 60, strip_text=True)
        assert b"Mark" not in buf
        # timekeeping TAL still present
        assert b"+5\x14\x14\x00" in buf

    def test_record_overflow_raises(self):
        # channel too small to even hold the timekeeping TAL
        with pytest.raises(ValueError):
            _encode_tal_record(1234567890.0, [], 3)


# ---------------------------------------------------------------------------
# Record onset computation
# ---------------------------------------------------------------------------


class TestComputeRecordOnsets:
    def test_continuous_no_gaps(self):
        onsets = _compute_record_onsets(5, 2.0, {})
        assert onsets == pytest.approx([0.0, 2.0, 4.0, 6.0, 8.0])

    def test_gap_shifts_subsequent_onsets(self):
        # Gap of 3 s before record 2 (data_pos = 2 * 1.0 = 2.0)
        onsets = _compute_record_onsets(4, 1.0, {2.0: 3.0})
        assert onsets == pytest.approx([0.0, 1.0, 5.0, 6.0])

    def test_multiple_gaps(self):
        # Gaps before records 1 and 3 (data_pos 1.0 and 3.0)
        onsets = _compute_record_onsets(4, 1.0, {1.0: 2.0, 3.0: 4.0})
        assert onsets == pytest.approx([0.0, 3.0, 4.0, 9.0])


# ---------------------------------------------------------------------------
# wall_clock_to_data_position — the inverse of _compute_record_onsets
# ---------------------------------------------------------------------------


class TestWallClockToDataPosition:
    """A discontinuous recording has two timelines; these pin the translation.

    ``_compute_record_onsets`` maps data position → wall clock (it is what writes the
    TAL onsets out). This is the inverse, and the two must agree exactly, or a stored
    annotation and the sample it describes end up on different timelines.
    """

    def test_no_gaps_is_the_identity(self):
        for onset in (0.0, 1.0, 12.5, 3600.0):
            assert wall_clock_to_data_position(onset, {}) == onset

    def test_before_the_first_gap_is_unchanged(self):
        gaps = {30.0: 5.0}
        assert wall_clock_to_data_position(0.0, gaps) == 0.0
        assert wall_clock_to_data_position(29.9, gaps) == pytest.approx(29.9)
        # The splice instant itself is still data position 30: the gap opens *at* 30,
        # so the last sample before it and the first sample after it are both there.
        assert wall_clock_to_data_position(30.0, gaps) == pytest.approx(30.0)

    def test_after_a_gap_shifts_back_by_the_gap(self):
        gaps = {30.0: 5.0}
        assert wall_clock_to_data_position(35.0, gaps) == pytest.approx(30.0)
        assert wall_clock_to_data_position(40.0, gaps) == pytest.approx(35.0)

    def test_multiple_gaps_accumulate(self):
        # Splices at data positions 10 and 20; 2 s and 3 s of dead time.
        gaps = {10.0: 2.0, 20.0: 3.0}
        assert wall_clock_to_data_position(5.0, gaps) == pytest.approx(5.0)
        assert wall_clock_to_data_position(15.0, gaps) == pytest.approx(13.0)
        assert wall_clock_to_data_position(30.0, gaps) == pytest.approx(25.0)

    def test_gap_order_does_not_matter(self):
        """The map is a dict; insertion order must not change the answer."""
        forward = {10.0: 2.0, 20.0: 3.0}
        reverse = {20.0: 3.0, 10.0: 2.0}
        for onset in (5.0, 15.0, 30.0, 100.0):
            assert wall_clock_to_data_position(onset, forward) == wall_clock_to_data_position(onset, reverse)

    def test_onset_inside_dead_time_collapses_onto_the_splice(self):
        """No sample exists for this instant. Collapsing is lossy but honest; letting
        it drift would place the annotation on signal recorded after the pause."""
        gaps = {30.0: 5.0}
        for onset in (30.1, 32.5, 34.9):
            assert wall_clock_to_data_position(onset, gaps) == pytest.approx(30.0)

    def test_is_monotonic(self):
        gaps = {10.0: 2.0, 20.0: 3.0}
        onsets = [i * 0.5 for i in range(80)]
        positions = [wall_clock_to_data_position(o, gaps) for o in onsets]
        assert positions == sorted(positions)

    @pytest.mark.parametrize(
        "n_records, duration, gaps",
        [
            (5, 2.0, {}),
            (4, 1.0, {2.0: 3.0}),
            (4, 1.0, {1.0: 2.0, 3.0: 4.0}),
            (10, 0.5, {1.5: 0.25, 4.0: 10.0}),
        ],
    )
    def test_round_trips_against_compute_record_onsets(self, n_records, duration, gaps):
        """Every record's wall-clock onset must translate back to its data position."""
        wall = _compute_record_onsets(n_records, duration, gaps)
        for r, onset in enumerate(wall):
            assert wall_clock_to_data_position(onset, gaps) == pytest.approx(r * duration)


# ---------------------------------------------------------------------------
# normalise_edf_records
# ---------------------------------------------------------------------------


def _make_edfplus_multisec(
    *,
    n_records: int,
    rec_duration: float,
    anno_sample_count: int = 80,
    discontinuous: bool = False,
    tals_per_record: list[list[bytes]] | None = None,
    eeg_sample_count: int | None = None,
) -> bytes:
    """Build an EDF+C (or EDF+D) file with a configurable record duration.

    EEG sample count defaults to ``int(256 * rec_duration)`` so the channel
    always has a 256 Hz sampling rate regardless of record duration.
    """
    if eeg_sample_count is None:
        eeg_sample_count = round(256 * rec_duration)
    reserved = "EDF+D" if discontinuous else "EDF+C"
    signals = [
        {
            "label": "EEG Fp1",
            "sample_count": eeg_sample_count,
            "phys_min": -100,
            "phys_max": 100,
            "dig_min": -32768,
            "dig_max": 32767,
        },
        {
            "label": "EDF Annotations",
            "sample_count": anno_sample_count,
            "phys_min": -1,
            "phys_max": 1,
            "dig_min": -32768,
            "dig_max": 32767,
            "prefiltering": "",
        },
    ]
    header_bytes = _make_edf_header(
        reserved=reserved,
        n_records=n_records,
        rec_duration=rec_duration,
        signals=signals,
    )

    if tals_per_record is None:
        tals_per_record = [[] for _ in range(n_records)]

    anno_record_bytes = anno_sample_count * 2

    # Fill EEG with a recognisable pattern so we can verify slicing.
    data_records = b""
    for r in range(n_records):
        onset = r * rec_duration
        tals = tals_per_record[r] if r < len(tals_per_record) else []
        anno_data = _make_anno_record(onset, tals, anno_record_bytes)
        # Each EEG sample encodes its record index in the low byte (little-endian int16).
        eeg_data = b"".join((r & 0xFF).to_bytes(2, "little") for _ in range(eeg_sample_count))
        data_records += eeg_data + anno_data

    return header_bytes + data_records


class TestNormaliseEdfRecords:
    """Tests for normalise_edf_records (splitting and merging)."""

    def _write_tmp(self, content: bytes, suffix: str = ".edf") -> Path:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
            f.write(content)
        return Path(f.name)

    def _parse(self, file_bytes: bytes):
        header = parse_edf_header(file_bytes)
        signal_infos = parse_signal_infos(file_bytes, header)
        annotations, gaps = parse_annotations(file_bytes, header, signal_infos)
        return header, signal_infos, annotations, gaps

    # ── Already 1-second: no-op ───────────────────────────────────────────

    def test_already_1s_returns_false(self):
        fb = _make_edfplus_multisec(n_records=3, rec_duration=1.0)
        path = self._write_tmp(fb)
        header, si, annos, gaps = self._parse(fb)
        try:
            assert normalise_edf_records(path, fb, header, si, annos, gaps) is False
        finally:
            path.unlink(missing_ok=True)

    # ── Skip conditions ───────────────────────────────────────────────────

    def test_skip_non_integer_sampling_rate(self):
        # 2.5 s records, EEG sample_count=3 → rate = 1.2 Hz (non-integer)
        fb = _make_edfplus_multisec(n_records=2, rec_duration=2.5, eeg_sample_count=3)
        path = self._write_tmp(fb)
        header, si, annos, gaps = self._parse(fb)
        try:
            assert normalise_edf_records(path, fb, header, si, annos, gaps) is False
        finally:
            path.unlink(missing_ok=True)

    def test_skip_non_integer_record_duration(self):
        # 2.5 s records, EEG sample_count=640 → rate = 256 Hz (integer) but D is non-integer
        fb = _make_edfplus_multisec(n_records=2, rec_duration=2.5, eeg_sample_count=640)
        path = self._write_tmp(fb)
        header, si, annos, gaps = self._parse(fb)
        try:
            assert normalise_edf_records(path, fb, header, si, annos, gaps) is False
        finally:
            path.unlink(missing_ok=True)

    def test_skip_merge_edfplusd_with_gaps(self):
        """EDF+D with 0.5 s records and a gap: merging must be skipped."""
        anno_bytes = 80 * 2
        signals = [
            {
                "label": "EEG Fp1",
                "sample_count": 128,
                "phys_min": -100,
                "phys_max": 100,
                "dig_min": -32768,
                "dig_max": 32767,
            },
            {
                "label": "EDF Annotations",
                "sample_count": 80,
                "phys_min": -1,
                "phys_max": 1,
                "dig_min": -32768,
                "dig_max": 32767,
                "prefiltering": "",
            },
        ]
        hdr_bytes = _make_edf_header(reserved="EDF+D", n_records=4, rec_duration=0.5, signals=signals)
        # Records 0, 1, 2 at expected times; record 3 has a gap (expected 1.5 s, TAL says 4.5 s)
        parts = []
        for r, onset in enumerate([0.0, 0.5, 1.0, 4.5]):
            parts.append(b"\x00\x00" * 128)  # EEG
            parts.append(_make_anno_record(onset, [], anno_bytes))
        fb = hdr_bytes + b"".join(parts)
        path = self._write_tmp(fb)
        header, si, annos, gaps = self._parse(fb)
        try:
            # Must skip because it's EDF+D + gaps + D < 1
            assert normalise_edf_records(path, fb, header, si, annos, gaps) is False
        finally:
            path.unlink(missing_ok=True)

    def test_skip_merge_non_divisible_record_count(self):
        # 0.5 s records, N=3 — not divisible by merge_factor=2
        fb = _make_edfplus_multisec(n_records=3, rec_duration=0.5)
        path = self._write_tmp(fb)
        header, si, annos, gaps = self._parse(fb)
        try:
            assert normalise_edf_records(path, fb, header, si, annos, gaps) is False
        finally:
            path.unlink(missing_ok=True)

    # ── Splitting ─────────────────────────────────────────────────────────

    def test_split_header_updated(self):
        """5 s records × 2 → 10 output records of 1 s each."""
        fb = _make_edfplus_multisec(n_records=2, rec_duration=5.0)
        path = self._write_tmp(fb)
        header, si, annos, gaps = self._parse(fb)
        try:
            result = normalise_edf_records(path, fb, header, si, annos, gaps)
            assert result is True
            assert header.data_record_count == 10
            assert header.data_record_duration == pytest.approx(1.0)
        finally:
            path.unlink(missing_ok=True)

    def test_split_sample_counts_updated(self):
        """EEG sample_count per record should be divided by the split factor."""
        fb = _make_edfplus_multisec(n_records=2, rec_duration=5.0)  # EEG: 256*5=1280
        path = self._write_tmp(fb)
        header, si, annos, gaps = self._parse(fb)
        try:
            normalise_edf_records(path, fb, header, si, annos, gaps)
            eeg_ch = next(s for s in si if not s.is_annotation_channel)
            assert eeg_ch.sample_count == 256
            assert eeg_ch.sampling_rate == pytest.approx(256.0)
        finally:
            path.unlink(missing_ok=True)

    def test_split_signal_bytes_sliced_correctly(self):
        """Each output record must contain the matching slice of the input EEG data.

        The fixture encodes the record index in every EEG sample (low byte).
        After splitting a 3-second record into 3 one-second records, sub-record
        j should contain the same sample byte as the original record.
        """
        fb = _make_edfplus_multisec(n_records=2, rec_duration=3.0)
        path = self._write_tmp(fb)
        header, si, annos, gaps = self._parse(fb)
        orig_n = header.data_record_count  # 2
        try:
            normalise_edf_records(path, fb, header, si, annos, gaps)
            new_bytes = path.read_bytes()
            new_header = parse_edf_header(new_bytes)
            new_si = parse_signal_infos(new_bytes, new_header)
            eeg_idx = next(i for i, s in enumerate(new_si) if not s.is_annotation_channel)
            new_rec_size = sum(s.sample_count * 2 for s in new_si)

            for orig_r in range(orig_n):
                for sub_j in range(3):
                    out_r = orig_r * 3 + sub_j
                    chan_off = sum(new_si[i].sample_count * 2 for i in range(eeg_idx))
                    rec_start = new_header.header_record_bytes + out_r * new_rec_size + chan_off
                    sample_byte = new_bytes[rec_start]  # low byte of first sample
                    # The fixture sets every sample to (record_index & 0xFF)
                    assert sample_byte == (orig_r & 0xFF), (
                        f"orig_r={orig_r} sub_j={sub_j}: expected {orig_r & 0xFF}, got {sample_byte}"
                    )
        finally:
            path.unlink(missing_ok=True)

    def test_split_tal_onsets_correct(self):
        """After splitting, each output record's TAL must have the correct onset."""
        fb = _make_edfplus_multisec(n_records=1, rec_duration=3.0)
        path = self._write_tmp(fb)
        header, si, annos, gaps = self._parse(fb)
        try:
            normalise_edf_records(path, fb, header, si, annos, gaps)
            new_bytes = path.read_bytes()
            new_hdr = parse_edf_header(new_bytes)
            new_si = parse_signal_infos(new_bytes, new_hdr)
            _, _ = parse_annotations(new_bytes, new_hdr, new_si)
            # Verify onset of each record via direct TAL parse
            anno_idx = next(i for i, s in enumerate(new_si) if s.is_annotation_channel)
            from recordings.processors.edf import _parse_tal_record

            rec_size = sum(s.sample_count * 2 for s in new_si)
            anno_off = sum(new_si[i].sample_count * 2 for i in range(anno_idx))
            anno_bytes = new_si[anno_idx].sample_count * 2
            for out_r in range(3):
                start = new_hdr.header_record_bytes + out_r * rec_size + anno_off
                onset, _ = _parse_tal_record(new_bytes[start : start + anno_bytes])
                assert onset == pytest.approx(out_r), f"record {out_r}: onset={onset}"
        finally:
            path.unlink(missing_ok=True)

    def test_split_text_annotations_bucketed(self):
        """Text annotations must appear in the correct output record after splitting."""
        tal_at_2s = _make_tal(2.3, "Spike")
        tal_at_4s = _make_tal(4.7, "Blink")
        fb = _make_edfplus_multisec(
            n_records=1,
            rec_duration=5.0,
            tals_per_record=[[tal_at_2s, tal_at_4s]],
            anno_sample_count=120,
        )
        path = self._write_tmp(fb)
        header, si, annos, gaps = self._parse(fb)
        try:
            normalise_edf_records(path, fb, header, si, annos, gaps)
            new_bytes = path.read_bytes()
            new_hdr = parse_edf_header(new_bytes)
            new_si = parse_signal_infos(new_bytes, new_hdr)
            new_annos, _ = parse_annotations(new_bytes, new_hdr, new_si)
            labels = {a.label for a in new_annos}
            assert "Spike" in labels
            assert "Blink" in labels
            # Onset values must survive the round-trip.
            spike = next(a for a in new_annos if a.label == "Spike")
            assert spike.onset == pytest.approx(2.3)
        finally:
            path.unlink(missing_ok=True)

    def test_split_clean_header_written(self):
        """The output file must have a de-identified header."""
        fb = _make_edfplus_multisec(n_records=2, rec_duration=2.0)
        path = self._write_tmp(fb)
        header, si, annos, gaps = self._parse(fb)
        try:
            normalise_edf_records(path, fb, header, si, annos, gaps)
            rewritten = path.read_bytes()
            assert rewritten[8:88].decode("ascii").strip() == "X X X X"
        finally:
            path.unlink(missing_ok=True)

    def test_split_edfplusd_gap_preserved(self):
        """Splitting an EDF+D file must preserve the gap structure."""
        anno_bytes_sz = 80 * 2
        signals = [
            {
                "label": "EEG Fp1",
                "sample_count": 512,  # 256 Hz × 2 s
                "phys_min": -100,
                "phys_max": 100,
                "dig_min": -32768,
                "dig_max": 32767,
            },
            {
                "label": "EDF Annotations",
                "sample_count": 80,
                "phys_min": -1,
                "phys_max": 1,
                "dig_min": -32768,
                "dig_max": 32767,
                "prefiltering": "",
            },
        ]
        hdr_bytes = _make_edf_header(reserved="EDF+D", n_records=2, rec_duration=2.0, signals=signals)
        rec0_eeg = b"\x00\x00" * 512
        rec1_eeg = b"\x01\x00" * 512
        # Record 0: onset=0, record 1: expected=2 but TAL says 7 (5 s gap)
        rec0_anno = _make_anno_record(0.0, [], anno_bytes_sz)
        rec1_anno = _make_anno_record(7.0, [], anno_bytes_sz)
        fb = hdr_bytes + rec0_eeg + rec0_anno + rec1_eeg + rec1_anno
        path = self._write_tmp(fb)
        header, si, annos, gaps = self._parse(fb)
        try:
            result = normalise_edf_records(path, fb, header, si, annos, gaps)
            assert result is True
            new_bytes = path.read_bytes()
            new_hdr = parse_edf_header(new_bytes)
            new_si = parse_signal_infos(new_bytes, new_hdr)
            # Two 2-second records → four 1-second records
            assert new_hdr.data_record_count == 4
            assert new_hdr.discontinuous
            # Check TAL onsets: 0, 1, 7, 8
            from recordings.processors.edf import _parse_tal_record

            anno_idx = next(i for i, s in enumerate(new_si) if s.is_annotation_channel)
            rec_size = sum(s.sample_count * 2 for s in new_si)
            anno_off = sum(new_si[i].sample_count * 2 for i in range(anno_idx))
            anno_sc = new_si[anno_idx].sample_count * 2
            expected_onsets = [0.0, 1.0, 7.0, 8.0]
            for out_r, exp in enumerate(expected_onsets):
                start = new_hdr.header_record_bytes + out_r * rec_size + anno_off
                onset, _ = _parse_tal_record(new_bytes[start : start + anno_sc])
                assert onset == pytest.approx(exp), f"record {out_r}: expected {exp}, got {onset}"
        finally:
            path.unlink(missing_ok=True)

    # ── Merging ───────────────────────────────────────────────────────────

    def test_merge_header_updated(self):
        """4 × 0.5 s records → 2 × 1 s records."""
        fb = _make_edfplus_multisec(n_records=4, rec_duration=0.5)  # EEG: 256*0.5=128
        path = self._write_tmp(fb)
        header, si, annos, gaps = self._parse(fb)
        try:
            result = normalise_edf_records(path, fb, header, si, annos, gaps)
            assert result is True
            assert header.data_record_count == 2
            assert header.data_record_duration == pytest.approx(1.0)
        finally:
            path.unlink(missing_ok=True)

    def test_merge_sample_counts_updated(self):
        """EEG sample_count per record should be multiplied by the merge factor."""
        fb = _make_edfplus_multisec(n_records=4, rec_duration=0.5)  # EEG: 128 → 256
        path = self._write_tmp(fb)
        header, si, annos, gaps = self._parse(fb)
        try:
            normalise_edf_records(path, fb, header, si, annos, gaps)
            eeg_ch = next(s for s in si if not s.is_annotation_channel)
            assert eeg_ch.sample_count == 256
            assert eeg_ch.sampling_rate == pytest.approx(256.0)
        finally:
            path.unlink(missing_ok=True)

    def test_merge_signal_bytes_concatenated(self):
        """After merging, the output record must hold concatenated sample bytes."""
        fb = _make_edfplus_multisec(n_records=4, rec_duration=0.5)
        path = self._write_tmp(fb)
        header, si, annos, gaps = self._parse(fb)
        try:
            normalise_edf_records(path, fb, header, si, annos, gaps)
            new_bytes = path.read_bytes()
            new_hdr = parse_edf_header(new_bytes)
            new_si = parse_signal_infos(new_bytes, new_hdr)
            eeg_idx = next(i for i, s in enumerate(new_si) if not s.is_annotation_channel)
            rec_size = sum(s.sample_count * 2 for s in new_si)
            anno_off = sum(new_si[i].sample_count * 2 for i in range(eeg_idx))
            # Output record 0 covers original records 0 and 1.
            # The fixture encodes record index in the low byte.
            start = new_hdr.header_record_bytes + 0 * rec_size + anno_off
            first_half = new_bytes[start : start + 128]  # original record 0 samples
            second_half = new_bytes[start + 128 * 2 : start + 128 * 2 + 128]  # 2 bytes/sample
            # Each sample is 2 bytes, low byte is record index.
            assert first_half[0] == 0  # record 0 → byte 0
            assert second_half[0] == 1  # record 1 → byte 1
        finally:
            path.unlink(missing_ok=True)

    # ── strip_annotation_text ─────────────────────────────────────────────

    def test_strip_during_split(self):
        """Text annotations must be absent from the file when strip=True."""
        tal = _make_tal(0.3, "PrivateNote")
        fb = _make_edfplus_multisec(
            n_records=1,
            rec_duration=3.0,
            tals_per_record=[[tal]],
            anno_sample_count=120,
        )
        path = self._write_tmp(fb)
        header, si, annos, gaps = self._parse(fb)
        try:
            normalise_edf_records(path, fb, header, si, annos, gaps, strip_annotation_text=True)
            new_bytes = path.read_bytes()
            assert b"PrivateNote" not in new_bytes
        finally:
            path.unlink(missing_ok=True)

    def test_strip_inplace_1s_records(self):
        """process_edf_file(strip_annotation_text=True) on a 1 s file removes text TALs."""
        tal = _make_tal(0.5, "Secret")
        fb = _make_edfplus_file(n_records=2, tals_per_record=[[tal], []])
        path = self._write_tmp(fb)
        try:
            result = process_edf_file(path, strip_annotation_text=True)
            # Parsed annotations still returned (used for DB storage).
            assert any(a.label == "Secret" for a in result.annotations)
            # But the text must be gone from the on-disk file.
            assert b"Secret" not in path.read_bytes()
        finally:
            path.unlink(missing_ok=True)

    # ── process_edf_file integration ──────────────────────────────────────

    def test_process_edf_file_normalises_and_updates_result(self):
        """process_edf_file on a 5 s file must return updated metadata."""
        fb = _make_edfplus_multisec(n_records=2, rec_duration=5.0)
        path = self._write_tmp(fb)
        try:
            result = process_edf_file(path)
            assert result.header.data_record_count == 10
            assert result.header.data_record_duration == pytest.approx(1.0)
            eeg_ch = next(s for s in result.signal_infos if not s.is_annotation_channel)
            assert eeg_ch.sample_count == 256
        finally:
            path.unlink(missing_ok=True)

    def test_process_edf_file_plain_1s_unchanged(self):
        """process_edf_file on an already-normalised file must leave structure intact."""
        raw = _make_edf_header()
        fb = raw + _make_edf_data([{"sample_count": 256}])
        path = self._write_tmp(fb)
        try:
            result = process_edf_file(path)
            assert result.header.data_record_count == 1
            assert result.header.data_record_duration == pytest.approx(1.0)
        finally:
            path.unlink(missing_ok=True)


class TestWriteEdf:
    """The float→EDF writer: round-trip through the parser, offset-aware scaling, validation."""

    def _read_channel(self, data, header, signal_infos, ch_idx):
        """Decode one channel's samples from the written file back to physical units."""
        import numpy as np

        spr = [s.sample_count for s in signal_infos]
        rec_size = sum(spr) * 2
        offset_in_record = sum(spr[:ch_idx]) * 2
        s = signal_infos[ch_idx]
        out = []
        for r in range(header.data_record_count):
            base = header.header_record_bytes + r * rec_size + offset_in_record
            seg = np.frombuffer(data, dtype="<i2", count=s.sample_count, offset=base)
            phys = (seg.astype(float) - s.digital_min) * (s.physical_max - s.physical_min) / (
                s.digital_max - s.digital_min
            ) + s.physical_min
            out.append(phys)
        return np.concatenate(out)

    def test_round_trip_parses_and_reconstructs(self):
        import numpy as np

        fs = 100
        n = 250  # 2.5 s → trims to 2 full 1-second records
        t = np.arange(n) / fs
        gravity = 9.81 + 0.5 * np.sin(2 * np.pi * 5 * t)
        lateral = 0.3 * np.cos(2 * np.pi * 3 * t)
        path = Path(tempfile.mktemp(suffix=".edf"))
        try:
            write_edf(
                path,
                [
                    EdfChannel("wrist_z", "m/s2", fs, gravity),
                    EdfChannel("wrist_x", "m/s2", fs, lateral),
                ],
            )
            data = path.read_bytes()
            header = parse_edf_header(data)
            sigs = parse_signal_infos(data, header)
            assert header.data_record_count == 2
            assert header.data_record_duration == pytest.approx(1.0)
            assert [s.label for s in sigs] == ["wrist_z", "wrist_x"]
            assert all(s.physical_unit == "m/s2" for s in sigs)
            assert all(s.sample_count == fs for s in sigs)
            # Gravity axis keeps its DC offset: range sits around 9.81, not 0.
            assert sigs[0].physical_min > 8.0
            assert sigs[0].physical_max < 11.0
            recon = self._read_channel(data, header, sigs, 0)
            assert np.max(np.abs(recon - gravity[: len(recon)])) < 1e-3
        finally:
            path.unlink(missing_ok=True)

    def test_physical_range_encloses_data_and_fits_field(self):
        import numpy as np

        samples = np.array([1.111111, 2.222222, 12.763914] * 40, dtype=float)
        path = Path(tempfile.mktemp(suffix=".edf"))
        try:
            write_edf(path, [EdfChannel("c", "uV", 100, samples)])
            data = path.read_bytes()
            sigs = parse_signal_infos(data, parse_edf_header(data))
            assert sigs[0].physical_min <= samples.min()
            assert sigs[0].physical_max >= samples.max()
            # Bounds must fit the 8-character EDF physical-min/max field.
            assert len(str(sigs[0].physical_min)) <= 8
            assert len(str(sigs[0].physical_max)) <= 8
        finally:
            path.unlink(missing_ok=True)

    def test_non_integer_rate_rejected(self):
        path = Path(tempfile.mktemp(suffix=".edf"))
        with pytest.raises(ValueError):
            write_edf(path, [EdfChannel("c", "uV", 99.7, [0.0] * 200)])

    def test_trims_partial_trailing_record(self):
        import numpy as np

        path = Path(tempfile.mktemp(suffix=".edf"))
        try:
            write_edf(path, [EdfChannel("c", "uV", 100, np.zeros(350))])
            header = parse_edf_header(path.read_bytes())
            assert header.data_record_count == 3  # 350 // 100; trailing 50 dropped
        finally:
            path.unlink(missing_ok=True)

    def test_empty_channels_rejected(self):
        with pytest.raises(ValueError):
            write_edf(Path(tempfile.mktemp(suffix=".edf")), [])
