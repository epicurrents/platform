"""Remontaging: derive the limited montage YASA needs from a full 10-20 EEG.

YASA sleep staging wants a single **central-to-mastoid** derivation (``C4-M1`` or
``C3-M2``), optionally an EOG and chin EMG. Ambulatory / long full-head EEGs are
10-20 and usually *referential*, so the channel must be derived: for any common
reference, ``(C4-ref) - (M1-ref) = C4-M1``, so a bipolar derivation is just the
difference of two referential channels — independent of the original reference.

Pure numpy + label logic (no MNE / YASA), so it is unit-testable. Handles the
common label variations: modality prefixes (``EEG C4-REF``), case, mastoid vs
earlobe (``M1``/``A1``, ``M2``/``A2``), and old↔new temporal naming
(``T3``/``T7`` etc.).
"""

from __future__ import annotations

import numpy as np

# Interchangeable electrode labels. Mastoid (M) and earlobe (A) references are
# treated as equivalent for derivation purposes (clinically near-identical, and
# used interchangeably as references) — a small approximation, documented.
_ALIAS_GROUPS = [
    {"m1", "a1"},
    {"m2", "a2"},
    {"t3", "t7"},
    {"t4", "t8"},
    {"t5", "p7"},
    {"t6", "p8"},
]
_ALIASES = {name: grp for grp in _ALIAS_GROUPS for name in grp}


def _bare(label: str) -> str:
    """Strip a modality prefix and common reference suffix from an EDF label.

    ``"EEG C4-REF"`` -> ``"c4"``, ``"EOG LOC"`` -> ``"loc"``. Conservative: only
    strips well-known conventions so it doesn't mangle genuine derivations.
    """
    n = label.strip().lower()
    for pre in ("eeg ", "eog ", "emg ", "ecg "):
        if n.startswith(pre):
            n = n[len(pre) :]
            break
    for suf in ("-ref", "-le", "-ee"):
        if n.endswith(suf):
            n = n[: -len(suf)]
            break
    return n.strip()


def resolve_index(ch_names: list[str], name: str) -> int:
    """Index of electrode ``name`` in ``ch_names`` (case/alias/label-tolerant)."""
    key = name.strip().lower()
    want = _ALIASES.get(key, {key})
    for i, c in enumerate(ch_names):
        if {c.strip().lower(), _bare(c)} & want:
            return i
    raise ValueError(
        f"Electrode {name!r} not found in recording (aliases tried: {sorted(want)}). "
        "Channels available: " + ", ".join(ch_names[:24]) + ("…" if len(ch_names) > 24 else "")
    )


def derive(data_uv: np.ndarray, ch_names: list[str], spec: str) -> np.ndarray:
    """Return the 1-D signal for a derivation ``spec``.

    ``spec`` is either an existing channel name (returned as-is) or a bipolar
    derivation ``"ANODE-CATHODE"`` (e.g. ``"C4-M1"``), computed as
    ``anode - cathode`` from referential channels. Microvolts in, microvolts out.
    """
    # Exact existing-channel match first (handles already-bipolar 'C4-M1' channels).
    for i, c in enumerate(ch_names):
        if c.strip().lower() == spec.strip().lower():
            return np.asarray(data_uv[i], dtype=np.float64)
    if "-" in spec:
        anode, cathode = spec.split("-", 1)
        ia = resolve_index(ch_names, anode)
        ic = resolve_index(ch_names, cathode)
        return np.asarray(data_uv[ia], dtype=np.float64) - np.asarray(data_uv[ic], dtype=np.float64)
    return np.asarray(data_uv[resolve_index(ch_names, spec)], dtype=np.float64)


def build_derivations(
    data_uv: np.ndarray, ch_names: list[str], specs: dict[str, str | None]
) -> tuple[np.ndarray, list[str], list[str]]:
    """Build the derivation array for a set of typed specs.

    ``specs`` maps MNE channel type -> derivation spec (or None to skip), e.g.
    ``{"eeg": "C4-M1", "eog": "E1-M2", "emg": None}``. Returns
    ``(array (n_derivs, n_samples) µV, names, ch_types)`` — names are the
    upper-cased types (``"EEG"``, ``"EOG"``, ``"EMG"``) YASA is pointed at.
    """
    rows, names, types = [], [], []
    for ch_type, spec in specs.items():
        if not spec:
            continue
        rows.append(derive(data_uv, ch_names, spec))
        names.append(ch_type.upper())
        types.append(ch_type)
    if not rows:
        raise ValueError("No derivations requested (at least an EEG spec is required).")
    return np.vstack(rows), names, types
