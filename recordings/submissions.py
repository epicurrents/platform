"""Validating submissions to a submission pool: the profile registry, the gate, the spool and who may submit.

⚠️ LOAD-BEARING — the submission gate.
The gate is what keeps a pooled dataset's members indistinguishable by origin, and what keeps
bytes that might identify someone from being written anywhere. Every check in
:func:`validate_file` and :func:`validate_sidecar` is a fingerprint or an identifier the profile
promised is absent: blanked identification fields as the de-identifier writes them, no
annotation channel, the channel set and order, the rate, ranges, length, the forbidden sidecar
keys, the declared hash and the embargo attestation. Dropping one, or letting a failing check repair the file instead of
refusing it, admits the site's or the vendor's signature into the pool with every locally
written test still green. The accept path is the other half: :func:`accept_submission` runs the
gate before any write, so a refused submission writes no row and no file, and a file the server
already holds reaches the spool only through :func:`spool_held_file`'s re-dither. Contract tests are
in ``recordings/tests/test_submissions.py`` (``TestGate`` pins each check, including the blank
fields against the de-identifier's actual output, ``TestPoolEndpoints`` the write-nothing refusal
and the ledger as the audit target, ``TestAcceptSubmission`` the shared path and the held route,
``TestPooledIngest`` the random order and the system author, ``TestPooledIngestTrail`` the unjoinable trail,
``TestUploadAgainstProfile`` the file checks for an upload and a stored file that still passes them).

A pooled dataset cannot trust an arriving file the way an upload trusts its author: the
file was prepared elsewhere against a published profile, and a file that departs from the
profile either fingerprints its origin (a site's channel template, a vendor's sampling
rate) or carries what the profile said to leave behind (header identification, annotation
text). The gate here checks a submission against a registered :class:`IngestProfile` and
answers with a list of violations, and the endpoint that calls it writes nothing, no row
and no file, until the list is empty. Today a file that fails ingest lands as a FAILED
recording until purge, which is the wrong outcome for bytes that might identify someone.

The split is deliberate. The platform owns the checks any pooled dataset needs, the ones
that read the EDF header and the sidecar's shape: identification fields blanked exactly
as the platform's own de-identifier writes them, no annotation channel, the channel set
and order, the sampling rate, the physical and digital ranges, the length on one of the
fixed durations, forbidden sidecar keys refused rather than dropped, the declared hash
equal to the received bytes, and the contributor's attestation of the profile's
embargo. The project owns the profile's values, whatever the sidecar
must say beyond its shape (``validate_sidecar``) and what happens to the sidecar once the
recording exists (``ingest``). A deployment that registers no profile has no submission
path; its upload endpoint and bulk import are untouched.

The registry is inert until a profile is registered from a project's ``apps.py::ready()``.
"""

from __future__ import annotations

import calendar
import hashlib
import json
import logging
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from django.conf import settings

logger = logging.getLogger(__name__)

# What ``recordings.processors.edf._build_clean_header`` writes into the four identification
# fields. A submission must arrive already carrying them: a file that does not has either not
# been through the preparation tool or has been altered since. ``test_submissions`` pins these
# to the de-identifier's actual output so the two cannot drift apart.
BLANK_PATIENT = "X X X X"
BLANK_RECORDING = "Startdate X X X X"
BLANK_START_DATE = "01.01.85"
BLANK_START_TIME = "00.00.00"

# Sidecar keys no pooled submission may carry, whatever the profile says. Free text a person
# typed at the centre, an annotator's name and any timestamp are what the preparation removes;
# a sidecar carrying one of these was not prepared by the tool, and rejecting it is what keeps
# a filter that quietly drops text from being a filter nobody notices when it stops working.
DEFAULT_FORBIDDEN_SIDECAR_KEYS: tuple[str, ...] = (
    "annotations",
    "text",
    "annotator",
    "created_at",
    "modified_at",
    "timestamp",
    "acquired_at",
    "recording_date",
    "patient",
    "subject",
)

DECLARED_HASH_KEY = "recording_sha256"

#: The sidecar key carrying a contributor's attestation, required where the profile sets an embargo.
ATTESTATION_KEY = "attestation"

#: The statement, inside the attestation, that the recording the file was exported from is not held on the platform.
#: An attributed whole beside its excerpt is matchable by content, and the platform cannot check the statement
#: without making that match.
SOURCE_NOT_HELD_KEY = "source_not_on_platform"

