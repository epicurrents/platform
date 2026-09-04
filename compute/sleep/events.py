"""YASA sleep micro-event detection: spindles and slow waves.

Runs ``yasa.spindles_detect`` / ``yasa.sw_detect`` on selected derivations and
returns per-event dicts (the ``.summary()`` rows). Analysis output — event lists,
not a signal transform. Optionally restrict to N2/N3 by passing a per-sample
hypnogram (see :func:`compute.sleep.staging.hypnogram_to_persample`).

``yasa`` is imported lazily. BSD-3 — no gate.
"""

from __future__ import annotations

import numpy as np


def _stack(data_uv, ch_names, channels):
    """Build (array µV, names) for the requested derivation specs."""
    from compute.sleep.montage import derive

    rows = [derive(data_uv, ch_names, spec) for spec in channels]
    return np.vstack(rows), list(channels)


def _summary_to_records(results) -> list[dict]:
    if results is None:
        return []
    df = results.summary()
    if df is None or len(df) == 0:
        return []
    return df.to_dict(orient="records")


def detect_spindles(
    data_uv: np.ndarray,
    srate: float,
    ch_names: list[str],
    *,
    channels: list[str] | None = None,
    hypno_persample: np.ndarray | None = None,
    include=(2, 3),
    freq_sp=(12, 15),
) -> list[dict]:
    """Detect sleep spindles on ``channels`` (default: a single ``C4-M1``).

    Returns one dict per spindle (YASA summary columns: Start, Peak, End,
    Duration, Amplitude, RMS, Frequency, Channel, Stage, …).
    """
    import yasa

    channels = channels or ["C4-M1"]
    data, names = _stack(data_uv, ch_names, channels)
    res = yasa.spindles_detect(
        data,
        sf=float(srate),
        ch_names=names,
        hypno=hypno_persample,
        include=include,
        freq_sp=freq_sp,
        verbose=False,
    )
    return _summary_to_records(res)


def detect_slow_waves(
    data_uv: np.ndarray,
    srate: float,
    ch_names: list[str],
    *,
    channels: list[str] | None = None,
    hypno_persample: np.ndarray | None = None,
    include=(2, 3),
) -> list[dict]:
    """Detect slow waves on ``channels`` (default: a single ``C4-M1``).

    Returns one dict per slow wave (Start, NegPeak, MidCrossing, PosPeak, End,
    Duration, PTP, Slope, Frequency, Channel, Stage, …).
    """
    import yasa

    channels = channels or ["C4-M1"]
    data, names = _stack(data_uv, ch_names, channels)
    res = yasa.sw_detect(
        data,
        sf=float(srate),
        ch_names=names,
        hypno=hypno_persample,
        include=include,
        verbose=False,
    )
    return _summary_to_records(res)
