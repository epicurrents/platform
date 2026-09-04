"""Tests for compute.sleep.

Remontaging + hypnogram helpers are pure and run everywhere; the YASA staging /
event-detection tests are guarded by importorskip.
"""

import numpy as np
import pytest

from compute.sleep.montage import build_derivations, derive
from compute.sleep.staging import hypnogram_to_persample

# ---- remontaging (pure) ----------------------------------------------------


def _rec(ch_names, n=1000, seed=0):
    rng = np.random.default_rng(seed)
    return rng.standard_normal((len(ch_names), n)) * 10.0, list(ch_names)


def test_derive_bipolar_is_difference():
    data, ch = _rec(["C4", "M1", "C3", "M2"])
    got = derive(data, ch, "C4-M1")
    np.testing.assert_array_equal(got, data[0] - data[1])


def test_derive_resolves_mastoid_earlobe_alias():
    # Recording labels the reference A1 (earlobe); spec asks for M1 (mastoid).
    data, ch = _rec(["C4", "A1"])
    np.testing.assert_array_equal(derive(data, ch, "C4-M1"), data[0] - data[1])


def test_derive_strips_edf_label_decoration():
    data, ch = _rec(["EEG C4-REF", "EEG M1-REF"])
    np.testing.assert_array_equal(derive(data, ch, "C4-M1"), data[0] - data[1])


def test_derive_exact_existing_bipolar_channel():
    data, ch = _rec(["C4-M1", "C3-M2"])
    np.testing.assert_array_equal(derive(data, ch, "C4-M1"), data[0])


def test_old_new_temporal_naming_alias():
    data, ch = _rec(["T7", "M2"])  # T7 == T3
    np.testing.assert_array_equal(derive(data, ch, "T3-M2"), data[0] - data[1])


def test_missing_electrode_raises():
    data, ch = _rec(["C4", "M1"])
    with pytest.raises(ValueError, match="C3"):
        derive(data, ch, "C3-M2")


def test_build_derivations_types_and_skip():
    data, ch = _rec(["C4", "M1", "E1", "M2"])
    arr, names, types = build_derivations(data, ch, {"eeg": "C4-M1", "eog": "E1-M2", "emg": None})
    assert arr.shape == (2, data.shape[1])
    assert names == ["EEG", "EOG"] and types == ["eeg", "eog"]


def test_hypnogram_to_persample():
    stages = np.array([0, 2, 4])  # WAKE, N2, REM
    up = hypnogram_to_persample(stages, srate=2.0, n_samples=180, epoch_seconds=30.0)
    assert up.shape == (180,)
    assert up[0] == 0 and up[60] == 2 and up[120] == 4


# ---- YASA integration (guarded) -------------------------------------------


def _psg(minutes=15, sf=100.0, seed=1):
    rng = np.random.default_rng(seed)
    ch = ["C4", "M1", "C3", "M2", "E1"]
    data = rng.standard_normal((len(ch), int(minutes * 60 * sf))) * 20.0
    return data, ch, sf


def test_stage_sleep_runs():
    pytest.importorskip("yasa")
    from compute.sleep.staging import stage_sleep

    data, ch, sf = _psg()
    res = stage_sleep(data, sf, ch, eeg="C4-M1", eog="E1-M2", age=40, male=1)
    assert len(res.stages) == res.stages_int.size == res.epoch_onsets_s.size
    assert res.stages_int.size >= 20  # ~30 epochs for 15 min
    assert all(s in ("WAKE", "N1", "N2", "N3", "REM", "ART", "UNS") for s in res.stages)


def test_detect_spindles_runs():
    pytest.importorskip("yasa")
    from compute.sleep.events import detect_spindles

    data, ch, sf = _psg(minutes=5)
    events = detect_spindles(data, sf, ch, channels=["C4-M1"])
    assert isinstance(events, list)  # may be empty on synthetic noise