#: The attested embargo, inside the attestation: the profile's ``min_recording_age_months``, repeated.
EMBARGO_KEY = "min_recording_age_months"

#: The deepest nesting a sidecar may have. The viewer's sidecar is a few levels deep; a document
#: nested far past that is not one, and the gate's walk over it would exhaust the stack.
SIDECAR_MAX_DEPTH = 32

_FLOAT_TOLERANCE = 1e-6


@dataclass(frozen=True)
class Violation:
    """One reason a submission was refused.

    ``code`` is a stable token the audit row counts by; ``message`` is for the contributor and
    may quote the file (a channel label, a value), which is why it never reaches the trail.
    """

    code: str
    message: str


@dataclass(frozen=True)
class IngestProfile:
    """The rules a submission to one dataset is checked against.

    Every field that describes the file is optional so a profile can pin what matters to its
    pool: an empty ``channels`` tuple checks no channel names, a ``None`` rate checks no rate.
    ``channels`` are compared against each channel's canonical label where the platform
    resolves one (``Fp1``, ``C3-P3``) and against the raw label otherwise (``ECG``), in file
    order. ``durations_seconds`` lists the excerpt lengths a file may have.

    ``validate_sidecar`` receives the sidecar after the shape checks passed and returns
    further violations; ``ingest`` receives the created recording and the sidecar, inside the
    ingest transaction, after the platform has written the sidecar's events, interruptions and
    coded labels, and writes whatever else the project derives from it. Both are optional.
    ``forbidden_sidecar_keys`` extends the default set; it cannot shrink it. ``server_sidecar_keys`` are top-level
    keys accepted only in a sidecar the server builds for a file it already holds, never in one a contributor sent,
    so what a project derives from such a key can be trusted to have come from the platform's own records.

    ``min_recording_age_months`` is the embargo: the least time between a recording's acquisition and its export.
    The platform never sees the acquisition date, so a contributor attests it, with the statement that the source is
    not held on the platform, in the sidecar's :data:`ATTESTATION_KEY` (see :func:`attestation`). A sidecar the
    server builds carries none, and the caller of :func:`accept_submission` dates the file instead.

    ``k`` and ``m`` are the pool's release conditions. ``k`` is the smallest equivalence class a release run publishes
    from, applied by the project's selector (``library.release.select_by_class_size``); ``m`` is the number of active
    members the pool's contributor group must have before intake opens (``library.pools.set_intake``). ``m`` is a
    condition on the pool rather than on each class because counting distinct contributors per class means holding
    which contributor fed which class, and beside the ingest runs that joins a recording back to its ledger. Both are
    recorded on every release run. ``approvals`` is the number of distinct curators who must
    approve a member before a release publishes it (``library.release.approved_items``); a
    profile that sets none asks for one, so no pool member is published without a human review.
    """

    key: str
    channels: tuple[str, ...] = ()
    sampling_rate: float | None = None
    physical_unit: str | None = None
    physical_min: float | None = None
    physical_max: float | None = None
    digital_min: int | None = None
    digital_max: int | None = None
    durations_seconds: tuple[float, ...] = ()
    required_sidecar_keys: tuple[str, ...] = ()
    forbidden_sidecar_keys: tuple[str, ...] = ()
    server_sidecar_keys: tuple[str, ...] = ()
    validate_sidecar: Callable[[dict], list[Violation]] | None = field(default=None, compare=False)
    ingest: Callable[[Any, dict], None] | None = field(default=None, compare=False)
    k: int | None = None
    m: int | None = None
    approvals: int | None = None
    min_recording_age_months: int | None = None

    @property
    def all_forbidden_sidecar_keys(self) -> frozenset[str]:
        """The default forbidden keys plus the profile's own."""
        return frozenset(DEFAULT_FORBIDDEN_SIDECAR_KEYS) | frozenset(self.forbidden_sidecar_keys)

    @property
    def required_approvals(self) -> int:
        """``approvals``, or one when the profile sets none."""
        return self.approvals if self.approvals is not None else 1


_PROFILES: dict[str, IngestProfile] = {}


