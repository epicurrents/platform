"""Automated EEG preprocessing / QC — ICLabel artifact removal + autoreject.

Cleaning *transform* stages that complement eigen-subspace and wavelet-domain
denoisers, plus autoreject QC:

* ``clean_with_iclabel`` — ICA + ICLabel component removal (continuous cleaner).
* ``bad_channels_ransac`` / ``interpolate_bad_channels`` — bad-channel detect/repair.
* ``epoch_reject_log`` / ``reject_log_to_spans`` — per-epoch reject log → bad-time spans.

Both underlying libraries (`mne-icalabel`, `autoreject`) are BSD-3 — no
non-commercial gate. They are imported lazily; install with the
``requirements-preprocess.txt`` extra (see the module README). Server-side today;
the README documents a possible Pyodide port later.
"""

from compute.cleaning._mne import positioned_channels  # noqa: F401
from compute.cleaning.iclabel import (
    DEFAULT_KEEP,  # noqa: F401
    ICLABEL_LABELS,  # noqa: F401
    IclabelResult,  # noqa: F401
    clean_with_iclabel,  # noqa: F401
    select_exclude,  # noqa: F401
)
from compute.cleaning.reject import (
    RejectLogResult,  # noqa: F401
    bad_channels_ransac,  # noqa: F401
    epoch_reject_log,  # noqa: F401
    interpolate_bad_channels,  # noqa: F401
    reject_log_to_spans,  # noqa: F401
)
