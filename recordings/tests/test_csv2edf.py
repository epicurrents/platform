"""Tests for the CSV → EDF converter and its subconverter registry."""

from pathlib import Path

import pytest

from recordings.converters import csv2edf
from recordings.converters.csv2edf import (
    CsvConvertError,
    TremsysAccConverter,
    _read_csv_document,
    convert,
    register_csv_subconverter,
)
from recordings.processors.edf import EdfChannel, parse_edf_header, parse_signal_infos


def _tremsys_csv(rows: int = 250, fs: int = 100) -> str:
    """Build a tremsys_acc-format CSV: LF comment block, then CRLF data rows."""
    comments = "\n".join(["# source: rec.xlsx", f"# resampled_fs: {fs}", "# method: nearest-neighbour"])
    header = "time,leftwrist_x[m/s2],leftwrist_y[m/s2],leftwrist_z[m/s2]"
    body = "\r\n".join(f"{i / fs:.4f},{0.01 * i:.6f},{-0.02 * i:.6f},{9.81 + 0.001 * i:.6f}" for i in range(rows))
    return comments + "\n" + header + "\r\n" + body + "\r\n"


def _write(tmp_path: Path, text: str, name: str = "rec.csv") -> Path:
    path = tmp_path / name
    path.write_text(text)
    return path


@pytest.fixture
def clean_registry():
    """Restore the project-subconverter registry after a test mutates it."""
    saved = list(csv2edf._REGISTERED_SUBCONVERTERS)
    yield
    csv2edf._REGISTERED_SUBCONVERTERS[:] = saved


class _DummyConverter:
    """A subconverter that claims every document — used to test precedence."""

    name = "dummy"

    def detect(self, doc):
        return True

    def build(self, doc):
        return [EdfChannel("dummy", "uV", 100, [0.0] * 200)]


def test_read_csv_document_splits_comments_header_data(tmp_path):
    doc = _read_csv_document(_write(tmp_path, _tremsys_csv(rows=10)))
    assert doc.comments["resampled_fs"] == "100"
    assert doc.comments["method"] == "nearest-neighbour"
    assert doc.header[0] == "time"
    assert len(doc.header) == 4
    assert len(doc.columns["time"]) == 10


def test_non_numeric_cell_raises(tmp_path):
    bad = "# resampled_fs: 100\ntime,leftwrist_x[m/s2]\r\n0.0,abc\r\n"
    with pytest.raises(CsvConvertError):
        _read_csv_document(_write(tmp_path, bad))


def test_tremsys_detect_accepts_known_layout(tmp_path):
    doc = _read_csv_document(_write(tmp_path, _tremsys_csv(rows=10)))
    assert TremsysAccConverter().detect(doc) is True


def test_tremsys_detect_rejects_other_layout(tmp_path):
    doc = _read_csv_document(_write(tmp_path, "a,b\n1,2\n3,4\n", name="other.csv"))
    assert TremsysAccConverter().detect(doc) is False


def test_convert_produces_parseable_edf(tmp_path):
    edf_path, sidecar = convert(_write(tmp_path, _tremsys_csv(rows=250)), tmp_path)
    assert sidecar is None
    data = edf_path.read_bytes()
    header = parse_edf_header(data)
    sigs = parse_signal_infos(data, header)
    assert header.data_record_count == 2  # 250 samples at 100 Hz → 2 full records
    assert header.data_record_duration == pytest.approx(1.0)
    assert [s.label for s in sigs] == ["leftwrist_x", "leftwrist_y", "leftwrist_z"]
    assert all(s.physical_unit == "m/s2" for s in sigs)
    assert all(s.sample_count == 100 for s in sigs)


def test_convert_unrecognised_format_raises(tmp_path):
    path = _write(tmp_path, "alpha,beta\n1,2\n3,4\n", name="weird.csv")
    with pytest.raises(CsvConvertError):
        convert(path, tmp_path)


def test_registered_subconverter_takes_precedence(tmp_path, clean_registry):
    register_csv_subconverter(_DummyConverter())
    edf_path, _ = convert(_write(tmp_path, _tremsys_csv()), tmp_path)
    data = edf_path.read_bytes()
    sigs = parse_signal_infos(data, parse_edf_header(data))
    # The registered dummy runs before the built-in tremsys_acc.
    assert [s.label for s in sigs] == ["dummy"]