def register_ingest_profile(profile: IngestProfile) -> None:
    """Register ``profile`` under its key, replacing an earlier registration of the same key.

    Called from a project's ``apps.py::ready()``. Replacement rather than refusal, because
    ``ready()`` runs once per process and a test that registers a fixture profile must be able
    to do so repeatedly.

    A profile naming a channel no file can carry is refused here, at boot, rather than by
    refusing every submission later: see :func:`unsatisfiable_channels`. So is a ``k``, ``m``,
    ``approvals`` or ``min_recording_age_months`` that is not a positive integer, which no release run, intake check
    or attestation could apply.
    """
    if not profile.key or not profile.key.replace("_", "").replace("-", "").replace(".", "").isalnum():
        raise ValueError(f"Ingest profile key {profile.key!r} must be a non-empty identifier.")
    for name in ("k", "m", "approvals", "min_recording_age_months"):
        value = getattr(profile, name)
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 1):
            raise ValueError(f"Ingest profile {profile.key!r} has {name}={value!r}; it must be a positive integer.")
    unsatisfiable = unsatisfiable_channels(profile)
    if unsatisfiable:
        raise ValueError(
            f"Ingest profile {profile.key!r} names channels no file can carry: {unsatisfiable}. A channel must fit "
            f"an EDF label (at most 16 printable ASCII characters) and be the label the platform resolves it to."
        )
    _PROFILES[profile.key] = profile


def attestation(profile: IngestProfile) -> dict[str, Any] | None:
    """The value a contributor's sidecar must carry under :data:`ATTESTATION_KEY`, or ``None`` with no embargo.

    One value per profile, compared exactly: anything a contributor could vary, an extra key or a longer embargo
    than asked, would be a mark of its origin.
    """
    if profile.min_recording_age_months is None:
        return None
    return {EMBARGO_KEY: profile.min_recording_age_months, SOURCE_NOT_HELD_KEY: True}


def months_after(moment: datetime, months: int) -> datetime:
    """*moment* moved *months* calendar months on, the day clamped to the end of a shorter month."""
    index = moment.month - 1 + months
    year, month = moment.year + index // 12, index % 12 + 1
    return moment.replace(year=year, month=month, day=min(moment.day, calendar.monthrange(year, month)[1]))


def get_ingest_profile(key: str) -> IngestProfile | None:
    """The profile registered under ``key``, or ``None``."""
    return _PROFILES.get(key)


def registered_ingest_profiles() -> list[IngestProfile]:
    """Every registered profile, ordered by key."""
    return [_PROFILES[key] for key in sorted(_PROFILES)]


def reset_ingest_profiles() -> None:
    """Clear the registry. Test use only."""
    _PROFILES.clear()


def gate_label(label: str) -> str:
    """The label the gate compares a channel labelled ``label`` by, exactly as :func:`validate_file` derives it."""
    from recordings.processors.channel_labels import classify_channel
    from recordings.processors.edf import extract_signal_type

    return classify_channel(label, extract_signal_type(label))[1] or label.strip()


def unsatisfiable_channels(profile: IngestProfile) -> list[str]:
    """The profile's channels that a file labelling a channel with that exact name would not satisfy.

    Two ways a channel can be met by no label at all: it does not fit the EDF label field, which
    holds 16 bytes of printable ASCII and names the annotation channel ``EDF Annotations``; or the
    platform's resolver maps it elsewhere (``Chin`` to ``EMG/Chin``, ``Fz-Cz`` to ``Fz``), since
    the gate compares the canonical label where it resolves one. The published profile promises
    that a channel labelled as listed passes, which is what lets a preparation tool write the list
    verbatim.
    """
    from recordings.processors.edf import _SW_LABEL

    def writable(channel: str) -> bool:
        return (
            0 < len(channel) <= _SW_LABEL
            and channel.isascii()
            and channel.isprintable()
            and channel.lower() not in ("edf annotations", "bdf annotations")
        )

    return [channel for channel in profile.channels if not writable(channel) or gate_label(channel) != channel]


