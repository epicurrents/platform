# epicurrents EEG tooling — phased integration roadmap

*A pruned, sequenced plan for folding the surveyed EEG tools into the epicurrents
`platform`, written to the existing `compute/` conventions. Companion to the
literature-search report (`eeg-tooling-literature-search.md`, kept in the archive
repository) and the SpikeNet 1 integration spike, which stayed there
too: its weights are published under terms incompatible with an Apache-2.0 tree,
so the public platform vendors no model.*

**Status (2026-09-04): three tracks are built, and the phases below still read as future work.**
Phase 2's autoreject and mne-icalabel steps shipped as [compute/cleaning/](../../compute/cleaning/)
(`reject.py`, `iclabel.py`); the YASA parallel track shipped as [compute/sleep/](../../compute/sleep/)
with a `sleep_stage` command; Phase 5's braindecode step shipped as a bring-your-own-weights serving
scaffold at [compute/braindecode/](../../compute/braindecode/). Phase 0 (the ML requirements file and
model fetcher), the rest of Phase 2, and all of Phases 3 and 4 are untouched.

---

## Framing: where things land, and the gate every item passes

Three placements, matching the platform's existing compute-placement philosophy:

- **Pyodide (browser)** — pure-Python, light, per-view computation. Candidates: the
  qEEG feature libraries and some preprocessing (MNE-Features, antropy, fooof,
  pycrostates, autoreject) *if* they stay within Pyodide's wheel constraints.
- **`compute/` app (server-side Python)** — needs compiled extensions, or is
  expensive-but-cacheable and reused across sessions. This is where the deep
  models live: SpikeNet, YASA's LightGBM, ICLabel/ONNX, foundation-model heads.
  Each follows the "Adding a new compute module" checklist already in
  `compute/README.md` (sub-package → optional `*Cache` model → Ninja endpoint →
  management command → tests).
- **Docker sidecar (own image)** — a model with a heavy or conflicting runtime
  (TensorFlow for SpikeNet 1, PyTorch for foundation models, a challenge-packaged
  detector) that shouldn't bloat the base image. Driven from Celery, packaged to
  a fixed I/O contract (see Phase 4 — SzCORE).

**The gate.** `epicurrents/platform` is public, so every candidate passes a
licence check before it's wired in. The check is cheaper than it sounds, because
the standing pattern redistributes nothing — see *Dependency & licence summary*
at the end. What the gate really asks is whether a tool can be reached the
ordinary way, as a library the operator installs or an image they pull, or
whether integrating it would put third-party code in the tree. The second is the
expensive case, and it is the one that needs an answer before any code.

Permissive, and reachable the ordinary way (BSD/MIT/Apache):
braindecode, BIOT, CBraMod, EEGPT, YASA, autoreject, mne-icalabel, PyPREP,
meegkit, MNE-Features, fooof, pycrostates, antropy, SeizureTransformer,
epilepsy2bids, SzCORE. Needs a decision: SpikeNet 1 (upstream licence conflicts — CC BY-NC vs CC BY-ND; non-commercial + verbatim),
SpikeNet 2 (CC BY-NC + gated weights), Neuro-GPT / spectral_connectivity (GPL),
anything "licence not stated" (ProtoEEG, DeepSOZ, SUMO, BENDR) — where the absence
is a restriction rather than an oversight, since no licence grants no permission.

Phases are ordered by value-per-unit-effort and dependency, not calendar. Each
lists concrete deliverables in the platform's module shape.

---

## Phase 0 — Foundations (enable everything downstream)

Small, reusable groundwork so later phases don't each reinvent it.

- **Model-artifact convention.** Generalise the "weights not vendored, resolved
  from a path/setting, provisioned at deploy" pattern into a documented
  convention for all pretrained models (a `MODELS_DIR` root + per-model
  subdir + a `manage.py fetch_models` helper). It is the mechanism the summary
  table's *bring-your-own-weights* rows depend on, and the reason a model's own
  licence never has to be compatible with this repository's.
- **Heavy-runtime worker extras.** Add `requirements-ml.txt` (TensorFlow-CPU,
  PyTorch, ONNXRuntime) installed only into a dedicated Celery worker image, so
  the base web/worker images stay lean. Route ML tasks to that worker's queue.
- **BIDS ingest seam.** Adopt `mne-bids` (BSD) as the standard on-ramp; it's the
  prerequisite for `epilepsy2bids`, `mne-bids-pipeline`, and the SzCORE contract.
- **Result-cache pattern for per-recording results.** The spike's
  `SpikeNetResultCache` (per-recording, params-keyed, float32 payload + JSON
  events) is the template every detector result reuses.

