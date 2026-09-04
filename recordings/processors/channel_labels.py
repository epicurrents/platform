"""Canonical channel-label normalisation.

Derives a canonical channel name (and refines the signal type) from a raw EDF/BDF
channel label so every downstream consumer (BIDS export / ``to_bids``, spike
detectors, the YASA remontage, the MNE forward model) shares one canonical name instead of
each re-implementing affix-stripping and nomenclature mapping. ``canonicalise_label``
is the EEG 10-10 primitive; ``classify_channel`` wraps it and the conservative
EOG/EMG/EKG resolvers to return ``(signal_type, canonical_label)``.

Pure and dependency-light: a function of ``(label, signal_type)`` with no Django
or I/O imports, so it is exhaustively unit-testable and safe to call on the ingest
hot path. This module itself never mutates anything — ``canonical_label`` is a
derived view recomputed on every reprocess, so it cannot drift from ``label``.
(The ingest de-identification pass in ``processors/edf.py`` *writes* the canonical
names into the stored file's labels; that mutation lives there, not here.)

Design (docs/engineering-notes/input-normalisation-implementation-plan.md §A):

* **Vocabulary is the filter.** A label is canonicalised only if it resolves to a
  known 10-10 electrode; anything else returns ``''``.  Known non-EEG signal types
  (EMG/EOG/EKG) are excluded up front, but the gate is deliberately *not*
  ``signal_type == 'eeg'``: many clinical EDFs label EEG channels bare (``Fp1``,
  ``C3``) so their inferred type is ``''`` — the vocabulary still resolves them.
* **Old→new rename covers the unambiguous position renames only** — T3→T7, T4→T8,
  T5→P7, T6→P8.  A1/A2 (earlobe reference sites) are kept **verbatim**, never
  remapped to the mastoid positions M1/M2 — that conflates physically distinct
  locations.  A future label-alias system is the home for A1↔M1 interchangeability.
* **Reference vs bipolar suffix.** For a ``PRIMARY-SUFFIX`` label, a suffix that is
  a known reference (``Ref``/``AVG``/``A1``/``A2``/``M1``/``M2``/``Cz``…) is stripped
  and the primary canonicalised (referential montage); a suffix that is itself a
  scalp electrode is a genuine bipolar derivation and both sides are canonicalised
  into ``A-B`` (referential conversion, if ever needed, is a downstream remontage
  concern, not this layer's).
* **Unresolved EEG labels return ``''``** — never a guess.  The backfill command
  counts them as a data-quality signal.
* **The 10-10 vocabulary also *withdraws* the EEG type.**  ``extract_signal_type``
  types a channel ``eeg`` on nothing more than the substring ``EEG`` appearing in
  the label, which is how ``EEG Photic-Ref`` (a photic-stimulator trace) and
  ``EEG Event-Ref`` (a marker line) come out typed as brain signal.  The gate is
  symmetric: a label claiming EEG that resolves to no 10-10 electrode is
  **demoted**, either to a recognised auxiliary role or to ``misc``.  Both
  directions use the same evidence — vocabulary membership — rather than trusting
  the substring in one direction and not the other.
* **Auxiliary roles are a vocabulary too.**  Photic, event, trigger, DC and the
  common polysomnography aux traces get controlled ``(type, canonical)`` pairs, so
  the channels that used to masquerade as EEG end up addressable by name instead of
  merely excluded.  Matching is against the *whole* label core (after the modality
  prefix and any reference suffix are stripped), never a substring — the mistake
  being fixed here.
"""

from __future__ import annotations

import re

