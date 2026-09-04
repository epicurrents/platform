"""YASA sleep staging + micro-event detection, with 10-20 remontaging.

YASA (BSD-3, bundled pretrained weights, no gate) wants a limited montage — a
central-to-mastoid derivation (``C4-M1``) plus optional EOG/EMG — so this module
front-ends it with remontaging that derives those channels from a full-head,
referential ambulatory EEG.

Entry points
------------
``stage_sleep(data_uv, srate, ch_names, eeg="C4-M1", ...)`` -> hypnogram.
``detect_spindles`` / ``detect_slow_waves`` -> event lists.
``derive`` / ``build_derivations`` -> remontaging (reusable, MNE-free).

Analysis stage: emits a hypnogram + events (annotations), not a cleaned signal.
"""

from compute.sleep.events import detect_slow_waves, detect_spindles  # noqa: F401
from compute.sleep.montage import build_derivations, derive, resolve_index  # noqa: F401
from compute.sleep.staging import (
    StagingResult,  # noqa: F401
    hypnogram_to_persample,  # noqa: F401
    stage_sleep,  # noqa: F401
)
