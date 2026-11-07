"""The record-aligned window reader behind the version signal endpoint: native rates, exact ranges, both widths."""

import numpy as np
import pytest

from recordings.processors.edf_window import WindowError, read_window
from recordings.tests.test_edf_processor import _make_edf_header


def _file(tmp_path, *, bdf=False, n_records=3):
    """Two channels at 4 and 2 samples per one-second record, the digital value counting up, range ±1000."""
    signals = [
        {"label": "Fp1", "sample_count": 4, "dig_min": -1000, "dig_max": 1000, "phys_min": -500.0, "phys_max": 500.0},
        {"label": "ECG", "sample_count": 2, "dig_min": -1000, "dig_max": 1000, "phys_min": -500.0, "phys_max": 500.0},
    ]
    width = 3 if bdf else 2
    header = bytearray(_make_edf_header(signals=signals, n_records=n_records))
    if bdf:
        header[0:8] = b"\xffBIOSEMI"
        header[192:236] = b"24BIT".ljust(44)
    data = bytearray()
    value = -6
    for _ in range(n_records):
        for signal in signals:
            for _ in range(signal["sample_count"]):
                data += int(value).to_bytes(width, "little", signed=True)
                value += 1
    path = tmp_path / ("w.bdf" if bdf else "w.edf")
    path.write_bytes(header + bytes(data))
    return path


@pytest.mark.parametrize("bdf", [False, True])
def test_each_channel_comes_back_at_its_own_rate_over_whole_records(tmp_path, bdf):
    fp1, ecg = read_window(_file(tmp_path, bdf=bdf), ["Fp1", "ECG"], 1, 2)
    # Record 0 held digital -6..-1; record 1 holds Fp1 0..3 and ECG 4..5; record 2 Fp1 6..9, ECG 10..11.
    assert (fp1.fs, fp1.start, ecg.fs, ecg.start) == (4.0, 4, 2.0, 2)
    assert np.array_equal(fp1.samples, np.array([0, 1, 2, 3, 6, 7, 8, 9], dtype=np.float32) * 0.5)
    assert np.array_equal(ecg.samples, np.array([4, 5, 10, 11], dtype=np.float32) * 0.5)
    assert fp1.samples.dtype == np.float32


def test_negative_bdf_samples_keep_their_sign(tmp_path):
    (fp1,) = read_window(_file(tmp_path, bdf=True), ["Fp1"], 0, 1)
    assert np.array_equal(fp1.samples, np.array([-6, -5, -4, -3], dtype=np.float32) * 0.5)


def test_channels_come_back_in_the_order_asked_for(tmp_path):
    windows = read_window(_file(tmp_path), ["ECG", "Fp1"], 0, 1)
    assert [w.name for w in windows] == ["ECG", "Fp1"]


@pytest.mark.parametrize(("names", "start", "count"), [(["T3"], 0, 1), (["Fp1"], 2, 2), (["Fp1"], -1, 1)])
def test_a_window_the_file_cannot_answer_is_refused(tmp_path, names, start, count):
    with pytest.raises(WindowError):
        read_window(_file(tmp_path), names, start, count)


def test_an_annotation_channel_is_never_decoded(tmp_path):
    """The signal route hands samples to de-identifying readers; annotation bytes are text, which they must not get."""
    signals = [
        {"label": "Fp1", "sample_count": 4, "dig_min": -1000, "dig_max": 1000, "phys_min": -500.0, "phys_max": 500.0},
        {"label": "EDF Annotations", "sample_count": 4, "dig_min": -32768, "dig_max": 32767},
    ]
    path = tmp_path / "a.edf"
    path.write_bytes(_make_edf_header(signals=signals, n_records=1, reserved="EDF+C") + b"\x00" * 16)
    assert [w.name for w in read_window(path, ["Fp1"], 0, 1)] == ["Fp1"]
    with pytest.raises(WindowError):
        read_window(path, ["EDF Annotations"], 0, 1)
