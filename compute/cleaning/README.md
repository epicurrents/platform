# compute.cleaning — automated EEG preprocessing / QC

Automated artifact cleaning and quality control built on **`mne-icalabel`** (ICLabel
ICA classification) and **`autoreject`** — both **BSD-3**, so there is **no
non-commercial gate**. Server-side today; a Pyodide port is sketched below.

## The cleaning ladder

These occupy distinct rungs and compose, rather than replacing eigen-subspace or wavelet-domain denoising:

| Rung | Tool | Removes | Home |
|---|---|---|---|
| Component-level | **ICLabel + ICA** (this module) | eye / muscle / heart / line / channel-noise ICs | `compute.cleaning.clean_with_iclabel` |
| Eigen-subspace | GEVD against a reference covariance | artefact subspace vs a reference covariance | project-supplied; no core implementation |
| Wavelet-domain | WQN | transient wavelet-domain corruption | project-supplied; no core implementation |
| Channel / epoch | **autoreject** (this module) | bad channels (interpolate), bad epochs (log) | `compute.cleaning.reject` |

Clean input compounds through every downstream detector, qEEG index, and future
braindecode model — which is why this sits high on the roadmap.

## Layout

| File | Purpose |
|---|---|
| `iclabel.py` | `clean_with_iclabel()` — extended-Infomax ICA → ICLabel labels → drop non-brain ICs → reconstruct. Pure `select_exclude()` decision helper (testable without the model). |
| `reject.py` | `bad_channels_ransac()`, `interpolate_bad_channels()`, `epoch_reject_log()`, pure `reject_log_to_spans()`. |
| `_mne.py` | Shared: `positioned_channels()`, `build_raw()` (microvolt→volt, montage). |
| `../management/commands/eeg_clean.py` | Host CLI: recording → cleaned EDF + report (EEG cleaned, other channels passed through). |

## Pipeline mapping

- **Transforms** (`raw → cleaned raw`, per `recordings/signal-pipeline-plan.md`):
  `clean_with_iclabel`, `interpolate_bad_channels`.
- **Analysis/QC** (emit findings, not signal): `bad_channels_ransac` (bad-channel
  list), `epoch_reject_log` + `reject_log_to_spans` (bad-time annotations).
- **Reproducibility:** ICA is seeded (`random_state`), so a rerun on the same
  input + **pinned** `mne`/`mne-icalabel` versions matches — but ICA convergence
  can drift across versions, so treat as `reproducible=True` only with pinned
  versions; otherwise archive the output (per the pipeline plan's `reproducible=False`).

## Usage

```bash
# preprocess extra (lazy-imported; not in the base image):
#   pip install "mne-icalabel[onnx]" autoreject      # onnxruntime, no torch
python manage.py eeg_clean --input rec.edf --output rec_clean.edf --method both
```

```python
from compute.cleaning import clean_with_iclabel, interpolate_bad_channels

clean_uv, bads = interpolate_bad_channels(data_uv, srate, ch_names)  # channels
res = clean_with_iclabel(clean_uv, srate, ch_names, prob_threshold=0.7)  # components
res.cleaned_uv  # (n_ch, n_samples) microvolts
res.components  # [{index, label, proba, excluded}, ...]
```

ICLabel's required recipe is folded in: 1–100 Hz band-pass (clamped below Nyquist
for low-rate recordings), common-average reference, extended-Infomax ICA fit on
the filtered data and applied to the original. Only channels with montage
positions are processed; select them with `positioned_channels()` (the command
does this and passes non-EEG channels through unchanged).

## Dependencies

Add to a `requirements-preprocess.txt` extra installed on the worker (both are
lazy-imported, so the platform stays importable without them):

```
autoreject>=0.4.4
mne-icalabel[onnx]>=0.9.0      # pulls onnxruntime; do NOT also install torch
```

Keeping `torch` out means ICLabel auto-selects the ONNX backend (its backend
order is torch→onnx), which is the light path. `autoreject` pulls scikit-learn.

## Validation gates

1. **Montage/naming.** Cleaning only touches channels resolvable to the montage;
   confirm your recordings' channel labels map (10-20 vs decorated `EEG Fp1-REF`).
2. **ICA component count & threshold.** `n_components` and `prob_threshold` trade
   artefact removal against brain-signal preservation — tune per montage/site;
   `prob_threshold=0` removes all non-brain/other (the documented ICLabel idiom).
3. **Low sample rates.** Below ~200 Hz the 100 Hz upper band is clamped below
   Nyquist — a deviation from ICLabel's training band; validate on your data.

---

## Server-side now, Pyodide port later

This runs server-side today for good reasons, but the design leaves a clean path
to a browser (Pyodide) port that mirrors the platform's existing lead-field
pattern — **fit the expensive part server-side, apply the cheap part client-side.**

**Why server-side now.** The *fitting* is heavy: extended-Infomax ICA is an
iterative O(n²·iters) decomposition; autoreject's cross-validated thresholding and
RANSAC resampling are compute-intensive; and `mne-icalabel` needs a native
`onnxruntime` wheel. Running all of that in Pyodide would be slow and awkward, and
these results are exactly the "expensive but cacheable" work the `compute/` app
exists for.

**The port that works: fit server-side, apply client-side.** The expensive output
of each stage is a *small linear operator* that is cheap to apply — so cache the
operator server-side (as the lead field is) and apply it in the browser:

- **ICA cleaning.** The server fits ICA and runs ICLabel, then caches the ICA
  **unmixing** and **mixing** matrices plus the **exclude set** (a few kB for a
  clinical montage). The browser reconstructs with a single matmul —
  `clean = mixing @ (diag(keep) @ (unmixing @ data))` — trivial in Pyodide/NumPy,
  no ICA, no onnxruntime.
- **Bad-channel interpolation.** Cache the spherical-spline **interpolation
  matrix** for the detected bad set; the browser applies `clean = interp @ data`.
- **Reject log.** Ship the per-epoch bad-time spans (already tiny) as annotations;
  no client compute.

Only the *classification* step genuinely needs a model in the browser, and ICLabel
is an **ONNX** model — so a fuller client-side variant could run it via
**`onnxruntime-web` (WASM)** on browser-fit ICA for short/low-channel segments.
But the fit-server/apply-client split above is the pragmatic first port: it reuses
the exact caching pattern already proven for the forward model, keeps the browser
work to cheap matrix multiplies, and needs no WASM ML runtime. When that port is
built, these functions become the server-side "fit" half and gain a small cache
model (mirroring `LeadFieldCache`) holding the per-recording operators.