*Exit criteria:* a second model can be added by copying a checklist, not by
inventing plumbing.

---

## Phase 1 — Spike detection MVP (highest-value single feature)

**Deliverable: SpikeNet 1, wired via the archived integration spike.**

> **Blocked on permission, not on effort.** The steps below describe work that is
> almost done, which is exactly what makes this entry dangerous to read quickly.
> SpikeNet 1's terms are non-commercial in the licence file and no-derivatives in
> the README, so the spike cannot come into this repository and its weights cannot
> be redistributed from it. Nothing here proceeds without written permission from
> the authors or a licence change upstream. If neither arrives, the honest options
> are a permissively-licensed detector instead, or a sidecar the operator builds
> and runs themselves from their own copy — see *Dependency & licence summary*.

The spike (`compute/spikenet/` in the archive repository) is written and its numerical core is tested
(montage build, channel aliasing, event extraction, full `detect_spikes` wiring
with a stub model — 7/7 passing). Remaining work is validation and productionising,
not authoring:

1. Clear the **validation gates** in the spike's README — above all,
   reproduce a reference `run_spikenet.py` probability trace on the same input
   (montage/order + filter parity). This is the correctness-critical step.
2. Add `SpikeNetResultCache` to `compute/models.py`; `makemigrations`/`migrate`.
3. Set `EEG_MAINS_HZ = 50` in settings for a 50 Hz-mains deployment —
   SpikeNet was trained on 60 Hz-notched US data, so the notch frequency is
   deployment-configurable in the spike; a 60 Hz notch on 50 Hz-mains data only
   distorts. Merge the Ninja routes; wire `tasks._load_recording_uv()` to `recordings`.
4. Surface events in the Vue viewer as an annotation track over the trace; stream
   the probability via `…/prob/` for a heat-strip overlay.
5. Licence: the upstream terms are restrictive and internally inconsistent between
   the licence file and the README, so any integration needs a licence review before
   code rather than after. Assume redistribution is not available and that
   fine-tuning produces a derivative that cannot be shared.

*Why first:* IED detection is the most-requested feature, it reuses the existing
10-20 montage handling and source-localisation UI real estate, and the
spike removes most of the authoring risk.

*Optional follow-on:* **ProtoEEG-kNN** as an interpretable second opinion (ships
weights + demo) once its licence is confirmed.

---

## Phase 2 — Automated preprocessing / QC layer (compounding value)

These feed *every* downstream analysis, including Phase 1's detections, and are
all permissive + MNE-native.

- **autoreject** (BSD) — automated epoch rejection/interpolation; a
  `compute/preprocess/` module + optional per-recording bad-segment cache.
- **mne-icalabel, ONNX backend** (BSD) — automatic ICA-component labelling; the
  ONNX path keeps the image light and is a natural `compute/` residency. Pairs
  with eigen-subspace and ICA decompositions.
- **python-meegkit ZapLine** (BSD) — DSS line-noise removal, filling the gap your
  notch filter leaves. Small, pure-Python; a Pyodide candidate.
- **PyPREP** (MIT) — robust referencing + bad-channel automation as an ingest step.

*Exit criteria:* a recording can be auto-QC'd (bad channels, bad epochs, labelled
ICs, line-noise removed) before any eigen-subspace or wavelet denoiser and
before any detector runs.

---

## Phase 3 — qEEG feature layer (a reusable index library)

A standardised, sklearn-ready feature surface that generalises a project's bespoke
qEEG index computation into a reusable library.

- **MNE-Features** (BSD) — ~40 spectral/entropy/Hjorth/connectivity features as
  sklearn transformers; the backbone of the feature matrix.
- **fooof / specparam** (Apache) — aperiodic (1/f) vs oscillatory decomposition;
  high scientific value for any spectral index.
- **pycrostates** (BSD) — resting-state microstate metrics.
- **antropy** (BSD) — fast entropy/complexity (retire any PyEEG usage).
- **MNE-Connectivity** (BSD) — extends the existing reference-coherence diagnostic
  to coherence/PLV/wPLI/Granger at near-zero integration cost.

Most of this is Pyodide-eligible; keep the heavier connectivity/microstate
clustering server-side.

---

## Phase 4 — Seizure detection + the SzCORE contract *(provisional, elaborated)*

This phase couples a concrete feature (seizure detection) with an architectural
decision (how the platform packages *all* detectors). Marked **provisional**
because the contract is a bigger bet than a single library add — but it is the
one that pays off most as the number of models grows.

