"""Validating submissions to a submission pool: the profile registry, the gate, the spool and who may submit.

⚠️ LOAD-BEARING — the submission gate.
The gate is what keeps a pooled dataset's members indistinguishable by origin, and what keeps
bytes that might identify someone from being written anywhere. Every check in
:func:`validate_file` and :func:`validate_sidecar` is a fingerprint or an identifier the profile
promised is absent: blanked identification fields as the de-identifier writes them, no
annotation channel, the channel set and order, the rate, ranges, length, the forbidden sidecar
keys and the declared hash. Dropping one, or letting a failing check repair the file instead of
refusing it, admits the site's or the vendor's signature into the pool with every locally
written test still green. The endpoint contract is the other half: a refused submission writes
no row and no file. Contract tests are in ``recordings/tests/test_submissions.py`` (``TestGate``
pins each check, including the blank fields against the de-identifier's actual output,
``TestPoolEndpoints`` the write-nothing refusal and the ledger as the audit target,
``TestPooledIngest`` the random order and the system author, ``TestPooledIngestTrail`` the unjoinable trail).

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
fixed durations, forbidden sidecar keys refused rather than dropped, and the declared hash
equal to the received bytes. The project owns the profile's values, whatever the sidecar
must say beyond its shape (``validate_sidecar``) and what happens to the sidecar once the
recording exists (``ingest``). A deployment that registers no profile has no submission
path; its upload endpoint and bulk import are untouched.

The registry is inert until a profile is registered from a project's ``apps.py::ready()``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
from collections.abc import Callable
from dataclasses import dataclass, field
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
    coded labels, and writes whatever else the project derives from it. Both are optional. ``forbidden_sidecar_keys`` extends the default set;
    it cannot shrink it.

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
    validate_sidecar: Callable[[dict], list[Violation]] | None = field(default=None, compare=False)
    ingest: Callable[[Any, dict], None] | None = field(default=None, compare=False)
    k: int | None = None
    m: int | None = None
    approvals: int | None = None

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
    refusing every submission later: see :func:`unsatisfiable_channels`. So is a ``k``, ``m`` or
    ``approvals`` that is not a positive integer, which no release run or intake check could apply.
    """
    if not profile.key or not profile.key.replace("_", "").replace("-", "").replace(".", "").isalnum():
        raise ValueError(f"Ingest profile key {profile.key!r} must be a non-empty identifier.")
    for name in ("k", "m", "approvals"):
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
    profile's own) and the name of the declared-hash key. ``validate_sidecar`` and ``ingest`` are
    code and are not published; the gate still runs the former on every submission.
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
        "required_sidecar_keys": [DECLARED_HASH_KEY, *profile.required_sidecar_keys],
        "forbidden_sidecar_keys": sorted(profile.all_forbidden_sidecar_keys),
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


def validate_sidecar(profile: IngestProfile, sidecar: Any, data: bytes) -> list[Violation]:
    """Check the sidecar's shape, its forbidden keys and its declared hash, then ask the profile."""
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
    for key in (DECLARED_HASH_KEY, *profile.required_sidecar_keys):
        if key not in sidecar:
            violations.append(Violation("sidecar_missing_key", f"The sidecar lacks the required key {key!r}."))
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


def validate_submission(profile: IngestProfile, data: bytes, sidecar: Any) -> list[Violation]:
    """Every violation of ``profile`` by the pair. Empty means the submission may be accepted."""
    return validate_file(profile, data) + validate_sidecar(profile, sidecar, data)


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
