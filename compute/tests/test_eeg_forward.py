"""Tests for compute.eeg.forward — lead field computation."""

import numpy as np
import pytest

from compute.eeg.forward import compute_eeg_lead_field

# ---------------------------------------------------------------------------
# Unit tests (no DB)
# ---------------------------------------------------------------------------


def test_basic_shape_standard_1020():
    """Standard 10-20 with fixed orientation produces the expected array shapes."""
    lf, src_pos, ch_names, _ = compute_eeg_lead_field("standard_1020", grid_resolution_mm=10.0)

    assert lf.ndim == 2
    assert lf.shape[0] == len(ch_names)
    n_src = src_pos.shape[0]
    assert lf.shape == (len(ch_names), n_src)  # n_orient=1 default
    assert src_pos.shape == (n_src, 3)
    assert len(ch_names) > 0
    assert all(isinstance(n, str) for n in ch_names)


def test_free_orientation_columns():
    """Free orientation (n_orient=3) triples the column count."""
    lf1, src1, *_ = compute_eeg_lead_field("standard_1020", grid_resolution_mm=10.0, n_orient=1)
    lf3, src3, *_ = compute_eeg_lead_field("standard_1020", grid_resolution_mm=10.0, n_orient=3)

    assert src1.shape[0] == src3.shape[0], "Same source grid regardless of orientation"
    assert lf3.shape[1] == lf1.shape[1] * 3


def test_dtype_and_contiguity():
    """Arrays must be float64 and C-contiguous (ready for tobytes())."""
    lf, src_pos, *_ = compute_eeg_lead_field("standard_1020", grid_resolution_mm=10.0)

    assert lf.dtype == np.float64
    assert src_pos.dtype == np.float64
    assert lf.flags["C_CONTIGUOUS"]
    assert src_pos.flags["C_CONTIGUOUS"]


def test_src_pos_inside_sphere():
    """All source positions must lie within the sphere."""
    sphere_radius = 0.09
    sphere_center = np.array([0.0, 0.0, 0.04])
    _, src_pos, *_ = compute_eeg_lead_field(
        "standard_1020",
        grid_resolution_mm=10.0,
        sphere_radius_m=sphere_radius,
        sphere_center_m=tuple(sphere_center),
    )

    distances = np.linalg.norm(src_pos - sphere_center, axis=1)
    assert np.all(distances < sphere_radius), (
        f"Some source positions lie outside the sphere: max dist = {distances.max():.4f} m"
    )


def test_invalid_montage_raises():
    with pytest.raises(ValueError, match="Unknown MNE montage"):
        compute_eeg_lead_field("not_a_real_montage_xyz")


def test_invalid_n_orient_raises():
    with pytest.raises(ValueError, match="n_orient must be"):
        compute_eeg_lead_field("standard_1020", n_orient=2)


def test_finer_resolution_more_sources():
    """A finer grid should produce strictly more source points."""
    _, src_coarse, *_ = compute_eeg_lead_field("standard_1020", grid_resolution_mm=10.0)
    _, src_fine, *_ = compute_eeg_lead_field("standard_1020", grid_resolution_mm=7.5)

    assert src_fine.shape[0] > src_coarse.shape[0]


def test_lead_field_is_finite():
    """No NaN / Inf entries in a normal compute."""
    lf, src_pos, _, _ = compute_eeg_lead_field("standard_1020", grid_resolution_mm=10.0)
    assert np.isfinite(lf).all()
    assert np.isfinite(src_pos).all()


def test_dropped_count_surfaces_for_singular_grid():
    """10 mm grid through (0,0,0.04) hits the centre singularity — count is reported."""
    lf, src_pos, _, n_dropped = compute_eeg_lead_field("standard_1020", grid_resolution_mm=10.0)
    assert n_dropped >= 1
    assert lf.shape[1] == src_pos.shape[0]  # post-filter, count matches


def test_dropped_count_zero_on_clean_grid():
    """7.5 mm grid avoids the centre singularity — count is zero."""
    _, _, _, n_dropped = compute_eeg_lead_field("standard_1020", grid_resolution_mm=7.5)
    assert n_dropped == 0


def test_roundtrip_bytes():
    """tobytes() / frombuffer() round-trip must reproduce the original array exactly."""
    lf, src_pos, *_ = compute_eeg_lead_field("standard_1020", grid_resolution_mm=10.0)

    lf_rt = np.frombuffer(lf.tobytes(), dtype=np.float64).reshape(lf.shape)
    sp_rt = np.frombuffer(src_pos.tobytes(), dtype=np.float64).reshape(src_pos.shape)

    np.testing.assert_array_equal(lf, lf_rt)
    np.testing.assert_array_equal(src_pos, sp_rt)