### 4a. The concrete add

- **epilepsy2bids** (MIT) — dataset → BIDS/HED-SCORE ingest (CHB-MIT, TUSZ, Siena).
- **SeizureTransformer** (MIT, open weights, already a public Docker image
  `yujjio/seizure_transformer`) — the reference detector; run it as a **Docker
  sidecar**, not an in-process import (it has its own runtime).
- **timescoring** (permissive) — event/sample metrics (FP/day, event sensitivity)
  instead of naive per-window accuracy.
- *Optional:* **DeepSOZ** for seizure-onset-zone localisation, pairing naturally
  with your source-localisation work (verify licence/weights first).

### 4b. Adopt the SzCORE Docker I/O contract as the platform's detector-packaging standard

**What it is.** [SzCORE](https://github.com/esl-epfl/szcore) (Seizure Community
Open-source Research Evaluation; EPFL ESL, *Epilepsia* 2025, MIT) defines a
standard way to package and evaluate an EEG detector: each algorithm is an
**isolated Docker image** that reads a BIDS-formatted recording from a predefined
input path and writes **HED-SCORE / TSV annotations** to a predefined output path,
running **offline** with all dependencies baked in at build time. A YAML registers
the algorithm; a template `Dockerfile` defines the image. It exists to end the
field's format/metric chaos — but the same contract is exactly what a
multi-model platform needs internally.

**Why adopt it here.** Today each model (a denoiser, a spike detector, a future seizure net)
risks its own bespoke wiring, runtime, and I/O shape. If instead **every detector
is a SzCORE-style container** — BIDS in, standardised annotations out, driven by a
Celery task that mounts input/output volumes — you get:

- **One packaging pattern** for heterogeneous runtimes (TF SpikeNet, PyTorch
  foundation heads, someone else's challenge image) without polluting the base
  image or fighting dependency conflicts.
- **Drop-in third-party models.** Anything published to the SzCORE spec runs on
  your platform unmodified — a large and growing pool (the 2025 challenge alone
  produced several open, containerised detectors).
- **Free, honest evaluation.** `timescoring` + the SzCORE benchmark give clinically
  meaningful metrics and a path to validate your own models against public
  leaderboards.
- **Provenance and reproducibility** align with the BIDS-App / Boutiques pattern
  you'd want anyway for a clinical-adjacent tool.

**How it maps onto `compute/`.** A new residency alongside "in-process module":
a **detector-sidecar** convention. A thin `compute/detectors/` layer holds, per
model, (i) a Celery task that stages the recording to BIDS (`mne-bids` /
`epilepsy2bids`), runs the container against the mounted I/O paths, and ingests
the returned annotations into your `annotations` app, and (ii) a registry entry
(image ref + the SzCORE YAML). The existing `docker-compose` gains a profile for
detector images; the ML worker queue dispatches the runs.

**Migration path (low-risk, incremental).**
1. Prove the pattern with **one** sidecar — SeizureTransformer's existing image —
   end to end: BIDS stage → container run → annotation ingest → viewer track.
2. Extract the reusable `run_detector_container()` Celery helper.
3. **Re-home Phase 1's SpikeNet** onto the same pattern (SpikeNet's TF runtime is
   the poster child for "belongs in a sidecar, not the base image"), retiring the
   in-process path if the container path is cleaner operationally.
4. Document a "add a detector" checklist mirroring "add a compute module".

**Risks / why provisional.**
- SzCORE is *seizure*-oriented; using its container+BIDS contract for spikes/sleep
  means adopting the *pattern*, not its seizure-specific annotation vocabulary
  verbatim — you'd extend the annotation schema for non-seizure events.
- Container orchestration from Celery (volume mounts, GPU passthrough, image
  lifecycle) is real ops surface; worth it past ~2–3 models, arguably overkill for
  one. Hence: prove with one, decide before generalising.
- The generic neuroimaging equivalent is **Boutiques + the BIDS-App pattern**; if
  you'd rather not tie the convention to a seizure project's spec, adopt
  Boutiques' JSON-descriptor framing and treat SzCORE as one conforming instance.

*Recommendation:* do 4a regardless; treat 4b as a **spike-then-commit** — build the
single SeizureTransformer sidecar, evaluate the ergonomics against just importing
models in-process, and only then decide whether it becomes the house standard.

---

## Phase 5 — Foundation models for custom detectors (strategic, optional)

