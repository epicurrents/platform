"""A hypnogram that arrives as a file of its own, beside the study it scores.

A scorer's export is often an EDF+ file holding nothing but annotations: one ``EDF Annotations`` signal, often one
record with a duration of 0, which ``parse_edf_header`` rightly refuses as a recording. Its stages are read here and
join the study as the same coded events a stage embedded in the study's own file becomes, through
``recordings.event_translation``, so nothing that reads a study's scoring needs to know which route it came by.

The file is read at the door and never kept, which is why this happens in the request or the import loop and not in
the worker. Its onsets count from its own header's start time, and the study's start time exists only in the study's
raw header: ingest blanks it and ``RecordingMeta.recording_date`` is always null. So a hypnogram cannot be attached to
a recording already ingested, since nothing is left to align it against. And its header identifies the subject as
fully as the study's does, while nothing de-identifies it, so what leaves this module is a stage code and two numbers.
The header, the labels and the bytes go no further, and nothing here puts any of them in a message. On the upload
route Django's memory threshold applies to the whole request, so beside any real study this file too is spooled to a
temporary file, which is removed when the request ends and left behind if the worker dies during it.

The stages wait on ``Recording.pending_scoring`` and become rows when the study first becomes openable
(``recordings.lifecycle.write_pending_scoring``, at the end of ingest or when post-ingest stages settle), so they follow
the study: a reader holding a grant from the upload finds no stage until the study itself can be read, and a study refused by the worker never has any. They must
not travel in a task argument: a stage is a reading about the subject, and no erasure path reaches the broker's
append-only file.

An annotation-only file is told from a recording by its signals rather than by its name, and a hypnogram is paired
with its study in an import by the subject field and start time the two headers share, since dataset file names need
not share a stem. A pairing that is not one-to-one is reported and not guessed at.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from pathlib import Path
from typing import BinaryIO

from recordings.event_translation import SourceEvent, translate_source_event, write_source_events

#: Two stage events closer than this count as contiguous. EDF writes onsets as ASCII decimals.
_CONTIGUOUS_SECONDS = 1e-6

#: A night scored epoch by epoch is a few tens of kilobytes; a file past this is something else.
MAX_HYPNOGRAM_BYTES = 4 * 1024 * 1024

#: The length given to a final stage that states no duration, the scoring epoch of the AASM and of Rechtschaffen and
#: Kales alike.
SCORING_EPOCH_SECONDS = 30.0

_FIXED_HEADER_BYTES = 256
_SIGNAL_HEADER_BYTES = 256
_SIGNAL_COUNT_FIELD = slice(252, 256)
_EDF_EXTENSIONS = (".bdf", ".edf")
_STAGE_PREFIX = "PSG_STAGE_"


class HypnogramError(ValueError):
    """A hypnogram that cannot stand beside its study. The message is safe to show the caller."""


def read_header_block(stream: BinaryIO) -> bytes:
    """The fixed header and signal block at the current position of *stream*, however short the stream is."""
    head = stream.read(_FIXED_HEADER_BYTES)
    try:
        signal_count = int(head[_SIGNAL_COUNT_FIELD].decode("ascii"))
    except (UnicodeDecodeError, ValueError):
        return head
    return head + stream.read(max(signal_count, 0) * _SIGNAL_HEADER_BYTES)


def _parse(block: bytes):
    """The header and signals of *block*, or ``None`` when it is not an EDF/BDF header at all."""
    from recordings.processors.edf import EdfParseError, parse_edf_header, parse_signal_infos

    try:
        header = parse_edf_header(block, allow_zero_duration=True)
        return header, parse_signal_infos(block, header)
    except (EdfParseError, IndexError, ValueError):
        return None


def is_annotation_file(block: bytes) -> bool:
    """True when the header *block* describes a file with annotation signals and nothing else."""
    parsed = _parse(block)
    if parsed is None:
        return False
    _, signals = parsed
    return bool(signals) and all(signal.is_annotation_channel for signal in signals)


def fails_as_recording(block: bytes) -> bool:
    """True for an annotation file ingest cannot take: one whose record duration of 0 ``parse_edf_header`` refuses.

    Narrower than :func:`is_annotation_file` on purpose. An annotation-only file with a real record duration parses,
    and is stored as a recording with no signals if it is sent as one.
    """
    return is_annotation_file(block) and _parse(block)[0].data_record_duration == 0


def _study_span(block: bytes):
    """The start time and duration a study's raw header *block* states."""
    from recordings.processors.edf import EdfParseError, parse_edf_header

    try:
        header = parse_edf_header(block)
    except (EdfParseError, IndexError, ValueError) as error:
        raise HypnogramError("The recording's header cannot be read to align the hypnogram with it.") from error
    return header.recording_date, max(header.data_record_count, 0) * header.data_record_duration