def public_profile(profile: IngestProfile) -> dict[str, Any]:
    """What a contributor's preparation tool needs to produce a file the gate accepts.

    Every value the platform checks, with the forbidden sidecar keys in full (the defaults and the
    profile's own), the name of the declared-hash key, and the embargo with the attestation that confirms it.
    ``validate_sidecar`` and ``ingest`` are code and are not published; the gate still runs the former on every
    submission.
    """
    return {
        "key": profile.key,
        "channels": list(profile.channels),
        "sampling_rate": profile.sampling_rate,
        "physical_unit": profile.physical_unit,
        "physical_min": profile.physical_min,
        "physical_max": profile.physical_max,
        "digital_min": profile.digital_min,
        "digital_max": profile.digital_max,
        "durations_seconds": list(profile.durations_seconds),
        "required_sidecar_keys": [
            DECLARED_HASH_KEY,
            *([ATTESTATION_KEY] if profile.min_recording_age_months is not None else []),
            *profile.required_sidecar_keys,
        ],
        "forbidden_sidecar_keys": sorted(profile.all_forbidden_sidecar_keys),
        "min_recording_age_months": profile.min_recording_age_months,
        "attestation": attestation(profile),
    }


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def _read(data: bytes, offset: int, width: int) -> str:
    return data[offset : offset + width].decode("ascii", errors="replace").strip()


def _nearly(a: float, b: float) -> bool:
    return math.isclose(a, b, rel_tol=0.0, abs_tol=_FLOAT_TOLERANCE)


def _refuse_constant(name: str) -> Any:
    raise ValueError(f"the sidecar carries {name}, which JSON does not define")


def parse_sidecar(raw: bytes) -> Any:
    """Parse a sidecar's bytes, raising ``ValueError`` for anything the gate cannot accept as JSON.

    ``NaN`` and ``Infinity`` are refused here: Python's parser accepts them and PostgreSQL's ``jsonb``
    does not, so a sidecar carrying one would pass the gate and fail at the insert. A document nested
    past what the parser itself can hold raises ``RecursionError``, which is turned into the same refusal.
    """
    try:
        return json.loads(raw.decode("utf-8"), parse_constant=_refuse_constant)
    except RecursionError as exc:
        raise ValueError("the sidecar is nested too deeply") from exc


def _shape_problem(value: Any, depth: int = 1) -> str | None:
    """Why *value* cannot be stored as the sidecar, or ``None``: too deep, or a string with a NUL.

    Iterative, so a document of any depth is measured without recursing into it.
    """
    stack = [(value, depth)]
    while stack:
        item, level = stack.pop()
        if level > SIDECAR_MAX_DEPTH:
            return f"it is nested deeper than {SIDECAR_MAX_DEPTH} levels"
        if isinstance(item, str):
            if "\x00" in item:
                return "a string in it contains a NUL character"
        elif isinstance(item, dict):
            for key, child in item.items():
                if isinstance(key, str) and "\x00" in key:
                    return "a key in it contains a NUL character"
                stack.append((child, level + 1))
        elif isinstance(item, list):
            stack.extend((child, level + 1) for child in item)
        elif isinstance(item, float) and not math.isfinite(item):
            return "it carries a non-finite number"
    return None


def _forbidden_keys_in(value: Any, forbidden: frozenset[str], path: str = "") -> list[str]:
    """Every forbidden key found anywhere in ``value``, as dotted paths."""
    found: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            here = f"{path}.{key}" if path else str(key)
            if str(key) in forbidden:
                found.append(here)
            found.extend(_forbidden_keys_in(child, forbidden, here))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(_forbidden_keys_in(child, forbidden, f"{path}[{index}]"))
    return found


