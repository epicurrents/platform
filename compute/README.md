# compute

Server-side scientific Python services for work that cannot reasonably run client-side — either because it needs native compiled extensions not available in Pyodide, or because the result is too large or too parameter-dependent to bundle with the viewer as a static asset. The app stores pre-computed results in cache models and serves them to the frontend on demand.

## When to put something here

Numerical work that meets all of the following belongs in `compute/`:

- Needs MNE-C, LAPACK / BLAS, OpenMEEG, or any other compiled scientific extension that is not available in Pyodide.
- The result is expensive to recompute but small enough to cache as a row in PostgreSQL (single-digit MB for typical use, ~10 MB upper bound — see [Storage](#storage) below).
- The same result is reused across many client sessions.
- Pre-bundling the result with the viewer library or shipping it as a separate static asset is not viable — the result is too large, too rarely used, or has too many parameter variations to ship every variant in advance.

Pure-Python or Pyodide-compatible computation should run in the browser (see the viewer's Pyodide service); ad-hoc one-offs belong in a management command, not here.

## Non-commercial feature gate

Some features in this app consume material licensed for **non-commercial use
only** — operator-provisioned model weights whose licence says so, for
instance. None of that material
is in this repository: the platform ships the mechanism, the operator provisions
the licensed artefact (see
[Integrating an ML model-analysis workflow](#integrating-an-ml-model-analysis-workflow)).
The features are **disabled by default** and unlock only when a deployment
explicitly declares non-commercial use via the ``EPICURRENTS_NONCOMMERCIAL_USE``
setting (env-driven; see `.env.example`).

The design is opt-in-by-assertion with a default of *off*, so a downstream —
possibly commercial — fork never inherits these features by accident; enabling
them is a deliberate act by someone declaring the deployment non-commercial.
**The flag is a compliance safeguard and an intent declaration, not a licence**:
it grants no commercial right, and "commercial" follows the Creative Commons
NonCommercial standard (a cost-recovery fee alone is not commercial).

The registry and helpers live in [`compute/licensing.py`](licensing.py):
`require_noncommercial(feature)` raises `NonCommercialFeatureDisabled` unless the
flag is set. The authoritative gate is at each feature's model/artefact loader
(hardest to bypass); management commands, Celery tasks, and API routes add
earlier, friendlier checks (a `CommandError` / clean `403`). When you add another
non-commercially-licensed capability, add one line to `NONCOMMERCIAL_FEATURES`
and call `require_noncommercial(...)` at its point of use. Gate only the step
that touches the licensed artefact, never a licence-clean fallback that computes
the same thing from public data.

## Current modules

### `compute.eeg`

EEG-specific server-side computation.

| Submodule | Public entry point | Purpose |
|---|---|---|
| [forward.py](eeg/forward.py) | `compute_eeg_lead_field()` | Build an EEG lead field matrix (forward solution) from a standard electrode montage and a spherical head model. Used by the browser-side source-localisation Pyodide script (sLORETA / eLORETA / dipole fit). |

The forward solution uses the analytical sphere formula in MNE, so no OpenMEEG / MNE-C compilation is required at runtime. Default sphere: 9 cm radius, centre 4 cm above origin — a standard adult-scalp approximation. Override per call for paediatric or unusual cap geometries.

**Singular-source filter.** A source grid point that lands exactly on (or near) the sphere centre is a coordinate singularity of the analytical formula — MNE divides by zero there and the corresponding lead-field column comes out as NaN / Inf. This is a property of the analytical model (Sarvas 1987 et al.), not an MNE bug; realistic BEM head models do not see it. `compute_eeg_lead_field` filters out affected sources after the forward solution and logs a warning with the count dropped, so the cached bytes never contain non-finite values. This most commonly trips when `grid_resolution_mm` lands on an integer multiple that coincides with the sphere centre (e.g. 10 mm spacing through `(0, 0, 0.04)`).

Orientation:

- `n_orient=1` (default) — fixed (radial) orientation; standard for sLORETA / eLORETA / dSPM.
- `n_orient=3` — free orientation; three orthogonal components per source. Use for unconstrained dipole fitting.

## Models

### `LeadFieldCache`

One row per `(montage_name, n_orient, grid_resolution_mm)` combination — those three fields form the unique constraint. A different orientation choice or grid resolution for the same montage is stored as a separate row.

Identity:

| Field | Type | Notes |
|---|---|---|
| `montage_name` | `CharField(128)` | MNE standard montage name, e.g. `'standard_1020'`, `'biosemi64'`, `'GSN-HydroCel-128'`. Must be accepted by `mne.channels.make_standard_montage()`. |
| `n_orient` | `PositiveSmallIntegerField` | `1` = fixed (radial), `3` = free (x/y/z). |
| `grid_resolution_mm` | `FloatField` | Source grid spacing in mm. Default `7.5`. |

Shape:

| Field | Notes |
|---|---|
| `n_channels` | Rows of the lead field matrix. |
| `n_sources` | Source grid points (columns ÷ `n_orient`). |

Head model:

| Field | Notes |
|---|---|
| `sphere_radius_m` | Default `0.09` (9 cm). |
| `sphere_center_m` | `[x, y, z]` in metres, head coordinates. Default `[0, 0, 0.04]`. |

Channels and binary data:

| Field | Type | Notes |
|---|---|---|
| `channel_names` | `JSONField` | Ordered channel name list — index matches matrix rows. |
| `lead_field` | `BinaryField` | Raw little-endian float64 bytes, shape `(n_channels, n_sources * n_orient)`, C order. |
| `src_pos` | `BinaryField` | Raw little-endian float64 bytes, shape `(n_sources, 3)`, C order, metres. |

Timestamps:

| Field | Notes |
|---|---|
| `created_at` | Row creation time. Preserved across `force=true` replacements. |
| `updated_at` | Last recompute time. Advances each time the lead field is replaced. |

## API endpoints

Mounted at `/compute/api/v1/` (see [compute/urls.py](urls.py)).

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `GET` | `/eeg/leadfield/` | Staff | List all cached entries (metadata only). |
| `POST` | `/eeg/leadfield/` | Staff (fresh) / Superuser (`force=true`) | Trigger a synchronous computation and cache the result. `201` on fresh create, `200` on `force=true` replace, `409` if the entry exists and `force=false`. Staff may create new entries; replacing an existing entry requires superuser. |
| `GET` | `/eeg/leadfield/{montage_name}/` | Authenticated | Metadata for one entry. Query params: `n_orient`, `grid_resolution_mm`. |
| `GET` | `/eeg/leadfield/{montage_name}/data/` | Authenticated | Binary download (`application/octet-stream`) of the lead field + source positions. |

### Binary response layout

The data endpoint returns two concatenated float64 arrays. Shape and section boundaries are in response headers, not in the body:

```
bytes 0 .. X-LeadField-Bytes - 1   →  lead field, (n_channels, n_sources * n_orient)
bytes X-LeadField-Bytes .. end     →  src_pos,    (n_sources, 3)
```

Response headers:

| Header | Type |
|---|---|
| `X-N-Channels` | int |
| `X-N-Sources` | int |
| `X-N-Orient` | int (`1` or `3`) |
| `X-LeadField-Bytes` | int — byte length of the lead field section |
| `X-SrcPos-Bytes` | int — byte length of the src_pos section |

`Access-Control-Expose-Headers` is set so a cross-origin `fetch` can read them.

### Browser consumption example

```js
const resp = await fetch('/compute/api/v1/eeg/leadfield/standard_1020/data/')
const nCh  = parseInt(resp.headers.get('X-N-Channels'))
const nSrc = parseInt(resp.headers.get('X-N-Sources'))
const nOri = parseInt(resp.headers.get('X-N-Orient'))
const lfLen = parseInt(resp.headers.get('X-LeadField-Bytes')) / 8
const buf  = await resp.arrayBuffer()
const leadField = new Float64Array(buf, 0, lfLen)
const srcPos    = new Float64Array(buf, lfLen * 8, nSrc * 3)
```

## Management command

### `compute_leadfield`

Pre-compute one or more lead field matrices and cache them. Use this for bulk pre-warming and for CI fixtures; the API endpoint covers the on-demand case.

```bash
# Default — fixed orientation, 7.5 mm grid
docker compose run --rm --no-deps web python manage.py compute_leadfield standard_1020

# Free orientation at a finer grid
docker compose run --rm --no-deps web python manage.py compute_leadfield biosemi64 --n-orient 3 --grid-resolution-mm 5

# Several montages in one invocation
docker compose run --rm --no-deps web python manage.py compute_leadfield standard_1020 biosemi64 GSN-HydroCel-128

# Replace an existing row (e.g. after an MNE upgrade)
docker compose run --rm --no-deps web python manage.py compute_leadfield standard_1020 --force
```

Arguments:

| Flag | Default | Notes |
|---|---|---|
| `MONTAGE [MONTAGE …]` | — | One or more standard montage names. |
| `--grid-resolution-mm MM` | `7.5` | Source grid spacing. |
| `--n-orient {1,3}` | `1` | Fixed (1) vs free (3) orientation. |
| `--sphere-radius-m R` | `0.09` | Spherical head model radius. |
| `--sphere-center-m X Y Z` | `0 0 0.04` | Sphere centre in head coordinates. |
| `--force` | off | Replace an existing matching row instead of skipping. |

Always run inside the Docker stack so the command writes to PostgreSQL, not the host SQLite dev database.

## Storage

For typical clinical EEG montages (19–64 channels, 200–2 000 source points) each row is well under 1 MB. High-density caps (256 channels, ~5 000 source points, free orientation) can reach ~10 MB — still within `bytea` limits. If a future module needs more, migrate the binary fields from `BinaryField` to `FileField` backed by a configurable storage backend.

## Integrating an ML model-analysis workflow

The platform's stance on ML detectors: **it ships the mechanism and the
instructions; the operator ships the model.** Model weights and model code are
routinely under restrictive terms (CC BY-NC / BY-ND, DUA-gated downloads), and
an Apache-2.0 repository cannot carry them — so no workflow may vendor licensed
artefacts, and every workflow must fail with actionable provisioning
instructions when its artefact is absent. Two provisioning difficulty levels
turn up in practice: openly downloadable weights under a restrictive licence, and
DUA-gated weights alongside model code that cannot be vendored. The in-tree
runtime that follows this pattern is [`braindecode/`](braindecode/README.md).

The pattern, in the order you build it:

1. **Admission test.** Confirm the work belongs in this tier at all — the
   checklist under [When to put something here](#when-to-put-something-here).
   A model that runs comfortably in Pyodide belongs in the browser.
2. **Licensing due diligence, first not last.** Establish what the code licence
   and the weights licence each permit *before* writing the wrapper — they
   often differ (CC BY-NC code with DUA-gated weights is a common pairing). Anything
   non-commercial gets a line in `NONCOMMERCIAL_FEATURES`
   ([licensing.py](licensing.py)) and a `require_noncommercial(...)` call at
   its loader — the authoritative gate sits where the artefact is loaded, with
   friendlier early checks in commands / tasks / routes.
3. **Split core from loader.** A dependency-light numerical core with no
   Django and no ML-framework imports (testable with a stub predictor), plus a
   lazy loader that resolves the operator-supplied artefact path from a
   `<MODEL>_DIR` / `<MODEL>_CHECKPOINT` setting-or-env and imports the heavy
   framework only when called.
4. **A host-safe management command** (file in → CSV out) so the pipeline can
   be exercised and validated on real recordings before any server wiring.
5. **Result caching.** Inference is expensive and reused: cache per-recording
   results in a `*Cache` model with a real `ForeignKey` to
   `recordings.Recording` (cascade, audit, and erasure then behave; a string
   UID orphans silently) and a unique constraint on the parameter set that
   changes the output. Mirror `LeadFieldCache` for the binary payload layout.
6. **Async dispatch.** A detector run is too slow for a request thread — the
   trigger endpoint enqueues a Celery task and answers 202 + task id, unlike
   the synchronous lead-field trigger; metadata and binary results are then
   served from the cache. The task opens an audited scope
   (`with_system_activity`) like any background writer.
7. **A processor-contract mapping.** Long-term, detectors conform to the
   analysis-processor contract in [contract.py](contract.py) — a pure
   `(SignalWindow, RunContext) -> AnalysisOutput` — so the execution pipeline
   can schedule them without knowing their internals. New workflows should
   state their mapping onto it even while integrating ahead of that pipeline.
8. **Validation gates before trust.** Channel order / montage parity against a
   reference run, filter parity, an operating point derived from your own
   labelled data — written down per-module (see
   [`braindecode/README.md` → Validation gates](braindecode/README.md)) and
   cleared before any output reaches a clinician.

Steps 1–4 are what [`braindecode/`](braindecode/README.md) ships today; 5–7 are
sketched in its "Serving path" section for whoever takes a workflow to always-on.

## Adding a new compute module

1. **Create a sub-package** under `compute/` (e.g. `compute/emg/`) with the module that contains the computation. Keep it dependency-light — no Django imports in the numerical core.
2. **Decide whether to cache.** If the result is reused, add a `*Cache` model in [compute/models.py](models.py) with a unique constraint on the parameter set that identifies a result. Store binary data as raw little-endian float64 bytes plus a `channel_names` / shape list in JSON; document the layout in the model docstring.
3. **Add an endpoint** under [compute/api/v1/ninja.py](api/v1/ninja.py). Follow the lead-field pattern: list (staff only), trigger (staff only, optional `force` flag), metadata (authenticated), binary download (authenticated). Set `Access-Control-Expose-Headers` for any custom `X-*` headers.
4. **Add a management command** for bulk pre-warming. Use the same defaults as the API. Always invoke via `docker compose run` in user-facing instructions.
5. **Tests** go in [compute/tests/](tests/) — see [test_eeg_forward.py](tests/test_eeg_forward.py) and [test_api.py](tests/test_api.py) for the patterns to mirror.

## Tests

```bash
pytest compute/tests/
```

`test_eeg_forward.py` exercises the numerical pipeline with a small montage. `test_api.py` covers the endpoint surface, including the staff/auth gates and the 409-on-duplicate behaviour.

## Dependencies

This app pulls in `mne` and its scientific stack (`numpy`, `scipy`), listed in [requirements.txt](../requirements.txt). The Pyodide-side scripts in the viewer reach this app only through the API.
