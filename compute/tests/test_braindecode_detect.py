"""Tests for the braindecode scoring core — no torch, no weights required.

Drives the generic core with a stub predictor to cover the spec contract,
channel resolution/reordering, resampling, windowing, softmax/probs handling,
normalization, and the optional per-sample/events path.
"""

import numpy as np
import pytest

from compute.braindecode.detect import score_recording
from compute.braindecode.spec import BraindecodeModelSpec

CHANS = ["Fp1", "Fp2", "C3", "C4", "O1", "O2"]


def _rec(n_samples=2000, seed=0, chans=CHANS):
    rng = np.random.default_rng(seed)
    return rng.standard_normal((len(chans), n_samples)) * 20.0, list(chans)


class _StubProbs:
    """Two-class model that returns a fixed probability for class 1."""

    def __init__(self, p1=0.8):
        self.p1 = p1

    def predict(self, batch):
        n = batch.shape[0]
        return np.column_stack([np.full(n, 1 - self.p1), np.full(n, self.p1)])


def _spec(**kw):
    base = {
        "name": "t",
        "n_chans": len(CHANS),
        "sfreq": 100.0,
        "channels": tuple(CHANS),
        "window_seconds": 1.0,
        "hop_seconds": 0.5,
        "n_outputs": 2,
        "output": "probs",
    }
    base.update(kw)
    return BraindecodeModelSpec(**base)


def test_spec_requires_exactly_one_window_length():
    with pytest.raises(ValueError):
        BraindecodeModelSpec(name="t", n_chans=6, sfreq=100.0)  # neither given
    with pytest.raises(ValueError):
        BraindecodeModelSpec(name="t", n_chans=6, sfreq=100.0, n_times=128, window_seconds=1.0)


def test_scores_shape_and_onsets():
    data, ch = _rec(n_samples=1000)
    res = score_recording(data, 100.0, ch, _spec(), model=_StubProbs())
    assert res.window_scores.shape[1] == 2
    assert res.window_scores.shape[0] == res.window_onsets_s.shape[0] > 0
    assert res.fs == 100.0


def test_channel_reordering_by_label():
    # Recording channels in a different order than the spec asks for.
    data, ch = _rec(n_samples=600, chans=["O2", "O1", "C4", "C3", "Fp2", "Fp1"])
    # spec wants CHANS order; core must reorder, not fail.
    res = score_recording(data, 100.0, ch, _spec(), model=_StubProbs())
    assert res.window_scores.shape[0] > 0


def test_missing_channel_raises():
    data, ch = _rec(n_samples=600, chans=CHANS[:-1])  # drop O2
    with pytest.raises(ValueError, match="missing channels"):
        score_recording(data, 100.0, ch, _spec(), model=_StubProbs())


def test_resample_changes_window_grid():
    data, ch = _rec(n_samples=2000)  # 200 Hz-worth of samples fed as 200 Hz
    res = score_recording(data, 200.0, ch, _spec(), model=_StubProbs())
    # Resampled to spec.sfreq=100 -> ~1000 samples -> windows exist.
    assert res.window_scores.shape[0] > 0


def test_logits_get_softmaxed():
    data, ch = _rec(n_samples=600)

    class _Logits:
        def predict(self, batch):
            n = batch.shape[0]
            return np.column_stack([np.full(n, 2.0), np.full(n, 0.0)])  # raw logits

    res = score_recording(data, 100.0, ch, _spec(output="logits"), model=_Logits())
    # After softmax, rows sum to 1 and class 0 > class 1.
    np.testing.assert_allclose(res.window_scores.sum(axis=1), 1.0, atol=1e-6)
    assert (res.window_scores[:, 0] > res.window_scores[:, 1]).all()


def test_positive_index_yields_per_sample_and_events():
    data, ch = _rec(n_samples=1500)
    spec = _spec(positive_index=1)
    res = score_recording(data, 100.0, ch, spec, model=_StubProbs(p1=0.9), threshold=0.5)
    assert res.per_sample is not None
    assert res.per_sample.shape[0] == 1500
    assert len(res.events) >= 1  # p1=0.9 > 0.5 everywhere


def test_normalization_modes_run():
    data, ch = _rec(n_samples=800)
    for mode in ("none", "zscore", "percentile", "exp_moving"):
        res = score_recording(data, 100.0, ch, _spec(normalization=mode), model=_StubProbs())
        assert res.window_scores.shape[0] > 0
