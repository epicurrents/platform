"""Serialisation for static, PWA-cacheable lead fields.

**Format (design decision — see the ROADMAP entry).** A static lead-field file is the
raw concatenated ``float64`` blob — *byte-identical* to the API's
``/eeg/leadfield/{montage}/data/`` response body — and everything a client needs to
slice it (shape, channel names, section byte-lengths) is carried in the
``manifest.json`` the client fetches anyway.

Why the raw blob rather than a self-describing container (e.g. npz): the viewer
fetches and slices the lead field in **JavaScript** (``Float64Array`` views) before
handing the arrays to Pyodide for the source-localisation math. A raw blob is trivial
to slice in JS — and keeps the door open to using lead fields directly JS-side in
future — whereas an npz would need a zip/`.npy` parser in the browser. The manifest
supplies what the ``/data/`` endpoint otherwise conveys via HTTP headers, which a
static file cannot carry.

**Content addressing.** :func:`leadfield_content_hash` hashes the arrays + identifying
params (not any envelope), so a recomputation — an MNE upgrade, a sphere-parameter
change, a fix to the singular-source filter — yields a new hash, a new filename, and a
service-worker cache miss that fetches fresh rather than serving stale bytes.
"""

from __future__ import annotations

import hashlib
import json

import numpy as np

#: Bump when the manifest entry shape changes incompatibly.
MANIFEST_FORMAT_VERSION = 1


def serialize_leadfield_blob(lead_field, src_pos) -> bytes:
    """Raw little-endian ``float64`` bytes: ``lead_field`` then ``src_pos``, C-order.

    Identical to the ``/data/`` endpoint body, so the static files and the API share
    one byte layout — a client's slicing code works for both, differing only in where
    it reads the section lengths (manifest for static, headers for the API).
    """
    lf = np.ascontiguousarray(lead_field, dtype="<f8")
    sp = np.ascontiguousarray(src_pos, dtype="<f8")
    return lf.tobytes() + sp.tobytes()


def _identity_payload(
    *,
    montage_name,
    n_orient,
    grid_resolution_mm,
    sphere_radius_m,
    sphere_center_m,
    channel_names,
) -> dict:
    return {
        "montage_name": str(montage_name),
        "n_orient": int(n_orient),
        "grid_resolution_mm": float(grid_resolution_mm),
        "sphere_radius_m": float(sphere_radius_m),
        "sphere_center_m": [float(x) for x in sphere_center_m],
        "channel_names": list(channel_names),
    }


def leadfield_content_hash(
    *,
    lead_field,
    src_pos,
    montage_name,
    n_orient,
    grid_resolution_mm,
    sphere_radius_m,
    sphere_center_m,
    channel_names,
    length: int = 12,
) -> str:
    """Deterministic content hash over the arrays + identity — the cache-busting key."""
    h = hashlib.sha256()
    h.update(np.ascontiguousarray(lead_field, dtype="<f8").tobytes())
    h.update(np.ascontiguousarray(src_pos, dtype="<f8").tobytes())
    h.update(
        json.dumps(
            _identity_payload(
                montage_name=montage_name,
                n_orient=n_orient,
                grid_resolution_mm=grid_resolution_mm,
                sphere_radius_m=sphere_radius_m,
                sphere_center_m=sphere_center_m,
                channel_names=channel_names,
            ),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    return h.hexdigest()[:length]


def build_manifest_entry(
    *,
    lead_field,
    src_pos,
    montage_name,
    n_orient,
    grid_resolution_mm,
    sphere_radius_m,
    sphere_center_m,
    channel_names,
    content_hash,
    filename,
    url,
) -> dict:
    """The manifest record for one lead field — everything a client needs to fetch
    and slice the blob without an HTTP header or a metadata API call.

    ``lead_field_bytes`` / ``src_pos_bytes`` are the section lengths: a client slices
    ``blob[0 : lead_field_bytes]`` → lead field ``(n_channels, n_sources*n_orient)``
    and ``blob[lead_field_bytes : ]`` → src_pos ``(n_sources, 3)``, both little-endian
    ``float64``.
    """
    lf = np.ascontiguousarray(lead_field, dtype="<f8")
    sp = np.ascontiguousarray(src_pos, dtype="<f8")
    return {
        "montage_name": str(montage_name),
        "n_orient": int(n_orient),
        "grid_resolution_mm": float(grid_resolution_mm),
        "sphere_radius_m": float(sphere_radius_m),
        "sphere_center_m": [float(x) for x in sphere_center_m],
        "n_channels": int(lf.shape[0]),
        "n_sources": int(sp.shape[0]),
        "channel_names": list(channel_names),
        "lead_field_bytes": int(lf.nbytes),
        "src_pos_bytes": int(sp.nbytes),
        "content_hash": str(content_hash),
        "file": str(filename),
        "url": str(url),
        "size_bytes": int(lf.nbytes + sp.nbytes),
    }
