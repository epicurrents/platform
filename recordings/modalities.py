"""What a recording may be declared to be, and the one condition a declaration carries.

Modality is declared, never inferred, for a polysomnography: the channel vote in ``_determine_modality`` would call
most of them EEG, and a long EEG with a chin lead and a belt looks like a sleep study to any heuristic. Every route
that sets it (upload, ``PATCH``, ``import_recordings --modality``) goes through this module, so the routes cannot
disagree on what is accepted.

A polysomnography must be continuous. The viewer's sleep module pages through a night by epoch on one timeline, so
a study is refused here rather than opened with epochs that do not line up with the clock. The test is what the
file's records say, through ``read_record_gaps``, and not the EDF+D marker: a file marked discontinuous whose records
run without a jump is continuous, and a file marked continuous whose onsets jump is not. Refusing happens before
the de-identification pass rewrites the file, so a refused EDF or BDF upload still holds the bytes as uploaded for
failed-mode preservation.
"""

from __future__ import annotations

from pathlib import Path

PSG = "psg"

#: Every value a client may declare. The options of the upload and edit dialogs, the types the channel vote returns,
#: and the modalities the viewer has a module for.
RECORDING_MODALITIES: frozenset[str] = frozenset(
    {"acc", "ecg", "ecog", "eeg", "ekg", "emg", "eog", "meg", "misc", "ncs", PSG, "seeg", "trig"}
)

_CONTINUOUS_EXTENSIONS: frozenset[str] = frozenset({".bdf", ".edf"})


class ModalityError(ValueError):
    """A modality that cannot be declared for this recording. The message is safe to show the caller."""


def normalise_modality(value: str | None) -> str:
    """Return *value* as stored, trimmed and lower-cased. An empty value clears the field; anything unknown raises."""
    modality = (value or "").strip().lower()
    if modality and modality not in RECORDING_MODALITIES:
        raise ModalityError(f"Unknown modality {modality!r}.")
    return modality


def count_file_gaps(path: Path) -> int:
    """How many times the timeline of the EDF/BDF file at *path* jumps, read without loading the signal."""
    from recordings.processors.edf import EdfParseError, parse_edf_header, parse_signal_infos, read_record_gaps

    with path.open("rb") as handle:
        header = parse_edf_header(handle.read(256))
        handle.seek(0)
        block = handle.read(header.header_record_bytes)
    if len(block) < header.header_record_bytes:
        # Parsed as it stands, a cut block gives blank signals, and blank signals have no timeline to break.
        raise EdfParseError(f"{path.name}: the signal block is truncated.")
    return len(read_record_gaps(path, header, parse_signal_infos(block, header)))


def ensure_continuous_file(path: Path) -> None:
    """Raise :class:`ModalityError` unless the file at *path* can stand as a polysomnography.

    A timeline that cannot be read refuses like a gap: ``read_record_gaps`` raises exactly so that "unreadable" is
    never taken for "continuous".
    """
    from recordings.processors.edf import EdfParseError

    if path.suffix.lower() not in _CONTINUOUS_EXTENSIONS:
        raise ModalityError("A polysomnography must be an EDF or BDF recording.")
    try:
        gaps = count_file_gaps(path)
    except (EdfParseError, OSError, ValueError) as error:
        raise ModalityError("The timeline of this recording cannot be read to confirm it is continuous.") from error
    if gaps:
        raise ModalityError(f"A polysomnography must be a continuous recording; this file has {gaps} interruption(s).")
