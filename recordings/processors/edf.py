"""EDF/BDF file processor.

⚠️ LOAD-BEARING — PHI removal in EDF / BDF headers.
``_build_clean_header`` (and the ``rewrite_edf_header`` wrapper that
writes its output to disk) is the canonical de-identification function
for EDF headers across the platform.  Four hardcoded byte values are
the deliberate PHI-removal contract:

* ``patient_bytes  = _pad("X X X X", 80)``       — patient field
* ``recording_bytes = _pad("Startdate X X X X", 80)`` — recording field
* ``startdate_bytes = b"01.01.85"`` — EDF anonymisation convention
* ``starttime_bytes = b"00.00.00"``

These look like they could be parameterised "for testability", or
swapped for a real value "to be more useful".  Don't.  Each silent
change of those constants leaks PHI:

* On every recording uploaded via the API (because the upload Celery
  task calls ``rewrite_edf_header`` at ingest, so the *stored* file
  carries the de-identified header).
* On every federated download served with ``apply_middleware=True``
  (because ``federation.middleware.AnonymizeEDFHeader`` delegates to
  this function — and that middleware *fails open* on parse error, so
  this function producing the right bytes is the last line of defense
  in that path).

See AGENTS.md → *Load-bearing files* before modifying.  The contract
tests in ``recordings/tests/test_edf_processor.py::TestRewriteEdfHeader``
assert each PHI-removal byte explicitly (``test_patient_field_blanked``,
``test_recording_field_blanked``, ``test_start_date_anonymised``,
``test_start_time_zeroed``) plus EDF+C/EDF+D marker preservation, BDF
binary version byte, ASCII cleaning, and data-records-untouched
invariants.

Supports:
- EDF  (European Data Format)       — 16-bit samples, plain header
- EDF+ (EDF plus, continuous)       — EDF with TAL annotation channel
- EDF+D (EDF plus, discontinuous)   — EDF+ with gaps between data records
- BDF  (Biosemi Data Format)        — 24-bit samples, plain header
- BDF+ / BDF+D                      — BDF equivalents of the above

Processing steps
----------------
1. Parse the file header leniently (non-fatal field errors are skipped).
2. Scan annotation channels (TAL format) to extract embedded text events and
   detect data gaps in discontinuous recordings.
3. Rewrite the header in-place to strict EDF+/BDF+ compliance:
   - All text fields cleaned to 7-bit ASCII.
   - Full de-identification of patient and recording fields.
   - Recording date/time replaced with the EDF anonymisation convention.
4. Return a structured result for the caller to persist to the database.

Bug fixes vs. the reference TypeScript EdfDecoder
--------------------------------------------------
- ``getAllSections`` fallback values: the TS version uses ``|| '--'`` on an
  array return, which is always truthy and therefore never used.  Python raises
  explicitly instead.
- ``priorOffset`` double-counting: if a file has multiple annotation channels
  per record (rare but valid), the TS code accumulates the same gap once per
  annotation channel.  Here the gap is detected and accumulated only once per
  record.
- ``byteArray`` inside inner loop: the TS code wraps the whole buffer in a new
  ``Uint8Array`` on every signal × record iteration.  Here a single
  ``memoryview`` is created once and sliced cheaply.
"""

from __future__ import annotations

import bisect
import contextlib
import re
import unicodedata
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from itertools import accumulate
from pathlib import Path
from typing import NamedTuple

from recordings.processors.channel_labels import classify_channel

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# EDF field widths (bytes), per the EDF/EDF+ specification.
_FW_VERSION = 8
_FW_PATIENT = 80
_FW_RECORDING = 80
_FW_STARTDATE = 8
_FW_STARTTIME = 8
_FW_HEADER_BYTES = 8
_FW_RESERVED = 44
_FW_NRECS = 8
_FW_REC_DURATION = 8
_FW_NS = 4

# Signal header field widths (per channel).
_SW_LABEL = 16
_SW_TRANSDUCER = 80
_SW_PHYS_UNIT = 8
_SW_PHYS_MIN = 8
_SW_PHYS_MAX = 8
_SW_DIG_MIN = 8
_SW_DIG_MAX = 8
_SW_PREFILTERING = 80
_SW_SAMPLE_COUNT = 8
_SW_RESERVED = 32

# TAL byte markers (per EDF+ spec).
_TAL_FIELD_SEP = 0x14  # separates onset / duration / labels within a TAL
_TAL_DUR_SEP = 0x15  # separates onset from duration
_TAL_END = 0x00  # terminates a TAL (null byte)

# Label used by EDF+ and BDF+ annotation channels.
_ANNO_LABEL_EDF = "edf annotations"
_ANNO_LABEL_BDF = "bdf annotations"

# Symbols with ASCII substitutions that preserve meaning in narrow fields.
# Applied before NFKD decomposition so multi-char replacements are controlled.
_SYMBOL_MAP = str.maketrans(
    {
        "µ": "u",  # micro sign → u (uV stays 2 chars)
        "μ": "u",  # Greek mu
        "Ω": "Ohm",
        "°": "deg",
        "α": "a",
        "β": "b",
        "γ": "g",
        "δ": "d",
        "θ": "th",
        "φ": "ph",
        "σ": "s",
        "²": "2",
        "³": "3",
        "\u00a0": " ",  # non-breaking space → space
        "\u2013": "-",  # en dash
        "\u2014": "-",  # em dash
        "\u2019": "'",  # right single quotation mark
        "\u201c": '"',  # left double quotation mark
        "\u201d": '"',  # right double quotation mark
        "\u2026": "...",  # horizontal ellipsis
    }
)

# Default label-to-type matchers applied when no custom matchers are given.
# Checked in order; first match wins. Case-insensitive.
_DEFAULT_TYPE_MATCHERS: list[tuple[str, str]] = [
    (r"emg", "emg"),
    (r"eog", "eog"),
    (r"ecg|ekg", "ekg"),
    (r"eeg", "eeg"),
]


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class EdfSignalInfo:
    """Parsed metadata for a single EDF/BDF signal channel."""

    label: str
    transducer_type: str
    physical_unit: str
    physical_min: float
    physical_max: float
    digital_min: int
    digital_max: int
    prefiltering: str
    sample_count: int
    reserved: str

    # Derived fields computed from the above.
    units_per_bit: float = 0.0
    digital_offset: float = 0.0
    sampling_rate: float = 0.0
    signal_type: str = ""
    canonical_label: str = ""
    is_annotation_channel: bool = False

    # Parsed filter values (Hz); 0 means not specified.
    highpass: float = 0.0
    lowpass: float = 0.0
    notch: float = 0.0

    # Originals of the fields deidentify_signal_infos rewrites, captured before
    # the rewrite so ingest can persist them author-private. Empty until the
    # cleaning pass runs (and for values that were empty in the source file).
    source_label: str = ""
    source_transducer_type: str = ""
    source_prefiltering: str = ""
    # Zero-based position of this channel in the uploaded file, captured by
    # reorder_edf_channels before any permutation; -1 until that pass runs.
    # Persisted author-private (the original template order is itself part of
    # the site fingerprint the reorder removes).
    source_index: int = -1


@dataclass
class EdfHeader:
    """Parsed EDF/BDF general header."""

    data_format: str  # 'edf', 'edf+', 'bdf', 'bdf+'
    patient_id: str
    local_recording_id: str
    recording_date: datetime | None
    header_record_bytes: int
    reserved: str
    data_record_count: int
    data_record_duration: float
    signal_count: int
    is_plus: bool
    discontinuous: bool
    record_byte_size: int = 0  # filled in after signal info is parsed
    signal_infos: list[EdfSignalInfo] = field(default_factory=list)


class AnnotationEntry(NamedTuple):
    """A single TAL annotation extracted from an EDF+/BDF+ file.

    ``onset`` is **wall-clock** time — the TAL field exactly as the file records it,
    counted from the recording's start including any time acquisition was paused. It is
    deliberately *not* translated here, because the file rewriter (
    :func:`_encode_tal_entry`) must be able to write the annotation back out with the
    timestamp the format demands. Anything that indexes into *samples* wants the other
    timeline instead — see :func:`wall_clock_to_data_position`.
    """

    onset: float  # seconds from recording start, wall clock (gaps included)
    duration: float  # seconds (0 if not specified)
    label: str


# Map from data-position (seconds) to gap duration (seconds).
GapMap = dict[float, float]