def validate_file(profile: IngestProfile, data: bytes) -> list[Violation]:
    """Check the recording bytes against ``profile``. Reads nothing but the header."""
    from recordings.processors.edf import EdfParseError, parse_edf_header, parse_signal_infos

    violations: list[Violation] = []
    if len(data) < 256:
        return [Violation("format", "The file is too short to hold an EDF header.")]
    try:
        header = parse_edf_header(data)
    except EdfParseError as exc:
        return [Violation("format", f"Not an EDF or BDF file: {exc}")]
    signals = parse_signal_infos(data, header)
    if header.signal_count and not signals:
        return [Violation("format", "The signal header is truncated or corrupt.")]

    # Identification fields, byte for byte what the de-identifier writes. Read from the raw
    # bytes rather than the parsed header so nothing the parser normalises can pass.
    if _read(data, 8, 80) != BLANK_PATIENT:
        violations.append(Violation("identification", "The patient identification field is not blanked."))
    if _read(data, 88, 80) != BLANK_RECORDING:
        violations.append(Violation("identification", "The recording identification field is not blanked."))
    if _read(data, 168, 8) != BLANK_START_DATE or _read(data, 176, 8) != BLANK_START_TIME:
        violations.append(Violation("identification", "The start date and time are not the de-identified values."))

    # The per-signal reserved field, raw: the spec gives it no content, so anything in it is a writer's
    # signature or free text. It is the last per-signal section of the header.
    ns = header.signal_count
    reserved_start = 256 + ns * (256 - 32)
    reserved = data[reserved_start : reserved_start + 32 * ns]
    if ns > 0 and (len(reserved) != 32 * ns or reserved.strip(b" ")):
        violations.append(Violation("signal_reserved", "A signal's reserved field is not blank."))

    if any(s.is_annotation_channel for s in signals):
        violations.append(Violation("annotations", "The file carries an annotation channel; export without TALs."))

    # One-second records, whatever the profile says. The ingest normalises every other split it can, and
    # the ones it cannot are the odd splits a particular writer chose, so an unusual record duration
    # would survive into the pool as that writer's signature.
    if header.data_record_duration != 1.0:
        violations.append(
            Violation(
                "record_duration",
                f"Data records are {header.data_record_duration:g} s long; a submission uses one-second records.",
            )
        )

    expected_size = header.header_record_bytes + header.record_byte_size * header.data_record_count
    if header.data_record_count < 0 or expected_size != len(data):
        violations.append(Violation("truncated", "The file length does not match its header."))

    data_channels = [s for s in signals if not s.is_annotation_channel]
    if profile.channels:
        found = tuple((s.canonical_label or s.label.strip()) for s in data_channels)
        if found != tuple(profile.channels):
            violations.append(
                Violation(
                    "channels",
                    f"Channel set or order differs from the profile: got {list(found)}, "
                    f"expected {list(profile.channels)}.",
                )
            )

    for index, signal in enumerate(data_channels):
        where = f"channel {index + 1} ({signal.label.strip()!r})"
        if profile.sampling_rate is not None and not _nearly(signal.sampling_rate, profile.sampling_rate):
            violations.append(
                Violation(
                    "sampling_rate",
                    f"{where}: {signal.sampling_rate:g} Hz, profile requires {profile.sampling_rate:g}.",
                )
            )
        if profile.physical_unit is not None and signal.physical_unit.strip() != profile.physical_unit:
            violations.append(
                Violation(
                    "unit",
                    f"{where}: unit {signal.physical_unit.strip()!r}, profile requires {profile.physical_unit!r}.",
                )
            )
        if profile.physical_min is not None and not _nearly(signal.physical_min, profile.physical_min):
            violations.append(
                Violation("range", f"{where}: physical minimum {signal.physical_min:g} differs from the profile.")
            )
        if profile.physical_max is not None and not _nearly(signal.physical_max, profile.physical_max):
            violations.append(
                Violation("range", f"{where}: physical maximum {signal.physical_max:g} differs from the profile.")
            )
        if profile.digital_min is not None and signal.digital_min != profile.digital_min:
            violations.append(
                Violation("range", f"{where}: digital minimum {signal.digital_min} differs from the profile.")
            )
        if profile.digital_max is not None and signal.digital_max != profile.digital_max:
            violations.append(
                Violation("range", f"{where}: digital maximum {signal.digital_max} differs from the profile.")
            )

    if profile.durations_seconds:
        length = header.data_record_count * header.data_record_duration
        if not any(_nearly(length, allowed) for allowed in profile.durations_seconds):
            violations.append(
                Violation(
                    "duration",
                    f"Length {length:g} s is not one of the profile's durations {list(profile.durations_seconds)}.",
                )
            )
    return violations


