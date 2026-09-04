"""EEG forward model computation using MNE.

This module runs on the Django server (full Python environment with compiled
extensions).  It must not be imported in Pyodide / browser contexts.

The public entry point is ``compute_eeg_lead_field``, which:

1.  Creates an ``mne.Info`` object from the requested standard montage.
2.  Builds a spherical head model via ``mne.make_sphere_model``.
3.  Sets up a volume source space on a regular grid inside the sphere via
    ``mne.setup_volume_source_space``.
4.  Computes the EEG forward solution via ``mne.make_forward_solution``
    using the analytical sphere formula (no OpenMEEG / MNE-C required).
5.  Returns the lead field matrix ``L`` of shape
    ``(n_channels, n_sources * n_orient)`` and source positions ``src_pos``
    of shape ``(n_sources, 3)`` (metres, head coordinates).

These two arrays, along with the channel names, are stored in
``compute.models.LeadFieldCache`` and later served to the browser via the
``/compute/api/v1/`` endpoints.

Head model defaults
-------------------
The default sphere is centred 4 cm above the origin and has a radius of
9 cm — a widely used approximation for adult human scalp EEG.  These can
be overridden per call for paediatric data or unusual cap placements.

Orientation convention
----------------------
``n_orient=1`` (default): fixed orientation — the source dipole is
constrained to point radially outward from the sphere centre.  This is
the standard choice for sLORETA / eLORETA / dSPM.

``n_orient=3``: free orientation — three orthogonal components (x, y, z)
per source.  The lead field has ``n_sources * 3`` columns.  Use this
for unconstrained dipole fitting.
"""

from __future__ import annotations

import logging

import mne
import numpy as np

logger = logging.getLogger(__name__)


