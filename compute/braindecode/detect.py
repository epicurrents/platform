"""Generic braindecode inference core — dependency-light, no torch, no Django.

Turns a microvolt EEG array into the ``(n_windows, n_chans, n_times)`` batches a
braindecode model expects (per its :class:`~compute.braindecode.spec.BraindecodeModelSpec`),
runs an injected predictor, and returns per-window scores over time — plus, for a
nominated class, a per-sample score and threshold-crossing events.

The torch model is loaded by :mod:`compute.braindecode.model` and injected as a
predictor exposing ``.predict(batch_np) -> np.ndarray`` of shape
``(n_windows, n_outputs)``, so this module has no torch import and is unit-testable
with a stub. numpy is the only hard dependency; scipy (resample) and mne (filter)
are imported lazily only when the spec asks for them.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from compute.braindecode.spec import BraindecodeModelSpec


@dataclass
class WindowScore:
    """One detected event for a nominated positive class (optional output)."""

    onset_s: float
    peak_s: float
    duration_s: float
    peak_prob: float


@dataclass
class BraindecodeResult:
    """Result of scoring a recording with a braindecode model."""

    window_scores: np.ndarray
    """Per-window model output, shape ``(n_windows, n_outputs)``."""
    window_onsets_s: np.ndarray
    """Onset time (s) of each window, shape ``(n_windows,)``."""
    fs: float
    """Sampling rate the model ran at (spec.sfreq)."""
    spec_name: str
    per_sample: np.ndarray | None = None
    """Per-sample score for ``spec.positive_index`` (None if not requested)."""
    events: list[WindowScore] = field(default_factory=list)
    threshold: float | None = None


def _resolve_channels(ch_names: list[str], wanted: tuple[str, ...]) -> list[int]:
    """Row indices of ``wanted`` labels within ``ch_names`` (case-insensitive)."""
    lower = {}
    for i, name in enumerate(ch_names):
        lower.setdefault(name.strip().lower(), i)
    idx, missing = [], []
    for ch in wanted:
        j = lower.get(ch.strip().lower())
        (missing if j is None else idx).append(ch if j is None else j)
    if missing:
        raise ValueError("Recording is missing channels the model expects: " + ", ".join(str(m) for m in missing))
    return idx


def _resample(data_uv: np.ndarray, srate: float, target: float) -> np.ndarray:
    if srate == target:
        return np.asarray(data_uv, dtype=np.float64)
    from math import gcd

    from scipy.signal import resample_poly

    g = gcd(round(srate), round(target))
    return resample_poly(np.asarray(data_uv, dtype=np.float64), round(target) // g, round(srate) // g, axis=1)


def _filter(x: np.ndarray, sfreq: float, spec: BraindecodeModelSpec) -> np.ndarray:
    if spec.bandpass is None and spec.notch_hz is None:
        return x
    from mne.filter import filter_data, notch_filter

    if spec.bandpass is not None:
        lo, hi = spec.bandpass
        x = filter_data(x, sfreq=sfreq, l_freq=lo, h_freq=hi, verbose="ERROR")
    if spec.notch_hz is not None:
        if not 0 < spec.notch_hz < sfreq / 2:
            raise ValueError(f"notch_hz={spec.notch_hz} not below Nyquist ({sfreq / 2} Hz).")
        x = notch_filter(x, Fs=sfreq, freqs=spec.notch_hz, verbose="ERROR")
    return x


def _normalize_window(win: np.ndarray, mode: str) -> np.ndarray:
    """Per-channel normalization of a ``(n_chans, n_times)`` window."""
    if mode == "none":
        return win
    x = win.astype(np.float64)
    if mode == "zscore":
        mu = x.mean(axis=1, keepdims=True)
        sd = x.std(axis=1, keepdims=True) + 1e-8
        return (x - mu) / sd
    if mode == "percentile":
        return x / (np.quantile(np.abs(x), 0.95, axis=1, keepdims=True) + 1e-8)
    if mode == "exp_moving":
        # Lightweight approximation of braindecode's exponential_moving_standardize
        # along time; for exact parity use braindecode.preprocessing on an mne.Raw.
        alpha = 0.001
        out = np.empty_like(x)
        mean = x[:, :1].copy()
        var = np.ones_like(mean)
        for t in range(x.shape[1]):
            col = x[:, t : t + 1]
            mean = alpha * col + (1 - alpha) * mean
            var = alpha * (col - mean) ** 2 + (1 - alpha) * var
            out[:, t : t + 1] = (col - mean) / (np.sqrt(var) + 1e-4)
        return out
    return win  # unreachable (validated in spec)


def preprocess(data_uv: np.ndarray, srate: float, ch_names: list[str], spec: BraindecodeModelSpec) -> np.ndarray:
    """Resample, pick/reorder channels, and filter — returns ``(n_chans, n)``."""
    x = _resample(data_uv, srate, spec.sfreq)
    if spec.channels is not None:
        idx = _resolve_channels(ch_names, spec.channels)
        x = x[idx]
    else:
        if len(ch_names) < spec.n_chans:
            raise ValueError(f"Recording has {len(ch_names)} channels; model needs {spec.n_chans}.")
        x = x[: spec.n_chans]
    if x.shape[0] != spec.n_chans:
        raise ValueError(f"Selected {x.shape[0]} channels; spec.n_chans={spec.n_chans}.")
    return _filter(x, spec.sfreq, spec)


def _softmax(a: np.ndarray) -> np.ndarray:
    a = a - a.max(axis=-1, keepdims=True)
    e = np.exp(a)
    return e / e.sum(axis=-1, keepdims=True)


def extract_events(
    per_sample: np.ndarray, fs: float, threshold: float, min_duration_s: float = 0.0
) -> list[WindowScore]:
    """Contiguous supra-threshold runs of a per-sample score -> events."""
    mask = per_sample >= threshold
    if not mask.any():
        return []
    edges = np.diff(mask.astype(np.int8))
    starts = np.flatnonzero(edges == 1) + 1
    ends = np.flatnonzero(edges == -1) + 1
    if mask[0]:
        starts = np.r_[0, starts]
    if mask[-1]:
        ends = np.r_[ends, mask.size]
    out = []
    for s, e in zip(starts, ends):
        dur = (e - s) / fs
        if dur < min_duration_s:
            continue
        seg = per_sample[s:e]
        k = int(np.argmax(seg))
        out.append(WindowScore(onset_s=s / fs, peak_s=(s + k) / fs, duration_s=dur, peak_prob=float(seg[k])))
    return out


def score_recording(
    data_uv: np.ndarray,
    srate: float,
    ch_names: list[str],
    spec: BraindecodeModelSpec,
    *,
    model=None,
    repo_id: str | None = None,
    checkpoint: str | None = None,
    threshold: float = 0.5,
    batch_size: int = 256,
) -> BraindecodeResult:
    """Scan a recording with a braindecode model and return per-window scores.

    ``model`` is a predictor with ``.predict(batch_np) -> (n_windows, n_outputs)``;
    if ``None`` it is loaded via :func:`compute.braindecode.model.load_model` from
    ``repo_id`` (HuggingFace, ``from_pretrained``) or a local ``checkpoint``.
    """
    x = preprocess(data_uv, srate, ch_names, spec)  # (n_chans, n)
    n_samples = x.shape[1]
    w = spec.window_samples()
    if n_samples < w:
        raise ValueError(f"Recording shorter than the {w}-sample model window.")

    if model is None:
        from compute.braindecode.model import load_model

        model = load_model(spec, repo_id=repo_id, checkpoint=checkpoint)

    step = max(1, round(spec.hop_seconds * spec.sfreq))
    starts = np.arange(0, n_samples - w + 1, step)

    scores = np.empty((starts.size, spec.n_outputs), dtype=np.float64)
    for b in range(0, starts.size, batch_size):
        chunk = starts[b : b + batch_size]
        batch = np.stack(
            [_normalize_window(x[:, s : s + w], spec.normalization) for s in chunk], axis=0
        )  # (chunk, n_chans, n_times)
        out = np.asarray(model.predict(batch), dtype=np.float64)
        if out.ndim == 1:
            out = out[:, None]
        if spec.output == "logits" and spec.n_outputs > 1:
            out = _softmax(out)
        scores[b : b + chunk.size] = out[: chunk.size]

    result = BraindecodeResult(
        window_scores=scores,
        window_onsets_s=starts / spec.sfreq,
        fs=float(spec.sfreq),
        spec_name=spec.name,
    )

    if spec.positive_index is not None:
        centers = starts + w // 2
        pos = scores[:, spec.positive_index]
        per_sample = np.interp(np.arange(n_samples), centers, pos, left=pos[0], right=pos[-1])
        result.per_sample = per_sample.astype(np.float32)
        result.events = extract_events(per_sample, spec.sfreq, threshold)
        result.threshold = threshold
    return result
