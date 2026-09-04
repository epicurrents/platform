# compute.sleep — YASA sleep staging + micro-events (with 10-20 remontaging)

Automated sleep staging and spindle/slow-wave detection via **YASA** (0.7.0,
BSD-3, bundled pretrained weights — no download, no GPU, **no non-commercial
gate**). Front-ended with **remontaging** so it works on your full-head,
referential ambulatory EEGs, not just PSG.

## Remontaging is the point

YASA staging expects a **limited montage** — a single central-to-mastoid
derivation (`C4-M1` or `C3-M2`), optionally EOG and chin EMG. Your ambulatory /
long EEGs are 10-20 and usually *referential*, so the channel has to be derived.
For any common reference, `(C4-ref) - (M1-ref) = C4-M1`, so a bipolar derivation
is just the difference of two referential channels — reference-independent.
[`montage.derive`](montage.py) does this (pure numpy, reusable, tested), handling
label variations: modality prefixes (`EEG C4-REF`), case, **mastoid/earlobe**
(`M1`/`A1`, `M2`/`A2`), and **old↔new temporal** naming (`T3`/`T7`, `T5`/`P7`, …).

```python
from compute.sleep import stage_sleep, detect_spindles

res = stage_sleep(data_uv, srate, ch_names, eeg="C4-M1", eog="E1-M2", age=45, male=1)
res.stages  # ['WAKE','N1','N2','N2','N3',...] per 30 s epoch
res.stages_int  # WAKE=0 N1=1 N2=2 N3=3 REM=4
res.epoch_onsets_s  # 0, 30, 60, ...
res.proba  # (n_epochs, n_classes) if available
```

## Layout

| File | Purpose |
|---|---|
| `montage.py` | `derive()` / `build_derivations()` / `resolve_index()` — remontaging, MNE-free, tested. |
| `staging.py` | `stage_sleep()` → `StagingResult` (hypnogram); `hypnogram_to_persample()`. |
| `events.py` | `detect_spindles()` / `detect_slow_waves()` → event dicts. |
| `../management/commands/sleep_stage.py` | Host CLI: recording → hypnogram CSV (+ micro-events CSV). |

## Where it fits

An **analysis** stage: it emits a hypnogram and events, not a cleaned signal.
Mapping to the platform's `annotations` app: the per-epoch hypnogram → one
`Label` per 30 s epoch (or a compact hypnogram artefact); spindles/slow-waves →
`Event` rows. Unlike the braindecode detectors it needs no gate (BSD-3)
and no weights provisioning (bundled).

## Dependencies

Add to a sleep extra installed on the worker (lazy-imported, so the platform
imports fine without it):

```
yasa>=0.7.0     # pulls lightgbm, scikit-learn, mne, antropy, lspopt, pooch, seaborn
```

## Usage

```bash
python manage.py sleep_stage --input night.edf --output hypno.csv \
    --eeg C4-M1 --eog E1-M2 --age 45 --male 1 --spindles --slow-waves
```

EEG-only staging works (`--eog`/`--emg` optional) but accuracy improves with EOG
(and, less so, EMG) and with `--age`/`--male`. YASA downsamples to 100 Hz and
band-passes internally — pass raw-ish data, do not pre-epoch.

## Validation gates

1. **Reference approximation.** Mastoid (`M`) and earlobe (`A`) are aliased for
   derivation; near-identical clinically but not identical — validate if it
   matters. A non-mastoid reference (e.g. `C4-Fpz` or average) is a documented
   deviation from YASA's validation set; confirm on your data.
2. **sklearn version.** YASA's bundled staging classifier is pickled against an
   older scikit-learn and emits an `InconsistentVersionWarning` on newer
   versions ("may lead to invalid results"). Pin scikit-learn to a version YASA
   supports on the worker, and sanity-check outputs.
3. **Recording content.** Staging assumes the recording *contains sleep*.
   Ambulatory/overnight EEG is the right target; a short daytime routine EEG has
   little/no sleep and the hypnogram will be mostly WAKE.
4. **Length.** Staging features use multi-minute smoothing windows — very short
   recordings stage poorly; full-night / long ambulatory is ideal.

---

## Server-side now, Pyodide port later

The three pieces split cleanly by portability:

- **Remontaging** (`derive`) is pure subtraction — trivially Pyodide-able already.
- **Event detection** (`spindles_detect` / `sw_detect`) is numpy/scipy — a
  candidate for a browser port (no compiled ML), though it's compute-heavy over a
  full night.
- **Staging** uses a **LightGBM** classifier (compiled), so it stays server-side;
  LightGBM has no practical Pyodide/WASM build today.

So the natural split mirrors the platform pattern: stage server-side (cache the
hypnogram — it's tiny), and if desired move remontaging + on-demand event
detection client-side. The hypnogram itself (a few hundred integers per night) is
cheap to cache and serve, like the lead field.