def _scored_events(data: bytes) -> tuple:
    """The start time of the file *data* and the scoring events in it, as ``{onset, duration, code}``."""
    from annotations.core_vocabularies import SCORING_STANDARD
    from recordings.processors.edf import parse_annotations

    parsed = _parse(data)
    if parsed is None or not is_annotation_file(data):
        raise HypnogramError("This file is not a hypnogram: it should hold annotations and no signals.")
    header, signals = parsed
    width = 3 if header.data_format.startswith("bdf") else 2
    record_size = sum(signal.sample_count for signal in signals) * width
    if not record_size:
        # Records of no size pass any length check, and parse_annotations walks every one of them in the request.
        raise HypnogramError("This file is not a hypnogram: its annotation signal has no samples.")
    if len(data) < header.header_record_bytes + max(header.data_record_count, 0) * record_size:
        # parse_annotations stops quietly at a cut, and part of a night reads as a shorter one.
        raise HypnogramError("This hypnogram file is truncated.")

    annotations, _ = parse_annotations(data, header, signals)
    events = []
    for annotation in annotations:
        if not (math.isfinite(annotation.onset) and math.isfinite(annotation.duration)):
            raise HypnogramError("This hypnogram holds a time that is not a number.")
        term = translate_source_event(
            SourceEvent(onset=annotation.onset, duration=annotation.duration, label=annotation.label)
        )
        if term is not None and term.standard == SCORING_STANDARD:
            events.append({"onset": annotation.onset, "duration": annotation.duration, "code": term.code})
    return header.recording_date, sorted(events, key=lambda event: event["onset"])


def _span_stages(events: list[dict]) -> None:
    """Give a stage that states no duration the time up to the next stage; the viewer reads an instant as one epoch."""
    stages = [event for event in events if event["code"].startswith(_STAGE_PREFIX)]
    for stage, following in zip(stages, [*stages[1:], None], strict=True):
        if not stage["duration"]:
            gap = following["onset"] - stage["onset"] if following else 0.0
            stage["duration"] = gap or SCORING_EPOCH_SECONDS


def _on_study_timeline(events: list[dict], offset: float, duration: float) -> list[dict]:
    """*events* moved by *offset* and cut to the study: a hypnogram often runs on past the end of the signal."""
    placed = []
    for event in events:
        start = event["onset"] + offset
        end = start + event["duration"]
        if start >= duration or end < 0 or (end == 0 and event["duration"]):
            continue
        start = max(start, 0.0)
        placed.append({**event, "onset": start, "duration": min(end, duration) - start})
    return placed


def _merge_stages(events: list[dict]) -> list[dict]:
    """*events* with each run of one stage as a single event, since every event written is two audited rows."""
    stages: list[dict] = []
    others = []
    for event in events:
        if not event["code"].startswith(_STAGE_PREFIX):
            others.append(event)
        elif (
            stages
            and stages[-1]["code"] == event["code"]
            and abs(stages[-1]["onset"] + stages[-1]["duration"] - event["onset"]) < _CONTIGUOUS_SECONDS
        ):
            stages[-1]["duration"] += event["duration"]
        else:
            stages.append(dict(event))
    return sorted([*stages, *others], key=lambda event: event["onset"])


