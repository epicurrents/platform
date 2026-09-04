"""autoreject-based QC and channel repair for continuous EEG.

`autoreject` (BSD-3) is epoch-based, so continuous data is cut into fixed-length
epochs first (``mne.make_fixed_length_epochs``). Three uses:

* :func:`bad_channels_ransac` — RANSAC bad-channel detection (analysis/QC).
* :func:`interpolate_bad_channels` — interpolate those channels (a `transform`
  stage: ``raw -> raw``, deterministic given the bad set).
* :func:`epoch_reject_log` — per-epoch good/bad log (analysis; maps to bad-time
  annotations via :func:`reject_log_to_spans`).

`mne` and `autoreject` are imported lazily. No non-commercial gate (BSD-3).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class RejectLogResult:
    bad_epochs: np.ndarray
    """Bool array ``(n_epochs,)`` — True where autoreject dropped the epoch."""
    epoch_onsets_s: np.ndarray
    epoch_seconds: float
    n_bad_epochs: int
    ch_names: list[str]


def bad_channels_ransac(
    data_uv: np.ndarray,
    srate: float,
    ch_names: list[str],
    *,
    montage: str = "standard_1020",
    epoch_seconds: float = 2.0,
    random_state: int = 42,
) -> list[str]:
    """Detect globally-bad channels with RANSAC (``autoreject.Ransac``)."""
    import mne
    from autoreject import Ransac

    from compute.cleaning._mne import build_raw

    raw = build_raw(data_uv, srate, ch_names, montage)
    epochs = mne.make_fixed_length_epochs(raw, duration=epoch_seconds, preload=True, verbose="ERROR")
    ransac = Ransac(random_state=random_state, n_jobs=1, verbose=False)
    ransac.fit(epochs)
    return list(ransac.bad_chs_)


def interpolate_bad_channels(
    data_uv: np.ndarray,
    srate: float,
    ch_names: list[str],
    *,
    montage: str = "standard_1020",
    bads: list[str] | None = None,
    epoch_seconds: float = 2.0,
    random_state: int = 42,
) -> tuple[np.ndarray, list[str]]:
    """Interpolate bad channels; detect them via RANSAC if ``bads`` is None.

    Returns ``(cleaned_uv, bads)`` with ``cleaned_uv`` in microvolts.
    """
    from compute.cleaning._mne import build_raw

    raw = build_raw(data_uv, srate, ch_names, montage)
    if bads is None:
        bads = bad_channels_ransac(
            data_uv, srate, ch_names, montage=montage, epoch_seconds=epoch_seconds, random_state=random_state
        )
    raw.info["bads"] = list(bads)
    if bads:
        raw.interpolate_bads(reset_bads=True, verbose="ERROR")
    return raw.get_data() * 1e6, list(bads)


def epoch_reject_log(
    data_uv: np.ndarray,
    srate: float,
    ch_names: list[str],
    *,
    montage: str = "standard_1020",
    epoch_seconds: float = 2.0,
    random_state: int = 42,
) -> RejectLogResult:
    """Per-epoch reject log via ``autoreject.AutoReject`` on fixed-length epochs."""
    import mne
    from autoreject import AutoReject

    from compute.cleaning._mne import build_raw

    raw = build_raw(data_uv, srate, ch_names, montage)
    epochs = mne.make_fixed_length_epochs(raw, duration=epoch_seconds, preload=True, verbose="ERROR")
    ar = AutoReject(random_state=random_state, n_jobs=1, verbose=False)
    _, log = ar.fit_transform(epochs, return_log=True)
    bad = np.asarray(log.bad_epochs, dtype=bool)
    return RejectLogResult(
        bad_epochs=bad,
        epoch_onsets_s=np.arange(bad.size) * epoch_seconds,
        epoch_seconds=epoch_seconds,
        n_bad_epochs=int(bad.sum()),
        ch_names=list(log.ch_names),
    )


def reject_log_to_spans(bad_epochs, epoch_seconds: float) -> list[tuple[float, float]]:
    """Merge consecutive bad epochs into ``(onset_s, duration_s)`` spans.

    Pure (no MNE) — unit-testable. Useful for turning a reject log into bad-time
    annotations on the recording.
    """
    bad = np.asarray(bad_epochs, dtype=bool)
    if not bad.any():
        return []
    edges = np.diff(bad.astype(np.int8))
    starts = np.flatnonzero(edges == 1) + 1
    ends = np.flatnonzero(edges == -1) + 1
    if bad[0]:
        starts = np.r_[0, starts]
    if bad[-1]:
        ends = np.r_[ends, bad.size]
    return [(float(s * epoch_seconds), float((e - s) * epoch_seconds)) for s, e in zip(starts, ends)]