def validate_sidecar(
    profile: IngestProfile, sidecar: Any, data: bytes, *, server_built: bool = False
) -> list[Violation]:
    """Check the sidecar's shape, its forbidden keys and its declared hash, then ask the profile.

    The profile's ``server_sidecar_keys`` are refused unless *server_built*, which only :func:`accept_submission`
    sets, for a held copy. The embargo attestation is required unless *server_built*: the server attests nothing, and
    dates the file instead.
    """
    if not isinstance(sidecar, dict):
        return [Violation("sidecar_shape", "The sidecar must be a JSON object.")]
    problem = _shape_problem(sidecar)
    if problem is not None:
        # Checked first and alone: the walks below recurse, and the store refuses what this names.
        return [Violation("sidecar_shape", f"The sidecar cannot be accepted: {problem}.")]
    from recordings.container import check_viewer_sidecar

    violations: list[Violation] = []
    try:
        # The pooled ingest writes the events, interruptions and labels from the sidecar, so a sidecar
        # it could not read is refused here rather than failing after acceptance.
        check_viewer_sidecar(sidecar)
    except ValueError as exc:
        violations.append(Violation("sidecar_shape", f"The sidecar is not the viewer's shape: {exc}."))
    for path in _forbidden_keys_in(sidecar, profile.all_forbidden_sidecar_keys):
        violations.append(Violation("sidecar_forbidden_key", f"The sidecar carries the forbidden key {path!r}."))
    if not server_built:
        for key in profile.server_sidecar_keys:
            if key in sidecar:
                violations.append(
                    Violation("sidecar_server_key", f"The key {key!r} is accepted only in a sidecar the server builds.")
                )
    for key in (DECLARED_HASH_KEY, *profile.required_sidecar_keys):
        if key not in sidecar:
            violations.append(Violation("sidecar_missing_key", f"The sidecar lacks the required key {key!r}."))
    expected = attestation(profile)
    if expected is not None and not server_built and not _same_json(sidecar.get(ATTESTATION_KEY), expected):
        violations.append(
            Violation(
                "sidecar_attestation",
                f"The sidecar must attest that the recording is at least {profile.min_recording_age_months} months "
                f"old and that its source is not held on the platform.",
            )
        )
    declared = sidecar.get(DECLARED_HASH_KEY)
    if isinstance(declared, str) and DECLARED_HASH_KEY in sidecar:
        if declared.strip().lower() != hashlib.sha256(data).hexdigest():
            violations.append(
                Violation("sidecar_hash", "The declared recording hash does not match the received bytes.")
            )
    elif DECLARED_HASH_KEY in sidecar:
        violations.append(Violation("sidecar_hash", "The declared recording hash must be a hex string."))
    if violations:
        return violations
    if profile.validate_sidecar is not None:
        # Project code: a failure of it refuses the submission rather than answering 500, and is logged
        # without the sidecar, which is the contributor's.
        try:
            answered = list(profile.validate_sidecar(sidecar))
        except Exception as exc:
            # The type only: an exception's message can quote the value it choked on.
            logger.error("submission gate: profile %r validate_sidecar raised %s", profile.key, type(exc).__name__)
            answered = [Violation("sidecar_profile", "The pool's sidecar check could not read this sidecar.")]
        violations.extend(answered)
    return violations


def _same_json(value: Any, expected: Any) -> bool:
    """Whether *value* equals *expected* with the JSON types kept apart, which Python's ``==`` does not: ``True`` is
    ``1`` and ``12.0`` is ``12`` to it."""
    if isinstance(expected, dict):
        return (
            isinstance(value, dict)
            and value.keys() == expected.keys()
            and all(_same_json(value[key], expected[key]) for key in expected)
        )
    return type(value) is type(expected) and value == expected


def validate_submission(
    profile: IngestProfile, data: bytes, sidecar: Any, *, server_built: bool = False
) -> list[Violation]:
    """Every violation of ``profile`` by the pair. Empty means the submission may be accepted."""
    return validate_file(profile, data) + validate_sidecar(profile, sidecar, data, server_built=server_built)


def validate_upload(profile: IngestProfile, data: bytes) -> list[Violation]:
    """Every violation of ``profile`` by the recording bytes of an ordinary upload. Empty means the file conforms.

    For a project that holds an upload to a pool's profile before the file ever reaches a pool: a refusal while the
    uploader can still act on it, and a second check of the stored file before a hand-over. An upload carries no gate
    sidecar, so only the file is checked, by :func:`validate_file`, and whatever a project sends beside the file is
    its own to check. A container from the viewer's export is checked as the EDF it carries, which is what ingest
    stores. Its footer must end where the file does, and what the footer holds is left to the upload's ingest.

    A file that passes still passes once the upload pipeline has processed it, which is what lets the stored file be
    checked again. ``TestUploadAgainstProfile`` pins that for a template in canonical order that ends in ECG.
    """
    from recordings.container import read_container_marker

    marker = read_container_marker(data)
    if marker is None:
        return validate_file(profile, data)
    _plain, total, kib = marker
    if kib < 1 or total + kib * 1024 != len(data):
        return [Violation("container", "The container's marker does not match the file's length.")]
    return validate_file(profile, data[:total])


