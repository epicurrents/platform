"""Lazy loader for a braindecode inference model.

Loads a model one of three ways and returns a torch-free predictor
(``.predict(batch_np) -> np.ndarray``) so the scoring core stays torch-free:

1. **from_pretrained** — ``repo_id`` given: pull foundation-model weights from
   the HuggingFace Hub via ``<Arch>.from_pretrained(repo_id, n_outputs=...)``.
2. **checkpoint** — ``checkpoint`` path given: a pickled ``nn.Module`` (used as
   is) or a ``state_dict`` (loaded into a freshly constructed ``spec.arch``).
3. **random-init** — ``allow_random_init=True`` with neither: an untrained
   ``spec.arch`` for exercising the scan/preprocess plumbing (meaningless output).

torch + braindecode are imported here, lazily, so the platform (and the scoring
core's unit tests, which inject a stub predictor) stay importable without them.
They belong in a separate ML requirements extra, not the base image.

**Licensing.** braindecode's core is BSD-3, but some Hub-hosted foundation
weights inherit CC-BY-NC from their upstream authors. When ``spec.noncommercial``
is set, loading is gated behind ``EPICURRENTS_NONCOMMERCIAL_USE`` via
``compute.licensing`` (key ``braindecode:<spec.name>``).
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def _get_arch(models_module, arch: str | None):
    if not arch:
        raise ValueError(
            "spec.arch is required to build the architecture (needed for "
            "from_pretrained, a state_dict checkpoint, or random-init)."
        )
    cls = getattr(models_module, arch, None)
    if cls is None:
        raise ValueError(
            f"braindecode.models has no architecture {arch!r}. Check the name "
            "against braindecode.models.util.models_dict for your version."
        )
    return cls


class _Predictor:
    """Wraps a braindecode nn.Module so the core passes numpy and gets numpy."""

    def __init__(self, model, device: str):
        self._model = model
        self._device = device

    def predict(self, batch_np):
        import numpy as np
        import torch

        with torch.no_grad():
            t = torch.as_tensor(np.asarray(batch_np), dtype=torch.float32, device=self._device)
            out = self._model(t)
        arr = out.detach().cpu().numpy()
        # Window-level output expected (n_windows, n_outputs). Cropped/temporal
        # models emit (n, n_outputs, n_crops); collapse the crop axis by mean so
        # the core still gets one score per window. Flag this if you use cropped
        # decoding and need per-crop outputs.
        if arr.ndim == 3:
            arr = arr.mean(axis=-1)
        return arr


def load_model(
    spec,
    *,
    repo_id: str | None = None,
    checkpoint: str | None = None,
    allow_random_init: bool = False,
    device: str | None = None,
):
    """Load a braindecode model per ``spec`` and return a :class:`_Predictor`."""
    if spec.noncommercial:
        from compute.licensing import require_noncommercial

        require_noncommercial(f"braindecode:{spec.name}")

    import torch
    from braindecode import models as bdm

    device = device or ("cuda" if torch.cuda.is_available() else "cpu")

    if repo_id and checkpoint:
        raise ValueError("Pass either repo_id or checkpoint, not both.")

    if repo_id:
        cls = _get_arch(bdm, spec.arch)
        if not hasattr(cls, "from_pretrained"):
            raise RuntimeError(f"{spec.arch} has no from_pretrained; use a checkpoint.")
        model = cls.from_pretrained(repo_id, n_outputs=spec.n_outputs)
        logger.info("braindecode %s loaded from_pretrained %s", spec.arch, repo_id)
    elif checkpoint:
        model = _load_checkpoint(bdm, spec, checkpoint, device)
    elif allow_random_init:
        cls = _get_arch(bdm, spec.arch)
        logger.warning(
            "braindecode %s built with RANDOM weights (allow_random_init) — "
            "plumbing test only; outputs are meaningless.",
            spec.arch,
        )
        model = _build(cls, spec)
    else:
        raise RuntimeError(
            "No weights provided. Pass repo_id (from_pretrained), a checkpoint "
            "path, or allow_random_init=True to test the pipeline without weights."
        )

    model.eval().to(device)
    return _Predictor(model, device)


def _build(cls, spec):
    """Construct a braindecode model from the spec's shape parameters."""
    return cls(
        n_chans=spec.n_chans,
        n_outputs=spec.n_outputs,
        n_times=spec.window_samples(),
        sfreq=spec.sfreq,
    )


def _load_checkpoint(bdm, spec, path: str, device: str):
    import os

    import torch

    if not os.path.exists(path):
        raise FileNotFoundError(f"Checkpoint not found: {path}")
    obj = torch.load(path, map_location=device, weights_only=False)
    if isinstance(obj, torch.nn.Module):
        return obj
    state = obj.get("state_dict", obj) if isinstance(obj, dict) else obj
    cls = _get_arch(bdm, spec.arch)
    model = _build(cls, spec)
    model.load_state_dict(state)
    logger.info("braindecode %s loaded state_dict from %s", spec.arch, path)
    return model