def wall_clock_to_data_position(onset: float, gaps: GapMap) -> float:
    """Translate a wall-clock *onset* into a data position, given a recording's *gaps*.

    A discontinuous recording has **two timelines** and conflating them is the whole
    bug class this function exists to close:

    * **Wall clock** — what the TAL onsets and a human's notes use. It includes the time
      acquisition was paused, so it has holes: no sample corresponds to a wall-clock
      instant inside a gap.
    * **Data position** — sample index divided by sampling rate. Total, contiguous, hole
      free, and the *only* timeline in which ``sample = t * fs`` is true. Every reader
      (MNE included) hands back records back-to-back, so this is the timeline a signal
      window, a segment plan and a detector's onset all live in. In data position a gap
      is not a hole but an instantaneous **splice**.

    The platform's canonical timeline is data position, for the plain reason that it is
    the one the samples are actually in — ``annotations.Interruption`` rows already store
    their timestamps this way, which makes the gap map a durable translation table rather
    than something that has to be recovered from the file every time.

    An onset that falls *inside* a gap is timestamped in dead time — the recording holds
    no sample for it — and collapses onto the splice itself. That is a lossy but honest
    answer: the alternative, letting it drift into the following segment, silently moves
    an annotation onto unrelated signal.

    Pure and monotonic; with no gaps it is the identity.
    """
    if not gaps:
        return onset
    shift = 0.0  # wall-clock time accumulated in gaps already passed
    for position in sorted(gaps):
        gap_start = position + shift  # where this gap opens in wall clock
        if onset < gap_start:
            break
        gap_end = gap_start + gaps[position]
        if onset < gap_end:
            return position  # inside dead time: collapse onto the splice
        shift += gaps[position]
    return onset - shift


@dataclass
class EdfProcessingResult:
    """All information extracted by :func:`process_edf_file`."""

    header: EdfHeader
    signal_infos: list[EdfSignalInfo]
    annotations: list[AnnotationEntry]
    gaps: GapMap


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class EdfParseError(Exception):
    """Raised for unrecoverable header parse failures."""


def _ascii_clean(text: str, max_bytes: int | None = None) -> str:
    """Normalize *text* to 7-bit ASCII, applying symbol substitutions first.

    Steps:
    1. Replace known symbols (µ→u, Ω→Ohm, °→deg, …) via a translation table.
    2. NFKD-decompose the result so accented characters become base+combining.
    3. Encode as ASCII, silently dropping any remaining non-ASCII bytes.
    4. Optionally truncate to *max_bytes*.
    """
    text = text.translate(_SYMBOL_MAP)
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    if max_bytes is not None:
        text = text[:max_bytes]
    return text


def _pad(text: str, width: int) -> bytes:
    """Return *text* ASCII-encoded and space-padded (or truncated) to *width* bytes."""
    cleaned = _ascii_clean(text, max_bytes=width)
    return cleaned.ljust(width).encode("ascii")


def _read_field(data: bytes | memoryview, offset: int, width: int) -> str:
    """Read *width* bytes at *offset* and decode as Latin-1, then strip spaces.

    Latin-1 (ISO-8859-1) is used because it covers all 256 byte values and
    preserves characters like µ (0xB5) and ° (0xB0) as their Unicode
    equivalents.  This allows :func:`_ascii_clean` to apply its symbol table
    correctly before dropping residual non-ASCII bytes.
    """
    raw = bytes(data[offset : offset + width])
    return raw.decode("latin-1").strip()


def _floats_nearly_equal(a: float, b: float, decimals: int = 10) -> bool:
    """Return True when *a* and *b* agree to *decimals* decimal places."""
    return round(abs(a - b), decimals) == 0


# ---------------------------------------------------------------------------
# Signal type / prefiltering
# ---------------------------------------------------------------------------


def extract_signal_type(
    label: str,
    extra_matchers: list[tuple[str, str]] | None = None,
) -> str:
    """Infer a signal type string from a channel *label*.

    *extra_matchers* is a list of ``(regex_pattern, type_string)`` pairs
    checked before the defaults.  Returns ``''`` when no match is found.
    """
    matchers = list(extra_matchers or []) + _DEFAULT_TYPE_MATCHERS
    for pattern, sig_type in matchers:
        if re.search(pattern, label, re.IGNORECASE):
            return sig_type
    return ""


def parse_prefiltering(text: str) -> tuple[float, float, float]:
    """Parse an EDF prefiltering field into ``(highpass, lowpass, notch)`` Hz.

    Follows the convention suggested in the EDF spec:
    ``HP:0.1Hz LP:75Hz N:50Hz`` (case-insensitive, any order).
    Returns 0.0 for any component not found in *text*.
    """
    hp_m = re.search(r"HP:([0-9.]+)Hz", text, re.IGNORECASE)
    lp_m = re.search(r"LP:([0-9.]+)Hz", text, re.IGNORECASE)
    n_m = re.search(r"N:([0-9.]+)Hz", text, re.IGNORECASE)
    return (
        float(hp_m.group(1)) if hp_m else 0.0,
        float(lp_m.group(1)) if lp_m else 0.0,
        float(n_m.group(1)) if n_m else 0.0,
    )


def format_prefiltering(highpass: float, lowpass: float, notch: float) -> str:
    """Render parsed filter values back into the spec-suggested prefiltering form.

    The inverse of :func:`parse_prefiltering` for the values it recognises:
    ``format_prefiltering(*parse_prefiltering(text))`` re-parses to the same
    triple. Components equal to 0 (not specified) are omitted; all-zero input
    yields ``''``. Values are formatted with ``%g`` so ``0.5`` and ``75`` come
    out without trailing zeros.
    """
    parts = []
    if highpass:
        parts.append(f"HP:{highpass:g}Hz")
    if lowpass:
        parts.append(f"LP:{lowpass:g}Hz")
    if notch:
        parts.append(f"N:{notch:g}Hz")
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Header parsing
# ---------------------------------------------------------------------------


def parse_edf_header(data: bytes) -> EdfHeader:
    """Parse the EDF/BDF general header from the first 256 bytes of *data*.

    Raises :class:`EdfParseError` only for fields that make further parsing
    impossible (format detection, record count, record duration, signal count).
    All other field errors are silently absorbed so lenient files still load.
    """
    mv = memoryview(data)
    offset = 0

    # ── Version / format detection ────────────────────────────────────────
    version_raw = _read_field(mv, 0, _FW_VERSION)
    if data[0] == 0xFF and version_raw[1:].strip() == "BIOSEMI":
        data_format = "bdf"
    elif version_raw.strip() == "0":
        data_format = "edf"
    else:
        raise EdfParseError(f"Unrecognised EDF/BDF version field: {version_raw!r}")
    offset += _FW_VERSION

    # ── Patient identification (80 bytes) ────────────────────────────────
    patient_id = _read_field(mv, offset, _FW_PATIENT)
    offset += _FW_PATIENT

    # ── Local recording identification (80 bytes) ─────────────────────────
    local_recording_id = _read_field(mv, offset, _FW_RECORDING)
    offset += _FW_RECORDING

    # ── Start date / time (8 + 8 bytes) ───────────────────────────────────
    recording_date: datetime | None = None
    # non-fatal — recording_date stays None if the header date can't be parsed
    with contextlib.suppress(Exception):
        date_str = _read_field(mv, offset, _FW_STARTDATE)
        time_str = _read_field(mv, offset + _FW_STARTDATE, _FW_STARTTIME)
        d, m, y = date_str.split(".")
        hh, mm, ss = time_str.split(".")
        year = int(y)
        year = (1900 + year) if year >= 85 else (2000 + year)
        recording_date = datetime(
            year,
            int(m),
            int(d),
            int(hh),
            int(mm),
            int(ss),
            tzinfo=timezone.utc,
        )
    offset += _FW_STARTDATE + _FW_STARTTIME

    # ── Header record byte count (8 bytes) ───────────────────────────────
    header_record_bytes = 0
    try:
        header_record_bytes = int(_read_field(mv, offset, _FW_HEADER_BYTES))
    except (ValueError, TypeError):
        pass  # will be estimated later if needed
    offset += _FW_HEADER_BYTES

    # ── Reserved / EDF+ marker (44 bytes) ────────────────────────────────
    reserved = _read_field(mv, offset, _FW_RESERVED)
    fmt_upper = data_format.upper()
    is_plus = reserved.upper().startswith(f"{fmt_upper}+")
    discontinuous = False
    if is_plus:
        data_format += "+"
        marker = reserved.upper()[len(fmt_upper) + 1 : len(fmt_upper) + 2]
        discontinuous = marker == "D"
    offset += _FW_RESERVED

    # ── Number of data records (8 bytes) ──────────────────────────────────
    try:
        data_record_count = int(_read_field(mv, offset, _FW_NRECS))
        if data_record_count == 0:
            raise EdfParseError("data_record_count is 0")
    except EdfParseError:
        raise
    except Exception as exc:
        raise EdfParseError(f"Cannot parse data record count: {exc}") from exc
    offset += _FW_NRECS

    # ── Data record duration (8 bytes, seconds) ───────────────────────────
    try:
        data_record_duration = float(_read_field(mv, offset, _FW_REC_DURATION))
        if data_record_duration == 0:
            raise EdfParseError("data_record_duration is 0")
    except EdfParseError:
        raise
    except Exception as exc:
        raise EdfParseError(f"Cannot parse data record duration: {exc}") from exc
    offset += _FW_REC_DURATION

    # ── Signal count (4 bytes) ─────────────────────────────────────────────
    try:
        signal_count = int(_read_field(mv, offset, _FW_NS))
    except Exception as exc:
        raise EdfParseError(f"Cannot parse signal count: {exc}") from exc
    offset += _FW_NS

    return EdfHeader(
        data_format=data_format,
        patient_id=patient_id,
        local_recording_id=local_recording_id,
        recording_date=recording_date,
        header_record_bytes=header_record_bytes,
        reserved=reserved,
        data_record_count=data_record_count,
        data_record_duration=data_record_duration,
        signal_count=signal_count,
        is_plus=is_plus,
        discontinuous=discontinuous,
    )


