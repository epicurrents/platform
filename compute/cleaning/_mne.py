"""Shared MNE helpers for the cleaning stages — no Django, MNE imported lazily.

The platform convention is to work in **microvolts**;
MNE works in volts, so these helpers convert on the way in and callers convert
back out (``raw.get_data() * 1e6``). ICLabel and autoreject both need electrode
positions, so a montage is required and validated up front.
"""

from __future__ import annotations


def positioned_channels(ch_names: list[str], montage: str = "standard_1020") -> list[str]:
    """Subset of ``ch_names`` that have positions in the named standard montage.

    Case-insensitive. Use this to select the EEG channels a cleaning stage can
    process, leaving polygraphic / unpositioned channels for pass-through.
    """
    import mne

    known = {n.lower() for n in mne.channels.make_standard_montage(montage).ch_names}
    return [c for c in ch_names if c.strip().lower() in known]


def build_raw(data_uv, srate: float, ch_names: list[str], montage: str = "standard_1020"):
    """Build an EEG ``mne.io.RawArray`` (volts) with the montage set.

    ``data_uv`` is microvolts, shape ``(n_channels, n_samples)``. Every channel
    must have a position in ``montage`` (ICLabel/interpolation need them) — a
    missing one raises ``ValueError`` naming it. Pre-filter with
    :func:`positioned_channels` to avoid that.
    """
    import mne
    import numpy as np

    info = mne.create_info(list(ch_names), sfreq=float(srate), ch_types="eeg")
    raw = mne.io.RawArray(np.asarray(data_uv, dtype=np.float64) * 1e-6, info, verbose="ERROR")
    m = mne.channels.make_standard_montage(montage)
    try:
        raw.set_montage(m, on_missing="raise", match_case=False, verbose="ERROR")
    except ValueError as exc:
        raise ValueError(
            f"Some channels have no position in montage {montage!r}: {exc}. "
            "Select EEG channels with compute.cleaning.positioned_channels() first."
        ) from exc
    return raw
