"""Tests for the pooled ingest's re-dither (recordings/processors/redither.py)."""

import hashlib

import numpy as np
import pytest

from recordings.processors.edf import parse_edf_header
from recordings.processors.redither import _draw_noise, redither_edf
from recordings.tests.test_edf_processor import _make_edf_header

BDF_VERSION = bytes([0xFF]) + b"BIOSEMI"

SIGNALS = [
    {"label": "EEG Fp1", "unit": "uV", "phys_min": -100.0, "phys_max": 100.0, "sample_count": 256},
    {"label": "EEG Fp2", "unit": "uV", "phys_min": -100.0, "phys_max": 100.0, "sample_count": 128},
]


def _signals(dig_min, dig_max):
    return [{**s, "dig_min": dig_min, "dig_max": dig_max} for s in SIGNALS]


def _file(tmp_path, *, bdf=False, n_records=4, values=None, dig=(-32768, 32767)):
    width = 3 if bdf else 2
    signals = _signals(*dig)
    header = _make_edf_header(version=BDF_VERSION if bdf else b"0       ", n_records=n_records, signals=signals)
    count = sum(s["sample_count"] for s in signals) * n_records
    if values is None:
        values = np.random.default_rng(1).integers(dig[0] // 2, dig[1] // 2, count)
    samples = np.asarray(values, dtype=np.int64) & ((1 << (8 * width)) - 1)
    body = b"".join(int(v).to_bytes(width, "little") for v in samples)
    path = tmp_path / ("x.bdf" if bdf else "x.edf")
    path.write_bytes(header + body)
    return path, len(header), width


def _samples(data: bytes, offset: int, width: int) -> np.ndarray:
    raw = [int.from_bytes(data[i : i + width], "little", signed=True) for i in range(offset, len(data), width)]
    return np.asarray(raw)


class TestRedither:
    @pytest.mark.parametrize("bdf", [False, True])
    def test_header_and_length_unchanged_and_every_record_differs(self, tmp_path, bdf):
        path, offset, width = _file(tmp_path, bdf=bdf)
        before = path.read_bytes()
        redither_edf(path)
        after = path.read_bytes()
        assert after[:offset] == before[:offset]
        assert len(after) == len(before)
        assert hashlib.sha256(after).digest() != hashlib.sha256(before).digest()
        record = sum(s["sample_count"] for s in SIGNALS) * width
        for start in range(offset, len(before), record):
            assert after[start : start + record] != before[start : start + record]

    @pytest.mark.parametrize("bdf", [False, True])
    def test_each_sample_moves_by_at_most_one_step(self, tmp_path, bdf):
        path, offset, width = _file(tmp_path, bdf=bdf)
        before = _samples(path.read_bytes(), offset, width)
        redither_edf(path)
        after = _samples(path.read_bytes(), offset, width)
        assert np.abs(after - before).max() == 1

    def test_samples_stay_inside_the_digital_range(self, tmp_path):
        count = sum(s["sample_count"] for s in SIGNALS) * 4
        values = np.array([-2048, 2047] * (count // 2))
        path, offset, width = _file(tmp_path, values=values, dig=(-2048, 2047))
        redither_edf(path)
        after = _samples(path.read_bytes(), offset, width)
        assert after.min() >= -2048
        assert after.max() <= 2047

    def test_a_declared_range_wider_than_the_format_does_not_wrap(self, tmp_path):
        count = sum(s["sample_count"] for s in SIGNALS) * 4
        path, offset, width = _file(tmp_path, values=np.full(count, 32767), dig=(-32768, 40000))
        redither_edf(path)
        after = _samples(path.read_bytes(), offset, width)
        assert after.min() >= 32766

    def test_two_runs_on_the_same_file_differ(self, tmp_path):
        path, _offset, _width = _file(tmp_path)
        original = path.read_bytes()
        redither_edf(path)
        first = path.read_bytes()
        path.write_bytes(original)
        redither_edf(path)
        assert path.read_bytes() != first

    def test_a_file_the_gate_would_refuse_is_refused(self, tmp_path):
        path, _offset, _width = _file(tmp_path)
        path.write_bytes(path.read_bytes()[:-2])
        with pytest.raises(ValueError):
            redither_edf(path)
        annotated = [*SIGNALS, {"label": "EDF Annotations", "sample_count": 60}]
        header = _make_edf_header(n_records=1, signals=annotated)
        path.write_bytes(header + b"\x00" * (2 * sum(s["sample_count"] for s in annotated)))
        assert parse_edf_header(path.read_bytes()).signal_count == 3
        with pytest.raises(ValueError):
            redither_edf(path)


class TestNoise:
    def test_uniform_over_three_values(self):
        drawn = _draw_noise(300_000)
        assert drawn.size == 300_000
        values, counts = np.unique(drawn, return_counts=True)
        assert list(values) == [-1, 0, 1]
        assert np.all(np.abs(counts - 100_000) < 1_500)

    def test_empty_draw(self):
        assert _draw_noise(0).size == 0