# ---------------------------------------------------------------------------
# Signal header parsing
# ---------------------------------------------------------------------------


def parse_signal_infos(data: bytes, header: EdfHeader) -> list[EdfSignalInfo]:
    """Parse the per-channel (signal) sections of the EDF/BDF header.

    Reads starting at byte 256 (after the general header). Each section
    consists of *ns* consecutive fixed-width fields for all channels.

    Returns an empty list (non-fatal) if the buffer is too short.
    """
    ns = header.signal_count
    if ns == 0:
        return []

    mv = memoryview(data)
    base = 256  # signal header starts at byte 256

    def _section(width: int) -> list[str]:
        nonlocal base
        fields = []
        for _ in range(ns):
            fields.append(_read_field(mv, base, width))
            base += width
        return fields

    try:
        labels = _section(_SW_LABEL)
        transducers = _section(_SW_TRANSDUCER)
        phys_units = _section(_SW_PHYS_UNIT)
        phys_mins = _section(_SW_PHYS_MIN)
        phys_maxs = _section(_SW_PHYS_MAX)
        dig_mins = _section(_SW_DIG_MIN)
        dig_maxs = _section(_SW_DIG_MAX)
        prefilterings = _section(_SW_PREFILTERING)
        sample_counts = _section(_SW_SAMPLE_COUNT)
        reserveds = _section(_SW_RESERVED)
    except Exception:
        return []  # buffer too short or corrupt signal section

    base_format = header.data_format.rstrip("+")
    bytes_per_sample = 3 if base_format == "bdf" else 2
    anno_label = f"{base_format} annotations"
    record_byte_size = 0
    result: list[EdfSignalInfo] = []

    for i in range(ns):
        try:
            phys_min = float(phys_mins[i])
            phys_max = float(phys_maxs[i])
            dig_min = int(dig_mins[i])
            dig_max = int(dig_maxs[i])
        except (ValueError, TypeError):
            phys_min = phys_max = 0.0
            dig_min = dig_max = 0

        dig_range = dig_max - dig_min
        units_per_bit = (phys_max - phys_min) / dig_range if dig_range else 0.0
        digital_offset = (phys_max / units_per_bit - dig_max) if units_per_bit else 0.0

        try:
            sample_count = int(sample_counts[i])
        except (ValueError, TypeError):
            sample_count = 0

        label = labels[i]
        is_anno = label.lower() == anno_label

        sampling_rate = (
            sample_count / header.data_record_duration if (not is_anno and header.data_record_duration) else 0.0
        )

        hp, lp, notch = parse_prefiltering(prefilterings[i])
        if is_anno:
            sig_type, canonical = "", ""
        else:
            # classify_channel refines the text-inferred type: a label that
            # resolves to a 10-10 electrode is typed 'eeg' even when the raw
            # label carried no 'EEG' marker (e.g. a bare 'Fp1').
            sig_type, canonical = classify_channel(label, extract_signal_type(label))

        info = EdfSignalInfo(
            label=label,
            transducer_type=transducers[i],
            physical_unit=phys_units[i],
            physical_min=phys_min,
            physical_max=phys_max,
            digital_min=dig_min,
            digital_max=dig_max,
            prefiltering=prefilterings[i],
            sample_count=sample_count,
            reserved=reserveds[i],
            units_per_bit=units_per_bit,
            digital_offset=digital_offset,
            sampling_rate=sampling_rate,
            signal_type=sig_type,
            canonical_label=canonical,
            is_annotation_channel=is_anno,
            highpass=hp,
            lowpass=lp,
            notch=notch,
        )
        result.append(info)
        record_byte_size += sample_count * bytes_per_sample

    header.record_byte_size = record_byte_size
    header.signal_infos = result

    # Validate / estimate header_record_bytes if not parsed correctly.
    expected_hdr_bytes = 256 + ns * (
        _SW_LABEL
        + _SW_TRANSDUCER
        + _SW_PHYS_UNIT
        + _SW_PHYS_MIN
        + _SW_PHYS_MAX
        + _SW_DIG_MIN
        + _SW_DIG_MAX
        + _SW_PREFILTERING
        + _SW_SAMPLE_COUNT
        + _SW_RESERVED
    )
    if header.header_record_bytes != expected_hdr_bytes:
        header.header_record_bytes = expected_hdr_bytes

    return result


# ---------------------------------------------------------------------------
# TAL (annotation) parsing
# ---------------------------------------------------------------------------


def _parse_tal_record(
    buf: bytes | memoryview,
) -> tuple[float | None, list[AnnotationEntry]]:
    """Parse all TALs from a single annotation channel data record buffer.

    Returns ``(record_onset, annotations)`` where *record_onset* is the
    absolute time (seconds from recording start) of this data record as given
    by the mandatory timekeeping TAL.  *annotations* contains all text TALs
    from this record.

    TAL format (EDF+ spec §2.2.4):
        ``+onset[\\x15duration]\\x14label\\x14[more_labels]\\x14\\x00``
    Timekeeping TAL (detected by the consecutive \\x14\\x14 pattern):
        ``+onset\\x14\\x14[\\x00]`` — empty annotation label, marks record start time.
    """
    data = bytes(buf)
    n = len(data)
    record_onset: float | None = None
    annotations: list[AnnotationEntry] = []
    i = 0

    while i < n:
        # Skip null padding between / after TALs.
        if data[i] == _TAL_END:
            break

        # ── Read onset ────────────────────────────────────────────────────
        onset_start = i
        if i < n and data[i] in (ord("+"), ord("-")):
            i += 1
        while i < n and data[i] not in (_TAL_FIELD_SEP, _TAL_DUR_SEP, _TAL_END):
            i += 1
        if i >= n or data[i] == _TAL_END:
            break
        try:
            onset = float(data[onset_start:i].decode("ascii", errors="replace"))
        except ValueError:
            break

        # ── Read optional duration ─────────────────────────────────────────
        duration = 0.0
        if data[i] == _TAL_DUR_SEP:
            i += 1
            dur_start = i
            while i < n and data[i] not in (_TAL_FIELD_SEP, _TAL_END):
                i += 1
            try:
                duration = float(data[dur_start:i].decode("ascii", errors="replace"))
            except ValueError:
                duration = 0.0

        # i now points at the first \x14 of the label section.
        if i >= n or data[i] != _TAL_FIELD_SEP:
            break

        # ── Detect timekeeping TAL ────────────────────────────────────────
        # The timekeeping TAL uses a double \x14 immediately after onset:
        # ``+onset\x14\x14\x00``
        # The first \x14 is the onset/label separator; the second \x14 is
        # the terminator of an empty annotation label.  We look ahead one
        # byte to identify this pattern, matching the approach used in the
        # reference TypeScript decoder.
        if i + 1 < n and data[i + 1] == _TAL_FIELD_SEP:
            if record_onset is None:
                record_onset = onset
            i += 2  # consume both \x14 bytes
            if i < n and data[i] == _TAL_END:
                i += 1
            continue

        # ── Read labels for a regular TAL ─────────────────────────────────
        labels: list[str] = []
        while i < n and data[i] == _TAL_FIELD_SEP:
            i += 1  # consume \x14
            label_start = i
            while i < n and data[i] not in (_TAL_FIELD_SEP, _TAL_END):
                i += 1
            label = data[label_start:i].decode("utf-8", errors="replace")
            if label:
                labels.append(label)

        for label in labels:
            annotations.append(AnnotationEntry(onset=onset, duration=max(0.0, duration), label=label))

        # Skip the null terminator.
        if i < n and data[i] == _TAL_END:
            i += 1

    return record_onset, annotations


