"""ICLabel-based automatic ICA artifact removal — continuous EEG cleaner.

Fits extended-Infomax ICA, classifies each component with ICLabel
(`mne-icalabel`, BSD-3), drops the non-brain components, and reconstructs — a
continuous ``raw -> cleaned raw`` transform. Complements eigen-subspace denoising (artefact subspace
denoising) and WQN (wavelet repair) by removing **component-level** artifacts
(eye, muscle, heart, line, channel noise).

`mne` and `mne_icalabel` are imported lazily; install the light backend with
``pip install "mne-icalabel[onnx]"`` (onnxruntime, no torch) — with torch absent,
ICLabel auto-selects the ONNX backend. Both this and autoreject are BSD-3, so
there is **no non-commercial gate** here.

Pipeline mapping: a `transform` stage in ``recordings/signal-pipeline-plan.md``.
Reproducibility is conditional — ICA is seeded (``random_state``) so a rerun on
the same input + pinned mne/mne-icalabel versions matches, but ICA convergence
can drift across library versions; treat as ``reproducible=True`` only with
pinned versions, else archive the output.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

# ICLabel's seven output classes, in column order.
ICLABEL_LABELS = (
    "brain",
    "muscle artifact",
    "eye blink",
    "heart beat",
    "line noise",
    "channel noise",
    "other",
)
# Components with these labels are kept; all others are candidates for removal.
DEFAULT_KEEP = ("brain", "other")


@dataclass
class IclabelResult:
    cleaned_uv: np.ndarray
    """Cleaned EEG in microvolts, shape ``(n_channels, n_samples)``."""
    ch_names: list[str]
    components: list[dict] = field(default_factory=list)
    """Per-IC ``{index, label, proba, excluded}``."""
    n_excluded: int = 0
    n_components: int = 0


def select_exclude(labels, probas, keep_labels=DEFAULT_KEEP, prob_threshold: float = 0.0) -> list[int]:
    """Indices of components to remove: label not in ``keep_labels`` and
    predicted probability ``>= prob_threshold``.

    Pure (no MNE) so it is unit-testable without the ICLabel model. ``probas`` is
    ICLabel's ``y_pred_proba`` (per-component confidence of its argmax label);
    raise ``prob_threshold`` (e.g. 0.7) to only remove confident artifacts.
    """
    keep = set(keep_labels)
    p = None if probas is None else np.asarray(probas, dtype=float).ravel()
    out = []
    for i, lab in enumerate(labels):
        if lab in keep:
            continue
        if p is None or p[i] >= prob_threshold:
            out.append(i)
    return out


def clean_with_iclabel(
    data_uv: np.ndarray,
    srate: float,
    ch_names: list[str],
    *,
    montage: str = "standard_1020",
    n_components: int = 15,
    l_freq: float = 1.0,
    h_freq: float = 100.0,
    random_state: int = 97,
    keep_labels=DEFAULT_KEEP,
    prob_threshold: float = 0.0,
    max_iter="auto",
) -> IclabelResult:
    """Remove ICLabel-flagged artifact components from a microvolt EEG array.

    Follows the ICLabel-required recipe: band-pass ``l_freq..h_freq`` (default
    1–100 Hz), common-average reference, extended-Infomax ICA — fit on the
    filtered data, applied to the **original** signal. ``n_components`` is capped
    at ``n_channels - 1``.
    """
    from mne.preprocessing import ICA
    from mne_icalabel import label_components

    from compute.cleaning._mne import build_raw

    raw = build_raw(data_uv, srate, ch_names, montage)
    # ICLabel is trained on 1-100 Hz, but a low-rate recording (e.g. 128 Hz,
    # Nyquist 64) can't reach 100 Hz — clamp below Nyquist. This is a deliberate
    # deviation from ICLabel's training band for low-sfreq data.
    nyquist = srate / 2.0
    h_eff = h_freq if (h_freq is not None and h_freq < nyquist) else max(l_freq + 1.0, nyquist - 1.0)
    filt = raw.copy().filter(l_freq=l_freq, h_freq=h_eff, verbose="ERROR")
    filt.set_eeg_reference("average", verbose="ERROR")

    n_comp = max(1, min(int(n_components), len(ch_names) - 1))
    ica = ICA(
        n_components=n_comp,
        max_iter=max_iter,
        method="infomax",
        random_state=random_state,
        fit_params={"extended": True},
    )
    ica.fit(filt, verbose="ERROR")

    res = label_components(filt, ica, method="iclabel")
    labels = list(res["labels"])
    probas = np.asarray(res["y_pred_proba"], dtype=float).ravel()
    exclude = select_exclude(labels, probas, keep_labels, prob_threshold)

    reconst = raw.copy()
    ica.apply(reconst, exclude=exclude, verbose="ERROR")

    excluded = set(exclude)
    components = [
        {"index": i, "label": labels[i], "proba": float(probas[i]), "excluded": i in excluded}
        for i in range(len(labels))
    ]
    return IclabelResult(
        cleaned_uv=reconst.get_data() * 1e6,
        ch_names=list(ch_names),
        components=components,
        n_excluded=len(exclude),
        n_components=n_comp,
    )