# --- 10-10 electrode vocabulary (canonical casing) -------------------------
# Clinical 10-20 plus the common 10-10 extension. A1/A2 (earlobe) and M1/M2
# (mastoid) are all valid canonical tokens; they are never mapped to each other.
_TEN_TEN_ELECTRODES: tuple[str, ...] = (
    "Fp1",
    "Fpz",
    "Fp2",
    "AF7",
    "AF3",
    "AFz",
    "AF4",
    "AF8",
    "F9",
    "F7",
    "F5",
    "F3",
    "F1",
    "Fz",
    "F2",
    "F4",
    "F6",
    "F8",
    "F10",
    "FT9",
    "FT7",
    "FC5",
    "FC3",
    "FC1",
    "FCz",
    "FC2",
    "FC4",
    "FC6",
    "FT8",
    "FT10",
    "T9",
    "T7",
    "C5",
    "C3",
    "C1",
    "Cz",
    "C2",
    "C4",
    "C6",
    "T8",
    "T10",
    "TP9",
    "TP7",
    "CP5",
    "CP3",
    "CP1",
    "CPz",
    "CP2",
    "CP4",
    "CP6",
    "TP8",
    "TP10",
    "P9",
    "P7",
    "P5",
    "P3",
    "P1",
    "Pz",
    "P2",
    "P4",
    "P6",
    "P8",
    "P10",
    "PO7",
    "PO3",
    "POz",
    "PO4",
    "PO8",
    "O1",
    "Oz",
    "O2",
    "Iz",
    "Nz",
    "A1",
    "A2",
    "M1",
    "M2",
)

# Uppercased token → canonical-cased electrode, for case-insensitive lookup.
_CANONICAL_BY_UPPER: dict[str, str] = {e.upper(): e for e in _TEN_TEN_ELECTRODES}

# Old (1985 10-20) → new (10-10) rename: identical physical position, new name.
# A1/A2 are intentionally absent — see module docstring.
_OLD_TO_NEW: dict[str, str] = {"T3": "T7", "T4": "T8", "T5": "P7", "T6": "P8"}

# Suffix tokens that denote a *reference*, not a bipolar partner. Checked before
# electrode membership so referential montages (``C3-Cz``, ``C4-A1``, ``F3-M2``)
# canonicalise to their primary. Cz/A1/A2/M1/M2 are common clinical references;
# they are treated as references *only in the suffix position* — as a standalone
# channel label they resolve to themselves.
_REFERENCE_SUFFIXES: frozenset[str] = frozenset(
    {"REF", "AVG", "AVE", "AV", "LE", "CAR", "A1", "A2", "M1", "M2", "CZ", "G2", "GND", "COM", "RE", "0"}
)

# Suffix marking a platform-written derived copy of another channel: a signal-repair
# stage writes the corrected signal under the original label and preserves the
# pristine source as ``<label>_orig`` in the same file (a project's signal-repair
# middleware writes it; ``correctedChannelSuffix`` in the viewer reads it).
# Registered here so the classifier and any montage-shape detector treat these as
# derived copies by construction — a re-uploaded platform-processed file must not
# have its ``_orig`` channels mistaken for duplicate electrodes or vendor junk.
DERIVED_COPY_SUFFIX = "_orig"

# The electrophysiological modalities: signals from tissue, with a montage.
PRIMARY_TYPES: frozenset[str] = frozenset({"eeg", "eog", "emg", "ekg"})

# Everything else a recording carries: stimulus and trigger lines, DC inputs,
# oximetry, respiration, position. Real channels that are not brain, muscle, eye or
# heart potentials — so they must never be counted as the recording's modality nor
# fed to a detector that assumes electrophysiology.
#
# ``trig``/``misc`` follow the BIDS ``channels.tsv`` type vocabulary (lower-cased to
# match the existing tokens), which already had to name exactly these things.
AUXILIARY_TYPES: frozenset[str] = frozenset({"trig", "misc"})

# Signal types (already inferred by ``extract_signal_type``) that are never EEG —
# used both to short-circuit ``canonicalise_label`` and to tell the backfill command
# that an empty ``canonical_label`` is expected rather than an unresolved electrode.
_NON_EEG_TYPES: frozenset[str] = (PRIMARY_TYPES - {"eeg"}) | AUXILIARY_TYPES | {"ecg"}

# Priors that name a specific *biological* modality. The auxiliary vocabulary defers
# to these (an EOG channel stays EOG), but not to a bare or ``eeg`` prior — and not
# to ``trig``/``misc``, so re-classifying an already-classified channel is idempotent.
_MODALITY_PRIORS: frozenset[str] = (PRIMARY_TYPES - {"eeg"}) | {"ecg"}

