# compute.braindecode — braindecode inference/serving scaffold

**Status: serving scaffold, bring-your-own-weights.** A generic runtime for
[braindecode](https://braindecode.org) (BSD-3) models: load a model, scan a
recording, get per-window scores. It trains nothing and ships no weights — it is
the *deployment target* for a checkpoint produced by a training facility (see
[Training facility — path forward](#training-facility--path-forward)). Until you
supply weights (`--repo-id`, `--checkpoint`, or `--random-init`), it does nothing.

## Why a spec instead of a fixed contract

A single-purpose detector wraps one model with a fixed input contract (a fixed
channel set, sampling rate and window length). braindecode ships 60+ architectures plus `from_pretrained`
foundation models that share **no** contract — channel count/order, sampling
rate, window length, preprocessing, and output semantics all differ per model.
So the contract is explicit, in a
[`BraindecodeModelSpec`](spec.py), and the scoring core is generic over it. There
is deliberately **no default input contract**: a wrong window length or channel
set silently produces garbage, so every served model needs its own spec, matched
to its model card.

## Layout

| File | Purpose |
|---|---|
| `spec.py` | `BraindecodeModelSpec` — the model-input contract (no torch/Django). |
| `detect.py` | Dependency-light scoring core (numpy; scipy/mne lazy). Preprocess → window → per-window scores; optional per-sample score + events for a nominated class. |
| `model.py` | Lazy loader: `from_pretrained` (HF Hub), local checkpoint (module or state_dict), or random-init. Torch-free `.predict()` wrapper. Non-commercial gate when `spec.noncommercial`. |
| `../management/commands/braindecode_score.py` | Host-safe CLI: recording → per-window scores CSV. |

## Try it (no weights, plumbing check)

```bash
# torch + braindecode live in a separate ML extra, not the base image:  pip install "braindecode[hub]" torch
python manage.py braindecode_score \
    --input rec.edf --output scores.csv \
    --arch EEGNetv4 --sfreq 128 --window-seconds 4 --n-outputs 2 \
    --channels Fp1,Fp2,C3,C4,O1,O2,... \
    --random-init          # untrained; meaningless output, exercises the scan
```

Swap `--random-init` for `--checkpoint model.pt` (your trained weights) or
`--repo-id braindecode/<model>` (a Hub foundation model). In code:

```python
from compute.braindecode import BraindecodeModelSpec, score_recording

spec = BraindecodeModelSpec(
    name="abnormal-eegnet",
    arch="EEGNetv4",
    n_chans=21,
    sfreq=100.0,
    channels=("Fp1", "Fp2", ...),
    window_seconds=6.0,
    hop_seconds=4.0,
    n_outputs=2,
    output="logits",
    positive_index=1,
    normalization="zscore",
)
result = score_recording(data_uv, srate, ch_names, spec, checkpoint="model.pt")
# result.window_scores: (n_windows, n_outputs); result.per_sample / result.events for class 1
```

## Serving path (when you have a checkpoint)

The result is intentionally generic (per-window scores, plus optional per-sample
score/events for a binary class), so the persistence and API layers follow the
workflow pattern in [../README.md](../README.md) rather than being prebuilt here,
because their exact shape depends on the model's output semantics:

- **Celery task + cache:** follow the workflow guide in
  [../README.md](../README.md) — a `*Cache` model with a real FK to
  `recordings.Recording` plus an async trigger. A binary detector
  (`positive_index` set) fits a per-sample-trace + events cache shape; a
  multi-class model wants a small dedicated cache row (store
  `window_scores`). Wire it when the first real model lands.
- **API / management parity:** the same staff-gated trigger / metadata / download
  routes as the lead-field module.
- **torch** goes in the ML worker requirements extra, imported lazily — never
  the base image.

## Licensing

braindecode's **core is BSD-3**, so the library itself is unrestricted. But some
Hub-hosted foundation weights inherit **CC-BY-NC** from their upstream authors.
Set `spec.noncommercial=True` for those, and the loader gates on
`EPICURRENTS_NONCOMMERCIAL_USE` via `compute.licensing` (key
`braindecode:<name>`). Permissive weights (BIOT, CBraMod,
EEGPT — MIT/Apache) need no gate.

## Validation gates

1. **The spec must match the model card.** Channel set/order, `sfreq`, window
   length, expected preprocessing, and `output` (logits vs probs) are all
   model-specific; a mismatch yields silent garbage. Confirm each against the
   architecture's documentation / the checkpoint's training config.
2. **Normalization parity.** `exp_moving` here approximates braindecode's
   `exponential_moving_standardize`; for exact parity, preprocess with
   braindecode's own `preprocessing` on an `mne.Raw` instead.
3. **Cropped decoding** emits per-crop outputs; the predictor mean-collapses the
   crop axis. If you use cropped models and need per-crop scores, extend `_Predictor`.

---

## Training facility — path forward

This deployment's server hardware can't train, but the design keeps training a
**portable, additive facility** so a future deployer with a GPU can stand it up
without reworking anything here. The split is deliberate: **training is offline;
serving is this scaffold.** They meet only at a checkpoint file.

**Where it lives (and doesn't).** Training is a data-science activity, not a
request/Celery path — it must **not** live in a Django-served module. Put it in a
separate, optional area (e.g. `research/braindecode/` or a `scripts/training/`
tree, or notebooks), installed via a dedicated `requirements-train.txt`
(`braindecode[hub]`, `torch` with CUDA, `skorch`, `moabb` if using public
benchmarks). It imports the platform only to *read data* and *write a checkpoint*;
the platform never imports it.

**Data path — from the store to a dataset.** braindecode is MNE-native, which
lines up with this platform: build `mne.Raw`/`Epochs` from a recording (the
existing EDF/reconstruction path), window with
`braindecode.preprocessing.create_windows_from_events` /
`create_fixed_length_windows`, and take labels from the `annotations` app
(`Event`/`Label` → window targets). No BIDS required (`create_from_mne_raw` /
`create_from_mne_epochs` / `create_from_X_y` accept MNE objects or numpy). A
labeling path is the real prerequisite — this deployment is "little/no labels
yet", so step one for any trainer is assembling a labeled set from annotations or
an external corpus (TUH, CHB-MIT via `epilepsy2bids`, MOABB).

**Two training modes, same output.**

- *Supervised from scratch* — `EEGClassifier("ShallowFBCSPNet"|"EEGNetv4", …)`
  (skorch: `.fit(X, y)` / `.predict`), CPU-trainable for small data. Best first
  baseline once labels exist.
- *Fine-tune a foundation model* — `Model.from_pretrained("braindecode/<model>",
  n_outputs=k)` then `reset_head(k)` and train the head (optionally unfreezing the
  backbone). This is the strategically valuable path: an **owned, permissively
  licensed** detector (BIOT/CBraMod/EEGPT) instead of a published model whose
  weights are CC-BY-NC or DUA-gated. Needs a GPU for the larger backbones.

**The round trip that makes it portable.** A trainer's deliverable is a checkpoint
(`torch.save(model.state_dict())` or the full module) plus a
`BraindecodeModelSpec` capturing the exact input contract it was trained with.
That pair drops straight into this scaffold: `score_recording(..., checkpoint=…)`
with the matching spec. So the facility and the serving scaffold are two ends of
one contract — a future deployer trains on their hardware, ships us (or their own
instance) a `(checkpoint, spec)` pair, and it serves with no code changes here.

**Reproducibility & provenance.** A trained checkpoint is a non-reproducible
artefact (GPU nondeterminism, data shuffling), so — per the signal-pipeline plan's
`reproducible=False` handling — it is *archived*, not rebuilt, and its identity is
pinned (checkpoint hash + spec + training-data manifest + code/seed). Record that
alongside the checkpoint so a served score can always be traced to the exact model
that produced it.

**Licensing carries through.** If fine-tuning starts from CC-BY-NC foundation
weights, the resulting checkpoint is a derivative under those terms — mark its
spec `noncommercial=True` so serving stays gated. Starting from BSD/MIT/Apache
weights (or training from scratch) keeps the result unrestricted.

**Suggested first milestone for a GPU-equipped deployer:** assemble a labeled set
(annotations or a public corpus) → fine-tune one permissive foundation model into
a binary head for a concrete task (abnormal-vs-normal is the usual first win) →
export `(checkpoint, spec)` → serve through this scaffold → wire the Celery task +
cache. Each step is independently testable, and only the last touches this app.