When you have your own labelled data and want models you fully control (a
permissive alternative to SpikeNet 2's gated weights):

- **braindecode** (BSD) — single dependency for 70+ architectures *and* a unified
  loader for open-weight foundation models.
- Fine-tune **BIOT / CBraMod / EEGPT** (all permissive, open weights, clinical-corpus
  pretraining) into task heads (IED, abnormal-EEG, custom events), served as
  `compute/` or sidecar tasks.
- Treat Neuro-GPT (GPL) and BENDR/BrainBERT (unspecified licence) as flagged.

---

## Parallel track — Sleep staging (independent, high-turnkey)

Not dependency-ordered with the above; schedule whenever sleep EEG is in scope.

- **YASA** (BSD, bundled weights, offline) — staging + spindle/slow-wave/artefact
  detection in one pip install; a clean `compute/sleep/` module.
- **OpenSpindleNet** (MIT, open weights) — DL spindle detection incl. iEEG, if you
  need more than YASA's detectors.

---

## Monitor, don't build yet

- EEG foundation-model landscape (trackers: `Altaheri/Brain_Foundation_Models`,
  `Dingkun0817/EEG-FM-Benchmark`).
- 2025–2026 preprocessing pipelines with *unverified* open code (EEG-cleanse,
  CLEAN) — confirm code availability before committing.
- SpikeNet 2 — revisit if its licensing/DUA terms loosen; it's the stronger model.

---

## Dependency & licence summary

**The standing pattern is that nothing third-party lives in this repository.** A module here is a wrapper: it imports its library inside the function that uses it, so the platform stays installable without it, and the operator provisions the library and any weights themselves. Under that pattern the repository redistributes no code, no models and no weights, which is what keeps a restrictive upstream licence from reaching the Apache-2.0 tree at all. Anything that would break the pattern — source copied or ported in, a library added to the pinned closure, weights committed — is an exception that needs a licence review *before* code is written, not after.

The two licence columns are separate on purpose. A permissive code licence says nothing about the weights, and SpikeNet is the case where the two diverge. "Terms not verified" means exactly that: nobody has read them yet, and doing so is part of integrating the tool.

| Phase | Tool | Code licence | Weights | How it would enter | Runs in |
|---|---|---|---|---|---|
| 1 | SpikeNet 1 | CC BY-NC / BY-ND, and the licence file and the README disagree | Restricted, on terms of their own | **Ported source — excluded**, see the note at the top of this file | — |
| 2 | autoreject | BSD-3 | n/a | Lazy optional dependency *(built)* | compute, Pyodide candidate |
| 2 | mne-icalabel (ONNX) | BSD-3 | Bundled with the package; terms not verified | Lazy optional dependency *(built)* | compute |
| 2 | python-meegkit (ZapLine) | BSD-3 | n/a | Lazy optional dependency | Pyodide or compute |
| 2 | PyPREP | MIT | n/a | Lazy optional dependency | compute |
| 3 | MNE-Features | BSD-3 | n/a | Lazy optional dependency | Pyodide or compute |
| 3 | fooof / specparam | Apache-2.0 | n/a | Lazy optional dependency | Pyodide |
| 3 | pycrostates | BSD-3 | n/a | Lazy optional dependency | compute |
| 3 | antropy | BSD-3 | n/a | Lazy optional dependency | Pyodide |
| 3 | MNE-Connectivity | BSD-3 | n/a | Lazy optional dependency | compute |
| 4 | epilepsy2bids | MIT | n/a | Lazy optional dependency | compute |
| 4 | SeizureTransformer | MIT | Open; terms not verified | Sidecar image, pulled by the operator | sidecar |
| 4 | SzCORE / timescoring | MIT | n/a | A convention, plus a lazy dependency for scoring | pattern / tooling |
| 5 | braindecode | BSD-3 | Per model, fetched by the operator; the model's terms, not braindecode's | Lazy optional dependency *(built)*, bring-your-own-weights | compute or sidecar |
| 5 | BIOT / CBraMod / EEGPT | MIT / MIT / Apache-2.0 | Open per model; terms not verified | Sidecar image | sidecar |
| ∥ | YASA | BSD-3 | Bundled with the package; terms not verified | Lazy optional dependency *(built)* | compute |
| ∥ | OpenSpindleNet | MIT | Open; terms not verified | Lazy optional dependency or sidecar | compute or sidecar |

Four rows are marked *built*: autoreject, mne-icalabel, braindecode and YASA have wrappers in the tree today. None of their libraries is in `requirements.txt` or the lock and no weights are committed, so those rows describe what ships rather than what is intended. The rest of the table is a recommendation, and the Phase-4 decision may move detectors from in-process residency to the sidecar pattern — which also puts useful distance between the platform and a copyleft upstream.