# ---------------------------------------------------------------------------
# Who may submit, and where accepted files wait
# ---------------------------------------------------------------------------


def can_submit_to_dataset(user: Any, dataset: Any) -> bool:
    """True when ``dataset`` is an open submission pool and ``user`` belongs to its group.

    Group membership only: a dataset manager who is not in the group does not submit, and a
    superuser is not implied. The gate on the dataset is required because a submission into an
    ungated dataset would surface on arrival, which is the one thing pooling exists to prevent;
    a pool always has it on (``library.pools``), and the check is repeated here rather than
    trusted. A pool with intake closed accepts nothing.
    """
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    if not getattr(dataset, "release_gated", False) or dataset.submission_group_id is None:
        return False
    if not getattr(dataset, "submission_profile", "") or not getattr(dataset, "submissions_open", False):
        return False
    if getattr(dataset, "deleted_at", None) is not None:
        return False
    return user.groups.filter(pk=dataset.submission_group_id).exists()


def unlink_spooled_bytes(sender, instance, **kwargs) -> None:
    """``pre_delete`` receiver on ``SubmissionFile``: unlink the row's spooled bytes once the deletion commits.

    Every path that deletes a row — the retirement windows, a withdrawal, a ledger or dataset
    cascade — then takes the bytes with it, where before a cascade left them on disk with nothing
    pointing at them. After the commit, so a rolled-back deletion keeps its bytes; a crash between
    the commit and the unlink leaves an orphan the spool sweep removes. Only a path inside the
    spool is touched: an ingested row still names its old spool path, which no longer exists.
    """
    from django.db import transaction

    path = Path(instance.file_path or "")
    if not instance.file_path:
        return
    try:
        root = submission_spool_root().resolve()
        inside = path.resolve().is_relative_to(root)
    except OSError:
        return
    if not inside:
        return

    def _unlink() -> None:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            logger.warning("submission spool: could not unlink the bytes of a deleted submission row")

    transaction.on_commit(_unlink)


def submission_spool_root() -> Path:
    """The directory accepted submissions wait in until the pooled ingest run takes them."""
    configured = getattr(settings, "RECORDINGS_SUBMISSION_SPOOL_PATH", "") or ""
    if configured:
        root = Path(configured)
    else:
        root = Path(settings.RECORDINGS_STAGING_PATH) / "submissions"
    if not root.is_absolute():
        root = Path(settings.BASE_DIR) / root
    return root


# ---------------------------------------------------------------------------
# The accept path
# ---------------------------------------------------------------------------


class SubmissionRefused(Exception):
    """The gate refused a submission. Nothing was written, and a held file's spooled copy is unlinked."""

    def __init__(self, violations: list[Violation]):
        super().__init__(f"{len(violations)} violation(s)")
        self.violations = violations


class PoolUnavailable(Exception):
    """The pool was closed, dissolved or left by the contributor before the file could be accepted."""


@dataclass(frozen=True)
class HeldFileCopy:
    """A re-dithered copy of a file the server holds, waiting in the spool for :func:`accept_submission`.

    Made only by :func:`spool_held_file`, so the held route cannot reach the spool without the re-dither.
    """

    path: Path
    extension: str


@dataclass(frozen=True)
class AcceptedSubmission:
    """The ledger a file was accepted against, and whether this acceptance created it."""

    ledger: Any
    ledger_created: bool


def _fresh_spool_path(extension: str) -> Path:
    import secrets

    spool = submission_spool_root()
    spool.mkdir(parents=True, exist_ok=True)
    while True:
        path = spool / f"{secrets.token_hex(16).upper()}{extension}"
        if not path.exists():
            return path


