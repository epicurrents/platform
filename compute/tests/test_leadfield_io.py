"""Tests for the static lead-field serialisation (compute/eeg/leadfield_io.py).

Pure numpy — no Django. Covers the raw-blob layout (a JS client slices it with the
manifest's section byte-lengths), the manifest entry, and the content-addressing
invariants: the hash is stable for identical input and changes when the bytes or the
identifying params change.
"""

import numpy as np

from compute.eeg.leadfield_io import (
    build_manifest_entry,
    leadfield_content_hash,
    serialize_leadfield_blob,
)


def _sample(**over):
    kw = {
        "lead_field": np.arange(6, dtype="<f8").reshape(2, 3),
        "src_pos": np.arange(9, dtype="<f8").reshape(3, 3),
        "montage_name": "standard_1020",
        "n_orient": 1,
        "grid_resolution_mm": 7.5,
        "sphere_radius_m": 0.09,
        "sphere_center_m": (0.0, 0.0, 0.04),
        "channel_names": ["Fp1", "Fp2"],
    }
    kw.update(over)
    return kw


def test_blob_is_leadfield_then_srcpos_and_slices_by_length():
    s = _sample()
    blob = serialize_leadfield_blob(s["lead_field"], s["src_pos"])
    lf_bytes = s["lead_field"].nbytes
    assert len(blob) == lf_bytes + s["src_pos"].nbytes
    # Slice exactly as a client would, using the section length.
    lf = np.frombuffer(blob[:lf_bytes], dtype="<f8").reshape(2, 3)
    sp = np.frombuffer(blob[lf_bytes:], dtype="<f8").reshape(3, 3)
    assert np.array_equal(lf, s["lead_field"])
    assert np.array_equal(sp, s["src_pos"])


def test_manifest_entry_carries_everything_to_slice_the_blob():
    s = _sample()
    entry = build_manifest_entry(content_hash="deadbeef0000", filename="f.bin", url="/static/leadfields/f.bin", **s)
    assert entry["n_channels"] == 2
    assert entry["n_sources"] == 3
    assert entry["channel_names"] == ["Fp1", "Fp2"]
    assert entry["lead_field_bytes"] == s["lead_field"].nbytes
    assert entry["src_pos_bytes"] == s["src_pos"].nbytes
    assert entry["size_bytes"] == entry["lead_field_bytes"] + entry["src_pos_bytes"]
    assert entry["content_hash"] == "deadbeef0000"
    assert entry["file"] == "f.bin"


def test_content_hash_is_deterministic_and_short():
    a = leadfield_content_hash(**_sample())
    b = leadfield_content_hash(**_sample())
    assert a == b
    assert len(a) == 12


def test_content_hash_changes_with_bytes_and_params():
    base = _sample()
    h0 = leadfield_content_hash(**base)
    assert leadfield_content_hash(**_sample(lead_field=base["lead_field"] + 1.0)) != h0
    assert leadfield_content_hash(**_sample(grid_resolution_mm=5.0)) != h0
    assert leadfield_content_hash(**_sample(channel_names=["Fp1", "Fp2", "Cz"])) != h0
