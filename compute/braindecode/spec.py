"""Model-input contract for a braindecode inference model.

braindecode ships 60+ architectures and a ``from_pretrained`` path for foundation
models, but they share **no fixed input contract**: channel
count and order, sampling rate, window length, expected preprocessing, and output
semantics all vary per model. This scaffold therefore carries the contract
explicitly in a :class:`BraindecodeModelSpec`, and the scoring core
(:mod:`compute.braindecode.detect`) is generic over it.

The spec is a plain dataclass (no torch, no Django) so it can be defined,
imported, and unit-tested anywhere. Supply one per model you serve.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class BraindecodeModelSpec:
    """Everything the scoring core needs to feed a braindecode model correctly.

    Attributes
    ----------
    name
        Human/log identifier (also the default non-commercial gate key).
    n_chans
        Number of input channels the model expects.
    sfreq
        Sampling rate (Hz) the model was trained at. Input is resampled to this.
    channels
        Ordered channel labels the model expects. If ``None``, the recording's
        first ``n_chans`` EEG channels are used in file order (rarely what you
        want for a real model — prefer an explicit list). Matching is
        case-insensitive, but no 10-10 <-> 10-20 aliasing is applied here;
        pass exact labels the recording carries.
    window_seconds, n_times
        Window length. Give exactly one: ``window_seconds`` (converted with
        ``sfreq``) or ``n_times`` (samples) directly.
    hop_seconds
        Sliding-window hop for scanning a continuous recording.
    n_outputs
        Model output dimension (number of classes / regression targets).
    output
        How to read raw model output: ``"logits"`` (apply softmax for probs),
        ``"probs"`` (already activated), or ``"raw"`` (regression — no softmax).
    bandpass
        Optional ``(l_freq, h_freq)`` band-pass applied before windowing (Hz).
    notch_hz
        Optional mains notch (Hz). No regional default — pass 50/60 or None.
    normalization
        Per-window, per-channel normalization: ``"none"``, ``"zscore"``,
        ``"percentile"`` (divide by 95th-percentile abs), or
        ``"exp_moving"`` (exponential-moving standardization, a braindecode
        default — approximated here; for exact parity use braindecode's own
        ``preprocessing`` on an mne.Raw instead).
    positive_index
        For a classifier whose per-window probability of one class should be
        turned into a per-sample score + events, the index of that class.
        ``None`` leaves the result as raw per-window scores only.
    noncommercial
        True if the chosen weights are licensed for non-commercial use only
        (some braindecode-hosted foundation weights inherit CC-BY-NC from their
        authors). Gates loading behind ``EPICURRENTS_NONCOMMERCIAL_USE``.
    """

    name: str
    n_chans: int
    sfreq: float
    # braindecode model class name (e.g. "EEGNetv4", "BENDR", "ShallowFBCSPNet").
    # Required to build the architecture for from_pretrained, a state_dict
    # checkpoint, or random-init; a full-module (pickled nn.Module) checkpoint
    # can omit it.
    arch: str | None = None
    channels: tuple[str, ...] | None = None
    window_seconds: float | None = None
    n_times: int | None = None
    hop_seconds: float = 1.0
    n_outputs: int = 2
    output: str = "logits"
    bandpass: tuple[float, float] | None = None
    notch_hz: float | None = None
    normalization: str = "none"
    positive_index: int | None = None
    noncommercial: bool = False
    extra: dict = field(default_factory=dict)

    def window_samples(self) -> int:
        """Resolve the window length in samples from ``n_times`` or seconds."""
        if self.n_times is not None:
            return int(self.n_times)
        if self.window_seconds is not None:
            return round(self.window_seconds * self.sfreq)
        raise ValueError(f"spec {self.name!r}: give either n_times or window_seconds.")

    def __post_init__(self):
        if self.output not in ("logits", "probs", "raw"):
            raise ValueError(f"spec {self.name!r}: output must be logits|probs|raw.")
        if self.normalization not in ("none", "zscore", "percentile", "exp_moving"):
            raise ValueError(f"spec {self.name!r}: unknown normalization {self.normalization!r}.")
        if (self.n_times is None) == (self.window_seconds is None):
            raise ValueError(f"spec {self.name!r}: set exactly one of n_times / window_seconds.")
        if self.positive_index is not None and not (0 <= self.positive_index < self.n_outputs):
            raise ValueError(f"spec {self.name!r}: positive_index out of range for n_outputs.")
