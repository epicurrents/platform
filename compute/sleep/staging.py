"""YASA automated sleep staging on a (remontaged) recording.

Derives the montage YASA needs (see :mod:`compute.sleep.montage`), builds a small
MNE ``RawArray``, and runs ``yasa.SleepStaging`` — which downsamples to 100 Hz,
band-passes, extracts features, and predicts a 30-second-epoch hypnogram with a
bundled pretrained classifier (no download, no GPU). YASA 0.7.0 returns a
``yasa.Hypnogram`` object; this wraps it into a plain result. BSD-3 — no gate.

This is an **analysis** stage: it emits a hypnogram (stage per epoch), not a
cleaned signal. ``yasa``/``mne`` are imported lazily.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

# Fallback string->int map for pre-0.7 YASA that returns a string ndarray.
_STAGE_INT = {"W": 0, "WAKE": 0, "N1": 1, "N2": 2, "N3": 3, "REM": 4, "ART": -1, "UNS": -2}


@dataclass
class StagingResult:
    stages: list[str]
    """Stage per 30 s epoch: 'WAKE' / 'N1' / 'N2' / 'N3' / 'REM'."""
    stages_int: np.ndarray
    """Integer codes (WAKE=0, N1=1, N2=2, N3=3, REM=4; ART=-1, UNS=-2)."""
    epoch_onsets_s: np.ndarray
    """Onset (s) of each 30 s epoch."""
    eeg_derivation: str
    proba: np.ndarray | None = None
    """Per-epoch class probabilities ``(n_epochs, n_classes)`` if available."""
    proba_classes: list[str] = field(default_factory=list)


def stage_sleep(
    data_uv: np.ndarray,
    srate: float,
    ch_names: list[str],
    *,
    eeg: str = "C4-M1",
    eog: str | None = None,
    emg: str | None = None,
    age: float | None = None,
    male: bool | None = None,
) -> StagingResult:
    """Stage sleep from a microvolt EEG array, remontaging to what YASA expects.

    Parameters
    ----------
    eeg, eog, emg
        Derivation specs (see :func:`compute.sleep.montage.derive`) — e.g.
        ``eeg="C4-M1"``. EEG-only staging works; EOG/EMG improve accuracy.
    age, male
        Optional subject metadata (years; ``male`` 1/True or 0/False) — YASA uses
        it to improve staging.
    """
    import mne

    from compute.sleep.montage import build_derivations

    arr_uv, names, types = build_derivations(data_uv, ch_names, {"eeg": eeg, "eog": eog, "emg": emg})
    info = mne.create_info(names, sfreq=float(srate), ch_types=types)
    raw = mne.io.RawArray(arr_uv * 1e-6, info, verbose="ERROR")  # µV -> V

    metadata = {}
    if age is not None:
        metadata["age"] = float(age)
    if male is not None:
        metadata["male"] = int(bool(male))

    import yasa

    sls = yasa.SleepStaging(
        raw,
        eeg_name="EEG",
        eog_name="EOG" if eog else None,
        emg_name="EMG" if emg else None,
        metadata=metadata or None,
    )
    sls.fit()  # 0.7.0's fit() returns None, so don't chain
    pred = sls.predict()

    proba = None
    proba_classes: list[str] = []
    if hasattr(pred, "hypno"):  # YASA >= 0.7.0 Hypnogram
        stages = [str(s) for s in pred.hypno.tolist()]
        stages_int = np.asarray(pred.as_int(), dtype=int).ravel()
        proba_df = getattr(pred, "proba", None)
        if proba_df is not None:
            proba = np.asarray(proba_df.to_numpy(), dtype=np.float64)
            proba_classes = [str(c) for c in proba_df.columns]
    else:  # older YASA: ndarray of stage strings
        stages = [str(s) for s in np.asarray(pred).ravel()]
        stages_int = np.array([_STAGE_INT.get(s.upper(), -2) for s in stages], dtype=int)

    return StagingResult(
        stages=stages,
        stages_int=stages_int,
        epoch_onsets_s=np.arange(len(stages)) * 30.0,
        eeg_derivation=eeg,
        proba=proba,
        proba_classes=proba_classes,
    )


def hypnogram_to_persample(
    stages_int: np.ndarray, srate: float, n_samples: int, epoch_seconds: float = 30.0
) -> np.ndarray:
    """Upsample a per-epoch hypnogram to a per-sample int array (for event detect)."""
    per = round(epoch_seconds * srate)
    up = np.repeat(np.asarray(stages_int, dtype=int), per)
    if up.size < n_samples:
        up = np.r_[up, np.full(n_samples - up.size, up[-1] if up.size else -2, dtype=int)]
    return up[:n_samples]