def compute_eeg_lead_field(
    montage_name: str,
    *,
    grid_resolution_mm: float = 7.5,
    n_orient: int = 1,
    sphere_radius_m: float = 0.09,
    sphere_center_m: tuple[float, float, float] = (0.0, 0.0, 0.04),
    mindist_mm: float = 5.0,
) -> tuple[np.ndarray, np.ndarray, list[str], int]:
    """Compute the EEG lead field matrix for a standard montage.

    Parameters
    ----------
    montage_name:
        Name accepted by ``mne.channels.make_standard_montage()``,
        e.g. ``'standard_1020'``, ``'biosemi64'``, ``'GSN-HydroCel-128'``.
    grid_resolution_mm:
        Spacing of the source grid in millimetres.  Smaller values give
        more spatial resolution but increase computation time and matrix
        size.  Typical values: 5–10 mm.
    n_orient:
        ``1`` — fixed (radial) orientation; ``3`` — free (x/y/z) orientation.
    sphere_radius_m:
        Outer radius of the spherical head model in metres.
    sphere_center_m:
        ``(x, y, z)`` centre of the sphere in metres, head coordinates.
        Default ``(0, 0, 0.04)`` places the centre 4 cm above the origin.
    mindist_mm:
        Sources closer than this distance (in mm) to the sphere surface are
        excluded, avoiding near-singularities in the forward solution.

    Returns
    -------
    lead_field:
        Float64 array of shape ``(n_channels, n_sources * n_orient)``,
        C-contiguous, in SI units (V/Am).
    src_pos:
        Float64 array of shape ``(n_sources, 3)``, C-contiguous, in metres.
    channel_names:
        Ordered list of EEG channel name strings matching the rows of
        ``lead_field``.
    n_dropped_singular:
        Count of source grid points that were dropped because their lead-field
        columns came out as NaN / Inf (sphere-centre coordinate singularity).
        Zero on a clean compute. Callers should surface a non-zero value to
        the operator — see [compute/README.md](../README.md).

    Raises
    ------
    ValueError
        If ``montage_name`` is not recognised by MNE, or if ``n_orient`` is
        not 1 or 3.
    RuntimeError
        If MNE's forward computation fails for any other reason.
    """
    if n_orient not in (1, 3):
        raise ValueError(f"n_orient must be 1 or 3, got {n_orient}.")

    logger.info(
        "Computing EEG lead field: montage=%s res=%.1fmm orient=%d",
        montage_name,
        grid_resolution_mm,
        n_orient,
    )

    # ------------------------------------------------------------------
    # 1. Electrode positions
    # ------------------------------------------------------------------
    try:
        montage = mne.channels.make_standard_montage(montage_name)
    except ValueError as exc:
        raise ValueError(
            f"Unknown MNE montage '{montage_name}'. Run mne.channels.get_builtin_montages() for the full list."
        ) from exc

    ch_names: list[str] = list(montage.ch_names)
    info = mne.create_info(ch_names=ch_names, sfreq=1.0, ch_types="eeg")
    # set_montage applies electrode positions in head coordinates.
    info.set_montage(montage)

    # ------------------------------------------------------------------
    # 2. Spherical head model
    # ------------------------------------------------------------------
    sphere = mne.make_sphere_model(
        r0=sphere_center_m,
        head_radius=sphere_radius_m,
        verbose=False,
    )

    # ------------------------------------------------------------------
    # 3. Volume source space on a regular grid inside the sphere
    # ------------------------------------------------------------------
    # setup_volume_source_space expects pos in mm and the sphere tuple as
    # (x_m, y_m, z_m, r_m) — all in metres for sphere_units='m'.
    sphere_tuple = (*sphere_center_m, sphere_radius_m)
    src = mne.setup_volume_source_space(
        subject=None,
        pos=grid_resolution_mm,  # mm
        sphere=sphere_tuple,
        sphere_units="m",
        # mindist here = minimum distance from each grid point to the sphere
        # boundary; grid points closer than this are dropped from the source
        # space. Distinct from make_forward_solution's mindist below.
        mindist=mindist_mm,
        verbose=False,
    )

    # ------------------------------------------------------------------
    # 4. Forward solution
    # ------------------------------------------------------------------
    # For a sphere model the sphere is already defined in head coordinates,
    # so an identity head→MRI transform is correct.
    trans = mne.Transform(fro="head", to="mri")

    fwd = mne.make_forward_solution(
        info=info,
        trans=trans,
        src=src,
        bem=sphere,
        eeg=True,
        meg=False,
        # mindist here = minimum source-to-sensor distance enforced during the
        # forward computation; sources too close to any sensor are excluded
        # from this call to avoid near-singularities. Same numerical value as
        # the source-space mindist above, but different semantics.
        mindist=mindist_mm,
        verbose=False,
    )

    # ------------------------------------------------------------------
    # 5. Orientation constraint
    # ------------------------------------------------------------------
    if n_orient == 1:
        # Fixed (radial) orientation: force_fixed collapses each triplet
        # of columns to a single column.
        fwd = mne.convert_forward_solution(
            fwd,
            surf_ori=False,
            force_fixed=True,
            verbose=False,
        )

    # ------------------------------------------------------------------
    # 6. Extract arrays
    # ------------------------------------------------------------------
    # fwd['sol']['data'] shape: (n_channels, n_sources * n_orient)
    lead_field: np.ndarray = np.ascontiguousarray(fwd["sol"]["data"], dtype=np.float64)

    # Source positions from the (single) volume source space.
    # fwd['src'][0]['rr']   — all candidate grid positions (metres)
    # fwd['src'][0]['inuse'] — boolean mask of actually used sources
    src_vol = fwd["src"][0]
    used_mask = src_vol["inuse"].astype(bool)
    src_pos: np.ndarray = np.ascontiguousarray(src_vol["rr"][used_mask], dtype=np.float64)

    n_sources = src_pos.shape[0]
    expected_cols = n_sources * n_orient
    if lead_field.shape != (len(ch_names), expected_cols):
        raise RuntimeError(
            f"Unexpected lead field shape {lead_field.shape}; expected ({len(ch_names)}, {expected_cols})."
        )

    # Singular-source filter: a grid point that lands on (or near) the sphere
    # centre is a coordinate singularity of the analytical sphere formula —
    # MNE's _compute_forward divides by zero there and the corresponding
    # column(s) come out as NaN / Inf. This is a property of the analytical
    # model (Sarvas 1987 et al.), not an MNE bug; BEM pipelines do not see
    # it because realistic head meshes exclude the centre implicitly.
    # We drop affected sources rather than caching poisoned bytes.
    finite_per_col = np.isfinite(lead_field).all(axis=0)
    if n_orient == 1:
        finite_per_source = finite_per_col
    else:
        finite_per_source = finite_per_col.reshape(-1, n_orient).all(axis=1)
    n_dropped = int((~finite_per_source).sum())
    if n_dropped:
        logger.warning(
            "Dropping %d source(s) with non-finite lead-field entries (likely sphere-centre singularity at r0=%s).",
            n_dropped,
            sphere_center_m,
        )
        keep_cols = finite_per_source if n_orient == 1 else np.repeat(finite_per_source, n_orient)
        lead_field = np.ascontiguousarray(lead_field[:, keep_cols])
        src_pos = np.ascontiguousarray(src_pos[finite_per_source])
        n_sources = src_pos.shape[0]

    # Final safety net: should never trip after the filter, but if MNE ever
    # produces a non-finite value through a path the filter doesn't cover
    # we fail loudly rather than cache it.
    if not np.isfinite(lead_field).all():
        raise RuntimeError("Lead field contains non-finite values (NaN or Inf) after filtering; refusing to cache.")

    logger.info(
        "Lead field computed: %d channels × %d sources (orient=%d).",
        len(ch_names),
        n_sources,
        n_orient,
    )

    return lead_field, src_pos, ch_names, n_dropped