def read_hypnogram(data: bytes, study_block: bytes) -> list[dict]:
    """The scoring events of the hypnogram file *data*, on the timeline of the study with raw header *study_block*.

    Each is ``{"onset", "duration", "code"}``, which is all that survives of the file and holds nothing from either
    header. Raises :class:`HypnogramError` for a file that is not a hypnogram, is cut short, names no stage, or starts
    as far from the study as the study is long, which is what a wrong pairing or one blanked date looks like. A mapped event that is
    not a scoring term is dropped, and so is free text, with no placeholder: the file is read for its scoring and the
    study's own annotations are the record of what happened during it.
    """
    if len(data) > MAX_HYPNOGRAM_BYTES:
        raise HypnogramError("This file is too large to be a hypnogram.")
    start, events = _scored_events(data)
    if not any(event["code"].startswith(_STAGE_PREFIX) for event in events):
        raise HypnogramError("No sleep stages were recognised in this hypnogram.")
    _span_stages(events)

    study_start, duration = _study_span(study_block)
    if start is None or study_start is None:
        raise HypnogramError("The hypnogram and the recording need readable start times to be aligned.")
    offset = (start - study_start).total_seconds()
    if abs(offset) >= duration:
        raise HypnogramError("This hypnogram starts too far from the recording; it may belong to another night.")

    placed = _on_study_timeline(events, offset, duration)
    if not any(event["code"].startswith(_STAGE_PREFIX) for event in placed):
        raise HypnogramError("None of this hypnogram's stages fall within the recording.")
    return _merge_stages(placed)


def without_stages(sources: Iterable[SourceEvent]) -> list[SourceEvent]:
    """*sources* less the stages: where a hypnogram file is given it is the scoring, and two would both be read."""
    kept = []
    for source in sources:
        term = translate_source_event(source)
        if term is None or not term.code.startswith(_STAGE_PREFIX):
            kept.append(source)
    return kept


def write_hypnogram(recording, events: list[dict]) -> int:
    """Write the events :func:`read_hypnogram` answered on *recording*. Returns how many were written."""
    sources = [SourceEvent(onset=event["onset"], duration=event["duration"], code=event["code"]) for event in events]
    return write_source_events(recording, sources, hash_prefix="hypnogram", discard_text=True, scoring_accepted=True)


def _read_file_block(path: Path) -> bytes:
    """The header block of the EDF/BDF file at *path*, empty for any other file or one that cannot be read.

    A file that cannot be read is left for the import to fail by itself rather than stopping the run here.
    """
    if path.suffix.lower() not in _EDF_EXTENSIONS:
        return b""
    try:
        with path.open("rb") as handle:
            return read_header_block(handle)
    except OSError:
        return b""


def _pairing_key(path: Path) -> tuple | None:
    """What a study and its hypnogram have in common: the directory, the subject field and the start time."""
    parsed = _parse(_read_file_block(path))
    if parsed is None or parsed[0].recording_date is None:
        return None
    return path.parent, parsed[0].patient_id, parsed[0].recording_date


def pair_hypnograms(paths: Iterable[Path]) -> tuple[list[Path], dict[Path, Path], list[Path]]:
    """Sort *paths* into ``(studies, {study: hypnogram}, unpaired hypnograms)``.

    A hypnogram pairs when exactly one study in its directory has the same subject field and start time, and no other
    hypnogram claims that study. Anything less is left unpaired rather than guessed at: a study scored by the wrong
    file reads as a finding about the wrong night, and a dataset with blanked headers matches on everything.
    """
    studies: list[Path] = []
    hypnograms: list[Path] = []
    for path in paths:
        (hypnograms if is_annotation_file(_read_file_block(path)) else studies).append(path)

    candidates: dict[tuple, list[Path]] = {}
    for study in studies:
        if (key := _pairing_key(study)) is not None:
            candidates.setdefault(key, []).append(study)
    claims: dict[tuple, list[Path]] = {}
    for hypnogram in hypnograms:
        claims.setdefault(_pairing_key(hypnogram), []).append(hypnogram)

    pairs = {}
    unpaired = []
    for key, claimants in claims.items():
        matches = candidates.get(key, [])
        if key is not None and len(claimants) == 1 and len(matches) == 1:
            pairs[matches[0]] = claimants[0]
        else:
            unpaired.extend(claimants)
    return studies, pairs, sorted(unpaired)
