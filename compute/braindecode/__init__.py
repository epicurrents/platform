"""braindecode inference/serving scaffold for the compute app.

A generic, bring-your-own-weights runtime for [braindecode](https://braindecode.org)
models (BSD-3): load a model (a foundation model via ``from_pretrained`` or a
local checkpoint), scan a recording, and get per-window scores over time.

Unlike a single-purpose detector with a fixed input contract, braindecode's 60+
architectures share no contract, so the contract is carried
explicitly in a :class:`~compute.braindecode.spec.BraindecodeModelSpec` and the
scoring core is generic over it. Nothing is trained here; this is the deployment
target for a checkpoint produced elsewhere (see the "Training facility — path
forward" section of ``README.md``).

Public entry points
-------------------
``BraindecodeModelSpec`` — the model-input contract.
``score_recording(data_uv, srate, ch_names, spec, repo_id=..., checkpoint=...)``
    Scan a microvolt EEG array and return a :class:`~compute.braindecode.detect.BraindecodeResult`.
"""

from compute.braindecode.detect import (
    BraindecodeResult,  # noqa: F401
    WindowScore,  # noqa: F401
    score_recording,  # noqa: F401
)
from compute.braindecode.spec import BraindecodeModelSpec  # noqa: F401