# Leading modality word to strip (``EEG Fp1-Cz`` → ``Fp1-Cz``).
_MODALITY_PREFIX = re.compile(r"^\s*eeg\b[\s:._-]*", re.IGNORECASE)

# Trailing prime marks (``C3'``): modified-position notation from some vendor
# exports and neonatal montages. Normalised away on resolution (``C3'`` → ``C3``):
# a primed montage never coexists with its unprimed originals in the same file —
# the primes *are* the montage — so stripping loses nothing within a recording,
# while a preserved ``C3'`` canonical would be unknown to every downstream
# consumer (forward model, detectors, BIDS export) and the prime convention is
# itself a site fingerprint. The primed original survives in the author-private
# ``source_label``. A pathological file carrying both ``C3`` and ``C3'``
# collides after normalisation and falls into the standard duplicate handling
# (first kept, rest demoted to ``MISC_<n>``, layout flagged ``mixed``).
_PRIME_SUFFIX = re.compile(r"'+$")


def is_non_eeg_type(signal_type: str) -> bool:
    """True when *signal_type* is a known non-EEG type (EMG/EOG/EKG/ECG/trig/misc).

    Used by the backfill command to decide whether an empty ``canonical_label``
    is expected (non-EEG) or an unresolved EEG label worth surfacing.
    """
    return signal_type.lower() in _NON_EEG_TYPES


def is_auxiliary_type(signal_type: str) -> bool:
    """True when *signal_type* is an auxiliary (non-electrophysiological) role.

    Callers that summarise a recording — "what modality is this?", "which channels
    can a detector see?" — must exclude these, or a file whose EEG labels are all
    non-standard will report itself as a ``misc`` recording.
    """
    return signal_type.lower() in AUXILIARY_TYPES


def _resolve_token(token: str) -> str | None:
    """Return the canonical 10-10 name for a single electrode *token*, or None.

    Trailing primes are normalised away (``C3'`` → ``C3``) — see ``_PRIME_SUFFIX``.
    """
    up = token.strip().upper()
    if not up:
        return None
    up = _PRIME_SUFFIX.sub("", up)
    if not up:
        return None
    up = _OLD_TO_NEW.get(up, up)
    return _CANONICAL_BY_UPPER.get(up)


def canonicalise_label(label: str, signal_type: str = "") -> str:
    """Return the canonical 10-10 electrode name for *label*, or ``''``.

    *signal_type* is the value inferred by
    :func:`recordings.processors.edf.extract_signal_type`; a known non-EEG type
    short-circuits to ``''``.  The function is pure and idempotent
    (``canonicalise_label(canonicalise_label(x)) == canonicalise_label(x)``).
    """
    if not label or signal_type.lower() in _NON_EEG_TYPES:
        return ""

    text = _MODALITY_PREFIX.sub("", label.strip())
    parts = [p.strip() for p in text.split("-") if p.strip()]

    if len(parts) == 1:
        return _resolve_token(parts[0]) or ""

    if len(parts) == 2:
        primary, suffix = parts
        primary_canon = _resolve_token(primary)
        if primary_canon is None:
            return ""
        if _PRIME_SUFFIX.sub("", suffix.upper()) in _REFERENCE_SUFFIXES:
            return primary_canon  # referential montage — reference stripped
        suffix_canon = _resolve_token(suffix)
        if suffix_canon is None:
            return ""  # neither a reference nor a known electrode → unresolved
        return f"{primary_canon}-{suffix_canon}"  # genuine bipolar derivation

    # 0 parts (empty after prefix strip) or >2 parts (not a montage we model).
    return ""


