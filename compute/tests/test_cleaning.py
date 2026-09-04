"""Tests for compute.cleaning.

Pure-logic tests (component-exclude decision, reject-log spans) run everywhere.
The end-to-end ICLabel / autoreject tests are guarded by importorskip so they
run where the optional libs are installed and skip cleanly otherwise.
"""

import numpy as np
import pytest

from compute.cleaning.iclabel import DEFAULT_KEEP, select_exclude
from compute.cleaning.reject import reject_log_to_spans

# ---- pure logic (no MNE / optional libs) ----------------------------------


def test_select_exclude_keeps_brain_and_other():
    labels = ["brain", "eye blink", "other", "muscle artifact", "line noise"]
    probas = [0.9, 0.95, 0.6, 0.88, 0.99]
    assert select_exclude(labels, probas, DEFAULT_KEEP) == [1, 3, 4]


def test_select_exclude_probability_threshold():
    labels = ["eye blink", "muscle artifact"]
    probas = [0.95, 0.4]
    # Only the confident artifact (>=0.7) is excluded.
    assert select_exclude(labels, probas, DEFAULT_KEEP, prob_threshold=0.7) == [0]


def test_select_exclude_custom_keep():
    labels = ["brain", "eye blink", "heart beat"]
    probas = [0.9, 0.9, 0.9]
    # Keep only brain -> eye blink and heart beat excluded.
    assert select_exclude(labels, probas, keep_labels=("brain",)) == [1, 2]


def test_reject_log_to_spans_merges_consecutive():
    bad = np.array([0, 1, 1, 0, 0, 1, 0], dtype=bool)
    spans = reject_log_to_spans(bad, epoch_seconds=2.0)
    # epochs 1-2 -> 2.0..6.0 (dur 4.0); epoch 5 -> 10.0..12.0 (dur 2.0)
    assert spans == [(2.0, 4.0), (10.0, 2.0)]


def test_reject_log_to_spans_empty_and_edges():
    assert reject_log_to_spans(np.zeros(5, bool), 1.0) == []
    assert reject_log_to_spans(np.ones(3, bool), 1.0) == [(0.0, 3.0)]


# ---- guarded integration (need mne + the optional libs) --------------------


def _synthetic_eeg(n_ch=8, seconds=20, sfreq=128.0, seed=0):
    """A few standard-1020 channels of noise so ICA/RANSAC have something to fit."""
    rng = np.random.default_rng(seed)
    ch_names = ["Fp1", "Fp2", "F3", "F4", "C3", "C4", "O1", "O2"][:n_ch]
    data_uv = rng.standard_normal((n_ch, int(seconds * sfreq))) * 20.0
    return data_uv, ch_names, sfreq


def test_iclabel_clean_roundtrip():
    pytest.importorskip("mne")
    pytest.importorskip("mne_icalabel")
    from compute.cleaning.iclabel import clean_with_iclabel

    data_uv, ch_names, sfreq = _synthetic_eeg()
    res = clean_with_iclabel(data_uv, sfreq, ch_names, n_components=6, random_state=7)
    assert res.cleaned_uv.shape == data_uv.shape
    assert len(res.components) == res.n_components
    assert all(
        c["label"] in ("brain", "muscle artifact", "eye blink", "heart beat", "line noise", "channel noise", "other")
        for c in res.components
    )


def test_ransac_and_reject_log():
    pytest.importorskip("mne")
    pytest.importorskip("autoreject")
    from compute.cleaning.reject import bad_channels_ransac, epoch_reject_log

    data_uv, ch_names, sfreq = _synthetic_eeg(seconds=30)
    bads = bad_channels_ransac(data_uv, sfreq, ch_names, epoch_seconds=2.0)
    assert isinstance(bads, list)  # may be empty on clean synthetic data
    log = epoch_reject_log(data_uv, sfreq, ch_names, epoch_seconds=2.0)
    assert log.bad_epochs.dtype == bool
    assert log.epoch_onsets_s.shape == log.bad_epochs.shape