def parse_annotations(
    data: bytes,
    header: EdfHeader,
    signal_infos: list[EdfSignalInfo],
) -> tuple[list[AnnotationEntry], GapMap]:
    """Scan every data record for TAL annotations and data gaps.

    Returns ``(annotations, gap_map)`` where *gap_map* is a
    ``{data_position: gap_duration}`` dict. *data_position* is the time offset
    (in seconds) within the uninterrupted data stream where the gap begins;
    *gap_duration* is how many seconds are missing.

    Gap detection is only active for EDF+D / BDF+D (discontinuous) recordings.
    """
    if not signal_infos:
        return [], {}

    base_format = header.data_format.rstrip("+")
    bytes_per_sample = 3 if base_format == "bdf" else 2
    anno_label = f"{base_format} annotations"

    anno_indices = [i for i, s in enumerate(signal_infos) if s.label.lower() == anno_label]
    if not anno_indices:
        return [], {}

    # Pre-compute the byte offset of each channel within a single data record.
    chan_byte_offset: list[int] = []
    cumulative = 0
    for s in signal_infos:
        chan_byte_offset.append(cumulative)
        cumulative += s.sample_count * bytes_per_sample
    record_size = cumulative

    # Determine number of records; handle -1 (recording still in progress).
    n_records = header.data_record_count
    if n_records < 0:
        remaining = len(data) - header.header_record_bytes
        n_records = remaining // record_size if record_size else 0

    mv = memoryview(data)
    all_annotations: list[AnnotationEntry] = []
    gap_map: GapMap = {}
    prior_offset = 0.0  # cumulative gap time accumulated so far

    for r in range(n_records):
        expected_start = r * header.data_record_duration + prior_offset
        record_onset: float | None = None
        record_annotations: list[AnnotationEntry] = []
        gap_recorded_this_record = False

        for i in anno_indices:
            chan_start = header.header_record_bytes + r * record_size + chan_byte_offset[i]
            chan_len = signal_infos[i].sample_count * bytes_per_sample
            if chan_start + chan_len > len(data):
                break  # truncated file — stop gracefully
            onset, chan_annos = _parse_tal_record(mv[chan_start : chan_start + chan_len])

            if onset is not None and record_onset is None:
                record_onset = onset

            record_annotations.extend(chan_annos)

        # Gap detection — only once per record (fix for TS double-count bug).
        if header.discontinuous and record_onset is not None and not gap_recorded_this_record:
            diff = record_onset - expected_start
            if diff > 1e-9 and not _floats_nearly_equal(record_onset, expected_start):
                # data_position = position in data time (gaps excluded)
                data_pos = r * header.data_record_duration
                gap_map[data_pos] = diff
                prior_offset += diff
                gap_recorded_this_record = True
            elif diff < -1e-9 and not _floats_nearly_equal(record_onset, expected_start):
                # Overlapping record starts — file may be corrupt; skip silently.
                pass

        all_annotations.extend(record_annotations)

    return all_annotations, gap_map


# ---------------------------------------------------------------------------
# Record timeline (seek-based) — where a discontinuous recording was spliced
# ---------------------------------------------------------------------------

#: Two record onsets closer than this to their nominal spacing count as contiguous.
#: EDF writes onsets as ASCII decimals, so exact equality is not available; this is
#: loose enough to absorb that rounding and far tighter than any real acquisition gap.
GAP_TOLERANCE_S = 1e-6