def canonicalise_label_keep_reference(label: str, signal_type: str = "") -> str:
    """Like :func:`canonicalise_label`, but keep a recognised reference suffix.

    ``EEG Fp1-A1`` → ``Fp1-A1`` where ``canonicalise_label`` returns ``Fp1``. Used by
    the ingest de-identification pass when reference stripping would make two
    channels collide on the same canonical name (``Fp1-A1`` + ``Fp1-A2``): the
    reference-preserving form keeps the labels unique and the ipsi/contra
    distinction intact. The reference token is normalised through the electrode
    table when it resolves (``CZ`` → ``Cz``) and upper-cased otherwise (``ref`` →
    ``REF``). Single-token and genuine-bipolar labels behave exactly as in
    :func:`canonicalise_label`. Pure and idempotent.
    """
    if not label or signal_type.lower() in _NON_EEG_TYPES:
        return ""

    text = _MODALITY_PREFIX.sub("", label.strip())
    parts = [p.strip() for p in text.split("-") if p.strip()]

    if len(parts) == 2:
        primary, suffix = parts
        primary_canon = _resolve_token(primary)
        if primary_canon is None:
            return ""
        if _PRIME_SUFFIX.sub("", suffix.upper()) in _REFERENCE_SUFFIXES:
            reference = _resolve_token(suffix) or _PRIME_SUFFIX.sub("", suffix.strip().upper())
            return f"{primary_canon}-{reference}"

    return canonicalise_label(label, signal_type)


# --- Non-EEG modality vocabularies (conservative) --------------------------
# Unlike 10-10 EEG, EOG/EMG/EKG naming is convention-based, so these map only
# well-known labels; anything else stays '' ("unclassified" — the UI shows the
# raw label). EEG is the only modality that strips a reference (``C3-Cz`` → ``C3``);
# each modality below has its own collapse rule.

_EOG_LEFT_KEYS = frozenset({"LOC", "EOGL", "LEOG", "E1"})
_EOG_RIGHT_KEYS = frozenset({"ROC", "EOGR", "REOG", "E2"})

_EMG_CHIN_KEYS = frozenset({"CHIN", "CHIN1", "CHIN2", "CHINZ", "SUBMENTAL"})
_EMG_LEGL_KEYS = frozenset({"LLEG", "LEGL", "LTIB", "TIBL", "LAT"})
_EMG_LEGR_KEYS = frozenset({"RLEG", "LEGR", "RTIB", "TIBR", "RAT"})
_EMG_ARML_KEYS = frozenset({"LARM", "ARML"})
_EMG_ARMR_KEYS = frozenset({"RARM", "ARMR"})
# Single-token → EMG site. Derivations (``Chin1-Chin2``) collapse to the site.
_EMG_SITE_BY_KEY = {
    **dict.fromkeys(_EMG_CHIN_KEYS, "Chin"),
    **dict.fromkeys(_EMG_LEGL_KEYS, "LegL"),
    **dict.fromkeys(_EMG_LEGR_KEYS, "LegR"),
    **dict.fromkeys(_EMG_ARML_KEYS, "ArmL"),
    **dict.fromkeys(_EMG_ARMR_KEYS, "ArmR"),
}
# Bare labels distinctive enough to set the modality without an explicit marker.
# LAT/RAT are excluded (ambiguous bare — could be 'lateral'); they map only when
# the label already carries 'EMG'.
_EMG_BARE_KEYS = frozenset(_EMG_SITE_BY_KEY) - {"LAT", "RAT"}

_ECG_LEAD_CANON = {
    "I": "I",
    "II": "II",
    "III": "III",
    "AVR": "aVR",
    "AVL": "aVL",
    "AVF": "aVF",
    "V1": "V1",
    "V2": "V2",
    "V3": "V3",
    "V4": "V4",
    "V5": "V5",
    "V6": "V6",
}


def _compact(text: str) -> str:
    """Uppercase and drop all separators, for tolerant single-token matching."""
    return re.sub(r"[\s._/\\-]", "", text).upper()


def _canonicalise_eog(label: str) -> str:
    """``LOC`` / ``ROC`` when a side is named (including the primary of a
    derivation like ``E1-M2``), else plain ``EOG``.

    E1/E2 are taken as left/right (AASM convention; a reversed montage is operator
    error, not something to second-guess here). NB: EGI high-density systems label
    EEG channels ``E1``..``EN`` — this mapping assumes clinical EOG usage.
    """
    key = _compact(label)
    if key in _EOG_LEFT_KEYS or ("EOG" in key and "LEFT" in key):
        return "LOC"
    if key in _EOG_RIGHT_KEYS or ("EOG" in key and "RIGHT" in key):
        return "ROC"
    # derivation (e.g. E1-M2): decide from the primary (first) electrode
    parts = [p for p in re.sub(r"[\s._/]", "", label).upper().strip("-").split("-") if p]
    if parts and parts[0] in _EOG_LEFT_KEYS:
        return "LOC"
    if parts and parts[0] in _EOG_RIGHT_KEYS:
        return "ROC"
    return "EOG"


