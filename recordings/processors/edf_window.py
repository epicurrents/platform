"""Decode a record-aligned window of named EDF/BDF channels to physical float32 samples, each at its own rate.

The reader behind ``GET /{hash}/versions/{version_id}/signal`` (``version-fetch-contract.md`` §2.2–2.3). A window is
whole data records, so every channel's sample range is exact whatever its rate — an EDF record holds an integer number
of samples of every signal — and each channel comes back at its native rate rather than resampled to a common one,
which is what MNE's reader does and what the contract exists to avoid.

Physical values follow the header's linear map from the digital to the physical range. Annotation channels are never
decoded: their bytes are text, and text is what a de-identifying grant strips.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

from recordings.processors.edf import EdfHeader, EdfSignalInfo, parse_edf_header, parse_signal_infos


class WindowError(ValueError):
    """A window the file cannot answer: an unknown or annotation channel, or records out of range."""


@dataclass(frozen=True)
class ChannelWindow:
    """One channel's samples over the window, with what a reader needs to place and label them."""

    name: str
    fs: float
    start: int
    units: str
    samples: np.ndarray  # float32, physical units


def read_header(path: Path) -> tuple[EdfHeader, list[EdfSignalInfo]]:
    """The general header and the signal headers of the EDF/BDF at *path*, read without its data records."""
    with path.open("rb") as handle:
        head = handle.read(256)
        size = parse_edf_header(head).header_record_bytes
        head += handle.read(size - 256)
    header = parse_edf_header(head)
    return header, parse_signal_infos(head, header)


def read_window(path: Path, names: list[str], start_record: int, n_records: int) -> list[ChannelWindow]:
    """Decode *names* over data records ``[start_record, start_record + n_records)`` of the file at *path*.

    Channels come back in the order asked for. A name is matched against the stored label exactly; a label that
    appears twice answers with its first occurrence, as the stored file's naming ensures it does not.
    """
    header, infos = read_header(path)
    by_name: dict[str, int] = {}
    for index, info in enumerate(infos):
        if not info.is_annotation_channel:
            by_name.setdefault(info.label.strip(), index)
    missing = [name for name in names if name not in by_name]
    if missing:
        raise WindowError(f"Channels not in this version: {', '.join(missing)}.")
    if start_record < 0 or n_records <= 0 or start_record + n_records > header.data_record_count:
        raise WindowError("The window falls outside the recording.")

    width = 3 if header.data_format.rstrip("+").lower() == "bdf" else 2
    sizes = [info.sample_count * width for info in infos]
    offsets = np.cumsum([0, *sizes[:-1]])
    record_size = sum(sizes)
    with path.open("rb") as handle:
        handle.seek(header.header_record_bytes + start_record * record_size)
        block = handle.read(n_records * record_size)
    if len(block) != n_records * record_size:
        raise WindowError("The file ends before the window does.")
    records = np.frombuffer(block, dtype=np.uint8).reshape(n_records, record_size)

    duration = header.data_record_duration
    windows = []
    for name in names:
        index = by_name[name]
        info = infos[index]
        raw = records[:, offsets[index] : offsets[index] + sizes[index]].reshape(-1)
        windows.append(
            ChannelWindow(
                name=name,
                fs=info.sample_count / duration if duration > 0 else 0.0,
                start=start_record * info.sample_count,
                units=info.physical_unit.strip(),
                samples=_physical(_digital(raw, width), info),
            )
        )
    return windows


def _digital(raw: np.ndarray, width: int) -> np.ndarray:
    """Little-endian two's-complement samples of *width* bytes as int32."""
    if width == 2:
        return raw.view("<i2").astype(np.int32)
    triples = raw.reshape(-1, 3).astype(np.int32)
    value = triples[:, 0] | (triples[:, 1] << 8) | (triples[:, 2] << 16)
    return np.where(value & 0x800000, value - 0x1000000, value)


def _physical(digital: np.ndarray, info: EdfSignalInfo) -> np.ndarray:
    span = info.digital_max - info.digital_min
    if span == 0:
        return np.full(digital.shape, info.physical_min, dtype=np.float32)
    gain = (info.physical_max - info.physical_min) / span
    return ((digital - info.digital_min) * gain + info.physical_min).astype(np.float32)