def read_record_gaps(
    path: Path,
    header: EdfHeader,
    signal_infos: Sequence[EdfSignalInfo],
    *,
    first_record: int = 0,
    last_record: int | None = None,
) -> list[tuple[int, float]]:
    """Return ``(record_index, extra_seconds)`` for every gap in *path*'s own timeline.

    Every data record of an EDF+/BDF+ file opens its annotation signal with a
    mandatory *timekeeping* TAL giving that record's wall-clock onset. In a continuous
    file those onsets advance by exactly one record duration; a jump means acquisition
    paused, so from that record on, sample position and clock time diverge.

    *record_index* is the record whose onset jumped — the splice sits immediately
    **before** it, at data position ``record_index * header.data_record_duration``.
    That is deliberately the same convention :data:`GapMap`'s keys use, so an index
    reported here names the same instant the ingest gap map and the persisted
    ``annotations.Interruption`` rows do. *extra_seconds* is how much wall-clock time
    the jump added; it is negative for a record that starts *before* its predecessor
    finished, which means a corrupt file rather than a pause — reported rather than
    swallowed, because the samples either side are not contiguous either way.

    Unlike :func:`parse_annotations` this reads the file by **seeking**: only the
    annotation channel's bytes of the records it needs, so the cost is proportional to
    the range asked for rather than to the recording's size and the file never has to
    be resident. That is what makes it usable from a signal loader that only wants to
    know about the splices near one window. The trade is that it sees the timekeeping
    TALs and nothing else — when the annotation *text* is wanted, use
    :func:`parse_annotations`.

    Restricting the range stays exact rather than becoming approximate because a gap is
    detected from the difference between two **consecutive** onsets: no state
    accumulates across records, so record *r*'s verdict depends on nothing earlier than
    ``r - 1``. ``first_record`` and ``last_record`` bound the returned **gap** indices
    inclusively, and one record before ``first_record`` is read to compare against.

    The header's own EDF+D marker is deliberately **not** consulted: a caller that
    wants to trust the marker can check ``header.discontinuous`` first and skip this
    call entirely, and one that does not can find out what the records actually say.

    Returns an empty list when the file carries no annotation channel or holds fewer
    than two records — there is then no timeline to compare against, which for a plain
    (non-plus) EDF is the correct answer rather than a failure. Raises
    :class:`EdfParseError` when an annotation channel *is* present but its timekeeping
    TAL cannot be read, because a caller relying on this to place splices must not
    mistake "unreadable" for "continuous".
    """
    bytes_per_sample = 3 if header.data_format.rstrip("+") == "bdf" else 2
    record_size = header.record_byte_size or sum(info.sample_count * bytes_per_sample for info in signal_infos)
    n_records = header.data_record_count
    if n_records < 0 and record_size:
        # A file still being written declares -1 records; recover the count from its
        # size on disk, the same way :func:`parse_annotations` recovers it from length.
        try:
            n_records = max(0, (path.stat().st_size - header.header_record_bytes) // record_size)
        except OSError:
            n_records = 0

    anno = next((i for i, info in enumerate(signal_infos) if info.is_annotation_channel), None)
    if anno is None or n_records < 2 or not record_size:
        return []

    byte_offset = sum(info.sample_count * bytes_per_sample for info in signal_infos[:anno])
    width = signal_infos[anno].sample_count * bytes_per_sample
    if width <= 0:
        return []

    stop = n_records - 1 if last_record is None else min(int(last_record), n_records - 1)
    start = max(1, int(first_record))
    if stop < start:
        return []

    onsets: dict[int, float] = {}
    with path.open("rb") as handle:
        for record in range(start - 1, stop + 1):
            handle.seek(header.header_record_bytes + record * record_size + byte_offset)
            blob = handle.read(width)
            if len(blob) < width:
                raise EdfParseError(
                    f"{path.name}: data record {record} is truncated "
                    f"({len(blob)} of {width} annotation bytes), so the recording's "
                    "timeline cannot be reconstructed."
                )
            text = blob.split(b"\x00")[0].decode("latin-1")
            try:
                onsets[record] = float(text.split("\x14")[0])
            except ValueError as exc:
                raise EdfParseError(
                    f"{path.name}: data record {record} has no readable timekeeping "
                    f"TAL (got {text[:32]!r}), so the recording's timeline cannot be "
                    "reconstructed."
                ) from exc

    gaps: list[tuple[int, float]] = []
    for record in range(start, stop + 1):
        extra = (onsets[record] - onsets[record - 1]) - header.data_record_duration
        if abs(extra) > GAP_TOLERANCE_S:
            gaps.append((record, extra))
    return gaps


def read_splice_positions(
    path: Path,
    header: EdfHeader,
    signal_infos: Sequence[EdfSignalInfo],
    *,
    first_record: int = 0,
    last_record: int | None = None,
) -> list[float]:
    """The **data positions** (seconds) of every splice in *path*'s timeline.

    :func:`read_record_gaps` expressed on the platform's canonical timeline and ready
    to hand to :mod:`compute.segmentation`: a gap before record *r* is a zero-width
    seam at ``r * header.data_record_duration``. In data position a gap is not an
    interval — no time passes at a splice, the samples either side simply were not
    recorded together.
    """
    return [
        record * header.data_record_duration
        for record, _extra in read_record_gaps(
            path,
            header,
            signal_infos,
            first_record=first_record,
            last_record=last_record,
        )
    ]


# ---------------------------------------------------------------------------
# TAL encoding (counterpart to the TAL parsing functions above)
# ---------------------------------------------------------------------------


def _encode_timekeeping_tal(onset: float) -> bytes:
    """Encode the mandatory timekeeping TAL for one data record.

    Format: ``+onset\\x14\\x14\\x00``
    """
    return f"+{onset:.10g}".encode("ascii") + bytes([_TAL_FIELD_SEP, _TAL_FIELD_SEP, _TAL_END])


def _encode_tal_entry(onset: float, duration: float, label: str) -> bytes:
    """Encode a single text TAL annotation entry.

    Format: ``+onset[\\x15duration]\\x14label\\x14\\x00``
    """
    buf = f"+{onset:.10g}".encode("ascii")
    if duration:
        buf += bytes([_TAL_DUR_SEP]) + f"{duration:.10g}".encode("ascii")
    buf += bytes([_TAL_FIELD_SEP])
    buf += label.encode("utf-8")
    buf += bytes([_TAL_FIELD_SEP, _TAL_END])
    return buf


def _encode_tal_record(
    record_onset: float,
    annotations: list[AnnotationEntry],
    channel_bytes: int,
    strip_text: bool = False,
) -> bytes:
    """Encode a complete TAL annotation channel for one data record.

    Writes the mandatory timekeeping TAL followed by all *annotations*, then
    pads with null bytes to fill *channel_bytes*.  If *strip_text* is True,
    only the timekeeping TAL is written; text annotations are omitted.

    Raises :class:`ValueError` if the encoded content exceeds *channel_bytes*.
    """
    parts: list[bytes] = [_encode_timekeeping_tal(record_onset)]
    if not strip_text:
        for anno in annotations:
            parts.append(_encode_tal_entry(anno.onset, anno.duration, anno.label))
    raw = b"".join(parts)
    if len(raw) > channel_bytes:
        raise ValueError(
            f"TAL content ({len(raw)} bytes) exceeds annotation channel capacity "
            f"({channel_bytes} bytes) for record at onset {record_onset}"
        )
    return raw + bytes(channel_bytes - len(raw))


# ---------------------------------------------------------------------------
# Record normalisation (restructure to 1-second data records)
# ---------------------------------------------------------------------------


def _compute_record_onsets(n_records: int, record_duration: float, gap_map: GapMap) -> list[float]:
    """Return the wall-clock onset (seconds) for each data record.

    For continuous recordings the onset of record *r* is simply
    ``r * record_duration``.  For discontinuous recordings (EDF+D) each gap
    in *gap_map* adds to the cumulative offset so the onset reflects the
    actual clock time at which that record was acquired.
    """
    onsets: list[float] = []
    cumulative_gap = 0.0
    for r in range(n_records):
        data_pos = r * record_duration
        cumulative_gap += gap_map.get(data_pos, 0.0)
        onsets.append(data_pos + cumulative_gap)
    return onsets


def normalise_edf_records(
    path: Path,
    data: bytes,
    header: EdfHeader,
    signal_infos: list[EdfSignalInfo],
    annotations: list[AnnotationEntry],
    gaps: GapMap,
    strip_annotation_text: bool = False,
) -> bool:
    """Restructure an EDF/BDF file to use 1-second data records.

    Reads *data* (the raw file bytes already held in memory), rewrites *path*
    with a new record layout, and mutates *header* and *signal_infos* in place
    so the caller's :class:`EdfProcessingResult` reflects the normalised state.

    **Splitting** (record duration > 1 s):
        Each N-second record is split into N independent 1-second records.
        TAL onsets in the annotation channel are recomputed so each output
        record carries the correct wall-clock onset, and text annotations are
        re-bucketed into the output record whose onset window they fall within.

    **Merging** (record duration < 1 s):
        M consecutive sub-second records are merged into one 1-second record.
        Skipped when the file is EDF+D and contains gaps, because a gap
        boundary could fall mid-way through a would-be merged record, making
        the output TAL timestamps ambiguous.

    **Skip conditions** (returns ``False``, file left unchanged):

    - Record duration is already 1.0 s.
    - Any non-annotation channel has a non-integer sampling rate (e.g. a
      0.2 Hz trend channel that cannot be evenly partitioned into 1-second
      records).
    - For splitting: record duration is not an integer number of seconds.
    - For merging: 1 / record_duration is not an integer; the record count is
      not divisible by the merge factor; or the file is EDF+D with gaps.

    When restructuring occurs the rewritten file always includes a fully
    de-identified clean header (equivalent to :func:`rewrite_edf_header`).
    If *strip_annotation_text* is ``True``, text TALs are omitted from the
    output; only timekeeping TALs are written.

    Returns ``True`` if the file was rewritten, ``False`` otherwise.
    """
    D = header.data_record_duration
    N = header.data_record_count

    if D == 1.0:
        return False

    base_format = header.data_format.rstrip("+")
    bytes_per_sample = 3 if base_format == "bdf" else 2

    # ── Validate: all non-annotation channels must have integer sampling rates ──
    for s in signal_infos:
        if s.is_annotation_channel:
            continue
        rate = s.sample_count / D
        if rate <= 0 or abs(rate - round(rate)) > 1e-9:
            return False

    # ── Determine split vs. merge and validate factor ────────────────────────
    is_splitting = D > 1.0
    if is_splitting:
        if abs(D - round(D)) > 1e-9:
            return False  # non-integer record duration
        factor = round(D)  # each original record → factor output records
        new_N = N * factor
    else:
        inv_D = 1.0 / D
        if abs(inv_D - round(inv_D)) > 1e-9:
            return False  # non-integer merge factor
        factor = round(inv_D)  # factor original records → 1 output record
        if N % factor != 0:
            return False
        if header.discontinuous and gaps:
            return False  # gap boundaries might fall mid-merged-record
        new_N = N // factor

    # ── Original channel byte layout ─────────────────────────────────────────
    orig_sample_counts = [s.sample_count for s in signal_infos]
    orig_chan_bytes = [sc * bytes_per_sample for sc in orig_sample_counts]
    orig_chan_offsets = list(accumulate(orig_chan_bytes, initial=0))
    orig_record_size = orig_chan_offsets[-1]
    orig_header_size = header.header_record_bytes  # captured before mutation

    # ── Compute new sample counts (annotation channels resolved later) ────────
    new_sample_counts_pre: list[int] = []
    for s in signal_infos:
        if s.is_annotation_channel:
            new_sample_counts_pre.append(-1)  # placeholder
        elif is_splitting:
            new_sample_counts_pre.append(round(s.sample_count / factor))
        else:
            new_sample_counts_pre.append(s.sample_count * factor)

    # ── Wall-clock onsets and per-output-record annotation assignment ─────────
    original_onsets = _compute_record_onsets(N, D, gaps)

    output_onsets: list[float] = []
    if is_splitting:
        for orig_r in range(N):
            for sub_j in range(factor):
                output_onsets.append(original_onsets[orig_r] + sub_j)
    else:
        for k in range(new_N):
            output_onsets.append(original_onsets[k * factor])

    # Each annotation goes into the output record with the largest onset ≤ anno.onset.
    output_anno_map: dict[int, list[AnnotationEntry]] = {}
    for anno in annotations:
        idx = bisect.bisect_right(output_onsets, anno.onset) - 1
        if 0 <= idx < new_N:
            output_anno_map.setdefault(idx, []).append(anno)

    # ── Annotation channel size: tight fit over all output records ────────────
    has_anno = any(s.is_annotation_channel for s in signal_infos)
    anno_sample_count = 0
    if has_anno:
        max_tal_bytes = 0
        for out_r in range(new_N):
            needed = len(_encode_timekeeping_tal(output_onsets[out_r]))
            if not strip_annotation_text:
                for anno in output_anno_map.get(out_r, []):
                    needed += len(_encode_tal_entry(anno.onset, anno.duration, anno.label))
            max_tal_bytes = max(max_tal_bytes, needed)
        max_tal_bytes = max(max_tal_bytes, 20)  # minimum headroom
        anno_sample_count = (max_tal_bytes + bytes_per_sample - 1) // bytes_per_sample + 1

    # ── Finalise new sample counts ────────────────────────────────────────────
    new_sample_counts = [anno_sample_count if v == -1 else v for v in new_sample_counts_pre]
    new_chan_bytes = [sc * bytes_per_sample for sc in new_sample_counts]
    new_chan_offsets = list(accumulate(new_chan_bytes, initial=0))
    new_record_size = new_chan_offsets[-1]

    # ── Mutate header and signal_infos to reflect the new structure ───────────
    header.data_record_count = new_N
    header.data_record_duration = 1.0
    header.record_byte_size = new_record_size
    for i, s in enumerate(signal_infos):
        s.sample_count = new_sample_counts[i]
        if not s.is_annotation_channel:
            s.sampling_rate = float(s.sample_count)  # samples per 1-second record

    # ── Build new clean header ────────────────────────────────────────────────
    new_header_bytes = _build_clean_header(header, signal_infos)

    # ── Build data records ────────────────────────────────────────────────────
    mv = memoryview(data)
    out_parts: list[bytes] = [new_header_bytes]

    for out_r in range(new_N):
        record = bytearray(new_record_size)
        for i, s in enumerate(signal_infos):
            dst_start = new_chan_offsets[i]
            dst_end = new_chan_offsets[i + 1]

            if s.is_annotation_channel:
                record[dst_start:dst_end] = _encode_tal_record(
                    output_onsets[out_r],
                    output_anno_map.get(out_r, []),
                    new_chan_bytes[i],
                    strip_text=strip_annotation_text,
                )
            elif is_splitting:
                orig_r = out_r // factor
                sub_j = out_r % factor
                src_start = (
                    orig_header_size + orig_r * orig_record_size + orig_chan_offsets[i] + sub_j * new_chan_bytes[i]
                )
                record[dst_start:dst_end] = mv[src_start : src_start + new_chan_bytes[i]]
            else:
                # Merge: concatenate samples from `factor` consecutive original records.
                orig_r_start = out_r * factor
                orig_sc_bytes = orig_sample_counts[i] * bytes_per_sample
                for m in range(factor):
                    src_start = orig_header_size + (orig_r_start + m) * orig_record_size + orig_chan_offsets[i]
                    sub_dst = dst_start + m * orig_sc_bytes
                    record[sub_dst : sub_dst + orig_sc_bytes] = mv[src_start : src_start + orig_sc_bytes]

        out_parts.append(bytes(record))

    path.write_bytes(b"".join(out_parts))
    return True


def _strip_annotation_text_inplace(
    path: Path,
    header: EdfHeader,
    signal_infos: list[EdfSignalInfo],
    gaps: GapMap,
) -> None:
    """Overwrite each annotation channel in-place with timekeeping-only TALs.

    Text annotations are replaced with null-byte padding.  Signal data and the
    header are not touched.  Only meaningful when the file already uses
    1-second records (after :func:`rewrite_edf_header` has run).
    """
    base_format = header.data_format.rstrip("+")
    bytes_per_sample = 3 if base_format == "bdf" else 2

    chan_bytes = [s.sample_count * bytes_per_sample for s in signal_infos]
    chan_offsets = list(accumulate(chan_bytes, initial=0))
    record_size = chan_offsets[-1]

    original_onsets = _compute_record_onsets(header.data_record_count, header.data_record_duration, gaps)

    with path.open("r+b") as fh:
        for r, onset in enumerate(original_onsets):
            for i, s in enumerate(signal_infos):
                if not s.is_annotation_channel:
                    continue
                chan_start = header.header_record_bytes + r * record_size + chan_offsets[i]
                fh.seek(chan_start)
                fh.write(_encode_tal_record(onset, [], chan_bytes[i], strip_text=True))


# ---------------------------------------------------------------------------
# Header rewriting (de-identification + ASCII normalisation)
# ---------------------------------------------------------------------------


def build_header(header: EdfHeader, signal_infos: list[EdfSignalInfo]) -> bytes:
    """Assemble EDF/BDF header bytes, preserving all identification fields.

    Public because building a header without writing a file is a operation in
    its own right: any structural transform that changes the channel set —
    channel dropping, downsampling, reordering — has to re-emit the header, and
    both the federation middleware and the FUSE filesystem do exactly that.

    Unlike :func:`_build_clean_header`, patient and recording fields are kept
    verbatim from *header*. That is the whole distinction between the two, and
    the reason only this one is public: this function is a serializer and makes
    no claim about the content it serializes, so a caller reaching for it must
    combine it with a separate anonymisation step. The de-identifying variant
    stays private because its hardcoded blanking values are the platform's PHI
    contract rather than a parameter — see the module docstring.

    The header byte count field is recomputed from ``len(signal_infos)`` so
    the output is structurally valid even when the channel count changes.
    """
    base_format = header.data_format.rstrip("+")
    is_bdf = base_format == "bdf"

    if is_bdf:
        version_bytes = bytes([0xFF]) + b"BIOSEMI"
    else:
        version_bytes = b"0       "

    patient_bytes = _pad(header.patient_id, _FW_PATIENT)
    recording_bytes = _pad(header.local_recording_id, _FW_RECORDING)

    if header.recording_date is not None:
        d = header.recording_date
        startdate_bytes = f"{d.day:02d}.{d.month:02d}.{str(d.year)[2:]}".encode("ascii")
        starttime_bytes = f"{d.hour:02d}.{d.minute:02d}.{d.second:02d}".encode("ascii")
    else:
        startdate_bytes = b"01.01.85"
        starttime_bytes = b"00.00.00"

    ns = len(signal_infos)
    hdr_bytes_value = 256 + ns * (
        _SW_LABEL
        + _SW_TRANSDUCER
        + _SW_PHYS_UNIT
        + _SW_PHYS_MIN
        + _SW_PHYS_MAX
        + _SW_DIG_MIN
        + _SW_DIG_MAX
        + _SW_PREFILTERING
        + _SW_SAMPLE_COUNT
        + _SW_RESERVED
    )
    hdr_bytes_bytes = _pad(str(hdr_bytes_value), _FW_HEADER_BYTES)
    reserved_bytes = _pad(header.reserved, _FW_RESERVED)
    nrecs_bytes = _pad(str(header.data_record_count), _FW_NRECS)
    dur_bytes = _pad(str(header.data_record_duration), _FW_REC_DURATION)
    ns_bytes = _pad(str(ns), _FW_NS)

    general = (
        version_bytes
        + patient_bytes
        + recording_bytes
        + startdate_bytes
        + starttime_bytes
        + hdr_bytes_bytes
        + reserved_bytes
        + nrecs_bytes
        + dur_bytes
        + ns_bytes
    )
    assert len(general) == 256, f"General header length mismatch: {len(general)}"

    def _sig_section(values: list[str], width: int) -> bytes:
        return b"".join(_pad(v, width) for v in values)

    sections = (
        _sig_section([s.label for s in signal_infos], _SW_LABEL)
        + _sig_section([s.transducer_type for s in signal_infos], _SW_TRANSDUCER)
        + _sig_section([s.physical_unit for s in signal_infos], _SW_PHYS_UNIT)
        + _sig_section([str(s.physical_min) for s in signal_infos], _SW_PHYS_MIN)
        + _sig_section([str(s.physical_max) for s in signal_infos], _SW_PHYS_MAX)
        + _sig_section([str(s.digital_min) for s in signal_infos], _SW_DIG_MIN)
        + _sig_section([str(s.digital_max) for s in signal_infos], _SW_DIG_MAX)
        + _sig_section([s.prefiltering for s in signal_infos], _SW_PREFILTERING)
        + _sig_section([str(s.sample_count) for s in signal_infos], _SW_SAMPLE_COUNT)
        + _sig_section([s.reserved for s in signal_infos], _SW_RESERVED)
    )

    return general + sections


def deidentify_signal_infos(signal_infos: list[EdfSignalInfo]) -> None:
    """Rewrite the site-fingerprinting per-signal fields in place, capturing originals.

    The channel-block counterpart of the subject de-identification in
    :func:`_build_clean_header`: channel labels, transducer strings, and
    prefiltering strings carry acquisition-site conventions (naming habits,
    device model strings, vendor formatting) that fingerprint the originating
    institution. Applied at ingest before the header rewrite, so the stored file
    — the artifact every serving path reads — never carries them. Full rationale
    and threat model in docs/engineering-notes/channel-deidentification-plan.md.

    Per signal:

    - ``label`` → the already-computed ``canonical_label`` when the channel
      resolved, else ``MISC_<n>`` (n counts unresolved channels in channel order;
      the underscore keeps generated names from embedding electrode tokens the
      way ``MISC3`` embeds ``C3``, which substring-matching consumers pick up).
      Fail-closed on purpose: unresolvable labels — DC inputs, vendor aux
      channels, trigger lines — are precisely the strongest site fingerprint, so
      the raw label is never kept. Annotation channels keep their spec-mandated
      label untouched.
    - ``transducer_type`` → ``''``.
    - ``prefiltering`` → :func:`format_prefiltering` over the parsed filter
      values. Vendor formatting the parser did not recognise is dropped with it
      (fail-closed; the author-private capture keeps it recoverable).

    Reference stripping can make distinct source channels collide on one
    canonical name (``Fp1-A1`` + ``Fp1-A2`` → ``Fp1``, a mixed-reference export).
    Colliding EEG channels fall back to the reference-preserving form
    (:func:`~recordings.processors.channel_labels.canonicalise_label_keep_reference`),
    keeping labels unique and the reference distinction intact; a channel still
    duplicated after that keeps its first occurrence and demotes the rest to
    ``MISC_<n>``. Non-EEG duplicates (two ``ECG`` channels) are left as-is —
    duplicate labels there match vendor reality and carry no montage identity.

    Originals land in the ``source_*`` attributes before being overwritten.
    Idempotent: canonical labels resolve to themselves, kept-reference forms
    re-collide to themselves, and ``MISC_<n>`` numbering is positional, so a
    second pass reproduces the same values.
    """
    from recordings.processors.channel_labels import canonicalise_label_keep_reference

    for info in signal_infos:
        info.source_label = info.label
        info.source_transducer_type = info.transducer_type
        info.source_prefiltering = info.prefiltering
        info.transducer_type = ""
        info.prefiltering = format_prefiltering(info.highpass, info.lowpass, info.notch)

    # Tentative labels: canonical where resolved, None where a MISC name is needed.
    cleaned: list[str | None] = [
        (info.canonical_label or None) if not info.is_annotation_channel else info.label for info in signal_infos
    ]

    # Collision fallback for EEG channels whose canonical names clash.
    counts = Counter(label for label in cleaned if label)
    for i, info in enumerate(signal_infos):
        if info.is_annotation_channel or not cleaned[i] or counts[cleaned[i]] < 2 or info.signal_type != "eeg":
            continue
        with_reference = canonicalise_label_keep_reference(info.source_label, info.signal_type)
        if with_reference:
            cleaned[i] = with_reference

    # Uniqueness sweep: an EEG label still duplicated (true duplicate channels in
    # the source) keeps its first occurrence; the rest get MISC names.
    seen: set[str] = set()
    for i, info in enumerate(signal_infos):
        if info.is_annotation_channel or not cleaned[i]:
            continue
        if info.signal_type == "eeg":
            if cleaned[i] in seen:
                cleaned[i] = None
            else:
                seen.add(cleaned[i])

    misc_counter = 0
    for info, label in zip(signal_infos, cleaned):
        if info.is_annotation_channel:
            continue
        if label is None:
            misc_counter += 1
            info.label = f"MISC_{misc_counter}"
        else:
            info.label = label


def compute_channel_order(signal_infos: list[EdfSignalInfo]) -> list[int]:
    """Return the original channel indices arranged in canonical order.

    The order spec (versioned as ``CHANNEL_ORDER_VERSION`` in
    ``processors.channel_labels``): EEG first, in the fixed homologous-pair
    sequence keyed by ``eeg_order_rank``; then EOG, EMG, EKG; then everything
    else (aux, trigger, unresolved, derived copies) in original relative order;
    annotation channels last. Groups without an internal spec keep their
    original relative order — the sort is stable by original index, so the
    result is deterministic for any input.
    """
    from recordings.processors.channel_labels import eeg_order_rank

    group_by_type = {"eeg": 0, "eog": 1, "emg": 2, "ekg": 3}

    def key(idx: int) -> tuple:
        info = signal_infos[idx]
        if info.is_annotation_channel:
            return (5, (0, 0), idx)
        group = group_by_type.get(info.signal_type, 4)
        sub = eeg_order_rank(info.label) if group == 0 else (0, 0)
        return (group, sub, idx)

    return sorted(range(len(signal_infos)), key=key)


def reorder_edf_channels(path: Path, header: EdfHeader, signal_infos: list[EdfSignalInfo]) -> list[int] | None:
    """Rewrite *path* so its channels appear in canonical order.

    Runs as the final ingest pass, after de-identification and any record
    normalisation, so the input header is already the clean layout. Streams the
    file record by record — each data record is permuted locally by slicing the
    per-channel blocks (``sample_count × sample_width`` bytes each, 3-byte
    samples for BDF) and concatenating them in canonical order — so memory
    stays bounded regardless of file size and sample bytes move verbatim
    (bit-exact, including annotation TALs).

    Mutates *signal_infos* into the on-disk order, records each channel's
    original file position in ``source_index`` first, and rewrites the header
    to match. Returns the applied permutation (new position → original index),
    or ``None`` when the file is already canonical (``source_index`` is still
    captured; it then equals the position).

    Raises :class:`EdfParseError` when a data record reads short — the record
    count and file length disagree, and permuting a truncated tail would
    corrupt data silently. A negative record count (EDF's ``-1`` "unknown"
    convention from streaming recorders) is resolved against the file size —
    silently trusting it would skip the record loop entirely and write a
    reordered header over unpermuted data, mislabelling every channel. The
    resolved count is also written back to *header* so the rewritten header
    and downstream metadata carry the true value.
    """
    for idx, info in enumerate(signal_infos):
        info.source_index = idx

    order = compute_channel_order(signal_infos)
    if order == list(range(len(signal_infos))):
        return None

    base_format = header.data_format.rstrip("+")
    bytes_per_sample = 3 if base_format == "bdf" else 2
    offsets: list[tuple[int, int]] = []
    cumulative = 0
    for info in signal_infos:
        length = info.sample_count * bytes_per_sample
        offsets.append((cumulative, length))
        cumulative += length
    record_size = cumulative
    data_start = header.header_record_bytes

    if header.data_record_count < 0:
        data_length = path.stat().st_size - data_start
        if record_size <= 0 or data_length < 0 or data_length % record_size:
            raise EdfParseError(
                f"Cannot resolve unknown record count: {data_length} data bytes is not a "
                f"whole number of {record_size}-byte records."
            )
        header.data_record_count = data_length // record_size

    with path.open("r+b") as fh:
        for rec in range(header.data_record_count):
            position = data_start + rec * record_size
            fh.seek(position)
            record = fh.read(record_size)
            if len(record) != record_size:
                raise EdfParseError(
                    f"Data record {rec} reads {len(record)} bytes, expected {record_size}; "
                    "record count and file length disagree — refusing to reorder."
                )
            permuted = b"".join(record[start : start + length] for start, length in (offsets[j] for j in order))
            fh.seek(position)
            fh.write(permuted)

        signal_infos[:] = [signal_infos[j] for j in order]
        fh.seek(0)
        fh.write(_build_clean_header(header, signal_infos))

    return order


def _build_clean_header(header: EdfHeader, signal_infos: list[EdfSignalInfo]) -> bytes:
    """Assemble a strictly EDF+/BDF+ compliant, de-identified header bytestring.

    De-identification rules (EDF+ spec §2.1.3.1 / BDF equivalent):
    - Patient field  → ``"X X X X"`` (code sex birthdate name all unknown).
    - Recording field → ``"Startdate X X X X"`` (all admin fields unknown).
    - Start date     → ``"01.01.85"`` (EDF anonymisation convention date).
    - Start time     → ``"00.00.00"``.

    All other text fields are ASCII-cleaned (non-ASCII chars replaced or
    dropped) and truncated / space-padded to their spec-mandated width.
    The binary version byte for BDF (0xFF) is preserved unchanged.
    """
    base_format = header.data_format.rstrip("+")
    is_bdf = base_format == "bdf"

    # ── Version (8 bytes) — preserve binary marker for BDF ───────────────
    if is_bdf:
        version_bytes = bytes([0xFF]) + b"BIOSEMI"
    else:
        version_bytes = b"0       "

    # ── De-identified fixed fields ────────────────────────────────────────
    patient_bytes = _pad("X X X X", _FW_PATIENT)
    recording_bytes = _pad("Startdate X X X X", _FW_RECORDING)
    startdate_bytes = b"01.01.85"
    starttime_bytes = b"00.00.00"

    # ── Header byte count ─────────────────────────────────────────────────
    ns = len(signal_infos)
    hdr_bytes_value = 256 + ns * (
        _SW_LABEL
        + _SW_TRANSDUCER
        + _SW_PHYS_UNIT
        + _SW_PHYS_MIN
        + _SW_PHYS_MAX
        + _SW_DIG_MIN
        + _SW_DIG_MAX
        + _SW_PREFILTERING
        + _SW_SAMPLE_COUNT
        + _SW_RESERVED
    )
    hdr_bytes_bytes = _pad(str(hdr_bytes_value), _FW_HEADER_BYTES)

    # ── Reserved / EDF+ marker ────────────────────────────────────────────
    fmt_upper = base_format.upper()
    continuity = "D" if header.discontinuous else "C"
    reserved_str = f"{fmt_upper}+{continuity}"
    reserved_bytes = _pad(reserved_str, _FW_RESERVED)

    # ── Record count, duration, signal count ──────────────────────────────
    nrecs_bytes = _pad(str(header.data_record_count), _FW_NRECS)
    dur_bytes = _pad(str(header.data_record_duration), _FW_REC_DURATION)
    ns_bytes = _pad(str(ns), _FW_NS)

    general = (
        version_bytes
        + patient_bytes
        + recording_bytes
        + startdate_bytes
        + starttime_bytes
        + hdr_bytes_bytes
        + reserved_bytes
        + nrecs_bytes
        + dur_bytes
        + ns_bytes
    )
    assert len(general) == 256, f"General header length mismatch: {len(general)}"

    # ── Signal header sections ────────────────────────────────────────────
    def _sig_section(values: list[str], width: int) -> bytes:
        return b"".join(_pad(v, width) for v in values)

    sections = (
        _sig_section([_ascii_clean(s.label, _SW_LABEL) for s in signal_infos], _SW_LABEL)
        + _sig_section(
            [_ascii_clean(s.transducer_type, _SW_TRANSDUCER) for s in signal_infos],
            _SW_TRANSDUCER,
        )
        + _sig_section(
            [_ascii_clean(s.physical_unit, _SW_PHYS_UNIT) for s in signal_infos],
            _SW_PHYS_UNIT,
        )
        + _sig_section([str(s.physical_min) for s in signal_infos], _SW_PHYS_MIN)
        + _sig_section([str(s.physical_max) for s in signal_infos], _SW_PHYS_MAX)
        + _sig_section([str(s.digital_min) for s in signal_infos], _SW_DIG_MIN)
        + _sig_section([str(s.digital_max) for s in signal_infos], _SW_DIG_MAX)
        + _sig_section(
            [_ascii_clean(s.prefiltering, _SW_PREFILTERING) for s in signal_infos],
            _SW_PREFILTERING,
        )
        + _sig_section([str(s.sample_count) for s in signal_infos], _SW_SAMPLE_COUNT)
        + _sig_section([_ascii_clean(s.reserved, _SW_RESERVED) for s in signal_infos], _SW_RESERVED)
    )

    return general + sections


def rewrite_edf_header(
    path: Path,
    header: EdfHeader,
    signal_infos: list[EdfSignalInfo],
) -> None:
    """Overwrite the header portion of the EDF/BDF file at *path* in-place.

    Only the header bytes are touched; the data records (signal samples and
    annotation TALs) are left completely unchanged.
    """
    clean_header = _build_clean_header(header, signal_infos)
    with path.open("r+b") as fh:
        fh.seek(0)
        fh.write(clean_header)


# ---------------------------------------------------------------------------
# EDF writing (public API)
# ---------------------------------------------------------------------------
#
# This writer is for sources whose samples are genuinely floating point (e.g.
# a CSV converter). EDF→EDF transforms (normalise_edf_records, rewrite_edf_
# header) deliberately do NOT route through here: they copy the original
# integer sample bytes verbatim, so an uploaded EDF is never round-tripped
# through float and never accrues a second quantisation error.


@dataclass
class EdfChannel:
    """A signal channel handed to :func:`write_edf`.

    ``samples`` holds physical-unit float values sampled uniformly at an
    integer ``sampling_rate`` (samples per second). ``physical_unit`` is the
    EDF physical dimension string (e.g. ``"uV"``, ``"m/s2"``); it and
    ``label`` are ASCII-cleaned and width-clipped by the header builder.
    """

    label: str
    physical_unit: str
    sampling_rate: float
    samples: Sequence[float]


def _fit_physical_bound(value: float, *, round_up: bool) -> float:
    """Round *value* outward to a number whose ``str()`` fits the 8-char EDF
    physical-min / physical-max field.

    The minimum rounds toward negative infinity and the maximum toward
    positive infinity, so the stored bound always encloses the real data and
    no sample clips when the affine digital↔physical map is inverted on read.
    """
    import math

    for decimals in range(6, -1, -1):
        factor = 10**decimals
        rounded = math.ceil(value * factor) if round_up else math.floor(value * factor)
        bound = rounded / factor
        if len(str(bound)) <= _SW_PHYS_MIN:
            return bound
    return float(math.ceil(value) if round_up else math.floor(value))


def write_edf(
    path: Path,
    channels: Sequence[EdfChannel],
    *,
    data_format: str = "edf",
) -> None:
    """Write float channel data to a de-identified EDF file at *path*.

    Each channel is stored at its own integer sampling rate in 1-second data
    records over the full 16-bit digital range. Per-channel physical min/max
    are derived from the data and rounded outward to the EDF field width, so a
    channel carrying a large DC offset — e.g. the gravity-loaded accelerometer
    axis — keeps full resolution across its actual span instead of wasting
    range on a symmetric scale. The header is emitted through
    :func:`_build_clean_header`, so the output is de-identified and parses back
    through :func:`parse_edf_header` identically to a platform-rewritten EDF.

    Trailing samples shorter than one whole record are dropped (EDF records
    must be equal length), losing at most one second minus one sample.
    """
    import numpy as np

    if not channels:
        raise ValueError("write_edf requires at least one channel")

    rates: list[int] = []
    arrays: list = []
    for ch in channels:
        rate = round(ch.sampling_rate)
        if rate <= 0 or abs(ch.sampling_rate - rate) > 1e-6:
            raise ValueError(f"Channel {ch.label!r} needs an integer sampling rate in Hz, got {ch.sampling_rate}.")
        arr = np.asarray(ch.samples, dtype=np.float64)
        arr = np.where(np.isfinite(arr), arr, 0.0)
        rates.append(rate)
        arrays.append(arr)

    n_records = min(len(arr) // rate for arr, rate in zip(arrays, rates))
    if n_records <= 0:
        raise ValueError("write_edf: every channel needs at least one full second of samples.")

    signal_infos: list[EdfSignalInfo] = []
    digital: list = []
    for ch, rate, arr in zip(channels, rates, arrays):
        usable = arr[: n_records * rate]
        pmin = _fit_physical_bound(float(usable.min()), round_up=False)
        pmax = _fit_physical_bound(float(usable.max()), round_up=True)
        if pmax <= pmin:
            # Constant channel: open a unit span so the affine map stays finite.
            pmax = _fit_physical_bound(pmin + 1.0, round_up=True)
        scale = 65535.0 / (pmax - pmin)
        dig = np.rint((usable - pmin) * scale - 32768.0)
        digital.append(np.clip(dig, -32768, 32767).astype("<i2"))
        signal_infos.append(
            EdfSignalInfo(
                label=ch.label,
                transducer_type="",
                physical_unit=ch.physical_unit,
                physical_min=pmin,
                physical_max=pmax,
                digital_min=-32768,
                digital_max=32767,
                prefiltering="",
                sample_count=rate,
                reserved="",
                sampling_rate=float(rate),
            )
        )

    header = EdfHeader(
        data_format=data_format,
        patient_id="",
        local_recording_id="",
        recording_date=None,
        header_record_bytes=256 + len(channels) * 256,
        reserved="",
        data_record_count=n_records,
        data_record_duration=1.0,
        signal_count=len(channels),
        is_plus=False,
        discontinuous=False,
        record_byte_size=sum(rates) * 2,
        signal_infos=signal_infos,
    )

    parts: list[bytes] = [_build_clean_header(header, signal_infos)]
    for r in range(n_records):
        for dig, rate in zip(digital, rates):
            parts.append(dig[r * rate : (r + 1) * rate].tobytes())

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"".join(parts))


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------


def process_edf_file(
    path: Path,
    strip_annotation_text: bool = False,
) -> EdfProcessingResult:
    """Parse, de-identify, normalise, and rewrite a single EDF or BDF file.

    Steps:
    1. Read the entire file into memory.
    2. Parse the general header and signal info headers.
    3. Scan annotation channels for TAL events and data gaps.
    4. Attempt to normalise to 1-second data records
       (see :func:`normalise_edf_records`).  When normalisation rewrites the
       file, the clean de-identified header is included in the same write.
    5. If the file was *not* restructured, rewrite the header in-place to
       apply de-identification.  When *strip_annotation_text* is ``True`` and
       the file is EDF+/BDF+, overwrite the annotation channels to contain
       only timekeeping TALs (text content removed).
    6. Reorder the channels into the canonical homologous-pair order
       (see :func:`reorder_edf_channels`), capturing each channel's original
       position in ``source_index``.

    After this call *header* and *signal_infos* reflect the state of the
    file on disk (record count / duration / sample counts may have changed if
    normalisation occurred).

    Raises :class:`EdfParseError` on unrecoverable parse failures.
    """
    data = path.read_bytes()

    header = parse_edf_header(data)
    signal_infos = parse_signal_infos(data, header)
    annotations, gaps = parse_annotations(data, header, signal_infos)

    # Channel-block de-identification, after annotation parsing (which reads the
    # raw labels) and before either rewrite branch, so the cleaned values land in
    # the written header on both paths and the returned signal_infos keep
    # describing the file on disk. The raw values survive in source_* for
    # author-private persistence.
    deidentify_signal_infos(signal_infos)

    restructured = normalise_edf_records(path, data, header, signal_infos, annotations, gaps, strip_annotation_text)
    if not restructured:
        rewrite_edf_header(path, header, signal_infos)
        if strip_annotation_text and header.is_plus:
            _strip_annotation_text_inplace(path, header, signal_infos, gaps)

    # Canonical channel order, as the final pass so it operates on the settled
    # record layout from either branch above. Captures source_index and mutates
    # signal_infos into the on-disk order.
    reorder_edf_channels(path, header, signal_infos)

    return EdfProcessingResult(
        header=header,
        signal_infos=signal_infos,
        annotations=annotations,
        gaps=gaps,
    )