def _emg_site(label: str) -> str:
    """The EMG site for *label*, collapsing derivations (``Chin1-Chin2`` → Chin),
    or '' when nothing matches or the parts disagree."""
    core = re.sub(r"[\s._/]", "", label).upper()  # keep '-' for the derivation split
    core = core.removeprefix("EMG")
    parts = [p for p in core.strip("-").split("-") if p]
    sites = {_EMG_SITE_BY_KEY.get(p) for p in parts}
    sites.discard(None)
    return sites.pop() if len(sites) == 1 else ""


def _canonicalise_emg(label: str) -> str:
    """``EMG/<site>`` for a recognised site (Chin/LegL/LegR/ArmL/ArmR; derivations
    collapse to the site), else '' — muscle-named or unmatched EMG is displayed by
    its raw label."""
    site = _emg_site(label)
    return f"EMG/{site}" if site else ""


def _canonicalise_ecg(label: str) -> str:
    """Lead name when present, numbered ``ECGn`` kept verbatim, else generic ``ECG``.

    Numbered channels are kept because we can't know which standard position (if
    any) they map to; bipolar numbered pairs (``ECG1-ECG2``) are kept as-is.
    """
    light = re.sub(r"[\s._]", "", label).upper()  # keep - / for structure
    m = re.fullmatch(r"E[CK]G(\d+)", light)
    if m:
        return f"ECG{m.group(1)}"
    m = re.fullmatch(r"E[CK]G(\d+)-E[CK]G(\d+)", light)
    if m:
        return f"ECG{m.group(1)}-ECG{m.group(2)}"
    core = re.sub(r"^E[CK]G", "", light).strip("-/")
    return _ECG_LEAD_CANON.get(core, "ECG")


def _detect_bare_modality(label: str) -> str:
    """Modality for a label carrying no explicit EOG/EMG/EKG marker, from a small
    set of distinctive tokens. Conservative — returns '' when unsure."""
    key = _compact(label)
    if key in _EOG_LEFT_KEYS or key in _EOG_RIGHT_KEYS:
        return "eog"
    if key in _EMG_BARE_KEYS:
        return "emg"
    return ""


# --- Auxiliary role vocabulary ---------------------------------------------
# Whole-label-core → ``(signal_type, canonical_label)`` for the non-electrophysiological
# channels a clinical recording carries alongside the montage. Matched only after the
# modality prefix and any reference suffix are stripped, and only against the *entire*
# core — the substring matching this table exists to replace.
#
# Two families:
#   trig — stimulus and event lines. Their samples encode *when something happened*,
#          not a biological potential, so an amplitude-based detector reading one is
#          reading a square wave (this is precisely ``EEG Photic-Ref``).
#   misc — real transduced measurements that are not brain/eye/muscle/heart potentials.
#          Usually in their own physical units too (see ``processors.units``).
_AUX_ROLES: dict[str, tuple[str, str]] = {
    # Stimulus / trigger / marker lines.
    "PHOTIC": ("trig", "Photic"),
    "PHOT": ("trig", "Photic"),
    "PHOTICSTIM": ("trig", "Photic"),
    "FLASH": ("trig", "Photic"),
    "STROBE": ("trig", "Photic"),
    "EVENT": ("trig", "Event"),
    "EVENTS": ("trig", "Event"),
    "MARKER": ("trig", "Event"),
    "MARK": ("trig", "Event"),
    "TRIG": ("trig", "Trigger"),
    "TRIGGER": ("trig", "Trigger"),
    "TTL": ("trig", "Trigger"),
    "STIM": ("trig", "Trigger"),
    "STATUS": ("trig", "Status"),  # BioSemi BDF trigger channel
    "SYNC": ("trig", "Sync"),
    # Cardio-respiratory and other transduced aux traces.
    "PULSE": ("misc", "Pulse"),
    "SPO2": ("misc", "SpO2"),
    "SAO2": ("misc", "SpO2"),
    "OSAT": ("misc", "SpO2"),
    "PLETH": ("misc", "PPG"),
    "PPG": ("misc", "PPG"),
    "RESP": ("misc", "Resp"),
    "AIRFLOW": ("misc", "Resp"),
    "FLOW": ("misc", "Resp"),
    "NASAL": ("misc", "Resp"),
    "THOR": ("misc", "Thorax"),
    "THORAX": ("misc", "Thorax"),
    "CHEST": ("misc", "Thorax"),
    "ABD": ("misc", "Abdomen"),
    "ABDOMEN": ("misc", "Abdomen"),
    "SNORE": ("misc", "Snore"),
    "POS": ("misc", "Position"),
    "POSITION": ("misc", "Position"),
    "BODYPOS": ("misc", "Position"),
    "TEMP": ("misc", "Temp"),
    "TEMPERATURE": ("misc", "Temp"),
    "LIGHT": ("misc", "Light"),
    "IMP": ("misc", "Impedance"),
    "IMPEDANCE": ("misc", "Impedance"),
}