def spool_held_file(source: Path, extension: str) -> HeldFileCopy:
    """Copy a file the server already holds into the spool under a fresh name and re-dither the copy.

    The source is left as it is. The copy's digest therefore equals no digest the server has recorded for the
    source, so no value joins the spool row to the source's own trail. Raises what ``redither_edf`` raises,
    with the copy unlinked. A copy waits without a row until it is accepted, so it must be accepted, or
    unlinked, within ``RECORDINGS_SUBMISSION_SPOOL_SWEEP_GRACE_HOURS``. *extension* is ``.edf`` or ``.bdf``,
    as on the endpoint.
    """
    import shutil

    from recordings.processors.redither import redither_edf

    if extension not in (".edf", ".bdf"):
        raise ValueError("A held file enters the spool as .edf or .bdf")
    path = _fresh_spool_path(extension)
    try:
        shutil.copyfile(source, path)
        redither_edf(path)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return HeldFileCopy(path=path, extension=extension)


def accept_submission(
    dataset: Any,
    contributor: Any,
    *,
    sidecar: Any,
    data: bytes | None = None,
    extension: str | None = None,
    held: HeldFileCopy | None = None,
    not_before: Any = None,
) -> AcceptedSubmission:
    """Check a submission against the pool's profile and, if it passes, accept it into the spool.

    The one accept path for a pool, shared by the submission endpoint and any server-side hand-over. Pass
    either the received ``data`` with its ``extension``, or a ``held`` copy from :func:`spool_held_file`. A
    held copy never travelled, so the server declares its digest in the sidecar itself before the gate runs, and its
    sidecar may carry the profile's ``server_sidecar_keys``.

    The gate runs before any write. A refusal raises :class:`SubmissionRefused` and writes nothing; received
    bytes were never written and a held copy is unlinked. Under the pool's row lock, the lock its configuration
    writes take, the contributor must still be allowed to submit, or :class:`PoolUnavailable` is raised. The
    contributor's ledger for the pool is created with their first accepted file. ``not_before`` holds the file
    back from the pooled run until that time.

    No activity is logged here: the caller audits against the returned ledger, or opens the system scope its
    row writes are recorded under. Received bytes are written inside the transaction, after the row, and any
    failure unlinks the spool file. Inside an outer transaction that later rolls back, the bytes stay without a
    row until the spool sweep removes them.
    """
    from django.db import transaction

    from library.models import Dataset
    from recordings.models import SubmissionFile, SubmissionLedger

    if (data is None) == (held is None):
        raise TypeError("accept_submission takes either data or held, not both and not neither")
    if held is not None:
        extension = held.extension
        # Before the cleanup below can run: a second acceptance of one copy that failed would unlink the
        # bytes the first one's row names.
        if SubmissionFile.objects.filter(file_path=str(held.path)).exists():
            raise ValueError("This held copy has already been accepted")
    elif not extension:
        raise TypeError("accept_submission needs the extension of received data")

    path = held.path if held is not None else None
    try:
        if held is not None:
            data = held.path.read_bytes()
            if isinstance(sidecar, dict):
                sidecar = {**sidecar, DECLARED_HASH_KEY: hashlib.sha256(data).hexdigest()}
        profile = get_ingest_profile(dataset.submission_profile)
        if profile is None:
            raise PoolUnavailable("The pool's ingest profile is no longer registered")
        # A held copy's sidecar is the server's own; one that arrived with the request is the contributor's.
        violations = validate_submission(profile, data, sidecar, server_built=held is not None)
        if violations:
            raise SubmissionRefused(violations)
        if path is None:
            path = _fresh_spool_path(extension)
        with transaction.atomic():
            locked = Dataset.objects.select_for_update().get(pk=dataset.pk)
            if not can_submit_to_dataset(contributor, locked):
                raise PoolUnavailable("The pool no longer accepts files from this contributor")
            ledger, created = SubmissionLedger.objects.get_or_create(dataset=locked, contributor=contributor)
            SubmissionFile.objects.create(
                ledger=ledger,
                stored_name=path.name,
                file_extension=extension,
                file_path=str(path),
                file_size=len(data),
                file_hash=hashlib.sha256(data).hexdigest(),
                sidecar_hash=hashlib.sha256(json.dumps(sidecar, sort_keys=True).encode()).hexdigest(),
                sidecar=sidecar,
                not_before=not_before,
            )
            if held is None:
                path.write_bytes(data)
    except BaseException:
        if path is not None:
            path.unlink(missing_ok=True)
        raise
    return AcceptedSubmission(ledger=ledger, ledger_created=created)
