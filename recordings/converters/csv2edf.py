"""CSV → EDF ingest converter with a registry of per-format subconverters.

A CSV is not a self-describing signal format: the same ``.csv`` extension
covers wildly different device exports. This module owns the shared work —
splitting the leading ``# key: value`` comment block from the column header
and numeric table, then writing the resulting channels to EDF through
:func:`recordings.processors.edf.write_edf` — and delegates *recognition* to a
list of subconverters, each of which knows one export layout.

The first subconverter whose :meth:`detect` accepts the parsed document wins;
its :meth:`build` maps columns to :class:`~recordings.processors.edf.EdfChannel`
objects (label, physical unit, sampling rate, float samples). If none match,
:class:`CsvConvertError` is raised and the recording lands FAILED with a
message naming the known formats.

Projects inject their own subconverters with :func:`register_csv_subconverter`
from their ``AppConfig.ready()``; registered subconverters are tried before the
built-ins, so a project may override a built-in format. The converter is wired
to ``.csv`` in ``recordings.pipelines._BUILTIN_CONVERTERS`` and obeys the same
``convert(input_path, output_dir) -> (edf_path, sidecar)`` contract as the
Nicolet ``.e`` converter.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

from recordings.processors.edf import EdfChannel, write_edf


class CsvConvertError(Exception):
    """Raised when a CSV cannot be recognised or converted to EDF."""


@dataclass
class CsvDocument:
    """A generic parse of a CSV upload.

    ``comments`` holds the leading ``# key: value`` lines, ``header`` the
    column names in file order, and ``columns`` maps each name to its parsed
    float values. ``path`` is kept so a subconverter that needs the raw bytes
    (an unusual layout the generic parse cannot represent) can reach them.
    """

    comments: dict[str, str]
    header: list[str]
    columns: dict[str, list[float]]
    path: Path = field(repr=False)


@runtime_checkable
class CsvSubconverter(Protocol):
    """Recognises one CSV export layout and maps it to EDF channels."""

    name: str

    def detect(self, doc: CsvDocument) -> bool:
        """Return True when this subconverter recognises *doc*'s layout."""
        ...

    def build(self, doc: CsvDocument) -> list[EdfChannel]:
        """Map the recognised document to EDF channels."""
        ...


# ── tremsys_acc — the simple wrist-accelerometer export ─────────────────────

# Column header of the form ``label[unit]`` — e.g. ``leftwrist_x[m/s2]``.
_LABEL_UNIT_RE = re.compile(r"^\s*(?P<label>[^\[]+?)\s*\[(?P<unit>[^\]]*)\]\s*$")


class TremsysAccConverter:
    """Wrist-accelerometer CSV: a ``time`` column plus ``label[unit]`` signal
    columns, with the sampling rate declared in a ``resampled_fs`` comment.

    Recognised by a leading ``time`` column, every other column matching the
    ``label[unit]`` shape, and a ``resampled_fs`` comment carrying the integer
    sampling rate the rows were resampled onto.
    """

    name = "tremsys_acc"

    def detect(self, doc: CsvDocument) -> bool:
        if not doc.header or "resampled_fs" not in doc.comments:
            return False
        if doc.header[0].strip().lower() != "time":
            return False
        signal_cols = doc.header[1:]
        return bool(signal_cols) and all(_LABEL_UNIT_RE.match(c) for c in signal_cols)

    def build(self, doc: CsvDocument) -> list[EdfChannel]:
        try:
            sampling_rate = float(doc.comments["resampled_fs"])
        except (KeyError, ValueError) as exc:
            raise CsvConvertError("tremsys_acc: missing or non-numeric 'resampled_fs' comment.") from exc
        channels: list[EdfChannel] = []
        for column in doc.header[1:]:
            match = _LABEL_UNIT_RE.match(column)
            if match is None:
                continue
            channels.append(
                EdfChannel(
                    label=match.group("label"),
                    physical_unit=match.group("unit"),
                    sampling_rate=sampling_rate,
                    samples=doc.columns[column],
                )
            )
        if not channels:
            raise CsvConvertError("tremsys_acc: no signal columns found.")
        return channels


_BUILTIN_SUBCONVERTERS: list[CsvSubconverter] = [TremsysAccConverter()]
_REGISTERED_SUBCONVERTERS: list[CsvSubconverter] = []


def register_csv_subconverter(subconverter: CsvSubconverter) -> None:
    """Register a project-specific CSV subconverter.

    Call from a project's ``AppConfig.ready()``. Registered subconverters are
    tried before the built-ins, so a project may override a built-in format.
    Idempotent — registering the same instance twice has no effect.
    """
    if subconverter not in _REGISTERED_SUBCONVERTERS:
        _REGISTERED_SUBCONVERTERS.append(subconverter)


def _subconverters() -> list[CsvSubconverter]:
    return [*_REGISTERED_SUBCONVERTERS, *_BUILTIN_SUBCONVERTERS]


def _read_csv_document(path: Path) -> CsvDocument:
    """Parse *path* into comments, a column header, and per-column float values.

    Leading ``# key: value`` lines become ``comments``; the first non-comment,
    non-blank line is the header; the rest are the numeric table. Non-numeric
    cells raise :class:`CsvConvertError`.
    """
    comments: dict[str, str] = {}
    data_lines: list[str] = []
    for line in path.read_text().splitlines():
        if line.startswith("#"):
            body = line[1:].strip()
            if ":" in body:
                key, value = body.split(":", 1)
                comments[key.strip()] = value.strip()
            continue
        if line.strip():
            data_lines.append(line)

    rows = list(csv.reader(data_lines))
    if not rows:
        raise CsvConvertError("CSV has no header or data rows.")

    header = [name.strip() for name in rows[0]]
    columns: dict[str, list[float]] = {name: [] for name in header}
    for row in rows[1:]:
        for name, cell in zip(header, row):
            try:
                columns[name].append(float(cell))
            except ValueError as exc:
                raise CsvConvertError(f"Non-numeric value {cell!r} in column {name!r}.") from exc
    return CsvDocument(comments=comments, header=header, columns=columns, path=path)


def convert(input_path: Path, output_dir: Path) -> tuple[Path, dict | None]:
    """Convert the CSV at *input_path* to an EDF written under *output_dir*.

    Parses the document, dispatches to the first matching subconverter, and
    writes the channels to a de-identified EDF. Returns ``(edf_path, None)``;
    the sidecar slot stays None until a format needs to surface events.
    """
    document = _read_csv_document(input_path)
    for subconverter in _subconverters():
        if subconverter.detect(document):
            channels = subconverter.build(document)
            edf_path = output_dir / f"{input_path.stem}.edf"
            write_edf(edf_path, channels)
            return edf_path, None
    raise CsvConvertError(
        f"No CSV subconverter recognised {input_path.name!r}. Known formats: {[s.name for s in _subconverters()]}."
    )