# Numbered analogue inputs (``DC1``…``DC12``, ``AUX3``), kept individually because the
# number is the only thing identifying what was plugged in.
_NUMBERED_AUX = re.compile(r"^(DC|AUX)(\d{1,2})$")


def _aux_role(label: str) -> tuple[str, str]:
    """Return ``(signal_type, canonical_label)`` for a recognised auxiliary channel,
    or ``('', '')``.

    Strips the ``EEG`` modality prefix and a reference suffix first, because these
    channels are routinely recorded referentially against the same reference as the
    montage (``EEG Photic-Ref``) and so arrive wearing EEG's clothes.
    """
    text = _MODALITY_PREFIX.sub("", label.strip())
    parts = [p.strip() for p in text.split("-") if p.strip()]
    if len(parts) == 2 and parts[1].upper() in _REFERENCE_SUFFIXES:
        parts = parts[:1]
    if len(parts) != 1:
        return "", ""
    key = _compact(parts[0])
    role = _AUX_ROLES.get(key)
    if role is not None:
        return role
    numbered = _NUMBERED_AUX.match(key)
    if numbered:
        return "misc", f"{numbered.group(1)}{int(numbered.group(2))}"
    return "", ""


def classify_channel(label: str, prior_type: str = "") -> tuple[str, str]:
    """Return ``(signal_type, canonical_label)`` for a raw channel *label*.

    Dispatches per modality, conservatively:

    * **EEG** — the 10-10 vocabulary is distinctive enough to resolve a bare
      ``Fp1`` and to *upgrade an empty prior_type* to ``eeg`` (a recognised
      electrode is definitionally EEG, a stronger signal than the label-substring
      heuristic).
    * **Auxiliary** — a label whose core is a known stimulus/trigger/aux token
      (``Photic``, ``Event``, ``Status``, ``DC1``, ``SpO2``…) is typed ``trig`` or
      ``misc`` with its own canonical name. Checked **before** a substring-derived
      ``eeg`` prior, because that substring is exactly what mislabels these.
    * **EOG / EMG / EKG** — canonicalised when the label carries the modality word
      (via *prior_type* from ``extract_signal_type``) or is a distinctive bare
      token (``LOC``/``ROC``/``E1``/``E2``, chin/leg/arm EMG). EOG →
      ``LOC``/``ROC``/``EOG``; EMG → ``EMG/<site>`` or ''; EKG → lead / ``ECGn`` /
      ``ECG``.
    * **Demotion** — a leftover ``eeg`` prior that resolved to no 10-10 electrode
      and matched no auxiliary role is downgraded to ``misc`` with a ``''``
      canonical. It is a real channel whose modality the label does not establish,
      and ``misc`` says that; leaving it ``eeg`` would put a photic trace or a DC
      input into a detector's montage.

    A non-empty prior_type is never overridden *except* by these two
    vocabulary-backed decisions — promotion on a 10-10 hit and demotion on an
    ``eeg`` miss. Anything not confidently placed returns a '' canonical
    ("unclassified") — the UI shows the raw label. Only EEG strips a reference;
    each other modality has its own collapse rule.

    Caveat, deliberately accepted: montages outside 10-10 — intracranial strips and
    depths (``RAH1``, ``LTP3``), high-density EGI (``E17``) — are demoted too when
    their label carries the ``EEG`` marker. That is the honest reading of the
    evidence available here; recognising them needs a per-recording montage
    declaration or the label-alias system the module docstring anticipates, not a
    looser substring rule.

    NB: this refines ``signal_type``, which **is** covered by the SignalInfo audit
    digest — so it applies at ingest and on reprocess (both re-bake the digest),
    never via the canonical-label backfill (which touches only the digest-excluded
    ``canonical_label``).
    """
    if label.rstrip().endswith(DERIVED_COPY_SUFFIX):
        # Platform-written derived copy (see DERIVED_COPY_SUFFIX): classify the base
        # label and, when it resolves, keep the suffixed form as the canonical name so
        # the pairing convention survives re-ingest. Typed ``misc`` — a copy is not a
        # montage member and must not feed detectors or layout assessment as one.
        base = label.rstrip()[: -len(DERIVED_COPY_SUFFIX)]
        # Resolve the base under a biological-modality prior only: the copy itself is
        # typed ``misc``, and feeding that (or ``trig``) back in would short-circuit the
        # vocabulary lookup and break re-classification idempotency.
        base_prior = prior_type if prior_type.lower() in _MODALITY_PRIORS else ""
        _, base_canonical = classify_channel(base, base_prior)
        if base_canonical:
            return "misc", f"{base_canonical}{DERIVED_COPY_SUFFIX}"
        return "misc", ""

    eeg = canonicalise_label(label, prior_type)
    if eeg and prior_type in ("", "eeg"):
        return "eeg", eeg

    if prior_type.lower() not in _MODALITY_PRIORS:
        aux_type, aux_canonical = _aux_role(label)
        if aux_type:
            return aux_type, aux_canonical

    modality = prior_type or _detect_bare_modality(label)
    if modality == "eog":
        return "eog", _canonicalise_eog(label)
    if modality == "emg":
        return "emg", _canonicalise_emg(label)
    if modality in ("ekg", "ecg"):
        return "ekg", _canonicalise_ecg(label)
    if modality == "eeg":
        # Claimed EEG, resolved to no electrode, matched no auxiliary role.
        return "misc", ""
    return prior_type, ""


# --- Montage-shape assessment ----------------------------------------------

LAYOUT_REFERENTIAL = "referential"
LAYOUT_BIPOLAR = "bipolar"
LAYOUT_MIXED = "mixed"
LAYOUT_UNKNOWN = "unknown"


def assess_channel_layout(channels) -> tuple[str, int]:
    """Return ``(channel_layout, unresolved_channel_count)`` for a parsed channel set.

    *channels* is any iterable of objects with ``signal_type``, ``canonical_label``,
    and ``is_annotation_channel`` attributes (duck-typed; both parsed
    ``EdfSignalInfo`` objects and ``SignalInfo`` model rows qualify).

    The layout verdict is computed from the resolved EEG canonicals only —
    detection keys on the parsed structure, never on raw label strings, so
    platform-written derived copies (classified ``misc`` via
    ``DERIVED_COPY_SUFFIX``) and auxiliary channels cannot distort it:

    - ``referential`` — every resolved EEG canonical is a unique bare electrode.
    - ``bipolar`` — every resolved EEG canonical is an ``A-B`` pair.
    - ``mixed`` — bare and pair forms coexist, or a bare canonical repeats (the
      signature of a mixed-reference export: distinct references stripped from the
      same electrode).
    - ``unknown`` — no resolved EEG channels at all.

    ``unresolved_channel_count`` counts non-annotation channels with no canonical
    name — the ones the de-identification pass writes as ``MISC_<n>``; ``0`` means
    the cleaner fully normalised the recording. Both values are re-derivable from
    a cleaned file: kept-reference collision forms re-strip to duplicate bares
    (``mixed`` survives a reprocess) and ``MISC_<n>`` labels stay unresolved.
    """
    unresolved = sum(1 for c in channels if not c.is_annotation_channel and not c.canonical_label)
    eeg_canonicals = [c.canonical_label for c in channels if c.signal_type == "eeg" and c.canonical_label]
    if not eeg_canonicals:
        return LAYOUT_UNKNOWN, unresolved
    bares = [c for c in eeg_canonicals if "-" not in c]
    pairs = [c for c in eeg_canonicals if "-" in c]
    if bares and pairs:
        return LAYOUT_MIXED, unresolved
    if pairs:
        return LAYOUT_BIPOLAR, unresolved
    if len(set(bares)) < len(bares):
        return LAYOUT_MIXED, unresolved
    return LAYOUT_REFERENTIAL, unresolved


# --- Canonical channel order ------------------------------------------------

# Version of the canonical channel-order spec below. Stamped on
# ``RecordingMeta.channel_order_version`` at ingest so downstream code never has
# to guess which convention a stored file follows; bump when the ordering rules
# change.
CHANNEL_ORDER_VERSION = 1

# The fixed EEG sequence: anterior to posterior, left/right homologous pairs
# adjacent (lateral before medial, matching clinical display convention), the
# midline electrode after its row's pairs. Nz leads (nasion), Iz closes the
# scalp rows, ear/mastoid reference sites last.
CANONICAL_EEG_ORDER: tuple[str, ...] = (
    "Nz",
    "Fp1",
    "Fp2",
    "Fpz",
    "AF7",
    "AF8",
    "AF3",
    "AF4",
    "AFz",
    "F9",
    "F10",
    "F7",
    "F8",
    "F5",
    "F6",
    "F3",
    "F4",
    "F1",
    "F2",
    "Fz",
    "FT9",
    "FT10",
    "FT7",
    "FT8",
    "FC5",
    "FC6",
    "FC3",
    "FC4",
    "FC1",
    "FC2",
    "FCz",
    "T9",
    "T10",
    "T7",
    "T8",
    "C5",
    "C6",
    "C3",
    "C4",
    "C1",
    "C2",
    "Cz",
    "TP9",
    "TP10",
    "TP7",
    "TP8",
    "CP5",
    "CP6",
    "CP3",
    "CP4",
    "CP1",
    "CP2",
    "CPz",
    "P9",
    "P10",
    "P7",
    "P8",
    "P5",
    "P6",
    "P3",
    "P4",
    "P1",
    "P2",
    "Pz",
    "PO7",
    "PO8",
    "PO3",
    "PO4",
    "POz",
    "O1",
    "O2",
    "Oz",
    "Iz",
    "A1",
    "A2",
    "M1",
    "M2",
)

_EEG_ORDER_RANK: dict[str, int] = {name.upper(): i for i, name in enumerate(CANONICAL_EEG_ORDER)}

# Rank for tokens outside the vocabulary (defensive — cleaned EEG labels are
# built from it, but the sort must not blow up on unexpected input).
_UNRANKED = len(CANONICAL_EEG_ORDER)


def eeg_order_rank(label: str) -> tuple[int, int]:
    """Return the canonical-order sort key for a cleaned EEG channel label.

    Handles the three cleaned-label shapes: a bare electrode (``Fp1``), a
    bipolar pair (``Fp1-F7``), and a kept-reference form (``Fp1-A1`` /
    ``Fp1-REF``). Ranks by the primary electrode first, the suffix second, so a
    bare channel sorts immediately before its derivations and homologous pairs
    stay adjacent. Unknown tokens rank after the whole vocabulary.
    """
    primary, _, suffix = label.partition("-")
    primary_rank = _EEG_ORDER_RANK.get(_PRIME_SUFFIX.sub("", primary.strip().upper()), _UNRANKED)
    suffix_rank = _EEG_ORDER_RANK.get(_PRIME_SUFFIX.sub("", suffix.strip().upper()), _UNRANKED) if suffix else -1
    return (primary_rank, suffix_rank)
